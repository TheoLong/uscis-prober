#!/usr/bin/env python3
# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""USCIS Prober — long-running local web dashboard.

Launches a Flask app on http://localhost:8080 that:
  - Reads case snapshots from `data/*_case.json`
  - Exposes REST endpoints the UI uses to render visualisations & diffs
  - Runs `session_fetch.py run` in a subprocess on demand (button) and on
    a cron schedule defined by `pull_hours` in config.json (America/New_York)
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

import playwright as _pw_module

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from flask import Flask, Response, jsonify, request, send_from_directory

from access_gate import configure as configure_access_gate
from diff_utils import (
    EVENT_CODE_LABELS,
    bin_by_day,
    day_changes,
    location_day_changes,
    summarize_case,
)
from mailer import notify_update
from redaction import redact_obj as _redact_obj
from system_log import (
    JSONL_STDERR_ENV as _SYSLOG_JSONL_ENV,
    log as sys_log,
    read_all as read_system_log,
    clear as clear_system_log,
    parse_jsonl_stderr_line as _parse_syslog_jsonl_line,
    push_capture as _syslog_push_capture,
    pop_capture as _syslog_pop_capture,
)

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = ROOT / "config.json"
STATIC_DIR = Path(__file__).resolve().parent / "static"
# Path mirrored from uscis_auth.STORAGE_STATE_PATH so the storage
# accounting doesn't need to import the auth module (which would
# pull in Playwright at Flask-route time).
STORAGE_SESSION_PATH = ROOT / ".uscis_session.json"

SCHEDULER_TZ = "America/New_York"
# Active cron hours for the automatic pull (24h, America/New_York).
# REQUIRED config with no baked-in default — the schedule and its
# rationale live entirely in config.json's `pull_hours` array (see
# load_pull_hours and config.example.json). Initialised empty so any
# read before main() resolves it schedules nothing rather than a hidden
# default. Every read-site (scheduler loop, /api status, startup log)
# uses this module global.
PULL_HOURS: tuple[int, ...] = ()

PULL_CMD = [sys.executable, str(Path(__file__).resolve().parent / "session_fetch.py"), "run"]

# Retry policy for a pull. `retry` + `retry_wait_seconds` are REQUIRED
# top-level keys in config.json — see config.example.json for the
# canonical template. Out-of-range values are clamped to these caps
# so a typo like `retry_wait_seconds: 6000` doesn't wedge a pull for
# 100 minutes.
RETRY_MAX_COUNT = 5
RETRY_MAX_WAIT_SECONDS = 600


class ConfigError(RuntimeError):
    """Raised when a required config.json field is missing or invalid."""


@dataclass(frozen=True)
class RetryPolicy:
    """Validated retry settings for a single pull.

    `retry` is the number of *additional* attempts after the initial
    failure — so `retry=0` means no retry, `retry=2` means two retries
    (3 total attempts), and so on. `retry_wait_seconds` is the delay
    between attempts.
    """
    retry: int
    retry_wait_seconds: float

    @property
    def total_attempts(self) -> int:
        return self.retry + 1


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


def load_retry_policy(config: dict | None = None) -> RetryPolicy:
    """Read the retry policy from config.json.

    Both `retry` and `retry_wait_seconds` are REQUIRED — missing keys
    raise `ConfigError` with a message that points the operator to
    config.example.json. This is deliberate: retry behaviour is
    load-bearing (especially for scheduled pulls that hit
    anti-bot throttling), so silently falling back to implicit defaults
    would hide misconfiguration from the operator who set up the VM.

    Non-numeric values also raise (catching typos at load time instead
    of masking them as "0 retries"). Out-of-range values are clamped
    with a warning so a well-meaning but inflated number can't make a
    pull sleep for an hour.
    """
    if config is None:
        config = load_config()  # propagate FileNotFoundError / JSONDecodeError

    if "retry" not in config:
        raise ConfigError(
            "config.json is missing required key `retry` (int, >=0). "
            "See config.example.json for the canonical template — "
            "recommended values are retry=2 and retry_wait_seconds=180."
        )
    if "retry_wait_seconds" not in config:
        raise ConfigError(
            "config.json is missing required key `retry_wait_seconds` "
            "(number of seconds between retry attempts, >=0). "
            "See config.example.json — recommended value is 180."
        )

    raw_retry = config["retry"]
    raw_wait = config["retry_wait_seconds"]

    try:
        retry = int(raw_retry)
    except (TypeError, ValueError):
        raise ConfigError(
            f"config.retry={raw_retry!r} is not an integer."
        )

    try:
        wait = float(raw_wait)
    except (TypeError, ValueError):
        raise ConfigError(
            f"config.retry_wait_seconds={raw_wait!r} is not numeric."
        )

    if retry < 0:
        raise ConfigError(
            f"config.retry={retry} must be >= 0 (0 disables retry)."
        )
    if wait < 0:
        raise ConfigError(
            f"config.retry_wait_seconds={wait} must be >= 0."
        )

    clamped_retry = _clamp(retry, 0, RETRY_MAX_COUNT)
    if clamped_retry != retry:
        logger.warning("config.retry=%d above cap %d; clamped to %d.",
                       retry, RETRY_MAX_COUNT, clamped_retry)

    clamped_wait = _clamp(wait, 0.0, float(RETRY_MAX_WAIT_SECONDS))
    if clamped_wait != wait:
        logger.warning("config.retry_wait_seconds=%s above cap %d; "
                       "clamped to %s.", wait, RETRY_MAX_WAIT_SECONDS,
                       clamped_wait)

    return RetryPolicy(retry=clamped_retry, retry_wait_seconds=clamped_wait)


def load_trace_successful_pulls(config: dict | None = None) -> bool:
    """Read `trace_successful_pulls` (bool) from config.json.

    Optional field — default is `false`. Traces are only written on
    auth failures. The UI's debug-mode pill flips this field on
    demand (via /api/debug-mode) to capture a trace on every pull
    for verification; it's not a field the operator edits by hand.
    Missing or absent = false; invalid type = ConfigError.
    """
    if config is None:
        config = load_config()
    raw = config.get("trace_successful_pulls", False)
    if not isinstance(raw, bool):
        raise ConfigError(
            f"config.trace_successful_pulls={raw!r} must be a boolean."
        )
    return raw


def load_pull_hours(config: dict | None = None) -> tuple[int, ...]:
    """Read the automatic-pull schedule from config.json `pull_hours`.

    REQUIRED field — there is no implicit default. Missing key raises
    ConfigError pointing at the template, exactly like `retry` /
    `retry_wait_seconds`. The schedule is load-bearing, so a deployment
    that forgot to set it should fail loudly at startup rather than
    silently fall back to some hardcoded cadence the operator never chose.

    The canonical schedule, its starter value, and the rationale for it
    live in config.json / config.example.json — not here. This function
    only validates and normalises whatever the operator configured.

    Accepts a non-empty JSON array of integer hours in 24h
    America/New_York. The list is normalised to a sorted, de-duplicated
    tuple so `[20, 7, 7, 14]` becomes `(7, 14, 20)` — duplicate slots
    would otherwise register colliding APScheduler job ids.

    Raises ConfigError on:
      - missing key                 -> required, see config.example.json
      - not a list                  -> must be a JSON array
      - empty list                  -> at least one hour is required
      - non-integer / bool item     -> every entry must be an int 0-23
      - hour outside 0..23          -> 24 is NOT midnight; CronTrigger
                                        only accepts 0..23, use 0
    """
    if config is None:
        config = load_config()
    if "pull_hours" not in config:
        raise ConfigError(
            "config.json is missing required key `pull_hours` (non-empty "
            "array of integer hours 0-23, 24h America/New_York). See "
            "config.example.json for the canonical template and starter "
            "schedule."
        )
    raw = config["pull_hours"]
    if not isinstance(raw, list):
        raise ConfigError(
            f"config.pull_hours={raw!r} must be a JSON array of "
            f"integer hours 0-23 (see config.example.json)."
        )
    if not raw:
        raise ConfigError(
            "config.pull_hours is an empty list — at least one hour is "
            "required (see config.example.json)."
        )
    hours: set[int] = set()
    for item in raw:
        # bool is an int subclass; reject it explicitly so `true` isn't
        # silently read as hour 1.
        if isinstance(item, bool) or not isinstance(item, int):
            raise ConfigError(
                f"config.pull_hours contains a non-integer entry {item!r}; "
                f"every entry must be an integer hour 0-23."
            )
        if item < 0 or item > 23:
            raise ConfigError(
                f"config.pull_hours entry {item} is out of range — hours "
                f"must be 0-23 (24h clock; 24 is not midnight, use 0)."
            )
        hours.add(item)
    return tuple(sorted(hours))


def load_redaction_enabled(config: dict | None = None) -> bool:
    """Read `redaction_enabled` (bool) from config.json.

    Optional field — default `false`. When true, the server masks PII in
    every JSON response (see the after-request hook) and blocks the data
    exports, so the dashboard can be shared without leaking private data.
    Toggled from the UI via /api/redaction-mode. Missing = false; a
    non-boolean is treated as false (defensive: a bad value must never
    accidentally *disable* sharing-safety, and reads happen per-response).
    """
    if config is None:
        try:
            config = load_config()
        except Exception:
            return False
    return config.get("redaction_enabled", False) is True


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
# Build version
# ---------------------------------------------------------------------------

def _version_label_from_iso(commit_iso: str | None) -> str | None:
    """Derive a sortable date-time version label from a commit's ISO date.

    `2026-04-22T20:32:28-04:00` → `2026-04-22.2032` (UTC).

    The label is:
      - **Sortable** — lexicographic comparison matches chronological order
        (`2026-04-22.2032` < `2026-04-22.2145`). The operator can eyeball
        "newer or older?" without remembering commit counts or hashes.
      - **Unique per commit-minute** — two commits in the same minute
        would collide, but that's vanishingly rare in a solo-dev repo.
      - **Derived from the commit's own timestamp** — NOT the boot time.
        Restarting the server with the same code doesn't bump the label.
    """
    if not commit_iso:
        return None
    try:
        # Parse even timezone-offset ISO strings. Normalize to UTC so the
        # label is stable regardless of the developer's local clock.
        dt = datetime.fromisoformat(commit_iso).astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d.%H%M")
    except (ValueError, TypeError):
        return None


def _resolve_version() -> dict:
    """Capture build-version metadata once at import time.

    Returns:
      - `label`:       human-readable sortable version like `2026-04-22.2032`
                       derived from the commit's authored time (UTC). The
                       operator can tell newer vs. older at a glance.
      - `sha`:         short commit hash (`fcb03e0`) — disambiguates when
                       two commits share the same minute.
      - `full_sha`:    40-char hash.
      - `commit_date`: raw ISO-8601 when this commit was authored.
      - `boot_time`:   when the server process started (useful for
                       spotting a no-code-change restart).

    Tries git rev-parse first (works on both dev boxes and the deployed
    VM, since the deploy script uses `git reset --hard`). Falls back to
    a static `.version` file written at deploy time.
    """
    result = {
        "label": None,
        "sha": "unknown",
        "full_sha": "unknown",
        "commit_date": None,
        "boot_time": datetime.now(timezone.utc).replace(microsecond=0)
                     .isoformat().replace("+00:00", "Z"),
    }
    try:
        import subprocess as _subproc
        sha = _subproc.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(ROOT), stderr=_subproc.DEVNULL, timeout=2,
        ).decode().strip()
        if sha:
            result["sha"] = sha
        full = _subproc.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=str(ROOT), stderr=_subproc.DEVNULL, timeout=2,
        ).decode().strip()
        if full:
            result["full_sha"] = full
        date = _subproc.check_output(
            ["git", "log", "-1", "--format=%cI"],
            cwd=str(ROOT), stderr=_subproc.DEVNULL, timeout=2,
        ).decode().strip()
        if date:
            result["commit_date"] = date
            result["label"] = _version_label_from_iso(date)
    except Exception:
        # Fallback: a static `.version` file written by the deploy script.
        # Format: "{label}\n{short}\n{full}\n{iso_commit_date}\n"
        # Any line optional.
        version_file = ROOT / ".version"
        if version_file.exists():
            try:
                lines = version_file.read_text().splitlines()
                if len(lines) >= 1 and lines[0].strip():
                    result["label"] = lines[0].strip()
                if len(lines) >= 2 and lines[1].strip():
                    result["sha"] = lines[1].strip()
                if len(lines) >= 3 and lines[2].strip():
                    result["full_sha"] = lines[2].strip()
                if len(lines) >= 4 and lines[3].strip():
                    result["commit_date"] = lines[3].strip()
                    if result["label"] is None:
                        result["label"] = _version_label_from_iso(
                            lines[3].strip()
                        )
            except Exception:
                pass
    return result


VERSION = _resolve_version()


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
    # Across retries we accumulate steps from every attempt, each
    # annotated with `attempt: N` (1-indexed) so the dashboard can group
    # them visually without losing the whole attempt's trace.
    steps: list[dict] = []
    # Per-attempt plain-stderr tails (non-event lines — Python
    # tracebacks, prints). Preserved across retries so a crash
    # signature on attempt 1 isn't lost when attempt 2 starts.
    per_attempt_stderr: list[list[str]] = []
    exit_code: int | None = None
    top_level: str = "info"
    timed_out = False
    crashed_error: str | None = None

    try:
        policy = load_retry_policy()
    except ConfigError as e:
        # Surface the specific missing/invalid field as a top-level
        # error step instead of crashing the pull thread. This puts an
        # actionable message in the dashboard System-log tab the next
        # time the operator opens it.
        sys_log(
            "pull_config_error", level="error", source="server",
            trigger=trigger,
            error=str(e),
        )
        with _pull_lock:
            _pull_state.running = False
            _pull_state.finished_at = _now_iso()
            _pull_state.ok = False
            _pull_state.last_error = str(e)
        return {
            "event": "pull",
            "level": "error",
            "source": "server",
            "trigger": trigger,
            "started_at": start_iso,
            "finished_at": _pull_state.finished_at,
            "duration_seconds": round(time.time() - start_wall, 2),
            "exit_code": None,
            "timed_out": False,
            "attempts": 0,
            "summary": {
                "case_snapshots": 0, "location_snapshots": 0,
                "case_fetch_failures": 0, "location_fetch_failures": 0,
                "new_diffs_emailed": 0, "notify_failures": 0,
                "session_expired_retries": 0, "attempts": 0,
            },
            "steps": list(thread_captured_steps),
        }
    attempt_num = 0

    # Retry loop. Each iteration is one full subprocess run. We stop
    # early on success, a timeout, a subprocess crash, or when retries
    # are exhausted. Only auth failures (identified by the presence of
    # the `cli_run_auth_failed` step) trigger a retry — a per-case API
    # blip or a mail error is not retry-worthy at this layer.
    while attempt_num < policy.total_attempts:
        attempt_num += 1

        if attempt_num > 1:
            # Announce that we're about to retry BEFORE sleeping so the
            # dashboard shows a live "retry pending" row rather than a
            # dead-air gap.
            sys_log(
                "auth_retry_waiting",
                source="server",
                attempt_after=attempt_num - 1,
                attempt_next=attempt_num,
                wait_seconds=policy.retry_wait_seconds,
            )
            time.sleep(policy.retry_wait_seconds)
            sys_log(
                "auth_retry_starting",
                source="server",
                attempt=attempt_num,
                total_attempts=policy.total_attempts,
            )

        attempt_steps: list[dict] = []
        attempt_plain_tail: list[str] = []
        attempt_exit: int | None = None
        attempt_timed_out = False
        attempt_crash: str | None = None

        logger.info("Spawning pull (attempt %d/%d): %s",
                    attempt_num, policy.total_attempts, " ".join(PULL_CMD))
        # Propagate trace-on-success into the child so the auth module
        # can decide whether to persist traces for successful logins.
        # Read fresh from config each attempt so a live config edit
        # takes effect on the next retry without a restart.
        try:
            trace_on_success = load_trace_successful_pulls()
        except ConfigError:
            trace_on_success = False
        # Forensic-retention rule: once any attempt in this pull has
        # failed, the whole pull envelope is flagged error at the top
        # level. Every attempt that belongs to such a pull must keep
        # its trace — including the retry that finally succeeded —
        # so the operator can diff "what USCIS returned on attempt 1"
        # vs "what USCIS returned on attempt 2" side-by-side in the
        # trace viewer. Without this, a successful retry discards
        # exactly the evidence that would explain the failure.
        force_trace_for_retry = attempt_num > 1
        child_env = {
            **os.environ,
            _SYSLOG_JSONL_ENV: "1",
            "USCIS_TRACE_ON_SUCCESS": (
                "1" if (trace_on_success or force_trace_for_retry) else "0"
            ),
            # Trigger label lands in the trace directory name so an
            # operator can tell at a glance whether a saved trace came
            # from a scheduled pull vs a manual click.
            "USCIS_PULL_TRIGGER": trigger or "pull",
        }
        try:
            proc = subprocess.run(
                PULL_CMD,
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=600,
                env=child_env,
            )
            attempt_exit = proc.returncode
            stderr = proc.stderr or ""

            attempt_steps, attempt_plain_tail = _collect_subprocess_steps(
                stderr
            )
            per_attempt_stderr.append(attempt_plain_tail)
            if attempt_exit != 0:
                attempt_steps.append({
                    "ts": _now_iso(), "event": "subprocess_exit_nonzero",
                    "level": "error", "source": "server",
                    "exit_code": attempt_exit,
                    "stderr_tail": attempt_plain_tail[-10:],
                    # Include every prior attempt's tail too so the
                    # operator can compare "what attempt 1 printed vs
                    # what attempt 2 printed" without hunting through
                    # the stream.
                    "all_attempts_stderr_tails": [
                        t[-10:] for t in per_attempt_stderr
                    ],
                })
        except subprocess.TimeoutExpired:
            attempt_timed_out = True
            attempt_exit = -1
            attempt_steps.append({
                "ts": _now_iso(), "event": "subprocess_timeout",
                "level": "error", "source": "server",
                "timeout_seconds": 600,
            })
        except Exception as e:
            attempt_crash = str(e)
            attempt_steps.append({
                "ts": _now_iso(), "event": "subprocess_crashed",
                "level": "error", "source": "server",
                "error": str(e),
            })

        # Tag each step with its attempt number for dashboard grouping.
        for s in attempt_steps:
            s.setdefault("attempt", attempt_num)

        steps.extend(attempt_steps)
        exit_code = attempt_exit
        timed_out = attempt_timed_out or timed_out
        if attempt_crash is not None:
            crashed_error = attempt_crash

        logger.info("Pull attempt %d finished exit=%s",
                    attempt_num, attempt_exit)

        # Success: stop retrying, send notifications, break out.
        if attempt_exit == 0:
            break

        # Classify the failure. Auth failures are retry-worthy; anything
        # else (timeout, crash, non-auth subprocess failure) is not — we
        # want to avoid hammering USCIS on a malformed config or a
        # genuinely broken code path.
        auth_failed = any(
            s.get("event") == "cli_run_auth_failed" for s in attempt_steps
        )
        retryable = auth_failed and not attempt_timed_out and attempt_crash is None

        if attempt_num >= policy.total_attempts or not retryable:
            break

    # Post-loop: update _pull_state once with the final attempt's result.
    duration = round(time.time() - start_wall, 2)
    with _pull_lock:
        _pull_state.running = False
        _pull_state.finished_at = _now_iso()
        _pull_state.exit_code = exit_code
        _pull_state.ok = exit_code == 0 and not timed_out and crashed_error is None
        _pull_state.log_tail = []
        if crashed_error is not None:
            _pull_state.last_error = crashed_error
        elif timed_out:
            _pull_state.last_error = "timeout (10min)"
        elif exit_code not in (0, None):
            _pull_state.last_error = f"exit={exit_code}"
        else:
            _pull_state.last_error = None

    # Only email on a successful pull. A failed pull may leave the
    # snapshot set half-updated; partial state isn't news.
    if exit_code == 0 and not timed_out and crashed_error is None:
        after_records = _all_update_records()
        new_records = [
            r for r in after_records if r.get("id") not in before_ids
        ]
        if new_records:
            logger.info("Emitting %d notification(s).", len(new_records))
            steps.extend(_send_notifications_for_new(new_records))
        else:
            logger.info("No new diffs — no email sent.")

    # Snapshot the thread-captured server-process events.
    captured_steps = list(thread_captured_steps)

    # Merge: subprocess steps (from the child's JSONL stderr) + server-
    # process events captured on this thread + any explicitly-appended
    # envelope steps (e.g. subprocess_exit_nonzero). Sort by timestamp
    # so the timeline is cohesive for the dashboard's expanded view.
    all_steps = list(steps) + captured_steps
    all_steps.sort(key=lambda s: s.get("ts", ""))

    # Derive the top-level severity and summary. Three-tier rule so
    # the dashboard conveys outcome, not just worst-step-severity:
    #
    #   error (red)    — operation ultimately failed (non-zero exit,
    #                    timeout, or crash). The pull DID NOT deliver
    #                    the data it promised. Needs immediate fix.
    #   warning (yellow) — operation succeeded but a failure step
    #                    happened along the way (e.g. attempt 1 hit an
    #                    anti-bot refusal, attempt 2 recovered). The
    #                    data is in; the retry path just got exercised.
    #                    Needs attention but not urgent.
    #   info (green)   — clean run, no error OR warning steps anywhere.
    #
    # The outer function emits the envelope AFTER it pops the capture
    # so this final event reaches disk.
    operation_failed = (
        exit_code not in (0, None) or timed_out or crashed_error is not None
    )
    worst_step = _worst_level(all_steps)
    if operation_failed:
        top_level = "error"
    elif worst_step == "error":
        # Error step but successful exit → we recovered (retry path).
        # Downgrade red → yellow: "needs attention, not devastated".
        top_level = "warning"
    else:
        top_level = worst_step  # warning or info, passthrough

    summary = _pull_summary_from_steps(all_steps)
    # Attempts is max(attempt) seen in steps; missing tag defaults to 1.
    attempts_run = max(
        (s.get("attempt", 1) for s in all_steps if isinstance(s.get("attempt", 1), int)),
        default=attempt_num or 1,
    )
    summary["attempts"] = attempts_run
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
        "attempts": attempts_run,
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


@app.after_request
def _redact_json(resp):
    """When redaction mode is on, mask PII in every JSON response *before* it
    leaves the server. This is the security backbone of redaction: the masked
    data is never sent, so it can't be recovered from the console, the network
    tab, or page source. Non-JSON responses (static assets, zip exports) are
    untouched here — exports are blocked separately at their endpoints.
    """
    try:
        ctype = resp.content_type or ""
        if resp.direct_passthrough or "application/json" not in ctype:
            return resp
        if not load_redaction_enabled():
            return resp
        data = json.loads(resp.get_data(as_text=True))
    except Exception:
        return resp  # never let redaction break a response
    resp.set_data(json.dumps(_redact_obj(data)))
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
    if load_redaction_enabled():
        return jsonify({"ok": False, "error": "redaction_enabled"}), 403
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


@app.route("/api/storage")
def api_storage():
    """Return a per-category breakdown of disk usage under data/.

    Response shape:
      {
        "total_bytes": <int>,
        "categories": [
          {"key": str, "label": str, "bytes": int, "file_count": int}
        ]
      }

    Categories are disjoint and cover the full data/ tree plus the
    config / session files. Used by the System-tab storage chart, which
    renders each category as a share of the total (no fixed quota).
    """
    # Walk collects every bucket; total_bytes uses ALL of them. Display
    # categories filter out `other` (sub-kilobyte flotsam from the data
    # dir walk — stale tmp files, .DS_Store, etc) since those add visual
    # noise without conveying useful information.
    categories_all = _collect_storage_categories()
    total = sum(c["bytes"] for c in categories_all)
    display = [c for c in categories_all if c["key"] != "other"]
    return jsonify({
        "total_bytes": total,
        "categories": display,
    })


_CASE_FILE_RE = re.compile(r"^(\d+)_(case|location)\.json$")


def _collect_storage_categories() -> list[dict]:
    """Walk the data directory + top-level state files, bucketing each
    file into one of a small set of categories. Returns a list sorted
    by bytes descending so the UI can display largest-first.

    Case + location snapshots are grouped per form number into a
    single bucket per case (e.g. `case_485` labelled "I-485") —
    matching how an operator thinks about the data, not how it's
    stored.
    """
    buckets: dict[str, dict] = {}

    def _bump(key: str, label: str, size: int) -> None:
        b = buckets.get(key)
        if b is None:
            b = buckets[key] = {
                "key": key, "label": label, "bytes": 0, "file_count": 0,
            }
        b["bytes"] += size
        b["file_count"] += 1

    def _safe_size(p: Path) -> int:
        try:
            return p.stat().st_size
        except OSError:
            return 0

    if DATA_DIR.exists():
        for root, _dirs, files in os.walk(DATA_DIR):
            root_p = Path(root)
            rel = root_p.relative_to(DATA_DIR)
            for name in files:
                full = root_p / name
                size = _safe_size(full)
                top = rel.parts[0] if rel.parts else ""
                # System-log bucket is the aggregate of every
                # diagnostics artefact (the event log itself and full
                # traces). Rationale: these are the things Clear log
                # wipes; grouping them matches operator mental model.
                if top == "full_traces":
                    _bump("system_log", "System log", size)
                    continue
                if not rel.parts:
                    if name == "system_log.json":
                        _bump("system_log", "System log", size)
                        continue
                    m = _CASE_FILE_RE.match(name)
                    if m:
                        num = m.group(1)
                        _bump(f"case_{num}", f"I-{num}", size)
                        continue
                _bump("other", "Other", size)

    # Session state + config files live at the repo root, not inside
    # data/. They're tied to the server's operational life; bundle
    # them into the system_log bucket so the storage breakdown only
    # ever shows three conceptual groups: per-case data, system log,
    # and other.
    if STORAGE_SESSION_PATH.exists():
        _bump("system_log", "System log",
              _safe_size(STORAGE_SESSION_PATH))
    if CONFIG_PATH.exists():
        _bump("other", "Other", _safe_size(CONFIG_PATH))

    # Return every non-empty bucket. Filtering for display (hiding
    # the always-tiny `other` bucket) happens in the API handler.
    categories = [b for b in buckets.values() if b["bytes"] > 0]
    categories.sort(key=lambda c: c["bytes"], reverse=True)
    return categories


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
    """Return the system log + every persisted trace as a zip archive.

    Bundle contents:
      - `system_log.json`        : the full event log (pretty-printed)
      - `full_traces/<dir>/...`  : every saved pull trace, exactly as
                                   laid out on disk (trace.zip +
                                   mfa_trace/ per pull)

    This is the "send me everything you have for debugging" download
    — the companion to the Clear log button, which wipes the same
    set. Separate from `/api/export` (case snapshots) because these
    diagnostics contain email bodies, HTTP bodies, etc. that the
    operator may want to hand-review before sharing.
    """
    if load_redaction_enabled():
        return jsonify({"ok": False, "error": "redaction_enabled"}), 403
    entries = read_system_log()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S-UTC")
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. system log, pretty-printed at the root.
        zf.writestr("system_log.json", json.dumps(entries, indent=2))

        # 2. every persisted trace directory. Arcname is relative to
        #    DATA_DIR so the zip's tree is `full_traces/<dir>/...` —
        #    no leading `data/` prefix.
        traces_dir = (DATA_DIR / "full_traces").resolve()
        if traces_dir.exists():
            data_root = DATA_DIR.resolve()
            for entry in sorted(traces_dir.rglob("*")):
                if not entry.is_file():
                    continue
                rel = entry.relative_to(data_root)
                zf.write(entry, arcname=str(rel).replace("\\", "/"))

    buf.seek(0)
    filename = f"uscis-diagnostics-{stamp}.zip"
    return Response(
        buf.getvalue(),
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.route("/api/system-log/recompute", methods=["POST"])
def api_system_log_recompute():
    """Force a fresh chronological diff recompute across every configured case.

    Diffs are derived state — `day_changes()` runs live off the snapshot
    JSONs on each API call and nothing is cached server-side, so this
    endpoint never *needs* to run for correctness. What it provides is
    observability on demand: it re-walks the full diff feed for every
    case and appends a single `diff_recomputed` event (per-case change
    counts) to the system log, the same routine the server runs at
    startup. The operator can fire it after dropping in a backfilled
    snapshot and confirm at a glance that the data flowed through.

    Returns the per-case stats payload that was logged so the UI can
    surface a confirmation without re-scraping the log.
    """
    config = load_config()
    stats = _recompute_diffs_at_startup(config)
    return jsonify({"ok": "error" not in stats, "stats": stats})


@app.route("/api/system-log/clear", methods=["POST"])
def api_system_log_clear():
    """Wipe the system log AND every persisted trace.

    This is the "reset diagnostics" button. `data/full_traces/` is
    wiped alongside `data/system_log.json` so a clear produces a
    consistent blank slate — otherwise orphaned trace files would
    linger without any event pointing at them, inflating storage
    with unreferenced artefacts.

    Two-step confirmation is enforced client-side. Server-side we
    require `{"confirm": true}` so a stray curl / XSRF probe cannot
    clear anything by accident.
    """
    body = request.get_json(silent=True) or {}
    if body.get("confirm") is not True:
        return jsonify({"ok": False, "error": "confirmation_required"}), 400

    prior = len(read_system_log())
    errors: list[str] = []

    try:
        clear_system_log()
    except OSError as exc:  # pragma: no cover — filesystem should not fail
        return jsonify({"ok": False, "error": str(exc)}), 500

    traces_removed = _wipe_tree_contents(DATA_DIR / "full_traces", errors)

    sys_log(
        "system_log_cleared", source="server",
        prior_entry_count=prior,
        traces_removed=traces_removed,
        clear_errors=errors or None,
    )
    return jsonify({
        "ok": True,
        "priorEntryCount": prior,
        "tracesRemoved": traces_removed,
        "errors": errors,
    })


def _wipe_tree_contents(root: Path, errors: list[str]) -> int:
    """Delete every file and subdirectory inside `root` (but keep
    `root` itself). Returns the count of files removed.

    Errors are collected into the shared `errors` list rather than
    raised, so a permission glitch on one file doesn't abort the
    rest of the wipe. The directory itself is preserved so the
    next trace write doesn't race with mkdir on a lazy creator.
    """
    if not root.exists():
        return 0
    removed = 0
    try:
        for child in list(root.iterdir()):
            try:
                if child.is_dir():
                    for sub in list(child.rglob("*")):
                        if sub.is_file() or sub.is_symlink():
                            try:
                                sub.unlink()
                                removed += 1
                            except OSError as e:
                                errors.append(f"unlink {sub}: {e}")
                    # Remove directories bottom-up.
                    for sub in sorted(
                        child.rglob("*"),
                        key=lambda p: len(p.parts), reverse=True,
                    ):
                        if sub.is_dir():
                            try:
                                sub.rmdir()
                            except OSError as e:
                                errors.append(f"rmdir {sub}: {e}")
                    try:
                        child.rmdir()
                    except OSError as e:
                        errors.append(f"rmdir {child}: {e}")
                else:
                    try:
                        child.unlink()
                        removed += 1
                    except OSError as e:
                        errors.append(f"unlink {child}: {e}")
            except OSError as e:  # pragma: no cover
                errors.append(f"walk {child}: {e}")
    except OSError as e:  # pragma: no cover
        errors.append(f"iter {root}: {e}")
    return removed


@app.route("/api/redaction-mode", methods=["GET", "POST"])
def api_redaction_mode():
    """Read / toggle `redaction_enabled` in config.json.

    GET  → {"enabled": bool}
    POST {"enabled": bool} → persists the change to config.json.

    When enabled, the server masks PII in every JSON response (see the
    `_redact_json` after-request hook) and blocks the data exports, so the
    dashboard can be screenshotted or shared without private data leaving
    the process. It's a global, server-side switch — the masking is not
    recoverable client-side. Writes are atomic (tmp file + os.replace).
    """
    if request.method == "GET":
        return jsonify({"enabled": load_redaction_enabled()})

    body = request.get_json(silent=True) or {}
    desired = body.get("enabled")
    if not isinstance(desired, bool):
        return jsonify({"ok": False, "error": "enabled_must_be_bool"}), 400

    try:
        cfg = load_config()
    except Exception as e:  # pragma: no cover — defensive
        return jsonify({"ok": False, "error": f"config_load_failed: {e}"}), 500

    cfg["redaction_enabled"] = desired
    try:
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2))
        os.replace(tmp, CONFIG_PATH)
    except OSError as e:  # pragma: no cover — filesystem should not fail
        return jsonify({"ok": False, "error": f"config_write_failed: {e}"}), 500

    return jsonify({"ok": True, "enabled": desired})


@app.route("/api/debug-mode", methods=["GET", "POST"])
def api_debug_mode():
    """Read / toggle `trace_successful_pulls` in config.json.

    GET  → {"enabled": bool}
    POST {"enabled": bool} → persists the change to config.json.
    When enabled, the next pull writes the full trace (Playwright
    trace.zip + MFA sidecar) even on success, so an operator can
    verify the capture system against a healthy pull.

    Writes are atomic: config.json is rewritten in full via a tmp
    file + os.replace to avoid a half-written config during a crash.
    """
    if request.method == "GET":
        try:
            enabled = load_trace_successful_pulls()
        except ConfigError as e:
            return jsonify({"ok": False, "error": str(e)}), 500
        return jsonify({"enabled": enabled})

    body = request.get_json(silent=True) or {}
    desired = body.get("enabled")
    if not isinstance(desired, bool):
        return jsonify({"ok": False, "error": "enabled_must_be_bool"}), 400

    try:
        cfg = load_config()
    except Exception as e:  # pragma: no cover — defensive
        return jsonify({"ok": False, "error": f"config_load_failed: {e}"}), 500

    prior = cfg.get("trace_successful_pulls")
    cfg["trace_successful_pulls"] = desired

    try:
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cfg, indent=2))
        os.replace(tmp, CONFIG_PATH)
    except OSError as e:  # pragma: no cover — filesystem should not fail
        return jsonify({"ok": False, "error": f"config_write_failed: {e}"}), 500

    # The toggle is a lightweight UI action — no system-log entry.
    # The next pull will carry `trace_successful_pulls` through its
    # env already, so the effect is audible without a separate event.
    _ = prior  # silence unused; kept to prove atomic read/write above.
    return jsonify({"ok": True, "enabled": desired})


@app.route("/api/full-trace/<dir_name>/<path:subpath>")
def api_full_trace(dir_name: str, subpath: str):
    """Serve a single file from `data/full_traces/<dir>/<subpath>`.

    The subpath is allowed to contain one level of nesting so the
    `mfa_trace/events.jsonl` and `mfa_trace/email_<uid>.eml` files
    are reachable without a separate route. Path-traversal-safe:
    every component is restricted to `[A-Za-z0-9._-]`, and the
    resolved target must resolve inside the traces directory.

    CORS allow-* is enabled so trace.playwright.dev can fetch
    trace.zip cross-origin when the operator drops the URL into the
    viewer. The only files served are sandboxed under full_traces/,
    so this doesn't expand the app's attack surface.
    """
    if not _is_safe_name_part(dir_name):
        return jsonify({"ok": False, "error": "invalid_dir"}), 400
    parts = subpath.split("/")
    if len(parts) > 2:
        return jsonify({"ok": False, "error": "invalid_path_depth"}), 400
    for part in parts:
        if not _is_safe_name_part(part):
            return jsonify({"ok": False, "error": "invalid_component"}), 400

    base = (DATA_DIR / "full_traces").resolve()
    try:
        target = (base / dir_name / subpath).resolve()
    except Exception:
        return jsonify({"ok": False, "error": "resolve_failed"}), 400
    if base not in target.parents and target != base:
        return jsonify({"ok": False, "error": "path_escape"}), 400
    if not target.is_file():
        return jsonify({"ok": False, "error": "not_found"}), 404

    leaf = target.name
    if leaf == "trace.zip":
        resp = Response(target.read_bytes(), mimetype="application/zip")
    elif leaf.endswith(".jsonl"):
        resp = Response(target.read_bytes(), mimetype="application/x-ndjson")
    elif leaf.endswith(".eml"):
        resp = Response(target.read_bytes(), mimetype="message/rfc822")
    elif leaf.endswith(".json"):
        resp = Response(target.read_bytes(), mimetype="application/json")
    elif leaf.endswith(".png"):
        resp = Response(target.read_bytes(), mimetype="image/png")
    else:
        resp = Response(target.read_bytes(),
                        mimetype="application/octet-stream")
    # trace.playwright.dev fetches the zip cross-origin; without CORS
    # the fetch is blocked and the viewer shows an empty timeline.
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


# Allowed chars for trace-dir / file-name URL components. Anything
# outside this set is rejected before the path is resolved.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _is_safe_name_part(name: str) -> bool:
    return bool(name) and bool(_SAFE_NAME_RE.match(name)) and ".." not in name


# Playwright ships its trace viewer as a static Vite bundle inside the
# installed Python package. Serving it from our origin means the
# viewer can fetch `trace.zip` without CORS/mixed-content issues, AND
# we don't depend on trace.playwright.dev being reachable from the
# VM's network (corporate firewall, air-gapped deploys, etc).
_PW_VIEWER_DIR = (
    Path(_pw_module.__file__).parent
    / "driver" / "package" / "lib" / "vite" / "traceViewer"
)


@app.route("/trace-viewer/")
@app.route("/trace-viewer/<path:filename>")
def trace_viewer(filename: str = "index.html"):
    """Serve the Playwright trace viewer (self-hosted).

    The viewer reads its target trace from a `?trace=<url>` query
    param. The frontend constructs that URL against our own
    `/api/full-trace/<dir>/trace.zip` route so the fetch stays on
    the same origin — no CORS, no mixed-content blocking, works
    identically on localhost and HTTPS production.
    """
    # Guard: only allow files that actually exist in the viewer dir.
    if ".." in filename:
        return jsonify({"ok": False, "error": "invalid_path"}), 400
    return send_from_directory(_PW_VIEWER_DIR, filename)


@app.route("/api/mfa-trace/<dir_name>/summary")
def api_mfa_trace_summary(dir_name: str):
    """Summarise a trace's MFA artefacts for the pop-out viewer.

    Returns:
      {
        "events":  [ {ts, event, cycle, ...extras}, ... ],
        "emails":  [ {uid, subject, from, date, size, preview}, ... ],
      }

    Bodies of emails are NOT returned here — use
    `/api/mfa-trace/<dir>/email/<uid>` to fetch a specific one on
    demand. Keeps the summary cheap to paint even for busy pulls
    with many cycles.
    """
    if not _is_safe_name_part(dir_name):
        return jsonify({"ok": False, "error": "invalid_dir"}), 400
    base = (DATA_DIR / "full_traces" / dir_name / "mfa_trace").resolve()
    parent = (DATA_DIR / "full_traces" / dir_name).resolve()
    if not parent.is_dir():
        return jsonify({"ok": False, "error": "trace_not_found"}), 404
    if not base.is_dir():
        return jsonify({"events": [], "emails": []})

    events: list[dict] = []
    events_path = base / "events.jsonl"
    if events_path.is_file():
        try:
            for line in events_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError as e:
            return jsonify({"ok": False, "error": f"read: {e}"}), 500

    emails: list[dict] = []
    import email as _email_lib
    for eml in sorted(base.glob("email_*.eml")):
        uid = eml.stem[len("email_"):]
        try:
            raw = eml.read_bytes()
            msg = _email_lib.message_from_bytes(raw)
            body = _extract_plain_body(msg)
            emails.append({
                "uid": uid,
                "subject": (msg["Subject"] or "")[:300],
                "from": (msg["From"] or "")[:200],
                "date": msg["Date"] or "",
                "size": len(raw),
                "preview": body[:280],
            })
        except Exception as e:  # pragma: no cover — best-effort
            emails.append({
                "uid": uid,
                "subject": "", "from": "", "date": "",
                "size": eml.stat().st_size,
                "preview": f"<parse error: {type(e).__name__}>",
            })

    return jsonify({"events": events, "emails": emails})


@app.route("/api/mfa-trace/<dir_name>/email/<uid>")
def api_mfa_trace_email(dir_name: str, uid: str):
    """Return headers + both renderings of one archived email.

    Response shape:
      {
        "headers": {"subject", "from", "to", "date"},
        "text":    <plain-text body, always present — may be empty>,
        "html":    <HTML body if the message had a text/html part,
                   else null>,
      }
    """
    if not _is_safe_name_part(dir_name) or not _is_safe_name_part(uid):
        return jsonify({"ok": False, "error": "invalid_path"}), 400
    path = (DATA_DIR / "full_traces" / dir_name / "mfa_trace" / f"email_{uid}.eml")
    if not path.is_file():
        return jsonify({"ok": False, "error": "not_found"}), 404
    import email as _email_lib
    try:
        raw = path.read_bytes()
        msg = _email_lib.message_from_bytes(raw)
    except Exception as e:
        return jsonify({"ok": False, "error": f"parse: {e}"}), 500
    return jsonify({
        "headers": {
            "subject": msg["Subject"] or "",
            "from": msg["From"] or "",
            "to": msg["To"] or "",
            "date": msg["Date"] or "",
        },
        "html": _extract_email_part(msg, "text/html"),
        # Raw RFC822 source — the thing you'd grep to build a new
        # regex against a redesigned template. Decoded best-effort;
        # replacement characters for any undecodable bytes.
        "raw": raw.decode("utf-8", errors="replace"),
    })


def _extract_plain_body(msg) -> str:
    """Summary-friendly plain-text body. Used by the emails-list
    `preview` field in /api/mfa-trace/<dir>/summary — prefers the
    text/plain part, falls back to tag-stripped HTML, else empty."""
    text = _extract_email_part(msg, "text/plain")
    if text:
        return text
    html = _extract_email_part(msg, "text/html")
    if html:
        return re.sub(r"<[^>]+>", " ", html)
    return ""


def _extract_email_part(msg, want_ctype: str) -> str | None:
    """Return the decoded body of the first part matching `want_ctype`
    (e.g. "text/plain" or "text/html"), or None if not present."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() != want_ctype:
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="replace")
            except Exception:
                return payload.decode("utf-8", errors="replace")
        return None
    # Single-part — serve only if its content-type matches.
    if msg.get_content_type() != want_ctype:
        return None
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except Exception:
        return payload.decode("utf-8", errors="replace")


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
    # Piggy-back build-version metadata on the status poll so the UI
    # always has an up-to-date chip without a second request. The
    # dedicated `/api/version` endpoint below is for ad-hoc scripts.
    state["version"] = VERSION
    return jsonify(state)


@app.route("/api/version")
def api_version():
    """Return build-version metadata for the currently running code.

    Shape: { sha, full_sha, commit_date, boot_time }. Resolved once at
    import time via `git rev-parse` (or `.version` fallback). Useful for
    deploy-verification scripts that want to confirm a specific SHA is
    live without having to SSH into the VM.
    """
    return jsonify(VERSION)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _recompute_diffs_at_startup(config: dict) -> dict:
    """Walk every configured case's snapshot history, recompute the diff feed,
    and emit a `diff_recomputed` system-log event with per-case counts.

    Rationale
    ---------
    Diffs are *derived* state: `day_changes()` runs live off the snapshot
    JSONs on every API call and nothing is cached server-side. So adding
    or replacing a snapshot file by hand is automatically picked up by
    the next page load — no restart is required for correctness.

    What an operator-driven restart *does* need is **observability**:
    proof that the new data made it through the pipeline. This helper
    walks the diff feed for every configured case and emits a single
    `diff_recomputed` event carrying the per-case change counts. The
    operator can open the system log after a restart and confirm at a
    glance that the recompute happened against the current on-disk data.

    Failure is non-fatal — the live API path still works (and will
    surface the same error there). We emit `diff_recompute_failed` and
    return an empty payload.

    Returns
    -------
    The event payload that was logged (excluding the wrapper keys) so
    tests can assert on the per-case summary without scraping the log.
    """
    try:
        cases_summary = []
        for c in (config.get("cases") or []):
            label = c.get("label")
            if not label:
                continue
            case_changes = day_changes(load_case_entries(label))
            loc_changes = location_day_changes(load_location_entries(label))
            cases_summary.append({
                "label": label,
                "case_changes": len(case_changes),
                "location_changes": len(loc_changes),
            })
        sys_log("diff_recomputed", source="server", cases=cases_summary)
        logger.info(
            "Diff recomputed: %s",
            ", ".join(
                f"{c['label']}=case:{c['case_changes']}+loc:{c['location_changes']}"
                for c in cases_summary
            ) or "(no cases configured)",
        )
        return {"cases": cases_summary}
    except Exception as e:
        sys_log(
            "diff_recompute_failed",
            level="warning", source="server",
            error=f"{type(e).__name__}: {e}"[:200],
        )
        logger.exception("Diff recompute failed; live API will retry per request.")
        return {"cases": [], "error": f"{type(e).__name__}: {e}"}


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Install the access gate before the scheduler / blueprint work —
    # pulls auth.optional_access_code out of config.json; no-op if it's empty.
    config: dict = {}
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

    # Resolve the REQUIRED pull schedule from config.json into the
    # module-global PULL_HOURS *before* the scheduler reads it. There is
    # no default cadence — a missing/invalid `pull_hours` leaves PULL_HOURS
    # empty, which the scheduler block below treats as "no automatic pulls"
    # and logs loudly. The dashboard still serves (manual pull button works)
    # so the operator can see the error and fix config.json without the
    # whole process crash-looping under systemd Restart=always.
    global PULL_HOURS
    try:
        PULL_HOURS = load_pull_hours(config)
    except ConfigError as e:
        sys_log(
            "config_pull_hours_invalid", level="error", source="server",
            error=str(e)[:200],
        )
        logger.error("Invalid/missing config.pull_hours (%s); no automatic "
                     "pulls will be scheduled until config.json is fixed.", e)
        PULL_HOURS = ()

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

    # Recompute the diff feed eagerly so an operator dropping a backfilled
    # snapshot in by hand can confirm via the system log that the restart
    # re-read the data. The live API path is already cache-free, so this
    # is observability rather than a correctness requirement — see the
    # helper docstring for details.
    _recompute_diffs_at_startup(config)

    # Don't use reloader — it spawns two processes and double-schedules jobs.
    # Bind to all interfaces — production access is gated by the optional
    # access-code middleware (see access_gate.py) when auth.optional_access_code is set.
    try:
        app.run(host="127.0.0.1", port=int(os.environ.get("USCIS_PORT", "8080")), debug=False, use_reloader=False)
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
