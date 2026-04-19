# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Poll an IMAP inbox for the latest USCIS MFA code.

IMAP host is picked automatically from the email domain (see
`providers.py`). The credentials come from
`config.json.auth.uscis_mfa_email` + `uscis_mfa_app_password` and must
be an app password (MFA enabled on the provider).
"""

from __future__ import annotations

import email
import email.message
import imaplib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from providers import imap_host_port

logger = logging.getLogger(__name__)

USCIS_SENDER = "MyAccount@uscis.dhs.gov"
USCIS_MFA_SUBJECT = "Secure two-step verification notification"
USCIS_MFA_BODY_ANCHOR = "Please enter this secure MFA code"

# The USCIS MFA email always wraps the code in:
#   Please enter this secure MFA code:
#     <span style='color: #0078AE; font-size: 24px; font-weight: 600;'>094897</span>
# Two independent anchors, either of which is sufficient:
#
#   PRIMARY : the unique sentence "Please enter this secure MFA code"
#             followed (after arbitrary HTML/whitespace) by a <span> with 6 digits.
#   FALLBACK: a <span> styled with the USCIS code color (#0078AE) containing 6 digits.
#
# Naïve `\b\d{6}\b` or `[^\d]*?\d{6}` matching must be avoided — the email body style
# contains `color: #333333`, `#0078AE`, `font-size: 24px`, etc. which produce
# spurious digit matches.
_CODE_RE_PRIMARY = re.compile(
    r"Please enter this secure MFA code"
    r"[\s\S]*?"
    r"<span[^>]*>\s*(\d{6})\s*</span>",
    re.IGNORECASE,
)
_CODE_RE_FALLBACK = re.compile(
    r"<span[^>]*color:\s*#0078AE[^>]*>\s*(\d{6})\s*</span>",
    re.IGNORECASE,
)


def _extract_code(body: str) -> str | None:
    """Extract the 6-digit USCIS MFA code from an email body.

    Returns None if no reliable anchor is found — we refuse to guess rather
    than return the first `\\d{6}` we see.
    """
    m = _CODE_RE_PRIMARY.search(body)
    if m:
        return m.group(1)
    m = _CODE_RE_FALLBACK.search(body)
    if m:
        return m.group(1)
    return None


def fetch_latest_code(
    uscis_mfa_email: str,
    uscis_mfa_app_password: str,
    *,
    since: datetime | None = None,
    max_wait_seconds: int = 180,
    poll_interval_seconds: int = 5,
) -> str:
    """Poll the inbox for a USCIS 6-digit MFA code newer than `since`.

    Raises TimeoutError if no matching email arrives within `max_wait_seconds`.
    """
    since = since or (datetime.now(timezone.utc) - timedelta(minutes=2))
    deadline = time.time() + max_wait_seconds

    while time.time() < deadline:
        code = _check_inbox_once(uscis_mfa_email, uscis_mfa_app_password, since)
        if code:
            return code
        time.sleep(poll_interval_seconds)

    raise TimeoutError(
        f"No USCIS MFA code from {USCIS_SENDER} "
        f"within {max_wait_seconds}s (since {since.isoformat()})."
    )


def _check_inbox_once(
    uscis_mfa_email: str,
    uscis_mfa_app_password: str,
    since: datetime,
) -> str | None:
    """One sweep of the inbox. Multiple anchors are required to accept a match:

    1. IMAP: sender is MyAccount@uscis.dhs.gov AND subject contains the MFA body phrase
       AND the message was received on/after `since` (date granularity).
    2. Python: Subject header starts with the exact MFA subject (case-insensitive).
    3. Python: Date header is newer than `since` (second-granularity filter).
    4. Python: body contains the MFA body anchor phrase.
    5. Python: body exposes the 6-digit code inside the styled `<span>`.
    """
    host, port = imap_host_port(uscis_mfa_email)
    with imaplib.IMAP4_SSL(host, port) as mail:
        mail.login(uscis_mfa_email, uscis_mfa_app_password)
        mail.select("INBOX", readonly=True)

        since_str = since.strftime("%d-%b-%Y")  # IMAP SINCE is date-only
        status, data = mail.search(
            None,
            f'(FROM "{USCIS_SENDER}" '
            f'SUBJECT "{USCIS_MFA_SUBJECT}" '
            f'SINCE {since_str})',
        )
        if status != "OK" or not data or not data[0]:
            return None

        ids = data[0].split()
        # Newest first: higher UIDs are more recent (typical on most IMAP servers)
        for num in reversed(ids):
            status, msg_data = mail.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data or not msg_data[0]:
                continue
            msg = email.message_from_bytes(msg_data[0][1])

            # Anchor 2: explicit subject match (defense in depth vs. IMAP quirks)
            subject = (msg["Subject"] or "").strip()
            if not subject.lower().startswith(USCIS_MFA_SUBJECT.lower()):
                continue

            # Anchor 3: second-granularity freshness filter
            try:
                msg_date = parsedate_to_datetime(msg["Date"])
            except (TypeError, ValueError):
                continue
            if msg_date.tzinfo is None:
                msg_date = msg_date.replace(tzinfo=timezone.utc)
            if msg_date < since:
                continue

            body = _extract_body(msg)

            # Anchor 4: the known MFA body phrase must appear
            if USCIS_MFA_BODY_ANCHOR.lower() not in body.lower():
                continue

            # Anchor 5: the code must appear inside the styled span
            code = _extract_code(body)
            if code:
                return code

    return None


def _extract_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        # The code anchor regex requires HTML markup (<span> tags), so
        # prefer HTML parts; fall back to plain if none are present. We
        # never concatenate the two — mixing them could let the fallback
        # regex match tags that span the part boundary.
        html_parts: list[str] = []
        plain_parts: list[str] = []
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype not in ("text/plain", "text/html"):
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            text = payload.decode(
                part.get_content_charset() or "utf-8", errors="ignore"
            )
            (html_parts if ctype == "text/html" else plain_parts).append(text)
        return "\n".join(html_parts or plain_parts)

    payload = msg.get_payload(decode=True)
    if payload:
        return payload.decode(
            msg.get_content_charset() or "utf-8", errors="ignore"
        )
    return ""
