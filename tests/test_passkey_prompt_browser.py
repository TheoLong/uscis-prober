# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exercise post-MFA selectors and navigation in Chromium, without USCIS traffic."""

import json
from datetime import datetime, timezone

import pytest
from playwright.sync_api import sync_playwright

import uscis_auth


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.mark.parametrize("mode,delay", [
    ("nudge", 0),
    ("nudge", 3000),
    ("normal", 3000),
    ("hidden", 500),
    ("race", 0),
])
def test_optional_passkey_prompt_in_chromium(browser, monkeypatch, mode, delay):
    html = """<html><body>
<input id="secure-verification-code"><button id="2fa-submit-btn">Submit</button>
<button id="use-passkey">Use existing passkey</button>
<div id="enrollment" hidden><h2>Register Passkey</h2>
<button id="2fa-passkeys-submit-btn">Create Passkey</button>
<button id="2fa-passkeys-skip-btn">Skip</button>
<input type="checkbox" id="passkey-nudge-checkbox">Do not show again for 60 days</div>
<script>
window.actions = [];
const landing = () => history.pushState({}, '', '/dashboard');
document.getElementById('2fa-submit-btn').onclick = () => {
    setTimeout(() => {
        if (MODE === 'nudge' || MODE === 'race') document.getElementById('enrollment').hidden = false;
        else landing();
    }, DELAY);
};
document.getElementById('2fa-passkeys-skip-btn').onclick = () => {
    actions.push('skip');
    document.getElementById('enrollment').hidden = true;
    setTimeout(landing, 100);
};
document.getElementById('2fa-passkeys-submit-btn').onclick = () => actions.push('create');
document.getElementById('passkey-nudge-checkbox').onchange = () => actions.push('preference');
document.getElementById('use-passkey').onclick = () => actions.push('use-passkey');
</script></body></html>""".replace("MODE", json.dumps(mode)).replace("DELAY", str(delay))
    monkeypatch.setattr(uscis_auth, "fetch_latest_code", lambda *a, **kw: "000000")
    with browser.new_context() as context:
        # Route every request so these pages never reach the network.
        context.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html", body=html,
        ))
        page = context.new_page()
        page.goto("https://myaccount.uscis.gov/auth")
        if mode == "race":
            locate = page.locator

            def locator(selector, **kwargs):
                node = locate(selector, **kwargs)
                if selector == '[id="2fa-passkeys-skip-btn"]':
                    is_visible = node.is_visible

                    def navigate_after_visibility(**kw):
                        visible = is_visible(**kw)
                        if visible:
                            node.evaluate("el => { el.remove(); setTimeout(landing, 100); }")
                        return visible

                    monkeypatch.setattr(node, "is_visible", navigate_after_visibility)
                return node

            monkeypatch.setattr(page, "locator", locator)
        uscis_auth._handle_mfa_if_present(
            page,
            {"uscis_mfa_email": "user@example.com", "uscis_mfa_app_password": "test"},
            datetime.now(timezone.utc),
        )
        assert page.url == "https://myaccount.uscis.gov/dashboard"
        assert page.evaluate("actions") == (["skip"] if mode == "nudge" else [])
        assert not page.locator("#passkey-nudge-checkbox").is_checked()


@pytest.mark.parametrize("intermediate", ["/api/fully_signed_in", "/dashboard"])
def test_login_waits_for_redirect_chain_before_bridging(browser, monkeypatch, intermediate):
    monkeypatch.setattr(uscis_auth, "_clear_stored_session", lambda context: None)
    monkeypatch.setattr(uscis_auth, "_handle_mfa_if_present", lambda page, *a, **kw:
                        page.goto("https://myaccount.uscis.gov" + intermediate))
    with browser.new_context() as context:
        def respond(route):
            if route.request.url == "https://myaccount.uscis.gov" + intermediate:
                body = """<script>setTimeout(() => {
                    console.log('natural-landing');
                    location.href = 'https://my.uscis.gov/account/applicant';
                }, 800);</script>"""
            elif "/account/applicant" in route.request.url:
                body = "<h1>Applicant dashboard</h1>"
            else:
                body = """<input id="email-address"><input id="password">
                    <button id="sign-in-btn">Sign In</button>"""
            route.fulfill(status=200, content_type="text/html", body=body)

        context.route("**/*", respond)
        page = context.new_page()
        natural_landings = []
        page.on("console", lambda message: natural_landings.append(message.text))
        forced_landings = []
        goto = page.goto

        def record_goto(url, **kwargs):
            if url == uscis_auth.DASHBOARD_URL:
                forced_landings.append(url)
            return goto(url, **kwargs)

        monkeypatch.setattr(page, "goto", record_goto)
        uscis_auth._do_login(context, page, {
            "uscis_email": "user@example.com", "uscis_password": "test",
        })
        assert page.url == uscis_auth.DASHBOARD_URL
        assert natural_landings == ["natural-landing"]
        assert forced_landings == []
