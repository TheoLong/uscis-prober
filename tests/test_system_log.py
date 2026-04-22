# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
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

def _stub_server_config(monkeypatch, tmp_path, cases=None, auth=None):
    import server
    data_dir = tmp_path / "data"; data_dir.mkdir(exist_ok=True)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({
        "cases": cases if cases is not None else [],
        "auth": auth or {},
    }))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())
    return server


# After the v2 refactor, one pull produces exactly one `pull` entry in the
# system log, with all its internal steps attached as `steps[]`. The tests
# below check both the top-level envelope (event, level, summary, trigger,
# duration, exit_code) and the step stream inside.

def test_run_pull_subprocess_emits_one_consolidated_entry_on_success(
    monkeypatch, tmp_path,
):
    import subprocess
    server = _stub_server_config(monkeypatch, tmp_path)
    from unittest.mock import MagicMock, patch
    proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch.object(subprocess, "run", return_value=proc), \
         patch.object(server, "_send_notifications_for_new", return_value=[]):
        server._run_pull_subprocess(trigger="manual")

    entries = system_log.read_all()
    pull_entries = [e for e in entries if e["event"] == "pull"]
    assert len(pull_entries) == 1, f"expected exactly 1 `pull` row, got {len(pull_entries)}"
    pull = pull_entries[0]
    assert pull["level"] == "info"
    assert pull["trigger"] == "manual"
    assert pull["exit_code"] == 0
    assert pull["timed_out"] is False
    assert "steps" in pull and isinstance(pull["steps"], list)
    assert "summary" in pull and isinstance(pull["summary"], dict)
    # Legacy events must NOT appear any more — the consolidated entry is it.
    for legacy in ("pull_started", "pull_finished", "pull_triggered_manually",
                   "pull_failed", "pull_timeout", "pull_crashed"):
        assert legacy not in {e["event"] for e in entries}, \
            f"legacy event {legacy!r} still being emitted"


def test_run_pull_subprocess_nonzero_exit_flips_level_to_error(monkeypatch, tmp_path):
    import subprocess
    server = _stub_server_config(monkeypatch, tmp_path)
    from unittest.mock import MagicMock, patch
    proc = MagicMock(returncode=1, stdout="", stderr="Missing auth keys\nother log line")
    with patch.object(subprocess, "run", return_value=proc):
        server._run_pull_subprocess(trigger="scheduled")

    pull = [e for e in system_log.read_all() if e["event"] == "pull"][0]
    assert pull["level"] == "error"
    assert pull["exit_code"] == 1
    assert pull["trigger"] == "scheduled"
    # Non-zero exit appends a `subprocess_exit_nonzero` step with stderr tail.
    subp_step = [s for s in pull["steps"] if s["event"] == "subprocess_exit_nonzero"]
    assert len(subp_step) == 1
    assert subp_step[0]["exit_code"] == 1
    assert "Missing auth keys" in subp_step[0]["stderr_tail"][0]


def test_run_pull_subprocess_timeout_embeds_step(monkeypatch, tmp_path):
    import subprocess
    server = _stub_server_config(monkeypatch, tmp_path)
    from unittest.mock import patch
    with patch.object(
        subprocess, "run",
        side_effect=subprocess.TimeoutExpired(cmd="x", timeout=1),
    ):
        server._run_pull_subprocess()

    pull = [e for e in system_log.read_all() if e["event"] == "pull"][0]
    assert pull["level"] == "error"
    assert pull["timed_out"] is True
    assert any(s["event"] == "subprocess_timeout" for s in pull["steps"])


def test_run_pull_subprocess_crash_embeds_step(monkeypatch, tmp_path):
    import subprocess
    server = _stub_server_config(monkeypatch, tmp_path)
    from unittest.mock import patch
    with patch.object(subprocess, "run", side_effect=RuntimeError("boom")):
        server._run_pull_subprocess()

    pull = [e for e in system_log.read_all() if e["event"] == "pull"][0]
    assert pull["level"] == "error"
    crash_steps = [s for s in pull["steps"] if s["event"] == "subprocess_crashed"]
    assert len(crash_steps) == 1
    assert crash_steps[0]["error"] == "boom"


def test_run_pull_subprocess_logs_skip_when_already_running(monkeypatch, tmp_path):
    import server
    monkeypatch.setattr(server, "_pull_state", server.PullState(running=True))
    server._run_pull_subprocess(trigger="manual")
    # Skip is still a standalone flat event — no subprocess ran, no steps
    # to aggregate. This row is deliberately kept tiny for that reason.
    events = [e for e in system_log.read_all()]
    skip_rows = [e for e in events if e["event"] == "pull_skipped_already_running"]
    assert len(skip_rows) == 1
    assert skip_rows[0]["trigger"] == "manual"
    # No `pull` envelope row for a skipped trigger.
    assert not any(e["event"] == "pull" for e in events)


def test_subprocess_steps_parsed_from_jsonl_stderr(monkeypatch, tmp_path):
    # Child processes emit events as prefixed JSONL lines on stderr; the
    # parent parses those into `steps[]`. This test drives that path end-
    # to-end with a synthetic stderr stream — no real Playwright launch.
    import subprocess
    server = _stub_server_config(monkeypatch, tmp_path)
    synthetic_stderr = "\n".join([
        "2026-04-22 22:25:01 INFO session_fetch: Fetching I-485 ...",   # python logging noise
        f"{system_log._JSONL_PREFIX}"
        + json.dumps({"ts": "2026-04-22T22:25:01Z", "event": "case_fetch_start",
                      "level": "info", "source": "session_fetch",
                      "label": "I-485", "receipt": "IOE1"}),
        f"{system_log._JSONL_PREFIX}"
        + json.dumps({"ts": "2026-04-22T22:25:02Z", "event": "case_snapshot_appended",
                      "level": "info", "source": "session_fetch",
                      "file": "485_case.json"}),
    ])
    from unittest.mock import MagicMock, patch
    proc = MagicMock(returncode=0, stdout="", stderr=synthetic_stderr)
    with patch.object(subprocess, "run", return_value=proc), \
         patch.object(server, "_send_notifications_for_new", return_value=[]):
        server._run_pull_subprocess()

    pull = [e for e in system_log.read_all() if e["event"] == "pull"][0]
    step_events = [s["event"] for s in pull["steps"]]
    assert "case_fetch_start" in step_events
    assert "case_snapshot_appended" in step_events
    # Summary derived from counting events in the step stream.
    assert pull["summary"]["case_snapshots"] == 1
    # Python-logging noise is NOT silently added as a step; only structured
    # JSONL lines become steps.
    assert all(s["event"] != "Fetching" for s in pull["steps"])


def test_send_notifications_returns_skip_step_when_auth_missing(monkeypatch, tmp_path):
    import server
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"auth": {}}))
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    # The function now RETURNS step dicts instead of writing to system_log.
    steps = server._send_notifications_for_new([{"id": "1", "kind": "event"}])
    assert len(steps) == 1
    assert steps[0]["event"] == "notify_skipped"
    assert steps[0]["reason"] == "auth_missing"
    assert steps[0]["count"] == 1
    # Nothing written to the log as a side effect.
    assert system_log.read_all() == []


def test_send_notifications_returns_sent_step_per_record(monkeypatch, tmp_path):
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
        steps = server._send_notifications_for_new([
            {"id": "IOE1:case:2026-03-10", "kind": "event"},
            {"id": "IOE2:case:2026-03-10", "kind": "silent_update"},
        ])
    assert {s["event"] for s in steps} == {"notify_sent"}
    assert {s["record_id"] for s in steps} == {
        "IOE1:case:2026-03-10", "IOE2:case:2026-03-10",
    }


def test_send_notifications_returns_failure_step_per_error(monkeypatch, tmp_path):
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
        steps = server._send_notifications_for_new([{"id": "1", "kind": "event"}])
    failed = [s for s in steps if s["event"] == "notify_failed"]
    assert len(failed) == 1
    assert failed[0]["level"] == "error"
    assert failed[0]["record_id"] == "1"


def test_jsonl_stderr_mode_writes_to_stderr_not_file(
    monkeypatch, tmp_path, capsys,
):
    # When USCIS_LOG_JSONL_STDERR=1, log() must write nothing to LOG_PATH
    # and instead emit one prefixed JSON line to stderr.
    monkeypatch.setenv("USCIS_LOG_JSONL_STDERR", "1")
    # Point LOG_PATH somewhere the test owns so we can assert nothing landed.
    monkeypatch.setattr(system_log, "LOG_PATH", tmp_path / "should_stay_empty.json")

    system_log.log("case_fetch_start", source="session_fetch", receipt="IOE1")

    assert not (tmp_path / "should_stay_empty.json").exists(), \
        "JSONL mode must not touch the on-disk log"
    captured = capsys.readouterr().err
    lines = [ln for ln in captured.splitlines() if ln.startswith(system_log._JSONL_PREFIX)]
    assert len(lines) == 1
    parsed = system_log.parse_jsonl_stderr_line(lines[0])
    assert parsed is not None
    assert parsed["event"] == "case_fetch_start"
    assert parsed["source"] == "session_fetch"
    assert parsed["receipt"] == "IOE1"


def test_parse_jsonl_stderr_line_ignores_plain_text():
    assert system_log.parse_jsonl_stderr_line("2026-04-22 INFO some.logger: hi") is None
    assert system_log.parse_jsonl_stderr_line("") is None
    # Well-formed prefix but malformed JSON also returns None.
    bad = system_log._JSONL_PREFIX + "{not json"
    assert system_log.parse_jsonl_stderr_line(bad) is None


# ======================================================================
# Error-handling coverage — every failure path must sys_log
# ======================================================================

def test_route_unhandled_exception_emits_sys_log(monkeypatch, tmp_path):
    import server
    _stub_server_config(monkeypatch, tmp_path)

    # Force any route to raise by monkeypatching a dependency it uses.
    def _kaboom(*a, **k):
        raise RuntimeError("surprise from the data layer")
    monkeypatch.setattr(server, "load_config", _kaboom)

    with server.app.test_client() as c:
        r = c.get("/api/cases")
    assert r.status_code == 500
    body = r.get_json()
    # The client gets an opaque body — no traceback leaks.
    assert body == {"ok": False, "error": "internal_error"}

    events = [e for e in system_log.read_all()
              if e["event"] == "route_unhandled_exception"]
    assert len(events) == 1
    ev = events[0]
    assert ev["path"] == "/api/cases"
    assert ev["method"] == "GET"
    assert ev["level"] == "error"
    assert "RuntimeError" in ev["error"]
    assert "surprise from the data layer" in ev["error"]
    # Traceback tail is present and bounded.
    assert "traceback_tail" in ev and len(ev["traceback_tail"]) <= 1200


def test_pull_pre_snapshot_failure_emits_sys_log(monkeypatch, tmp_path):
    # If the before-pull snapshot read itself crashes (e.g. a case file
    # with a shape that slips past load_case_entries' gracefulness), we
    # should emit `pull_pre_snapshot_failed` and KEEP GOING rather than
    # abort the whole pull.
    import subprocess
    server = _stub_server_config(monkeypatch, tmp_path)

    monkeypatch.setattr(
        server, "_all_update_records",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom in pre-snapshot")),
    )

    from unittest.mock import MagicMock, patch
    proc = MagicMock(returncode=0, stdout="", stderr="")
    with patch.object(subprocess, "run", return_value=proc), \
         patch.object(server, "_send_notifications_for_new", return_value=[]):
        # Note: the notifications call inside _run_pull_subprocess also
        # uses _all_update_records; it'll crash, but that's caught by
        # the `if proc.returncode == 0` branch — which is wrapped in the
        # function's own try/except via the pull envelope. Skip
        # notifications entirely by patching.
        pass
    # Re-patch _all_update_records only for the pre-snapshot call — keep
    # the post-pull call working so we can assert the pre-snapshot
    # event appears alongside a normal pull entry.
    calls = {"n": 0}
    def _partial_fail(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom in pre-snapshot")
        return []
    monkeypatch.setattr(server, "_all_update_records", _partial_fail)

    with patch.object(subprocess, "run", return_value=proc):
        server._run_pull_subprocess(trigger="manual")

    entries = system_log.read_all()
    pre_fail = [e for e in entries if e["event"] == "pull_pre_snapshot_failed"]
    assert len(pre_fail) == 1
    assert "boom in pre-snapshot" in pre_fail[0]["error"]
    # The pull still completed and wrote its consolidated entry.
    pulls = [e for e in entries if e["event"] == "pull"]
    assert len(pulls) == 1


def test_pull_thread_crash_emits_sys_log(monkeypatch, tmp_path):
    import server
    _stub_server_config(monkeypatch, tmp_path)

    # Force _run_pull_subprocess to raise so the outer _runner try/except
    # in _spawn_pull_async is exercised — the only defender against a
    # truly unexpected crash in the pull thread.
    def _boom(trigger="scheduled"):
        raise RuntimeError("runner exploded")
    monkeypatch.setattr(server, "_run_pull_subprocess", _boom)

    server._spawn_pull_async(trigger="manual")
    # The daemon thread runs async; give it a beat to finish and write
    # its sys_log entry. The call is cheap (our patched runner raises
    # immediately) so 1s is plenty of headroom.
    import time as _time
    for _ in range(20):
        crashes = [e for e in system_log.read_all()
                   if e["event"] == "pull_thread_crashed"]
        if crashes:
            break
        _time.sleep(0.05)

    assert len(crashes) == 1
    assert "runner exploded" in crashes[0]["error"]
    assert crashes[0]["trigger"] == "manual"
    # pull_state should have been cleared so the dashboard doesn't think
    # a pull is still in flight.
    assert server._pull_state.running is False
    assert server._pull_state.ok is False


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
        body = r.get_json()
        events = body["events"]
        assert len(events) == 5
        # The newest 5 entries (indexes 15..19), returned oldest-first.
        assert [e["i"] for e in events] == [15, 16, 17, 18, 19]
        # `total` reflects the true count on disk regardless of limit.
        assert body["total"] == 20
        assert body["limit"] == 5
        assert body["offset"] == 0


def test_api_system_log_paginates_with_offset(monkeypatch, tmp_path):
    import server
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "auth": {}}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    for i in range(888):
        system_log.log("tick", i=i)
    with server.app.test_client() as c:
        # Page 1 (newest 100): indexes 788..887.
        body = c.get("/api/system-log?limit=100&offset=0").get_json()
        assert body["total"] == 888
        assert [e["i"] for e in body["events"]] == list(range(788, 888))

        # Page 2: indexes 688..787.
        body = c.get("/api/system-log?limit=100&offset=100").get_json()
        assert [e["i"] for e in body["events"]] == list(range(688, 788))

        # Last (partial) page, page 9 with offset=800: indexes 0..87 —
        # 888 total, per_page=100 → 9 pages, last has 88 entries.
        body = c.get("/api/system-log?limit=100&offset=800").get_json()
        assert [e["i"] for e in body["events"]] == list(range(0, 88))
        assert len(body["events"]) == 88


def test_api_system_log_offset_past_end_returns_empty_slice(monkeypatch, tmp_path):
    import server
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "auth": {}}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    for _ in range(10):
        system_log.log("tick")
    with server.app.test_client() as c:
        body = c.get("/api/system-log?limit=100&offset=9999").get_json()
        # No crash; total still correct; empty slice.
        assert body["total"] == 10
        assert body["events"] == []


def test_api_system_log_clamps_limit_to_500(monkeypatch, tmp_path):
    import server
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "auth": {}}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    for i in range(700):
        system_log.log("tick", i=i)
    with server.app.test_client() as c:
        body = c.get("/api/system-log?limit=99999").get_json()
        # Clamped to MAX_SYSLOG_PAGE_SIZE (500) so a huge `limit` can't DoS
        # the dashboard by forcing an unbounded JSON serialisation.
        assert body["limit"] == 500
        assert len(body["events"]) == 500


def test_api_system_log_default_limit_is_page_size(monkeypatch, tmp_path):
    import server
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "auth": {}}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    for _ in range(250):
        system_log.log("tick")
    with server.app.test_client() as c:
        body = c.get("/api/system-log").get_json()
        # No limit param → default page size = 100.
        assert body["limit"] == server.DEFAULT_SYSLOG_PAGE_SIZE == 100
        assert len(body["events"]) == 100
        assert body["total"] == 250


def test_api_system_log_negative_offset_clamped_to_zero(monkeypatch, tmp_path):
    import server
    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "auth": {}}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    for i in range(5):
        system_log.log("tick", i=i)
    with server.app.test_client() as c:
        body = c.get("/api/system-log?offset=-9").get_json()
        assert body["offset"] == 0
        assert len(body["events"]) == 5


def test_system_log_count_matches_read_all(monkeypatch, tmp_path):
    # `count()` must agree with `len(read_all())` — it's the cheap variant,
    # not a different counter.
    monkeypatch.setattr(system_log, "LOG_PATH", tmp_path / "system_log.json")
    assert system_log.count() == 0
    for i in range(7):
        system_log.log("tick", i=i)
    assert system_log.count() == 7
    assert system_log.count() == len(system_log.read_all())


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


# -------- clear endpoint ---------------------------------------------

def test_api_system_log_clear_wipes_and_audits(monkeypatch, tmp_path):
    import server

    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "auth": {}}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)

    for i in range(3):
        system_log.log("tick", i=i)
    assert len(system_log.read_all()) == 3

    with server.app.test_client() as c:
        r = c.post(
            "/api/system-log/clear",
            data=json.dumps({"confirm": True}),
            content_type="application/json",
        )
        assert r.status_code == 200
        body = r.get_json()
        assert body["ok"] is True
        assert body["priorEntryCount"] == 3

    # After wipe, only the audit breadcrumb remains (the clear records itself).
    entries = system_log.read_all()
    assert len(entries) == 1
    assert entries[0]["event"] == "system_log_cleared"
    assert entries[0]["prior_entry_count"] == 3
    assert entries[0]["source"] == "server"


def test_api_system_log_clear_rejects_without_confirm(monkeypatch, tmp_path):
    import server

    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "auth": {}}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)

    system_log.log("tick")
    before = system_log.read_all()

    with server.app.test_client() as c:
        # no body
        r = c.post("/api/system-log/clear")
        assert r.status_code == 400
        assert r.get_json()["error"] == "confirmation_required"

        # body present but confirm not literally `true`
        r = c.post(
            "/api/system-log/clear",
            data=json.dumps({"confirm": "yes"}),
            content_type="application/json",
        )
        assert r.status_code == 400

        # Empty JSON object
        r = c.post(
            "/api/system-log/clear",
            data=json.dumps({}),
            content_type="application/json",
        )
        assert r.status_code == 400

    # Log is untouched across all three rejected calls.
    assert system_log.read_all() == before


def test_api_system_log_clear_on_empty_log_is_noop_plus_audit(monkeypatch, tmp_path):
    import server

    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "auth": {}}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)

    # Log starts empty (autouse fixture cleared it).
    assert system_log.read_all() == []

    with server.app.test_client() as c:
        r = c.post(
            "/api/system-log/clear",
            data=json.dumps({"confirm": True}),
            content_type="application/json",
        )
        assert r.status_code == 200
        assert r.get_json()["priorEntryCount"] == 0

    entries = system_log.read_all()
    assert len(entries) == 1
    assert entries[0]["event"] == "system_log_cleared"
    assert entries[0]["prior_entry_count"] == 0


# -------- system log is NOT included in the cases export ------------

def test_api_export_excludes_system_log(monkeypatch, tmp_path):
    # The combined /api/export zip is for cases only. The system log has
    # its own dedicated endpoint so operators can share case archives
    # without leaking diagnostic metadata (email addresses, scheduler
    # fires, etc.).
    import io, zipfile
    import server

    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "auth": {}}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)

    system_log.log("server_startup")
    system_log.log("pull_finished", exit_code=0)

    with server.app.test_client() as c:
        r = c.get("/api/export")
        assert r.status_code == 200
        with zipfile.ZipFile(io.BytesIO(r.data)) as z:
            names = set(z.namelist())
            assert "system_log.json" not in names
            assert "manifest.json" in names


# -------- dedicated /api/system-log/export ---------------------------

def test_api_system_log_export_returns_json_attachment(monkeypatch, tmp_path):
    import server

    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "auth": {}}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)

    system_log.log("server_startup", hours=[7, 14, 20])
    system_log.log("pull_finished", exit_code=0)

    with server.app.test_client() as c:
        r = c.get("/api/system-log/export")
        assert r.status_code == 200
        assert r.mimetype == "application/json"
        disp = r.headers.get("Content-Disposition", "")
        assert disp.startswith("attachment;")
        assert "uscis-system-log-" in disp
        assert disp.endswith('.json"')

        body = json.loads(r.data)
        assert isinstance(body, list)
        event_names = [e["event"] for e in body]
        assert "server_startup" in event_names
        assert "pull_finished" in event_names


def test_api_system_log_export_on_empty_log_is_empty_array(monkeypatch, tmp_path):
    import server

    data_dir = tmp_path / "data"; data_dir.mkdir()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"cases": [], "auth": {}}))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)

    # Autouse fixture already ensured log is empty.
    with server.app.test_client() as c:
        r = c.get("/api/system-log/export")
        assert r.status_code == 200
        assert json.loads(r.data) == []
