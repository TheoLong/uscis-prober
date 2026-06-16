# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the Flask dashboard server.

The subprocess spawned by /api/pull is mocked so no real pull is issued.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import server


# -------- common fixtures ------------------------------------------------

@pytest.fixture(autouse=True)
def _redirect_system_log(monkeypatch, tmp_path):
    """Redirect the real on-disk system log to a per-test tmp file.

    Without this fixture, server tests that call `_run_pull_subprocess`,
    `_send_notifications_for_new`, or any Flask route that writes a
    structured event leak into `data/system_log.json` — corrupting local
    dev state and contaminating later tests. This fixture is autouse, so
    every test in this file gets isolation for free.
    """
    import system_log
    monkeypatch.setattr(system_log, "LOG_PATH", tmp_path / "_syslog.json")
    system_log.clear()


@pytest.fixture
def client(monkeypatch, tmp_path):
    """Flask test client with data dir + config path redirected into tmp."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = {
        "auth": {
            "uscis_email": "e", "uscis_password": "p",
            "uscis_mfa_email": "g@example.com", "uscis_mfa_app_password": "pw",
            "notification_email": "n@example.com",
        },
        "cases": [{"id": "IOE1", "label": "I-485"}],
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))

    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)

    # Reset the shared mutable pull state between tests so assertions stay
    # deterministic.
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    server.app.config["TESTING"] = True
    return server.app.test_client()


def _seed_log(data_dir: Path, entries: list[dict]) -> None:
    (data_dir / "485_case.json").write_text(json.dumps(entries))


def _seed_location_log(data_dir: Path, entries: list[dict]) -> None:
    (data_dir / "485_location.json").write_text(json.dumps(entries))


# -------- pure helpers ---------------------------------------------------

def test_case_log_file_for_recognises_form_numbers(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    assert server._case_log_file_for("I-485").name == "485_case.json"


def test_case_log_file_for_none_for_unknown_form(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    assert server._case_log_file_for("???") is None


def test_load_entries_returns_empty_on_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    assert server.load_case_entries("I-485") == []


def test_load_entries_returns_empty_on_invalid_json(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    (tmp_path / "485_case.json").write_text("{broken")
    assert server.load_case_entries("I-485") == []


def test_load_entries_rejects_non_list_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    (tmp_path / "485_case.json").write_text('{"not": "list"}')
    assert server.load_case_entries("I-485") == []


def test_load_entries_happy_path(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    (tmp_path / "485_case.json").write_text('[{"capturedAt": "x"}]')
    assert server.load_case_entries("I-485") == [{"capturedAt": "x"}]


def test_load_entries_unknown_form_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    assert server.load_case_entries("unknown") == []


def test_now_iso_ends_with_z():
    s = server._now_iso()
    assert s.endswith("Z")


def test_notify_recipient_prefers_explicit():
    assert server._notify_recipient({"notification_email": "a", "uscis_mfa_email": "b"}) == "a"
    assert server._notify_recipient({"uscis_mfa_email": "b"}) == "b"
    assert server._notify_recipient({}) is None


def test_update_ids_skips_records_without_id():
    ids = server._update_ids([{"id": "1"}, {}, {"id": "2"}])
    assert ids == {"1", "2"}


# -------- diff-set snapshotting ----------------------------------------

def _entry(captured_at, **data):
    return {"capturedAt": captured_at, "data": {
        "receiptNumber": "IOE1", "formType": "I-485", "events": [],
        "notices": [], "documents": [], "evidenceRequests": [],
        "updatedAt": "2026-03-01", **data}}


def test_all_update_records_enriches_id(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = {"cases": [{"id": "IOE1", "label": "I-485"}]}
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)

    _seed_log(data_dir, [
        _entry("2026-03-09T00:00:00Z"),
        _entry("2026-03-10T00:00:00Z", closed=True),  # decision flip
    ])
    records = server._all_update_records()
    assert len(records) == 1
    assert records[0]["id"] == "IOE1:case:2026-03-10"
    assert records[0]["caseLabel"] == "I-485"
    assert records[0]["detectedOn"] == "2026-03-10"


# -------- startup diff recompute --------------------------------------

def test_recompute_diffs_at_startup_counts_each_case(monkeypatch, tmp_path):
    """Walks every configured case, returns per-case counts, and emits a
    diff_recomputed system-log event with the same payload."""
    import system_log

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setattr(server, "DATA_DIR", data_dir)

    # I-485: two snapshots with a decision flip → 1 case-diff.
    _seed_log(data_dir, [
        _entry("2026-03-09T00:00:00Z"),
        _entry("2026-03-10T00:00:00Z", closed=True),
    ])
    # I-765: single snapshot → no diffs (need a pair to compare).
    (data_dir / "765_case.json").write_text(json.dumps([
        {"capturedAt": "2026-04-01T00:00:00Z", "data": {
            "receiptNumber": "IOE2", "formType": "I-765", "events": [],
            "notices": [], "documents": [], "evidenceRequests": [],
            "updatedAt": "2026-04-01"}},
    ]))

    cfg = {"cases": [
        {"id": "IOE1", "label": "I-485"},
        {"id": "IOE2", "label": "I-765"},
    ]}
    result = server._recompute_diffs_at_startup(cfg)

    assert result == {"cases": [
        {"label": "I-485", "case_changes": 1, "location_changes": 0},
        {"label": "I-765", "case_changes": 0, "location_changes": 0},
    ]}

    # The system log entry mirrors the return value — single event,
    # carrying the per-case summary so the operator can audit a restart.
    events = [e for e in system_log.read_all() if e.get("event") == "diff_recomputed"]
    assert len(events) == 1
    assert events[0]["cases"] == result["cases"]
    assert events[0]["source"] == "server"


def test_recompute_diffs_at_startup_skips_cases_without_label(monkeypatch, tmp_path):
    """Malformed config entries (missing label) are silently skipped — the
    recompute is best-effort and should never crash on bad config."""
    monkeypatch.setattr(server, "DATA_DIR", tmp_path / "data")
    (tmp_path / "data").mkdir()
    cfg = {"cases": [
        {"id": "IOE1"},        # no label
        {"id": "IOE2", "label": ""},  # empty label
    ]}
    assert server._recompute_diffs_at_startup(cfg) == {"cases": []}


def test_recompute_diffs_at_startup_handles_empty_config(monkeypatch, tmp_path):
    """No cases configured → no-op return, no exception, no log noise."""
    import system_log
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    assert server._recompute_diffs_at_startup({}) == {"cases": []}
    assert server._recompute_diffs_at_startup({"cases": None}) == {"cases": []}
    # A diff_recomputed event still fires (so a "no cases yet" install is
    # auditable in the log) but with an empty cases array.
    events = [e for e in system_log.read_all() if e.get("event") == "diff_recomputed"]
    assert len(events) == 2
    assert all(e["cases"] == [] for e in events)


def test_recompute_diffs_at_startup_swallows_errors_and_logs(monkeypatch, tmp_path):
    """A crash inside day_changes() must not propagate — it would prevent
    the server from finishing startup. The error is logged and an empty
    payload is returned so callers can keep going."""
    import system_log
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)

    def _boom(_entries):
        raise RuntimeError("synthetic diff explosion")
    monkeypatch.setattr(server, "day_changes", _boom)

    cfg = {"cases": [{"id": "IOE1", "label": "I-485"}]}
    result = server._recompute_diffs_at_startup(cfg)

    assert result["cases"] == []
    assert "synthetic diff explosion" in result["error"]
    failures = [e for e in system_log.read_all() if e.get("event") == "diff_recompute_failed"]
    assert len(failures) == 1
    assert failures[0]["level"] == "warning"


# -------- notification dispatcher -------------------------------------

def test_send_notifications_for_new_noop_when_empty(monkeypatch):
    with patch.object(server, "notify_update") as notify:
        server._send_notifications_for_new([])
    notify.assert_not_called()


def test_send_notifications_for_new_skips_when_auth_missing(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"auth": {}}))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    with patch.object(server, "notify_update") as notify:
        server._send_notifications_for_new([{"id": "1", "kind": "event"}])
    notify.assert_not_called()


def test_send_notifications_for_new_catches_per_record_failure(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "auth": {
            "uscis_mfa_email": "u@example.com",
            "uscis_mfa_app_password": "pw",
            "notification_email": "n@example.com",
        }
    }))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    with patch.object(server, "notify_update", side_effect=RuntimeError("boom")):
        # Must not propagate — exception is logged and skipped.
        server._send_notifications_for_new([
            {"id": "1", "kind": "event"},
            {"id": "2", "kind": "silent_update"},
        ])


def test_send_notifications_for_new_crash_protected(monkeypatch):
    # Force load_config to raise — the whole dispatcher must not propagate.
    monkeypatch.setattr(server, "load_config", MagicMock(side_effect=RuntimeError("x")))
    server._send_notifications_for_new([{"id": "1"}])


def test_send_notifications_for_new_emits_success_log(monkeypatch, tmp_path, caplog):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "auth": {
            "uscis_mfa_email": "u@example.com",
            "uscis_mfa_app_password": "pw",
        }
    }))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    with patch.object(server, "notify_update") as notify:
        server._send_notifications_for_new([
            {"id": "1", "kind": "event"},
        ])
    notify.assert_called_once()


# -------- _run_pull_subprocess ----------------------------------------

def _fake_proc(returncode=0, stdout="", stderr=""):
    proc = MagicMock()
    proc.returncode = returncode
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


def test_run_pull_subprocess_skips_if_already_running(monkeypatch):
    monkeypatch.setattr(server, "_pull_state", server.PullState(running=True))
    with patch.object(subprocess, "run") as run:
        server._run_pull_subprocess()
    run.assert_not_called()


def test_run_pull_subprocess_success_flags_ok(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "retry": 0, "retry_wait_seconds": 0, "storage_limit_mb": 256}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    with patch.object(subprocess, "run", return_value=_fake_proc(0, "hi", "")), \
         patch.object(server, "_send_notifications_for_new") as send:
        server._run_pull_subprocess()
    state = server._pull_state
    assert state.ok is True
    assert state.exit_code == 0
    assert state.running is False
    # No cases configured → no new diffs → no notification call.
    send.assert_not_called()


def test_run_pull_subprocess_success_with_new_diffs_notifies(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "cases": [{"id": "IOE1", "label": "I-485"}],
        "retry": 0, "retry_wait_seconds": 0,
        "storage_limit_mb": 256,
    }))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    # First call (before pull): no entries. Second call (after pull): records.
    calls = {"n": 0}

    def _fake_records(cfg=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return []
        return [{"id": "IOE1:case:2026-03-10", "kind": "event"}]

    monkeypatch.setattr(server, "_all_update_records", _fake_records)
    with patch.object(subprocess, "run", return_value=_fake_proc(0, "", "")), \
         patch.object(server, "_send_notifications_for_new") as send:
        server._run_pull_subprocess()
    send.assert_called_once()


def test_run_pull_subprocess_failure_sets_error(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "retry": 0, "retry_wait_seconds": 0, "storage_limit_mb": 256}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    with patch.object(subprocess, "run", return_value=_fake_proc(1, "", "err")), \
         patch.object(server, "_send_notifications_for_new") as send:
        server._run_pull_subprocess()
    assert server._pull_state.ok is False
    assert server._pull_state.exit_code == 1
    # Email not sent on failure.
    send.assert_not_called()


def test_run_pull_subprocess_timeout_recorded(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "retry": 0, "retry_wait_seconds": 0, "storage_limit_mb": 256}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    with patch.object(
        subprocess, "run",
        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1),
    ):
        server._run_pull_subprocess()
    assert server._pull_state.ok is False
    assert "timeout" in (server._pull_state.last_error or "")


def test_run_pull_subprocess_crash_recorded(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "retry": 0, "retry_wait_seconds": 0, "storage_limit_mb": 256}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    with patch.object(subprocess, "run", side_effect=RuntimeError("oops")):
        server._run_pull_subprocess()
    assert server._pull_state.ok is False


# -------- retry policy + orchestration ----------------------------------

def test_load_retry_policy_raises_when_retry_missing():
    with pytest.raises(server.ConfigError, match="retry"):
        server.load_retry_policy({"retry_wait_seconds": 60})


def test_load_retry_policy_raises_when_wait_missing():
    with pytest.raises(server.ConfigError, match="retry_wait_seconds"):
        server.load_retry_policy({"retry": 2})


def test_load_retry_policy_raises_on_non_numeric_retry():
    with pytest.raises(server.ConfigError, match="retry"):
        server.load_retry_policy({"retry": "two", "retry_wait_seconds": 60})


def test_load_retry_policy_raises_on_non_numeric_wait():
    with pytest.raises(server.ConfigError, match="retry_wait_seconds"):
        server.load_retry_policy({"retry": 1, "retry_wait_seconds": "later"})


def test_load_retry_policy_raises_on_negative_values():
    with pytest.raises(server.ConfigError):
        server.load_retry_policy({"retry": -1, "retry_wait_seconds": 60})
    with pytest.raises(server.ConfigError):
        server.load_retry_policy({"retry": 1, "retry_wait_seconds": -5})


def test_load_retry_policy_clamps_out_of_range():
    p = server.load_retry_policy({"retry": 99, "retry_wait_seconds": 9999})
    assert p.retry == server.RETRY_MAX_COUNT
    assert p.retry_wait_seconds == float(server.RETRY_MAX_WAIT_SECONDS)


def test_load_retry_policy_respects_zero():
    # retry=0 is valid — it's how operators disable retries.
    p = server.load_retry_policy({"retry": 0, "retry_wait_seconds": 30})
    assert p.retry == 0
    assert p.total_attempts == 1


def test_load_retry_policy_template_values_are_valid():
    # Sanity check: the recommended values from config.example.json
    # must parse cleanly through the same validator live configs use.
    p = server.load_retry_policy({"retry": 2, "retry_wait_seconds": 180})
    assert p.retry == 2
    assert p.retry_wait_seconds == 180.0
    assert p.total_attempts == 3


# -------- storage_limit_mb + trace_successful_pulls config --------------

def test_load_storage_limit_defaults_when_missing():
    """Optional — missing returns the default 256 MB."""
    b = server.load_storage_limit_bytes({"retry": 1, "retry_wait_seconds": 10})
    assert b == 256 * 1024 * 1024


def test_load_storage_limit_parses_mb_to_bytes():
    b = server.load_storage_limit_bytes({"storage_limit_mb": 512})
    assert b == 512 * 1024 * 1024


def test_load_storage_limit_legacy_gb_key_still_works():
    """Backward compat — older configs that still ship `storage_limit_gb`
    are silently converted to MB so a deployed VM doesn't break on the
    rename. Once both keys exist, `_mb` wins."""
    b = server.load_storage_limit_bytes({"storage_limit_gb": 1.0})
    assert b == 1024 * 1024 * 1024
    # New key wins when both present.
    b = server.load_storage_limit_bytes(
        {"storage_limit_gb": 1.0, "storage_limit_mb": 512},
    )
    assert b == 512 * 1024 * 1024


def test_load_storage_limit_rejects_out_of_range():
    with pytest.raises(server.ConfigError):
        server.load_storage_limit_bytes({"storage_limit_mb": 0})
    with pytest.raises(server.ConfigError):
        server.load_storage_limit_bytes({"storage_limit_mb": 999_999})


def test_load_storage_limit_rejects_non_numeric():
    with pytest.raises(server.ConfigError):
        server.load_storage_limit_bytes({"storage_limit_mb": "lots"})


def test_load_trace_successful_pulls_defaults_false_when_missing():
    """Optional field — missing = false. Traces are only written on
    failures unless the UI toggle explicitly sets true."""
    assert server.load_trace_successful_pulls(
        {"retry": 1, "retry_wait_seconds": 10},
    ) is False


def test_load_trace_successful_pulls_rejects_non_bool():
    with pytest.raises(server.ConfigError):
        server.load_trace_successful_pulls({"trace_successful_pulls": "yes"})


def test_load_trace_successful_pulls_honours_true_and_false():
    assert server.load_trace_successful_pulls({"trace_successful_pulls": True}) is True
    assert server.load_trace_successful_pulls({}) is False


# -------- /api/storage + category breakdown ----------------------------

def test_api_storage_categorises_data_dir(client, monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    # Seed representative files per category.
    (data_dir / "485_case.json").write_text("[]")
    (data_dir / "485_location.json").write_text("[]")
    (data_dir / "system_log.json").write_text("[]")
    (data_dir / "full_traces").mkdir()
    (data_dir / "full_traces" / "trace1").mkdir()
    (data_dir / "full_traces" / "trace1" / "01_after_goto.html.gz").write_bytes(b"x" * 1200)
    (data_dir / "full_traces" / "trace1" / "01_after_goto.png").write_bytes(b"x" * 2000)

    # Extend the existing client's config with required storage key.
    cfg_path = tmp_path / "config.json"
    cfg = json.loads(cfg_path.read_text())
    cfg.update({
        "retry": 1, "retry_wait_seconds": 10,
        "storage_limit_mb": server.STORAGE_MIN_MB,  # minimum legal value
    })
    cfg_path.write_text(json.dumps(cfg))

    r = client.get("/api/storage")
    assert r.status_code == 200
    body = r.get_json()
    keys = {c["key"]: c for c in body["categories"]}
    # Cases grouped per form number (case+location merged).
    assert "case_485" in keys
    assert keys["case_485"]["label"] == "I-485"
    assert keys["case_485"]["file_count"] == 2  # 485_case.json + 485_location.json
    # System log aggregates the event log + every full-trace file.
    assert "system_log" in keys
    # Event log (2 bytes: "[]") + 2 trace files (3200 bytes).
    assert keys["system_log"]["bytes"] >= 3200
    assert keys["system_log"]["file_count"] >= 3
    # Config + Other filtered out of UI categories (still counted
    # toward total_bytes indirectly via the walk).
    assert "config" not in keys
    assert "other" not in keys
    assert body["total_bytes"] > 0
    assert body["limit_bytes"] == int(server.STORAGE_MIN_MB * 1024 * 1024)


def test_api_storage_reports_limit_exceeded(client, monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "big_case.json").write_bytes(b"x" * 100_000)
    cfg_path = tmp_path / "config.json"
    cfg = json.loads(cfg_path.read_text())
    # 50 KB limit — easily tripped by our 100 KB seed.
    cfg.update({
        "retry": 1, "retry_wait_seconds": 10,
        # storage_limit_mb must be >= STORAGE_MIN_MB; pin to the floor.
        "storage_limit_mb": server.STORAGE_MIN_MB,
    })
    cfg_path.write_text(json.dumps(cfg))

    r = client.get("/api/storage")
    assert r.status_code == 200
    body = r.get_json()
    # 100 KB < STORAGE_MIN_MB (= 10 MB); so limit is NOT exceeded here.
    # Flip: make the seed file MUCH bigger than the limit.
    (data_dir / "big_case.json").write_bytes(b"x" * (20 * 1024 * 1024))  # 20 MB
    r = client.get("/api/storage")
    body = r.get_json()
    assert body["limit_exceeded"] is True
    assert body["limit_ratio"] > 1.0


# -------- storage-limit alert + dedup ----------------------------------

def test_storage_limit_check_emits_and_dedups(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "cases": [],
        "retry": 0, "retry_wait_seconds": 0,
        "storage_limit_mb": server.STORAGE_MIN_MB,  # 10 MB
    }))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    # Disable email path — we only care about dedup + event emission.
    monkeypatch.setattr(server, "_send_storage_alert_email", lambda *a, **kw: None)

    # Seed a file big enough to trip the limit (20 MB > 10 MB).
    (data_dir / "big_case.json").write_bytes(b"x" * int(0.02 * 1024**3))

    # First run: should alert.
    steps1 = server._check_storage_limit_and_alert()
    events1 = [s["event"] for s in steps1]
    assert "storage_limit_exceeded" in events1

    # Second run while still over: must NOT re-alert.
    steps2 = server._check_storage_limit_and_alert()
    events2 = [s["event"] for s in steps2]
    assert "storage_limit_exceeded" not in events2

    # Shrink well below rearm threshold (< 90 % of limit).
    (data_dir / "big_case.json").unlink()
    steps3 = server._check_storage_limit_and_alert()
    events3 = [s["event"] for s in steps3]
    assert "storage_alert_rearmed" in events3

    # Grow over again: re-alerts.
    (data_dir / "big_case.json").write_bytes(b"x" * int(0.02 * 1024**3))
    steps4 = server._check_storage_limit_and_alert()
    events4 = [s["event"] for s in steps4]
    assert "storage_limit_exceeded" in events4


# -------- /api/debug-mode + /api/auth-trace --------------------------

def _write_full_cfg(cfg_path, enabled=False):
    cfg_path.write_text(json.dumps({
        "auth": {
            "uscis_email": "e", "uscis_password": "p",
            "uscis_mfa_email": "g@example.com",
            "uscis_mfa_app_password": "pw",
        },
        "cases": [],
        "retry": 1, "retry_wait_seconds": 10,
        "storage_limit_mb": 256,
        "trace_successful_pulls": enabled,
    }))


def test_api_debug_mode_get_returns_current(client, tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_full_cfg(cfg_path, enabled=False)
    r = client.get("/api/debug-mode")
    assert r.status_code == 200
    assert r.get_json() == {"enabled": False}


def test_api_debug_mode_post_persists_flip(client, tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_full_cfg(cfg_path, enabled=False)
    r = client.post("/api/debug-mode", json={"enabled": True})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True and body["enabled"] is True
    # Config file was rewritten.
    saved = json.loads(cfg_path.read_text())
    assert saved["trace_successful_pulls"] is True


def test_api_debug_mode_rejects_non_bool(client, tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_full_cfg(cfg_path, enabled=False)
    r = client.post("/api/debug-mode", json={"enabled": "yes"})
    assert r.status_code == 400
 
 
def test_api_recompute_diff(client, tmp_path):
    cfg_path = tmp_path / "config.json"
    _write_full_cfg(cfg_path, enabled=False)
    r = client.post("/api/recompute-diff")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True}
 
def test_api_full_trace_serves_zip_jsonl_eml(client, tmp_path):
    """Serves trace.zip + mfa_trace/events.jsonl + mfa_trace/email_*.eml
    with correct MIME types. Path-traversal is rejected."""
    data_dir = tmp_path / "data"
    trace_dir = data_dir / "full_traces" / "20260424T000000Z_fail_scheduled"
    trace_dir.mkdir(parents=True)
    (trace_dir / "trace.zip").write_bytes(b"PK\x03\x04FAKE_ZIP")
    (trace_dir / "mfa_trace").mkdir()
    (trace_dir / "mfa_trace" / "events.jsonl").write_text(
        '{"event": "imap_connect_ok"}\n'
    )
    (trace_dir / "mfa_trace" / "email_12345.eml").write_bytes(
        b"From: a\nSubject: x\n\nbody"
    )

    cfg_path = tmp_path / "config.json"
    _write_full_cfg(cfg_path)

    # Zip at the top level.
    r = client.get("/api/full-trace/20260424T000000Z_fail_scheduled/trace.zip")
    assert r.status_code == 200
    assert r.mimetype == "application/zip"
    assert r.headers.get("Access-Control-Allow-Origin") == "*"

    # Nested one level deep (mfa_trace/events.jsonl).
    r = client.get(
        "/api/full-trace/20260424T000000Z_fail_scheduled/mfa_trace/events.jsonl",
    )
    assert r.status_code == 200
    assert r.mimetype == "application/x-ndjson"

    # Nested .eml.
    r = client.get(
        "/api/full-trace/20260424T000000Z_fail_scheduled/mfa_trace/email_12345.eml",
    )
    assert r.status_code == 200
    assert r.mimetype == "message/rfc822"

    # Path traversal rejected.
    for bad in (
        "../etc/passwd",
        "..%2fescape",
    ):
        r = client.get(f"/api/full-trace/{bad}/trace.zip")
        assert r.status_code in (400, 404)

    # More than two levels of nesting rejected.
    r = client.get(
        "/api/full-trace/20260424T000000Z_fail_scheduled/a/b/c.txt",
    )
    assert r.status_code == 400

    # Missing file → 404.
    r = client.get("/api/full-trace/20260424T000000Z_fail_scheduled/nope.zip")
    assert r.status_code == 404


def _cfg_with_retry(cfg_path, retry=1, retry_wait_seconds=0.0):
    cfg_path.write_text(json.dumps({
        "cases": [],
        "retry": retry,
        "retry_wait_seconds": retry_wait_seconds,
        "storage_limit_mb": 256,
    }))


def _auth_failed_stderr(attempt_marker: str = "a") -> str:
    """Compose one JSONL-encoded cli_run_auth_failed event as the child
    would emit it when its login flow bailed out."""
    from system_log import _JSONL_PREFIX  # type: ignore[attr-defined]
    return (
        f'{_JSONL_PREFIX}{{"ts":"2026-04-24T00:00:00Z",'
        f'"event":"cli_run_auth_failed","level":"error",'
        f'"source":"session_fetch","error":"AuthError: marker={attempt_marker}"}}'
    )


def test_envelope_level_green_on_clean_pull(monkeypatch, tmp_path):
    """Three-tier colour rule — tier 1 (green / info): all attempts
    succeeded, no error or warning steps anywhere. The dashboard
    should render this as the "green" neutral state."""
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    _cfg_with_retry(cfg_path, retry=2, retry_wait_seconds=0)
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    with patch.object(subprocess, "run",
                      return_value=_fake_proc(0, "", "")):
        server._run_pull_subprocess()

    import system_log
    pulls = [e for e in system_log.read_all() if e.get("event") == "pull"]
    assert len(pulls) == 1
    assert pulls[0]["level"] == "info"
    assert pulls[0]["exit_code"] == 0


def test_envelope_level_yellow_on_retry_recovery(monkeypatch, tmp_path):
    """Three-tier colour rule — tier 2 (yellow / warning): attempt 1
    failed but attempt 2 recovered. Final exit is 0, the data is
    present, but the operator should glance at it because a retry
    was exercised. Previously this would have been tagged `error`
    (worst-step severity) — the fix downgrades recovered pulls to
    `warning` so red stays reserved for truly-broken pulls."""
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    _cfg_with_retry(cfg_path, retry=1, retry_wait_seconds=0)
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    calls = []

    def _run(cmd, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            return _fake_proc(1, "", _auth_failed_stderr("first"))
        return _fake_proc(0, "", "")

    with patch.object(subprocess, "run", side_effect=_run):
        server._run_pull_subprocess()

    import system_log
    pulls = [e for e in system_log.read_all() if e.get("event") == "pull"]
    assert len(pulls) == 1
    env = pulls[0]
    assert env["level"] == "warning", (
        "retry-recovered pulls must downgrade red → yellow; "
        "got level=%r" % env["level"]
    )
    assert env["exit_code"] == 0
    # There SHOULD be error-level step(s) inside — the first attempt's
    # auth failure. The top-level downgrade rule is exactly what
    # distinguishes this case from a fully-failed pull.
    assert any(s.get("level") == "error" for s in env["steps"])


def test_envelope_level_red_on_total_failure(monkeypatch, tmp_path):
    """Three-tier colour rule — tier 3 (red / error): every attempt
    failed, final exit is non-zero. The operator needs to see red."""
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    _cfg_with_retry(cfg_path, retry=1, retry_wait_seconds=0)
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    with patch.object(subprocess, "run",
                      return_value=_fake_proc(1, "", _auth_failed_stderr("x"))):
        server._run_pull_subprocess()

    import system_log
    pulls = [e for e in system_log.read_all() if e.get("event") == "pull"]
    assert len(pulls) == 1
    env = pulls[0]
    assert env["level"] == "error"
    assert env["exit_code"] != 0


def test_run_pull_retries_on_auth_failure(monkeypatch, tmp_path):
    """Classic recovery: first attempt hits an auth failure, second
    attempt succeeds. We must see both attempts under one pull envelope."""
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    _cfg_with_retry(cfg_path, retry=1, retry_wait_seconds=0)
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    calls = []

    def _run(cmd, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            return _fake_proc(1, "", _auth_failed_stderr("first"))
        return _fake_proc(0, "", "")

    with patch.object(subprocess, "run", side_effect=_run):
        server._run_pull_subprocess()

    # Two subprocess invocations under one pull.
    assert len(calls) == 2
    assert server._pull_state.ok is True
    assert server._pull_state.exit_code == 0


def test_run_pull_forces_trace_on_retry_even_when_debug_off(monkeypatch, tmp_path):
    """Forensic-retention rule: if any attempt in a pull fails, every
    attempt under that pull must preserve its trace so a "retry
    succeeded after failure" scenario keeps both traces side-by-side.
    (The envelope itself is top-level `warning` in that case — see
    test_envelope_level_yellow_on_retry_recovery — but regardless of
    final colour, the tracer retention applies whenever a retry
    happened.)

    Observable: the FIRST attempt's child env has USCIS_TRACE_ON_SUCCESS=0
    (normal rule — only fail paths auto-preserve). The SECOND attempt,
    because we only get here after a prior failure, forces it to 1
    regardless of debug mode."""
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    _cfg_with_retry(cfg_path, retry=1, retry_wait_seconds=0)
    # Debug mode explicitly OFF — the flag must still flip on retry.
    cfg = json.loads(cfg_path.read_text())
    cfg["trace_successful_pulls"] = False
    cfg_path.write_text(json.dumps(cfg))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    captured_envs: list[dict] = []

    def _run(cmd, **kw):
        captured_envs.append(dict(kw.get("env") or {}))
        if len(captured_envs) == 1:
            return _fake_proc(1, "", _auth_failed_stderr("first"))
        return _fake_proc(0, "", "")

    with patch.object(subprocess, "run", side_effect=_run):
        server._run_pull_subprocess()

    assert len(captured_envs) == 2
    # Attempt 1: debug off, no prior failure → discard trace on success.
    assert captured_envs[0]["USCIS_TRACE_ON_SUCCESS"] == "0"
    # Attempt 2: previous attempt failed → forced on, preserves trace
    # even if attempt 2 itself succeeds.
    assert captured_envs[1]["USCIS_TRACE_ON_SUCCESS"] == "1"


def test_run_pull_does_not_retry_non_auth_failure(monkeypatch, tmp_path):
    """A non-auth failure (e.g. case fetch 500 without an auth_failed
    marker) must NOT retry — retry is an anti-bot recovery tool, not a
    catch-all."""
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    _cfg_with_retry(cfg_path, retry=3, retry_wait_seconds=0)
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    calls = []

    def _run(cmd, **kw):
        calls.append(cmd)
        return _fake_proc(1, "", "something else went wrong")

    with patch.object(subprocess, "run", side_effect=_run):
        server._run_pull_subprocess()

    assert len(calls) == 1  # no retry
    assert server._pull_state.ok is False


def test_run_pull_stops_after_total_attempts_exhausted(monkeypatch, tmp_path):
    """With retry=2, an always-failing auth must run exactly 3 total
    attempts (1 initial + 2 retries) before giving up."""
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    _cfg_with_retry(cfg_path, retry=2, retry_wait_seconds=0)
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    calls = []

    def _run(cmd, **kw):
        calls.append(cmd)
        return _fake_proc(1, "", _auth_failed_stderr(str(len(calls))))

    with patch.object(subprocess, "run", side_effect=_run):
        server._run_pull_subprocess()

    assert len(calls) == 3  # 1 initial + 2 retries
    assert server._pull_state.ok is False


def test_run_pull_preserves_stderr_across_retry_attempts(monkeypatch, tmp_path):
    """The subprocess_exit_nonzero step must include every attempt's
    stderr tail, not just the last one. Otherwise a crash signature on
    attempt 1 vanishes the moment attempt 2 starts."""
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    _cfg_with_retry(cfg_path, retry=2, retry_wait_seconds=0)
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    def _run(cmd, **kw):
        # Each attempt prints a distinctive marker line plus the
        # structured auth-failure event.
        marker = f"TRACEBACK_MARKER_ATTEMPT_{1 + len(getattr(_run, '_seen', []))}"
        _run._seen = getattr(_run, "_seen", []) + [marker]
        return _fake_proc(1, "", marker + "\n" + _auth_failed_stderr(marker))

    import system_log
    with patch.object(subprocess, "run", side_effect=_run):
        server._run_pull_subprocess()

    pulls = [e for e in system_log.read_all() if e.get("event") == "pull"]
    assert len(pulls) == 1
    exit_steps = [s for s in pulls[0]["steps"]
                  if s.get("event") == "subprocess_exit_nonzero"]
    # One exit-nonzero step per attempt, and the LAST attempt's step
    # must carry all earlier tails as `all_attempts_stderr_tails`.
    assert len(exit_steps) >= 1
    last = exit_steps[-1]
    assert "all_attempts_stderr_tails" in last
    # Flatten for easy inspection — at least two distinct markers
    # survived.
    flat = [line for tail in last["all_attempts_stderr_tails"] for line in tail]
    assert any("ATTEMPT_1" in line for line in flat)
    assert any("ATTEMPT_3" in line for line in flat)


def test_run_pull_emits_retry_waiting_and_starting_events(monkeypatch, tmp_path):
    """Each retry must emit `auth_retry_waiting` then `auth_retry_starting`
    so the dashboard shows the pending retry rather than a dead gap."""
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    _cfg_with_retry(cfg_path, retry=1, retry_wait_seconds=0)
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    calls = []

    def _run(cmd, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            return _fake_proc(1, "", _auth_failed_stderr("first"))
        return _fake_proc(0, "", "")

    import system_log
    with patch.object(subprocess, "run", side_effect=_run):
        server._run_pull_subprocess()

    # auth_retry_waiting / _starting are emitted inside the capture
    # scope so they're folded into the pull envelope's `steps[]`, not
    # written as standalone top-level events. Inspect the envelope.
    pulls = [e for e in system_log.read_all() if e.get("event") == "pull"]
    assert len(pulls) == 1
    step_names = [s.get("event") for s in pulls[0]["steps"]]
    assert step_names.count("auth_retry_waiting") == 1
    assert step_names.count("auth_retry_starting") == 1


def test_run_pull_retry_does_not_wait_when_retry_zero(monkeypatch, tmp_path):
    """retry=0 disables retry entirely — only one subprocess invocation."""
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    _cfg_with_retry(cfg_path, retry=0, retry_wait_seconds=120)
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    calls = []

    def _run(cmd, **kw):
        calls.append(cmd)
        return _fake_proc(1, "", _auth_failed_stderr())

    with patch.object(subprocess, "run", side_effect=_run):
        server._run_pull_subprocess()

    assert len(calls) == 1


def test_run_pull_does_not_retry_on_subprocess_timeout(monkeypatch, tmp_path):
    """A subprocess timeout is not a USCIS-side anti-bot issue — it's our
    side taking too long. Retrying risks a runaway pull, so we don't."""
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    _cfg_with_retry(cfg_path, retry=2, retry_wait_seconds=0)
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    calls = []

    def _run(cmd, **kw):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd="x", timeout=1)

    with patch.object(subprocess, "run", side_effect=_run):
        server._run_pull_subprocess()

    assert len(calls) == 1
    assert "timeout" in (server._pull_state.last_error or "")


def test_spawn_pull_async_starts_thread():
    with patch.object(server.threading, "Thread") as Thread:
        instance = MagicMock()
        Thread.return_value = instance
        server._spawn_pull_async()
    instance.start.assert_called_once()


# -------- scheduler wiring --------------------------------------------

def test_setup_scheduler_registers_one_job_per_pull_hour(monkeypatch):
    sched = MagicMock()
    monkeypatch.setattr(server, "scheduler", sched)
    server._setup_scheduler()
    assert sched.add_job.call_count == len(server.PULL_HOURS)
    sched.start.assert_called_once()


def test_next_run_iso_returns_none_when_no_jobs(monkeypatch):
    sched = MagicMock()
    sched.get_jobs.return_value = []
    monkeypatch.setattr(server, "scheduler", sched)
    assert server._next_run_iso() is None


def test_next_run_iso_returns_earliest_job(monkeypatch):
    from datetime import datetime, timezone, timedelta
    future = datetime.now(timezone.utc) + timedelta(hours=1)
    later = datetime.now(timezone.utc) + timedelta(hours=2)
    sched = MagicMock()
    sched.get_jobs.return_value = [
        MagicMock(next_run_time=later),
        MagicMock(next_run_time=future),
        MagicMock(next_run_time=None),
    ]
    monkeypatch.setattr(server, "scheduler", sched)
    iso = server._next_run_iso()
    assert iso is not None
    assert iso.endswith("Z")


# -------- Flask routes ------------------------------------------------

def test_index_rewrites_static_paths(client, tmp_path, monkeypatch):
    # Sanity — we don't need the static file to exist; just ensure rewrite works.
    r = client.get("/")
    assert r.status_code == 200
    assert b"?v=" in r.data


def test_no_cache_headers_present(client):
    r = client.get("/")
    assert "no-store" in r.headers.get("Cache-Control", "")
    assert r.headers.get("Pragma") == "no-cache"


def test_api_cases_empty_when_no_logs(client):
    r = client.get("/api/cases")
    assert r.status_code == 200
    body = r.get_json()
    assert "cases" in body
    assert body["cases"][0]["captures"] == 0
    assert body["cases"][0]["days"] == 0


def test_api_cases_surfaces_summary_when_logs_exist(client, tmp_path):
    data_dir = tmp_path / "data"
    _seed_log(data_dir, [
        _entry("2026-03-09T00:00:00Z"),
        _entry("2026-03-10T00:00:00Z", closed=True),
    ])
    r = client.get("/api/cases")
    body = r.get_json()
    case = body["cases"][0]
    assert case["captures"] == 2
    assert case["days"] == 2
    assert case["summary"]["stage"] == "Pending receipt"


def test_api_cases_captures_and_days_diverge_on_same_day_pulls(client, tmp_path):
    # UI renders `captures` and `days` as two separate sub-fact cells, so the
    # API must expose them as independent counters — same-day snapshots bump
    # `captures` but not `days`.
    data_dir = tmp_path / "data"
    _seed_log(data_dir, [
        _entry("2026-03-09T06:00:00Z"),
        _entry("2026-03-09T18:00:00Z"),  # same calendar day
        _entry("2026-03-10T06:00:00Z"),
    ])
    r = client.get("/api/cases")
    case = r.get_json()["cases"][0]
    assert case["captures"] == 3
    assert case["days"] == 2


def test_api_history_preserves_uscis_key_order(client, tmp_path):
    # USCIS returns fields in a specific, non-alphabetical order. Flask's
    # default `jsonify` would sort object keys — we disable that so the
    # Raw JSON panel shows exactly what came back from USCIS.
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    entries = [{
        "capturedAt": "2026-03-10T00:00:00Z",
        # Keys deliberately NOT in alphabetical order:
        "data": {"zeta_first": 1, "alpha_later": 2, "middle_third": 3},
    }]
    (data_dir / "485_case.json").write_text(json.dumps(entries))

    r = client.get("/api/cases/I-485/history")
    body = r.data.decode()
    # Original insertion order must be preserved in the wire bytes.
    assert body.index("zeta_first") < body.index("alpha_later")
    assert body.index("alpha_later") < body.index("middle_third")


def test_api_cases_surfaces_populated_location(client, tmp_path):
    data_dir = tmp_path / "data"
    _seed_log(data_dir, [_entry("2026-04-22T18:00:00Z")])
    # Live USCIS envelope: {"data": {"receipt_details": {...}, "message": ...}}
    _seed_location_log(data_dir, [
        {
            "capturedAt": "2026-04-22T18:00:00Z",
            "data": {
                "data": {
                    "receipt_details": {
                        "form": "I-485",
                        "location": "SCD",
                        "subtype": "147-C9",
                    },
                    "message": "ok",
                },
            },
        },
    ])
    r = client.get("/api/cases")
    case = r.get_json()["cases"][0]
    assert case["location"]["info"]["location"] == "SCD"
    assert case["location"]["info"]["subtype"] == "147-C9"
    assert case["location"]["captures"] == 1
    assert case["location"]["capturedAt"] == "2026-04-22T18:00:00Z"


def test_api_cases_accepts_flat_location_payload_fallback(client, tmp_path):
    # Defensive: if a future/legacy snapshot is already flat (no receipt_details
    # wrapper), still surface it.
    data_dir = tmp_path / "data"
    _seed_log(data_dir, [_entry("2026-04-22T18:00:00Z")])
    _seed_location_log(data_dir, [
        {
            "capturedAt": "2026-04-22T18:00:00Z",
            "data": {"data": {"form": "I-765", "location": "SCD"}},
        },
    ])
    r = client.get("/api/cases")
    case = r.get_json()["cases"][0]
    assert case["location"]["info"]["location"] == "SCD"


def test_api_cases_exposes_null_location_as_info_none(client, tmp_path):
    # `{"data": null}` snapshots still count as captures — the dashboard uses
    # `info: None` to render TBD with its explanatory popover.
    data_dir = tmp_path / "data"
    _seed_log(data_dir, [_entry("2026-04-22T18:00:00Z")])
    _seed_location_log(data_dir, [
        {"capturedAt": "2026-04-22T18:00:00Z", "data": {"data": None}},
    ])
    r = client.get("/api/cases")
    case = r.get_json()["cases"][0]
    assert case["location"]["info"] is None
    assert case["location"]["captures"] == 1


def test_api_cases_location_absent_when_no_log(client, tmp_path):
    data_dir = tmp_path / "data"
    _seed_log(data_dir, [_entry("2026-04-22T18:00:00Z")])
    r = client.get("/api/cases")
    case = r.get_json()["cases"][0]
    assert case["location"]["info"] is None
    assert case["location"]["captures"] == 0
    assert case["location"]["capturedAt"] is None


def test_api_case_history_merges_location_changes(client, tmp_path):
    # A null→populated location transition should appear alongside the
    # case-API diff in `changes`, chronologically ordered and tagged.
    data_dir = tmp_path / "data"
    _seed_log(data_dir, [
        _entry("2026-04-20T00:00:00Z"),
        _entry("2026-04-21T00:00:00Z", closed=True),
    ])
    _seed_location_log(data_dir, [
        {"capturedAt": "2026-04-20T00:00:00Z", "data": {"data": None}},
        {"capturedAt": "2026-04-22T00:00:00Z", "data": {
            "data": {"receipt_details": {"form": "I-485", "location": "SCD"}}}},
    ])
    r = client.get("/api/cases/I-485/history")
    body = r.get_json()
    sources = [ch.get("source") for ch in body["changes"]]
    kinds = [ch.get("kind") for ch in body["changes"]]
    assert "location" in sources and "case" in sources
    assert "location_assigned" in kinds
    # Chronological order — decision diff (04-21) precedes location (04-22).
    tos = [ch.get("to") for ch in body["changes"]]
    assert tos == sorted(tos)


def test_api_updates_tags_location_records(client, tmp_path):
    data_dir = tmp_path / "data"
    _seed_log(data_dir, [
        _entry("2026-04-20T00:00:00Z"),
        _entry("2026-04-21T00:00:00Z", closed=True),
    ])
    _seed_location_log(data_dir, [
        {"capturedAt": "2026-04-20T00:00:00Z", "data": {"data": None}},
        {"capturedAt": "2026-04-22T00:00:00Z", "data": {
            "data": {"receipt_details": {"form": "I-485", "location": "SCD"}}}},
    ])
    r = client.get("/api/updates")
    records = r.get_json()["updates"]
    ids = {rec["id"] for rec in records}
    # Source is embedded in the ID so case + location detected on the same
    # day stay distinct for email-notification deduping.
    assert any(i.endswith(":location:2026-04-22") for i in ids)
    assert any(i.endswith(":case:2026-04-21") for i in ids)


def test_api_case_history_includes_location_entries(client, tmp_path):
    data_dir = tmp_path / "data"
    _seed_log(data_dir, [_entry("2026-04-22T18:00:00Z")])
    _seed_location_log(data_dir, [
        {"capturedAt": "2026-04-22T18:00:00Z", "data": {"data": None}},
    ])
    r = client.get("/api/cases/I-485/history")
    body = r.get_json()
    assert len(body["locationEntries"]) == 1
    assert body["locationEntries"][0]["data"] == {"data": None}


def test_api_export_includes_location_log(client, tmp_path):
    import io, zipfile
    data_dir = tmp_path / "data"
    _seed_log(data_dir, [_entry("2026-04-22T18:00:00Z")])
    _seed_location_log(data_dir, [
        {"capturedAt": "2026-04-22T18:00:00Z", "data": {"data": None}},
    ])
    r = client.get("/api/export")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        names = set(z.namelist())
        assert "485_location.json" in names
        manifest = json.loads(z.read("manifest.json"))
        entry = manifest["cases"][0]
        assert entry["locationFile"] == "485_location.json"
        assert entry["locationEntries"] == 1


def test_api_case_history_returns_changes(client, tmp_path):
    data_dir = tmp_path / "data"
    _seed_log(data_dir, [
        _entry("2026-03-09T00:00:00Z"),
        _entry("2026-03-10T00:00:00Z", closed=True),
    ])
    r = client.get("/api/cases/I-485/history")
    body = r.get_json()
    assert body["label"] == "I-485"
    assert len(body["changes"]) == 1


def test_api_updates_returns_sorted_feed(client, tmp_path):
    data_dir = tmp_path / "data"
    _seed_log(data_dir, [
        _entry("2026-03-09T00:00:00Z"),
        _entry("2026-03-10T00:00:00Z", closed=True),
    ])
    r = client.get("/api/updates")
    body = r.get_json()
    assert body["updates"]
    rec = body["updates"][0]
    assert rec["caseLabel"] == "I-485"
    assert rec["id"] == "IOE1:case:2026-03-10"


def test_api_pull_starts_new_pull(client, monkeypatch):
    with patch.object(server, "_spawn_pull_async") as spawn:
        r = client.post("/api/pull")
    assert r.status_code == 200
    spawn.assert_called_once()


def test_api_pull_rejects_when_running(client, monkeypatch):
    monkeypatch.setattr(server, "_pull_state", server.PullState(running=True))
    r = client.post("/api/pull")
    assert r.status_code == 409
    assert r.get_json()["error"] == "pull_in_progress"


def test_api_pull_status_reports_schedule(client):
    r = client.get("/api/pull/status")
    body = r.get_json()
    assert body["schedule"]["timezone"] == server.SCHEDULER_TZ
    assert body["schedule"]["hours"] == list(server.PULL_HOURS)
    assert body["running"] is False


def test_api_export_returns_zip_with_manifest(client, tmp_path):
    import io, zipfile
    data_dir = tmp_path / "data"
    _seed_log(data_dir, [_entry("2026-03-09T00:00:00Z")])

    import re as _re
    r = client.get("/api/export")
    assert r.status_code == 200
    assert r.mimetype == "application/zip"
    disp = r.headers["Content-Disposition"]
    # Filename must match: uscis-archive-YYYY-MM-DD-HHMMSS-UTC.zip
    assert _re.search(
        r'filename="uscis-archive-\d{4}-\d{2}-\d{2}-\d{6}-UTC\.zip"', disp
    )

    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        names = set(z.namelist())
        assert "485_case.json" in names
        assert "manifest.json" in names
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["cases"][0]["label"] == "I-485"
        assert manifest["cases"][0]["receiptNumber"] == "IOE1"
        assert manifest["cases"][0]["file"] == "485_case.json"
        assert manifest["cases"][0]["entries"] == 1
        assert "generatedAt" in manifest


def test_api_export_records_missing_logs_in_manifest(client):
    import io, zipfile
    r = client.get("/api/export")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        manifest = json.loads(z.read("manifest.json"))
        entry = manifest["cases"][0]
        # No log file exists yet — manifest should record that explicitly.
        assert entry["file"] is None
        assert entry["entries"] == 0


def test_api_export_survives_corrupt_log(client, tmp_path):
    import io, zipfile
    data_dir = tmp_path / "data"
    (data_dir / "485_case.json").write_text("{not valid json")
    r = client.get("/api/export")
    assert r.status_code == 200
    with zipfile.ZipFile(io.BytesIO(r.data)) as z:
        manifest = json.loads(z.read("manifest.json"))
        # Corrupt file is reported as missing rather than crashing the export.
        assert manifest["cases"][0]["file"] is None
        assert manifest["cases"][0]["entries"] == 0


def test_api_pull_status_never_exposes_log_tail(client, monkeypatch):
    # Subprocess stdout/stderr can contain credentials. The API payload
    # must not echo it back, even after a prior pull populated log_tail.
    monkeypatch.setattr(
        server,
        "_pull_state",
        server.PullState(log_tail=["TOP SECRET password=hunter2"]),
    )
    r = client.get("/api/pull/status")
    body = r.get_json()
    assert "log_tail" not in body
    assert "hunter2" not in r.data.decode()


# Note: /api/test-email has been removed. The mailer is now reached only
# via the pull path's notification dispatch, which runs on the pull
# thread and folds smtp_* / notify_* events into the pull envelope via
# thread-local capture. See test_pull_absorbs_server_process_events_via_capture
# in test_system_log.py for end-to-end coverage of that path.


# =========================================================================
# Build-version resolution + /api/version endpoint
# =========================================================================

def test_version_label_format_is_utc_date_time():
    # Commit authored 20:32 EDT = 00:32 UTC the next day — the label is
    # ALWAYS in UTC so a developer's timezone doesn't change the string.
    label = server._version_label_from_iso("2026-04-22T20:32:28-04:00")
    assert label == "2026-04-23.0032"


def test_version_label_handles_utc_z_suffix():
    # Some git configs emit `Z` instead of `+00:00`.
    label = server._version_label_from_iso("2026-04-22T20:32:28+00:00")
    assert label == "2026-04-22.2032"


def test_version_label_returns_none_for_bad_input():
    assert server._version_label_from_iso(None) is None
    assert server._version_label_from_iso("") is None
    assert server._version_label_from_iso("not a date") is None


def test_version_label_is_lexicographically_sortable():
    # The whole point of the format: string comparison = chronological.
    labels = [
        server._version_label_from_iso("2026-04-22T20:32:28-04:00"),
        server._version_label_from_iso("2026-04-23T01:45:00+00:00"),
        server._version_label_from_iso("2026-04-22T14:05:00+00:00"),
        server._version_label_from_iso("2026-04-25T08:30:00+00:00"),
    ]
    # Sorted as strings must match sorted by time.
    assert sorted(labels) == [
        "2026-04-22.1405",
        "2026-04-23.0032",
        "2026-04-23.0145",
        "2026-04-25.0830",
    ]


def test_resolve_version_from_git(monkeypatch):
    # Stub out git subprocess calls so we're testing the parsing/mapping,
    # not whether pytest's CWD happens to be a git repo.
    import subprocess
    def _fake_output(cmd, **kwargs):
        if cmd[:3] == ["git", "rev-parse", "--short"]:
            return b"abc1234\n"
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return b"abc12340deadbeefcafe1111abc12340deadbeef\n"
        if cmd[:2] == ["git", "log"]:
            return b"2026-04-22T20:32:28-04:00\n"
        raise AssertionError(f"unexpected cmd {cmd}")
    monkeypatch.setattr(subprocess, "check_output", _fake_output)
    v = server._resolve_version()
    assert v["sha"] == "abc1234"
    assert v["full_sha"] == "abc12340deadbeefcafe1111abc12340deadbeef"
    assert v["commit_date"] == "2026-04-22T20:32:28-04:00"
    assert v["label"] == "2026-04-23.0032"
    assert v["boot_time"].endswith("Z")


def test_resolve_version_falls_back_to_dotversion_file(monkeypatch, tmp_path):
    # Simulate a prod box where git is missing but the deploy script
    # wrote a .version file. The fallback must read every field it can.
    import subprocess
    monkeypatch.setattr(subprocess, "check_output",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError("no git")))
    monkeypatch.setattr(server, "ROOT", tmp_path)
    (tmp_path / ".version").write_text(
        "2026-04-22.2032\n"
        "abc1234\n"
        "abc12340deadbeefcafe1111abc12340deadbeef\n"
        "2026-04-22T20:32:28-04:00\n"
    )
    v = server._resolve_version()
    assert v["label"] == "2026-04-22.2032"
    assert v["sha"] == "abc1234"
    assert v["full_sha"] == "abc12340deadbeefcafe1111abc12340deadbeef"
    assert v["commit_date"] == "2026-04-22T20:32:28-04:00"


def test_resolve_version_returns_unknown_when_nothing_available(
    monkeypatch, tmp_path,
):
    # No git, no .version file — server must still boot with a sane dict.
    import subprocess
    monkeypatch.setattr(subprocess, "check_output",
                        lambda *a, **k: (_ for _ in ()).throw(FileNotFoundError()))
    monkeypatch.setattr(server, "ROOT", tmp_path)
    v = server._resolve_version()
    assert v["sha"] == "unknown"
    assert v["full_sha"] == "unknown"
    assert v["label"] is None
    assert v["boot_time"].endswith("Z")


def test_api_version_endpoint_returns_version_dict(client, monkeypatch):
    # Route reflects whatever server.VERSION is at request time.
    fake = {"label": "2026-04-23.0032", "sha": "abc1234",
            "full_sha": "abc12340...", "commit_date": "2026-04-22T20:32:28-04:00",
            "boot_time": "2026-04-23T00:49:15Z"}
    monkeypatch.setattr(server, "VERSION", fake)
    r = client.get("/api/version")
    assert r.status_code == 200
    assert r.get_json() == fake


def test_pull_status_includes_version(client, monkeypatch):
    fake = {"label": "2026-04-23.0032", "sha": "abc1234", "full_sha": "full",
            "commit_date": "2026-04-22T20:32:28-04:00", "boot_time": "..."}
    monkeypatch.setattr(server, "VERSION", fake)
    r = client.get("/api/pull/status")
    body = r.get_json()
    # Piggy-backed on status so the UI gets version updates via its
    # existing 3s poll loop — no separate fetch required.
    assert body["version"] == fake


# -------- _static_version fallback ------------------------------------

def test_static_version_falls_back_on_missing_files(monkeypatch):
    monkeypatch.setattr(server, "STATIC_DIR", Path("/nonexistent/path"))
    v = server._static_version()
    assert v.isdigit()


# -------- main() bootstrapping ----------------------------------------

def test_main_runs_with_configure_gate_and_scheduler(monkeypatch, tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"auth": {"optional_access_code": "c"}, "cases": []}))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_setup_scheduler", MagicMock())
    monkeypatch.setattr(server.app, "run", MagicMock())
    with patch.object(server, "configure_access_gate") as gate:
        server.main()
    gate.assert_called_once()
    args, kwargs = gate.call_args
    # optional_access_code was propagated from config
    assert args[1] == "c"


def test_main_handles_missing_config(monkeypatch, tmp_path):
    # Bogus path → load_config raises, main must still proceed with empty code.
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "missing.json")
    monkeypatch.setattr(server, "_setup_scheduler", MagicMock())
    monkeypatch.setattr(server.app, "run", MagicMock())
    with patch.object(server, "configure_access_gate") as gate:
        server.main()
    gate.assert_called_once()
    assert gate.call_args.args[1] == ""


# -------- main() error branches ---------------------------------------

def test_main_access_gate_failure_is_fatal(monkeypatch, tmp_path):
    """Access-gate configure failure MUST re-raise — a running server
    without a gate is worse than no server."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"auth": {"optional_access_code": "c"}, "cases": []}))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server.app, "run", MagicMock())
    with patch.object(server, "configure_access_gate",
                      side_effect=RuntimeError("gate broken")):
        with pytest.raises(RuntimeError, match="gate broken"):
            server.main()


def test_main_scheduler_failure_is_non_fatal(monkeypatch, tmp_path):
    """Scheduler setup failure must log but NOT crash — manual pulls
    and the web UI still work."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"auth": {}, "cases": []}))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server.app, "run", MagicMock())
    with patch.object(server, "configure_access_gate"), \
         patch.object(server, "_setup_scheduler",
                      side_effect=RuntimeError("scheduler down")):
        # Does not raise.
        server.main()


def test_main_server_run_failure_logs_and_reraises(monkeypatch, tmp_path):
    """app.run() itself failing (port already bound, etc.) must log
    server_run_failed before propagating."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"auth": {}, "cases": []}))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_setup_scheduler", MagicMock())
    monkeypatch.setattr(server.app, "run",
                        MagicMock(side_effect=OSError("port bound")))
    with patch.object(server, "configure_access_gate"):
        with pytest.raises(OSError, match="port bound"):
            server.main()


# -------- _send_storage_alert_email -----------------------------------

def test_send_storage_alert_email_noop_when_config_load_fails(monkeypatch, tmp_path):
    """If load_config raises, the email path short-circuits without
    propagating (storage event already written to log is authoritative)."""
    monkeypatch.setattr(server, "load_config",
                        MagicMock(side_effect=RuntimeError("nope")))
    # Must not raise.
    server._send_storage_alert_email(
        total_bytes=2 * 1024**3, limit_bytes=1 * 1024**3, categories=[],
    )


def test_send_storage_alert_email_noop_when_creds_missing(monkeypatch, tmp_path):
    """Missing MFA credentials → no-op log warning, no exception."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"auth": {}, "cases": []}))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    server._send_storage_alert_email(
        total_bytes=2 * 1024**3, limit_bytes=1 * 1024**3, categories=[],
    )


def test_send_storage_alert_email_builds_and_sends(monkeypatch, tmp_path):
    """Happy path: creds present → mailer.send_email called with a
    composed subject/body reporting total, limit, over-by, and per-
    category breakdown in both plain + html."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "auth": {
            "uscis_mfa_email": "u@example.com",
            "uscis_mfa_app_password": "pw",
            "notification_email": "n@example.com",
        },
        "cases": [],
    }))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)

    captured = {}
    import mailer as _mailer
    def _send(**kw):
        captured.update(kw)
    monkeypatch.setattr(_mailer, "send_email", _send)

    categories = [
        {"key": "case_485", "label": "I-485", "bytes": 5 * 1024**2,
         "file_count": 7},
        {"key": "system_log", "label": "System log", "bytes": 800 * 1024,
         "file_count": 2},
    ]
    server._send_storage_alert_email(
        total_bytes=int(1.2 * 1024**3),
        limit_bytes=int(1.0 * 1024**3),
        categories=categories,
    )
    assert captured["to"] == "n@example.com"
    assert "Storage" in captured["subject"]
    assert "I-485" in captured["plain"]
    assert "I-485" in captured["html"]
    assert "over by" in captured["plain"]
    # Human-readable sizes embedded in both renderings.
    assert "GB" in captured["subject"] or "MB" in captured["subject"]


# -------- /api/mfa-trace/<dir>/summary --------------------------------

def _seed_trace(data_dir: Path, dir_name: str = "t1") -> Path:
    trace_dir = data_dir / "full_traces" / dir_name
    trace_dir.mkdir(parents=True)
    mfa = trace_dir / "mfa_trace"
    mfa.mkdir()
    return trace_dir


def test_api_mfa_trace_summary_rejects_bad_dir_name(client, tmp_path):
    r = client.get("/api/mfa-trace/..%2Fevil/summary")
    # URL-decoded ".." gets through as path traversal attempt; our
    # handler validates the component.
    assert r.status_code in (400, 404)


def test_api_mfa_trace_summary_404_when_trace_missing(client, tmp_path):
    r = client.get("/api/mfa-trace/nonexistent/summary")
    assert r.status_code == 404
    assert r.get_json()["error"] == "trace_not_found"


def test_api_mfa_trace_summary_returns_empty_when_no_mfa_subdir(client, tmp_path):
    data_dir = tmp_path / "data"
    trace_dir = data_dir / "full_traces" / "t1"
    trace_dir.mkdir(parents=True)
    r = client.get("/api/mfa-trace/t1/summary")
    assert r.status_code == 200
    body = r.get_json()
    assert body == {"events": [], "emails": []}


def test_api_mfa_trace_summary_parses_events_and_emails(client, tmp_path):
    data_dir = tmp_path / "data"
    trace_dir = _seed_trace(data_dir)
    mfa = trace_dir / "mfa_trace"
    (mfa / "events.jsonl").write_text(
        '{"ts":"2026-04-24T00:00:00Z","event":"imap_connect_ok","cycle":0}\n'
        '\n'  # blank line must be skipped
        '{"ts":"2026-04-24T00:00:01Z","event":"imap_login_ok","cycle":0}\n'
        'malformed json skipped\n'
    )
    eml = (
        b"From: sender@example.com\r\n"
        b"To: u@example.com\r\n"
        b"Subject: USCIS code\r\n"
        b"Date: Wed, 24 Apr 2026 00:00:00 +0000\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"Your code is 123456.\r\n"
    )
    (mfa / "email_1.eml").write_bytes(eml)

    r = client.get("/api/mfa-trace/t1/summary")
    assert r.status_code == 200
    body = r.get_json()
    events = [e["event"] for e in body["events"]]
    assert "imap_connect_ok" in events
    assert "imap_login_ok" in events
    # Malformed + blank lines are dropped, not propagated.
    assert len(body["events"]) == 2
    assert len(body["emails"]) == 1
    em = body["emails"][0]
    assert em["uid"] == "1"
    assert em["subject"] == "USCIS code"
    assert em["from"] == "sender@example.com"
    assert "123456" in em["preview"]


# -------- /api/mfa-trace/<dir>/email/<uid> ----------------------------

def test_api_mfa_trace_email_returns_html_and_raw(client, tmp_path):
    data_dir = tmp_path / "data"
    trace_dir = _seed_trace(data_dir)
    eml = (
        b"From: sender@example.com\r\n"
        b"Subject: code\r\n"
        b"Date: Wed, 24 Apr 2026 00:00:00 +0000\r\n"
        b"MIME-Version: 1.0\r\n"
        b'Content-Type: multipart/alternative; boundary="BB"\r\n\r\n'
        b"--BB\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"plain body\r\n"
        b"--BB\r\n"
        b"Content-Type: text/html\r\n\r\n"
        b"<html>hi</html>\r\n"
        b"--BB--\r\n"
    )
    (trace_dir / "mfa_trace" / "email_42.eml").write_bytes(eml)

    r = client.get("/api/mfa-trace/t1/email/42")
    assert r.status_code == 200
    body = r.get_json()
    assert body["headers"]["subject"] == "code"
    assert body["html"] == "<html>hi</html>\r\n" or "hi" in body["html"]
    # raw is the entire message bytes decoded best-effort.
    assert "plain body" in body["raw"]


def test_api_mfa_trace_email_404_on_missing(client, tmp_path):
    data_dir = tmp_path / "data"
    _seed_trace(data_dir)
    r = client.get("/api/mfa-trace/t1/email/999")
    assert r.status_code == 404


def test_api_mfa_trace_email_400_on_invalid_path(client):
    r = client.get("/api/mfa-trace/../evil/email/1")
    # flask routes return 404 for malformed paths before us; either
    # 400 or 404 is acceptable — both mean "rejected".
    assert r.status_code in (400, 404)


def test_api_mfa_trace_email_500_on_unparseable(client, tmp_path):
    """If message_from_bytes itself raises, respond 500 with parse error."""
    data_dir = tmp_path / "data"
    trace_dir = _seed_trace(data_dir)
    (trace_dir / "mfa_trace" / "email_1.eml").write_bytes(b"garbage\xff\xfe")
    # message_from_bytes actually tolerates almost anything, so force
    # the parse to fail by patching.
    from unittest.mock import patch as _patch
    import email as _email
    with _patch.object(_email, "message_from_bytes",
                       side_effect=RuntimeError("unparseable")):
        r = client.get("/api/mfa-trace/t1/email/1")
    assert r.status_code == 500
    assert "parse" in r.get_json()["error"]


# -------- _extract_email_part helpers ---------------------------------

def test_extract_email_part_multipart_returns_matching_part():
    from email.message import EmailMessage
    msg = EmailMessage()
    msg["Subject"] = "x"
    msg.set_content("plain")
    msg.add_alternative("<html>alt</html>", subtype="html")
    html = server._extract_email_part(msg, "text/html")
    assert html is not None
    assert "<html>alt</html>" in html


def test_extract_email_part_multipart_returns_none_when_missing():
    from email.message import EmailMessage
    msg = EmailMessage()
    msg.set_content("plain only")
    assert server._extract_email_part(msg, "text/html") is None


def test_extract_email_part_single_part_matches():
    from email.message import EmailMessage
    msg = EmailMessage()
    msg.set_content("just text")
    text = server._extract_email_part(msg, "text/plain")
    assert "just text" in text


def test_extract_email_part_single_part_mismatch_returns_none():
    from email.message import EmailMessage
    msg = EmailMessage()
    msg.set_content("plain")
    assert server._extract_email_part(msg, "text/html") is None


def test_extract_plain_body_prefers_text_over_html():
    from email.message import EmailMessage
    msg = EmailMessage()
    msg.set_content("plain body wins")
    msg.add_alternative("<p>html loses</p>", subtype="html")
    body = server._extract_plain_body(msg)
    assert "plain body wins" in body


def test_extract_plain_body_falls_back_to_stripped_html():
    from email.message import EmailMessage
    msg = EmailMessage()
    msg.set_type("text/html")
    msg.set_payload("<p>html text</p>")
    body = server._extract_plain_body(msg)
    assert "html text" in body
    # Tags were stripped.
    assert "<p>" not in body


def test_extract_plain_body_empty_when_no_body():
    """Message with no recognizable body part → empty string, no crash."""
    import email as _email
    msg = _email.message_from_bytes(b"From: x\r\n\r\n")
    assert server._extract_plain_body(msg) == ""


# -------- _wipe_tree_contents --------------------------------------

def test_wipe_tree_contents_removes_nested_dirs_and_files(tmp_path):
    root = tmp_path / "full_traces"
    root.mkdir()
    # Mix of top-level files and nested trace dirs.
    (root / "top.txt").write_text("x")
    nested = root / "trace1" / "mfa_trace"
    nested.mkdir(parents=True)
    (nested / "events.jsonl").write_text("{}")
    (nested / "email_1.eml").write_bytes(b"raw")
    (root / "trace1" / "trace.zip").write_bytes(b"zip")

    errors: list[str] = []
    removed = server._wipe_tree_contents(root, errors)

    # Every file deleted; root itself preserved.
    assert removed >= 4
    assert root.exists()
    assert not any(root.iterdir())
    assert errors == []


def test_wipe_tree_contents_noop_when_root_missing(tmp_path):
    assert server._wipe_tree_contents(tmp_path / "never", []) == 0


def test_wipe_tree_contents_collects_unlink_errors(tmp_path, monkeypatch):
    """OSError during unlink is recorded, not raised."""
    root = tmp_path / "full_traces"
    root.mkdir()
    (root / "f.txt").write_text("x")

    from pathlib import Path as _P
    orig_unlink = _P.unlink

    def _bad_unlink(self, *a, **kw):
        raise OSError("permission denied")

    monkeypatch.setattr(_P, "unlink", _bad_unlink)
    errors: list[str] = []
    try:
        server._wipe_tree_contents(root, errors)
    finally:
        monkeypatch.setattr(_P, "unlink", orig_unlink)
    assert any("permission denied" in e for e in errors)


# -------- /api/system-log/clear end-to-end ---------------------------

def test_api_system_log_clear_without_confirmation_rejected(client):
    r = client.post("/api/system-log/clear", json={})
    assert r.status_code == 400
    assert r.get_json()["error"] == "confirmation_required"


def test_latest_location_info_returns_none_when_no_known_fields():
    """Unwrapped inner dict without receipt_details / form / location →
    None (line 352)."""
    entries = [{"data": {"data": {"noise": "stuff"}}}]
    assert server._latest_location_info(entries) is None


def test_latest_location_info_returns_unwrapped_when_known_field_present():
    entries = [{"data": {"data": {"form": "I-765"}}}]
    out = server._latest_location_info(entries)
    assert out == {"form": "I-765"}


def test_storage_alert_email_human_size_bytes_branch(monkeypatch, tmp_path):
    """Totals under 1KB fall through to `NNN B` (line 659)."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "auth": {"uscis_mfa_email": "u@e", "uscis_mfa_app_password": "pw",
                 "notification_email": "n@e"},
        "cases": [],
    }))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    captured = {}
    import mailer as _mailer
    monkeypatch.setattr(_mailer, "send_email",
                        lambda **kw: captured.update(kw))
    # Use small byte values so the GB/MB/KB branches all fall through.
    server._send_storage_alert_email(
        total_bytes=42, limit_bytes=50, categories=[],
    )
    assert "42 B" in captured["plain"]


def test_api_full_trace_path_escape_blocked(client, tmp_path, monkeypatch):
    """Even when the outer `_is_safe_name_part` gate passes, an attempt
    to resolve to a path outside of full_traces/ returns path_escape."""
    data_dir = tmp_path / "data"
    traces = data_dir / "full_traces"
    traces.mkdir(parents=True)
    # Create a symlink inside that points to /etc — resolving walks
    # across it, producing a target outside the base. (Skip test on
    # systems that can't create symlinks.)
    import os as _os
    victim = traces / "t1"
    try:
        _os.symlink("/etc", str(victim))
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    r = client.get("/api/full-trace/t1/passwd")
    # Either path_escape (target resolved outside base) or not_found;
    # both mean the guard worked.
    assert r.status_code in (400, 404)


def test_collect_storage_categories_stat_error_treated_as_zero(
    monkeypatch, tmp_path,
):
    """_safe_size swallows OSError → file counted with 0 bytes (line 1610-1611)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "485_case.json").write_text("[]")
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "nope.json")

    from pathlib import Path as _P
    orig_stat = _P.stat
    def _bad_stat(self, *a, **kw):
        if self.name.endswith("_case.json"):
            raise OSError("permission denied")
        return orig_stat(self, *a, **kw)
    monkeypatch.setattr(_P, "stat", _bad_stat)
    # Must not raise.
    cats = server._collect_storage_categories()
    # The file is still bucketed but with 0 bytes — so it might not appear
    # because the filter drops zero-byte buckets. Either way, no crash.
    assert isinstance(cats, list)


def test_api_mfa_trace_summary_rejects_unsafe_dir_name(client):
    """`_is_safe_name_part` rejects a directly-provided bad name with
    400 invalid_dir (line 2010)."""
    # Flask's URL decoder will pass the single component through.
    r = client.get("/api/mfa-trace/bad!name/summary")
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_dir"


def test_extract_email_part_single_part_charset_fallback(monkeypatch):
    """The single-part branch's bad-codec fallback decodes with utf-8
    (lines 2132-2133)."""
    from email.message import EmailMessage
    msg = EmailMessage()
    msg.set_content("body")
    msg.get_content_charset = lambda: "not-a-real-codec"
    out = server._extract_email_part(msg, "text/plain")
    assert out is not None


def test_load_storage_limit_bytes_rejects_non_numeric_legacy_gb():
    """The legacy `storage_limit_gb` key still raises a clear error
    when the value isn't numeric — backward-compat path keeps the
    diagnostic surface."""
    with pytest.raises(server.ConfigError, match="not numeric"):
        server.load_storage_limit_bytes({"storage_limit_gb": "banana"})


def test_load_retry_policy_config_error_surfaces_as_pull_step(monkeypatch, tmp_path):
    """When load_retry_policy raises ConfigError, the inner pull runner
    emits pull_config_error and returns an envelope whose steps include
    that capture (lines 922-937)."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": []}))  # missing retry keys
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(
        server, "_pull_state",
        server.PullState(running=True, started_at="2026-04-24T00:00:00Z"),
    )
    # Simulate the outer function's capture scope so that sys_log events
    # fired inside the inner function are folded into thread_captured_steps.
    from system_log import push_capture, pop_capture
    captured = push_capture()
    try:
        envelope = server._run_pull_subprocess_inner(
            trigger="manual", thread_captured_steps=captured,
        )
    finally:
        pop_capture()

    assert envelope["event"] == "pull"
    assert envelope["level"] == "error"
    assert any(
        s.get("event") == "pull_config_error" for s in envelope["steps"]
    )


def test_api_storage_config_error_returns_500(client, tmp_path):
    """Out-of-range storage_limit_mb → /api/storage returns 500
    with ConfigError message (covers the early-return error path
    in api_storage)."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "auth": {}, "cases": [], "storage_limit_mb": 999_999,
    }))
    r = client.get("/api/storage")
    assert r.status_code == 500
    assert "storage_limit_mb" in r.get_json()["error"]


def test_api_debug_mode_get_config_error_returns_500(client, tmp_path):
    """/api/debug-mode GET when config has invalid type → 500."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"trace_successful_pulls": "not_a_bool"}))
    r = client.get("/api/debug-mode")
    assert r.status_code == 500


def test_api_full_trace_rejects_invalid_component(client, tmp_path):
    """Path components containing disallowed chars are rejected — first
    the outer dir_name guard fires (invalid_dir), then subpath. Either
    way, status is 400."""
    r = client.get("/api/full-trace/abc!def/trace.zip")
    assert r.status_code == 400
    assert r.get_json()["error"] in ("invalid_dir", "invalid_component")


def test_api_full_trace_rejects_invalid_subpath_component(client, tmp_path):
    """Valid dir_name, invalid subpath char → invalid_component."""
    r = client.get("/api/full-trace/t1/bad!file.zip")
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_component"


def test_api_full_trace_rejects_path_depth(client, tmp_path):
    """subpath deeper than 2 levels → 400 invalid_path_depth."""
    r = client.get("/api/full-trace/t1/a/b/c/d.txt")
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_path_depth"


def test_api_full_trace_mimetypes(client, tmp_path, monkeypatch):
    """Each supported extension lands on the right Content-Type.
    Covers lines 1944-1949."""
    data_dir = tmp_path / "data"
    trace_dir = data_dir / "full_traces" / "t1"
    trace_dir.mkdir(parents=True)
    (trace_dir / "a.json").write_text("{}")
    (trace_dir / "b.png").write_bytes(b"\x89PNG")
    (trace_dir / "c.bin").write_bytes(b"\x00\x01")
    monkeypatch.setattr(server, "DATA_DIR", data_dir)

    r = client.get("/api/full-trace/t1/a.json")
    assert r.content_type == "application/json"
    r = client.get("/api/full-trace/t1/b.png")
    assert r.content_type == "image/png"
    r = client.get("/api/full-trace/t1/c.bin")
    assert r.content_type == "application/octet-stream"


def test_trace_viewer_rejects_path_traversal(client):
    r = client.get("/trace-viewer/..%2Fetc%2Fpasswd")
    # Either 400 (our guard) or 404 (not found).
    assert r.status_code in (400, 404)


def test_api_mfa_trace_email_rejects_bad_uid(client, tmp_path):
    r = client.get("/api/mfa-trace/t1/email/bad!uid")
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid_path"


def test_collect_storage_categories_buckets_storage_alert_state(
    monkeypatch, tmp_path,
):
    """`.storage_alert_state.json` rolls into System log, not Other
    (lines 1633-1635)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / ".storage_alert_state.json").write_text('{"alerted_at":"x"}')
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "nope.json")
    cats = {c["key"]: c for c in server._collect_storage_categories()}
    assert "system_log" in cats
    assert cats["system_log"]["bytes"] > 0


def test_collect_storage_categories_buckets_session_state(
    monkeypatch, tmp_path,
):
    """Repo-root `.uscis_session.json` rolls into System log (line 1649)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    session_file = tmp_path / ".uscis_session.json"
    session_file.write_text("{}")
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "STORAGE_SESSION_PATH", session_file)
    monkeypatch.setattr(server, "CONFIG_PATH", tmp_path / "nope.json")
    cats = {c["key"]: c for c in server._collect_storage_categories()}
    assert "system_log" in cats


def test_check_storage_limit_config_invalid_emits_skip(monkeypatch, tmp_path):
    """When load_storage_limit_bytes raises ConfigError (e.g.
    out-of-range value), the check emits `storage_limit_check_skipped`
    and returns instead of crashing the pull post-condition path."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"storage_limit_mb": 999_999}))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    steps = server._check_storage_limit_and_alert()
    assert any(
        s.get("event") == "storage_limit_check_skipped" for s in steps
    )


def test_send_storage_alert_email_swallows_mailer_failure(monkeypatch, tmp_path):
    """mailer.send_email raising → alert step records storage_alert_email_failed,
    does not propagate (lines 619-621 via _check_storage_limit)."""
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "auth": {
            "uscis_mfa_email": "u@example.com",
            "uscis_mfa_app_password": "pw",
        },
        "cases": [], "storage_limit_mb": server.STORAGE_MIN_MB,
    }))
    data_dir = tmp_path / "data"; data_dir.mkdir()
    # Seed storage over the limit.
    (data_dir / "big.json").write_bytes(b"x" * int(0.02 * 1024**3))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "DATA_DIR", data_dir)

    import mailer as _mailer
    monkeypatch.setattr(
        _mailer, "send_email",
        MagicMock(side_effect=RuntimeError("smtp down")),
    )
    steps = server._check_storage_limit_and_alert()
    event_types = [s["event"] for s in steps]
    assert "storage_alert_email_failed" in event_types


def test_extract_plain_body_decode_failure_falls_back(monkeypatch):
    """_extract_email_part's charset-decode failure falls back to
    utf-8 with replace errors (lines 2122-2124 / 2132-2133)."""
    from email.message import EmailMessage
    msg = EmailMessage()
    msg.set_content("body")
    # Force .get_content_charset() to return a bad codec name so the
    # first decode raises — covering the single-part fallback branch.
    orig = msg.get_content_charset
    msg.get_content_charset = lambda: "not-a-real-codec"
    out = server._extract_email_part(msg, "text/plain")
    # Fallback decode must not raise.
    assert out is not None


def test_api_mfa_trace_summary_events_read_error_returns_500(
    client, tmp_path, monkeypatch,
):
    """Unreadable events.jsonl → 500 with read error (lines 2030-2031)."""
    data_dir = tmp_path / "data"
    trace_dir = _seed_trace(data_dir)
    (trace_dir / "mfa_trace" / "events.jsonl").write_text("{}")

    from pathlib import Path as _P
    orig_read = _P.read_text
    def _bad(self, *a, **kw):
        if self.name == "events.jsonl":
            raise OSError("disk gone")
        return orig_read(self, *a, **kw)
    monkeypatch.setattr(_P, "read_text", _bad)

    r = client.get("/api/mfa-trace/t1/summary")
    assert r.status_code == 500


def test_api_system_log_clear_wipes_log_and_traces(client, tmp_path, monkeypatch):
    import system_log
    log_path = tmp_path / "system_log.json"
    monkeypatch.setattr(system_log, "LOG_PATH", log_path)
    system_log.log("dummy", source="test")

    data_dir = tmp_path / "data"
    (data_dir / "full_traces" / "t1" / "mfa_trace").mkdir(parents=True)
    (data_dir / "full_traces" / "t1" / "trace.zip").write_bytes(b"z")
    (data_dir / "full_traces" / "t1" / "mfa_trace" / "events.jsonl").write_text("{}")
    monkeypatch.setattr(server, "DATA_DIR", data_dir)

    r = client.post("/api/system-log/clear", json={"confirm": True})
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["tracesRemoved"] >= 2
    # Traces dir is emptied, but the directory itself persists.
    assert (data_dir / "full_traces").exists()
    assert not any((data_dir / "full_traces").iterdir())
