"""Tests for the system event log.

Covers the log module in isolation (writes, reads, rotation, atomic
writes, failure tolerance) and then cross-checks that production call
sites actually emit the events we expect them to emit.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import system_log


@pytest.fixture(autouse=True)
def _redirect_log(monkeypatch, tmp_path):
    """Every test gets a fresh log file in a tmp dir."""
    monkeypatch.setattr(system_log, "LOG_PATH", tmp_path / "system_log.json")
    system_log.clear()
    yield


# -------- basic write / read -----------------------------------------

def test_log_writes_an_entry_with_required_fields():
    system_log.log("server_startup", schedule_hours=[7, 14, 20])
    entries = system_log.read_all()
    assert len(entries) == 1
    e = entries[0]
    assert e["event"] == "server_startup"
    assert e["level"] == "info"
    assert e["ts"].endswith("Z")
    assert isinstance(e["pid"], int)
    assert e["schedule_hours"] == [7, 14, 20]


def test_log_accepts_explicit_level():
    system_log.log("pull_failed", level="error", exit_code=1)
    e = system_log.read_all()[0]
    assert e["level"] == "error"
    assert e["exit_code"] == 1


def test_log_records_source_when_given():
    system_log.log("pull_started", source="scheduler")
    assert system_log.read_all()[0]["source"] == "scheduler"


def test_log_omits_source_when_not_given():
    system_log.log("anything")
    assert "source" not in system_log.read_all()[0]


# -------- edge cases / robustness -----------------------------------

def test_log_coerces_non_json_values_with_repr():
    class Weird:
        def __repr__(self): return "<weird thing>"
    system_log.log("strange_event", payload=Weird())
    e = system_log.read_all()[0]
    assert e["payload"] == "<weird thing>"


def test_read_all_with_limit():
    for i in range(10):
        system_log.log("tick", i=i)
    last_three = system_log.read_all(limit=3)
    assert [e["i"] for e in last_three] == [7, 8, 9]


def test_read_all_returns_empty_before_any_writes():
    assert system_log.read_all() == []


def test_clear_wipes_existing_file():
    system_log.log("e1")
    assert system_log.LOG_PATH.exists()
    system_log.clear()
    assert not system_log.LOG_PATH.exists()
    assert system_log.read_all() == []


def test_log_tolerates_missing_file(monkeypatch, tmp_path):
    # Point to a nested dir that doesn't exist yet; log() must create it.
    nested = tmp_path / "nested" / "deeper" / "system_log.json"
    monkeypatch.setattr(system_log, "LOG_PATH", nested)
    system_log.log("boot")
    assert nested.exists()


def test_log_recovers_from_corrupt_file(monkeypatch, tmp_path):
    path = tmp_path / "system_log.json"
    path.write_text("not valid json {")
    monkeypatch.setattr(system_log, "LOG_PATH", path)
    system_log.log("recovery_event")
    # After writing once, the file is overwritten to a valid JSON array.
    data = json.loads(path.read_text())
    assert isinstance(data, list)
    assert data[0]["event"] == "recovery_event"


# -------- rotation --------------------------------------------------

def test_log_rotates_when_exceeding_max(monkeypatch):
    monkeypatch.setattr(system_log, "MAX_ENTRIES", 5)
    for i in range(12):
        system_log.log("tick", i=i)
    entries = system_log.read_all()
    assert len(entries) == 5
    # Rotation drops OLDEST entries; newest are retained.
    assert [e["i"] for e in entries] == [7, 8, 9, 10, 11]


# -------- server-side integration: sys_log fires from the right places

def test_run_pull_subprocess_logs_started_and_finished(monkeypatch, tmp_path):
    import subprocess
    import server

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": []}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    from unittest.mock import MagicMock, patch
    proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch.object(subprocess, "run", return_value=proc), \
         patch.object(server, "_send_notifications_for_new"):
        server._run_pull_subprocess()

    events = [e["event"] for e in system_log.read_all()]
    assert "pull_started" in events
    assert "pull_finished" in events


def test_run_pull_subprocess_logs_failure_on_nonzero_exit(monkeypatch, tmp_path):
    import subprocess
    import server

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": []}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    from unittest.mock import MagicMock, patch
    proc = MagicMock(returncode=1, stdout="", stderr="Missing auth keys")
    with patch.object(subprocess, "run", return_value=proc):
        server._run_pull_subprocess()

    events = [e for e in system_log.read_all() if e["event"] == "pull_failed"]
    assert len(events) == 1
    assert events[0]["level"] == "error"
    assert events[0]["exit_code"] == 1


def test_run_pull_subprocess_logs_timeout(monkeypatch, tmp_path):
    import subprocess
    import server

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": []}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    from unittest.mock import patch
    with patch.object(
        subprocess, "run",
        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1),
    ):
        server._run_pull_subprocess()

    timeouts = [e for e in system_log.read_all() if e["event"] == "pull_timeout"]
    assert len(timeouts) == 1
    assert timeouts[0]["level"] == "error"


def test_run_pull_subprocess_logs_crash(monkeypatch, tmp_path):
    import subprocess
    import server

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": []}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    from unittest.mock import patch
    with patch.object(subprocess, "run", side_effect=RuntimeError("boom")):
        server._run_pull_subprocess()

    crashes = [e for e in system_log.read_all() if e["event"] == "pull_crashed"]
    assert len(crashes) == 1
    assert crashes[0]["error"] == "boom"


def test_run_pull_subprocess_logs_skip_when_already_running(monkeypatch, tmp_path):
    import server
    monkeypatch.setattr(server, "_pull_state", server.PullState(running=True))
    server._run_pull_subprocess()
    events = [e["event"] for e in system_log.read_all()]
    assert "pull_skipped_already_running" in events


def test_send_notifications_logs_skip_when_auth_missing(monkeypatch, tmp_path):
    import server
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"auth": {}}))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    server._send_notifications_for_new([{"id": "1", "kind": "event"}])
    events = [e for e in system_log.read_all() if e["event"] == "notify_skipped"]
    assert len(events) == 1
    assert events[0]["reason"] == "auth_missing"
    assert events[0]["count"] == 1


def test_send_notifications_logs_sent_per_record(monkeypatch, tmp_path):
    import server
    from unittest.mock import patch
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "auth": {
            "uscis_mfa_email": "u@example.com",
            "uscis_mfa_app_password": "pw",
        }
    }))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    with patch.object(server, "notify_update"):
        server._send_notifications_for_new([
            {"id": "IOE1:2026-03-10", "kind": "event"},
            {"id": "IOE2:2026-03-10", "kind": "silent_update"},
        ])
    sent = [e for e in system_log.read_all() if e["event"] == "notify_sent"]
    assert len(sent) == 2
    assert {e["record_id"] for e in sent} == {"IOE1:2026-03-10", "IOE2:2026-03-10"}


def test_send_notifications_logs_per_record_failure(monkeypatch, tmp_path):
    import server
    from unittest.mock import patch
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "auth": {
            "uscis_mfa_email": "u@example.com",
            "uscis_mfa_app_password": "pw",
        }
    }))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    with patch.object(server, "notify_update", side_effect=RuntimeError("smtp down")):
        server._send_notifications_for_new([{"id": "1", "kind": "event"}])
    failed = [e for e in system_log.read_all() if e["event"] == "notify_failed"]
    assert len(failed) == 1
    assert failed[0]["level"] == "error"
    assert failed[0]["record_id"] == "1"


def test_setup_scheduler_logs_configured_event(monkeypatch):
    import server
    from unittest.mock import MagicMock
    sched = MagicMock()
    monkeypatch.setattr(server, "scheduler", sched)
    server._setup_scheduler()
    events = [e for e in system_log.read_all() if e["event"] == "scheduler_configured"]
    assert len(events) == 1
    assert events[0]["hours"] == list(server.PULL_HOURS)
    assert events[0]["timezone"] == server.SCHEDULER_TZ


# -------- HTTP endpoint ---------------------------------------------

def test_api_system_log_returns_events(tmp_path, monkeypatch):
    import server

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "auth": {}}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)

    system_log.log("server_startup", schedule_hours=[7, 14, 20])
    system_log.log("pull_started", source="server")

    with server.app.test_client() as c:
        r = c.get("/api/system-log")
        assert r.status_code == 200
        body = r.get_json()
        assert "events" in body
        event_names = [e["event"] for e in body["events"]]
        assert "server_startup" in event_names
        assert "pull_started" in event_names


def test_api_system_log_respects_limit(monkeypatch, tmp_path):
    import server
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "auth": {}}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    for i in range(20):
        system_log.log("tick", i=i)
    with server.app.test_client() as c:
        r = c.get("/api/system-log?limit=5")
        events = r.get_json()["events"]
        assert len(events) == 5
        assert [e["i"] for e in events] == [15, 16, 17, 18, 19]


def test_api_system_log_ignores_bad_limit(monkeypatch, tmp_path):
    import server
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "auth": {}}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    system_log.log("tick")
    with server.app.test_client() as c:
        r = c.get("/api/system-log?limit=not-an-int")
        assert r.status_code == 200
        assert len(r.get_json()["events"]) == 1


# -------- export zip includes system_log.json -----------------------

def test_api_export_includes_system_log(monkeypatch, tmp_path):
    import io, zipfile
    import server

    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "auth": {}}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)

    system_log.log("server_startup", hours=[7, 14, 20])
    system_log.log("pull_finished", exit_code=0)

    with server.app.test_client() as c:
        r = c.get("/api/export")
        assert r.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r.data)) as z:
            names = set(z.namelist())
            assert "system_log.json" in names
            body = json.loads(z.read("system_log.json"))
            event_names = [e["event"] for e in body]
            assert "server_startup" in event_names
            assert "pull_finished" in event_names
