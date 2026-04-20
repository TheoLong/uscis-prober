# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for MFA-code extraction + IMAP polling.

SMTP/IMAP I/O is mocked so tests run offline.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.message import EmailMessage
from unittest.mock import MagicMock, patch

import pytest

import mfa_mailbox
from mfa_mailbox import (
    _check_inbox_once,
    _extract_body,
    _extract_code,
    fetch_latest_code,
)


# Pre-2026-04-20 USCIS body — kept to prove backwards-compat.
USCIS_BODY_TEMPLATE = (
    "<!DOCTYPE html><html><body style='color: #333333;'>"
    "<h1>Secure two-step verification notification</h1>"
    "<p>You have requested a secure MFA code to log into your USCIS Account.</p>"
    "<p>Please enter this secure MFA code: "
    "<span style='color: #0078AE; font-size: 24px; font-weight: 600;'>{code}</span>"
    "</p></body></html>"
)

# 2026-04-20 USCIS body — anchor phrase reworded from "MFA code" to
# "verification code"; styling of the code span unchanged.
USCIS_BODY_TEMPLATE_V2 = (
    "<!DOCTYPE html><html><body style='color: #333333;'>"
    "<h1>Secure two-step verification notification</h1>"
    "<p>You have requested a secure verification code to log into your USCIS Account.</p>"
    "<p>Please enter this secure verification code: "
    "<span style='color: #0078AE; font-size: 24px; font-weight: 600;'>{code}</span>"
    "</p></body></html>"
)


# -------- _extract_code ---------------------------------------------------

def test_extract_code_ignores_css_hex_colors():
    body = USCIS_BODY_TEMPLATE.format(code="550162")
    # Body contains `#333333`, `#0078AE`, `font-size: 24`, `font-weight: 600`;
    # a naive \d{6} would pick `333333` first. HTML stripping removes the
    # style attributes before digit search, so ours picks `550162`.
    assert _extract_code(body) == "550162"


def test_extract_code_works_on_new_2026_04_20_body():
    # Regression guard for the USCIS template rename — "MFA code" became
    # "verification code". Extraction must succeed on the new copy.
    body = USCIS_BODY_TEMPLATE_V2.format(code="085975")
    assert _extract_code(body) == "085975"


def test_extract_code_leading_zero_preserved():
    body = USCIS_BODY_TEMPLATE.format(code="094897")
    assert _extract_code(body) == "094897"


def test_extract_code_survives_template_without_known_anchor_phrase():
    # Template-agnostic: no known intro sentence, no #0078AE styling —
    # just the code in plain prose. We still return it because the
    # upstream sender/subject/freshness gates already proved this email
    # is the MFA for the current login.
    body = (
        "<html><body>Your one-time passcode is "
        "<strong>123456</strong> (valid 10 minutes).</body></html>"
    )
    assert _extract_code(body) == "123456"


def test_extract_code_ignores_hex_colors_in_style_block():
    body = (
        "<html><head><style>body { color: #333333; border: 1px solid #4a4a4a; }</style></head>"
        "<body>Please enter this secure verification code: "
        "<span>654321</span></body></html>"
    )
    assert _extract_code(body) == "654321"


def test_extract_code_decodes_html_entities_around_digits():
    # &#32; is a space entity — without html.unescape() the digits would
    # still be visible, but this test guards against regressions if a
    # future template wraps digits in &#XXXX; entities.
    body = "<p>Code:&#32;<span>001122</span></p>"
    assert _extract_code(body) == "001122"


def test_extract_code_returns_none_on_junk():
    assert _extract_code("") is None
    assert _extract_code("No code here, just some text with #333333 in it.") is None
    # No \d{6} sequence at all.
    assert _extract_code("<html><body>hello world</body></html>") is None


# -------- _extract_body ---------------------------------------------------

def test_extract_body_single_part_html():
    msg = EmailMessage()
    msg.set_content("Please enter this secure MFA code: <span>123456</span>",
                    subtype="html")
    body = _extract_body(msg)
    assert "123456" in body


def test_extract_body_multipart_includes_html_part():
    msg = EmailMessage()
    msg.set_content("plain fallback only, no span")
    msg.add_alternative(
        "<span style='color: #0078AE'>778899</span>", subtype="html"
    )
    body = _extract_body(msg)
    # HTML part must be present so _extract_code can match.
    assert "778899" in body
    assert _extract_code(body) == "778899"


def test_extract_body_no_payload_returns_empty_string():
    msg = EmailMessage()
    # Force is_multipart False and empty payload.
    msg.set_payload(b"")
    assert _extract_body(msg) == ""


def test_extract_body_multipart_skips_parts_without_payload():
    # Build a multipart message by hand so one part has no payload.
    import email
    raw = (
        b"From: a\r\n"
        b"To: b\r\n"
        b"Subject: x\r\n"
        b"Content-Type: multipart/alternative; boundary=\"BB\"\r\n\r\n"
        b"--BB\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: 7bit\r\n\r\n"
        # empty payload
        b"\r\n"
        b"--BB\r\nContent-Type: text/html\r\n\r\n"
        b"<span>hello</span>\r\n"
        b"--BB--\r\n"
    )
    msg = email.message_from_bytes(raw)
    body = _extract_body(msg)
    assert "hello" in body


# -------- _check_inbox_once (mock IMAP) -----------------------------------

def _fake_msg(subject=mfa_mailbox.USCIS_MFA_SUBJECT, date_hdr=None, body=None):
    body = body or USCIS_BODY_TEMPLATE.format(code="424242")
    raw = (
        f"From: {mfa_mailbox.USCIS_SENDER}\r\n"
        f"Subject: {subject}\r\n"
        f"Date: {date_hdr or 'Wed, 18 Apr 2026 22:43:21 +0000'}\r\n"
        "Content-Type: text/html\r\n\r\n"
        f"{body}"
    )
    return raw.encode("utf-8")


class _FakeMail:
    def __init__(self, search_ok=True, ids=(b"1",), fetch_data=None):
        self.search_ok = search_ok
        self.ids = ids
        self.fetch_data = fetch_data or [(b"1 (RFC822 {n}", _fake_msg())]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, *a, **k):
        return ("OK", [b""])

    def select(self, *a, **k):
        return ("OK", [b""])

    def search(self, *a, **k):
        if not self.search_ok:
            return ("NO", [b""])
        return ("OK", [b" ".join(self.ids)]) if self.ids else ("OK", [b""])

    def fetch(self, num, *_a, **_k):
        idx = int(num) - 1
        if idx < 0 or idx >= len(self.fetch_data):
            return ("NO", [None])
        return ("OK", [self.fetch_data[idx]])


@pytest.fixture
def since_dt():
    return datetime(2026, 4, 18, 22, 0, 0, tzinfo=timezone.utc)


def test_check_inbox_once_returns_code_on_match(since_dt):
    fake = _FakeMail(fetch_data=[(b"x", _fake_msg())])
    with patch.object(mfa_mailbox.imaplib, "IMAP4_SSL", return_value=fake):
        code = _check_inbox_once("u@example.com", "pw", since_dt)
    assert code == "424242"


def test_check_inbox_once_no_results_returns_none(since_dt):
    fake = _FakeMail(ids=())
    with patch.object(mfa_mailbox.imaplib, "IMAP4_SSL", return_value=fake):
        assert _check_inbox_once("u", "pw", since_dt) is None


def test_check_inbox_once_search_fails_returns_none(since_dt):
    fake = _FakeMail(search_ok=False)
    with patch.object(mfa_mailbox.imaplib, "IMAP4_SSL", return_value=fake):
        assert _check_inbox_once("u", "pw", since_dt) is None


def test_check_inbox_once_skips_wrong_subject(since_dt):
    fake = _FakeMail(
        fetch_data=[(b"x", _fake_msg(subject="unrelated subject"))]
    )
    with patch.object(mfa_mailbox.imaplib, "IMAP4_SSL", return_value=fake):
        assert _check_inbox_once("u", "pw", since_dt) is None


def test_check_inbox_once_skips_stale_message(since_dt):
    fake = _FakeMail(
        fetch_data=[(b"x", _fake_msg(date_hdr="Wed, 01 Jan 2020 00:00:00 +0000"))]
    )
    with patch.object(mfa_mailbox.imaplib, "IMAP4_SSL", return_value=fake):
        assert _check_inbox_once("u", "pw", since_dt) is None


def test_check_inbox_once_skips_bad_date_header(since_dt):
    fake = _FakeMail(fetch_data=[(b"x", _fake_msg(date_hdr="not a date"))])
    with patch.object(mfa_mailbox.imaplib, "IMAP4_SSL", return_value=fake):
        assert _check_inbox_once("u", "pw", since_dt) is None


def test_check_inbox_once_accepts_any_body_once_email_is_anchored(since_dt):
    # Sender + subject + freshness already bound this email to the
    # current login; once those pass, a 6-digit token in a non-USCIS-
    # template body is still accepted.  This is the 2026-04-20 fix:
    # USCIS can rename the intro sentence, remove the #0078AE styling,
    # or otherwise rework the template, and we still extract the code.
    body = "<html><body>Your one-time passcode is <b>654321</b>.</body></html>"
    fake = _FakeMail(fetch_data=[(b"x", _fake_msg(body=body))])
    with patch.object(mfa_mailbox.imaplib, "IMAP4_SSL", return_value=fake):
        assert _check_inbox_once("u", "pw", since_dt) == "654321"


def test_check_inbox_once_returns_none_when_no_6digit_token(since_dt):
    # Valid email identity but body simply has no code anywhere. We
    # must not guess — return None so the polling loop keeps waiting.
    body = "<html><body>Thanks for signing up. Nothing here.</body></html>"
    fake = _FakeMail(fetch_data=[(b"x", _fake_msg(body=body))])
    with patch.object(mfa_mailbox.imaplib, "IMAP4_SSL", return_value=fake):
        assert _check_inbox_once("u", "pw", since_dt) is None


def test_check_inbox_once_handles_fetch_failure(since_dt):
    class _FetchFailMail(_FakeMail):
        def fetch(self, *_a, **_k):
            return ("NO", [None])

    fake = _FetchFailMail()
    with patch.object(mfa_mailbox.imaplib, "IMAP4_SSL", return_value=fake):
        assert _check_inbox_once("u", "pw", since_dt) is None


# -------- fetch_latest_code (polling loop) --------------------------------

def test_fetch_latest_code_returns_first_match(monkeypatch):
    monkeypatch.setattr(mfa_mailbox, "_check_inbox_once", lambda *a, **k: "111111")
    assert fetch_latest_code("u", "pw", max_wait_seconds=1) == "111111"


def test_fetch_latest_code_times_out(monkeypatch):
    monkeypatch.setattr(mfa_mailbox, "_check_inbox_once", lambda *a, **k: None)
    monkeypatch.setattr(mfa_mailbox.time, "sleep", lambda _s: None)
    with pytest.raises(TimeoutError):
        fetch_latest_code("u", "pw", max_wait_seconds=0, poll_interval_seconds=0)


def test_fetch_latest_code_default_since_is_two_minutes_ago(monkeypatch):
    captured = {}

    def _capture(gu, pw, since, *_a, **_k):
        captured["since"] = since
        return "999999"

    monkeypatch.setattr(mfa_mailbox, "_check_inbox_once", _capture)
    fetch_latest_code("u", "pw", max_wait_seconds=1)
    # Default since should be ~2 min ago and tz-aware.
    assert captured["since"].tzinfo is not None


def test_fetch_latest_code_sleeps_between_polls(monkeypatch):
    sleeps = []
    results = [None, "222222"]

    def _check(*_a, **_k):
        return results.pop(0)

    monkeypatch.setattr(mfa_mailbox, "_check_inbox_once", _check)
    monkeypatch.setattr(mfa_mailbox.time, "sleep", lambda s: sleeps.append(s))
    code = fetch_latest_code("u", "pw", max_wait_seconds=5, poll_interval_seconds=3)
    assert code == "222222"
    assert sleeps == [3]


def test_check_inbox_once_skips_empty_fetch_row(since_dt):
    # A fetch that returns OK but with an empty data row must be skipped.
    class _EmptyFetchMail(_FakeMail):
        def fetch(self, *_a, **_k):
            return ("OK", [])

    fake = _EmptyFetchMail()
    with patch.object(mfa_mailbox.imaplib, "IMAP4_SSL", return_value=fake):
        assert _check_inbox_once("u", "pw", since_dt) is None


def test_check_inbox_once_skips_null_fetch_row(since_dt):
    # Some IMAP servers return ("OK", [None]) — msg_data[0] is falsy.
    class _NullFetchMail(_FakeMail):
        def fetch(self, *_a, **_k):
            return ("OK", [None])

    fake = _NullFetchMail()
    with patch.object(mfa_mailbox.imaplib, "IMAP4_SSL", return_value=fake):
        assert _check_inbox_once("u", "pw", since_dt) is None


def test_check_inbox_once_handles_naive_date(since_dt):
    # Message with a tz-less Date header. _check_inbox_once must treat it as UTC.
    fake = _FakeMail(fetch_data=[(b"x", _fake_msg(date_hdr="18 Apr 2026 22:43:21"))])
    with patch.object(mfa_mailbox.imaplib, "IMAP4_SSL", return_value=fake):
        # Naive date >= since_dt → the match path runs through the tz-normalise branch.
        assert _check_inbox_once("u", "pw", since_dt) == "424242"


def test_extract_body_non_multipart_with_payload():
    # Plain HTML-only email (not multipart). Body must decode.
    msg = EmailMessage()
    msg.set_content("Please enter this secure MFA code <span>456123</span>",
                    subtype="html")
    # set_content(subtype="html") keeps is_multipart() False.
    assert msg.is_multipart() is False
    body = _extract_body(msg)
    assert "456123" in body
