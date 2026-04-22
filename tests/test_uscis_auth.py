# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
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


def test_probe_session_hits_authenticated_path_not_root():
    # Regression guard: the probe URL must be an authenticated path.
    # Navigating to `my.uscis.gov/` (public root) serves a page even
    # when the session is dead, so the heuristic would incorrectly
    # return True. The probe must hit something that redirects to
    # /sign-in when unauthed.
    assert "/account" in uscis_auth.DASHBOARD_URL
    assert uscis_auth.DASHBOARD_URL != "https://my.uscis.gov/"

    page = MagicMock()
    page.url = "https://my.uscis.gov/account/applicant"
    probe_session(page)
    url_used = page.goto.call_args.args[0]
    assert "/account" in url_used


def test_probe_session_detects_stale_session_via_signin_redirect():
    # When the session is stale, USCIS redirects to
    # `myaccount.uscis.gov/sign-in`. `probe_session` must return False.
    page = MagicMock()
    page.url = "https://myaccount.uscis.gov/sign-in"
    assert probe_session(page) is False


def test_probe_session_returns_false_on_timeout():
    page = MagicMock()
    page.goto.side_effect = PlaywrightTimeout("slow")
    page.url = ""
    assert probe_session(page) is False


def test_probe_session_returns_false_on_net_aborted():
    # Regression guard: USCIS sometimes bounces /account/applicant through a
    # redirect chain that races with Chromium, producing
    # `net::ERR_ABORTED`. That raises playwright.sync_api.Error (NOT
    # TimeoutError) — probe_session must still report the session as
    # stale so ensure_authenticated falls through to _do_login.
    from playwright.sync_api import Error as PlaywrightError
    page = MagicMock()
    page.goto.side_effect = PlaywrightError(
        "Page.goto: net::ERR_ABORTED at https://my.uscis.gov/account/applicant"
    )
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


# -------- system log instrumentation ------------------------------------

@pytest.fixture
def syslog_to_tmp(monkeypatch, tmp_path):
    """Redirect system_log.LOG_PATH to tmp and return a helper that
    reads the events back out. Every sys_log-focused test uses this."""
    import system_log
    monkeypatch.setattr(system_log, "LOG_PATH", tmp_path / "system_log.json")
    system_log.clear()
    def _read():
        return system_log.read_all()
    return _read


def _mock_authed_page(url="https://my.uscis.gov/account/applicant"):
    p = MagicMock()
    p.url = url
    p.title.return_value = "USCIS — applicant"
    body = MagicMock()
    body.inner_text.return_value = "welcome to your USCIS account"
    p.locator.return_value = body
    return p


def _mock_stale_page(url="https://myaccount.uscis.gov/sign-in"):
    p = MagicMock()
    p.url = url
    p.title.return_value = "Sign in to USCIS"
    body = MagicMock()
    body.inner_text.return_value = "Sign in to continue"
    p.locator.return_value = body
    return p


def test_probe_session_emits_valid_result(syslog_to_tmp):
    page = _mock_authed_page()
    assert probe_session(page) is True
    events = [e for e in syslog_to_tmp()
              if e["event"] == "probe_session_result"]
    assert len(events) == 1
    assert events[0]["outcome"] == "valid"


def test_probe_session_emits_stale_result_with_snapshot(syslog_to_tmp):
    page = _mock_stale_page()
    assert probe_session(page) is False
    events = [e for e in syslog_to_tmp()
              if e["event"] == "probe_session_result"]
    assert len(events) == 1
    ev = events[0]
    assert ev["outcome"] == "stale"
    # Snapshot fields must be present so operators can see where we
    # actually landed.
    assert ev["url"] == "https://myaccount.uscis.gov/sign-in"
    assert "title" in ev and "body_preview" in ev


def test_probe_session_emits_error_outcome_on_net_aborted(syslog_to_tmp):
    from playwright.sync_api import Error as PlaywrightError
    page = _mock_stale_page()
    page.goto.side_effect = PlaywrightError("net::ERR_ABORTED")
    assert probe_session(page) is False
    events = [e for e in syslog_to_tmp()
              if e["event"] == "probe_session_result"]
    assert len(events) == 1
    assert events[0]["outcome"] == "error"
    assert "net::ERR_ABORTED" in events[0]["error"]


def test_probe_session_emits_timeout_outcome(syslog_to_tmp):
    page = _mock_stale_page()
    page.goto.side_effect = PlaywrightTimeout("slow")
    assert probe_session(page) is False
    events = [e for e in syslog_to_tmp()
              if e["event"] == "probe_session_result"]
    assert len(events) == 1
    assert events[0]["outcome"] == "timeout"


def test_ensure_authenticated_emits_already_valid(syslog_to_tmp):
    page = _mock_authed_page()
    context = MagicMock()
    ensure_authenticated(context, page, {}, allow_login=True)
    outcomes = [e.get("outcome") for e in syslog_to_tmp()
                if e["event"] == "auth_ensure_result"]
    assert outcomes == ["already_valid"]


def test_ensure_authenticated_emits_refused_stale_no_login(syslog_to_tmp):
    page = _mock_stale_page()
    context = MagicMock()
    with pytest.raises(AuthError):
        ensure_authenticated(context, page, {}, allow_login=False)
    outcomes = [e.get("outcome") for e in syslog_to_tmp()
                if e["event"] == "auth_ensure_result"]
    assert outcomes == ["refused_stale_no_login"]


def test_ensure_authenticated_emits_login_verify_failed(syslog_to_tmp):
    page = _mock_stale_page()
    context = MagicMock()
    with patch.object(uscis_auth, "_do_login"):  # no-op login
        with pytest.raises(AuthError):
            ensure_authenticated(context, page, {}, allow_login=True)
    outcomes = [e.get("outcome") for e in syslog_to_tmp()
                if e["event"] == "auth_ensure_result"]
    assert outcomes == ["login_verify_failed"]


def test_do_login_emits_login_result_ok_on_success(syslog_to_tmp):
    page = _mock_authed_page()
    # First wait_for_selector = email form (ok), second = MFA (absent).
    page.wait_for_selector.side_effect = [None, PlaywrightTimeout("no mfa")]
    tos = MagicMock(); tos.count.return_value = 0
    page.locator.side_effect = [tos, MagicMock()]  # tos + body snapshot
    context = MagicMock()
    auth = {"uscis_email": "e", "uscis_password": "p",
            "uscis_mfa_email": "g", "uscis_mfa_app_password": "a"}

    with patch.object(uscis_auth, "fetch_latest_code"):
        _do_login(context, page, auth)

    results = [e for e in syslog_to_tmp()
               if e["event"] == "login_result"]
    assert len(results) == 1
    assert results[0]["outcome"] == "ok"


def test_do_login_emits_login_result_failed_with_snapshot(syslog_to_tmp):
    # This is TODAY'S failure mode: #email-address selector never appears.
    # The login_result event must capture step=email_form and the page
    # snapshot so we can see what USCIS was actually showing.
    page = MagicMock()
    page.url = "https://my.uscis.gov/oidc/login"
    page.title.return_value = "Too Many Attempts"
    body = MagicMock()
    body.inner_text.return_value = (
        "Your IP has been rate-limited. Try again in 15 minutes."
    )
    page.locator.return_value = body
    page.wait_for_selector.side_effect = PlaywrightTimeout(
        "Timeout 20000ms exceeded. waiting for #email-address"
    )

    context = MagicMock()
    auth = {"uscis_email": "e", "uscis_password": "p",
            "uscis_mfa_email": "g", "uscis_mfa_app_password": "a"}

    with pytest.raises(PlaywrightTimeout):
        _do_login(context, page, auth)

    results = [e for e in syslog_to_tmp()
               if e["event"] == "login_result"]
    assert len(results) == 1
    ev = results[0]
    assert ev["outcome"] == "failed"
    assert ev["step"] == uscis_auth.LOGIN_STEP_EMAIL_FORM
    assert "TimeoutError" in ev["error"]
    # Page snapshot must carry the signal an operator would need:
    assert ev["title"] == "Too Many Attempts"
    assert "rate-limited" in ev["body_preview"]
    assert ev["url"] == "https://my.uscis.gov/oidc/login"


def test_do_login_emits_login_result_failed_with_step_goto(syslog_to_tmp):
    # Navigating to the login page itself fails.  step must be goto_login.
    page = _mock_stale_page("")
    page.goto.side_effect = PlaywrightTimeout("login page never loaded")
    context = MagicMock()
    auth = {"uscis_email": "e", "uscis_password": "p",
            "uscis_mfa_email": "g", "uscis_mfa_app_password": "a"}

    with pytest.raises(PlaywrightTimeout):
        _do_login(context, page, auth)

    results = [e for e in syslog_to_tmp()
               if e["event"] == "login_result"]
    assert len(results) == 1
    assert results[0]["outcome"] == "failed"
    assert results[0]["step"] == uscis_auth.LOGIN_STEP_GOTO


def test_handle_mfa_if_present_emits_prompt_absent(syslog_to_tmp):
    page = MagicMock()
    page.wait_for_selector.side_effect = PlaywrightTimeout("none")
    with patch.object(uscis_auth, "fetch_latest_code"):
        _handle_mfa_if_present(
            page, {"uscis_mfa_email": "g", "uscis_mfa_app_password": "a"},
            datetime.now(timezone.utc),
        )
    results = [e for e in syslog_to_tmp()
               if e["event"] == "login_mfa_result"]
    assert [e["outcome"] for e in results] == ["prompt_absent"]


def test_handle_mfa_if_present_emits_prompt_present_and_submitted(syslog_to_tmp):
    page = MagicMock()
    page.wait_for_selector.return_value = None
    remember = MagicMock(); remember.count.return_value = 1
    remember.is_checked.return_value = False
    page.locator.return_value = remember
    with patch.object(uscis_auth, "fetch_latest_code", return_value="424242"):
        _handle_mfa_if_present(
            page, {"uscis_mfa_email": "g", "uscis_mfa_app_password": "a"},
            datetime.now(timezone.utc),
        )
    outcomes = [e["outcome"] for e in syslog_to_tmp()
                if e["event"] == "login_mfa_result"]
    assert outcomes == ["prompt_present", "extracted_and_submitted"]


# -------- clear session state before login ------------------------------

def test_do_login_clears_cookies_before_submitting(syslog_to_tmp):
    # Regression guard for the 2026-04-22 half-auth cascade: when
    # earlier attempts left session cookies in a "waiting for MFA"
    # state, USCIS skips the email/password form.  _do_login MUST
    # wipe cookies (and the persisted storage file) before it
    # navigates to the login URL, guaranteeing a fresh form.
    page = _mock_authed_page()
    page.wait_for_selector.side_effect = [None, PlaywrightTimeout("no mfa")]
    tos = MagicMock(); tos.count.return_value = 0
    page.locator.side_effect = [tos, MagicMock()]
    context = MagicMock()
    auth = {"uscis_email": "e", "uscis_password": "p",
            "uscis_mfa_email": "g", "uscis_mfa_app_password": "a"}

    with patch.object(uscis_auth, "fetch_latest_code"):
        _do_login(context, page, auth)

    # Cookie clearance must happen before anything else is called on
    # the context (new_page, etc.). Assert clear_cookies is the first
    # context mutation.
    context.clear_cookies.assert_called_once()
    # And a login_storage_cleared event must fire so the dashboard
    # shows the recovery step.
    events = [e for e in syslog_to_tmp()
              if e["event"] == "login_storage_cleared"]
    assert len(events) == 1
    assert events[0]["cookies_cleared"] is True


def test_do_login_unlinks_storage_state_file(monkeypatch, tmp_path, syslog_to_tmp):
    # Point the storage-state path at a tmp file, touch it, then run
    # _do_login and assert the file was removed.
    fake_storage = tmp_path / ".uscis_session.json"
    fake_storage.write_text('{"cookies": []}')
    monkeypatch.setattr(uscis_auth, "STORAGE_STATE_PATH", fake_storage)

    page = _mock_authed_page()
    page.wait_for_selector.side_effect = [None, PlaywrightTimeout("no mfa")]
    tos = MagicMock(); tos.count.return_value = 0
    page.locator.side_effect = [tos, MagicMock()]
    context = MagicMock()
    auth = {"uscis_email": "e", "uscis_password": "p",
            "uscis_mfa_email": "g", "uscis_mfa_app_password": "a"}

    with patch.object(uscis_auth, "fetch_latest_code"):
        _do_login(context, page, auth)

    assert not fake_storage.exists()
    events = [e for e in syslog_to_tmp()
              if e["event"] == "login_storage_cleared"]
    assert len(events) == 1
    assert events[0]["file_cleared"] is True


def test_do_login_tolerates_missing_storage_state_file(
    monkeypatch, tmp_path, syslog_to_tmp,
):
    # First-ever login: no saved storage state yet. The unlink should
    # skip silently and file_cleared must be False.
    fake_storage = tmp_path / ".uscis_session.json"
    monkeypatch.setattr(uscis_auth, "STORAGE_STATE_PATH", fake_storage)
    assert not fake_storage.exists()

    page = _mock_authed_page()
    page.wait_for_selector.side_effect = [None, PlaywrightTimeout("no mfa")]
    tos = MagicMock(); tos.count.return_value = 0
    page.locator.side_effect = [tos, MagicMock()]
    context = MagicMock()
    auth = {"uscis_email": "e", "uscis_password": "p",
            "uscis_mfa_email": "g", "uscis_mfa_app_password": "a"}

    with patch.object(uscis_auth, "fetch_latest_code"):
        _do_login(context, page, auth)

    events = [e for e in syslog_to_tmp()
              if e["event"] == "login_storage_cleared"]
    assert len(events) == 1
    assert events[0]["cookies_cleared"] is True
    assert events[0]["file_cleared"] is False
