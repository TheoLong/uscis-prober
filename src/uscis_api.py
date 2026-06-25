# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
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

Diagnostics
-----------
Every failure branch emits a categorised `api_*` sys_log event:
  - `api_worker_tab_failed`: dashboard pre-warm nav failed (no Referer,
    hostile network, etc.).
  - `api_fetch_nav_failed`:  case-endpoint navigation itself crashed
    (timeout, connection reset, ERR_ABORTED, etc.).
  - `api_fetch_non_200`:     server returned a status other than 200.
  - `api_fetch_empty_body`:  status 200 but body was empty.
  - `api_fetch_bad_json`:    status 200 but body wasn't valid JSON.

`SessionExpired` (status 401) is raised without a dedicated error event
because the caller already categorises it as `case_fetch_session_expired`
in `session_fetch._extract_cases`.
"""

from __future__ import annotations

import json
import logging

from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeout,
)

from system_log import log as sys_log

logger = logging.getLogger(__name__)

CASE_ENDPOINT = "https://my.uscis.gov/account/case-service/api/cases/{receipt}"
LOCATION_ENDPOINT = (
    "https://my.uscis.gov/secure-messaging/api/case-service/receipt_info/{receipt}"
)
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
    try:
        tab = context.new_page()
    except Exception as e:
        sys_log(
            "api_worker_tab_failed", level="error", source="uscis_api",
            phase="new_page",
            error=f"{type(e).__name__}: {e}"[:200],
        )
        raise

    try:
        tab.goto(_DASHBOARD_URL, wait_until="domcontentloaded", timeout=30_000)
    except PlaywrightTimeout as e:
        sys_log(
            "api_worker_tab_failed", level="error", source="uscis_api",
            phase="dashboard_goto", target=_DASHBOARD_URL,
            error=f"PlaywrightTimeout: {e}"[:200],
            final_url=getattr(tab, "url", "") or "",
        )
        raise
    except PlaywrightError as e:
        sys_log(
            "api_worker_tab_failed", level="error", source="uscis_api",
            phase="dashboard_goto", target=_DASHBOARD_URL,
            error=f"{type(e).__name__}: {e}"[:200],
            final_url=getattr(tab, "url", "") or "",
        )
        raise

    tab.wait_for_timeout(1000)  # let the SPA settle
    logger.info("Worker tab loaded at %s", tab.url)
    return tab


def _parse_case_response(tab: Page, receipt: str, status: int) -> dict:
    """Read the JSON body from a tab that just navigated to an API URL.

    Every non-success branch emits a sys_log event so the operator can
    distinguish 401 (session expired), 403 (permissions), 500 (USCIS
    server error), and parse-level failures (empty body, non-JSON).
    """
    try:
        body_text = tab.evaluate("() => document.body.innerText || ''").strip()
    except Exception as e:
        sys_log(
            "api_fetch_body_read_failed", level="error", source="uscis_api",
            receipt=receipt, status=status,
            error=f"{type(e).__name__}: {e}"[:200],
        )
        raise ApiError(receipt, status, f"body read failed: {e}")

    if status == 401:
        # SessionExpired carries its own signal to the caller; no
        # dedicated sys_log here since session_fetch logs
        # `case_fetch_session_expired` on the way up.
        raise SessionExpired(receipt, status, body_text)
    if status != 200:
        sys_log(
            "api_fetch_non_200", level="error", source="uscis_api",
            receipt=receipt, status=status,
            body_preview=body_text[:400],
        )
        raise ApiError(receipt, status, body_text)
    if not body_text:
        sys_log(
            "api_fetch_empty_body", level="error", source="uscis_api",
            receipt=receipt, status=status,
        )
        raise ApiError(receipt, status, "(empty body)")
    try:
        return json.loads(body_text)
    except json.JSONDecodeError as e:
        sys_log(
            "api_fetch_bad_json", level="error", source="uscis_api",
            receipt=receipt, status=status,
            body_preview=body_text[:400],
            error=f"JSONDecodeError: {e}"[:200],
        )
        raise ApiError(
            receipt, status, f"non-JSON body ({e}): {body_text[:200]}"
        )


def fetch_case(tab: Page, receipt: str) -> dict:
    """Navigate `tab` to /cases/{id} and return the parsed rich JSON.

    `tab` must already be on a my.uscis.gov page (see `open_worker_tab`)
    so the navigation carries the right Referer.
    """
    url = CASE_ENDPOINT.format(receipt=receipt)
    try:
        response = tab.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except PlaywrightTimeout as e:
        sys_log(
            "api_fetch_nav_failed", level="error", source="uscis_api",
            receipt=receipt, target=url,
            error=f"PlaywrightTimeout: {e}"[:200],
            final_url=getattr(tab, "url", "") or "",
        )
        raise
    except PlaywrightError as e:
        sys_log(
            "api_fetch_nav_failed", level="error", source="uscis_api",
            receipt=receipt, target=url,
            error=f"{type(e).__name__}: {e}"[:200],
            final_url=getattr(tab, "url", "") or "",
        )
        raise

    status = response.status if response else 0
    return _parse_case_response(tab, receipt, status)


def fetch_location(tab: Page, receipt: str) -> dict:
    """Fetch the case location/service-center assignment for `receipt`.

    Hits `/secure-messaging/api/case-service/receipt_info/{receipt}`. This
    endpoint returns EITHER a 200 with `{"data": null}` (USCIS hasn't
    assigned a service center yet — common for I-485 and I-131) OR a 200
    with a populated `{"data": {"form": ..., "location": ..., ...}}`
    (typical for I-765).

    Unlike `fetch_case`, a `null`-data payload is a legitimate, retainable
    snapshot — we store it so the dashboard can show "TBD" and record
    exactly when USCIS starts returning data. Only transport-level errors
    (non-200, bad JSON, empty body) raise.
    """
    url = LOCATION_ENDPOINT.format(receipt=receipt)
    try:
        response = tab.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except PlaywrightTimeout as e:
        sys_log(
            "api_location_nav_failed", level="error", source="uscis_api",
            receipt=receipt, target=url,
            error=f"PlaywrightTimeout: {e}"[:200],
            final_url=getattr(tab, "url", "") or "",
        )
        raise
    except PlaywrightError as e:
        sys_log(
            "api_location_nav_failed", level="error", source="uscis_api",
            receipt=receipt, target=url,
            error=f"{type(e).__name__}: {e}"[:200],
            final_url=getattr(tab, "url", "") or "",
        )
        raise

    status = response.status if response else 0
    try:
        body_text = tab.evaluate("() => document.body.innerText || ''").strip()
    except Exception as e:
        sys_log(
            "api_location_body_read_failed", level="error", source="uscis_api",
            receipt=receipt, status=status,
            error=f"{type(e).__name__}: {e}"[:200],
        )
        raise ApiError(receipt, status, f"body read failed: {e}")

    if status == 401:
        raise SessionExpired(receipt, status, body_text)
    if status != 200:
        sys_log(
            "api_location_non_200", level="error", source="uscis_api",
            receipt=receipt, status=status,
            body_preview=body_text[:400],
        )
        raise ApiError(receipt, status, body_text)
    if not body_text:
        sys_log(
            "api_location_empty_body", level="error", source="uscis_api",
            receipt=receipt, status=status,
        )
        raise ApiError(receipt, status, "(empty body)")
    try:
        return json.loads(body_text)
    except json.JSONDecodeError as e:
        sys_log(
            "api_location_bad_json", level="error", source="uscis_api",
            receipt=receipt, status=status,
            body_preview=body_text[:400],
            error=f"JSONDecodeError: {e}"[:200],
        )
        raise ApiError(
            receipt, status, f"non-JSON body ({e}): {body_text[:200]}"
        )


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
    try:
        tab = context.new_page()
    except Exception as e:
        sys_log(
            "api_fetch_nav_failed", level="error", source="uscis_api",
            receipt=receipt, phase="new_tab",
            error=f"{type(e).__name__}: {e}"[:200],
        )
        raise

    try:
        tab.goto(_DASHBOARD_URL, wait_until="domcontentloaded", timeout=30_000)
    except (PlaywrightTimeout, PlaywrightError) as e:
        sys_log(
            "api_fetch_nav_failed", level="error", source="uscis_api",
            receipt=receipt, phase="dashboard_goto", target=_DASHBOARD_URL,
            error=f"{type(e).__name__}: {e}"[:200],
            final_url=getattr(tab, "url", "") or "",
        )
        raise

    tab.wait_for_timeout(400)
    data = fetch_case(tab, receipt)
    return tab, data
