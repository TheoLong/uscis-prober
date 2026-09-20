# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Post-MFA navigation with optional passkey enrollment."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeout

import uscis_auth


@pytest.fixture
def flow(monkeypatch):
    class Flow:
        elapsed = 0.0
        nudge_at = None
        landing_at = None
        skipped = False
        skip_advances = True
        hidden = False
        clicks = []
        page = MagicMock()

        def __init__(self):
            self.page.url = "https://myaccount.uscis.gov/auth"
            self.page.title.return_value = "Verification Code"
            self.page.locator.side_effect = self.locator
            self.page.click.side_effect = self.click
            self.page.wait_for_timeout.side_effect = self.wait
            self.page.wait_for_url.side_effect = self.wait_for_url

        def locator(self, selector):
            node = MagicMock()
            node.count.return_value = 0
            node.is_visible.return_value = False
            if selector == '[id="2fa-passkeys-skip-btn"]':
                node.count.return_value = int(self.nudge_at is not None)
                node.is_visible.return_value = (
                    self.nudge_at is not None and self.elapsed >= self.nudge_at
                    and not self.hidden and not self.skipped
                )
                node.click.side_effect = lambda **kw: self.click(selector)
            return node

        def click(self, selector, **kwargs):
            self.clicks.append(selector)
            if selector == '[id="2fa-passkeys-skip-btn"]':
                self.skipped = True
                if self.skip_advances:
                    self.landing_at = self.elapsed + 1

        def wait(self, ms):
            self.elapsed += ms / 1000
            if self.landing_at is not None and self.elapsed >= self.landing_at:
                self.page.url = "https://myaccount.uscis.gov/dashboard"

        def wait_for_url(self, predicate, *, timeout, **kwargs):
            self.wait(timeout)
            if not predicate(self.page.url):
                raise PlaywrightTimeout("navigation pending")

        def run(self):
            uscis_auth._handle_mfa_if_present(
                self.page,
                {"uscis_mfa_email": "user@example.com", "uscis_mfa_app_password": "test"},
                datetime.now(timezone.utc),
            )

    f = Flow()
    monkeypatch.setattr(uscis_auth.time, "monotonic", lambda: f.elapsed)
    monkeypatch.setattr(uscis_auth, "fetch_latest_code", lambda *a, **kw: "000000")
    return f


@pytest.mark.parametrize("nudge_at", [0, 4])
def test_optional_enrollment_is_skipped_when_visible(flow, nudge_at):
    flow.nudge_at = nudge_at
    flow.run()
    assert flow.page.url == "https://myaccount.uscis.gov/dashboard"
    assert flow.clicks == ['[id="2fa-submit-btn"]', '[id="2fa-passkeys-skip-btn"]']


@pytest.mark.parametrize("landing_at", [0, 5])
def test_login_without_enrollment_prompt_does_not_click_skip(flow, landing_at):
    flow.landing_at = landing_at
    flow.run()
    assert flow.page.url == "https://myaccount.uscis.gov/dashboard"
    assert flow.clicks == ['[id="2fa-submit-btn"]']


def test_hidden_enrollment_controls_are_untouched(flow):
    flow.nudge_at = 0
    flow.hidden = True
    flow.landing_at = 3
    flow.run()
    assert flow.clicks == ['[id="2fa-submit-btn"]']


def test_existing_passkey_controls_are_untouched(flow):
    original_locator = flow.page.locator.side_effect

    def locator(selector):
        if selector in ('[id="2fa-passkeys-submit-btn"]', "#passkey-nudge-checkbox"):
            pytest.fail("must not enroll a passkey or change account preferences")
        return original_locator(selector)

    flow.page.locator.side_effect = locator
    flow.landing_at = 3
    flow.run()
    assert not flow.skipped


def test_rejected_code_still_fails_with_bounded_wait(flow):
    with pytest.raises(uscis_auth.AuthError, match="page is still on MFA"):
        flow.run()
    assert flow.elapsed <= 46
    assert not flow.skipped


def test_enrollment_can_be_skipped_without_an_mfa_prompt(flow, monkeypatch):
    flow.page.wait_for_selector.side_effect = PlaywrightTimeout("no code prompt")
    flow.nudge_at = 0
    fetch = MagicMock()
    monkeypatch.setattr(uscis_auth, "fetch_latest_code", fetch)
    flow.run()
    fetch.assert_not_called()
    assert flow.page.url == "https://myaccount.uscis.gov/dashboard"
    assert flow.clicks == ['[id="2fa-passkeys-skip-btn"]']


def test_authenticated_login_without_mfa_does_not_touch_passkeys(flow, monkeypatch):
    flow.page.url = "https://myaccount.uscis.gov/dashboard"
    flow.page.wait_for_selector.side_effect = PlaywrightTimeout("no code prompt")
    fetch = MagicMock()
    monkeypatch.setattr(uscis_auth, "fetch_latest_code", fetch)
    flow.run()
    fetch.assert_not_called()
    assert flow.clicks == []


def test_missing_mfa_prompt_does_not_accept_stalled_auth_page(flow):
    flow.page.wait_for_selector.side_effect = PlaywrightTimeout("no code prompt")
    with pytest.raises(uscis_auth.AuthError, match="not advanced"):
        flow.run()
    assert flow.elapsed <= 46


@pytest.mark.parametrize("landing,bridge_required", [
    (uscis_auth.DASHBOARD_URL, False),
    (uscis_auth.DASHBOARD_URL + "/", False),
    ("https://myaccount.uscis.gov/dashboard", True),
])
def test_bridge_does_not_interrupt_direct_dashboard_landing(monkeypatch, landing, bridge_required):
    page = MagicMock()
    page.url = "https://myaccount.uscis.gov/sign-in"
    monkeypatch.setattr(uscis_auth, "_clear_stored_session", lambda context: None)

    def finish_mfa(*args, **kwargs):
        page.url = landing

    monkeypatch.setattr(uscis_auth, "_handle_mfa_if_present", finish_mfa)

    def wait_for_url(target, **kwargs):
        if hasattr(target, "match") and bridge_required:
            raise PlaywrightTimeout("no automatic bridge")

    page.wait_for_url.side_effect = wait_for_url
    uscis_auth._do_login(MagicMock(), page, {
        "uscis_email": "user@example.com", "uscis_password": "test",
    })
    bridge_calls = [c for c in page.goto.call_args_list
                    if c.args[0] == uscis_auth.DASHBOARD_URL]
    assert len(bridge_calls) == int(bridge_required)
    page.wait_for_load_state.assert_any_call("domcontentloaded")


def test_skip_without_navigation_still_fails(flow):
    flow.nudge_at = 0
    flow.skip_advances = False
    with pytest.raises(uscis_auth.AuthError, match="page is still on MFA"):
        flow.run()
    assert flow.skipped
    assert flow.clicks.count('[id="2fa-passkeys-skip-btn"]') == 1
    assert flow.elapsed <= 46
