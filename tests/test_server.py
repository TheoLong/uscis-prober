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
    (data_dir / "485_logs.json").write_text(json.dumps(entries))


# -------- pure helpers ---------------------------------------------------

def test_log_file_for_recognises_form_numbers(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    assert server._log_file_for("I-485").name == "485_logs.json"


def test_log_file_for_none_for_unknown_form(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    assert server._log_file_for("???") is None


def test_load_entries_returns_empty_on_missing_file(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    assert server.load_entries("I-485") == []


def test_load_entries_returns_empty_on_invalid_json(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    (tmp_path / "485_logs.json").write_text("{broken")
    assert server.load_entries("I-485") == []


def test_load_entries_rejects_non_list_payload(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    (tmp_path / "485_logs.json").write_text('{"not": "list"}')
    assert server.load_entries("I-485") == []


def test_load_entries_happy_path(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    (tmp_path / "485_logs.json").write_text('[{"capturedAt": "x"}]')
    assert server.load_entries("I-485") == [{"capturedAt": "x"}]


def test_load_entries_unknown_form_is_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    assert server.load_entries("unknown") == []


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
    assert records[0]["id"] == "IOE1:2026-03-10"
    assert records[0]["caseLabel"] == "I-485"
    assert records[0]["detectedOn"] == "2026-03-10"


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
    cfg_path.write_text(json.dumps({"cases": []}))
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
    cfg_path.write_text(json.dumps({"cases": [{"id": "IOE1", "label": "I-485"}]}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    # First call (before pull): no entries. Second call (after pull): records.
    calls = {"n": 0}

    def _fake_records(cfg=None):
        calls["n"] += 1
        if calls["n"] == 1:
            return []
        return [{"id": "IOE1:2026-03-10", "kind": "event"}]

    monkeypatch.setattr(server, "_all_update_records", _fake_records)
    with patch.object(subprocess, "run", return_value=_fake_proc(0, "", "")), \
         patch.object(server, "_send_notifications_for_new") as send:
        server._run_pull_subprocess()
    send.assert_called_once()


def test_run_pull_subprocess_failure_sets_error(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": []}))
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
    cfg_path.write_text(json.dumps({"cases": []}))
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
    cfg_path.write_text(json.dumps({"cases": []}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())

    with patch.object(subprocess, "run", side_effect=RuntimeError("oops")):
        server._run_pull_subprocess()
    assert server._pull_state.ok is False


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
    assert case["summary"]["stage"] == "Pending receipt"


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
    (data_dir / "485_logs.json").write_text(json.dumps(entries))

    r = client.get("/api/cases/I-485/history")
    body = r.data.decode()
    # Original insertion order must be preserved in the wire bytes.
    assert body.index("zeta_first") < body.index("alpha_later")
    assert body.index("alpha_later") < body.index("middle_third")


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
    assert rec["id"] == "IOE1:2026-03-10"


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
        assert "485_logs.json" in names
        assert "manifest.json" in names
        manifest = json.loads(z.read("manifest.json"))
        assert manifest["cases"][0]["label"] == "I-485"
        assert manifest["cases"][0]["receiptNumber"] == "IOE1"
        assert manifest["cases"][0]["file"] == "485_logs.json"
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
    (data_dir / "485_logs.json").write_text("{not valid json")
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


def test_api_test_email_requires_auth(client, monkeypatch, tmp_path):
    # Overwrite config with blank auth
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"auth": {}, "cases": []}))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    r = client.post("/api/test-email")
    assert r.status_code == 400


def test_api_test_email_uses_synthetic_record_when_no_data(client, monkeypatch):
    sent = {}

    def _notify(auth, to, rec, labels):
        sent["rec"] = rec

    monkeypatch.setattr(server, "notify_update", _notify)
    r = client.post("/api/test-email")
    body = r.get_json()
    assert body["ok"] is True
    assert sent["rec"]["id"] == "TEST:sample"


def test_api_test_email_uses_latest_real_record_when_available(client, tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    _seed_log(data_dir, [
        _entry("2026-03-09T00:00:00Z"),
        _entry("2026-03-10T00:00:00Z", closed=True),
    ])
    sent = {}
    monkeypatch.setattr(server, "notify_update",
                        lambda auth, to, rec, labels: sent.update({"rec": rec}))
    r = client.post("/api/test-email")
    assert r.status_code == 200
    assert sent["rec"]["id"] == "IOE1:2026-03-10"


def test_api_test_email_surfaces_failure(client, monkeypatch):
    monkeypatch.setattr(server, "notify_update",
                        MagicMock(side_effect=RuntimeError("smtp down with user@example.com")))
    r = client.post("/api/test-email")
    assert r.status_code == 500
    # Error is deliberately generic — must NOT leak the exception text,
    # which routinely contains the configured email address or auth details.
    body = r.get_json()
    assert body["ok"] is False
    assert body["error"] == "send_failed"
    assert "smtp down" not in str(body)
    assert "user@example.com" not in str(body)


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
