# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the server-side PII redaction module (src/redaction.py)."""

from __future__ import annotations

import redaction
from redaction import REDACTION_MASK, redact_obj, scrub_text


def test_redact_obj_masks_pii_keys_at_top_level():
    out = redact_obj({
        "receiptNumber": "IOE0000000000",
        "applicantName": "DOE, JANE",
        "representativeName": "SMITH, JOHN",
        "formName": "I-485, Application to Register Permanent Residence",
        "formType": "I485",
        "closed": False,
    })
    assert out["receiptNumber"] == REDACTION_MASK
    assert out["applicantName"] == REDACTION_MASK
    assert out["representativeName"] == REDACTION_MASK
    assert out["formName"] == "I-485, Application to Register Permanent Residence"
    assert out["formType"] == "I485"
    assert out["closed"] is False


def test_redact_obj_masks_system_log_receipt_key_and_nesting():
    out = redact_obj({
        "receipt": "IOE0000000000",
        "concurrentCases": [
            {"receiptNumber": "IOE2", "formType": "I765"},
            {"receiptNumber": "IOE3"},
        ],
        "deep": {"inner": {"applicantName": "X, Y"}},
    })
    assert out["receipt"] == REDACTION_MASK
    assert out["concurrentCases"][0]["receiptNumber"] == REDACTION_MASK
    assert out["concurrentCases"][0]["formType"] == "I765"
    assert out["concurrentCases"][1]["receiptNumber"] == REDACTION_MASK
    assert out["deep"]["inner"]["applicantName"] == REDACTION_MASK


def test_redact_obj_scrubs_pii_embedded_in_strings():
    out = redact_obj({
        # `url` is a URI key -> fully masked (no id-token in a shared demo).
        "url": "https://egov.uscis.gov/casestatus/IOE0000000000/x",
        # A non-URI free-text field still gets pattern-scrubbed in place.
        "statusText": "Receipt Number IOE0000000000 is being processed",
        "note": "no identifiers here",
    })
    assert out["url"] == REDACTION_MASK              # masked outright
    assert "IOE0000000000" not in out["statusText"]
    assert REDACTION_MASK in out["statusText"]       # scrubbed in place
    assert out["note"] == "no identifiers here"


def test_redact_obj_is_pure():
    src = {"receiptNumber": "IOE0000000000", "events": [{"code": "ABC"}]}
    out = redact_obj(src)
    assert src["receiptNumber"] == "IOE0000000000"  # unchanged
    assert out["receiptNumber"] == REDACTION_MASK
    assert out["events"] is not src["events"]


def test_scrub_text_patterns():
    assert scrub_text("case IOE0000000000 here") == f"case {REDACTION_MASK} here"
    assert "@" not in scrub_text("mail me a.b@example.com")
    assert scrub_text("form I-485 on 2026-06-16") == "form I-485 on 2026-06-16"
    assert scrub_text(42) == 42  # non-strings pass through


def test_redact_keys_cover_both_receipt_spellings():
    assert {"receiptNumber", "receipt", "applicantName", "representativeName"} <= set(redaction.REDACT_KEYS)


def test_redact_obj_pseudonymizes_render_critical_ids_masks_the_rest():
    out = redact_obj({
        "events": [{"eventId": "abc-123", "eventCode": "RFE"}],
        "notices": [{"letterId": "425512420"}],
        "pid": "P-7",
        "noticeId": "94aa58520cdb",
        "formType": "I485",
    })
    eid = out["events"][0]["eventId"]
    # Render-critical id: pseudonymized (unique token, real value withheld).
    assert eid not in ("abc-123", REDACTION_MASK) and "abc-123" not in eid
    assert eid.startswith("id-")
    # Display-only ids: FULLY MASKED — no id-looking token in a shared demo.
    assert out["notices"][0]["letterId"] == REDACTION_MASK
    assert out["pid"] == REDACTION_MASK
    assert out["noticeId"] == REDACTION_MASK
    # Non-identifier siblings untouched.
    assert out["events"][0]["eventCode"] == "RFE"
    assert out["formType"] == "I485"


def test_pseudonymize_is_stable_and_distinct():
    # Same input → same token (so dedup + link overlay still match); different
    # input → different token (no collisions merging distinct events).
    a1 = redact_obj({"eventId": "same"})["eventId"]
    a2 = redact_obj({"originId": "same"})["originId"]
    b = redact_obj({"eventId": "other"})["eventId"]
    assert a1 == a2, "stable across keys/calls for the same real id"
    assert a1 != b, "distinct ids yield distinct tokens"
    # reemitId is also render-critical (overlay endpoint).
    assert redact_obj({"reemitId": "x"})["reemitId"].startswith("id-")
    assert redact_obj({"id": "x"})["id"].startswith("id-")


def test_name_keys_masked_except_safe_allowlist():
    # Defense-in-depth: any key ending in "name" (beyond the explicit
    # REDACT_KEYS) is masked, so a future USCIS name field can't leak in
    # redaction mode. Known-safe name-suffixed keys stay visible.
    out = redact_obj({
        "applicantName": "TESTNAME",        # explicit REDACT_KEY
        "beneficiaryName": "SOMEONE",       # new → masked by heuristic
        "petitionerName": "ACME CORP",      # new → masked by heuristic
        "fullName": "A B",                  # new → masked by heuristic
        "formName": "I-485",                # allowlisted → visible
        "statusName": "Pending",            # allowlisted → visible
    })
    assert out["applicantName"] == REDACTION_MASK
    assert out["beneficiaryName"] == REDACTION_MASK
    assert out["petitionerName"] == REDACTION_MASK
    assert out["fullName"] == REDACTION_MASK
    assert out["formName"] == "I-485"
    assert out["statusName"] == "Pending"


def test_uri_keys_masked_but_lookalikes_untouched():
    # URL/URI/link/href keys carry receipt-bearing paths or opaque tokens and
    # must be FULLY MASKED (no id-looking token in a shared demo). Keys that
    # merely embed the letters (jurisdictionDescription, documentCount) pass.
    out = redact_obj({
        "documentUri": "FAKETOKEN_abcdefghijklmnopqrstuvwxyz012345",
        "url": "https://my.uscis.gov/account/case-service/api/cases/IOE0000000000",
        "url_before": "https://myaccount.uscis.gov/sign-in",
        "url_after": "https://myaccount.uscis.gov/dashboard",
        "pageHref": "https://example.com/x",
        "jurisdictionDescription": "CHARLOTTE, NC",
        "documentCount": 0,
    })
    for k in ("documentUri", "url", "url_before", "url_after", "pageHref"):
        assert out[k] == REDACTION_MASK, f"{k} must be masked"
    # Lookalikes untouched.
    assert out["jurisdictionDescription"] == "CHARLOTTE, NC"
    assert out["documentCount"] == 0
