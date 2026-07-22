# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the session-fetch orchestrator.

Playwright and auth/api modules are mocked — no browser is launched.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

import session_fetch
from session_fetch import (
    _extract_cases,
    _hold_browser_open,
    _now_iso_utc,
    append_case_snapshot,
    cmd_extract,
    cmd_login,
    cmd_run,
    load_auth,
    load_config,
    case_log_file_for,
    main,
)


# -------- small helpers --------------------------------------------------

def test_now_iso_utc_is_z_suffixed_and_second_precision():
    s = _now_iso_utc()
    assert s.endswith("Z")
    # No microseconds
    assert "." not in s
    # Shape YYYY-MM-DDTHH:MM:SSZ
    assert len(s) == 20


def test_case_log_file_for_parses_form_number():
    assert case_log_file_for("I-485").name == "485_case.json"
    assert case_log_file_for("I485").name == "485_case.json"
    assert case_log_file_for("Form I-131").name == "131_case.json"


def test_case_log_file_for_raises_on_unrecognized_form():
    with pytest.raises(ValueError):
        case_log_file_for("bogus")


# -------- load_config / load_auth ---------------------------------------

def test_load_config_reads_json(tmp_path, monkeypatch):
    cfg = {"auth": {"uscis_email": "e"}, "cases": []}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setattr(session_fetch, "CONFIG_PATH", p)
    assert load_config() == cfg


def test_load_auth_accepts_explicit_config():
    cfg = {
        "auth": {
            "uscis_email": "e",
            "uscis_password": "p",
            "uscis_mfa_email": "g",
            "uscis_mfa_app_password": "a",
        }
    }
    auth = load_auth(cfg)
    assert auth["uscis_email"] == "e"
    assert set(auth.keys()) == set(session_fetch.REQUIRED_AUTH_KEYS)


def test_load_auth_missing_keys_raises_system_exit():
    with pytest.raises(SystemExit):
        load_auth({"auth": {"uscis_email": "e"}})


def test_load_auth_reads_config_when_none_passed(tmp_path, monkeypatch):
    cfg = {
        "auth": {
            "uscis_email": "e", "uscis_password": "p",
            "uscis_mfa_email": "g", "uscis_mfa_app_password": "a",
        }
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setattr(session_fetch, "CONFIG_PATH", p)
    # Pass config=None so it reads from disk.
    result = load_auth()
    assert result["uscis_email"] == "e"


# -------- append_case_snapshot ----------------------------------------------

def test_append_case_snapshot_creates_file_and_appends(tmp_path, monkeypatch):
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    path = append_case_snapshot("I-485", {"a": 1}, "2026-04-18T00:00:00Z")
    assert path.exists()
    data = json.loads(path.read_text())
    assert len(data) == 1
    assert data[0]["capturedAt"] == "2026-04-18T00:00:00Z"

    # Second append keeps the first.
    append_case_snapshot("I-485", {"a": 2}, "2026-04-19T00:00:00Z")
    data = json.loads(path.read_text())
    assert len(data) == 2


def test_append_case_snapshot_recovers_from_invalid_json(tmp_path, monkeypatch):
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    p = tmp_path / "485_case.json"
    p.write_text("{broken json")
    append_case_snapshot("I-485", {"ok": True}, "2026-04-18T00:00:00Z")
    data = json.loads(p.read_text())
    assert len(data) == 1


def test_append_case_snapshot_recovers_from_non_list_json(tmp_path, monkeypatch):
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    p = tmp_path / "485_case.json"
    p.write_text('{"not": "a list"}')
    append_case_snapshot("I-485", {"ok": True}, "2026-04-18T00:00:00Z")
    data = json.loads(p.read_text())
    assert isinstance(data, list)
    assert len(data) == 1


# -------- _extract_cases ------------------------------------------------

def test_extract_cases_unexpected_error_is_isolated_to_one_case(monkeypatch, tmp_path):
    """A generic exception in fetch_case (TypeError from bad JSON,
    KeyError, Playwright Error from a dead tab) must NOT kill the
    whole pull. It must emit case_fetch_unexpected_error with a
    traceback and continue to the next case."""
    import system_log
    monkeypatch.setattr(system_log, "LOG_PATH", tmp_path / "_syslog.json")
    system_log.clear()
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    monkeypatch.setattr(session_fetch, "ROOT", tmp_path)

    context = MagicMock()
    cases = [
        {"id": "IOE1", "label": "I-485"},  # this one crashes
        {"id": "IOE2", "label": "I-765"},  # this one must still run
    ]
    case_2_data = {"data": {"formType": "I-765", "receiptNumber": "IOE2"}}

    call_count = {"n": 0}

    def _fetch_case(tab, receipt):
        call_count["n"] += 1
        if call_count["n"] == 1:
            # Simulate a malformed-JSON parse deep in fetch_case.
            raise TypeError("'NoneType' object is not subscriptable")
        return case_2_data

    with patch.object(session_fetch, "open_worker_tab") as owt, \
         patch.object(session_fetch, "fetch_case", side_effect=_fetch_case):
        owt.return_value = MagicMock()
        failures = _extract_cases(context, cases, "2026-04-24T01:00:00Z")

    # Second case succeeded despite the first's crash.
    assert (tmp_path / "765_case.json").exists()
    assert failures == 1  # only the first counted
    # The crash was logged as case_fetch_unexpected_error with a traceback.
    unexpected = [e for e in system_log.read_all()
                  if e.get("event") == "case_fetch_unexpected_error"]
    assert len(unexpected) == 1
    ev = unexpected[0]
    assert ev["receipt"] == "IOE1"
    assert "TypeError" in ev["error"]
    assert "traceback_tail" in ev and ev["traceback_tail"]


def test_extract_cases_happy_path(tmp_path, monkeypatch):
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    monkeypatch.setattr(session_fetch, "ROOT", tmp_path)

    context = MagicMock()
    cases = [{"id": "IOE1", "label": "I-485"}]
    data = {"data": {"formType": "I-485", "receiptNumber": "IOE1"}}

    with patch.object(session_fetch, "open_worker_tab") as owt, \
         patch.object(session_fetch, "fetch_case", return_value=data):
        owt.return_value = MagicMock()
        failures = _extract_cases(context, cases, "2026-04-18T00:00:00Z")
    assert failures == 0
    assert (tmp_path / "485_case.json").exists()


def test_extract_cases_counts_api_error_as_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    context = MagicMock()
    cases = [{"id": "IOE1", "label": "I-485"}]

    from uscis_api import ApiError
    with patch.object(session_fetch, "open_worker_tab"), \
         patch.object(session_fetch, "fetch_case",
                      side_effect=ApiError("IOE1", 500, "oops")):
        failures = _extract_cases(context, cases, "2026-04-18T00:00:00Z")
    assert failures == 1


def test_extract_cases_unknown_form_counts_as_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    context = MagicMock()
    # Case has no recognizable form number in label, and response lacks formType.
    cases = [{"id": "IOE1", "label": "unknown"}]
    data = {"receiptNumber": "IOE1"}  # no formType

    with patch.object(session_fetch, "open_worker_tab"), \
         patch.object(session_fetch, "fetch_case", return_value=data):
        failures = _extract_cases(context, cases, "2026-04-18T00:00:00Z")
    assert failures == 1


def test_extract_cases_session_expired_propagates():
    from uscis_api import SessionExpired

    context = MagicMock()
    cases = [{"id": "IOE1", "label": "I-485"}]

    with patch.object(session_fetch, "open_worker_tab"), \
         patch.object(session_fetch, "fetch_case",
                      side_effect=SessionExpired("IOE1", 401, "denied")):
        with pytest.raises(SessionExpired):
            _extract_cases(context, cases, "2026-04-18T00:00:00Z")


def test_extract_cases_keep_alive_opens_new_tab_per_case(monkeypatch, tmp_path):
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    monkeypatch.setattr(session_fetch, "ROOT", tmp_path)
    context = MagicMock()
    cases = [{"id": "IOE1", "label": "I-485"}]
    data = {"formType": "I-485"}

    with patch.object(session_fetch, "fetch_case_in_new_tab",
                      return_value=(MagicMock(), data)) as fcnt, \
         patch.object(session_fetch, "open_worker_tab") as owt:
        _extract_cases(context, cases, "2026-04-18T00:00:00Z", keep_alive=True)
    fcnt.assert_called_once()
    owt.assert_not_called()


# -------- _hold_browser_open -------------------------------------------

def _args(**over):
    base = {"keep_alive": False, "headless": True}
    base.update(over)
    return SimpleNamespace(**base)


def test_hold_browser_open_noop_for_headless_and_no_keepalive():
    # Should return immediately without touching stdin or signal module.
    _hold_browser_open(_args())


def test_hold_browser_open_waits_for_enter_in_interactive_headful(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    with patch("builtins.input", return_value="") as inp:
        _hold_browser_open(_args(headless=False))
    inp.assert_called_once()


def test_hold_browser_open_eof_does_not_crash(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    with patch("builtins.input", side_effect=EOFError):
        _hold_browser_open(_args(headless=False))


def test_hold_browser_open_keep_alive_waits_for_signal(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    import signal as signal_mod
    signals_installed = []

    def _signal(sig, handler):
        signals_installed.append(sig)
        # Fire the handler so the while-loop sees stop["fired"] = True.
        handler(sig, None)

    monkeypatch.setattr(signal_mod, "signal", _signal)
    monkeypatch.setattr(signal_mod, "pause", lambda: None)

    _hold_browser_open(_args(keep_alive=True, headless=False))
    assert signal_mod.SIGINT in signals_installed
    assert signal_mod.SIGTERM in signals_installed


def test_hold_browser_open_keep_alive_handles_keyboard_interrupt(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    import signal as signal_mod
    monkeypatch.setattr(signal_mod, "signal", lambda *a, **k: None)
    monkeypatch.setattr(signal_mod, "pause", MagicMock(side_effect=KeyboardInterrupt))

    # Must not propagate — the KeyboardInterrupt breaks out of the loop.
    _hold_browser_open(_args(keep_alive=True, headless=False))


# -------- CLI subcommands (mock Playwright) ----------------------------

class _PlaywrightCtx:
    def __init__(self):
        self.browser = MagicMock()
        self.context = MagicMock()
        self.page = MagicMock()
        self.browser.new_context.return_value = self.context
        self.context.new_page.return_value = self.page

    def __enter__(self):
        pw = MagicMock()
        pw.chromium.launch.return_value = self.browser
        return pw

    def __exit__(self, *exc):
        return False


@pytest.fixture
def mock_playwright(monkeypatch):
    holder = _PlaywrightCtx()
    monkeypatch.setattr(session_fetch, "sync_playwright", lambda: holder)
    return holder


def _cfg_on_disk(tmp_path, monkeypatch, cases=None):
    cfg = {
        "auth": {
            "uscis_email": "e", "uscis_password": "p",
            "uscis_mfa_email": "g", "uscis_mfa_app_password": "a",
        },
        "cases": cases if cases is not None else [{"id": "IOE1", "label": "I-485"}],
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    monkeypatch.setattr(session_fetch, "CONFIG_PATH", p)
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path / "data")
    return cfg


def test_cmd_run_with_no_cases_returns_1(tmp_path, monkeypatch, mock_playwright):
    _cfg_on_disk(tmp_path, monkeypatch, cases=[])
    rc = cmd_run(_args())
    assert rc == 1


def test_cmd_run_happy_path(tmp_path, monkeypatch, mock_playwright):
    _cfg_on_disk(tmp_path, monkeypatch)
    with patch.object(session_fetch, "ensure_authenticated"), \
         patch.object(session_fetch, "_extract_cases", return_value=0):
        rc = cmd_run(_args())
    assert rc == 0


def test_cmd_run_wipes_storage_state_at_start(tmp_path, monkeypatch, mock_playwright):
    """Policy: every pull starts fresh. If a storage-state file exists
    from a prior cmd_login, cmd_run must delete it before the pull
    runs, so the next ensure_authenticated does a cold OIDC + MFA."""
    _cfg_on_disk(tmp_path, monkeypatch)
    # Pre-seed a session file at the path cmd_run watches.
    session_path = tmp_path / ".uscis_session.json"
    session_path.write_text('{"cookies": ["stale"]}')
    monkeypatch.setattr(session_fetch, "STORAGE_STATE_PATH", session_path)
    with patch.object(session_fetch, "ensure_authenticated"), \
         patch.object(session_fetch, "_extract_cases", return_value=0):
        rc = cmd_run(_args())
    assert rc == 0
    # The session file is gone — next pull can't accidentally reuse it.
    assert not session_path.exists()


def test_cmd_run_does_not_persist_session_at_end(
    tmp_path, monkeypatch, mock_playwright,
):
    """cmd_run must NOT call context.storage_state(path=...). Even when
    login succeeded within the pull, nothing is written to disk."""
    _cfg_on_disk(tmp_path, monkeypatch)
    with patch.object(session_fetch, "ensure_authenticated"), \
         patch.object(session_fetch, "_extract_cases", return_value=0):
        cmd_run(_args())
    mock_playwright.context.storage_state.assert_not_called()


def test_cmd_run_with_failures_returns_2(tmp_path, monkeypatch, mock_playwright):
    _cfg_on_disk(tmp_path, monkeypatch)
    with patch.object(session_fetch, "ensure_authenticated"), \
         patch.object(session_fetch, "_extract_cases", return_value=1):
        rc = cmd_run(_args())
    assert rc == 2


def test_cmd_run_reauths_after_mid_run_expiry(tmp_path, monkeypatch, mock_playwright):
    _cfg_on_disk(tmp_path, monkeypatch)

    from uscis_api import SessionExpired
    results = [SessionExpired("IOE1", 401, "stale"), 0]

    def _extract(*a, **k):
        item = results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    auth_calls = []

    def _auth_stub(*a, **k):
        auth_calls.append(k.get("allow_login"))

    with patch.object(session_fetch, "ensure_authenticated", _auth_stub), \
         patch.object(session_fetch, "_extract_cases", _extract):
        rc = cmd_run(_args())
    assert rc == 0
    assert auth_calls.count(True) == 2  # initial + re-auth


def test_cmd_login_happy_path(tmp_path, monkeypatch, mock_playwright):
    _cfg_on_disk(tmp_path, monkeypatch)
    with patch.object(session_fetch, "ensure_authenticated"):
        rc = cmd_login(_args())
    assert rc == 0


def test_cmd_login_reports_auth_error(tmp_path, monkeypatch, mock_playwright):
    _cfg_on_disk(tmp_path, monkeypatch)
    from uscis_auth import AuthError
    with patch.object(session_fetch, "ensure_authenticated",
                      side_effect=AuthError("nope")):
        rc = cmd_login(_args())
    assert rc == 1


def test_cmd_extract_requires_saved_session(tmp_path, monkeypatch, mock_playwright):
    _cfg_on_disk(tmp_path, monkeypatch)
    # Ensure the storage-state file does NOT exist.
    monkeypatch.setattr(session_fetch, "STORAGE_STATE_PATH",
                        tmp_path / "nonexistent.json")
    rc = cmd_extract(_args())
    assert rc == 1


def test_cmd_extract_no_cases_returns_1(tmp_path, monkeypatch, mock_playwright):
    _cfg_on_disk(tmp_path, monkeypatch, cases=[])
    monkeypatch.setattr(session_fetch, "STORAGE_STATE_PATH",
                        tmp_path / "nonexistent.json")
    rc = cmd_extract(_args())
    assert rc == 1


def test_cmd_extract_happy_path(tmp_path, monkeypatch, mock_playwright):
    _cfg_on_disk(tmp_path, monkeypatch)
    session_path = tmp_path / ".uscis_session.json"
    session_path.write_text("{}")
    monkeypatch.setattr(session_fetch, "STORAGE_STATE_PATH", session_path)
    with patch.object(session_fetch, "ensure_authenticated"), \
         patch.object(session_fetch, "_extract_cases", return_value=0):
        rc = cmd_extract(_args())
    assert rc == 0


def test_cmd_extract_swallows_storage_state_error(tmp_path, monkeypatch, mock_playwright):
    _cfg_on_disk(tmp_path, monkeypatch)
    session_path = tmp_path / ".uscis_session.json"
    session_path.write_text("{}")
    monkeypatch.setattr(session_fetch, "STORAGE_STATE_PATH", session_path)
    mock_playwright.context.storage_state.side_effect = RuntimeError("disk full")
    with patch.object(session_fetch, "ensure_authenticated"), \
         patch.object(session_fetch, "_extract_cases", return_value=0):
        rc = cmd_extract(_args())
    assert rc == 0


def test_cmd_extract_auth_error_returns_1(tmp_path, monkeypatch, mock_playwright):
    _cfg_on_disk(tmp_path, monkeypatch)
    session_path = tmp_path / ".uscis_session.json"
    session_path.write_text("{}")
    monkeypatch.setattr(session_fetch, "STORAGE_STATE_PATH", session_path)
    from uscis_auth import AuthError
    with patch.object(session_fetch, "ensure_authenticated",
                      side_effect=AuthError("stale")):
        rc = cmd_extract(_args())
    assert rc == 1


def test_cmd_extract_session_expired_mid_run_returns_1(
    tmp_path, monkeypatch, mock_playwright
):
    _cfg_on_disk(tmp_path, monkeypatch)
    session_path = tmp_path / ".uscis_session.json"
    session_path.write_text("{}")
    monkeypatch.setattr(session_fetch, "STORAGE_STATE_PATH", session_path)

    from uscis_api import SessionExpired
    with patch.object(session_fetch, "ensure_authenticated"), \
         patch.object(session_fetch, "_extract_cases",
                      side_effect=SessionExpired("IOE1", 401, "stale")):
        rc = cmd_extract(_args())
    assert rc == 1


def test_cmd_extract_browser_close_swallows_error(
    tmp_path, monkeypatch, mock_playwright, syslog_to_tmp,
):
    """cmd_extract's teardown must swallow browser.close errors and
    log browser_close_failed with cmd=extract (lines 715-716)."""
    _cfg_on_disk(tmp_path, monkeypatch)
    session_path = tmp_path / ".uscis_session.json"
    session_path.write_text("{}")
    monkeypatch.setattr(session_fetch, "STORAGE_STATE_PATH", session_path)
    mock_playwright.browser.close.side_effect = RuntimeError("zombie proc")
    with patch.object(session_fetch, "ensure_authenticated"), \
         patch.object(session_fetch, "_extract_cases", return_value=0):
        rc = cmd_extract(_args())
    assert rc == 0
    events = [e for e in syslog_to_tmp()
              if e["event"] == "browser_close_failed"]
    assert len(events) == 1
    assert events[0]["cmd"] == "extract"


def test_cmd_extract_failures_return_2(tmp_path, monkeypatch, mock_playwright):
    _cfg_on_disk(tmp_path, monkeypatch)
    session_path = tmp_path / ".uscis_session.json"
    session_path.write_text("{}")
    monkeypatch.setattr(session_fetch, "STORAGE_STATE_PATH", session_path)
    with patch.object(session_fetch, "ensure_authenticated"), \
         patch.object(session_fetch, "_extract_cases", return_value=1):
        rc = cmd_extract(_args())
    assert rc == 2


# -------- main() argparse dispatch -------------------------------------

def test_main_defaults_to_run(monkeypatch):
    monkeypatch.setattr("sys.argv", ["session_fetch.py"])
    with patch.object(session_fetch, "cmd_run", return_value=0) as run:
        assert main() == 0
    run.assert_called_once()


def test_main_dispatches_login(monkeypatch):
    monkeypatch.setattr("sys.argv", ["session_fetch.py", "login"])
    with patch.object(session_fetch, "cmd_login", return_value=0) as c:
        assert main() == 0
    c.assert_called_once()


def test_main_dispatches_extract(monkeypatch):
    monkeypatch.setattr("sys.argv", ["session_fetch.py", "extract"])
    with patch.object(session_fetch, "cmd_extract", return_value=0) as c:
        assert main() == 0
    c.assert_called_once()


def test_main_fetch_alias_runs_cmd_run(monkeypatch):
    monkeypatch.setattr("sys.argv", ["session_fetch.py", "fetch"])
    with patch.object(session_fetch, "cmd_run", return_value=0) as run:
        assert main() == 0
    run.assert_called_once()


def test_main_keep_alive_forces_headful(monkeypatch):
    monkeypatch.setattr("sys.argv", ["session_fetch.py", "--keep-alive"])
    captured = {}

    def _stub(args):
        captured["headless"] = args.headless
        captured["keep_alive"] = args.keep_alive
        return 0

    with patch.object(session_fetch, "cmd_run", _stub):
        main()
    assert captured["keep_alive"] is True
    assert captured["headless"] is False


def test_main_verbose_enables_debug(monkeypatch):
    monkeypatch.setattr("sys.argv", ["session_fetch.py", "-v"])
    with patch.object(session_fetch, "cmd_run", return_value=0), \
         patch.object(session_fetch.logging, "basicConfig") as bc:
        main()
    # verbose flag must flip the configured level.
    assert bc.call_args.kwargs["level"] == session_fetch.logging.DEBUG


# -------- system log instrumentation ------------------------------------

@pytest.fixture
def syslog_to_tmp(monkeypatch, tmp_path):
    import system_log
    monkeypatch.setattr(system_log, "LOG_PATH", tmp_path / "system_log.json")
    system_log.clear()
    return lambda: system_log.read_all()


def test_load_config_emits_file_not_found(monkeypatch, tmp_path, syslog_to_tmp):
    monkeypatch.setattr(session_fetch, "CONFIG_PATH", tmp_path / "missing.json")
    with pytest.raises(FileNotFoundError):
        session_fetch.load_config()
    events = [e for e in syslog_to_tmp() if e["event"] == "config_load_failed"]
    assert len(events) == 1
    assert events[0]["reason"] == "file_not_found"


def test_load_config_emits_malformed_json(monkeypatch, tmp_path, syslog_to_tmp):
    cfg = tmp_path / "config.json"
    cfg.write_text("not { valid json")
    monkeypatch.setattr(session_fetch, "CONFIG_PATH", cfg)
    with pytest.raises(json.JSONDecodeError):
        session_fetch.load_config()
    events = [e for e in syslog_to_tmp() if e["event"] == "config_load_failed"]
    assert len(events) == 1
    assert events[0]["reason"] == "malformed_json"


def test_load_auth_emits_missing_keys(syslog_to_tmp):
    with pytest.raises(SystemExit):
        session_fetch.load_auth({"auth": {"uscis_email": "e"}})
    events = [e for e in syslog_to_tmp()
              if e["event"] == "auth_config_missing_keys"]
    assert len(events) == 1
    # All three remaining required keys must be reported.
    missing = events[0]["missing"]
    assert "uscis_password" in missing
    assert "uscis_mfa_email" in missing
    assert "uscis_mfa_app_password" in missing


def test_append_case_snapshot_emits_invalid_json_warning(
    tmp_path, monkeypatch, syslog_to_tmp,
):
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    log_file = tmp_path / "485_case.json"
    log_file.write_text("not { valid json")
    session_fetch.append_case_snapshot("I-485", {"x": 1}, "2026-04-22T00:00:00Z")
    events = [e for e in syslog_to_tmp()
              if e["event"] == "snapshot_log_invalid_json"]
    assert len(events) == 1
    assert events[0]["file"] == "485_case.json"


def test_append_case_snapshot_emits_not_array_warning(
    tmp_path, monkeypatch, syslog_to_tmp,
):
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    log_file = tmp_path / "485_case.json"
    log_file.write_text('{"this": "is an object, not a list"}')
    session_fetch.append_case_snapshot("I-485", {"x": 1}, "2026-04-22T00:00:00Z")
    events = [e for e in syslog_to_tmp()
              if e["event"] == "snapshot_log_not_array"]
    assert len(events) == 1
    assert events[0]["existing_type"] == "dict"


# -------- cmd_run browser-launch / context failure branches ------------

def test_cmd_run_emits_browser_launch_failed(
    tmp_path, monkeypatch, mock_playwright, syslog_to_tmp,
):
    """chromium.launch() raising → browser_launch_failed event, then
    the exception propagates (lines 444-451)."""
    _cfg_on_disk(tmp_path, monkeypatch)
    with patch.object(mock_playwright.__enter__(), "chromium",
                      create=True):
        pass  # fall-through; use explicit stub below

    class _BrokenPw:
        def __enter__(self):
            pw = MagicMock()
            pw.chromium.launch.side_effect = RuntimeError("chromium missing")
            return pw

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(session_fetch, "sync_playwright", _BrokenPw)
    with pytest.raises(RuntimeError, match="chromium missing"):
        cmd_run(_args())
    events = [e for e in syslog_to_tmp()
              if e["event"] == "browser_launch_failed"]
    assert len(events) == 1
    assert "chromium missing" in events[0]["error"]


def test_cmd_run_emits_browser_context_failed(
    tmp_path, monkeypatch, syslog_to_tmp,
):
    """new_context() raising → browser_context_failed + browser.close
    called (lines 460-468)."""
    _cfg_on_disk(tmp_path, monkeypatch)

    browser = MagicMock()
    browser.new_context.side_effect = RuntimeError("bad storage state")

    class _CtxFailPw:
        def __enter__(self):
            pw = MagicMock()
            pw.chromium.launch.return_value = browser
            return pw

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(session_fetch, "sync_playwright", _CtxFailPw)
    with pytest.raises(RuntimeError, match="bad storage state"):
        cmd_run(_args())
    events = [e for e in syslog_to_tmp()
              if e["event"] == "browser_context_failed"]
    assert len(events) == 1
    browser.close.assert_called_once()


# -------- cmd_run auth-failure + session-expired-retry branches --------

def test_cmd_run_auth_error_propagates_and_logs(
    tmp_path, monkeypatch, mock_playwright, syslog_to_tmp,
):
    """AuthError from ensure_authenticated → cli_run_auth_failed event,
    then propagates out of cmd_run (lines 499-507)."""
    _cfg_on_disk(tmp_path, monkeypatch)
    from uscis_auth import AuthError
    with patch.object(session_fetch, "ensure_authenticated",
                      side_effect=AuthError("login blocked")):
        with pytest.raises(AuthError):
            cmd_run(_args())
    events = [e for e in syslog_to_tmp()
              if e["event"] == "cli_run_auth_failed"]
    assert len(events) == 1
    assert "login blocked" in events[0]["error"]


def test_cmd_run_auth_error_during_session_expired_retry(
    tmp_path, monkeypatch, mock_playwright, syslog_to_tmp,
):
    """First auth succeeds → _extract_cases raises SessionExpired → second
    ensure_authenticated raises AuthError → cli_run_auth_failed with
    phase=post_session_expired_retry (lines 525-534)."""
    _cfg_on_disk(tmp_path, monkeypatch)
    from uscis_api import SessionExpired
    from uscis_auth import AuthError

    auth_calls = [None, AuthError("reauth failed")]

    def _auth(*a, **k):
        rv = auth_calls.pop(0)
        if isinstance(rv, Exception):
            raise rv

    with patch.object(session_fetch, "ensure_authenticated", _auth), \
         patch.object(session_fetch, "_extract_cases",
                      side_effect=SessionExpired("IOE1", 401, "stale")):
        with pytest.raises(AuthError):
            cmd_run(_args())
    events = [e for e in syslog_to_tmp()
              if e["event"] == "cli_run_auth_failed"]
    assert len(events) == 1
    assert events[0]["phase"] == "post_session_expired_retry"


def test_cmd_run_session_expired_twice(
    tmp_path, monkeypatch, mock_playwright, syslog_to_tmp,
):
    """_extract_cases raises SessionExpired → reauth succeeds →
    _extract_cases raises SessionExpired *again* →
    cli_run_session_expired_twice event + exception propagates
    (lines 539-547)."""
    _cfg_on_disk(tmp_path, monkeypatch)
    from uscis_api import SessionExpired

    with patch.object(session_fetch, "ensure_authenticated"), \
         patch.object(session_fetch, "_extract_cases",
                      side_effect=SessionExpired("IOE2", 401, "still stale")):
        with pytest.raises(SessionExpired):
            cmd_run(_args())
    events = [e for e in syslog_to_tmp()
              if e["event"] == "cli_run_session_expired_twice"]
    assert len(events) == 1
    assert events[0]["receipt"] == "IOE2"


def test_cmd_run_browser_close_swallows_error(
    tmp_path, monkeypatch, mock_playwright, syslog_to_tmp,
):
    """browser.close() raising during teardown must not propagate — it
    logs browser_close_failed and the original return code is preserved
    (lines 715-716 / 720+)."""
    _cfg_on_disk(tmp_path, monkeypatch)
    mock_playwright.browser.close.side_effect = RuntimeError("zombie proc")
    with patch.object(session_fetch, "ensure_authenticated"), \
         patch.object(session_fetch, "_extract_cases", return_value=0):
        rc = cmd_run(_args())
    # Close error is swallowed — cmd_run still reports success.
    assert rc == 0


# -------- _extract_cases post-fetch edge cases -----------------------

def test_extract_cases_post_fetch_rewarm_failure_logs_warning(
    monkeypatch, tmp_path, syslog_to_tmp,
):
    """The dashboard re-warm nav after the per-case fetch is best-effort;
    a failure just logs a warning and the run continues."""
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    monkeypatch.setattr(session_fetch, "ROOT", tmp_path)

    worker_tab = MagicMock()
    # fetch_case / status are mocked, so the only goto call on the worker
    # tab is the dashboard rewarm — make it raise.
    worker_tab.goto.side_effect = RuntimeError("tab dead")

    context = MagicMock()
    cases = [{"id": "IOE1", "label": "I-485"}]
    case_data = {"data": {"formType": "I-485", "receiptNumber": "IOE1"}}

    with patch.object(session_fetch, "open_worker_tab", return_value=worker_tab), \
         patch.object(session_fetch, "fetch_case", return_value=case_data):
        _extract_cases(context, cases, "2026-04-24T00:00:00Z")
    events = [e for e in syslog_to_tmp()
              if e["event"] == "post_fetch_rewarm_failed"]
    assert len(events) == 1


# -------- main() uncaught-exception wrapper ----------------------------

def test_main_uncaught_exception_logs_and_reraises(
    monkeypatch, syslog_to_tmp,
):
    """Any non-SystemExit exception that escapes cmd_run must be
    categorised as cli_uncaught_exception with a traceback tail,
    THEN re-raised (lines 790-801)."""
    monkeypatch.setattr("sys.argv", ["session_fetch.py"])
    with patch.object(session_fetch, "cmd_run",
                      side_effect=RuntimeError("kaboom")):
        with pytest.raises(RuntimeError, match="kaboom"):
            main()
    events = [e for e in syslog_to_tmp()
              if e["event"] == "cli_uncaught_exception"]
    assert len(events) == 1
    assert "kaboom" in events[0]["error"]
    assert events[0]["traceback_tail"]


def test_main_system_exit_propagates_without_logging(
    monkeypatch, syslog_to_tmp,
):
    """SystemExit (from argparse / load_auth failure) must propagate
    untouched and NOT produce a cli_uncaught_exception."""
    monkeypatch.setattr("sys.argv", ["session_fetch.py"])
    with patch.object(session_fetch, "cmd_run",
                      side_effect=SystemExit(2)):
        with pytest.raises(SystemExit):
            main()
    events = [e for e in syslog_to_tmp()
              if e["event"] == "cli_uncaught_exception"]
    assert events == []
