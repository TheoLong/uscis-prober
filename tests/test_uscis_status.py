# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for uscis_status.fetch_case_status.

The fetch is a thin nav-and-parse over the dashboard status endpoint. We
drive it with a fake Playwright tab so no network or browser is needed.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from uscis_status import StatusFetchError, fetch_case_status  # noqa: E402


class _FakeResponse:
    def __init__(self, status):
        self.status = status


class _FakeTab:
    """Minimal Playwright Page stand-in: goto returns a status, evaluate
    returns a canned body."""

    def __init__(self, status, body, *, goto_raises=None):
        self._status = status
        self._body = body
        self._goto_raises = goto_raises
        self.url = "https://my.uscis.gov/account/applicant"

    def goto(self, url, **kwargs):
        if self._goto_raises:
            raise self._goto_raises
        return _FakeResponse(self._status)

    def evaluate(self, _script):
        return self._body


_OK_PAYLOAD = {
    "data": {
        "receiptNumber": "IOE0000000000",
        "formType": "I-485",
        "currentActionCode": "HA",
        "statusTitle": "Response To USCIS' Request For Evidence Was Received",
        "statusText": "On ... we received your response ...",
    }
}


def test_fetch_case_status_returns_parsed_envelope():
    tab = _FakeTab(200, json.dumps(_OK_PAYLOAD))
    out = fetch_case_status(tab, "IOE0000000000")
    assert out == _OK_PAYLOAD
    assert out["data"]["statusTitle"].startswith("Response To USCIS")


def test_fetch_case_status_non_200_raises():
    tab = _FakeTab(500, "server error")
    with pytest.raises(StatusFetchError) as ei:
        fetch_case_status(tab, "IOE0000000000")
    assert ei.value.status == 500


def test_fetch_case_status_empty_body_raises():
    tab = _FakeTab(200, "   ")
    with pytest.raises(StatusFetchError):
        fetch_case_status(tab, "IOE0000000000")


def test_fetch_case_status_bad_json_raises():
    tab = _FakeTab(200, "<html>not json</html>")
    with pytest.raises(StatusFetchError):
        fetch_case_status(tab, "IOE0000000000")


def test_fetch_case_status_nav_failure_raises():
    from playwright.sync_api import Error as PlaywrightError

    tab = _FakeTab(200, "{}", goto_raises=PlaywrightError("nav boom"))
    with pytest.raises(StatusFetchError) as ei:
        fetch_case_status(tab, "IOE0000000000")
    assert ei.value.status == 0
