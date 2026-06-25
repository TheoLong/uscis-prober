# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pick IMAP/SMTP hosts automatically from the `uscis_mfa_email` domain.

Every supported provider is equal — the user supplies their email and
app password, we look up the hosts. Unknown domains fall back to the
first entry in `_HOSTS` (currently Gmail, purely because it's the most
common; change the `DEFAULT` constant if you want a different fallback).

To add a provider, append to `_HOSTS`. No config schema change needed.
"""

from __future__ import annotations

# domain -> (imap_host, imap_port, smtp_host, smtp_port)
_HOSTS: dict[str, tuple[str, int, str, int]] = {
    "gmail.com":      ("imap.gmail.com",       993, "smtp.gmail.com",     587),
    "googlemail.com": ("imap.gmail.com",       993, "smtp.gmail.com",     587),
    "outlook.com":    ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "hotmail.com":    ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "live.com":       ("outlook.office365.com", 993, "smtp.office365.com", 587),
    "icloud.com":     ("imap.mail.me.com",      993, "smtp.mail.me.com",    587),
    "me.com":         ("imap.mail.me.com",      993, "smtp.mail.me.com",    587),
    "mac.com":        ("imap.mail.me.com",      993, "smtp.mail.me.com",    587),
    "yahoo.com":      ("imap.mail.yahoo.com",   993, "smtp.mail.yahoo.com", 587),
    "fastmail.com":   ("imap.fastmail.com",     993, "smtp.fastmail.com",   587),
}

DEFAULT = _HOSTS["gmail.com"]


def _domain(email: str) -> str:
    return (email or "").split("@")[-1].strip().lower()


def imap_host_port(email: str) -> tuple[str, int]:
    """Return (imap_host, imap_port) for the email's domain; default if unknown."""
    imap_host, imap_port, _, _ = _HOSTS.get(_domain(email), DEFAULT)
    return imap_host, imap_port


def smtp_host_port(email: str) -> tuple[str, int]:
    """Return (smtp_host, smtp_port) for the email's domain; default if unknown."""
    _, _, smtp_host, smtp_port = _HOSTS.get(_domain(email), DEFAULT)
    return smtp_host, smtp_port
