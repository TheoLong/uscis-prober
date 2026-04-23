#!/usr/bin/env python3
# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""USCIS Prober — long-running local web dashboard.

Launches a Flask app on http://localhost:8080 that:
  - Reads case snapshots from `data/*_case.json`
  - Exposes REST endpoints the UI uses to render visualisations & diffs
  - Runs `session_fetch.py run` in a subprocess on demand (button) and on
    a cron schedule (07:00, 14:00, and 20:00 America/New_York daily)
  - Surfaces the next scheduled run + last run status for the UI countdown

Playwright is kept strictly out of the Flask process by running the pull
in a subprocess.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import zipfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, Response, jsonify, request, send_from_directory

from access_gate import configure as configure_access_gate
from diff_utils import (
    EVENT_CODE_LABELS,
    bin_by_day,
    day_changes,
    day_of,
    location_day_changes,
    summarize_case,
)
from mailer import notify_update
from system_log import (
    JSONL_STDERR_ENV as _SYSLOG_JSONL_ENV,
    log as sys_log,
    read_all as read_system_log,
    count as count_system_log,
    clear as clear_system_log,
    parse_jsonl_stderr_line as _parse_syslog_jsonl_line,
    push_capture as _syslog_push_capture,
    pop_capture as _syslog_pop_capture,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = ROOT / "config.json"
STATIC_DIR = Path(__file__).resolve().parent / "static"

SCHEDULER_TZ = "America/New_York"
# Cron hours for the automatic pull (24h, America/New_York).
# Chosen after analysing observed updatedAtTimestamp values:
#   - 07:00 catches overnight batches (e.g. 00:30 silent updates)
#   - 14:00 catches the 10:00–13:14 Eastern Time daytime activity cluster
#   - 20:00 insurance slot for any post-2pm updates
PULL_HOURS: tuple[int, ...] = (7, 14, 20)

PULL_CMD = [sys.executable, str(Path(__file__).resolve().parent / "session_fetch.py"), "run"]

_FORM_NUM_RE = re.compile(r"I-?(\d+)")

logger = logging.getLogger("server")


@dataclass
class PullState:
    """State of the most recent / currently running pull."""

    running: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    ok: bool | None = None
    exit_code: int | None = None
    last_error: str | None = None
    log_tail: list[str] = field(default_factory=list)


_pull_state = PullState()
_pull_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


def _case_log_file_for(form_label: str) -> Path | None:
    """Resolve the case-API snapshot-log path for a form label.

    The regex already constrains the filename to digits only, but we
    verify path containment explicitly so any future relaxation of the
    pattern can't silently enable path traversal.
    """
    return _log_path_for(form_label, suffix="_case.json")


def _location_log_file_for(form_label: str) -> Path | None:
    """Resolve the location-API snapshot-log path for a form label."""
    return _log_path_for(form_label, suffix="_location.json")


def _log_path_for(form_label: str, *, suffix: str) -> Path | None:
    m = _FORM_NUM_RE.search(form_label or "")
    if not m:
        return None
    candidate = (DATA_DIR / f"{m.group(1)}{suffix}").resolve()
    if candidate.parent != DATA_DIR.resolve():  # pragma: no cover — regex forbids traversal today
        return None
    return candidate


def _load_json_list(path: Path | None) -> list[dict]:
    if not path or not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON; treating as empty.", path.name)
        return []


def load_case_entries(form_label: str) -> list[dict]:
    """Case-API snapshot history for a form label (oldest → newest)."""
    return _load_json_list(_case_log_file_for(form_label))


def load_location_entries(form_label: str) -> list[dict]:
    """Location-API snapshot history for a form label (oldest → newest)."""
    return _load_json_list(_location_log_file_for(form_label))


def _latest_location_payload(entries: list[dict]) -> dict | None:
    """Return the most recent location API response body, or None.

    Response envelope is `{"data": null}` or `{"data": {...}}`. We return
    the full envelope (not the unwrapped `.data`) so the UI can
    distinguish "USCIS returned null" from "we never fetched".
    """
    if not entries:
        return None
    return entries[-1].get("data")


def _latest_location_info(entries: list[dict]) -> dict | None:
    """Return the populated `receipt_details` object from the most recent
    location snapshot, or None when USCIS is still returning null.

    Live envelope shape from `/receipt_info/{receipt}`:
      - unassigned: `{"data": null}`
      - assigned:   `{"data": {"receipt_details": {form, location, subtype,
                    receipt_date, ...}, "message": "..."}}`
    """
    payload = _latest_location_payload(entries)
    if not isinstance(payload, dict):
        return None
    inner = payload.get("data")
    if not isinstance(inner, dict) or not inner:
        return None
    details = inner.get("receipt_details")
    if isinstance(details, dict) and details:
        return details
    # Fallback: some snapshots may already be unwrapped. Return `inner`
    # as-is if it looks like a details record (has at least one of the
    # known fields). This keeps older test fixtures working.
    if any(k in inner for k in ("form", "location", "subtype")):
        return inner
    return None


# ---------------------------------------------------------------------------
# Pull runner (subprocess)
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


# ---------------------------------------------------------------------------
# Diff-set snapshotting (for email notifications)
# ---------------------------------------------------------------------------

def _all_update_records(config: dict | None = None) -> list[dict]:
    """Every diff across every configured case, same shape as /api/updates.

    Includes both case-API diffs and location-API diffs (tagged via
    `source`). IDs embed the source so case and location diffs detected
    on the same day produce distinct records — critical for the
    "new since last pull" email-notification snapshot set.
    """
    config = config or load_config()
    records: list[dict] = []
    for c in config.get("cases", []):
        label = c["label"]
        receipt = c["id"]
        merged = _merge_changes(
            day_changes(load_case_entries(label)),
            location_day_changes(load_location_entries(label)),
        )
        for change in merged:
            detected_on = (change.get("to") or "")[:10]
            source = change.get("source") or "case"
            real_update_date = (change.get("scalars") or {}).get("updatedAt", {}).get("to")
            rec = dict(change)
            rec.update({
                "id": f"{receipt}:{source}:{detected_on}",
                "caseLabel": label,
                "receiptNumber": receipt,
                "detectedOn": detected_on,
                "realUpdateDate": real_update_date,
            })
            records.append(rec)
    return records


def _update_ids(records: list[dict]) -> set[str]:
    return {r["id"] for r in records if r.get("id")}


def _notify_recipient(auth: dict) -> str | None:
    """Who to email. Prefer an explicit `auth.notify_email`, else the verification mailbox."""
    return auth.get("notification_email") or auth.get("uscis_mfa_email")


def _send_notifications_for_new(new_records: list[dict]) -> list[dict]:
    """Send a notification email for each new diff record.

    Returns a list of step-dicts (same shape as other system-log steps)
    describing what happened — `notify_sent`, `notify_failed`,
    `notify_skipped`, or `notify_dispatcher_crashed`. The caller folds
    these into the consolidated `pull` system-log entry.

    The step list replaces the old "emit each event individually" pattern:
    a pull that sends 3 emails used to produce 3 flat rows in the system
    log; now it produces 3 `steps` inside a single `pull` row.
    """
    steps: list[dict] = []
    if not new_records:
        return steps
    try:
        config = load_config()
        auth = (config.get("auth") or {})
        recipient = _notify_recipient(auth)
        if not recipient or not auth.get("uscis_mfa_email") or not auth.get("uscis_mfa_app_password"):
            logger.warning("Email auth missing — skipping %d notification(s).", len(new_records))
            steps.append({
                "ts": _now_iso(), "event": "notify_skipped",
                "level": "warning", "source": "server",
                "reason": "auth_missing", "count": len(new_records),
            })
            return steps
        for rec in new_records:
            try:
                notify_update(auth, recipient, rec, EVENT_CODE_LABELS)
                logger.info("Notified: %s (%s)", rec.get("id"), rec.get("kind"))
                steps.append({
                    "ts": _now_iso(), "event": "notify_sent",
                    "level": "info", "source": "server",
                    "record_id": rec.get("id"), "kind": rec.get("kind"),
                })
            except Exception as e:
                logger.exception("Notify failed for %s: %s", rec.get("id"), e)
                steps.append({
                    "ts": _now_iso(), "event": "notify_failed",
                    "level": "error", "source": "server",
                    "record_id": rec.get("id"), "error": str(e),
                })
    except Exception as e:
        logger.exception("Notification dispatcher crashed: %s", e)
        steps.append({
            "ts": _now_iso(), "event": "notify_dispatcher_crashed",
            "level": "error", "source": "server", "error": str(e),
        })
    return steps


def _worst_level(steps: list[dict]) -> str:
    """Return the most severe level across `steps` — error > warning > info."""
    levels = {s.get("level", "info") for s in steps}
    if "error" in levels:
        return "error"
    if "warning" in levels or "warn" in levels:
        return "warning"
    return "info"


def _pull_summary_from_steps(steps: list[dict]) -> dict:
    """Derive a compact summary from the step stream for the UI header.

    The summary is what makes the single consolidated entry useful at a
    glance: cases fetched, failures, new diffs, emails sent. The full
    detail is still there in `steps` for anyone expanding the row.
    """
    def _count(event_name: str) -> int:
        return sum(1 for s in steps if s.get("event") == event_name)

    return {
        "case_snapshots": _count("case_snapshot_appended"),
        "location_snapshots": _count("location_snapshot_appended"),
        "case_fetch_failures": _count("case_fetch_api_error"),
        "location_fetch_failures": _count("location_fetch_failed"),
        "new_diffs_emailed": _count("notify_sent"),
        "notify_failures": _count("notify_failed"),
        "session_expired_retries": _count("cli_run_session_expired_retry"),
    }


def _collect_subprocess_steps(stderr_text: str) -> tuple[list[dict], list[str]]:
    """Split a subprocess's stderr into (structured events, non-event tail).

    The child emits one line per `system_log.log()` call, prefixed with
    the JSONL sentinel. Any line missing the sentinel is Python-logging
    text — we keep the last few for the crash-diagnosis step shown if
    the subprocess exits non-zero.
    """
    events: list[dict] = []
    plain: list[str] = []
    for line in stderr_text.splitlines():
        parsed = _parse_syslog_jsonl_line(line)
        if parsed is not None:
            events.append(parsed)
        else:
            plain.append(line)
    return events, plain


def _run_pull_subprocess(trigger: str = "scheduled") -> None:
    """Invoke session_fetch.py run and write ONE consolidated system-log entry.

    The child runs with `USCIS_LOG_JSONL_STDERR=1`, which makes every
    internal `system_log.log(...)` call emit a JSON line to stderr
    instead of appending to disk. We collect those lines, append
    notification events, and write a single `pull` entry with the full
    stream as `steps` so the dashboard can render it as one expandable
    row rather than 15+ flat rows.

    `trigger` is either "manual" (from POST /api/pull) or "scheduled"
    (from APScheduler). Recorded on the entry for later filtering.
    """
    global _pull_state
    with _pull_lock:
        if _pull_state.running:
            logger.info("Pull already running; skipping trigger.")
            sys_log("pull_skipped_already_running", source="server",
                    trigger=trigger)
            return
        _pull_state = PullState(
            running=True,
            started_at=_now_iso(),
        )

    # Start thread-local capture so any sys_log() call fired from the
    # server process on this thread (smtp_* from mailer, snapshot_log_*
    # from load_*_entries, pull_pre_snapshot_failed, etc.) folds into
    # the pull envelope's steps[] instead of writing a separate flat row.
    # Other threads (Flask request handlers) are unaffected.
    #
    # Wrapped in a try/finally that guarantees pop_capture() even if an
    # uncaught exception escapes the function body — the outer
    # `_runner` in _spawn_pull_async still emits `pull_thread_crashed`,
    # but without the pop the next pull on this thread would silently
    # inherit the old buffer.
    thread_captured_steps = _syslog_push_capture()
    envelope: dict | None = None
    try:
        envelope = _run_pull_subprocess_inner(
            trigger=trigger, thread_captured_steps=thread_captured_steps,
        )
    finally:
        # Always pop — the inner function never pops. This keeps the
        # capture lifecycle 1:1 with this function call, so an unexpected
        # crash in the inner body can't poison the next pull on this
        # thread. If the inner crashed before producing an envelope,
        # `_spawn_pull_async` already emits `pull_thread_crashed`.
        _syslog_pop_capture()

    # Emit the consolidated envelope OUTSIDE the capture scope so this
    # final sys_log() reaches disk instead of being folded into its own
    # step buffer.
    if envelope is not None:
        sys_log(**envelope)


def _run_pull_subprocess_inner(
    *, trigger: str, thread_captured_steps: list[dict],
) -> dict:
    """The original pull-runner body, extracted so the outer function can
    own the capture push/pop in a proper try/finally. Splitting the two
    keeps the capture lifecycle visibly correct at the call site and
    avoids accidental `return` paths that bypass cleanup.

    Returns the kwargs for the final envelope `sys_log()` call — the
    outer function emits it OUTSIDE the capture scope so the envelope
    itself reaches disk instead of being folded into its own buffer.
    """
    global _pull_state

    start_iso = _pull_state.started_at
    start_wall = time.time()
    # Snapshot the diff set *before* pulling so we can identify records
    # that appear as a direct result of this run. This call can throw if
    # a case log file on disk is unreadable or malformed in a way
    # load_case_entries() doesn't already swallow — we keep the pull
    # going with an empty before-set so a bad file doesn't abort the
    # whole operation, but we emit a `pull_pre_snapshot_failed` so the
    # operator can see why notification dedup might be off for this run.
    try:
        before_ids = _update_ids(_all_update_records())
    except Exception as _e:  # noqa: BLE001 — truly catch-all by design
        before_ids = set()
        sys_log(
            "pull_pre_snapshot_failed",
            level="warning",
            source="server",
            trigger=trigger,
            error=f"{type(_e).__name__}: {_e}"[:300],
            traceback_tail="".join(
                traceback.format_exception(type(_e), _e, _e.__traceback__)
            )[-800:],
        )

    # Structured events collected from all sources during this run.
    steps: list[dict] = []
    plain_stderr_tail: list[str] = []
    exit_code: int | None = None
    top_level: str = "info"
    timed_out = False
    crashed_error: str | None = None

    logger.info("Spawning pull: %s", " ".join(PULL_CMD))
    child_env = {**os.environ, _SYSLOG_JSONL_ENV: "1"}
    try:
        proc = subprocess.run(
            PULL_CMD,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=600,
            env=child_env,
        )
        exit_code = proc.returncode
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        tail_lines = (stdout + "\n" + stderr).splitlines()[-80:]

        steps, plain_stderr_tail = _collect_subprocess_steps(stderr)
        duration = round(time.time() - start_wall, 2)

        with _pull_lock:
            _pull_state.running = False
            _pull_state.finished_at = _now_iso()
            _pull_state.exit_code = exit_code
            _pull_state.ok = exit_code == 0
            _pull_state.log_tail = tail_lines
            if exit_code != 0:
                _pull_state.last_error = f"exit={exit_code}"
        logger.info("Pull finished exit=%d", exit_code)

        # Only email on a successful pull. A failed pull may leave the
        # snapshot set half-updated; partial state isn't news.
        if exit_code == 0:
            after_records = _all_update_records()
            new_records = [
                r for r in after_records if r.get("id") not in before_ids
            ]
            if new_records:
                logger.info("Emitting %d notification(s).", len(new_records))
                steps.extend(_send_notifications_for_new(new_records))
            else:
                logger.info("No new diffs — no email sent.")
        else:
            # Capture the tail of raw stderr so the operator can see
            # what the subprocess printed right before it died, even if
            # no structured event covered it.
            steps.append({
                "ts": _now_iso(), "event": "subprocess_exit_nonzero",
                "level": "error", "source": "server",
                "exit_code": exit_code,
                "stderr_tail": plain_stderr_tail[-10:],
            })
    except subprocess.TimeoutExpired:
        timed_out = True
        duration = round(time.time() - start_wall, 2)
        with _pull_lock:
            _pull_state.running = False
            _pull_state.finished_at = _now_iso()
            _pull_state.exit_code = -1
            _pull_state.ok = False
            _pull_state.last_error = "timeout (10min)"
        logger.error("Pull timed out after 10min.")
        steps.append({
            "ts": _now_iso(), "event": "subprocess_timeout",
            "level": "error", "source": "server",
            "timeout_seconds": 600,
        })
    except Exception as e:
        crashed_error = str(e)
        duration = round(time.time() - start_wall, 2)
        with _pull_lock:
            _pull_state.running = False
            _pull_state.finished_at = _now_iso()
            _pull_state.ok = False
            _pull_state.last_error = str(e)
        logger.exception("Pull crashed.")
        steps.append({
            "ts": _now_iso(), "event": "subprocess_crashed",
            "level": "error", "source": "server",
            "error": str(e),
        })

    # Snapshot the thread-captured server-process events.
    captured_steps = list(thread_captured_steps)

    # Merge: subprocess steps (from the child's JSONL stderr) + server-
    # process events captured on this thread + any explicitly-appended
    # envelope steps (e.g. subprocess_exit_nonzero). Sort by timestamp
    # so the timeline is cohesive for the dashboard's expanded view.
    all_steps = list(steps) + captured_steps
    all_steps.sort(key=lambda s: s.get("ts", ""))

    # Derive the top-level severity and summary. The outer function
    # emits the envelope AFTER it pops the capture so this final event
    # reaches disk.
    top_level = _worst_level(all_steps)
    if exit_code not in (0, None) or timed_out or crashed_error is not None:
        top_level = "error"

    summary = _pull_summary_from_steps(all_steps)
    return {
        "event": "pull",
        "level": top_level,
        "source": "server",
        "trigger": trigger,
        "started_at": start_iso,
        "finished_at": _pull_state.finished_at,
        "duration_seconds": duration,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "summary": summary,
        "steps": all_steps,
    }


def _spawn_pull_async(trigger: str = "scheduled") -> None:
    """Fire `_run_pull_subprocess` on a daemon thread.

    Wraps the call in a last-resort catch so a crash inside the pull
    runner (or the logging of a crash inside the pull runner) produces a
    dashboard-visible `pull_thread_crashed` event instead of dying
    silently on the thread.
    """
    def _runner():
        try:
            _run_pull_subprocess(trigger)
        except Exception as e:  # noqa: BLE001 — last line of defence
            tb_tail = "".join(
                traceback.format_exception(type(e), e, e.__traceback__)
            )[-1200:]
            sys_log(
                "pull_thread_crashed",
                level="error",
                source="server",
                trigger=trigger,
                error=f"{type(e).__name__}: {e}"[:300],
                traceback_tail=tb_tail,
            )
            logger.exception("Pull thread crashed outside the usual wrapper.")
            # Clear the running flag so the dashboard doesn't think a
            # pull is still in flight.
            try:
                global _pull_state
                with _pull_lock:
                    _pull_state.running = False
                    _pull_state.ok = False
                    _pull_state.last_error = f"thread_crashed: {type(e).__name__}"
                    _pull_state.finished_at = _now_iso()
            except Exception:  # pragma: no cover
                logger.exception("Failed to clear pull_state after thread crash.")

    t = threading.Thread(target=_runner, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler(timezone=SCHEDULER_TZ)


def _on_scheduler_job_error(event) -> None:
    """Last-resort hook for any scheduler-fired callback that raises.

    `_spawn_pull_async` wraps `_run_pull_subprocess` in a daemon thread and
    the pull function has its own try/except — so this listener normally
    never fires. But if APScheduler itself fails to dispatch (e.g. jobstore
    crash, missed trigger bookkeeping), we still want a dashboard-visible
    event instead of a silent stderr traceback.
    """
    try:
        tb = getattr(event, "traceback", "") or ""
        sys_log(
            "scheduler_job_error",
            level="error",
            source="server",
            job_id=getattr(event, "job_id", None),
            scheduled_run_time=str(getattr(event, "scheduled_run_time", "")),
            error=str(getattr(event, "exception", "")),
            traceback_tail=tb[-1200:] if tb else "",
        )
    except Exception:  # pragma: no cover — belt-and-suspenders
        logger.exception("Scheduler error listener itself failed.")


def _setup_scheduler() -> None:
    for hour in PULL_HOURS:
        scheduler.add_job(
            _spawn_pull_async,
            CronTrigger(hour=hour, minute=0, timezone=SCHEDULER_TZ),
            id=f"pull-{hour:02d}",
            name=f"Daily pull @ {hour:02d}:00 {SCHEDULER_TZ}",
            replace_existing=True,
        )
    # EVENT_JOB_ERROR fires when the callback itself raises. Our pull
    # wrapper normally catches everything, but this is a safety net.
    from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED
    scheduler.add_listener(_on_scheduler_job_error, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    scheduler.start()
    sys_log("scheduler_configured", source="server",
            timezone=SCHEDULER_TZ, hours=list(PULL_HOURS))


def _next_run_iso() -> str | None:
    jobs = scheduler.get_jobs()
    times = [j.next_run_time for j in jobs if j.next_run_time]
    if not times:
        return None
    return min(times).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__, static_folder=str(STATIC_DIR), static_url_path="/static")
# Local dev: never let the browser cache HTML/CSS/JS. Prevents the "refresh
# doesn't show my change" class of bug.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

# Flask's `jsonify` alphabetises object keys by default — that scrambles
# USCIS's original field order when the Raw-JSON panel renders. Preserve
# insertion order so the panel shows exactly what USCIS returned.
# (The attribute name moved in Flask 2.2; set both for cross-version safety.)
app.config["JSON_SORT_KEYS"] = False
try:
    app.json.sort_keys = False
except AttributeError:
    pass


@app.after_request
def _no_cache(resp):
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.errorhandler(Exception)
def _catch_all_exception(exc):
    """Catch-all for any route that raises without its own try/except.

    Emits one `route_unhandled_exception` sys_log event with the route,
    method, status the browser will see, and the last ~1200 chars of
    the traceback. Then returns an opaque JSON body — we deliberately
    don't leak the traceback to the HTTP response because it may
    contain file paths or fragments of user data.

    Flask's default behaviour lets HTTPException subclasses (e.g.
    abort(403)) pass through; we only log true exceptions.
    """
    from werkzeug.exceptions import HTTPException
    if isinstance(exc, HTTPException):
        # Let Flask handle abort(...) / 404 / 405 etc. with its default
        # response. These are intentional, not crashes.
        return exc

    try:
        tb_tail = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__)
        )[-1200:]
    except Exception:  # pragma: no cover — traceback formatting shouldn't fail
        tb_tail = f"<traceback formatting failed: {type(exc).__name__}>"

    try:
        path = request.path
        method = request.method
    except Exception:  # pragma: no cover — no request context (shouldn't happen)
        path = "?"
        method = "?"

    sys_log(
        "route_unhandled_exception",
        level="error",
        source="server",
        path=path,
        method=method,
        error=f"{type(exc).__name__}: {exc}"[:300],
        traceback_tail=tb_tail,
    )
    logger.exception("Unhandled exception in %s %s", method, path)
    # Opaque response body — the structured event in the system log is
    # the operator-facing detail; the client just needs to know 500.
    return jsonify({"ok": False, "error": "internal_error"}), 500


def _static_version() -> str:
    """Max mtime of static assets, as an int string. Used to cache-bust."""
    paths = [STATIC_DIR / "style.css", STATIC_DIR / "app.js", STATIC_DIR / "index.html"]
    try:
        return str(int(max(p.stat().st_mtime for p in paths if p.exists())))
    except Exception:
        return str(int(time.time()))


@app.route("/")
def index():
    html_path = STATIC_DIR / "index.html"
    html = html_path.read_text()
    v = _static_version()
    # Append ?v=<mtime> to local static references so browsers always fetch
    # the version that's on disk right now.
    html = html.replace("/static/style.css", f"/static/style.css?v={v}")
    html = html.replace("/static/app.js", f"/static/app.js?v={v}")
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/api/cases")
def api_cases():
    """List configured cases with their latest snapshot and aggregate signals."""
    config = load_config()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    cases = []
    for c in config.get("cases", []):
        entries = load_case_entries(c["label"])
        days = bin_by_day(entries)
        latest = days[-1] if days else None
        latest_data = (latest or {}).get("data") or {}
        summary = summarize_case(entries, today_iso=today) if entries else {}

        location_entries = load_location_entries(c["label"])
        location_info = _latest_location_info(location_entries)
        last_location_entry = location_entries[-1] if location_entries else None

        cases.append(
            {
                "id": c["id"],
                "label": c["label"],
                "applicantName": latest_data.get("applicantName"),
                "formName": latest_data.get("formName"),
                "receiptNumber": latest_data.get("receiptNumber") or c["id"],
                "updatedAt": latest_data.get("updatedAt"),
                "closed": latest_data.get("closed"),
                "actionRequired": latest_data.get("actionRequired"),
                "capturedAt": (latest or {}).get("capturedAt"),
                "captures": len(entries),
                "days": len(days),
                "events": latest_data.get("events", []),
                "notices": latest_data.get("notices", []),
                "latest": latest_data,
                "summary": summary,
                "location": {
                    "info": location_info,            # populated inner data, or None
                    "captures": len(location_entries),
                    "capturedAt": (last_location_entry or {}).get("capturedAt"),
                },
            }
        )
    return jsonify({"cases": cases, "eventCodeLabels": EVENT_CODE_LABELS})


def _merge_changes(case_changes: list[dict], loc_changes: list[dict]) -> list[dict]:
    """Interleave case and location change records in chronological order.

    Both feeds already carry a `source` tag (`case` / `location`) so the
    UI can style them distinctively. Sort key is the `to` timestamp —
    the moment the diff was observed — newest last so callers that need
    newest-first can `reversed()` once.
    """
    merged = list(case_changes) + list(loc_changes)
    merged.sort(key=lambda c: c.get("to") or "")
    return merged


@app.route("/api/cases/<label>/history")
def api_case_history(label: str):
    entries = load_case_entries(label)
    days = bin_by_day(entries)
    location_entries = load_location_entries(label)
    changes = _merge_changes(day_changes(entries), location_day_changes(location_entries))
    return jsonify(
        {
            "label": label,
            "entries": entries,         # all raw case-API captures
            "days": days,               # one entry per UTC day (latest of day)
            "changes": changes,         # merged case + location diffs, chronological
            "locationEntries": location_entries,  # raw location-API captures
        }
    )


@app.route("/api/updates")
def api_updates():
    """Flat feed of every diff across every configured case, newest first.

    Each record is enriched with:
      - `id`            stable key `{receipt}:{source}:{toDate}` (dedup / email tracking)
      - `caseLabel`     e.g. "I-485"
      - `receiptNumber`
      - `source`        "case" | "location" (which endpoint produced the diff)
      - `detectedOn`    day we observed this diff (YYYY-MM-DD)
      - `realUpdateDate`   actual updatedAt date post-change (may differ from detectedOn)
    Plus the original diff body (scalars / events / notices / documents / kind).
    """
    records = _all_update_records()
    records.sort(key=lambda r: r.get("to") or "", reverse=True)
    return jsonify({"updates": records, "eventCodeLabels": EVENT_CODE_LABELS})


@app.route("/api/export")
def api_export():
    """Stream a zip containing every `data/*_case.json` plus a manifest.

    The manifest maps each file to its configured case label + receipt
    so the archive is self-describing. Produced in memory — the repo's
    snapshot history is tiny (a few MB at most).
    """
    config = load_config()
    now_iso = _now_iso()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        manifest = {"generatedAt": now_iso, "cases": []}
        for c in config.get("cases", []):
            label = c.get("label") or ""
            receipt = c.get("id") or ""
            path = _case_log_file_for(label)
            entries: list = []
            arcname: str | None = None
            if path:
                # Read the log once and reuse the bytes for both the
                # archive entry and the entry-count — eliminates both the
                # double-read and the TOCTOU gap between exists() and
                # write(). If the file disappears or is mid-write, fall
                # through to the "missing" manifest branch.
                try:
                    raw = path.read_bytes()
                    parsed = json.loads(raw) if raw else []
                    if isinstance(parsed, list):
                        entries = parsed
                        arcname = path.name
                        z.writestr(arcname, raw)
                except (OSError, json.JSONDecodeError):
                    entries = []
                    arcname = None
            # Location snapshot log (sibling file; may not exist yet)
            lpath = _location_log_file_for(label)
            location_entries: list = []
            location_arcname: str | None = None
            if lpath and lpath.exists():
                try:
                    raw = lpath.read_bytes()
                    parsed = json.loads(raw) if raw else []
                    if isinstance(parsed, list):
                        location_entries = parsed
                        location_arcname = lpath.name
                        z.writestr(location_arcname, raw)
                except (OSError, json.JSONDecodeError):
                    location_entries = []
                    location_arcname = None

            manifest["cases"].append({
                "label": label,
                "receiptNumber": receipt,
                "file": arcname,
                "entries": len(entries),
                "locationFile": location_arcname,
                "locationEntries": len(location_entries),
            })
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        # Note: the system event log is NOT included in this archive. It has
        # its own dedicated `/api/system-log/export` endpoint so operators
        # can decide whether to share it (it contains diagnostic details —
        # email addresses, case labels — that don't belong with a case
        # archive intended for external review).
    # Human-readable download timestamp: YYYY-MM-DD-HHMMSS-UTC so the file
    # sorts chronologically and is obvious when saved to disk.
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S-UTC")
    filename = f"uscis-archive-{stamp}.zip"
    # Build the response by hand so we don't depend on a specific Flask
    # version's send_file signature (`attachment_filename` vs `download_name`).
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


DEFAULT_SYSLOG_PAGE_SIZE = 100
MAX_SYSLOG_PAGE_SIZE = 500


@app.route("/api/system-log")
def api_system_log():
    """Return a paginated slice of the structured event log.

    Pages are counted from the *newest* end:
      - `offset=0, limit=100` → most recent 100 entries (oldest-first within
        the slice — the UI reverses for newest-first display).
      - `offset=100, limit=100` → the 100 entries older than the first page.

    Response shape:
      {
        "events": [...],   # oldest-first slice for this page
        "total": <int>,    # count of all entries on disk
        "limit": <int>,
        "offset": <int>,
      }

    `total` is always the true on-disk count so the UI can compute
    `ceil(total/limit)` pages and render navigation. It is independent of
    the slice returned — a `limit=100` fetch against an 888-entry log
    still reports `total=888`.

    Query params:
      - `limit=N` (default 100, clamped to [1, 500])
      - `offset=N` (default 0, clamped to >= 0)
    """
    def _positive_int(raw: str | None, default: int, *, max_val: int | None = None) -> int:
        try:
            v = int(raw) if raw is not None else default
        except ValueError:
            v = default
        v = max(v, 0)
        if max_val is not None:
            v = min(v, max_val)
        return v

    limit = _positive_int(
        request.args.get("limit"),
        DEFAULT_SYSLOG_PAGE_SIZE,
        max_val=MAX_SYSLOG_PAGE_SIZE,
    ) or 1  # never zero: an explicit limit=0 would be useless, round up.
    offset = _positive_int(request.args.get("offset"), 0)

    entries = read_system_log()   # oldest-first on disk
    total = len(entries)
    end = max(0, total - offset)
    start = max(0, end - limit)
    page_events = entries[start:end]
    return jsonify(
        {
            "events": page_events,
            "total": total,
            "limit": limit,
            "offset": offset,
        }
    )


@app.route("/api/system-log/export")
def api_system_log_export():
    """Return the system log as a downloadable JSON file.

    Separate from `/api/export` (the cases archive) because the system log
    contains diagnostic details (email addresses, case labels, scheduler
    fires) that the operator may want to keep local even when sharing a
    case archive for external review.
    """
    entries = read_system_log()
    body = json.dumps(entries, indent=2)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S-UTC")
    filename = f"uscis-system-log-{stamp}.json"
    return Response(
        body,
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/system-log/clear", methods=["POST"])
def api_system_log_clear():
    """Wipe the system log. Two-step confirmation is enforced client-side.

    Server-side we require the body to carry `{"confirm": true}` so a stray
    curl / XSRF probe cannot clear the log by accident.
    """
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"ok": False, "error": "confirmation_required"}), 400

    prior = len(read_system_log())
    try:
        clear_system_log()
    except OSError as exc:  # pragma: no cover — filesystem should not fail
        return jsonify({"ok": False, "error": str(exc)}), 500

    # Record the clear itself so there's always an audit breadcrumb of who
    # wiped what and when. (This single entry is the only thing the fresh
    # log contains after the POST returns.)
    sys_log("system_log_cleared", source="server", prior_entry_count=prior)
    return jsonify({"ok": True, "priorEntryCount": prior})


@app.route("/api/pull", methods=["POST"])
def api_pull():
    with _pull_lock:
        if _pull_state.running:
            return jsonify({"ok": False, "error": "pull_in_progress"}), 409
    # `trigger="manual"` ends up on the consolidated `pull` system-log
    # entry as the `trigger` field — no separate `pull_triggered_manually`
    # row anymore.
    _spawn_pull_async(trigger="manual")
    return jsonify({"ok": True, "message": "Pull started"})


@app.route("/api/pull/status")
def api_pull_status():
    with _pull_lock:
        state = asdict(_pull_state)
    # Subprocess stdout/stderr can contain credentials or verification codes if
    # anything goes wrong inside session_fetch — never surface it to the
    # dashboard. A summary error code is enough for the UI.
    state.pop("log_tail", None)
    state["next_run"] = _next_run_iso()
    state["schedule"] = {
        "timezone": SCHEDULER_TZ,
        "hours": list(PULL_HOURS),
    }
    state["server_time"] = _now_iso()
    return jsonify(state)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Install the access gate before the scheduler / blueprint work —
    # pulls auth.optional_access_code out of config.json; no-op if it's empty.
    try:
        config = load_config()
        optional_access_code = (config.get("auth") or {}).get("optional_access_code") or ""
    except Exception as e:
        # Config load failing at startup means the operator can't reach
        # the dashboard without a restart. Log loudly AND continue with
        # an unarmed access gate — at least they can still open the site
        # and see the error banner.
        sys_log(
            "server_config_load_failed_at_startup", level="error",
            source="server",
            error=f"{type(e).__name__}: {e}"[:200],
            traceback_tail="".join(
                traceback.format_exception(type(e), e, e.__traceback__)
            )[-800:],
        )
        logger.exception("Failed to load config at startup; access gate disabled.")
        optional_access_code = ""

    try:
        configure_access_gate(app, optional_access_code, root=ROOT)
    except Exception as e:
        # Access gate wiring failure leaves the app without auth. Log
        # and re-raise — this one IS fatal because a publicly-reachable
        # dashboard without an access gate is worse than a dead dashboard.
        sys_log(
            "access_gate_configure_failed", level="error", source="server",
            error=f"{type(e).__name__}: {e}"[:200],
            traceback_tail="".join(
                traceback.format_exception(type(e), e, e.__traceback__)
            )[-800:],
        )
        raise

    try:
        _setup_scheduler()
    except Exception as e:
        # Scheduler-start failure means no automatic pulls will fire.
        # The web UI still works (manual pulls, viewing snapshots); log
        # loudly and keep serving rather than crash the dashboard.
        sys_log(
            "scheduler_setup_failed", level="error", source="server",
            error=f"{type(e).__name__}: {e}"[:200],
            traceback_tail="".join(
                traceback.format_exception(type(e), e, e.__traceback__)
            )[-800:],
        )
        logger.exception("Scheduler failed to start; automatic pulls disabled.")
    else:
        logger.info(
            "Scheduler started: daily pulls at %s (%s)",
            ", ".join(f"{h:02d}:00" for h in PULL_HOURS),
            SCHEDULER_TZ,
        )

    sys_log(
        "server_startup",
        source="server",
        schedule_hours=list(PULL_HOURS),
        schedule_timezone=SCHEDULER_TZ,
        access_gate_armed=bool(optional_access_code),
    )
    # Don't use reloader — it spawns two processes and double-schedules jobs.
    # Bind to all interfaces — production access is gated by the optional
    # access-code middleware (see access_gate.py) when auth.optional_access_code is set.
    try:
        app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)
    except Exception as e:
        # If `app.run` itself blows up (port already bound, etc.) we
        # still want a dashboard trail even if nothing is listening.
        sys_log(
            "server_run_failed", level="error", source="server",
            error=f"{type(e).__name__}: {e}"[:200],
        )
        raise


if __name__ == "__main__":  # pragma: no cover
    main()
