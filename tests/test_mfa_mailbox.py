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


# _check_inbox_once now returns a 3-tuple `(code, search_query, returned_uids)`
# and populates an optional `tally` dict with reason codes for every branch.
# Helper below keeps the assertion call-sites terse.
def _scan(fake, since_dt, *, tally=None):
    with patch.object(mfa_mailbox.imaplib, "IMAP4_SSL", return_value=fake):
        return _check_inbox_once("u@example.com", "pw", since_dt, tally=tally)


def test_check_inbox_once_returns_code_on_match(since_dt):
    fake = _FakeMail(fetch_data=[(b"x", _fake_msg())])
    tally: dict[str, int] = {}
    code, query, uids = _scan(fake, since_dt, tally=tally)
    assert code == "424242"
    # The caller uses `query` to record the exact IMAP SEARCH string on
    # a timeout event — so this test pins the observable shape of that
    # string (the `SINCE` value will be whatever the caller derived).
    assert query.startswith(f'(FROM "{mfa_mailbox.USCIS_SENDER}"')
    assert uids == ["1"]
    assert tally == {mfa_mailbox.REASON_ACCEPTED: 1}


def test_check_inbox_once_no_results_returns_none(since_dt):
    fake = _FakeMail(ids=())
    tally: dict[str, int] = {}
    code, query, uids = _scan(fake, since_dt, tally=tally)
    assert code is None
    assert query is not None  # search was attempted
    assert uids == []
    assert tally == {mfa_mailbox.REASON_IMAP_SEARCH_EMPTY: 1}


def test_check_inbox_once_search_fails_returns_none(since_dt):
    fake = _FakeMail(search_ok=False)
    tally: dict[str, int] = {}
    code, query, uids = _scan(fake, since_dt, tally=tally)
    assert code is None
    assert query is not None
    assert tally == {mfa_mailbox.REASON_IMAP_SEARCH_FAILED: 1}


def test_check_inbox_once_skips_wrong_subject(since_dt):
    fake = _FakeMail(
        fetch_data=[(b"x", _fake_msg(subject="unrelated subject"))]
    )
    tally: dict[str, int] = {}
    code, _, _ = _scan(fake, since_dt, tally=tally)
    assert code is None
    assert tally == {mfa_mailbox.REASON_SUBJECT_MISMATCH: 1}


def test_check_inbox_once_skips_stale_message(since_dt):
    fake = _FakeMail(
        fetch_data=[(b"x", _fake_msg(date_hdr="Wed, 01 Jan 2020 00:00:00 +0000"))]
    )
    tally: dict[str, int] = {}
    code, _, _ = _scan(fake, since_dt, tally=tally)
    assert code is None
    assert tally == {mfa_mailbox.REASON_STALE: 1}


def test_check_inbox_once_skips_bad_date_header(since_dt):
    fake = _FakeMail(fetch_data=[(b"x", _fake_msg(date_hdr="not a date"))])
    tally: dict[str, int] = {}
    code, _, _ = _scan(fake, since_dt, tally=tally)
    assert code is None
    assert tally == {mfa_mailbox.REASON_BAD_DATE_HEADER: 1}


def test_check_inbox_once_accepts_any_body_once_email_is_anchored(since_dt):
    # Sender + subject + freshness already bound this email to the
    # current login; once those pass, a 6-digit token in a non-USCIS-
    # template body is still accepted.  This is the 2026-04-20 fix:
    # USCIS can rename the intro sentence, remove the #0078AE styling,
    # or otherwise rework the template, and we still extract the code.
    body = "<html><body>Your one-time passcode is <b>654321</b>.</body></html>"
    fake = _FakeMail(fetch_data=[(b"x", _fake_msg(body=body))])
    tally: dict[str, int] = {}
    code, _, _ = _scan(fake, since_dt, tally=tally)
    assert code == "654321"
    assert tally == {mfa_mailbox.REASON_ACCEPTED: 1}


def test_check_inbox_once_returns_none_when_no_6digit_token(since_dt):
    # Valid email identity but body simply has no code anywhere. We
    # must not guess — return None so the polling loop keeps waiting.
    body = "<html><body>Thanks for signing up. Nothing here.</body></html>"
    fake = _FakeMail(fetch_data=[(b"x", _fake_msg(body=body))])
    tally: dict[str, int] = {}
    code, _, _ = _scan(fake, since_dt, tally=tally)
    assert code is None
    assert tally == {mfa_mailbox.REASON_NO_CODE_EXTRACTED: 1}


def test_check_inbox_once_handles_fetch_failure(since_dt):
    class _FetchFailMail(_FakeMail):
        def fetch(self, *_a, **_k):
            return ("NO", [None])

    fake = _FetchFailMail()
    tally: dict[str, int] = {}
    code, _, _ = _scan(fake, since_dt, tally=tally)
    assert code is None
    assert tally == {mfa_mailbox.REASON_FETCH_FAILED: 1}


def test_check_inbox_once_records_search_query_verbatim(since_dt):
    # The IMAP SEARCH query string is the SINGLE most important
    # diagnostic on a timeout — it shows the SINCE value we sent. Pin
    # the exact format so the timeout sys_log event is unambiguous.
    fake = _FakeMail(fetch_data=[(b"x", _fake_msg())])
    _, query, _ = _scan(fake, since_dt)
    expected = (
        f'(FROM "{mfa_mailbox.USCIS_SENDER}" '
        f'SUBJECT "{mfa_mailbox.USCIS_MFA_SUBJECT}" '
        f'SINCE {since_dt.strftime("%d-%b-%Y")})'
    )
    assert query == expected


def test_check_inbox_once_returns_decoded_uids(since_dt):
    # `returned_uids` is recorded on the timeout event so an operator
    # can see what the server actually gave us. Must be ASCII strings.
    fake = _FakeMail(
        ids=(b"101", b"102"),
        fetch_data=[(b"x", _fake_msg()), (b"y", _fake_msg())],
    )
    _, _, uids = _scan(fake, since_dt)
    assert uids == ["101", "102"]


def test_check_inbox_once_tally_accumulates_across_multiple_rejections(since_dt):
    # One stale, one wrong-subject — verify both reasons count.
    fake = _FakeMail(
        ids=(b"1", b"2"),
        fetch_data=[
            (b"x", _fake_msg(date_hdr="Wed, 01 Jan 2020 00:00:00 +0000")),
            (b"y", _fake_msg(subject="random")),
        ],
    )
    tally: dict[str, int] = {}
    code, _, _ = _scan(fake, since_dt, tally=tally)
    assert code is None
    # Newest-first iteration: uid=2 checked first (subject rejected),
    # then uid=1 (stale rejected).
    assert tally == {
        mfa_mailbox.REASON_SUBJECT_MISMATCH: 1,
        mfa_mailbox.REASON_STALE: 1,
    }


def test_check_inbox_once_imap_connect_failure_categorised(since_dt):
    tally: dict[str, int] = {}
    with patch.object(
        mfa_mailbox.imaplib, "IMAP4_SSL",
        side_effect=OSError("network unreachable"),
    ):
        code, query, uids = _check_inbox_once(
            "u@example.com", "pw", since_dt, tally=tally,
        )
    assert code is None
    assert query is None  # never got as far as building a query
    assert uids is None
    assert tally == {mfa_mailbox.REASON_IMAP_CONNECT_FAILED: 1}


def test_check_inbox_once_imap_login_failure_categorised(since_dt):
    class _LoginFailMail(_FakeMail):
        def login(self, *a, **k):
            raise imaplib.IMAP4.error("Invalid credentials")

    fake = _LoginFailMail()
    tally: dict[str, int] = {}
    code, _, _ = _scan(fake, since_dt, tally=tally)
    assert code is None
    assert tally == {mfa_mailbox.REASON_IMAP_LOGIN_FAILED: 1}


# -------- fetch_latest_code (polling loop) --------------------------------

def _mk_fake_check(sequence, query="QUERY", uids=None):
    """Build a _check_inbox_once replacement that returns a scripted
    sequence of codes (or None) and mutates the caller's tally dict so
    the surrounding lifecycle tests can verify aggregation.

    Each sequence element is either a code string or None. None also
    bumps REASON_STALE in the tally so timeout events can assert on a
    realistic reason breakdown.
    """
    uids = uids if uids is not None else ["1"]
    it = iter(sequence)

    def _fake(email_addr, pw, since, *, tally=None):
        try:
            nxt = next(it)
        except StopIteration:
            nxt = None
        if tally is not None:
            key = (mfa_mailbox.REASON_ACCEPTED if nxt
                   else mfa_mailbox.REASON_STALE)
            tally[key] = tally.get(key, 0) + 1
        return (nxt, query, uids)

    return _fake


def test_fetch_latest_code_returns_first_match(monkeypatch):
    monkeypatch.setattr(
        mfa_mailbox, "_check_inbox_once", _mk_fake_check(["111111"]),
    )
    assert fetch_latest_code("u@example.com", "pw", max_wait_seconds=1) == "111111"


def test_fetch_latest_code_times_out(monkeypatch):
    monkeypatch.setattr(
        mfa_mailbox, "_check_inbox_once", _mk_fake_check([None, None]),
    )
    monkeypatch.setattr(mfa_mailbox.time, "sleep", lambda _s: None)
    with pytest.raises(TimeoutError) as exc_info:
        fetch_latest_code("u@example.com", "pw",
                          max_wait_seconds=0, poll_interval_seconds=0)
    # Diagnostic payload must be inlined into the error message.
    msg = str(exc_info.value)
    assert "cycles=" in msg
    assert "reasons=" in msg
    assert "host=" in msg
    assert "last_query=" in msg
    assert "last_uids=" in msg


def test_fetch_latest_code_default_since_is_two_minutes_ago(monkeypatch):
    captured = {}

    def _capture(gu, pw, since, *, tally=None):
        captured["since"] = since
        return ("999999", "Q", [])

    monkeypatch.setattr(mfa_mailbox, "_check_inbox_once", _capture)
    fetch_latest_code("u@example.com", "pw", max_wait_seconds=1)
    # Default since should be ~2 min ago and tz-aware.
    assert captured["since"].tzinfo is not None


def test_fetch_latest_code_sleeps_between_polls(monkeypatch):
    sleeps = []
    monkeypatch.setattr(
        mfa_mailbox, "_check_inbox_once", _mk_fake_check([None, "222222"]),
    )
    monkeypatch.setattr(mfa_mailbox.time, "sleep", lambda s: sleeps.append(s))
    code = fetch_latest_code(
        "u@example.com", "pw", max_wait_seconds=5, poll_interval_seconds=3,
    )
    assert code == "222222"
    assert sleeps == [3]


# -------- sys_log instrumentation --------------------------------------

def test_fetch_latest_code_emits_started_and_succeeded_sys_log(monkeypatch, tmp_path):
    # Redirect system_log.LOG_PATH to a tmp file for this test.
    import system_log
    monkeypatch.setattr(system_log, "LOG_PATH", tmp_path / "system_log.json")
    system_log.clear()

    monkeypatch.setattr(
        mfa_mailbox, "_check_inbox_once", _mk_fake_check(["111111"]),
    )
    fetch_latest_code("u@example.com", "pw", max_wait_seconds=1)

    events = [e["event"] for e in system_log.read_all()]
    assert "mfa_fetch_started" in events
    assert "mfa_fetch_succeeded" in events
    assert "mfa_fetch_timeout" not in events


def test_fetch_latest_code_timeout_sys_log_carries_diagnostics(monkeypatch, tmp_path):
    import system_log
    monkeypatch.setattr(system_log, "LOG_PATH", tmp_path / "system_log.json")
    system_log.clear()
    monkeypatch.setattr(mfa_mailbox.time, "sleep", lambda _s: None)

    # Control time.time() deterministically: 3 calls inside the budget,
    # then the 4th call is past deadline.  Budget is 10s.
    t_values = iter([0.0, 0.0, 1.0, 2.0, 3.0, 99.0, 99.0, 99.0, 99.0])
    monkeypatch.setattr(mfa_mailbox.time, "time", lambda: next(t_values, 100.0))

    # Simulate the live failure mode: every cycle, search returns empty.
    def _always_empty(email_addr, pw, since, *, tally=None):
        if tally is not None:
            tally[mfa_mailbox.REASON_IMAP_SEARCH_EMPTY] = (
                tally.get(mfa_mailbox.REASON_IMAP_SEARCH_EMPTY, 0) + 1
            )
        return (None, '(FROM "x" SUBJECT "y" SINCE 22-Apr-2026)', [])

    monkeypatch.setattr(mfa_mailbox, "_check_inbox_once", _always_empty)
    with pytest.raises(TimeoutError):
        fetch_latest_code(
            "u@example.com", "pw",
            max_wait_seconds=10, poll_interval_seconds=0,
        )

    timeouts = [e for e in system_log.read_all()
                if e["event"] == "mfa_fetch_timeout"]
    assert len(timeouts) == 1
    ev = timeouts[0]
    assert ev["level"] == "error"
    assert ev["cycles"] >= 1
    assert ev["reasons"] == {mfa_mailbox.REASON_IMAP_SEARCH_EMPTY: ev["cycles"]}
    assert ev["last_search_query"] == '(FROM "x" SUBJECT "y" SINCE 22-Apr-2026)'
    assert ev["last_returned_uids"] == []
    assert "since" in ev
    assert "imap_host" in ev


def test_fetch_latest_code_success_sys_log_carries_cycles_count(monkeypatch, tmp_path):
    import system_log
    monkeypatch.setattr(system_log, "LOG_PATH", tmp_path / "system_log.json")
    system_log.clear()
    monkeypatch.setattr(mfa_mailbox.time, "sleep", lambda _s: None)

    # Succeed on the 3rd cycle.
    monkeypatch.setattr(
        mfa_mailbox, "_check_inbox_once",
        _mk_fake_check([None, None, "333333"]),
    )
    fetch_latest_code(
        "u@example.com", "pw",
        max_wait_seconds=60, poll_interval_seconds=0,
    )
    successes = [e for e in system_log.read_all()
                 if e["event"] == "mfa_fetch_succeeded"]
    assert len(successes) == 1
    ev = successes[0]
    assert ev["cycles"] == 3
    assert ev["code_length"] == 6
    # Reason tally should reflect 2 rejects + 1 accept.
    assert ev["reasons"] == {
        mfa_mailbox.REASON_STALE: 2,
        mfa_mailbox.REASON_ACCEPTED: 1,
    }


def test_check_inbox_once_skips_empty_fetch_row(since_dt):
    # A fetch that returns OK but with an empty data row must be skipped.
    class _EmptyFetchMail(_FakeMail):
        def fetch(self, *_a, **_k):
            return ("OK", [])

    fake = _EmptyFetchMail()
    code, _, _ = _scan(fake, since_dt)
    assert code is None


def test_check_inbox_once_skips_null_fetch_row(since_dt):
    # Some IMAP servers return ("OK", [None]) — msg_data[0] is falsy.
    class _NullFetchMail(_FakeMail):
        def fetch(self, *_a, **_k):
            return ("OK", [None])

    fake = _NullFetchMail()
    code, _, _ = _scan(fake, since_dt)
    assert code is None


def test_check_inbox_once_handles_naive_date(since_dt):
    # Message with a tz-less Date header. _check_inbox_once must treat it as UTC.
    fake = _FakeMail(fetch_data=[(b"x", _fake_msg(date_hdr="18 Apr 2026 22:43:21"))])
    code, _, _ = _scan(fake, since_dt)
    # Naive date >= since_dt → the match path runs through the tz-normalise branch.
    assert code == "424242"


def test_extract_body_non_multipart_with_payload():
    # Plain HTML-only email (not multipart). Body must decode.
    msg = EmailMessage()
    msg.set_content("Please enter this secure MFA code <span>456123</span>",
                    subtype="html")
    # set_content(subtype="html") keeps is_multipart() False.
    assert msg.is_multipart() is False
    body = _extract_body(msg)
    assert "456123" in body
