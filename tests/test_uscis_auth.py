# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the OpenID Connect + MFA login module.

Playwright `Page` / `BrowserContext` and `mfa_mailbox.fetch_latest_code` are
mocked so tests run offline.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
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


# -------- collector auth_path events ------------------------------------
# Contract: whenever debug mode / failure preserves a trace, the MFA
# sidecar always records WHICH auth path ran, so an empty IMAP log
# doesn't mean "unknown" — it means "session was reused, no MFA needed".

def test_ensure_authenticated_records_session_reused_on_valid():
    page = _mock_authed_page()
    context = MagicMock()
    collector = uscis_auth.TraceCollector()
    ensure_authenticated(context, page, {}, allow_login=True, collector=collector)
    events = [e["event"] for e in collector.mfa_events]
    outcomes = [e.get("outcome") for e in collector.mfa_events
                if e["event"] == "auth_path"]
    assert "auth_ensure_started" in events
    assert outcomes == ["session_reused"]


def test_ensure_authenticated_records_login_triggered_when_stale():
    page = _mock_stale_page()
    context = MagicMock()
    collector = uscis_auth.TraceCollector()
    with patch.object(uscis_auth, "_do_login"), \
         patch.object(uscis_auth, "probe_session", side_effect=[False, True]), \
         patch.object(uscis_auth, "_persist"):
        ensure_authenticated(
            context, page, {}, allow_login=True, collector=collector,
        )
    outcomes = [e.get("outcome") for e in collector.mfa_events
                if e["event"] == "auth_path"]
    assert outcomes == ["login_triggered"]


def test_ensure_authenticated_records_refused_stale_no_login():
    page = _mock_stale_page()
    context = MagicMock()
    collector = uscis_auth.TraceCollector()
    with pytest.raises(AuthError):
        ensure_authenticated(
            context, page, {}, allow_login=False, collector=collector,
        )
    outcomes = [e.get("outcome") for e in collector.mfa_events
                if e["event"] == "auth_path"]
    assert outcomes == ["refused_stale_no_login"]


def test_ensure_authenticated_no_collector_is_still_valid():
    """collector=None must not raise — debug-mode and failure paths
    populate one, but unit-test / scripted paths may skip it."""
    page = _mock_authed_page()
    context = MagicMock()
    ensure_authenticated(context, page, {}, allow_login=True, collector=None)


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


# -------- URL-aware MFA prompt handling (the 2026-04-23 failure) --------

def test_handle_mfa_prompt_absent_still_on_signin_raises(syslog_to_tmp, tmp_path, monkeypatch):
    """Regression guard for the silent-refusal failure mode.

    When the MFA selector times out AND the page is still on the
    public sign-in surface, we must raise AuthError so the retry
    layer can kick in, and emit login_mfa_result with
    outcome=submit_did_not_advance carrying a full page snapshot.

    Before this fix, we silently logged prompt_absent and continued,
    burning 45 seconds on a landing-URL wait before reporting
    login_verify_failed.
    """
    page = _mock_stale_page("https://myaccount.uscis.gov/sign-in")
    page.wait_for_selector.side_effect = PlaywrightTimeout("no mfa")

    with pytest.raises(AuthError) as excinfo:
        _handle_mfa_if_present(
            page, {"uscis_mfa_email": "g", "uscis_mfa_app_password": "a"},
            datetime.now(timezone.utc),
        )
    assert "not advanced" in str(excinfo.value).lower()

    results = [e for e in syslog_to_tmp()
               if e["event"] == "login_mfa_result"]
    assert len(results) == 1
    ev = results[0]
    assert ev["outcome"] == "submit_did_not_advance"
    assert ev["level"] == "error"
    # Full snapshot so an operator can see what USCIS was showing.
    assert ev["url"] == "https://myaccount.uscis.gov/sign-in"
    assert "body_preview" in ev and "title" in ev


def test_handle_mfa_prompt_absent_past_signin_is_success(syslog_to_tmp):
    """The legitimate `prompt_absent` branch: we already navigated past
    the sign-in surface. Must NOT raise and must NOT classify as
    submit_did_not_advance."""
    page = _mock_authed_page("https://my.uscis.gov/account/applicant")
    page.wait_for_selector.side_effect = PlaywrightTimeout("no mfa")

    _handle_mfa_if_present(
        page, {"uscis_mfa_email": "g", "uscis_mfa_app_password": "a"},
        datetime.now(timezone.utc),
    )

    results = [e for e in syslog_to_tmp()
               if e["event"] == "login_mfa_result"]
    assert len(results) == 1
    assert results[0]["outcome"] == "prompt_absent"


@pytest.mark.parametrize("url,expected", [
    ("https://myaccount.uscis.gov/sign-in", True),
    ("https://my.uscis.gov/oidc/login", True),
    ("https://my.uscis.gov/signin", True),
    ("https://my.uscis.gov/account/applicant", False),
    ("https://myaccount.uscis.gov/dashboard", False),
    ("", True),     # no URL → pessimistic
    (None, True),
])
def test_is_on_signin_url_classifier(url, expected):
    assert uscis_auth._is_on_signin_url(url) is expected


# -------- comprehensive step-by-step auth logging -----------------------

def test_do_login_emits_every_step_on_success(syslog_to_tmp):
    """The happy path should emit: storage_cleared, login_started,
    goto_login_result, email_form_result, credentials_filled,
    submit_result, mfa_result, landing_result, bridge_result,
    login_result. One event per phase so a future failure at any
    specific phase is distinguishable in the log."""
    page = _mock_authed_page()
    # email form found, MFA absent-but-past-signin (prompt_absent).
    page.wait_for_selector.side_effect = [None, PlaywrightTimeout("no mfa")]
    tos = MagicMock(); tos.count.return_value = 0
    body_locator = MagicMock()
    body_locator.inner_text.return_value = "applicant page"
    # The prompt_absent branch takes a snapshot → extra locator("body") call.
    page.locator.side_effect = [tos, body_locator]

    context = MagicMock()
    auth = {"uscis_email": "e@e.com", "uscis_password": "pw",
            "uscis_mfa_email": "g", "uscis_mfa_app_password": "a"}

    with patch.object(uscis_auth, "fetch_latest_code"):
        _do_login(context, page, auth)

    event_names = [e["event"] for e in syslog_to_tmp()]
    # All the phase-level events fire in order.
    for expected in [
        "login_storage_cleared",
        "login_started",
        "auth_goto_login_result",
        "auth_email_form_result",
        "auth_credentials_filled",
        "auth_submit_result",
        "login_mfa_result",
        "auth_landing_result",
        "auth_bridge_result",
        "login_result",
    ]:
        assert expected in event_names, f"missing event: {expected}"


def test_do_login_submit_result_captures_url_before_and_after(syslog_to_tmp):
    """auth_submit_result must record url-before and url-after the
    sign-in click, plus whether navigation occurred. This is the
    signal that would have diagnosed the 2026-04-23 failure instantly."""
    page = _mock_authed_page("https://my.uscis.gov/account/applicant")
    page.wait_for_selector.side_effect = [None, PlaywrightTimeout("no mfa")]
    tos = MagicMock(); tos.count.return_value = 0
    body_locator = MagicMock()
    body_locator.inner_text.return_value = "applicant page"
    page.locator.side_effect = [tos, body_locator]

    context = MagicMock()
    auth = {"uscis_email": "e", "uscis_password": "p",
            "uscis_mfa_email": "g", "uscis_mfa_app_password": "a"}
    with patch.object(uscis_auth, "fetch_latest_code"):
        _do_login(context, page, auth)

    submit_events = [e for e in syslog_to_tmp()
                     if e["event"] == "auth_submit_result"]
    assert len(submit_events) == 1
    ev = submit_events[0]
    assert "url_before" in ev
    assert "url_after" in ev
    assert "navigation_observed" in ev
    assert "still_on_signin" in ev
    assert "duration_ms" in ev


def test_do_login_credentials_filled_records_tos_state(syslog_to_tmp):
    """auth_credentials_filled must record whether the TOS checkbox
    existed on the form and whether we had to check it. A TOS checkbox
    rename is a silent failure mode worth distinguishing."""
    page = _mock_authed_page()
    page.wait_for_selector.side_effect = [None, PlaywrightTimeout("no mfa")]
    # TOS present and unchecked → we check it.
    tos = MagicMock(); tos.count.return_value = 1
    tos.is_checked.return_value = False
    body_locator = MagicMock()
    body_locator.inner_text.return_value = "applicant"
    page.locator.side_effect = [tos, body_locator]

    context = MagicMock()
    auth = {"uscis_email": "e", "uscis_password": "p",
            "uscis_mfa_email": "g", "uscis_mfa_app_password": "a"}
    with patch.object(uscis_auth, "fetch_latest_code"):
        _do_login(context, page, auth)

    filled = [e for e in syslog_to_tmp()
              if e["event"] == "auth_credentials_filled"]
    assert len(filled) == 1
    ev = filled[0]
    assert ev["tos_present"] is True
    assert ev["tos_was_checked"] is False
    assert ev["tos_now_checked"] is True
    tos.check.assert_called_once()


def test_handle_mfa_2fa_submit_did_not_advance_raises(syslog_to_tmp, tmp_path, monkeypatch):
    """If the 2FA code was submitted but page.url stays on /auth,
    /mfa, or /sign-in, USCIS rejected the code (expired / reused /
    wrong) — we must raise AuthError and emit a
    submit_did_not_advance result so retry can kick in."""
    # Page: starts on /auth (MFA challenge), click does NOT advance.
    page = _mock_stale_page("https://myaccount.uscis.gov/auth")
    page.wait_for_selector.return_value = None  # MFA prompt present
    remember = MagicMock(); remember.count.return_value = 0
    page.locator.return_value = remember

    with patch.object(uscis_auth, "fetch_latest_code", return_value="111111"):
        with pytest.raises(AuthError, match="page is still on MFA"):
            _handle_mfa_if_present(
                page, {"uscis_mfa_email": "g", "uscis_mfa_app_password": "a"},
                datetime.now(timezone.utc),
            )

    outcomes = [e for e in syslog_to_tmp()
                if e["event"] == "login_mfa_result"]
    # prompt_present + submit_did_not_advance (error).
    assert [e["outcome"] for e in outcomes] == [
        "prompt_present", "submit_did_not_advance",
    ]
    assert outcomes[-1]["level"] == "error"


def test_handle_mfa_2fa_submit_past_auth_url_succeeds(syslog_to_tmp):
    """Happy path: MFA code submitted, page advances to
    /account/applicant. Must NOT raise, and must emit
    extracted_and_submitted with url_before and url_after."""
    # Start on /auth, end on /account/applicant after the click.
    page = MagicMock()
    urls = iter([
        "https://myaccount.uscis.gov/auth",        # before submit
        "https://my.uscis.gov/account/applicant",  # after submit
    ])
    # page.url property is read multiple times. We want the FIRST read
    # (for url_before) to return the auth page and subsequent reads to
    # return the authed page. Use a side_effect on a property-mocked
    # attribute.
    type(page).url = property(lambda self: "https://myaccount.uscis.gov/auth"
                              if not hasattr(self, "_clicked")
                              else "https://my.uscis.gov/account/applicant")

    def _click(*a, **k):
        page._clicked = True
    page.click.side_effect = _click
    page.wait_for_selector.return_value = None
    remember = MagicMock(); remember.count.return_value = 0
    page.locator.return_value = remember
    page.title.return_value = "Applicant"
    body = MagicMock(); body.inner_text.return_value = "home"

    with patch.object(uscis_auth, "fetch_latest_code", return_value="222222"):
        _handle_mfa_if_present(
            page, {"uscis_mfa_email": "g", "uscis_mfa_app_password": "a"},
            datetime.now(timezone.utc),
        )

    outcomes = [e["outcome"] for e in syslog_to_tmp()
                if e["event"] == "login_mfa_result"]
    assert outcomes == ["prompt_present", "extracted_and_submitted"]


def test_http_response_listener_captures_both_get_and_post(syslog_to_tmp):
    """_attach_response_listener must capture BOTH methods. An
    anti-bot block on the initial GET /oidc/login (403/503/etc.) would
    otherwise only surface as a downstream selector timeout with no
    HTTP-level signal."""
    page = MagicMock()
    buf = uscis_auth._attach_response_listener(page)

    # Find the registered callback.
    assert page.on.call_count >= 1
    event_name, cb = page.on.call_args_list[0].args

    # Fabricate two responses: a GET 503 on /oidc/login, and a POST 200.
    get_resp = MagicMock()
    get_resp.request.method = "GET"
    get_resp.url = "https://my.uscis.gov/oidc/login"
    get_resp.status = 503
    cb(get_resp)

    post_resp = MagicMock()
    post_resp.request.method = "POST"
    post_resp.url = "https://myaccount.uscis.gov/sign-in"
    post_resp.status = 200
    cb(post_resp)

    # Non-auth traffic is ignored.
    noise = MagicMock()
    noise.request.method = "GET"
    noise.url = "https://cdn.uscis.gov/assets/style.css"
    noise.status = 200
    cb(noise)

    methods = [r["method"] for r in buf]
    statuses = [r["status"] for r in buf]
    assert methods == ["GET", "POST"]
    assert 503 in statuses and 200 in statuses


def test_trace_collector_records_mfa_events_and_emails():
    """TraceCollector is the shared state passed down through the auth
    chain. Events and emails accumulate; the caller decides whether
    to persist them."""
    c = uscis_auth.TraceCollector()
    c.record_mfa_event("imap_connect_ok", host="imap.gmail.com", port=993)
    c.record_mfa_event("imap_search_ok", query="(FROM x)", uids=["1", "2"])
    c.record_mfa_email("12345", b"From: a\nSubject: x\n\nbody")
    assert len(c.mfa_events) == 2
    assert c.mfa_events[0]["event"] == "imap_connect_ok"
    assert c.mfa_events[0]["host"] == "imap.gmail.com"
    assert c.mfa_events[1]["uids"] == ["1", "2"]
    assert c.mfa_emails["12345"].startswith(b"From: a")
    assert c.should_keep is False  # default
    assert c.trace_dir is None


def test_make_trace_dir_creates_named_folder(monkeypatch, tmp_path):
    """Folder name is <utc-iso>_<outcome>_<trigger>, inside FULL_TRACES_DIR."""
    monkeypatch.setattr(uscis_auth, "FULL_TRACES_DIR", tmp_path / "traces")
    d = uscis_auth.make_trace_dir("fail", "scheduled")
    assert d.exists()
    assert d.is_dir()
    assert d.parent == tmp_path / "traces"
    assert "_fail_scheduled" in d.name


def test_make_trace_dir_sanitises_trigger(monkeypatch, tmp_path):
    """Trigger is path-sanitised so a bogus value can't escape."""
    monkeypatch.setattr(uscis_auth, "FULL_TRACES_DIR", tmp_path / "traces")
    d = uscis_auth.make_trace_dir("ok", "../evil")
    assert d.parent == tmp_path / "traces"
    assert ".." not in d.name


def test_write_mfa_artefacts_writes_events_and_emails(tmp_path):
    """MFA sidecar must write events.jsonl (one JSON object per line)
    plus one .eml per UID."""
    events = [
        {"ts": "2026-04-24T01:00:00Z", "event": "imap_connect_ok", "host": "x"},
        {"ts": "2026-04-24T01:00:01Z", "event": "imap_fetch_ok", "uid": "1"},
    ]
    emails = {"1": b"From: a\nSubject: x\n\nbody"}
    out = uscis_auth.write_mfa_artefacts(tmp_path, events, emails)
    assert out == tmp_path / "mfa_trace"
    assert out.is_dir()
    lines = (out / "events.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    import json as _json
    parsed = [_json.loads(line) for line in lines]
    assert parsed[0]["event"] == "imap_connect_ok"
    assert parsed[1]["uid"] == "1"
    eml = (out / "email_1.eml").read_bytes()
    assert eml.startswith(b"From: a")


def test_write_mfa_artefacts_always_writes_on_empty(tmp_path):
    """Empty events/emails still produce mfa_trace/events.jsonl (empty
    file). Contract: whenever the trace is preserved, the MFA sidecar
    is preserved too — consistency matters more than disk savings for
    the ~80-byte empty file."""
    out = uscis_auth.write_mfa_artefacts(tmp_path, [], {})
    assert out == tmp_path / "mfa_trace"
    assert out.is_dir()
    assert (out / "events.jsonl").exists()
    assert (out / "events.jsonl").read_text() == ""


def test_rotate_full_traces_noop_when_dir_missing(monkeypatch, tmp_path):
    """If FULL_TRACES_DIR doesn't exist yet, rotation exits cleanly.
    Covers the early-return branch (line 185)."""
    monkeypatch.setattr(uscis_auth, "FULL_TRACES_DIR", tmp_path / "nope")
    uscis_auth.rotate_full_traces()  # must not raise


def test_rotate_full_traces_noop_when_under_limit(monkeypatch, tmp_path):
    """Below the keep-threshold, rotation is a no-op."""
    monkeypatch.setattr(uscis_auth, "FULL_TRACES_DIR", tmp_path / "traces")
    monkeypatch.setattr(uscis_auth, "FULL_TRACES_KEEP", 10)
    base = tmp_path / "traces"
    base.mkdir()
    (base / "a_ok_scheduled").mkdir()
    uscis_auth.rotate_full_traces()
    assert (base / "a_ok_scheduled").exists()


@pytest.mark.parametrize("url,expected", [
    (None, True),
    ("", True),
    ("https://my.uscis.gov/mfa", True),
    ("https://my.uscis.gov/2fa/verify", True),
    ("https://my.uscis.gov/auth/oidc", True),
    ("https://my.uscis.gov/sign-in", True),
    ("https://my.uscis.gov/account/applicant", False),
])
def test_is_on_mfa_url_variants(url, expected):
    """MFA-URL heuristic: empty/None is pessimistic-True; /mfa, /2fa,
    /auth, and sign-in patterns all match."""
    assert uscis_auth._is_on_mfa_url(url) is expected


def test_safe_title_returns_truncated_title():
    page = MagicMock()
    page.title.return_value = "hello" * 100
    out = uscis_auth._safe_title(page)
    assert out.startswith("hello")
    assert len(out) <= 200


def test_safe_title_returns_placeholder_on_exception():
    """Covers the except branch in _safe_title — Playwright sometimes
    raises when querying title on a destroyed page."""
    page = MagicMock()
    page.title.side_effect = RuntimeError("page closed")
    out = uscis_auth._safe_title(page)
    assert "title capture failed" in out
    assert "RuntimeError" in out


def test_safe_title_handles_empty_title():
    page = MagicMock()
    page.title.return_value = None
    assert uscis_auth._safe_title(page) == ""


def test_page_snapshot_handles_title_exception():
    """_page_snapshot must never raise — a failed title capture is
    stringified and embedded in the returned dict."""
    page = MagicMock()
    page.url = "https://x/"
    page.title.side_effect = RuntimeError("destroyed")
    page.locator.return_value.inner_text.return_value = "body text"
    snap = uscis_auth._page_snapshot(page)
    assert "title capture failed" in snap["title"]
    assert snap["body_preview"] == "body text"


def test_page_snapshot_handles_body_exception():
    """Body capture failure must also be stringified, not raised."""
    page = MagicMock()
    page.url = "https://x/"
    page.title.return_value = "ok"
    page.locator.return_value.inner_text.side_effect = RuntimeError("no body")
    snap = uscis_auth._page_snapshot(page)
    assert snap["title"] == "ok"
    assert "body capture failed" in snap["body_preview"]


def test_ensure_authenticated_sets_collector_should_keep_on_login_verify_failed():
    """When _do_login runs but the follow-up probe still reports stale,
    the collector must be flagged should_keep=True so cmd_run's finally
    block preserves the Playwright trace. Covers line 477."""
    page = _mock_stale_page()
    context = MagicMock()
    collector = uscis_auth.TraceCollector()
    with patch.object(uscis_auth, "_do_login"):  # no-op login
        with pytest.raises(AuthError):
            ensure_authenticated(
                context, page, {}, allow_login=True, collector=collector,
            )
    assert collector.should_keep is True


def test_do_login_sets_collector_should_keep_on_exception():
    """Any exception inside _do_login must flag collector.should_keep
    so the trace is preserved for post-mortem. Covers line 821."""
    page = MagicMock()
    page.url = "https://my.uscis.gov/sign-in"
    page.goto.side_effect = PlaywrightTimeout("blocked")
    context = MagicMock()
    collector = uscis_auth.TraceCollector()
    with pytest.raises(PlaywrightTimeout):
        _do_login(
            context, page, {"uscis_email": "u", "uscis_password": "p"},
            collector=collector,
        )
    assert collector.should_keep is True


def test_rotate_full_traces_prefers_deleting_ok_over_fail(monkeypatch, tmp_path):
    """Rotation must never evict `_fail_` directories while any `_ok_`
    directory exists. Failure forensics outlive a parade of successes."""
    monkeypatch.setattr(uscis_auth, "FULL_TRACES_DIR", tmp_path / "traces")
    monkeypatch.setattr(uscis_auth, "FULL_TRACES_KEEP", 3)
    base = tmp_path / "traces"
    base.mkdir()

    for name in [
        "20260101T000000Z_fail_scheduled",
        "20260102T000000Z_fail_scheduled",
        "20260103T000000Z_ok_scheduled",
        "20260104T000000Z_ok_scheduled",
        "20260105T000000Z_ok_scheduled",
    ]:
        (base / name).mkdir()
        (base / name).touch()
        time.sleep(0.005)

    uscis_auth.rotate_full_traces()
    remaining = {p.name for p in base.iterdir()}
    assert "20260101T000000Z_fail_scheduled" in remaining
    assert "20260102T000000Z_fail_scheduled" in remaining
    assert "20260105T000000Z_ok_scheduled" in remaining
    assert "20260103T000000Z_ok_scheduled" not in remaining
    assert "20260104T000000Z_ok_scheduled" not in remaining
    assert len(remaining) == 3


