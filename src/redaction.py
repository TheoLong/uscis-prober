# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server-side PII redaction.

When redaction mode is enabled, the server masks sensitive data *before* it
leaves the process, so the dashboard can be screenshotted or screen-shared
without private data ever reaching the browser — it isn't recoverable from the
console, the network tab, or page source.

Mirrors the client-side helpers in static/app.js (REDACT_KEYS / scrubText /
redactSnapshot) so both layers agree on what counts as PII.
"""

from __future__ import annotations

import re
from typing import Any

# Object keys whose values are PII, matched anywhere in any payload tree: the
# case/receipt number (snapshot `receiptNumber` and system-log `receipt`), the
# applicant's name, and the representative's name.
#
# Identifier keys (eventId, letterId, pid, …) are deliberately NOT masked here:
# the browser keys the event timeline on the real eventId, so the server can't
# withhold it. Those are masked client-side at the display layer instead (see
# isRedactKey in static/app.js).
REDACT_KEYS = frozenset({
    "receiptNumber", "receipt", "applicantName", "representativeName",
})

# Fixed-width mask — leaks nothing about the original length.
REDACTION_MASK = "•" * 8  # ••••••••

# PII embedded in otherwise-free text (URLs, titles, log messages): USCIS
# receipt numbers (e.g. IOE0000000000) and email addresses.
_PATTERNS = (
    re.compile(r"\b[A-Z]{3}\d{7,}\b"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
)


def scrub_text(value: Any) -> Any:
    """Scrub PII *patterns* out of a string. Non-strings pass through."""
    if not isinstance(value, str):
        return value
    out = value
    for pat in _PATTERNS:
        out = pat.sub(REDACTION_MASK, out)
    return out


def redact_obj(value: Any) -> Any:
    """Deep-copy a JSON-ish value, masking PII-keyed values outright and
    scrubbing PII embedded in any remaining string. Never mutates the input."""
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k in REDACT_KEYS and v is not None and not isinstance(v, (dict, list)):
                out[k] = REDACTION_MASK
            else:
                out[k] = redact_obj(v)
        return out
    return scrub_text(value)
