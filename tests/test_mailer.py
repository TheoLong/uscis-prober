# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for email content formatting. No SMTP — just build_update_email."""

from mailer import build_update_email


def _record(kind, **extra):
    base = {
        "id": "IOE0000000001:2026-04-18",
        "caseLabel": "I-485",
        "receiptNumber": "IOE0000000001",
        "kind": kind,
        "from": "2026-03-18T17:00:00Z",
        "to": "2026-04-18T22:43:21Z",
        "detectedOn": "2026-04-18",
        "realUpdateDate": "2026-04-07",
        "scalars": {},
        "events": {"added": [], "removed": []},
        "notices": {"added": [], "removed": []},
        "documents": {"added": [], "removed": []},
        "addendums": {"added": [], "removed": []},
    }
    base.update(extra)
    return base


def test_subject_includes_case_and_kind():
    subject, plain, html = build_update_email(_record("silent_update"))
    assert "I-485" in subject
    assert "silent update" in subject


def test_plain_body_surfaces_real_update_date_when_different():
    _, plain, _ = build_update_email(_record("silent_update"))
    assert "2026-04-18" in plain  # detected
    assert "2026-04-07" in plain  # actual update date


def test_event_body_uses_code_labels_when_provided():
    record = _record(
        "event",
        scalars={"updatedAt": {"from": "2026-03-05", "to": "2026-03-10"}},
        events={
            "added": [{"eventCode": "FTA0", "eventDateTime": "2026-03-10"}],
            "removed": [],
        },
    )
    labels = {"FTA0": "Biometrics received / fingerprints submitted to FBI"}
    _, plain, html = build_update_email(record, labels)
    assert "FTA0" in plain
    assert "Biometrics received" in plain
    assert "FTA0" in html


def test_html_escapes_angle_brackets_in_values():
    record = _record(
        "status",
        scalars={"statusText": {"from": "<old>", "to": "<new>"}},
    )
    _, _, html = build_update_email(record)
    assert "&lt;old&gt;" in html
    assert "&lt;new&gt;" in html
    # Raw angle brackets from values should not escape to literal HTML
    assert "<old>" not in html


def test_appointment_kind_description():
    record = _record(
        "appointment",
        notices={
            "added": [{
                "actionType": "Appointment Scheduled",
                "letterId": "L1",
                "appointmentDateTime": "2026-04-20T13:00:00Z",
            }],
            "removed": [],
        },
    )
    _, plain, _ = build_update_email(record)
    assert "Appointment Scheduled" in plain
    assert "L1" in plain
    assert "2026-04-20" in plain


# -------- removed items render ------------------------------------------

def test_removed_events_appear_with_minus_sign():
    record = _record(
        "event",
        events={
            "added": [],
            "removed": [{"eventCode": "RFE0", "eventDateTime": "2026-02-01"}],
        },
    )
    _, plain, html = build_update_email(record, {"RFE0": "Request for Evidence"})
    assert "- RFE0" in plain
    assert "RFE0" in html
    # HTML negative-change bullet uses the minus glyph
    assert "−" in html


def test_removed_notices_rendered_in_plain_text():
    record = _record(
        "notice",
        notices={
            "added": [],
            "removed": [{
                "actionType": "Old Notice",
                "letterId": "OLD123",
            }],
        },
    )
    _, plain, _ = build_update_email(record)
    assert "- Old Notice" in plain
    assert "OLD123" in plain


def test_documents_and_addendums_appear_in_body():
    record = _record(
        "status",
        documents={"added": [{"id": "doc1", "type": "letter"}], "removed": []},
        addendums={"added": [], "removed": [{"id": "add1"}]},
    )
    _, plain, html = build_update_email(record)
    assert "Documents:" in plain
    assert "Addendums:" in plain
    assert "doc1" in plain
    assert "add1" in plain
    # HTML includes both section headers (uppercased via CSS, lowercased here by esc)
    assert "Documents" in html
    assert "Addendums" in html


# -------- detection == actual update date collapses the date line -----------------

def test_date_line_collapses_when_real_update_date_equals_detected():
    _, plain, _ = build_update_email(_record("event", realUpdateDate="2026-04-18"))
    assert "Detected 2026-04-18" in plain
    assert "actual update date" not in plain


def test_date_line_falls_back_to_to_date_when_detected_missing():
    rec = _record("silent_update")
    rec["detectedOn"] = ""
    _, plain, _ = build_update_email(rec)
    # "to" is "2026-04-18..."  — detection derived from to[:10]
    assert "Detected 2026-04-18" in plain


# -------- unknown kind falls back to the kind string itself --------------

def test_unknown_kind_used_verbatim():
    rec = _record("status")
    rec["kind"] = "weird_custom_kind"
    subject, plain, _ = build_update_email(rec)
    assert "weird_custom_kind" in subject
    assert plain  # body still renders


def test_missing_kind_defaults_to_status():
    rec = _record("status")
    del rec["kind"]
    subject, _, _ = build_update_email(rec)
    assert "status change" in subject


# -------- SMTP wiring (mocked) -----------------------------------------

def test_send_email_invokes_smtp_flow():
    from unittest.mock import MagicMock, patch
    from mailer import send_email

    fake_smtp = MagicMock()
    fake_smtp.__enter__.return_value = fake_smtp
    with patch("mailer.smtplib.SMTP", return_value=fake_smtp) as smtp_ctor:
        send_email(
            uscis_mfa_email="u@example.com",
            uscis_mfa_app_password="pw",
            to="x@example.com",
            subject="hi",
            plain="plain",
            html="<p>html</p>",
        )
    smtp_ctor.assert_called_once()
    # ehlo is called twice: once before STARTTLS, again after the TLS
    # handshake (RFC 3207 §4) to refresh the server capability list.
    assert fake_smtp.ehlo.call_count == 2
    fake_smtp.starttls.assert_called_once()
    fake_smtp.login.assert_called_once_with("u@example.com", "pw")
    fake_smtp.send_message.assert_called_once()


def test_notify_update_uses_auth_dict():
    from unittest.mock import patch
    from mailer import notify_update

    auth = {"uscis_mfa_email": "u", "uscis_mfa_app_password": "pw"}
    with patch("mailer.send_email") as send:
        notify_update(auth, "x@example.com", _record("silent_update"))
    send.assert_called_once()
    kwargs = send.call_args.kwargs
    assert kwargs["uscis_mfa_email"] == "u"
    assert kwargs["uscis_mfa_app_password"] == "pw"
    assert kwargs["to"] == "x@example.com"
    assert "I-485" in kwargs["subject"]


# -------- describe helpers handle missing fields ------------------------

def test_describe_event_without_label():
    from mailer import _describe_event
    assert "UNKNOWN_CODE" in _describe_event(
        {"eventCode": "UNKNOWN_CODE", "eventDateTime": "2026-01-01"}, {}
    )


def test_describe_event_with_missing_fields():
    from mailer import _describe_event
    desc = _describe_event({}, {})
    assert "?" in desc


def test_describe_notice_without_appointment():
    from mailer import _describe_notice
    desc = _describe_notice({"actionType": "Receipt", "letterId": "X1"})
    assert "Receipt" in desc
    assert "X1" in desc
    assert "appt" not in desc
