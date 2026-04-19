#!/usr/bin/env python3
"""USCIS Checker — long-running local web dashboard.

Launches a Flask app on http://localhost:8080 that:
  - Reads case snapshots from `data/*_logs.json`
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
import re
import subprocess
import sys
import threading
import time
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
    summarize_case,
)
from mailer import notify_update
from system_log import log as sys_log, read_all as read_system_log

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


def _log_file_for(form_label: str) -> Path | None:
    """Resolve the snapshot-log path for a form label.

    The regex already constrains the filename to digits only, but we
    verify path containment explicitly so any future relaxation of the
    pattern can't silently enable path traversal.
    """
    m = _FORM_NUM_RE.search(form_label or "")
    if not m:
        return None
    candidate = (DATA_DIR / f"{m.group(1)}_logs.json").resolve()
    if candidate.parent != DATA_DIR.resolve():  # pragma: no cover — regex forbids traversal today
        return None
    return candidate


def load_entries(form_label: str) -> list[dict]:
    path = _log_file_for(form_label)
    if not path or not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON; treating as empty.", path.name)
        return []


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
    """Every diff across every configured case, same shape as /api/updates."""
    config = config or load_config()
    records: list[dict] = []
    for c in config.get("cases", []):
        label = c["label"]
        receipt = c["id"]
        for change in day_changes(load_entries(label)):
            detected_on = (change.get("to") or "")[:10]
            real_update_date = (change.get("scalars") or {}).get("updatedAt", {}).get("to")
            rec = dict(change)
            rec.update({
                "id": f"{receipt}:{detected_on}",
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


def _send_notifications_for_new(new_records: list[dict]) -> None:
    if not new_records:
        return
    try:
        config = load_config()
        auth = (config.get("auth") or {})
        recipient = _notify_recipient(auth)
        if not recipient or not auth.get("uscis_mfa_email") or not auth.get("uscis_mfa_app_password"):
            logger.warning("Email auth missing — skipping %d notification(s).", len(new_records))
            sys_log("notify_skipped", level="warning", source="server",
                    reason="auth_missing", count=len(new_records))
            return
        for rec in new_records:
            try:
                notify_update(auth, recipient, rec, EVENT_CODE_LABELS)
                logger.info("Notified: %s (%s)", rec.get("id"), rec.get("kind"))
                sys_log("notify_sent", source="server",
                        record_id=rec.get("id"), kind=rec.get("kind"))
            except Exception as e:
                logger.exception("Notify failed for %s: %s", rec.get("id"), e)
                sys_log("notify_failed", level="error", source="server",
                        record_id=rec.get("id"), error=str(e))
    except Exception as e:
        logger.exception("Notification dispatcher crashed: %s", e)
        sys_log("notify_dispatcher_crashed", level="error", source="server",
                error=str(e))


def _run_pull_subprocess() -> None:
    """Invoke session_fetch.py run. Captures tail of stdout+stderr for the UI.

    Snapshots the set of diff IDs before and after the pull so that any
    records appearing as a direct result can be emailed exactly once.
    The snapshot lives only in this function's scope — restart-safe.
    """
    global _pull_state
    with _pull_lock:
        if _pull_state.running:
            logger.info("Pull already running; skipping trigger.")
            sys_log("pull_skipped_already_running", source="server")
            return
        _pull_state = PullState(
            running=True,
            started_at=_now_iso(),
        )

    start_wall = time.time()
    # Snapshot the diff set *before* pulling.
    before_ids = _update_ids(_all_update_records())
    sys_log("pull_started", source="server", before_diff_count=len(before_ids))

    logger.info("Spawning pull: %s", " ".join(PULL_CMD))
    try:
        proc = subprocess.run(
            PULL_CMD,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=600,
        )
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        tail_lines = (stdout + "\n" + stderr).splitlines()[-80:]
        duration = round(time.time() - start_wall, 2)
        with _pull_lock:
            _pull_state.running = False
            _pull_state.finished_at = _now_iso()
            _pull_state.exit_code = proc.returncode
            _pull_state.ok = proc.returncode == 0
            _pull_state.log_tail = tail_lines
            if proc.returncode != 0:
                _pull_state.last_error = f"exit={proc.returncode}"
        logger.info("Pull finished exit=%d", proc.returncode)

        # Only email on a successful pull. A failed pull may leave the
        # snapshot set half-updated; don't treat partial state as news.
        if proc.returncode == 0:
            after_records = _all_update_records()
            new_records = [
                r for r in after_records if r.get("id") not in before_ids
            ]
            sys_log(
                "pull_finished",
                source="server",
                exit_code=0,
                duration_seconds=duration,
                new_diff_count=len(new_records),
            )
            if new_records:
                logger.info("Emitting %d notification(s).", len(new_records))
                _send_notifications_for_new(new_records)
            else:
                logger.info("No new diffs — no email sent.")
        else:
            sys_log(
                "pull_failed",
                level="error",
                source="server",
                exit_code=proc.returncode,
                duration_seconds=duration,
                stderr_tail=stderr.splitlines()[-10:],
            )
    except subprocess.TimeoutExpired:
        duration = round(time.time() - start_wall, 2)
        with _pull_lock:
            _pull_state.running = False
            _pull_state.finished_at = _now_iso()
            _pull_state.exit_code = -1
            _pull_state.ok = False
            _pull_state.last_error = "timeout (10min)"
        logger.error("Pull timed out after 10min.")
        sys_log("pull_timeout", level="error", source="server",
                duration_seconds=duration)
    except Exception as e:
        duration = round(time.time() - start_wall, 2)
        with _pull_lock:
            _pull_state.running = False
            _pull_state.finished_at = _now_iso()
            _pull_state.ok = False
            _pull_state.last_error = str(e)
        logger.exception("Pull crashed.")
        sys_log("pull_crashed", level="error", source="server",
                duration_seconds=duration, error=str(e))


def _spawn_pull_async() -> None:
    t = threading.Thread(target=_run_pull_subprocess, daemon=True)
    t.start()


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

scheduler = BackgroundScheduler(timezone=SCHEDULER_TZ)


def _setup_scheduler() -> None:
    for hour in PULL_HOURS:
        scheduler.add_job(
            _spawn_pull_async,
            CronTrigger(hour=hour, minute=0, timezone=SCHEDULER_TZ),
            id=f"pull-{hour:02d}",
            name=f"Daily pull @ {hour:02d}:00 {SCHEDULER_TZ}",
            replace_existing=True,
        )
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
        entries = load_entries(c["label"])
        days = bin_by_day(entries)
        latest = days[-1] if days else None
        latest_data = (latest or {}).get("data") or {}
        summary = summarize_case(entries, today_iso=today) if entries else {}

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
            }
        )
    return jsonify({"cases": cases, "eventCodeLabels": EVENT_CODE_LABELS})


@app.route("/api/cases/<label>/history")
def api_case_history(label: str):
    entries = load_entries(label)
    days = bin_by_day(entries)
    changes = day_changes(entries)
    return jsonify(
        {
            "label": label,
            "entries": entries,         # all raw captures
            "days": days,               # one entry per UTC day (latest of day)
            "changes": changes,         # day-to-day diffs (only days that changed)
        }
    )


@app.route("/api/updates")
def api_updates():
    """Flat feed of every diff across every configured case, newest first.

    Each record is enriched with:
      - `id`            stable key `{receipt}:{toDate}` (dedup / email tracking)
      - `caseLabel`     e.g. "I-485"
      - `receiptNumber`
      - `detectedOn`    day we observed this diff (YYYY-MM-DD)
      - `realUpdateDate`   actual updatedAt date post-change (may differ from detectedOn)
    Plus the original diff body (scalars / events / notices / documents / kind).
    """
    config = load_config()
    records: list[dict] = []
    for c in config.get("cases", []):
        label = c["label"]
        receipt = c["id"]
        for change in day_changes(load_entries(label)):
            detected_on = (change.get("to") or "")[:10]
            real_update_date = (
                (change.get("scalars") or {})
                .get("updatedAt", {})
                .get("to")
            )
            rec = dict(change)
            rec.update(
                {
                    "id": f"{receipt}:{detected_on}",
                    "caseLabel": label,
                    "receiptNumber": receipt,
                    "detectedOn": detected_on,
                    "realUpdateDate": real_update_date,
                }
            )
            records.append(rec)

    records.sort(key=lambda r: r.get("to") or "", reverse=True)
    return jsonify({"updates": records, "eventCodeLabels": EVENT_CODE_LABELS})


@app.route("/api/export")
def api_export():
    """Stream a zip containing every `data/*_logs.json` plus a manifest.

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
            path = _log_file_for(label)
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
            manifest["cases"].append({
                "label": label,
                "receiptNumber": receipt,
                "file": arcname,
                "entries": len(entries),
            })
        z.writestr("manifest.json", json.dumps(manifest, indent=2))
        # Include the system event log — crucial for post-hoc debugging.
        try:
            z.writestr(
                "system_log.json",
                json.dumps(read_system_log(), indent=2),
            )
        except Exception:  # pragma: no cover — never fail an export on log read
            pass
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


@app.route("/api/system-log")
def api_system_log():
    """Return the structured event log (for the dashboard's System log tab).

    Optional query params:
      - `limit=N` — return only the last N events (default: all up to MAX_ENTRIES).
    """
    try:
        limit_raw = request.args.get("limit")
        limit = int(limit_raw) if limit_raw else None
    except ValueError:
        limit = None
    entries = read_system_log(limit=limit)
    return jsonify({"events": entries})


@app.route("/api/pull", methods=["POST"])
def api_pull():
    with _pull_lock:
        if _pull_state.running:
            return jsonify({"ok": False, "error": "pull_in_progress"}), 409
    sys_log("pull_triggered_manually", source="server")
    _spawn_pull_async()
    return jsonify({"ok": True, "message": "Pull started"})


@app.route("/api/test-email", methods=["POST"])
def api_test_email():
    """Send ONE email with the most recent real diff record as sample payload.

    Uses the most recent real update so the mail shows realistic content.
    If there are no updates at all, sends a synthetic "this is a test" record.
    """
    config = load_config()
    auth = (config.get("auth") or {})
    recipient = _notify_recipient(auth)
    if not recipient or not auth.get("uscis_mfa_email") or not auth.get("uscis_mfa_app_password"):
        return jsonify({"ok": False, "error": "auth missing: uscis_mfa_email, uscis_mfa_app_password"}), 400

    records = _all_update_records(config)
    if records:
        sample = max(records, key=lambda r: r.get("to") or "")
    else:
        sample = {
            "id": "TEST:sample",
            "caseLabel": "I-TEST",
            "receiptNumber": "IOE0000000000",
            "kind": "silent_update",
            "from": "2026-01-01T00:00:00Z",
            "to": "2026-01-02T00:00:00Z",
            "detectedOn": "2026-01-02",
            "realUpdateDate": "2026-01-02",
            "scalars": {
                "updatedAt": {"from": "2026-01-01", "to": "2026-01-02"},
            },
            "events": {"added": [], "removed": []},
            "notices": {"added": [], "removed": []},
        }

    try:
        notify_update(auth, recipient, sample, EVENT_CODE_LABELS)
    except Exception:
        # Full traceback is in server logs — don't echo it to the caller,
        # where SMTP errors routinely include the email address or
        # auth-failure reasons that shouldn't reach the browser.
        logger.exception("Test email failed")
        return jsonify({"ok": False, "error": "send_failed"}), 500
    return jsonify({
        "ok": True,
        "to": recipient,
        "sampleId": sample.get("id"),
        "kind": sample.get("kind"),
    })


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
    except Exception:
        optional_access_code = ""
    configure_access_gate(app, optional_access_code, root=ROOT)

    _setup_scheduler()
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
    app.run(host="0.0.0.0", port=8080, debug=False, use_reloader=False)


if __name__ == "__main__":  # pragma: no cover
    main()
