#!/usr/bin/env python3
# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""USCIS Case Snapshot — orchestrator.

Splits login and API extraction into two isolated modules:
  - `uscis_auth` owns the OpenID Connect/MFA flow and is the ONLY path that burns MFA codes.
  - `uscis_api`  owns the case-service API and never initiates a login.

The session persists in `.uscis_session.json` (gitignored). A typical run:

  1. Launch Chromium with the saved session.
  2. Probe the API first. If it works, we never enter the login flow.
  3. If the API is rejecting the session, re-authenticate exactly once and retry.

Daily snapshots are appended to `data/{formNum}_logs.json` as
`{ "capturedAt": "YYYY-MM-DDTHH:MM:SSZ", "data": <full API response> }`.
Each run's timestamp is second-precision ISO-8601 UTC; the diff engine
slices to the date when it needs day-level grouping.

Subcommands:
  run             default: probe → (maybe login) → fetch all cases (same as `fetch`).
  fetch           same as `run`.
  login           force a full login now (burns an MFA code).
  extract         fetch cases using the persisted session ONLY; never logs in.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import (
    Error as PlaywrightError,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)

from uscis_api import (
    ApiError,
    SessionExpired,
    fetch_case,
    fetch_case_in_new_tab,
    open_worker_tab,
)
from uscis_auth import (
    AuthError,
    STORAGE_STATE_PATH,
    ensure_authenticated,
)
from system_log import log as sys_log

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.json"
DATA_DIR = ROOT / "data"

_FORM_NUM_RE = re.compile(r"I-?(\d+)")

REQUIRED_AUTH_KEYS = (
    "uscis_email",
    "uscis_password",
    "uscis_mfa_email",
    "uscis_mfa_app_password",
)

logger = logging.getLogger("session_fetch")


def _now_iso_utc() -> str:
    """Current moment as a second-precision ISO-8601 UTC string (e.g. 2026-04-18T22:38:24Z)."""
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def load_config() -> dict:
    """Load config.json.  Emits a sys_log event on any failure so the
    caller has a categorised record even if the process dies before
    the calling command gets to print anything."""
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except FileNotFoundError as e:
        sys_log(
            "config_load_failed", level="error", source="session_fetch",
            reason="file_not_found", path=str(CONFIG_PATH),
            error=f"{type(e).__name__}: {e}"[:200],
        )
        raise
    except json.JSONDecodeError as e:
        sys_log(
            "config_load_failed", level="error", source="session_fetch",
            reason="malformed_json", path=str(CONFIG_PATH),
            error=f"{type(e).__name__}: {e}"[:200],
        )
        raise


def load_auth(
    config: dict | None = None,
    *,
    required: tuple[str, ...] = REQUIRED_AUTH_KEYS,
) -> dict[str, str]:
    """Pull the `auth` section from config.json and validate required keys."""
    config = config if config is not None else load_config()
    auth = config.get("auth") or {}
    missing = [k for k in required if not auth.get(k)]
    if missing:
        sys_log(
            "auth_config_missing_keys", level="error", source="session_fetch",
            missing=missing,
        )
        raise SystemExit(
            f"Missing auth keys in {CONFIG_PATH}: {', '.join(missing)}\n"
            f"See config.example.json for the expected shape."
        )
    return {k: auth[k] for k in required}


def log_file_for(form_type: str) -> Path:
    m = _FORM_NUM_RE.search(form_type or "")
    if not m:
        raise ValueError(f"Unrecognized form type: {form_type!r}")
    return DATA_DIR / f"{m.group(1)}_logs.json"


def append_snapshot(form_type: str, data: dict, captured_at: str) -> Path:
    """Append a snapshot entry. Never replaces — multiple runs per day each
    get their own row keyed by the full ISO-8601 timestamp in `capturedAt`.

    Emits sys_log events when an existing log file is malformed, so a
    silent "start fresh" never happens without a trace.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = log_file_for(form_type)

    logs: list[dict] = []
    if path.exists():
        try:
            with open(path) as f:
                existing = json.load(f)
                if isinstance(existing, list):
                    logs = existing
                else:
                    logger.warning(
                        "%s was not a JSON array — starting fresh.", path.name
                    )
                    sys_log(
                        "snapshot_log_not_array", level="warning",
                        source="session_fetch",
                        file=path.name,
                        existing_type=type(existing).__name__,
                    )
        except json.JSONDecodeError as e:
            logger.warning("%s was not valid JSON — starting fresh.", path.name)
            sys_log(
                "snapshot_log_invalid_json", level="warning",
                source="session_fetch",
                file=path.name,
                error=f"{type(e).__name__}: {e}"[:200],
            )

    logs.append({"capturedAt": captured_at, "data": data})

    with open(path, "w") as f:
        json.dump(logs, f, indent=2)
    return path


def _extract_cases(
    context, cases: list[dict], captured_at: str, *, keep_alive: bool = False
) -> int:
    """Iterate cases and append each API response to its log file.

    Default mode: one reused worker tab. Fastest, no tab flicker.

    keep_alive mode: open a fresh tab per case and render the JSON response
    inline in each tab. No tabs are closed — the caller keeps the browser
    alive for visual inspection.

    Returns the number of failures. Raises SessionExpired if the session
    dies mid-run so callers can re-auth and retry.
    """
    probe_tab = open_worker_tab(context) if not keep_alive else None

    failures = 0
    for case in cases:
        receipt = case["id"]
        label = case.get("label") or case.get("type") or ""
        logger.info("Fetching %s (%s)...", label or "?", receipt)
        sys_log("case_fetch_start", source="session_fetch",
                label=label or "?", receipt=receipt)
        try:
            if keep_alive:
                _tab, data = fetch_case_in_new_tab(
                    context, receipt, label or "?"
                )
            else:
                data = fetch_case(probe_tab, receipt)
        except SessionExpired:
            sys_log("case_fetch_session_expired", level="warning",
                    source="session_fetch", label=label or "?", receipt=receipt)
            raise
        except ApiError as e:
            logger.error("  ✗ %s", e)
            failures += 1
            sys_log("case_fetch_api_error", level="error",
                    source="session_fetch", label=label or "?",
                    receipt=receipt, status=getattr(e, "status", None),
                    error=str(e))
            continue

        form_type = (
            (data.get("data") or data).get("formType")
            if isinstance(data, dict) else None
        ) or label
        payload = (
            data.get("data") if isinstance(data, dict) and "data" in data else data
        )

        try:
            path = append_snapshot(form_type, payload, captured_at)
        except ValueError as e:
            logger.error("  ✗ cannot determine log file: %s", e)
            failures += 1
            sys_log("snapshot_append_failed", level="error",
                    source="session_fetch", label=label or "?",
                    receipt=receipt, error=str(e))
            continue
        logger.info("  → %s", path.relative_to(ROOT))
        sys_log("snapshot_appended", source="session_fetch",
                label=label or "?", receipt=receipt,
                form_type=form_type, file=path.name)

    return failures


def _hold_browser_open(args) -> None:
    """Pause before closing so the user can inspect the browser.

    - With --keep-alive: block until SIGINT/SIGTERM (works when detached).
    - Otherwise, in headful mode with a TTY: wait for Enter.
    """
    want_hold = getattr(args, "keep_alive", False) or not args.headless
    if not want_hold:
        return

    interactive = sys.stdin.isatty()
    if interactive and not getattr(args, "keep_alive", False):
        try:
            input("\nBrowser is held open. Press Enter to close... ")
        except (EOFError, KeyboardInterrupt):
            pass
        return

    # No TTY (or --keep-alive explicitly requested): wait on a signal.
    import signal

    logger.info(
        "Browser is held open. Send SIGINT/SIGTERM (e.g. kill %d) to close.",
        os.getpid(),
    )
    stop = {"fired": False}

    def _handler(signum, frame):
        stop["fired"] = True

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)
    while not stop["fired"]:
        try:
            signal.pause()
        except KeyboardInterrupt:
            break


def cmd_run(args) -> int:
    """Probe → (maybe login) → extract."""
    config = load_config()
    auth = load_auth(config)
    cases = config.get("cases", [])
    if not cases:
        logger.error("No cases in %s", CONFIG_PATH)
        sys_log("cli_run_no_cases", level="error", source="session_fetch")
        return 1

    captured_at = _now_iso_utc()
    logger.info("USCIS Case Snapshot — %s (%d cases)", captured_at, len(cases))
    sys_log("cli_run_start", source="session_fetch",
            case_count=len(cases), captured_at=captured_at,
            headless=args.headless, keep_alive=args.keep_alive,
            storage_state_exists=STORAGE_STATE_PATH.exists())

    failures = 0
    with sync_playwright() as pw:
        # Browser + context + page construction is a distinct failure
        # surface (chromium binary missing, corrupt storage_state, etc.)
        # that must not silently manifest as a top-level traceback.
        try:
            browser = pw.chromium.launch(headless=args.headless)
        except Exception as e:
            sys_log(
                "browser_launch_failed", level="error",
                source="session_fetch",
                error=f"{type(e).__name__}: {e}"[:200],
                headless=args.headless,
            )
            raise

        try:
            context = browser.new_context(
                storage_state=str(STORAGE_STATE_PATH)
                if STORAGE_STATE_PATH.exists()
                else None
            )
            page = context.new_page()
        except Exception as e:
            sys_log(
                "browser_context_failed", level="error",
                source="session_fetch",
                error=f"{type(e).__name__}: {e}"[:200],
                storage_state_exists=STORAGE_STATE_PATH.exists(),
            )
            browser.close()
            raise

        try:
            try:
                ensure_authenticated(context, page, auth, allow_login=True)
            except AuthError as e:
                # ensure_authenticated already emitted an
                # `auth_ensure_result` event with snapshot — re-emit a
                # cli-level marker so the run-level event is easy to
                # correlate in the dashboard timeline.
                sys_log(
                    "cli_run_auth_failed", level="error",
                    source="session_fetch",
                    error=f"AuthError: {e}"[:200],
                )
                raise

            try:
                failures = _extract_cases(
                    context, cases, captured_at, keep_alive=args.keep_alive
                )
            except SessionExpired as e:
                logger.warning("Session expired mid-run — re-authenticating once.")
                sys_log(
                    "cli_run_session_expired_retry", level="warning",
                    source="session_fetch",
                    receipt=getattr(e, "receipt", None),
                )
                try:
                    ensure_authenticated(context, page, auth, allow_login=True)
                except AuthError as ae:
                    sys_log(
                        "cli_run_auth_failed", level="error",
                        source="session_fetch",
                        phase="post_session_expired_retry",
                        error=f"AuthError: {ae}"[:200],
                    )
                    raise
                try:
                    failures = _extract_cases(
                        context, cases, captured_at, keep_alive=args.keep_alive
                    )
                except SessionExpired as e2:
                    # Session died AGAIN after a fresh login — give up.
                    sys_log(
                        "cli_run_session_expired_twice", level="error",
                        source="session_fetch",
                        receipt=getattr(e2, "receipt", None),
                    )
                    raise
        finally:
            # Persist whatever session state we ended up with. Failures
            # here are non-fatal but must be visible so we don't lose a
            # working session silently.
            try:
                context.storage_state(path=str(STORAGE_STATE_PATH))
            except Exception as e:
                sys_log(
                    "storage_state_persist_failed", level="warning",
                    source="session_fetch",
                    error=f"{type(e).__name__}: {e}"[:200],
                )
            _hold_browser_open(args)
            try:
                browser.close()
            except Exception as e:  # pragma: no cover — teardown best-effort
                sys_log(
                    "browser_close_failed", level="warning",
                    source="session_fetch",
                    error=f"{type(e).__name__}: {e}"[:200],
                )

    if failures:
        logger.warning("Completed with %d failure(s).", failures)
        sys_log("cli_run_finished", level="warning", source="session_fetch",
                case_count=len(cases), failures=failures)
        return 2
    logger.info("Done.")
    sys_log("cli_run_finished", source="session_fetch",
            case_count=len(cases), failures=0)
    return 0


def cmd_login(args) -> int:
    """Force a full login now and persist the session."""
    auth = load_auth()
    rc = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        context = browser.new_context(
            storage_state=str(STORAGE_STATE_PATH)
            if STORAGE_STATE_PATH.exists()
            else None
        )
        page = context.new_page()
        try:
            ensure_authenticated(context, page, auth, allow_login=True)
            logger.info("Login OK.")
        except AuthError as e:
            logger.error("Login failed: %s", e)
            rc = 1
        finally:
            _hold_browser_open(args)
            browser.close()
    return rc


def cmd_extract(args) -> int:
    """Run the API fetch using the persisted session only. Never logs in."""
    config = load_config()
    # Extract-only mode passes allow_login=False, so `ensure_authenticated`
    # never reaches a code path that reads credentials. An empty dict is
    # therefore safe — we skip `load_auth()` to avoid requiring creds in
    # `config.json` just to re-use a saved session.
    auth: dict[str, str] = {}
    cases = config.get("cases", [])
    if not cases:
        logger.error("No cases in %s", CONFIG_PATH)
        return 1
    if not STORAGE_STATE_PATH.exists():
        logger.error(
            "No saved session at %s. Run `python src/session_fetch.py login` first.",
            STORAGE_STATE_PATH.name,
        )
        return 1

    captured_at = _now_iso_utc()
    logger.info("USCIS Case Extract — %s (%d cases)", captured_at, len(cases))

    failures = 0
    rc = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        context = browser.new_context(storage_state=str(STORAGE_STATE_PATH))
        page = context.new_page()
        try:
            # Raises AuthError if session is stale — we do NOT log in here.
            ensure_authenticated(context, page, auth, allow_login=False)
            try:
                failures = _extract_cases(
                    context, cases, captured_at, keep_alive=args.keep_alive
                )
            except SessionExpired:
                logger.error(
                    "Session expired. Re-run `python src/session_fetch.py login`."
                )
                rc = 1
        except AuthError as e:
            logger.error("%s", e)
            rc = 1
        finally:
            try:
                context.storage_state(path=str(STORAGE_STATE_PATH))
            except Exception:
                pass
            _hold_browser_open(args)
            browser.close()

    if rc:
        return rc
    return 2 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--headless",
        dest="headless",
        action="store_true",
        default=True,
    )
    parser.add_argument(
        "--headful", dest="headless", action="store_false",
    )
    parser.add_argument(
        "--keep-alive",
        dest="keep_alive",
        action="store_true",
        default=False,
        help=(
            "Open a dedicated tab per case, render the JSON response inline, "
            "and keep every tab open. Pauses until you press Enter so you can "
            "inspect each tab manually. Implies --headful."
        ),
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )

    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("run", help="Probe → (maybe login) → fetch (default).")
    sub.add_parser("fetch", help="Alias for `run`.")
    sub.add_parser("login", help="Force login now. Burns one MFA code.")
    sub.add_parser(
        "extract",
        help="Fetch using the persisted session only; refuses to log in.",
    )

    args = parser.parse_args()

    # --keep-alive only makes sense with a visible browser
    if args.keep_alive:
        args.headless = False

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cmd = args.cmd or "run"
    # Wrap every subcommand in a last-resort handler so an uncaught
    # exception anywhere in the whole process still produces a
    # categorised `cli_uncaught_exception` system-log event.  Without
    # this, the only surviving record of a surprise crash is a stderr
    # traceback captured by the parent server process — useful, but
    # not dashboard-visible and not correlatable to a specific run.
    try:
        if cmd in ("run", "fetch"):
            return cmd_run(args)
        if cmd == "login":
            return cmd_login(args)
        if cmd == "extract":
            return cmd_extract(args)
        parser.error(f"unknown command: {cmd}")  # pragma: no cover — argparse blocks unknowns
        return 2  # pragma: no cover
    except SystemExit:
        # Propagate argparse / load_auth SystemExit untouched.
        raise
    except BaseException as e:
        tb_tail = "".join(traceback.format_exception(e))[-1200:]
        sys_log(
            "cli_uncaught_exception", level="error", source="session_fetch",
            cmd=cmd,
            error=f"{type(e).__name__}: {e}"[:200],
            traceback_tail=tb_tail,
        )
        raise


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
