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
    fetch_location,
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


# -------- system log instrumentation ------------------------------------

@pytest.fixture
def syslog_to_tmp(monkeypatch, tmp_path):
    import system_log
    monkeypatch.setattr(system_log, "LOG_PATH", tmp_path / "system_log.json")
    system_log.clear()
    return lambda: system_log.read_all()


def test_parse_case_response_non_200_emits_sys_log(syslog_to_tmp):
    tab = _tab(body_text="service unavailable", status=503)
    with pytest.raises(ApiError):
        _parse_case_response(tab, "IOE1", 503)
    events = [e for e in syslog_to_tmp() if e["event"] == "api_fetch_non_200"]
    assert len(events) == 1
    assert events[0]["status"] == 503
    assert events[0]["receipt"] == "IOE1"
    assert "service unavailable" in events[0]["body_preview"]


def test_parse_case_response_empty_body_emits_sys_log(syslog_to_tmp):
    tab = _tab(body_text="", status=200)
    with pytest.raises(ApiError):
        _parse_case_response(tab, "IOE1", 200)
    events = [e for e in syslog_to_tmp() if e["event"] == "api_fetch_empty_body"]
    assert len(events) == 1


def test_parse_case_response_bad_json_emits_sys_log(syslog_to_tmp):
    tab = _tab(body_text="not json at all", status=200)
    with pytest.raises(ApiError):
        _parse_case_response(tab, "IOE1", 200)
    events = [e for e in syslog_to_tmp() if e["event"] == "api_fetch_bad_json"]
    assert len(events) == 1
    assert "not json" in events[0]["body_preview"]


def test_parse_case_response_401_does_not_spam_sys_log(syslog_to_tmp):
    # SessionExpired is already categorised upstream as
    # case_fetch_session_expired — we must NOT also emit an api_*
    # event so the same failure isn't double-counted in dashboards.
    tab = _tab(body_text="denied", status=401)
    with pytest.raises(SessionExpired):
        _parse_case_response(tab, "IOE1", 401)
    assert syslog_to_tmp() == []


def test_fetch_case_nav_failure_emits_sys_log(syslog_to_tmp):
    from playwright.sync_api import Error as PlaywrightError
    tab = _tab()
    tab.goto.side_effect = PlaywrightError("net::ERR_CONNECTION_RESET")
    with pytest.raises(PlaywrightError):
        fetch_case(tab, "IOE1")
    events = [e for e in syslog_to_tmp() if e["event"] == "api_fetch_nav_failed"]
    assert len(events) == 1
    assert events[0]["receipt"] == "IOE1"
    assert "ERR_CONNECTION_RESET" in events[0]["error"]


def test_fetch_location_returns_parsed_envelope_with_data():
    body = '{"data": {"form": "I-765", "location": "SCD", "subtype": "147-C9"}}'
    tab = _tab(body_text=body, status=200)
    out = fetch_location(tab, "IOE1")
    assert out["data"]["location"] == "SCD"
    # Request hit the location endpoint, not the case endpoint.
    assert "receipt_info/IOE1" in tab.goto.call_args.args[0]


def test_fetch_location_returns_null_data_envelope():
    # USCIS commonly returns {"data": null} for I-485 / I-131 pre-assignment.
    tab = _tab(body_text='{"data": null}', status=200)
    out = fetch_location(tab, "IOE1")
    assert out == {"data": None}


def test_fetch_location_401_bubbles_session_expired():
    tab = _tab(body_text="denied", status=401)
    with pytest.raises(SessionExpired):
        fetch_location(tab, "IOE1")


def test_fetch_location_non_200_raises_api_error(syslog_to_tmp):
    tab = _tab(body_text="service unavailable", status=503)
    with pytest.raises(ApiError):
        fetch_location(tab, "IOE1")
    events = [e for e in syslog_to_tmp() if e["event"] == "api_location_non_200"]
    assert len(events) == 1 and events[0]["status"] == 503


def test_fetch_location_empty_body_raises(syslog_to_tmp):
    tab = _tab(body_text="", status=200)
    with pytest.raises(ApiError):
        fetch_location(tab, "IOE1")
    events = [e for e in syslog_to_tmp() if e["event"] == "api_location_empty_body"]
    assert len(events) == 1


def test_fetch_location_bad_json_raises(syslog_to_tmp):
    tab = _tab(body_text="<html>gateway timeout</html>", status=200)
    with pytest.raises(ApiError):
        fetch_location(tab, "IOE1")
    events = [e for e in syslog_to_tmp() if e["event"] == "api_location_bad_json"]
    assert len(events) == 1


def test_fetch_location_nav_failure_emits_sys_log(syslog_to_tmp):
    from playwright.sync_api import Error as PlaywrightError
    tab = _tab()
    tab.goto.side_effect = PlaywrightError("net::ERR_CONNECTION_RESET")
    with pytest.raises(PlaywrightError):
        fetch_location(tab, "IOE1")
    events = [e for e in syslog_to_tmp() if e["event"] == "api_location_nav_failed"]
    assert len(events) == 1


def test_open_worker_tab_dashboard_timeout_emits_sys_log(syslog_to_tmp):
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    context = MagicMock()
    tab = _tab()
    tab.goto.side_effect = PlaywrightTimeout("dashboard too slow")
    context.new_page.return_value = tab
    with pytest.raises(PlaywrightTimeout):
        open_worker_tab(context)
    events = [e for e in syslog_to_tmp() if e["event"] == "api_worker_tab_failed"]
    assert len(events) == 1
    assert events[0]["phase"] == "dashboard_goto"
    assert "dashboard too slow" in events[0]["error"]
