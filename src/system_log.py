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
import threading
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = _ROOT / "data" / "system_log.json"
MAX_ENTRIES = 5000

_lock = threading.Lock()
_logger = logging.getLogger(__name__)


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

    try:
        with _lock:
            entries = _read_file()
            entries.append(entry)
            if len(entries) > MAX_ENTRIES:
                entries = entries[-MAX_ENTRIES:]
            _write_file(entries)
    except Exception:  # pragma: no cover — belt-and-suspenders
        _logger.exception("system_log.log() failed for event=%s", event)

    # Also echo to the Python logger so events land in journalctl / stderr
    # for operators tailing the service log.
    py_level = {
        "warning": logging.WARNING,
        "warn": logging.WARNING,
        "error": logging.ERROR,
    }.get(level, logging.INFO)
    _logger.log(py_level, "[systemlog] event=%s %s", event, {k: v for k, v in entry.items() if k not in ("ts", "pid")})


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
