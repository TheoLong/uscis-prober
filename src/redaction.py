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

# Object keys whose values are PII, masked outright wherever they appear: the
# case/receipt number (snapshot `receiptNumber` and system-log `receipt`), the
# applicant's name, and the representative's name.
REDACT_KEYS = frozenset({
    "receiptNumber", "receipt", "applicantName", "representativeName",
})

# Defense-in-depth for future USCIS API changes: any *other* key ending in
# "name" (beneficiaryName, petitionerName, firstName, lastName, fullName, …) is
# also masked, so a newly-added name field can't silently leak in redaction
# mode before REDACT_KEYS is updated. A small allowlist exempts the known
# name-suffixed keys that are NOT PII and must stay visible (e.g. the form
# type). Verified against real data (2026-07): the only *Name keys present are
# applicantName / representativeName (PII, masked) and formName (safe).
_NAME_KEY_ALLOW = frozenset({"formname", "statusname", "eventname"})


def _is_name_key(key: Any) -> bool:
    if not isinstance(key, str):
        return False
    k = key.lower()
    return k.endswith("name") and k not in _NAME_KEY_ALLOW and key not in REDACT_KEYS

# Fixed-width mask — leaks nothing about the original length.
REDACTION_MASK = "•" * 8  # ••••••••

# PII embedded in otherwise-free text (URLs, titles, log messages): USCIS
# receipt numbers (e.g. IOE0000000000) and email addresses.
_PATTERNS = (
    re.compile(r"\b[A-Z]{3}\d{7,}\b"),
    re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
)

# Every identifier is fully MASKED in redacted / demo output — no id-looking
# token (not even a pseudonym) should appear anywhere a case is shared. The
# frontend no longer keys on any id VALUE: the event timeline dedups on a
# composite natural row key (eventCode|eventTimestamp|createdAtTimestamp) and
# the reemit overlay wires on the same key (see event_links._row_key), so
# masking every id can't collapse the timeline or mis-wire the overlay.


def _is_id_key(key: Any) -> bool:
    # ANY key ending in "id" (eventId, originId, reemitId, id, noticeId,
    # letterId, pid, cmsContentId, …) is masked. No exceptions.
    return (
        isinstance(key, str)
        and key.lower().endswith("id")
        and key not in REDACT_KEYS
    )


# URL / URI / link keys carry receipt-bearing paths (…/cases/IOE…) or opaque
# access tokens (documentUri) that must never reach a shared demo. None of them
# are render-critical, so they are fully masked. Matched when url/uri/link/href
# appears as a whole word-segment of the key — split on non-alphanumerics AND
# camelCase — so `url`, `documentUri`, `url_before`, `url_after`, `pageHref`,
# `sourceLink` are all caught, while lookalikes that merely embed the letters
# (`jurisdictionDescription`, `documentCount`) are not.
_URI_KEY_TOKENS = frozenset({"url", "uri", "link", "href"})
_CAMEL_SPLIT = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])")


def _is_uri_key(key: Any) -> bool:
    if not isinstance(key, str) or key in REDACT_KEYS:
        return False
    segments = {s.lower() for s in _CAMEL_SPLIT.split(key) if s}
    return bool(segments & _URI_KEY_TOKENS)


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
            elif _is_name_key(k) and scalar:
                out[k] = REDACTION_MASK
            elif _is_id_key(k) and scalar:
                # ANY identifier key (eventId, noticeId, letterId, pid, …).
                out[k] = REDACTION_MASK
            elif _is_uri_key(k) and scalar:
                # URLs / tokens (documentUri, url, …): fully masked.
                out[k] = REDACTION_MASK
            else:
                out[k] = redact_obj(v)
        return out
    return scrub_text(value)
