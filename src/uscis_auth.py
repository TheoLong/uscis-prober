# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""USCIS login flow — isolated from fetching.

Only call `ensure_authenticated` when you actually need to log in. It is the
*only* function in the project that will submit credentials or trigger an
MFA email. All other code paths must treat an invalid session as an error.

Session policy: `cmd_run` (the pull runner) wipes `.uscis_session.json`
before every pull and never persists it at the end, so every scheduled
or manual pull exercises the full OIDC + MFA flow from zero. The CLI-
only `cmd_login` and `cmd_extract` subcommands still use the file as
a debug/inspection aid (login once, inspect repeatedly).

Diagnostics
-----------
The entire login path is the second-biggest silent-failure surface in
the tracker (first being the MFA mailbox — see `mfa_mailbox.py`).
Every function here emits exactly one terminal sys_log event when it
finishes, so we can reconstruct why a pull failed without SSHing
into the VM. Failure events are at error level and carry a
`_page_snapshot()` of whatever Playwright had on screen at the time
(URL, title, first ~400 chars of body text) — essential for
distinguishing "USCIS changed the selector" from "we hit a CAPTCHA
page" from "the VM lost network" from "the account is rate-limited".
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeout,
)

from mfa_mailbox import fetch_latest_code
from system_log import log as sys_log

logger = logging.getLogger(__name__)

LOGIN_URL = "https://my.uscis.gov/oidc/login"
# Authenticated landing page. Hitting this unauthed redirects to
# `/sign-in` on `myaccount.uscis.gov`, which is how `probe_session`
# detects a stale session. The public root `my.uscis.gov/` serves a
# page even when the session is dead, so it gives a false positive.
# This URL must also live under `my.uscis.gov` (not `myaccount…`) so
# the post-login bridge primes the right cookie origin for the
# case-service API.
DASHBOARD_URL = "https://my.uscis.gov/account/applicant"

ROOT = Path(__file__).resolve().parent.parent
STORAGE_STATE_PATH = ROOT / ".uscis_session.json"


# Full-trace capture directory. One subdirectory per pull that
# produced traces (`<ts>_fail_<trigger>/` or `<ts>_ok_<trigger>/`)
# containing a Playwright `trace.zip` and (if MFA ran) an
# `mfa_trace/` subdir with wire-level IMAP events + every raw email
# considered. Rotation is failure-aware: when total count exceeds
# FULL_TRACES_KEEP, we delete the oldest `_ok` directory first;
# only when no `_ok` remains do we touch `_fail` directories.
# Failure forensics survive a parade of green pulls.
FULL_TRACES_DIR = ROOT / "data" / "full_traces"
FULL_TRACES_KEEP = 40
# Env var set by the server when config.trace_successful_pulls is
# true. Read by cmd_run to decide whether to persist the zip. The
# server passes it through child_env on each subprocess invocation
# so a config edit takes effect on the next pull without a restart.
TRACE_ON_SUCCESS_ENV = "USCIS_TRACE_ON_SUCCESS"


@dataclass
class TraceCollector:
    """Shared state for a single pull's trace artefacts.

    Lifecycle: created in cmd_run, passed down through
    ensure_authenticated → _do_login → _handle_mfa_if_present →
    fetch_latest_code. Populated as the pull runs. Persisted (or
    discarded) by cmd_run's finally block.

    Fields:
      * `mfa_events`   — wire-level IMAP + email-extraction events
                         (one dict per event). Written to
                         mfa_trace/events.jsonl when persisted.
      * `mfa_emails`   — raw email bytes per UID fetched.
                         Key is the UID string, value is the
                         raw RFC822 bytes. Written one-file-per-UID
                         when persisted.
      * `should_keep`  — set by cmd_run's finally based on exit
                         outcome + TRACE_ON_SUCCESS_ENV. When True,
                         the caller persists artefacts; when False,
                         everything is discarded.
      * `trace_dir`    — chosen directory path (set when persisting).
    """
    mfa_events: list[dict] = field(default_factory=list)
    mfa_emails: dict[str, bytes] = field(default_factory=dict)
    should_keep: bool = False
    trace_dir: Path | None = None

    def record_mfa_event(self, event: str, **fields) -> None:
        self.mfa_events.append({
            "ts": datetime.now(timezone.utc).isoformat(
                timespec="milliseconds",
            ).replace("+00:00", "Z"),
            "event": event,
            **fields,
        })

    def record_mfa_email(self, uid: str, raw_bytes: bytes) -> None:
        self.mfa_emails[str(uid)] = bytes(raw_bytes)


_SAFE_TAG_RE = re.compile(r"[^a-z0-9_]+")


def make_trace_dir(outcome: str, trigger: str) -> Path:
    """Create a `data/full_traces/<ts>_<outcome>_<trigger>/` dir and
    return the path. Safe to call from any writing phase; the
    directory is idempotent.
    """
    FULL_TRACES_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_trigger = _SAFE_TAG_RE.sub(
        "_", (trigger or "pull").lower()
    ).strip("_") or "pull"
    safe_outcome = "fail" if outcome == "fail" else "ok"
    d = FULL_TRACES_DIR / f"{ts}_{safe_outcome}_{safe_trigger}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_mfa_artefacts(
    trace_dir: Path, events: list[dict], emails: dict[str, bytes],
) -> Path | None:
    """Write MFA events + raw emails under `<trace_dir>/mfa_trace/`.

    Always creates the directory and events.jsonl when called — the
    caller decides (via `should_keep`) whether to preserve the trace
    at all, and if preserved the mfa_trace sidecar is always written
    so debug-mode and failure captures are consistent ("full trace +
    full auth record" is the contract, regardless of whether MFA was
    actually exercised on this pull).
    """
    mfa_dir = trace_dir / "mfa_trace"
    try:
        mfa_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:  # pragma: no cover
        logger.warning("mfa_trace mkdir failed: %s", e)
        return None

    try:
        with (mfa_dir / "events.jsonl").open("w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
    except Exception as e:  # pragma: no cover
        logger.warning("mfa events.jsonl write failed: %s", e)

    for uid, raw in emails.items():
        safe_uid = _SAFE_TAG_RE.sub("_", str(uid).lower()) or "unknown"
        try:
            (mfa_dir / f"email_{safe_uid}.eml").write_bytes(raw)
        except Exception as e:  # pragma: no cover
            logger.warning("mfa email write failed (uid=%s): %s", uid, e)

    return mfa_dir


def rotate_full_traces() -> None:
    """Cap FULL_TRACES_DIR to FULL_TRACES_KEEP subdirs, preferring to
    delete the oldest `_ok_` directory before any `_fail_`. Failure
    forensics are preserved across successful pulls indefinitely —
    a parade of green pulls cannot evict a two-week-old `_fail_`
    directory that you want to compare against. Only when every slot
    is occupied by failures do we delete the oldest failure.
    """
    try:
        if not FULL_TRACES_DIR.exists():
            return
        subs = [p for p in FULL_TRACES_DIR.iterdir() if p.is_dir()]
        if len(subs) <= FULL_TRACES_KEEP:
            return
        subs.sort(key=lambda p: p.stat().st_mtime)
        oks = [p for p in subs if "_ok_" in p.name]
        fails = [p for p in subs if p not in oks]
        while len(oks) + len(fails) > FULL_TRACES_KEEP:
            victim = oks.pop(0) if oks else fails.pop(0)
            try:
                for item in victim.rglob("*"):
                    if item.is_file() or item.is_symlink():
                        try:
                            item.unlink()
                        except OSError as e:  # pragma: no cover
                            logger.warning("trace rotate unlink: %s", e)
                for item in sorted(
                    victim.rglob("*"),
                    key=lambda p: len(p.parts), reverse=True,
                ):
                    if item.is_dir():
                        try:
                            item.rmdir()
                        except OSError as e:  # pragma: no cover
                            logger.warning("trace rotate rmdir: %s", e)
                victim.rmdir()
            except OSError as e:  # pragma: no cover
                logger.warning("trace rotate victim: %s", e)
    except OSError as e:  # pragma: no cover
        logger.warning("trace rotate list: %s", e)

# Heuristic for "we never left the public sign-in page". Used to
# distinguish a genuine `remember-this-browser` skip (good) from a
# silently-refused credential POST (bad) — both present the same
# `MFA selector not found` signal.
_SIGNIN_URL_PATTERNS = ("/sign-in", "/signin", "/oidc/login")

# Heuristic for "we never left the MFA challenge page after submitting
# the 2FA code". Includes the sign-in patterns because USCIS sometimes
# kicks a rejected 2FA back to the email/password form rather than
# re-rendering the MFA form.
_MFA_URL_PATTERNS = _SIGNIN_URL_PATTERNS + ("/auth", "/mfa", "/2fa")

# Step identifiers for the `login_result` sys_log event. Kept as
# stable strings so the dashboard and any alerting can key off them.
LOGIN_STEP_GOTO                    = "goto_login"
LOGIN_STEP_EMAIL_FORM              = "email_form"
LOGIN_STEP_FILL_CREDENTIALS        = "fill_credentials"
LOGIN_STEP_TOS                     = "tos"
LOGIN_STEP_SUBMIT                  = "submit_credentials"
LOGIN_STEP_MFA                     = "mfa"
LOGIN_STEP_AUTHENTICATED_LANDING   = "authenticated_landing"
LOGIN_STEP_BRIDGE                  = "bridge"


class AuthError(RuntimeError):
    """Raised when login cannot complete (bad creds, verification timeout, etc.)."""


# ---------------------------------------------------------------------------
# Diagnostic helper — used by every failure event
# ---------------------------------------------------------------------------

def _is_on_signin_url(url: str | None) -> bool:
    """True when the given URL is still on the public sign-in / OIDC-login
    surface — i.e. the credential POST didn't advance us.

    Used to reclassify what was previously the `prompt_absent` branch:
    if the MFA selector didn't appear *and* we're still on /sign-in,
    that's a refused submit, not a successful `remember-this-browser`
    skip. Prior to this reclassification, a soft anti-bot refusal was
    silently treated as success and the pull would then timeout waiting
    for an authenticated landing URL — costing 60+ s per failure with
    no actionable signal.
    """
    u = (url or "").lower()
    if not u:
        return True  # no URL → assume worst case (still on form)
    return any(p in u for p in _SIGNIN_URL_PATTERNS)


def _is_on_mfa_url(url: str | None) -> bool:
    """True when we're still on the MFA challenge or any sign-in surface
    after attempting to submit the 2FA code. See `_MFA_URL_PATTERNS`
    for the full list — includes `/auth`, `/mfa`, `/2fa` plus the
    `_SIGNIN_URL_PATTERNS` (USCIS sometimes kicks a rejected 2FA back
    to the email/password form rather than re-rendering MFA)."""
    u = (url or "").lower()
    if not u:
        return True  # pessimistic on empty
    return any(p in u for p in _MFA_URL_PATTERNS)


def _safe_title(page: Page) -> str:
    """Cheap `page.title()` wrapper that never raises and never calls
    `page.locator()`. Used on success paths where the full
    `_page_snapshot()` would be overkill (and would force tests to mock
    an extra `locator("body")` return value on every happy path).
    """
    try:
        return str(page.title() or "")[:200]
    except Exception as e:
        return f"<title capture failed: {type(e).__name__}>"


def _page_snapshot(page: Page, *, body_chars: int = 400) -> dict:
    """Capture what Playwright currently has on screen.

    Returns a dict of `{url, title, body_preview}`. Every field is
    best-effort: capture failures are stringified so we never raise
    from inside an error handler. Body preview is capped to prevent
    runaway strings, and any PII in the preview is limited because we
    only snapshot on the sign-in / OIDC flow (USCIS's public login
    pages), not on authenticated applicant screens.
    """
    url = getattr(page, "url", "") or ""
    try:
        title = page.title() or ""
    except Exception as e:
        title = f"<title capture failed: {type(e).__name__}>"
    try:
        body = page.locator("body").inner_text(timeout=2000)
    except Exception as e:
        body = f"<body capture failed: {type(e).__name__}>"
    return {
        "url": str(url)[:500],
        "title": str(title)[:200],
        "body_preview": str(body)[:body_chars],
    }


# ---------------------------------------------------------------------------
# Session URL heuristics
# ---------------------------------------------------------------------------

def is_session_page_authenticated(page: Page) -> bool:
    """Heuristic: true when the page has landed somewhere that is neither a
    sign-in nor MFA screen, on a USCIS domain."""
    url = (page.url or "").lower()
    if not url:
        return False
    on_uscis = "my.uscis.gov" in url or "myaccount.uscis.gov" in url
    on_auth_flow = any(
        p in url for p in ("/sign-in", "/auth", "/oidc/", "/login")
    )
    return on_uscis and not on_auth_flow


def is_session_page_authenticated_url(url: str) -> bool:
    url = (url or "").lower()
    if not url:
        return False
    return (
        ("my.uscis.gov" in url or "myaccount.uscis.gov" in url)
        and not any(p in url for p in ("/sign-in", "/auth", "/oidc/", "/login"))
    )


# ---------------------------------------------------------------------------
# probe_session — session liveness check
# ---------------------------------------------------------------------------

def probe_session(
    page: Page, *, collector: "TraceCollector | None" = None,
) -> bool:
    """Navigate to the authenticated landing page and report whether the
    session is currently valid. Does NOT initiate a login.

    Emits exactly one `probe_session_result` sys_log event per call
    with `outcome` ∈ {"valid", "stale", "timeout", "error"} and a page
    snapshot on non-valid outcomes. When a `collector` is supplied, also
    pushes `probe_session_started` + `probe_session_result` into the
    collector's event stream so the MFA/auth modal has a full record
    even on the session-reused path (where no IMAP / login activity
    would otherwise leave a trace).

    Any navigation error is treated as 'session stale' — this includes:
      - PlaywrightTimeout: page never reached domcontentloaded.
      - net::ERR_ABORTED: common when USCIS bounces us through a
        redirect chain `/account/applicant` -> `/oidc/...` -> `/sign-in`;
        Chromium aborts the first navigation when a redirect races.
      - Any other Playwright Error during goto.

    In every case the caller treats False as "run _do_login".
    """
    def _rec(event: str, **fields) -> None:
        if collector is not None:
            collector.record_mfa_event(event, **fields)

    _rec("probe_session_started", target=DASHBOARD_URL, timeout_ms=20_000)
    try:
        page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=20_000)
    except PlaywrightTimeout as e:
        snap = _page_snapshot(page)
        sys_log(
            "probe_session_result", level="warning", source="auth",
            outcome="timeout", target=DASHBOARD_URL,
            error=f"PlaywrightTimeout: {e}"[:200],
            **snap,
        )
        _rec(
            "probe_session_result", outcome="timeout",
            target=DASHBOARD_URL,
            error=f"PlaywrightTimeout: {e}"[:200],
            final_url=snap.get("url", ""),
        )
        logger.warning("probe: timeout on %s", DASHBOARD_URL)
        return False
    except PlaywrightError as e:
        snap = _page_snapshot(page)
        sys_log(
            "probe_session_result", level="warning", source="auth",
            outcome="error", target=DASHBOARD_URL,
            error=f"{type(e).__name__}: {e}"[:200],
            **snap,
        )
        _rec(
            "probe_session_result", outcome="error",
            target=DASHBOARD_URL,
            error=f"{type(e).__name__}: {e}"[:200],
            final_url=snap.get("url", ""),
        )
        logger.warning("probe: %s on %s: %s", type(e).__name__, DASHBOARD_URL, e)
        return False

    page.wait_for_timeout(1500)  # let any redirect settle
    ok = is_session_page_authenticated(page)
    if ok:
        sys_log(
            "probe_session_result", source="auth",
            outcome="valid", target=DASHBOARD_URL,
            url=page.url,
        )
        _rec(
            "probe_session_result", outcome="valid",
            target=DASHBOARD_URL, final_url=page.url,
        )
    else:
        snap = _page_snapshot(page)
        sys_log(
            "probe_session_result", source="auth",
            outcome="stale", target=DASHBOARD_URL,
            **snap,
        )
        _rec(
            "probe_session_result", outcome="stale",
            target=DASHBOARD_URL, final_url=snap.get("url", ""),
        )
    logger.info("probe: %s (url=%s)", "valid" if ok else "stale", page.url)
    return ok


# ---------------------------------------------------------------------------
# ensure_authenticated — orchestrates probe + login
# ---------------------------------------------------------------------------

def ensure_authenticated(
    context: BrowserContext,
    page: Page,
    auth: dict[str, str],
    *,
    allow_login: bool = True,
    collector: "TraceCollector | None" = None,
) -> None:
    """Ensure the session is valid, logging in only if necessary.

    Emits exactly one `auth_ensure_result` sys_log event per call,
    with `outcome` ∈ {"already_valid", "refused_stale_no_login",
    "logged_in", "login_verify_failed"}.

    Args:
        context/page: Playwright context and its main page.
        auth: credentials dict.
        allow_login: when False, a stale session raises `AuthError`
                     instead of triggering the login flow.
        collector: optional TraceCollector — when supplied, MFA
                   wire-level events and raw emails are recorded
                   into it. The caller decides whether to persist.
    """
    sys_log("auth_ensure_started", source="auth", allow_login=allow_login)
    if collector is not None:
        collector.record_mfa_event(
            "auth_ensure_started", allow_login=allow_login,
        )

    if probe_session(page, collector=collector):
        sys_log("auth_ensure_result", source="auth", outcome="already_valid")
        if collector is not None:
            collector.record_mfa_event(
                "auth_path", outcome="session_reused",
                note="probe_session returned valid — no login, no MFA.",
            )
        return

    if not allow_login:
        sys_log(
            "auth_ensure_result", level="error", source="auth",
            outcome="refused_stale_no_login",
        )
        if collector is not None:
            collector.record_mfa_event(
                "auth_path", outcome="refused_stale_no_login",
            )
        raise AuthError(
            "Session is stale and allow_login=False. "
            "Run the login flow explicitly (e.g. `python src/session_fetch.py login`)."
        )

    if collector is not None:
        collector.record_mfa_event(
            "auth_path", outcome="login_triggered",
            note="Session stale — running full _do_login flow.",
        )
    _do_login(context, page, auth, collector=collector)

    if not probe_session(page, collector=collector):
        snap = _page_snapshot(page)
        sys_log(
            "auth_ensure_result", level="error", source="auth",
            outcome="login_verify_failed",
            **snap,
        )
        if collector is not None:
            collector.should_keep = True
        raise AuthError(
            f"Login flow completed but session is still not authenticated "
            f"(current url={page.url})."
        )

    sys_log("auth_ensure_result", source="auth", outcome="logged_in")
    _persist(context)


def _persist(context: BrowserContext) -> None:
    context.storage_state(path=str(STORAGE_STATE_PATH))
    logger.info("Saved session state → %s", STORAGE_STATE_PATH.name)


def _clear_stored_session(context: BrowserContext) -> None:
    """Wipe every bit of session state before a fresh login.

    Why: when an earlier login fails partway (e.g. the MFA step times
    out), USCIS has already accepted our first factor.  The session
    cookies on disk + in the live context are now in a
    "half-authenticated" state.  A follow-up attempt to `/oidc/login`
    gets redirected to the MFA challenge page at
    `myaccount.uscis.gov/auth` instead of the email/password form —
    and `_do_login` then times out forever waiting for the
    `#email-address` selector that isn't there.

    Clear:
      * the live BrowserContext's cookies (so the current attempt
        starts clean);
      * the persisted `.uscis_session.json` (so a future process that
        loads storage_state doesn't re-inherit the bad cookies).

    Both operations are best-effort — any failure is logged but does
    not abort the login, because a retry with dirty cookies is still
    more likely to succeed than aborting entirely.
    """
    cookies_cleared = False
    try:
        context.clear_cookies()
        cookies_cleared = True
    except Exception as e:  # pragma: no cover — best-effort
        logger.warning("clear_cookies failed: %s", e)

    file_cleared = False
    try:
        if STORAGE_STATE_PATH.exists():
            STORAGE_STATE_PATH.unlink()
            file_cleared = True
    except Exception as e:  # pragma: no cover — best-effort
        logger.warning("unlink storage state failed: %s", e)

    sys_log(
        "login_storage_cleared", source="auth",
        cookies_cleared=cookies_cleared,
        file_cleared=file_cleared,
    )
    logger.info(
        "Cleared session state before login (cookies=%s, file=%s)",
        cookies_cleared, file_cleared,
    )


# ---------------------------------------------------------------------------
# _do_login — full OIDC form + MFA (only path that burns a code)
# ---------------------------------------------------------------------------

def _attach_response_listener(page: Page) -> list[dict]:
    """Install a response listener and return a list that will be
    populated with every HTTP response (GET + POST) whose URL touches
    the sign-in / OIDC / auth / MFA surfaces.

    Each captured dict has: `ts`, `url`, `status`, `method`. GETs are
    included so that an anti-bot block on the initial
    `GET /oidc/login` (403 / 500 / 503 / 429) surfaces as a real
    `auth_http_response` event — otherwise it would only show up as a
    downstream `auth_email_form_result: timeout` with no upstream
    status. Non-auth traffic (static assets, analytics) is filtered
    out so the list never grows unboundedly.
    """
    captured: list[dict] = []
    interesting = ("/sign-in", "/signin", "/oidc", "/auth", "/mfa",
                   "/2fa", "/login")

    def _on_response(resp) -> None:
        try:
            req = resp.request
            method = (req.method or "").upper()
            url = (resp.url or "").lower()
            if method not in ("GET", "POST"):
                return
            if not any(p in url for p in interesting):
                return
            captured.append({
                "ts": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"
                ).replace("+00:00", "Z"),
                "method": method,
                "url": resp.url[:500],
                "status": resp.status,
            })
        except Exception as e:  # pragma: no cover — best-effort
            logger.debug("response listener swallow: %s", e)

    try:
        page.on("response", _on_response)
    except Exception as e:  # pragma: no cover — best-effort
        logger.warning("failed to attach response listener: %s", e)

    return captured


def _do_login(
    context: BrowserContext, page: Page, auth: dict[str, str],
    *, collector: "TraceCollector | None" = None,
) -> None:
    """Full OpenID Connect login + MFA. This is the *only* code path that
    burns an MFA code.

    Always starts from a blank slate. See `_clear_stored_session()` for
    why half-auth cookies from a previously-failed login MUST be wiped
    before we try again: USCIS reads those cookies and skips the
    email/password form, redirecting straight to the MFA challenge
    page — where `_do_login` can't operate (no `#email-address`
    selector exists on that page).

    Emits step-by-step events so every phase is self-diagnosing in the
    System log:

      * `login_storage_cleared`    — cookies + file wiped
      * `login_started`            — nav begins
      * `auth_goto_login_result`   — after goto (success or failure)
      * `auth_email_form_result`   — email field found / timeout
      * `auth_credentials_filled`  — fill + TOS state
      * `auth_submit_result`       — url-before/after, nav observed, duration
      * `auth_post_response`       — HTTP status + final URL (from network
                                     listener), emitted once per matching POST
      * `login_mfa_result`         — MFA prompt present / absent / refused
                                     (URL-aware — see _handle_mfa_if_present)
      * `auth_landing_result`      — authenticated landing URL seen / timeout
      * `auth_bridge_result`       — post-landing dashboard bridge
      * `login_result`             — terminal ok/failed summary

    On any failure path `collector.should_keep` is set to True so the
    caller (cmd_run) persists the preserved trace to
    `data/full_traces/<ts>_fail_.../`. That directory contains
    Playwright's native `trace.zip` (DOM + network + screenshots +
    console) plus the `mfa_trace/` sidecar with wire-level IMAP
    events and archived emails.
    """
    _clear_stored_session(context)
    sys_log("login_started", source="auth", target=LOGIN_URL)
    logger.info("Starting login flow (this will send a MFA email)...")

    http_responses = _attach_response_listener(page)

    def _drain_http(at: str) -> None:
        """Emit one `auth_http_response` per new captured response, then
        clear the buffer. Called at every phase boundary so responses
        are attributed to the phase that caused them."""
        while http_responses:
            resp = http_responses.pop(0)
            sys_log(
                "auth_http_response", source="auth",
                method=resp.get("method"),
                url=resp.get("url"),
                status=resp.get("status"),
                at=at,
            )

    step = LOGIN_STEP_GOTO
    try:
        # ---- Step: navigate to /oidc/login -----------------------------
        goto_started = time.monotonic()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        goto_ms = int((time.monotonic() - goto_started) * 1000)
        # Cheap URL/title only — skip the body.inner_text() capture on
        # success paths to keep this hot path fast and to avoid
        # disturbing callers that only mock `page.locator` for the
        # form-selector path.
        sys_log(
            "auth_goto_login_result", source="auth",
            outcome="ok",
            duration_ms=goto_ms,
            url=getattr(page, "url", "") or "",
            title=_safe_title(page),
        )
        _drain_http(at="after_goto_login")

        # ---- Step: wait for #email-address -----------------------------
        step = LOGIN_STEP_EMAIL_FORM
        try:
            page.wait_for_selector("#email-address", timeout=20_000)
            sys_log(
                "auth_email_form_result", source="auth",
                outcome="found",
                url=getattr(page, "url", "") or "",
            )
        except PlaywrightTimeout:
            snap = _page_snapshot(page)
            sys_log(
                "auth_email_form_result", level="error", source="auth",
                outcome="timeout",
                **snap,
            )
            raise  # outer handler flips collector.should_keep to preserve trace

        # ---- Step: fill credentials + TOS ------------------------------
        step = LOGIN_STEP_FILL_CREDENTIALS
        page.fill("#email-address", auth["uscis_email"])
        page.fill("#password", auth["uscis_password"])

        step = LOGIN_STEP_TOS
        tos = page.locator("#checkbox")
        tos_present = bool(tos.count())
        tos_was_checked = tos.is_checked() if tos_present else False
        tos_now_checked = tos_was_checked
        if tos_present and not tos_was_checked:
            tos.check()
            tos_now_checked = True
        sys_log(
            "auth_credentials_filled", source="auth",
            email_length=len(auth.get("uscis_email", "")),
            password_length=len(auth.get("uscis_password", "")),
            tos_present=tos_present,
            tos_was_checked=tos_was_checked,
            tos_now_checked=tos_now_checked,
        )
        # Capture right after filling so a failing login has the full
        # pre-submit form state in its trace. If the form layout changes
        # (new fields, hidden honeypots, CAPTCHA iframes USCIS added),
        # this phase will show it.

        # ---- Step: submit credentials ----------------------------------
        step = LOGIN_STEP_SUBMIT
        # Record submit time; only MFA emails dated after this are acceptable.
        # Small 2s slack for mail-server clock skew.
        submit_time = datetime.now(timezone.utc) - timedelta(seconds=2)

        url_before = getattr(page, "url", "") or ""
        submit_started = time.monotonic()
        page.click("#sign-in-btn")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2500)
        submit_ms = int((time.monotonic() - submit_started) * 1000)
        url_after = getattr(page, "url", "") or ""
        navigation_observed = url_after != url_before
        still_on_signin = _is_on_signin_url(url_after)
        sys_log(
            "auth_submit_result", source="auth",
            url_before=url_before,
            url_after=url_after,
            navigation_observed=navigation_observed,
            still_on_signin=still_on_signin,
            duration_ms=submit_ms,
        )
        _drain_http(at="after_submit")

        # ---- Step: handle MFA prompt (URL-aware) -----------------------
        step = LOGIN_STEP_MFA
        _handle_mfa_if_present(page, auth, submit_time, collector=collector)

        # ---- Step: wait for authenticated landing ----------------------
        step = LOGIN_STEP_AUTHENTICATED_LANDING
        landing_started = time.monotonic()
        try:
            page.wait_for_url(
                lambda url: is_session_page_authenticated_url(url),
                timeout=45_000,
            )
            landing_ms = int((time.monotonic() - landing_started) * 1000)
            sys_log(
                "auth_landing_result", source="auth",
                outcome="landed",
                url=getattr(page, "url", "") or "",
                duration_ms=landing_ms,
            )
        except PlaywrightTimeout:
            # Non-fatal: proceed to bridge nav. Surface in sys_log so
            # we know the post-MFA landing never settled organically.
            # The at_landing trace capture below still runs so the
            # post-timeout page state is preserved.
            landing_ms = int((time.monotonic() - landing_started) * 1000)
            snap = _page_snapshot(page)
            sys_log(
                "auth_landing_result", level="warning", source="auth",
                outcome="timeout",
                duration_ms=landing_ms,
                **snap,
            )
            # Back-compat alias — dashboards / tests still key off this name.
            sys_log(
                "login_landing_timeout", level="warning", source="auth",
                **snap,
            )
            logger.warning("Did not observe authenticated landing (url=%s).", page.url)

        # ---- Step: bridge to my.uscis.gov ------------------------------
        step = LOGIN_STEP_BRIDGE
        # The case-service API lives on my.uscis.gov. The first sign-in may land
        # on myaccount.uscis.gov/dashboard — navigate once more to bridge cookies.
        bridge_started = time.monotonic()
        try:
            page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            sys_log(
                "auth_bridge_result", source="auth",
                outcome="ok",
                duration_ms=int((time.monotonic() - bridge_started) * 1000),
                url=getattr(page, "url", "") or "",
            )
        except PlaywrightTimeout:
            sys_log(
                "auth_bridge_result", level="warning", source="auth",
                outcome="timeout",
                error="PlaywrightTimeout",
            )
            sys_log(
                "login_bridge_warning", level="warning", source="auth",
                error="PlaywrightTimeout",
            )
            logger.warning("Timed out bridging to my.uscis.gov.")
        except PlaywrightError as e:
            sys_log(
                "auth_bridge_result", level="warning", source="auth",
                outcome="error",
                error=f"{type(e).__name__}: {e}"[:200],
            )
            sys_log(
                "login_bridge_warning", level="warning", source="auth",
                error=f"{type(e).__name__}: {e}"[:200],
            )
            logger.warning("Navigation error bridging to my.uscis.gov: %s", e)

        _drain_http(at="after_bridge")
        sys_log("login_result", source="auth", outcome="ok")

    except Exception as e:
        _drain_http(at="at_failure")
        # Signal to the outer pull runner that this pull must keep
        # its Playwright trace on disk. cmd_run's finally block reads
        # collector.should_keep to decide. The trace.zip captured by
        # context.tracing covers DOM / network / screenshots / console
        # for this failure moment; the mfa_trace sidecar covers any
        # IMAP-side activity the collector recorded before we got here.
        if collector is not None:
            collector.should_keep = True
        snap = _page_snapshot(page)
        sys_log(
            "login_result", level="error", source="auth",
            outcome="failed", step=step,
            error=f"{type(e).__name__}: {e}"[:200],
            **snap,
        )
        logger.error("Login failed at step=%s: %s: %s",
                     step, type(e).__name__, e)
        raise


# ---------------------------------------------------------------------------
# _handle_mfa_if_present — MFA code prompt + submission
# ---------------------------------------------------------------------------

def _handle_mfa_if_present(
    page: Page, auth: dict[str, str], submit_time: datetime,
    *, collector: "TraceCollector | None" = None,
) -> None:
    """Handle the MFA prompt if USCIS shows it — URL-aware.

    The MFA-selector-missing branch is split into two outcomes based
    on the current page URL:

      * `prompt_absent`          — MFA selector absent AND we're past
                                   sign-in (legitimate remember-browser
                                   skip).
      * `submit_did_not_advance` — MFA selector absent AND we're still
                                   on the sign-in / OIDC-login surface.
                                   Raises AuthError so the caller can
                                   retry.

    Any `TraceCollector` passed in receives IMAP wire-level events
    and raw email bytes via `fetch_latest_code`. The MFA code itself
    is never logged — only `code_length`.
    """
    code_selector = (
        '#secure-verification-code, input[name="code"], '
        'input[autocomplete="one-time-code"]'
    )
    try:
        page.wait_for_selector(code_selector, timeout=15_000)
    except PlaywrightTimeout:
        url = getattr(page, "url", "") or ""
        snap = _page_snapshot(page)
        if _is_on_signin_url(url):
            # The credential POST was silently refused — we never
            # advanced off the public sign-in page. Playwright tracing
            # is still recording (the context-level trace.start is live
            # for the entire pull), so the DOM + screenshot of this
            # exact moment is preserved inside the saved trace.zip.
            sys_log(
                "login_mfa_result", level="error", source="auth",
                outcome="submit_did_not_advance",
                note=(
                    "MFA prompt not found AND page is still on sign-in. "
                    "Most likely a silent anti-bot refusal of the "
                    "credential POST. Full trace saved on raise."
                ),
                **snap,
            )
            logger.error(
                "MFA prompt absent and still on sign-in (url=%s) — "
                "treating as refused submit.", url,
            )
            raise AuthError(
                f"Credential submit was not advanced by USCIS "
                f"(still on sign-in url={url}). Retry after backoff."
            )
        # Legitimate "remember-browser skip": we're past the public
        # sign-in surface and USCIS didn't prompt for a code.
        sys_log(
            "login_mfa_result", source="auth",
            outcome="prompt_absent",
            note="remember-this-browser cookie likely still valid",
            **snap,
        )
        logger.info(
            "No MFA prompt detected at url=%s — 'Remember this browser' "
            "likely active.", url,
        )
        return

    sys_log(
        "login_mfa_result", source="auth", outcome="prompt_present",
        url=getattr(page, "url", "") or "",
    )
    logger.info(
        "MFA prompt detected — polling inbox for the MFA code newer than %s...",
        submit_time.isoformat(timespec="seconds"),
    )

    # fetch_latest_code is itself instrumented (mfa_fetch_started /
    # _succeeded / _timeout). Any TimeoutError here propagates up into
    # _do_login's outer try, which categorises it under step="mfa".
    # The collector (if provided) receives wire-level IMAP events and
    # raw email bytes so a failure — including an extraction regex
    # miss after a USCIS template rewrite — has absolutely everything
    # needed to diagnose it offline.
    code = fetch_latest_code(
        auth["uscis_mfa_email"],
        auth["uscis_mfa_app_password"],
        since=submit_time,
        collector=collector,
    )
    logger.info("Received MFA code (length=%d)", len(code))

    page.fill(code_selector, code)

    # 24h "don't ask again" — critical to avoid daily verification-email spam.
    remember = page.locator("#remember-me-checkbox")
    remember_clicked = False
    if remember.count() and not remember.is_checked():
        remember.check()
        remember_clicked = True

    mfa_submit_started = time.monotonic()
    url_before_2fa = getattr(page, "url", "") or ""
    page.click('[id="2fa-submit-btn"]')
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2000)
    url_after_2fa = getattr(page, "url", "") or ""

    # Verify the 2FA POST actually advanced us past the MFA challenge
    # page. If we're still on /auth, /mfa, /sign-in, or /oidc, USCIS
    # silently rejected the code (expired / reused / wrong) and re-
    # rendered the same form. Without this check, a rejected code
    # looks identical to a network blip in the log — the next signal
    # is a 45-second landing-URL timeout 45s later.
    if _is_on_mfa_url(url_after_2fa):
        snap = _page_snapshot(page)
        sys_log(
            "login_mfa_result", level="error", source="auth",
            outcome="submit_did_not_advance",
            note=(
                "2FA code was submitted but page did not advance past the "
                "MFA / sign-in surface. Most likely an expired, reused, or "
                "rejected code. Full trace saved on raise."
            ),
            code_length=len(code),
            remember_clicked=remember_clicked,
            url_before=url_before_2fa,
            url_after=url_after_2fa,
            duration_ms=int((time.monotonic() - mfa_submit_started) * 1000),
            **snap,
        )
        logger.error(
            "2FA submit did not advance past MFA (url=%s → %s)",
            url_before_2fa, url_after_2fa,
        )
        raise AuthError(
            f"2FA code submitted but page is still on MFA/sign-in "
            f"(url={url_after_2fa}). Retry after backoff."
        )

    sys_log(
        "login_mfa_result", source="auth",
        outcome="extracted_and_submitted",
        code_length=len(code),
        remember_clicked=remember_clicked,
        url_before=url_before_2fa,
        url_after=url_after_2fa,
        duration_ms=int((time.monotonic() - mfa_submit_started) * 1000),
    )
