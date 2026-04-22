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
    append_snapshot,
    cmd_extract,
    cmd_login,
    cmd_run,
    load_auth,
    load_config,
    log_file_for,
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


def test_log_file_for_parses_form_number():
    assert log_file_for("I-485").name == "485_logs.json"
    assert log_file_for("I485").name == "485_logs.json"
    assert log_file_for("Form I-131").name == "131_logs.json"


def test_log_file_for_raises_on_unrecognized_form():
    with pytest.raises(ValueError):
        log_file_for("bogus")


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


# -------- append_snapshot ----------------------------------------------

def test_append_snapshot_creates_file_and_appends(tmp_path, monkeypatch):
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    path = append_snapshot("I-485", {"a": 1}, "2026-04-18T00:00:00Z")
    assert path.exists()
    data = json.loads(path.read_text())
    assert len(data) == 1
    assert data[0]["capturedAt"] == "2026-04-18T00:00:00Z"

    # Second append keeps the first.
    append_snapshot("I-485", {"a": 2}, "2026-04-19T00:00:00Z")
    data = json.loads(path.read_text())
    assert len(data) == 2


def test_append_snapshot_recovers_from_invalid_json(tmp_path, monkeypatch):
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    p = tmp_path / "485_logs.json"
    p.write_text("{broken json")
    append_snapshot("I-485", {"ok": True}, "2026-04-18T00:00:00Z")
    data = json.loads(p.read_text())
    assert len(data) == 1


def test_append_snapshot_recovers_from_non_list_json(tmp_path, monkeypatch):
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    p = tmp_path / "485_logs.json"
    p.write_text('{"not": "a list"}')
    append_snapshot("I-485", {"ok": True}, "2026-04-18T00:00:00Z")
    data = json.loads(p.read_text())
    assert isinstance(data, list)
    assert len(data) == 1


# -------- _extract_cases ------------------------------------------------

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
    assert (tmp_path / "485_logs.json").exists()


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


def test_cmd_run_swallows_storage_state_error(tmp_path, monkeypatch, mock_playwright):
    _cfg_on_disk(tmp_path, monkeypatch)
    mock_playwright.context.storage_state.side_effect = RuntimeError("disk full")
    with patch.object(session_fetch, "ensure_authenticated"), \
         patch.object(session_fetch, "_extract_cases", return_value=0):
        rc = cmd_run(_args())
    assert rc == 0


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


def test_append_snapshot_emits_invalid_json_warning(
    tmp_path, monkeypatch, syslog_to_tmp,
):
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    log_file = tmp_path / "485_logs.json"
    log_file.write_text("not { valid json")
    session_fetch.append_snapshot("I-485", {"x": 1}, "2026-04-22T00:00:00Z")
    events = [e for e in syslog_to_tmp()
              if e["event"] == "snapshot_log_invalid_json"]
    assert len(events) == 1
    assert events[0]["file"] == "485_logs.json"


def test_append_snapshot_emits_not_array_warning(
    tmp_path, monkeypatch, syslog_to_tmp,
):
    monkeypatch.setattr(session_fetch, "DATA_DIR", tmp_path)
    log_file = tmp_path / "485_logs.json"
    log_file.write_text('{"this": "is an object, not a list"}')
    session_fetch.append_snapshot("I-485", {"x": 1}, "2026-04-22T00:00:00Z")
    events = [e for e in syslog_to_tmp()
              if e["event"] == "snapshot_log_not_array"]
    assert len(events) == 1
    assert events[0]["existing_type"] == "dict"
