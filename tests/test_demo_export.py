# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the static demo-export builder.

The demo artifact must (1) be self-contained (no external asset refs),
(2) carry the live frontend verbatim, (3) fully redact PII, and (4) embed a
fetch shim that reports redaction enabled so actions render locked.
"""

import json
from pathlib import Path

import pytest

import server
import demo_export


# A synthetic receipt in the reserved IOE000000000X placeholder range — NOT a
# real case number. It stands in for the sensitive input the export must mask.
FAKE_RECEIPT = "IOE0000000001"
FAKE_NAME = "Jane Q Applicant"
FAKE_REP = "Larry L Lawyer"
FAKE_EMAIL = "applicant@example.com"


def _seed(data_dir: Path):
    """Write a single case snapshot carrying obvious PII to be masked."""
    entries = [
        {
            "capturedAt": "2026-05-01T00:00:00Z",
            "data": {
                "receiptNumber": FAKE_RECEIPT,
                "applicantName": FAKE_NAME,
                "representativeName": FAKE_REP,
                "formName": "I-485",
                "updatedAt": "2026-05-01T00:00:00Z",
                "closed": False,
                "actionRequired": False,
                "events": [
                    {"eventId": "evt-abc-123", "eventCode": "FTA0",
                     "eventTimestamp": "2026-04-01T00:00:00Z"},
                ],
                "notices": [],
            },
        }
    ]
    (data_dir / "485_case.json").write_text(json.dumps(entries))
    (data_dir / "485_status.json").write_text(json.dumps({
        "capturedAt": "2026-05-01T00:00:00Z",
        "data": {
            "statusTitle": "Case Was Received",
            "statusText": f"We received your case, Receipt Number {FAKE_RECEIPT}.",
            "historicalCaseStatuses": [],
        },
    }))


@pytest.fixture
def app(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    cfg = {
        "auth": {
            "uscis_email": FAKE_EMAIL, "uscis_password": "p",
            "uscis_mfa_email": FAKE_EMAIL, "uscis_mfa_app_password": "pw",
        },
        "cases": [{"id": FAKE_RECEIPT, "label": "I-485"}],
        "pull_hours": [0],
        "retry": 0, "retry_wait_seconds": 0,
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    monkeypatch.setattr(server, "DATA_DIR", data_dir)
    monkeypatch.setattr(server, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(server, "_pull_state", server.PullState())
    _seed(data_dir)
    server.app.config["TESTING"] = True
    return server.app


def test_build_returns_html_filename(app):
    filename, html = demo_export.build_demo_html(app)
    assert filename.startswith("uscis-prober-demo-")
    assert filename.endswith(".html")
    assert html.lstrip().startswith("<!")  # doctype/comment prologue
    assert "</html>" in html


def test_demo_is_self_contained(app):
    _, html = demo_export.build_demo_html(app)
    # Live frontend inlined, no external fetches for assets.
    assert "<style>" in html                        # css inlined
    assert 'src="/static/app.js"' not in html       # app.js inlined, not linked
    assert 'href="/static/style.css"' not in html   # css inlined, not linked
    assert "/static/logo.svg" not in html           # logo → data URI
    assert "data:image/svg+xml;base64," in html


def test_demo_redacts_all_pii(app):
    _, html = demo_export.build_demo_html(app)
    # Every real identifier must be masked out of the artifact.
    assert FAKE_RECEIPT not in html
    assert FAKE_NAME not in html
    assert FAKE_REP not in html
    assert FAKE_EMAIL not in html
    # The receipt embedded in free-text status must be scrubbed too.
    assert "\u2022" * 8 in html   # fixed-width mask present


def test_demo_embeds_frozen_data_and_shim(app):
    _, html = demo_export.build_demo_html(app)
    assert "Injected by demo_export.py" in html
    assert "window.fetch = function" in html
    # Frozen redaction state must report enabled so the app locks actions.
    assert "'/api/redaction-mode'" in html
    # Demo-mode flag drives the universal "not available" notice on actions.
    assert "window.__DEMO_MODE__ = true" in html


def test_frontend_has_demo_mode_notice():
    """app.js must short-circuit guarded actions to one universal demo notice."""
    app_js = (Path(demo_export.__file__).resolve().parent
              / "static" / "app.js").read_text(encoding="utf-8")
    assert "window.__DEMO_MODE__" in app_js
    assert "Action not available in demo site" in app_js


def test_capture_pseudonymizes_ids_not_masks(app):
    # Identifier keys are pseudonymized (stable opaque token), not fixed-masked,
    # so the client-side timeline can still key on unique ids.
    _, html = demo_export.build_demo_html(app)
    assert "evt-abc-123" not in html      # real id gone
    assert "id-" in html                  # pseudonymized token present


def test_demo_route_serves_inline(app):
    """GET /demo renders the artifact inline (no attachment disposition)."""
    app.config["TESTING"] = True
    client = app.test_client()
    resp = client.get("/demo")
    assert resp.status_code == 200
    assert resp.mimetype == "text/html"
    # Inline: must NOT force a download.
    assert "attachment" not in (resp.headers.get("Content-Disposition") or "")
    body = resp.get_data(as_text=True)
    assert "USCIS Prober" in body
    assert "window.__DEMO_MODE__ = true" in body
    # Same PII guarantee as the download path.
    assert FAKE_RECEIPT not in body


def test_export_demo_route_downloads(app):
    """GET /api/export-demo keeps the attachment disposition (download)."""
    app.config["TESTING"] = True
    client = app.test_client()
    resp = client.get("/api/export-demo")
    assert resp.status_code == 200
    cd = resp.headers.get("Content-Disposition") or ""
    assert "attachment" in cd
    assert ".html" in cd
