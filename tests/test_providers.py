# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Domain → host/port lookup."""

import pytest

from providers import imap_host_port, smtp_host_port


@pytest.mark.parametrize("email,expected_imap,expected_smtp", [
    ("a@gmail.com",       "imap.gmail.com",        "smtp.gmail.com"),
    ("a@googlemail.com",  "imap.gmail.com",        "smtp.gmail.com"),
    ("a@outlook.com",     "outlook.office365.com", "smtp.office365.com"),
    ("a@hotmail.com",     "outlook.office365.com", "smtp.office365.com"),
    ("a@live.com",        "outlook.office365.com", "smtp.office365.com"),
    ("a@icloud.com",      "imap.mail.me.com",      "smtp.mail.me.com"),
    ("a@me.com",          "imap.mail.me.com",      "smtp.mail.me.com"),
    ("a@mac.com",         "imap.mail.me.com",      "smtp.mail.me.com"),
    ("a@yahoo.com",       "imap.mail.yahoo.com",   "smtp.mail.yahoo.com"),
    ("a@fastmail.com",    "imap.fastmail.com",     "smtp.fastmail.com"),
])
def test_known_providers(email, expected_imap, expected_smtp):
    assert imap_host_port(email)[0] == expected_imap
    assert smtp_host_port(email)[0] == expected_smtp


def test_unknown_domain_falls_back_to_default():
    assert imap_host_port("x@self-hosted.example")[0] == "imap.gmail.com"
    assert smtp_host_port("x@self-hosted.example")[0] == "smtp.gmail.com"


def test_empty_or_none_falls_back_to_default():
    assert imap_host_port("")[0] == "imap.gmail.com"
    assert smtp_host_port("")[0] == "smtp.gmail.com"
    assert imap_host_port(None)[0] == "imap.gmail.com"


def test_domain_matching_is_case_insensitive():
    assert imap_host_port("user@GMAIL.COM")[0] == "imap.gmail.com"
    assert smtp_host_port("User@Outlook.Com")[0] == "smtp.office365.com"


def test_returns_ports_too():
    _, imap_port = imap_host_port("a@gmail.com")
    _, smtp_port = smtp_host_port("a@gmail.com")
    assert imap_port == 993
    assert smtp_port == 587
