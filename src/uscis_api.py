"""USCIS case-service API — isolated from login.

Call `fetch_case` with an authenticated Playwright context. This module will
NEVER initiate a login; if the session is stale, callers get an HTTP 401 in
the response, and they are responsible for deciding whether to re-auth.

Implementation: each case fetch is a top-level *navigation* (page.goto) to
`/account/case-service/api/cases/{id}` from a tab whose previous page is
the authenticated dashboard. Chromium renders the `application/json` body
inline, and we parse it out of `document.body.innerText`. Establishing the
dashboard origin first is required — a cold navigation to the API URL
(no Referer, no prior my.uscis.gov navigation) returns 401.

There is a single endpoint by design — no fallback. If `/cases/{id}`
returns non-200 for a specific receipt, that case is reported as a failure
rather than silently downgrading to a summary endpoint.
"""

from __future__ import annotations

import json
import logging

from playwright.sync_api import BrowserContext, Page

logger = logging.getLogger(__name__)

CASE_ENDPOINT = "https://my.uscis.gov/account/case-service/api/cases/{receipt}"
_DASHBOARD_URL = "https://my.uscis.gov/account/applicant"


class ApiError(RuntimeError):
    """Raised when the API returns a non-200 response."""

    def __init__(self, receipt: str, status: int, body: str):
        super().__init__(f"{receipt}: HTTP {status} — {body[:200]}")
        self.receipt = receipt
        self.status = status
        self.body = body


class SessionExpired(ApiError):
    """Raised when the API returns 401 — caller should re-auth."""


def open_worker_tab(context: BrowserContext) -> Page:
    """Open a single long-lived tab, pre-warmed at the authenticated dashboard.

    The dashboard visit is not optional — subsequent navigation to the
    case-service API relies on having my.uscis.gov as the Referer. Reuse
    this tab for every API call in a run.
    """
    tab = context.new_page()
    tab.goto(_DASHBOARD_URL, wait_until="domcontentloaded", timeout=30_000)
    tab.wait_for_timeout(1000)  # let the SPA settle
    logger.info("Worker tab loaded at %s", tab.url)
    return tab


def _parse_case_response(tab: Page, receipt: str, status: int) -> dict:
    """Read the JSON body from a tab that just navigated to an API URL."""
    body_text = tab.evaluate("() => document.body.innerText || ''").strip()

    if status == 401:
        raise SessionExpired(receipt, status, body_text)
    if status != 200:
        raise ApiError(receipt, status, body_text)
    if not body_text:
        raise ApiError(receipt, status, "(empty body)")
    try:
        return json.loads(body_text)
    except json.JSONDecodeError as e:
        raise ApiError(
            receipt, status, f"non-JSON body ({e}): {body_text[:200]}"
        )


def fetch_case(tab: Page, receipt: str) -> dict:
    """Navigate `tab` to /cases/{id} and return the parsed rich JSON.

    `tab` must already be on a my.uscis.gov page (see `open_worker_tab`)
    so the navigation carries the right Referer.
    """
    url = CASE_ENDPOINT.format(receipt=receipt)
    response = tab.goto(url, wait_until="domcontentloaded", timeout=20_000)
    status = response.status if response else 0
    return _parse_case_response(tab, receipt, status)


def fetch_case_in_new_tab(
    context: BrowserContext,
    receipt: str,
    label: str,
) -> tuple[Page, dict]:
    """Keep-alive mode: open a dedicated tab per case, warm to the dashboard,
    then navigate to /cases/{id}. Tab stays open on the JSON response so the
    user can inspect it. Caller owns the tab.

    Returns (tab, case_json).
    """
    tab = context.new_page()
    tab.goto(_DASHBOARD_URL, wait_until="domcontentloaded", timeout=30_000)
    tab.wait_for_timeout(400)
    data = fetch_case(tab, receipt)
    return tab, data
