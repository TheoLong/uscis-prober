# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Format and send diff-update notifications via SMTP.

SMTP host is picked automatically from the email domain (see
`providers.py`). Uses the same credentials as MFA inbox polling —
`config.json.auth.uscis_mfa_email` + `uscis_mfa_app_password`.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from providers import smtp_host_port
from system_log import log as sys_log

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

_KIND_HEADLINE = {
    "silent_update":  "silent update",
    "event":       "new case event",
    "notice":      "new notice",
    "appointment": "appointment change",
    "decision":    "decision flag flipped",
    "status":      "status change",
}

_KIND_LEAD = {
    "silent_update":  "USCIS advanced the record's update date with no new visible event or notice. Community associates this with active internal work, but it is not predictive of timing.",
    "event":       "A new case event appeared on the record. Check the event code for meaning.",
    "notice":      "USCIS issued a new notice (e.g. Request for Evidence, receipt).",
    "appointment": "A notice with an appointment date was added, removed, or changed. Usually a biometrics (biometrics) reschedule.",
    "decision":    "A decision-related boolean (closed, actionRequired, isPremiumProcessed) changed state — this is a high-signal event.",
    "status":      "A tracked scalar changed on the record.",
}


def _describe_event(ev: dict, code_labels: dict[str, str]) -> str:
    code = ev.get("eventCode") or "?"
    label = code_labels.get(code)
    when = ev.get("eventDateTime") or "—"
    return f"{code} ({label}) @ {when}" if label else f"{code} @ {when}"


def _describe_notice(n: dict) -> str:
    appt = n.get("appointmentDateTime")
    tail = f" (appt {appt.replace('T', ' ')[:16]})" if appt else ""
    return f"{n.get('actionType') or '?'} — letter {n.get('letterId') or '?'}{tail}"


def build_update_email(
    record: dict, code_labels: dict[str, str] | None = None
) -> tuple[str, str, str]:
    """Return (subject, plain_text_body, html_body) for a single update record."""
    code_labels = code_labels or {}
    case_label = record.get("caseLabel") or "?"
    receipt = record.get("receiptNumber") or "?"
    kind = record.get("kind") or "status"
    headline = _KIND_HEADLINE.get(kind, kind)
    lead = _KIND_LEAD.get(kind, "")

    detected = record.get("detectedOn") or (record.get("to") or "")[:10]
    real = record.get("realUpdateDate")
    date_line = (
        f"Detected {detected} — actual update date {real}"
        if real and real != detected
        else f"Detected {detected}"
    )

    subject = f"[USCIS] {case_label} — {headline}"

    # ----- plain text -----
    lines: list[str] = [
        f"{case_label} ({receipt})",
        date_line,
        "",
        lead,
        "",
    ]
    scalars = record.get("scalars") or {}
    if scalars:
        lines.append("Field changes:")
        for k, v in scalars.items():
            lines.append(f"  {k}: {v.get('from')!s}  →  {v.get('to')!s}")
        lines.append("")
    for key, title in (
        ("events", "Events"),
        ("notices", "Notices"),
        ("documents", "Documents"),
        ("addendums", "Addendums"),
    ):
        coll = record.get(key) or {}
        added = coll.get("added") or []
        removed = coll.get("removed") or []
        if not (added or removed):
            continue
        lines.append(f"{title}:")
        for a in added:
            desc = _describe_event(a, code_labels) if key == "events" else (
                _describe_notice(a) if key == "notices" else str(a)
            )
            lines.append(f"  + {desc}")
        for r in removed:
            desc = _describe_event(r, code_labels) if key == "events" else (
                _describe_notice(r) if key == "notices" else str(r)
            )
            lines.append(f"  - {desc}")
        lines.append("")
    lines.append("—")
    lines.append("USCIS Prober (local dashboard)")
    plain = "\n".join(lines)

    # ----- HTML -----
    def esc(s: str) -> str:
        return (
            (str(s) if s is not None else "—")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )

    html_sections: list[str] = []
    if scalars:
        rows = "".join(
            f"<tr><td style='padding:4px 10px 4px 0;color:#64748b;font-family:monospace;'>{esc(k)}</td>"
            f"<td style='padding:4px 10px 4px 0;color:#b91c1c;text-decoration:line-through;font-family:monospace;'>{esc(v.get('from'))}</td>"
            f"<td style='padding:4px 0;color:#047857;font-family:monospace;'>{esc(v.get('to'))}</td></tr>"
            for k, v in scalars.items()
        )
        html_sections.append(
            f"<h4 style='margin:16px 0 4px;color:#475569;text-transform:uppercase;"
            f"letter-spacing:0.08em;font-size:12px;'>Field changes</h4>"
            f"<table style='border-collapse:collapse;font-size:13px;'>{rows}</table>"
        )
    for key, title in (
        ("events", "Events"),
        ("notices", "Notices"),
        ("documents", "Documents"),
        ("addendums", "Addendums"),
    ):
        coll = record.get(key) or {}
        added = coll.get("added") or []
        removed = coll.get("removed") or []
        if not (added or removed):
            continue
        items = []
        for a in added:
            desc = _describe_event(a, code_labels) if key == "events" else (
                _describe_notice(a) if key == "notices" else str(a)
            )
            items.append(
                f"<li style='color:#047857;font-family:monospace;font-size:13px;'>+ {esc(desc)}</li>"
            )
        for r in removed:
            desc = _describe_event(r, code_labels) if key == "events" else (
                _describe_notice(r) if key == "notices" else str(r)
            )
            items.append(
                f"<li style='color:#b91c1c;font-family:monospace;font-size:13px;'>− {esc(desc)}</li>"
            )
        html_sections.append(
            f"<h4 style='margin:16px 0 4px;color:#475569;text-transform:uppercase;"
            f"letter-spacing:0.08em;font-size:12px;'>{esc(title)}</h4>"
            f"<ul style='margin:0;padding-left:18px;'>{''.join(items)}</ul>"
        )

    html = f"""\
<!DOCTYPE html>
<html><body style='font-family:system-ui,-apple-system,sans-serif;color:#0f172a;
  max-width:640px;padding:24px;line-height:1.55;background:#f8fafc;'>
  <div style='background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;
    padding:22px 24px;'>
    <div style='font-size:11px;letter-spacing:0.14em;text-transform:uppercase;
      color:#64748b;margin-bottom:4px;'>USCIS update</div>
    <h2 style='margin:0 0 2px;font-size:22px;'>{esc(case_label)}
      <span style='font-family:monospace;font-size:14px;color:#64748b;
      font-weight:400;'>{esc(receipt)}</span></h2>
    <div style='color:#334155;font-size:14px;margin-bottom:2px;'>
      <strong>{esc(headline)}</strong></div>
    <div style='color:#64748b;font-size:13px;font-family:monospace;
      margin-bottom:12px;'>{esc(date_line)}</div>
    <p style='margin:0 0 4px;color:#334155;font-size:13px;'>{esc(lead)}</p>
    {''.join(html_sections)}
  </div>
  <div style='color:#94a3b8;font-size:11px;margin-top:14px;text-align:center;'>
    USCIS Prober — local dashboard
  </div>
</body></html>"""

    return subject, plain, html


# ---------------------------------------------------------------------------
# SMTP
# ---------------------------------------------------------------------------

def send_email(
    *,
    uscis_mfa_email: str,
    uscis_mfa_app_password: str,
    to: str,
    subject: str,
    plain: str,
    html: str,
) -> None:
    """Build + send a multipart email via the mailbox's provider SMTP.

    Every distinct failure stage (DNS, TLS handshake, auth, send) emits a
    categorised `smtp_*` sys_log event before re-raising, so the caller
    (typically `server._send_notifications_for_new`) sees only the
    exception and we keep a dashboard-visible breadcrumb of which step
    failed — valuable because an app-password being wrong looks nothing
    like the DNS-can't-resolve-smtp.gmail.com case in a traceback but
    the operator needs to react very differently.
    """
    msg = EmailMessage()
    msg["From"] = uscis_mfa_email
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")

    host, port = smtp_host_port(uscis_mfa_email)
    logger.info("SMTP %s → %s : %s", host, to, subject)

    try:
        smtp = smtplib.SMTP(host, port, timeout=30)
    except Exception as e:
        sys_log(
            "smtp_connect_failed", level="error", source="mailer",
            host=host, port=port, to=to,
            error=f"{type(e).__name__}: {e}"[:200],
        )
        raise

    try:
        try:
            smtp.ehlo()
            smtp.starttls()
            # RFC 3207 §4: re-issue EHLO after the TLS handshake because
            # the server's capability list can change over the encrypted
            # channel.
            smtp.ehlo()
        except Exception as e:
            sys_log(
                "smtp_tls_failed", level="error", source="mailer",
                host=host, port=port,
                error=f"{type(e).__name__}: {e}"[:200],
            )
            raise
        try:
            smtp.login(uscis_mfa_email, uscis_mfa_app_password)
        except smtplib.SMTPAuthenticationError as e:
            # Auth errors are the single most common real-world SMTP
            # failure — categorise them separately so the dashboard can
            # surface "your app password is wrong" instead of a generic
            # SMTPException.
            sys_log(
                "smtp_auth_failed", level="error", source="mailer",
                host=host, port=port, user=uscis_mfa_email,
                smtp_code=getattr(e, "smtp_code", None),
                error=f"{type(e).__name__}: {e}"[:200],
            )
            raise
        except Exception as e:
            sys_log(
                "smtp_login_failed", level="error", source="mailer",
                host=host, port=port, user=uscis_mfa_email,
                error=f"{type(e).__name__}: {e}"[:200],
            )
            raise
        try:
            smtp.send_message(msg)
        except Exception as e:
            sys_log(
                "smtp_send_failed", level="error", source="mailer",
                host=host, port=port, to=to,
                error=f"{type(e).__name__}: {e}"[:200],
            )
            raise
    finally:
        try:
            smtp.quit()
        except Exception:  # pragma: no cover — teardown best-effort
            pass


def notify_update(
    auth: dict, to: str, record: dict, code_labels: dict[str, str] | None = None
) -> None:
    """High-level helper: format and send one update email."""
    subject, plain, html = build_update_email(record, code_labels)
    send_email(
        uscis_mfa_email=auth["uscis_mfa_email"],
        uscis_mfa_app_password=auth["uscis_mfa_app_password"],
        to=to,
        subject=subject,
        plain=plain,
        html=html,
    )
