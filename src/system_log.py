# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Append-only structured log of what the system did at what time.

Intended for backtracing and debugging when something goes wrong (e.g.
a scheduled pull failed silently before writing a snapshot). Writes a
JSON array to `data/system_log.json` — cheap to produce, easy to
visualise in the dashboard, self-contained (ships inside the export
zip).

Usage
-----
    from system_log import log

    log("pull_started", source="scheduler", case_count=3)
    log("pull_failed", level="error", exit_code=1, duration_seconds=0.2)

Entry schema
------------
    {
      "ts":      "2026-04-19T11:00:00Z",   # ISO-8601 UTC, second precision
      "event":   "pull_started",           # short stable identifier
      "level":   "info" | "warning" | "error",
      "pid":     12345,                    # process that emitted it
      "source":  "server" | "session_fetch" | ...   # optional
      # plus any detail fields the caller passed.
    }

Rotation
--------
Capped at MAX_ENTRIES. Oldest entries are dropped first so the file
never grows without bound.

Reliability
-----------
Writes are atomic (temp-file + os.replace). Read/parse failures fall
back to an empty list so a corrupted log never breaks the caller.
The `log()` function never raises — a logging bug must never be able
to take down the process it was instrumenting.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = _ROOT / "data" / "system_log.json"
MAX_ENTRIES = 5000

# When set to "1", `log()` emits one JSON line per event to stderr *instead*
# of appending to LOG_PATH. This lets a parent process (e.g. server.py's
# _run_pull_subprocess) capture a child's events as they happen and bubble
# them up into a single consolidated parent entry — so one `pull` operation
# produces ONE row in the dashboard's System log, with its 15+ internal
# auth / fetch / snapshot events attached as sub-steps, instead of 15+
# separate flat rows.
JSONL_STDERR_ENV = "USCIS_LOG_JSONL_STDERR"

_lock = threading.Lock()
_logger = logging.getLogger(__name__)

# Thread-local capture stack. When a thread has one or more active buffers
# on its stack, `log()` appends to the innermost buffer instead of writing
# to disk. Used by `_run_pull_subprocess` to fold server-process events
# (smtp_*, pull_pre_snapshot_failed, etc.) into the consolidated `pull`
# envelope. Separate threads (Flask request workers) are unaffected — they
# each have their own `_thread_local.capture_stack`.
_thread_local = threading.local()


def _current_capture_buffer() -> list[dict] | None:
    """Return the top buffer on this thread's capture stack, or None.

    None means "no active capture; write to disk as normal."
    """
    stack = getattr(_thread_local, "capture_stack", None)
    if stack:
        return stack[-1]
    return None


def push_capture() -> list[dict]:
    """Start capturing `log()` calls on this thread. Returns the buffer.

    Pair with exactly one `pop_capture()`. Nested pushes are allowed —
    the innermost buffer wins. Typical use in a try/finally:

        buf = push_capture()
        try:
            ...do work that may emit sys_log events...
        finally:
            pop_capture()

    Events appended to the buffer preserve the exact dict `log()` would
    have written to disk, so callers can embed them as `steps[]` of a
    parent envelope verbatim.
    """
    buf: list[dict] = []
    stack = getattr(_thread_local, "capture_stack", None)
    if stack is None:
        stack = []
        _thread_local.capture_stack = stack
    stack.append(buf)
    return buf


def pop_capture() -> list[dict]:
    """Stop the innermost capture on this thread and return its buffer."""
    stack = getattr(_thread_local, "capture_stack", None)
    if not stack:
        return []
    return stack.pop()


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def log(event: str, *, level: str = "info", source: str | None = None, **details) -> None:
    """Append one event entry to the system log.

    Never raises — logging failures are swallowed (reported via the
    Python logger) so that log-write bugs don't break the caller.
    """
    entry: dict = {
        "ts": _now_iso(),
        "event": event,
        "level": level,
        "pid": os.getpid(),
    }
    if source:
        entry["source"] = source
    # Merge caller-supplied details. Any non-JSON-serialisable value is
    # coerced to its repr so we never crash on exotic inputs.
    for k, v in details.items():
        try:
            json.dumps(v)
            entry[k] = v
        except (TypeError, ValueError):
            entry[k] = repr(v)

    if os.environ.get(JSONL_STDERR_ENV) == "1":
        # JSONL-stderr mode: the parent process will collect these lines
        # and fold them into a consolidated entry. Do NOT write to disk
        # ourselves; the parent owns the persistence.
        try:
            sys.stderr.write(_JSONL_PREFIX + json.dumps(entry) + "\n")
            sys.stderr.flush()
        except Exception:  # pragma: no cover — stderr failures are fatal anyway
            _logger.exception("system_log jsonl-stderr emit failed for event=%s", event)
        return

    buf = _current_capture_buffer()
    if buf is not None:
        # This thread has an active capture (e.g. the pull runner has set
        # one up so server-process events fold into the pull envelope).
        # Append to the buffer INSTEAD of writing to disk so we don't
        # double-record the event once as a flat row and once as a step.
        buf.append(entry)
        _echo_to_python_logger(entry)
        return

    try:
        with _lock:
            entries = _read_file()
            entries.append(entry)
            if len(entries) > MAX_ENTRIES:
                entries = entries[-MAX_ENTRIES:]
            _write_file(entries)
    except Exception:  # pragma: no cover — belt-and-suspenders
        _logger.exception("system_log.log() failed for event=%s", event)

    _echo_to_python_logger(entry)


def _echo_to_python_logger(entry: dict) -> None:
    """Mirror a structured event to the Python logger so operators tailing
    journalctl / stderr see the same thing the dashboard sees."""
    py_level = {
        "warning": logging.WARNING,
        "warn": logging.WARNING,
        "error": logging.ERROR,
    }.get(entry.get("level", "info"), logging.INFO)
    _logger.log(
        py_level,
        "[systemlog] event=%s %s",
        entry.get("event"),
        {k: v for k, v in entry.items() if k not in ("ts", "pid")},
    )


# Magic prefix on stderr lines emitted by child processes, so the parent
# can parse its subprocess's stderr without mistaking `logging` output
# (which is interleaved) for event JSON. Every JSONL event starts with
# this prefix; non-matching lines are treated as opaque log text.
_JSONL_PREFIX = "@@SYSLOG_EVT@@ "


def parse_jsonl_stderr_line(line: str) -> dict | None:
    """Parse a stderr line emitted by a child running in JSONL mode.

    Returns the event dict if the line is a well-formed event, else None.
    Used by the parent (`server._run_pull_subprocess`) to turn a subprocess
    stderr stream into `steps[]` for the consolidated pull entry.
    """
    if not line.startswith(_JSONL_PREFIX):
        return None
    try:
        obj = json.loads(line[len(_JSONL_PREFIX):])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def read_all(limit: int | None = None) -> list[dict]:
    """Return the current log contents (list of entries, oldest-first)."""
    with _lock:
        entries = _read_file()
    if limit is not None:
        return entries[-limit:]
    return entries


def count() -> int:
    """Total number of entries currently stored. Cheaper than read_all when
    the caller only needs the count (e.g. to surface "total vs shown" in
    the dashboard badge without double-reading a limited slice)."""
    with _lock:
        return len(_read_file())


def clear() -> None:
    """Wipe the log file. Irreversible — used both by tests and by the
    operator-triggered `POST /api/system-log/clear` endpoint."""
    with _lock:
        if LOG_PATH.exists():
            LOG_PATH.unlink()


# ---------------------------------------------------------------------------
# Internal IO
# ---------------------------------------------------------------------------

def _read_file() -> list[dict]:
    if not LOG_PATH.exists():
        return []
    try:
        with open(LOG_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _write_file(entries: list[dict]) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to a sibling temp file, then rename. A crash
    # mid-write leaves the original log intact.
    tmp = LOG_PATH.with_suffix(LOG_PATH.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
    os.replace(tmp, LOG_PATH)
