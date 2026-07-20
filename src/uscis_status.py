# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""USCIS human-readable case-status — isolated from login and the rich API.

The rich case-service API (`uscis_api.fetch_case`) returns machine codes
(`HA`, `FTA1`, `IAF`) with no plain-English meaning. The authenticated
dashboard separately calls

    GET /account/case-service/api/case_status/{receipt}

which returns the same plain-English status a user sees on the public
"Check Case Status" tool — a `statusTitle` headline and a `statusText`
paragraph — plus the `currentActionCode` that maps the cryptic event code
to that English text.

Like `uscis_api`, this module NEVER initiates a login. Each fetch is a
top-level navigation from a tab already warmed at the dashboard, so the
Referer is correct; the JSON body is read from `document.body.innerText`.

This fetch is BEST-EFFORT: a failure here must never fail a pull. The rich
API is the source of truth for events and diffs; the status text is an
additive human-readable layer. Callers catch `StatusFetchError` and move on.
"""

from __future__ import annotations

import json
import logging

from playwright.sync_api import (
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeout,
)

from system_log import log as sys_log

logger = logging.getLogger(__name__)

STATUS_ENDPOINT = (
    "https://my.uscis.gov/account/case-service/api/case_status/{receipt}"
)


class StatusFetchError(RuntimeError):
    """Raised on any non-success while fetching the status endpoint.

    Best-effort by contract: the caller logs and continues, never letting
    a status failure abort the pull.
    """

    def __init__(self, receipt: str, status: int, body: str):
        super().__init__(f"{receipt}: HTTP {status} — {body[:200]}")
        self.receipt = receipt
        self.status = status
        self.body = body


def fetch_case_status(tab: Page, receipt: str) -> dict:
    """Navigate `tab` to the status endpoint and return the parsed JSON.

    `tab` must already be on a my.uscis.gov page so the navigation carries
    the right Referer (see `uscis_api.open_worker_tab`). Returns the full
    response envelope (`{"data": {...}}`) verbatim so downstream storage is
    faithful to what USCIS returned.

    Raises `StatusFetchError` on any transport- or parse-level failure.
    """
    url = STATUS_ENDPOINT.format(receipt=receipt)
    try:
        response = tab.goto(url, wait_until="domcontentloaded", timeout=20_000)
    except (PlaywrightTimeout, PlaywrightError) as e:
        sys_log(
            "api_status_nav_failed", level="warning", source="uscis_status",
            receipt=receipt, target=url,
            error=f"{type(e).__name__}: {e}"[:200],
            final_url=getattr(tab, "url", "") or "",
        )
        raise StatusFetchError(receipt, 0, f"nav failed: {e}")

    status = response.status if response else 0
    try:
        body_text = tab.evaluate("() => document.body.innerText || ''").strip()
    except Exception as e:  # noqa: BLE001 — body read is its own failure surface
        sys_log(
            "api_status_body_read_failed", level="warning", source="uscis_status",
            receipt=receipt, status=status,
            error=f"{type(e).__name__}: {e}"[:200],
        )
        raise StatusFetchError(receipt, status, f"body read failed: {e}")

    if status != 200:
        sys_log(
            "api_status_non_200", level="warning", source="uscis_status",
            receipt=receipt, status=status, body_preview=body_text[:400],
        )
        raise StatusFetchError(receipt, status, body_text)
    if not body_text:
        sys_log(
            "api_status_empty_body", level="warning", source="uscis_status",
            receipt=receipt, status=status,
        )
        raise StatusFetchError(receipt, status, "(empty body)")
    try:
        return json.loads(body_text)
    except json.JSONDecodeError as e:
        sys_log(
            "api_status_bad_json", level="warning", source="uscis_status",
            receipt=receipt, status=status, body_preview=body_text[:400],
            error=f"JSONDecodeError: {e}"[:200],
        )
        raise StatusFetchError(
            receipt, status, f"non-JSON body ({e}): {body_text[:200]}"
        )
