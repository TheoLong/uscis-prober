"""USCIS login flow — isolated from fetching.

Only call `ensure_authenticated` when you actually need to log in. It is the
*only* function in the project that will submit credentials or trigger an
MFA email. All other code paths must treat an invalid session as an error.

Session state is persisted to `.uscis_session.json` via Playwright's
storage_state mechanism, and is reused on subsequent runs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import (
    BrowserContext,
    Page,
    TimeoutError as PlaywrightTimeout,
)

from mfa_mailbox import fetch_latest_code

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


class AuthError(RuntimeError):
    """Raised when login cannot complete (bad creds, verification timeout, etc.)."""


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


def probe_session(page: Page) -> bool:
    """Navigate to the authenticated landing page and report whether the
    session is currently valid. Does NOT initiate a login."""
    try:
        page.goto(DASHBOARD_URL, wait_until="domcontentloaded", timeout=20_000)
    except PlaywrightTimeout:
        logger.warning("Timeout probing %s", DASHBOARD_URL)
        return False
    page.wait_for_timeout(1500)  # let any redirect settle
    ok = is_session_page_authenticated(page)
    logger.info("Session probe → %s (url=%s)", "valid" if ok else "expired", page.url)
    return ok


def ensure_authenticated(
    context: BrowserContext,
    page: Page,
    auth: dict[str, str],
    *,
    allow_login: bool = True,
) -> None:
    """Ensure the session is valid, logging in only if necessary.

    Args:
        context/page: Playwright context and its main page.
        auth: credentials dict — keys uscis_email, uscis_password,
              uscis_mfa_email, uscis_mfa_app_password.
        allow_login: when False, a stale session raises `AuthError` instead of
                     triggering the (MFA-burning) login flow. Pass False from
                     scripts that are iterating on API logic.
    """
    if probe_session(page):
        return

    if not allow_login:
        raise AuthError(
            "Session is stale and allow_login=False. "
            "Run the login flow explicitly (e.g. `python src/session_fetch.py login`)."
        )

    _do_login(context, page, auth)

    # Verify we ended up authenticated.
    if not probe_session(page):
        raise AuthError(
            f"Login flow completed but session is still not authenticated "
            f"(current url={page.url})."
        )
    _persist(context)


def _persist(context: BrowserContext) -> None:
    context.storage_state(path=str(STORAGE_STATE_PATH))
    logger.info("Saved session state → %s", STORAGE_STATE_PATH.name)


def _do_login(context: BrowserContext, page: Page, auth: dict[str, str]) -> None:
    """Full OpenID Connect login + MFA. This is the *only* code path that burns an MFA code."""
    logger.info("Starting login flow (this will send a MFA email)...")
    page.goto(LOGIN_URL, wait_until="domcontentloaded")

    page.wait_for_selector("#email-address", timeout=20_000)
    page.fill("#email-address", auth["uscis_email"])
    page.fill("#password", auth["uscis_password"])

    tos = page.locator("#checkbox")
    if tos.count() and not tos.is_checked():
        tos.check()

    # Record submit time; only MFA emails dated after this are acceptable.
    # Small 2s slack for mail-server clock skew.
    submit_time = datetime.now(timezone.utc) - timedelta(seconds=2)
    page.click("#sign-in-btn")
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2500)

    _handle_mfa_if_present(page, auth, submit_time)

    # Wait for the authenticated landing (either dashboard or applicant page).
    try:
        page.wait_for_url(
            lambda url: is_session_page_authenticated_url(url),
            timeout=45_000,
        )
    except PlaywrightTimeout:
        logger.warning("Did not observe authenticated landing (url=%s).", page.url)

    # The case-service API lives on my.uscis.gov. The first sign-in may land on
    # myaccount.uscis.gov/dashboard — navigate once more to bridge cookies over.
    try:
        page.goto(DASHBOARD_URL, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
    except PlaywrightTimeout:
        logger.warning("Timed out bridging to my.uscis.gov.")


def is_session_page_authenticated_url(url: str) -> bool:
    url = (url or "").lower()
    if not url:
        return False
    return (
        ("my.uscis.gov" in url or "myaccount.uscis.gov" in url)
        and not any(p in url for p in ("/sign-in", "/auth", "/oidc/", "/login"))
    )


def _handle_mfa_if_present(
    page: Page, auth: dict[str, str], submit_time: datetime
) -> None:
    code_selector = (
        '#secure-verification-code, input[name="code"], '
        'input[autocomplete="one-time-code"]'
    )
    try:
        page.wait_for_selector(code_selector, timeout=15_000)
    except PlaywrightTimeout:
        logger.info("No MFA prompt detected — 'Remember this browser' likely active.")
        return

    logger.info(
        "MFA prompt detected — polling inbox for the MFA code newer than %s...",
        submit_time.isoformat(timespec="seconds"),
    )
    code = fetch_latest_code(
        auth["uscis_mfa_email"],
        auth["uscis_mfa_app_password"],
        since=submit_time,
    )
    logger.info("Received MFA code: %s", code)

    page.fill(code_selector, code)

    # 24h "don't ask again" — critical to avoid daily verification-email spam.
    remember = page.locator("#remember-me-checkbox")
    if remember.count() and not remember.is_checked():
        remember.check()

    page.click('[id="2fa-submit-btn"]')
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2000)
