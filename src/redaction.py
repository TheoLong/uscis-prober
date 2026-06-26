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

import hashlib
import hmac
import re
import secrets
from typing import Any

# Object keys whose values are PII, masked outright wherever they appear: the
# case/receipt number (snapshot `receiptNumber` and system-log `receipt`), the
# applicant's name, and the representative's name.
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

# Identifier keys (eventId, letterId, originId, pid, the synthetic update `id`, …)
# can't be fixed-masked: the browser keys the event timeline + link overlay on
# the real eventId, so collapsing them all to one mask would merge every event.
# Instead we pseudonymize — replace each id with a stable opaque token — so the
# real value never reaches the browser while uniqueness (all the client needs)
# is preserved. The salt is per-process and random, so tokens can't be reversed.
_ID_SALT = secrets.token_bytes(16)


def _is_id_key(key: Any) -> bool:
    return isinstance(key, str) and key.lower().endswith("id") and key not in REDACT_KEYS


def _pseudonymize(value: Any) -> str:
    token = hmac.new(_ID_SALT, str(value).encode("utf-8"), hashlib.sha256).hexdigest()
    return "id-" + token[:12]


def scrub_text(value: Any) -> Any:
    """Scrub PII *patterns* out of a string. Non-strings pass through."""
    if not isinstance(value, str):
        return value
    out = value
    for pat in _PATTERNS:
        out = pat.sub(REDACTION_MASK, out)
    return out


def redact_obj(value: Any) -> Any:
    """Deep-copy a JSON-ish value, masking PII-keyed values outright,
    pseudonymizing identifier-keyed values, and scrubbing PII embedded in any
    remaining string. Never mutates the input."""
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            scalar = v is not None and not isinstance(v, (dict, list))
            if k in REDACT_KEYS and scalar:
                out[k] = REDACTION_MASK
            elif _is_id_key(k) and scalar:
                out[k] = _pseudonymize(v)
            else:
                out[k] = redact_obj(v)
        return out
    return scrub_text(value)
