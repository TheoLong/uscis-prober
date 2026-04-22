# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""USCIS login flow — isolated from fetching.

Only call `ensure_authenticated` when you actually need to log in. It is the
*only* function in the project that will submit credentials or trigger an
MFA email. All other code paths must treat an invalid session as an error.

Session state is persisted to `.uscis_session.json` via Playwright's
storage_state mechanism, and is reused on subsequent runs.

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

import logging
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

def probe_session(page: Page) -> bool:
    """Navigate to the authenticated landing page and report whether the
    session is currently valid. Does NOT initiate a login.

    Emits exactly one `probe_session_result` sys_log event per call
    with `outcome` ∈ {"valid", "stale", "timeout", "error"} and a page
    snapshot on non-valid outcomes.

    Any navigation error is treated as 'session stale' — this includes:
      - PlaywrightTimeout: page never reached domcontentloaded.
      - net::ERR_ABORTED: common when USCIS bounces us through a
        redirect chain `/account/applicant` -> `/oidc/...` -> `/sign-in`;
        Chromium aborts the first navigation when a redirect races.
      - Any other Playwright Error during goto.

    In every case the caller treats False as "run _do_login".
    """
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
    else:
        snap = _page_snapshot(page)
        sys_log(
            "probe_session_result", source="auth",
            outcome="stale", target=DASHBOARD_URL,
            **snap,
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
) -> None:
    """Ensure the session is valid, logging in only if necessary.

    Emits exactly one `auth_ensure_result` sys_log event per call,
    with `outcome` ∈ {"already_valid", "refused_stale_no_login",
    "logged_in", "login_verify_failed"}.

    Args:
        context/page: Playwright context and its main page.
        auth: credentials dict — keys uscis_email, uscis_password,
              uscis_mfa_email, uscis_mfa_app_password.
        allow_login: when False, a stale session raises `AuthError` instead of
                     triggering the (MFA-burning) login flow. Pass False from
                     scripts that are iterating on API logic.
    """
    sys_log("auth_ensure_started", source="auth", allow_login=allow_login)

    if probe_session(page):
        sys_log("auth_ensure_result", source="auth", outcome="already_valid")
        return

    if not allow_login:
        sys_log(
            "auth_ensure_result", level="error", source="auth",
            outcome="refused_stale_no_login",
        )
        raise AuthError(
            "Session is stale and allow_login=False. "
            "Run the login flow explicitly (e.g. `python src/session_fetch.py login`)."
        )

    _do_login(context, page, auth)

    # Verify we ended up authenticated.
    if not probe_session(page):
        snap = _page_snapshot(page)
        sys_log(
            "auth_ensure_result", level="error", source="auth",
            outcome="login_verify_failed",
            **snap,
        )
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

def _do_login(context: BrowserContext, page: Page, auth: dict[str, str]) -> None:
    """Full OpenID Connect login + MFA. This is the *only* code path that
    burns an MFA code.

    Always starts from a blank slate. See `_clear_stored_session()` for
    why half-auth cookies from a previously-failed login MUST be wiped
    before we try again: USCIS reads those cookies and skips the
    email/password form, redirecting straight to the MFA challenge
    page — where `_do_login` can't operate (no `#email-address`
    selector exists on that page).

    Emits exactly one `login_result` sys_log event at function exit:
      - outcome="ok"     on successful login (code delivered & submitted)
      - outcome="failed" on any step failure, with:
          * step:   which logical step was in progress (one of the
                    LOGIN_STEP_* constants)
          * error:  exception type + truncated message
          * url/title/body_preview: `_page_snapshot()` so you can see
            exactly what USCIS was showing at the failure moment —
            the single most useful field for distinguishing rate-limit
            pages, CAPTCHA interstitials, and selector renames.
    """
    _clear_stored_session(context)
    sys_log("login_started", source="auth", target=LOGIN_URL)
    logger.info("Starting login flow (this will send a MFA email)...")

    step = LOGIN_STEP_GOTO
    try:
        page.goto(LOGIN_URL, wait_until="domcontentloaded")

        step = LOGIN_STEP_EMAIL_FORM
        page.wait_for_selector("#email-address", timeout=20_000)

        step = LOGIN_STEP_FILL_CREDENTIALS
        page.fill("#email-address", auth["uscis_email"])
        page.fill("#password", auth["uscis_password"])

        step = LOGIN_STEP_TOS
        tos = page.locator("#checkbox")
        if tos.count() and not tos.is_checked():
            tos.check()

        step = LOGIN_STEP_SUBMIT
        # Record submit time; only MFA emails dated after this are acceptable.
        # Small 2s slack for mail-server clock skew.
        submit_time = datetime.now(timezone.utc) - timedelta(seconds=2)
        page.click("#sign-in-btn")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(2500)

        step = LOGIN_STEP_MFA
        _handle_mfa_if_present(page, auth, submit_time)

        step = LOGIN_STEP_AUTHENTICATED_LANDING
        # Wait for the authenticated landing (either dashboard or applicant page).
        try:
            page.wait_for_url(
                lambda url: is_session_page_authenticated_url(url),
                timeout=45_000,
            )
        except PlaywrightTimeout:
            # Non-fatal: proceed to bridge nav. Surface in sys_log so
            # we know the post-MFA landing never settled organically.
            snap = _page_snapshot(page)
            sys_log(
                "login_landing_timeout", level="warning", source="auth",
                **snap,
            )
            logger.warning("Did not observe authenticated landing (url=%s).", page.url)

        step = LOGIN_STEP_BRIDGE
        # The case-service API lives on my.uscis.gov. The first sign-in may land
        # on myaccount.uscis.gov/dashboard — navigate once more to bridge cookies.
        try:
            page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
        except PlaywrightTimeout:
            sys_log(
                "login_bridge_warning", level="warning", source="auth",
                error="PlaywrightTimeout",
            )
            logger.warning("Timed out bridging to my.uscis.gov.")
        except PlaywrightError as e:
            sys_log(
                "login_bridge_warning", level="warning", source="auth",
                error=f"{type(e).__name__}: {e}"[:200],
            )
            logger.warning("Navigation error bridging to my.uscis.gov: %s", e)

        sys_log("login_result", source="auth", outcome="ok")

    except Exception as e:
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
    page: Page, auth: dict[str, str], submit_time: datetime
) -> None:
    """Handle the MFA prompt if USCIS shows it.

    Emits one `login_mfa_result` sys_log event describing which branch
    executed: `prompt_absent` (remember-this-browser cookie still
    valid), `extracted_and_submitted` (success), or a terminal error.
    The MFA code itself is never logged — only `code_length`.
    """
    code_selector = (
        '#secure-verification-code, input[name="code"], '
        'input[autocomplete="one-time-code"]'
    )
    try:
        page.wait_for_selector(code_selector, timeout=15_000)
    except PlaywrightTimeout:
        sys_log(
            "login_mfa_result", source="auth",
            outcome="prompt_absent",
            note="remember-this-browser cookie likely still valid",
        )
        logger.info("No MFA prompt detected — 'Remember this browser' likely active.")
        return

    sys_log("login_mfa_result", source="auth", outcome="prompt_present")
    logger.info(
        "MFA prompt detected — polling inbox for the MFA code newer than %s...",
        submit_time.isoformat(timespec="seconds"),
    )

    # fetch_latest_code is itself instrumented (mfa_fetch_started /
    # _succeeded / _timeout). Any TimeoutError here propagates up into
    # _do_login's outer try, which categorises it under step="mfa".
    code = fetch_latest_code(
        auth["uscis_mfa_email"],
        auth["uscis_mfa_app_password"],
        since=submit_time,
    )
    logger.info("Received MFA code (length=%d)", len(code))

    page.fill(code_selector, code)

    # 24h "don't ask again" — critical to avoid daily verification-email spam.
    remember = page.locator("#remember-me-checkbox")
    remember_clicked = False
    if remember.count() and not remember.is_checked():
        remember.check()
        remember_clicked = True

    page.click('[id="2fa-submit-btn"]')
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2000)

    sys_log(
        "login_mfa_result", source="auth",
        outcome="extracted_and_submitted",
        code_length=len(code),
        remember_clicked=remember_clicked,
    )
