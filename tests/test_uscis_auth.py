"""Tests for the OpenID Connect + MFA login module.

Playwright `Page` / `BrowserContext` and `mfa_mailbox.fetch_latest_code` are
mocked so tests run offline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

import uscis_auth
from uscis_auth import (
    AuthError,
    _do_login,
    _handle_mfa_if_present,
    _persist,
    ensure_authenticated,
    is_session_page_authenticated,
    is_session_page_authenticated_url,
    probe_session,
)


# -------- URL / page heuristics ------------------------------------------

@pytest.mark.parametrize("url,expected", [
    ("https://my.uscis.gov/account/applicant", True),
    ("https://myaccount.uscis.gov/dashboard", True),
    ("https://my.uscis.gov/sign-in", False),
    ("https://my.uscis.gov/oidc/login", False),
    ("https://example.com/", False),
    ("", False),
    (None, False),
])
def test_is_session_page_authenticated_url(url, expected):
    assert is_session_page_authenticated_url(url) is expected


def test_is_session_page_authenticated_reads_page_url():
    page = MagicMock()
    page.url = "https://my.uscis.gov/account/applicant"
    assert is_session_page_authenticated(page) is True

    page.url = "https://my.uscis.gov/sign-in"
    assert is_session_page_authenticated(page) is False

    page.url = ""
    assert is_session_page_authenticated(page) is False


# -------- probe_session --------------------------------------------------

def test_probe_session_returns_true_on_authenticated_url():
    page = MagicMock()
    page.url = "https://my.uscis.gov/account/applicant"
    assert probe_session(page) is True
    page.goto.assert_called_once()


def test_probe_session_returns_false_on_timeout():
    page = MagicMock()
    page.goto.side_effect = PlaywrightTimeout("slow")
    page.url = ""
    assert probe_session(page) is False


def test_probe_session_returns_false_on_sign_in_url():
    page = MagicMock()
    page.url = "https://my.uscis.gov/sign-in"
    assert probe_session(page) is False


# -------- ensure_authenticated ------------------------------------------

def test_ensure_authenticated_noop_when_session_valid(monkeypatch):
    page = MagicMock()
    page.url = "https://my.uscis.gov/account/applicant"
    context = MagicMock()
    # No login attempted because probe returns True.
    with patch.object(uscis_auth, "_do_login") as do_login:
        ensure_authenticated(context, page, {}, allow_login=True)
        do_login.assert_not_called()


def test_ensure_authenticated_raises_when_stale_and_allow_login_false():
    page = MagicMock()
    page.url = "https://my.uscis.gov/sign-in"
    context = MagicMock()
    with pytest.raises(AuthError):
        ensure_authenticated(context, page, {}, allow_login=False)


def test_ensure_authenticated_calls_login_and_verifies():
    # Probe: first call False (stale), post-login probe True.
    page = MagicMock()
    page.url = "https://my.uscis.gov/sign-in"

    context = MagicMock()

    def flip_to_authed(*a, **k):
        page.url = "https://my.uscis.gov/account/applicant"

    with patch.object(uscis_auth, "_do_login", side_effect=flip_to_authed) as do_login:
        ensure_authenticated(context, page, {"uscis_email": "u", "uscis_password": "p"},
                             allow_login=True)
        do_login.assert_called_once()
    # session state persisted after login
    context.storage_state.assert_called_once()


def test_ensure_authenticated_raises_when_login_fails_to_authenticate():
    page = MagicMock()
    page.url = "https://my.uscis.gov/sign-in"
    context = MagicMock()
    with patch.object(uscis_auth, "_do_login"):
        with pytest.raises(AuthError):
            ensure_authenticated(context, page, {}, allow_login=True)


# -------- _persist -----------------------------------------------------

def test_persist_writes_storage_state():
    context = MagicMock()
    _persist(context)
    context.storage_state.assert_called_once()
    path_arg = context.storage_state.call_args.kwargs.get("path")
    assert path_arg and str(path_arg).endswith(".uscis_session.json")


# -------- _do_login ----------------------------------------------------

def test_do_login_happy_path_without_mfa():
    page = MagicMock()
    page.url = "https://my.uscis.gov/account/applicant"
    # First selector wait = email prompt (ok); second = MFA prompt (absent).
    page.wait_for_selector.side_effect = [None, PlaywrightTimeout("no mfa")]

    tos = MagicMock()
    tos.count.return_value = 1
    tos.is_checked.return_value = False
    page.locator.return_value = tos

    context = MagicMock()
    auth = {"uscis_email": "e", "uscis_password": "p",
            "uscis_mfa_email": "g", "uscis_mfa_app_password": "a"}

    with patch.object(uscis_auth, "fetch_latest_code") as fetch_code:
        _do_login(context, page, auth)
        fetch_code.assert_not_called()

    # Credentials filled and submitted.
    fills = [c.args[0] for c in page.fill.call_args_list]
    assert "#email-address" in fills
    assert "#password" in fills


def test_do_login_with_mfa_submits_fetched_code():
    page = MagicMock()
    page.url = "https://my.uscis.gov/account/applicant"

    # First locator() call is Terms-of-Service checkbox — second is remember-me.
    tos = MagicMock(); tos.count.return_value = 0
    remember = MagicMock(); remember.count.return_value = 1; remember.is_checked.return_value = False
    page.locator.side_effect = [tos, remember]

    # Both wait_for_selector calls succeed (email + MFA prompt).
    page.wait_for_selector.return_value = None

    context = MagicMock()
    auth = {"uscis_email": "e", "uscis_password": "p",
            "uscis_mfa_email": "g", "uscis_mfa_app_password": "a"}

    with patch.object(uscis_auth, "fetch_latest_code", return_value="123456") as fetch_code:
        _do_login(context, page, auth)
        fetch_code.assert_called_once()

    # The code was filled somewhere.
    fills = [c.args[1] for c in page.fill.call_args_list]
    assert "123456" in fills
    remember.check.assert_called_once()


def test_do_login_swallows_url_wait_timeout():
    page = MagicMock()
    page.url = "https://my.uscis.gov/account/applicant"
    # email prompt ok, MFA absent.
    page.wait_for_selector.side_effect = [None, PlaywrightTimeout("no mfa")]
    page.locator.return_value = MagicMock(count=lambda: 0)

    # Both wait_for_url and the dashboard bridge goto raise.
    page.wait_for_url.side_effect = PlaywrightTimeout("didn't land")
    # First goto is LOGIN_URL (ok), second (bridge) raises.
    gotos = [None, PlaywrightTimeout("bridge failed")]

    def _goto(*a, **k):
        item = gotos.pop(0) if gotos else None
        if isinstance(item, Exception):
            raise item
        return None
    page.goto.side_effect = _goto

    context = MagicMock()
    auth = {"uscis_email": "e", "uscis_password": "p",
            "uscis_mfa_email": "g", "uscis_mfa_app_password": "a"}

    # Must not propagate — both timeouts are swallowed and logged.
    _do_login(context, page, auth)


# -------- _handle_mfa_if_present skip path -----------------------------

def test_handle_mfa_if_present_skips_when_no_prompt():
    page = MagicMock()
    page.wait_for_selector.side_effect = PlaywrightTimeout("none")
    with patch.object(uscis_auth, "fetch_latest_code") as fetch_code:
        _handle_mfa_if_present(
            page, {"uscis_mfa_email": "g", "uscis_mfa_app_password": "a"},
            datetime.now(timezone.utc),
        )
    fetch_code.assert_not_called()


def test_handle_mfa_if_present_without_remember_checkbox():
    page = MagicMock()
    page.wait_for_selector.return_value = None
    # Remember-me doesn't exist on this page.
    remember = MagicMock(); remember.count.return_value = 0
    page.locator.return_value = remember

    with patch.object(uscis_auth, "fetch_latest_code", return_value="111111"):
        _handle_mfa_if_present(
            page,
            {"uscis_mfa_email": "g", "uscis_mfa_app_password": "a"},
            datetime.now(timezone.utc),
        )
    remember.check.assert_not_called()
