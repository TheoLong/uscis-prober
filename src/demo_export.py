# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static demo-export builder.

Produces a single self-contained `.html` file that renders **identically to
the live dashboard in redaction mode**, but with no backend: all data is
frozen into the file and every action is inert.

Design — reuse the live frontend wholesale:
  The export does NOT reimplement any rendering. It inlines the real
  `static/index.html`, `style.css`, and `app.js` byte-for-byte, and only swaps
  the *data source*: a tiny `fetch` shim (injected before `app.js`) replays a
  captured, redacted snapshot of every read endpoint the SPA calls on boot.
  Because the markup, styles, and render logic are the live ones, any visual
  change to the dashboard flows into the export automatically — this module
  only ever deals with data shapes, never presentation.

Redaction:
  Captured responses are passed through the same `redaction.redact_obj` the
  server uses, so the artifact carries the exact masking of the live redacted
  site (case/receipt numbers + names masked, identifier keys pseudonymized to
  stable opaque tokens, PII patterns scrubbed from free text). Redaction is
  applied unconditionally, independent of the live redaction toggle, so a demo
  is always safe to share.

Inertness:
  The shim serves the frozen reads and rejects everything else (writes, action
  POSTs, forensic trace/MFA endpoints) with the same `403 redaction_enabled`
  shape the live server returns while redaction is latched. The captured
  `/api/redaction-mode` reports `enabled: true`, so `app.js` applies its
  redaction latch on first paint and grays every action button — exactly as on
  the live redacted site.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from redaction import redact_obj

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Reads the SPA performs on boot / interaction that carry data. Each is
# captured via the app's own route handlers, then redacted, then frozen.
# Order is irrelevant; the shim matches by path at replay time.


def _client_get_json(client, url: str) -> Any:
    """GET a JSON endpoint through the app's test client and parse the body.

    Going through the real handler (not a reimplementation) guarantees the
    captured shape matches exactly what the live SPA receives.
    """
    resp = client.get(url)
    if resp.status_code != 200:
        return None
    try:
        return json.loads(resp.get_data(as_text=True))
    except (ValueError, TypeError):
        return None


def _capture_full_system_log(client) -> list:
    """Page through /api/system-log and return the full oldest-first event list.

    The shim paginates client-side from this full array, mirroring the server's
    newest-end offset slicing, so the demo's System-log pager behaves like live.
    """
    first = _client_get_json(client, "/api/system-log?limit=500&offset=0")
    if not isinstance(first, dict):
        return []
    total = int(first.get("total") or 0)
    # The server returns an oldest-first slice for each page. Rebuild the full
    # oldest-first list by walking newest→older offsets and prepending.
    events: list = list(first.get("events") or [])
    got = len(events)
    while got < total:
        page = _client_get_json(
            client, f"/api/system-log?limit=500&offset={got}"
        )
        if not isinstance(page, dict):
            break
        chunk = list(page.get("events") or [])
        if not chunk:
            break
        events = chunk + events
        got += len(chunk)
    return events


def _capture(app) -> dict[str, Any]:
    """Capture every read the SPA needs, redacted, into a JSON-able dict."""
    with app.test_client() as client:
        cases_doc = _client_get_json(client, "/api/cases") or {"cases": []}

        histories: dict[str, Any] = {}
        for case in cases_doc.get("cases", []):
            label = case.get("label")
            if not label:
                continue
            from urllib.parse import quote

            hist = _client_get_json(
                client, f"/api/cases/{quote(str(label))}/history"
            )
            if hist is not None:
                histories[label] = hist

        updates_doc = _client_get_json(client, "/api/updates") or {"updates": []}
        pull_status = _client_get_json(client, "/api/pull/status") or {}
        storage = _client_get_json(client, "/api/storage") or {}
        version = _client_get_json(client, "/api/version") or {}
        system_log = _capture_full_system_log(client)

    # A frozen snapshot never has a pull in flight; make sure the UI doesn't
    # sit in a "pulling…" state waiting on a poll that will never resolve.
    if isinstance(pull_status, dict):
        pull_status["running"] = False
        pull_status["pullRunning"] = False

    raw = {
        "cases": cases_doc,
        "history": histories,
        "updates": updates_doc,
        "pullStatus": pull_status,
        "storage": storage,
        "version": version,
        "systemLog": system_log,
        "generatedAt": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    # Single, unconditional redaction pass — same masking as the live server.
    return redact_obj(raw)


def _read_static(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def _logo_data_uri() -> str:
    raw = (STATIC_DIR / "logo.svg").read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


def _fetch_shim(data: dict[str, Any]) -> str:
    """The JS that makes the static file behave like a live (redacted) server.

    Replays frozen reads and rejects everything else with the live redacted
    server's `403 redaction_enabled` shape, so guarded actions stay locked.
    """
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return (
        "<script>\n"
        "// Injected by demo_export.py — replays a frozen, redacted snapshot so\n"
        "// the live app.js renders offline with zero backend. Do not edit the\n"
        "// app logic here; this only swaps the data source.\n"
        "(function () {\n"
        f"  const DATA = {payload};\n"
        "  // Marks this as the static demo build so app.js short-circuits every\n"
        "  // guarded action to a universal 'not available in demo' notice.\n"
        "  window.__DEMO_MODE__ = true;\n"
        "  const enc = new TextEncoder();\n"
        "  function jsonResponse(obj, status) {\n"
        "    const body = JSON.stringify(obj);\n"
        "    return Promise.resolve(new Response(body, {\n"
        "      status: status || 200,\n"
        "      headers: { 'Content-Type': 'application/json' },\n"
        "    }));\n"
        "  }\n"
        "  // Mirror the live server's newest-end offset slicing.\n"
        "  function systemLogPage(limit, offset) {\n"
        "    const all = DATA.systemLog || [];\n"
        "    const total = all.length;\n"
        "    const end = Math.max(0, total - offset);\n"
        "    const start = Math.max(0, end - limit);\n"
        "    return { events: all.slice(start, end), total, limit, offset };\n"
        "  }\n"
        "  function reply(path, qs) {\n"
        "    if (path === '/api/cases') return DATA.cases;\n"
        "    if (path === '/api/updates') return DATA.updates;\n"
        "    if (path === '/api/pull/status') return DATA.pullStatus;\n"
        "    if (path === '/api/storage') return DATA.storage;\n"
        "    if (path === '/api/version') return DATA.version;\n"
        "    if (path === '/api/redaction-mode') return { enabled: true };\n"
        "    if (path === '/api/access-lockout') return { enabled: false };\n"
        "    if (path === '/api/debug-mode') return { enabled: false };\n"
        "    if (path === '/api/system-log') {\n"
        "      const limit = Math.max(1, parseInt(qs.get('limit') || '100', 10));\n"
        "      const offset = Math.max(0, parseInt(qs.get('offset') || '0', 10));\n"
        "      return systemLogPage(limit, offset);\n"
        "    }\n"
        "    const hm = path.match(/^\\/api\\/cases\\/([^/]+)\\/history$/);\n"
        "    if (hm) {\n"
        "      const label = decodeURIComponent(hm[1]);\n"
        "      return (DATA.history || {})[label] || null;\n"
        "    }\n"
        "    return undefined;\n"
        "  }\n"
        "  window.fetch = function (input, init) {\n"
        "    const raw = typeof input === 'string' ? input : (input && input.url) || '';\n"
        "    const noOrigin = raw.replace(/^https?:\\/\\/[^/]+/, '');\n"
        "    const [path, query] = noOrigin.split('?');\n"
        "    const method = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();\n"
        "    if (method === 'GET') {\n"
        "      const body = reply(path, new URLSearchParams(query || ''));\n"
        "      if (body !== undefined) return jsonResponse(body);\n"
        "    }\n"
        "    // Writes, action POSTs, and forensic endpoints: inert, matching the\n"
        "    // live redacted server's locked-action response.\n"
        "    return jsonResponse({ ok: false, error: 'redaction_enabled' }, 403);\n"
        "  };\n"
        "})();\n"
        "</script>"
    )


def build_demo_html(app) -> tuple[str, str]:
    """Build the static demo artifact.

    Returns ``(filename, html)``. The HTML inlines the live index/CSS/JS plus a
    fetch shim carrying the redacted snapshot — a single portable file safe to
    share as case data or a demo.
    """
    data = _capture(app)

    html = _read_static("index.html")
    css = _read_static("style.css")
    app_js = _read_static("app.js")
    logo = _logo_data_uri()

    # Inline the stylesheet.
    html = html.replace(
        '<link rel="stylesheet" href="/static/style.css" />',
        f"<style>\n{css}\n</style>",
    )
    # Inline the logo everywhere it's referenced (favicon + brand marks).
    html = html.replace("/static/logo.svg", logo)

    # Inject the shim immediately before the app script, then inline the app
    # script. The shim must define window.fetch before app.js runs.
    shim = _fetch_shim(data)
    html = html.replace(
        '<script src="/static/app.js"></script>',
        f"{shim}\n<script>\n{app_js}\n</script>",
    )

    # --- Demo-only chrome (never touches the live app) --------------------
    # 1. Retitle the shared artifact so the browser tab / bookmark reads as the
    #    demo, not the live tracker.
    html = html.replace("<title>USCIS Prober</title>",
                        "<title>uscis-prober-demo</title>", 1)

    # 2. Pin a red banner to the very top of the page announcing that this is a
    #    static, share-only snapshot with no interactive features. The banner is
    #    part of normal document flow (not position:fixed) so it ALWAYS pushes
    #    the page down by its true height — even when the text wraps to 2–3
    #    lines on narrow screens — and never overlaps the sticky topbar. The
    #    topbar's sticky `top:0` then sticks it just under the banner on scroll.
    #    Styles inlined to keep the artifact self-contained.
    demo_banner = (
        '<div class="demo-static-banner" role="alert">'
        "This is a static demo site for sharing case data \u2014 "
        "interactive functions are not available."
        "</div>"
    )
    demo_banner_css = (
        "<style>\n"
        ".demo-static-banner{background:#c0392b;color:#fff;text-align:center;"
        "font:600 0.82rem/1.4 var(--font-mono,ui-monospace,monospace);"
        "letter-spacing:0.01em;padding:8px 16px;width:100%;box-sizing:border-box;"
        "box-shadow:0 1px 6px rgba(0,0,0,0.25);}\n"
        "</style>"
    )
    html = html.replace("</head>", f"{demo_banner_css}\n</head>", 1)
    # Inject the banner as the first thing inside the REAL document body. We
    # anchor on the topbar header rather than "<body>" because the inlined CSS
    # contains the literal string "<body>" too (in selectors/comments), so a
    # positional <body> replace can land in a dead zone. The topbar is always
    # the first real body element, so this puts the banner directly above it.
    html = html.replace(
        '<header class="topbar">',
        f'{demo_banner}\n  <header class="topbar">',
        1,
    )

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S-UTC")
    filename = f"uscis-prober-demo-{stamp}.html"
    return filename, html
