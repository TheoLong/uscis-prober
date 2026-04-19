# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the case-service API wrapper.

Playwright's `Page` / `BrowserContext` are replaced by MagicMocks so tests run
offline without a browser.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from uscis_api import (
    ApiError,
    SessionExpired,
    _parse_case_response,
    fetch_case,
    fetch_case_in_new_tab,
    open_worker_tab,
)


def _tab(body_text="", status=200):
    tab = MagicMock()
    tab.url = "https://my.uscis.gov/account/applicant"
    tab.evaluate.return_value = body_text
    response = MagicMock()
    response.status = status
    tab.goto.return_value = response
    return tab


# -------- ApiError / SessionExpired --------------------------------------

def test_api_error_message_truncates_long_body():
    body = "x" * 1000
    err = ApiError("R1", 500, body)
    assert err.receipt == "R1"
    assert err.status == 500
    # The message truncates to 200 chars.
    assert len(str(err)) < len(body)


def test_session_expired_is_subclass_of_api_error():
    exc = SessionExpired("R1", 401, "oops")
    assert isinstance(exc, ApiError)


# -------- _parse_case_response -------------------------------------------

def test_parse_case_response_401_raises_session_expired():
    tab = _tab(body_text="unauthorized", status=401)
    with pytest.raises(SessionExpired) as exc:
        _parse_case_response(tab, "IOE1", 401)
    assert exc.value.status == 401


def test_parse_case_response_non_200_raises_api_error():
    tab = _tab(body_text="bad request", status=400)
    with pytest.raises(ApiError) as exc:
        _parse_case_response(tab, "IOE1", 400)
    assert exc.value.status == 400
    assert not isinstance(exc.value, SessionExpired)


def test_parse_case_response_empty_body_raises_api_error():
    tab = _tab(body_text="", status=200)
    with pytest.raises(ApiError) as exc:
        _parse_case_response(tab, "IOE1", 200)
    assert "empty body" in str(exc.value)


def test_parse_case_response_non_json_raises_api_error():
    tab = _tab(body_text="<html>not json</html>", status=200)
    with pytest.raises(ApiError) as exc:
        _parse_case_response(tab, "IOE1", 200)
    assert "non-JSON body" in str(exc.value)


def test_parse_case_response_happy_path_returns_dict():
    tab = _tab(body_text='{"formType": "I-485", "status": "ok"}', status=200)
    data = _parse_case_response(tab, "IOE1", 200)
    assert data["formType"] == "I-485"


# -------- open_worker_tab ------------------------------------------------

def test_open_worker_tab_warms_dashboard():
    context = MagicMock()
    tab = _tab()
    context.new_page.return_value = tab
    result = open_worker_tab(context)
    assert result is tab
    tab.goto.assert_called_once()
    # Dashboard URL must be the first argument.
    assert "my.uscis.gov" in tab.goto.call_args.args[0]
    tab.wait_for_timeout.assert_called()


# -------- fetch_case -----------------------------------------------------

def test_fetch_case_returns_parsed_json():
    tab = _tab(body_text='{"receiptNumber": "IOE1", "formType": "I-485"}', status=200)
    data = fetch_case(tab, "IOE1")
    assert data["formType"] == "I-485"
    # URL formatted with the receipt.
    assert "IOE1" in tab.goto.call_args.args[0]


def test_fetch_case_handles_none_response():
    tab = MagicMock()
    tab.goto.return_value = None  # Playwright returned no response.
    tab.evaluate.return_value = ""
    with pytest.raises(ApiError):
        fetch_case(tab, "IOE1")


def test_fetch_case_401_bubbles_session_expired():
    tab = _tab(body_text="denied", status=401)
    with pytest.raises(SessionExpired):
        fetch_case(tab, "IOE1")


# -------- fetch_case_in_new_tab -----------------------------------------

def test_fetch_case_in_new_tab_returns_tab_and_data():
    context = MagicMock()
    tab = _tab(body_text='{"formType": "I-131"}', status=200)
    context.new_page.return_value = tab
    returned_tab, data = fetch_case_in_new_tab(context, "IOE1", "I-131")
    assert returned_tab is tab
    assert data["formType"] == "I-131"
    # Dashboard visited first, then the case endpoint.
    assert tab.goto.call_count == 2
