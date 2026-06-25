# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Poll an IMAP inbox for the latest USCIS MFA code.

IMAP host is picked automatically from the email domain (see
`providers.py`). The credentials come from
`config.json.auth.uscis_mfa_email` + `uscis_mfa_app_password` and must
be an app password (MFA enabled on the provider).

Diagnostics
-----------
The MFA fetch flow is the single most common silent-failure surface in
the whole tracker (USCIS has rewritten email copy, IMAP servers apply
their own timezone to SINCE filters, spam filters drop messages,
etc.). Every branch in this module emits a categorised reason code
so that when a pull fails we can reconstruct exactly *why* without
SSHing into the server:

  - `sys_log` (the durable event log shown in the dashboard) gets
    one event per fetch lifecycle: started, succeeded, timed_out.
    The timeout event carries the full dict of per-branch reason
    counts, the IMAP host, the exact SEARCH query used, the UIDs the
    server returned, and the `since` timestamp. That's everything
    you need to triage the next failure from the dashboard alone.
  - The Python logger (stderr, `journalctl -u uscis-checker`) gets a
    line per poll cycle and a line per candidate message — useful
    when you want the full trace of every decision the scanner made.
"""

from __future__ import annotations

import email
import email.message
import html
import imaplib
import logging
import re
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

from providers import imap_host_port
from system_log import log as sys_log

logger = logging.getLogger(__name__)

# Canonical set of reason codes — one per possible exit branch in the
# scanner.  Kept as an explicit catalogue so that (a) new branches
# force us to add a new key (rather than silently mis-categorising),
# and (b) dashboards/alerts can key off stable strings.
REASON_PROVIDER_LOOKUP_FAILED = "provider_lookup_failed"
REASON_IMAP_CONNECT_FAILED    = "imap_connect_failed"
REASON_IMAP_LOGIN_FAILED      = "imap_login_failed"
REASON_IMAP_SELECT_FAILED     = "imap_select_failed"
REASON_IMAP_SEARCH_FAILED     = "imap_search_failed"
REASON_IMAP_SEARCH_EMPTY      = "imap_search_empty"
REASON_FETCH_FAILED           = "fetch_failed"
REASON_PARSE_FAILED           = "parse_failed"
REASON_SUBJECT_MISMATCH       = "subject_mismatch"
REASON_BAD_DATE_HEADER        = "bad_date_header"
REASON_STALE                  = "stale"
REASON_NO_CODE_EXTRACTED      = "no_code_extracted"
REASON_UNEXPECTED_EXCEPTION   = "unexpected_exception"
REASON_ACCEPTED               = "accepted"

USCIS_SENDER = "MyAccount@uscis.dhs.gov"
USCIS_MFA_SUBJECT = "Secure two-step verification notification"

# Because the IMAP SEARCH doesn't use SINCE (Gmail TZ bug — see
# `_check_inbox_once` docstring), the server can return every
# historical USCIS MFA email.  Scan only the most recent N; the MFA
# we're waiting for is seconds old and reverse-UID iteration returns
# on first match, so this cap is pure defence against unbounded
# mailbox growth.
MAX_SEARCH_IDS_SCANNED = 50


_STYLE_BLOCK_RE = re.compile(r"<style[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
# Wipe CSS hex-colour tokens that might survive HTML stripping (inline
# mentions in body copy, preview snippets, etc.). `#333333` would
# otherwise match the 6-digit regex below.
_HEX_COLOUR_RE = re.compile(r"#[0-9a-fA-F]{3,8}\b")
_SIX_DIGITS_RE = re.compile(r"\b(\d{6})\b")


def _extract_code(body: str) -> str | None:
    """Extract the 6-digit USCIS MFA code from an email body.

    Strategy — rendered-text extraction:
      1. Remove <style>...</style> blocks (their CSS rules contain hex
         colours like `#333333` that would otherwise look like 6-digit
         tokens).
      2. Strip every remaining HTML tag — including `style="..."`
         attributes on arbitrary tags, which would also leak hex colours.
      3. Decode HTML entities so `&#48;` etc. don't mask a digit.
      4. Return the first standalone 6-digit token in the plain text.

    This is deliberately template-agnostic.  USCIS has changed the
    anchor wording at least once (2026-04-20: "MFA code" -> "verification
    code") and will probably tweak it again.  The sender + subject +
    freshness filters in `_check_inbox_once` already guarantee the
    email IS the MFA email for the current login; the only job of this
    function is to pull the code out of whatever template they ship.

    Returns None if no 6-digit token survives the HTML stripping — we
    refuse to return hex colours or other style fragments.
    """
    without_style = _STYLE_BLOCK_RE.sub(" ", body)
    plain = _TAG_RE.sub(" ", without_style)
    plain = html.unescape(plain)
    plain = _HEX_COLOUR_RE.sub(" ", plain)
    m = _SIX_DIGITS_RE.search(plain)
    return m.group(1) if m else None


def fetch_latest_code(
    uscis_mfa_email: str,
    uscis_mfa_app_password: str,
    *,
    since: datetime | None = None,
    max_wait_seconds: int = 180,
    poll_interval_seconds: int = 5,
    collector=None,
) -> str:
    """Poll the inbox for a USCIS 6-digit MFA code newer than `since`.

    Raises TimeoutError if no matching email arrives within
    `max_wait_seconds`. On timeout the error message AND a durable
    `mfa_fetch_timeout` system-log event both contain per-branch
    reason counters, the IMAP host queried, the SEARCH query string,
    and the UIDs the last cycle returned.

    When `collector` (a TraceCollector) is supplied, every IMAP
    command + response + timing + gate check is recorded as a
    wire-level event, and every raw email fetched is stored in the
    collector's email dict keyed by UID. Persisted only when the
    caller asks (e.g. on failure or trace_successful_pulls=true) —
    gives absolute replay/diagnosis capability without a second
    MFA cycle.
    """
    since = since or (datetime.now(timezone.utc) - timedelta(minutes=2))
    deadline = time.time() + max_wait_seconds
    started_at = time.time()

    # Resolve the IMAP host up front so we can record it even if the
    # first cycle's connect fails.
    try:
        host, port = imap_host_port(uscis_mfa_email)
        imap_host = f"{host}:{port}"
    except Exception as e:  # pragma: no cover — defensive, provider map is static
        imap_host = f"<lookup-failed: {type(e).__name__}>"

    sys_log(
        "mfa_fetch_started",
        source="auth",
        imap_host=imap_host,
        since=since.isoformat(),
        max_wait_seconds=max_wait_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )
    logger.info(
        "mfa/fetch started: host=%s since=%s max_wait=%ds",
        imap_host, since.isoformat(), max_wait_seconds,
    )

    # Aggregate reason counts across every poll cycle of this fetch.
    tally: dict[str, int] = {}
    # Reasons we've already surfaced as an early-warning event. Keeps
    # noise down (one event per distinct failure reason, not per
    # cycle) while still catching e.g. bad-app-password in seconds
    # instead of at the full timeout.
    reasons_surfaced: set[str] = set()
    # Keep the latest cycle's SEARCH query and returned UIDs so the
    # timeout event records what we actually saw at the end.
    last_search_query: str | None = None
    last_returned_uids: list[str] = []
    cycle_count = 0

    while time.time() < deadline:
        cycle_count += 1
        logger.debug("mfa/cycle %d start (elapsed=%.1fs)",
                     cycle_count, time.time() - started_at)

        code, search_query, returned_uids = _check_inbox_once(
            uscis_mfa_email,
            uscis_mfa_app_password,
            since,
            tally=tally,
            collector=collector,
            cycle=cycle_count,
        )
        if search_query is not None:
            last_search_query = search_query
        if returned_uids is not None:
            last_returned_uids = returned_uids

        # Surface any NEW failure reason as a warning on its first
        # occurrence. Fast feedback for IMAP-side problems (bad app
        # password, provider outage, SSL handshake failure) so
        # operators don't have to wait for the full deadline to see
        # *what* went wrong. Benign reasons (no match yet, stale
        # message, wrong subject) are suppressed — a healthy fetch
        # cycles through those every time.
        benign = {
            REASON_IMAP_SEARCH_EMPTY,
            REASON_STALE,
            REASON_SUBJECT_MISMATCH,
            REASON_ACCEPTED,
        }
        for reason, count in tally.items():
            if reason in reasons_surfaced:
                continue
            if reason in benign:
                reasons_surfaced.add(reason)
                continue
            sys_log(
                "mfa_fetch_cycle_error",
                level="warning",
                source="auth",
                imap_host=imap_host,
                cycle=cycle_count,
                reason=reason,
                reason_count=count,
                note=(
                    "First cycle observing this failure mode — "
                    "surfacing early so the operator doesn't have "
                    "to wait for the full fetch timeout."
                ),
            )
            reasons_surfaced.add(reason)

        if code:
            elapsed = round(time.time() - started_at, 2)
            sys_log(
                "mfa_fetch_succeeded",
                source="auth",
                imap_host=imap_host,
                cycles=cycle_count,
                elapsed_seconds=elapsed,
                reasons=dict(tally),  # snapshot
                code_length=len(code),
            )
            logger.info(
                "mfa/fetch succeeded after cycle %d (%.2fs): code_length=%d",
                cycle_count, elapsed, len(code),
            )
            return code

        time.sleep(poll_interval_seconds)

    elapsed = round(time.time() - started_at, 2)
    sys_log(
        "mfa_fetch_timeout",
        level="error",
        source="auth",
        imap_host=imap_host,
        cycles=cycle_count,
        elapsed_seconds=elapsed,
        since=since.isoformat(),
        last_search_query=last_search_query,
        last_returned_uids=last_returned_uids,
        reasons=dict(tally),
    )
    logger.error(
        "mfa/fetch timed out: host=%s cycles=%d elapsed=%.2fs "
        "last_query=%r last_uids=%s reasons=%s",
        imap_host, cycle_count, elapsed,
        last_search_query, last_returned_uids, tally,
    )

    raise TimeoutError(
        f"No USCIS MFA code from {USCIS_SENDER} within {max_wait_seconds}s "
        f"(since {since.isoformat()}). "
        f"cycles={cycle_count} host={imap_host} "
        f"last_query={last_search_query!r} "
        f"last_uids={last_returned_uids} reasons={tally}"
    )


def _check_inbox_once(
    uscis_mfa_email: str,
    uscis_mfa_app_password: str,
    since: datetime,
    *,
    tally: dict[str, int] | None = None,
    collector=None,
    cycle: int = 0,
) -> tuple[str | None, str | None, list[str] | None]:
    """One sweep of the inbox. Accepted when every gate passes:

    1. IMAP: sender is MyAccount@uscis.dhs.gov AND subject contains the MFA
       subject.  NOTE: we do NOT include a SINCE clause — Gmail evaluates
       SINCE in server-local (Pacific) time, so an email dated
       `2026-04-22T00:00:09Z` is indexed as 21-Apr on Gmail's side and
       `SINCE 22-Apr-2026` silently excludes it.  The Python-side
       Date-header gate below is the authoritative freshness check.
    2. Python: Subject header starts with the exact MFA subject (case-insensitive).
    3. Python: Date header is newer than `since` (second-granularity filter).
    4. Python: body yields a 6-digit code after HTML stripping
       (template-agnostic — see `_extract_code`).

    Returns a 3-tuple `(code, search_query, returned_uids)`:
      - `code`          : the extracted MFA code, or None if no candidate
                          passed every gate this cycle.
      - `search_query`  : the exact IMAP SEARCH query string we sent.
                          Captured so the caller can record it on a
                          timeout event — the query literal is the
                          single most important piece of data for
                          reasoning about a failing search.
      - `returned_uids` : the UIDs IMAP SEARCH returned (decoded), in the
                          order the server gave them.

    If `tally` is passed, every branch that exits without returning a
    code increments a stable reason key in the dict — see the REASON_*
    catalogue at the top of the module.  The caller can aggregate these
    across poll cycles to produce a post-mortem summary.
    """
    def bump(reason: str) -> None:
        if tally is not None:
            tally[reason] = tally.get(reason, 0) + 1

    def rec(event: str, **fields) -> None:
        """Wire-level trace recorder. No-op when no collector."""
        if collector is not None:
            collector.record_mfa_event(event, cycle=cycle, **fields)

    # --- host lookup ---
    try:
        host, port = imap_host_port(uscis_mfa_email)
    except Exception as e:  # pragma: no cover — provider lookup is static
        logger.warning("mfa/provider lookup failed: %s: %s", type(e).__name__, e)
        bump(REASON_PROVIDER_LOOKUP_FAILED)
        rec("provider_lookup_failed",
            error=f"{type(e).__name__}: {e}"[:200])
        return None, None, None

    rec("imap_connect_started", host=host, port=port)
    search_query: str | None = None
    returned_uids: list[str] = []

    # --- IMAP session ---
    t0 = time.time()
    try:
        mail = imaplib.IMAP4_SSL(host, port)
    except Exception as e:
        logger.warning("mfa/IMAP connect failed: %s: %s", type(e).__name__, e)
        bump(REASON_IMAP_CONNECT_FAILED)
        rec("imap_connect_failed", host=host, port=port,
            duration_ms=int((time.time() - t0) * 1000),
            error=f"{type(e).__name__}: {e}"[:200])
        return None, None, None
    rec("imap_connect_ok", host=host, port=port,
        duration_ms=int((time.time() - t0) * 1000))

    try:
        with mail:
            # --- login ---
            t0 = time.time()
            try:
                mail.login(uscis_mfa_email, uscis_mfa_app_password)
            except Exception as e:
                logger.error("mfa/IMAP login failed: %s: %s",
                             type(e).__name__, e)
                bump(REASON_IMAP_LOGIN_FAILED)
                rec("imap_login_failed",
                    duration_ms=int((time.time() - t0) * 1000),
                    error=f"{type(e).__name__}: {e}"[:200])
                return None, None, None
            rec("imap_login_ok",
                duration_ms=int((time.time() - t0) * 1000))

            # --- select INBOX ---
            t0 = time.time()
            try:
                sel_status, sel_data = mail.select("INBOX", readonly=True)
            except Exception as e:
                logger.error("mfa/IMAP select INBOX failed: %s: %s",
                             type(e).__name__, e)
                bump(REASON_IMAP_SELECT_FAILED)
                rec("imap_select_failed", mailbox="INBOX",
                    duration_ms=int((time.time() - t0) * 1000),
                    error=f"{type(e).__name__}: {e}"[:200])
                return None, None, None
            if sel_status != "OK":
                logger.error("mfa/IMAP select INBOX returned %s", sel_status)
                bump(REASON_IMAP_SELECT_FAILED)
                rec("imap_select_failed", mailbox="INBOX", status=sel_status,
                    duration_ms=int((time.time() - t0) * 1000))
                return None, None, None
            # sel_data[0] is typically the message count as bytes.
            try:
                msg_count = int((sel_data[0] or b"0").decode("ascii"))
            except Exception:
                msg_count = None
            rec("imap_select_ok", mailbox="INBOX",
                message_count=msg_count,
                duration_ms=int((time.time() - t0) * 1000))

            # --- build + run search ---
            search_query = (
                f'(FROM "{USCIS_SENDER}" '
                f'SUBJECT "{USCIS_MFA_SUBJECT}")'
            )
            rec("imap_search_started", query=search_query)
            t0 = time.time()
            try:
                status, data = mail.search(None, search_query)
            except Exception as e:
                logger.error("mfa/IMAP search errored: %s: %s",
                             type(e).__name__, e)
                bump(REASON_IMAP_SEARCH_FAILED)
                rec("imap_search_failed", query=search_query,
                    duration_ms=int((time.time() - t0) * 1000),
                    error=f"{type(e).__name__}: {e}"[:200])
                return None, search_query, returned_uids

            if status != "OK":
                logger.warning("mfa/IMAP search non-OK status=%s query=%r",
                               status, search_query)
                bump(REASON_IMAP_SEARCH_FAILED)
                rec("imap_search_failed", query=search_query, status=status,
                    duration_ms=int((time.time() - t0) * 1000))
                return None, search_query, returned_uids

            if not data or not data[0]:
                logger.info("mfa/IMAP search empty: query=%r", search_query)
                bump(REASON_IMAP_SEARCH_EMPTY)
                rec("imap_search_empty", query=search_query,
                    duration_ms=int((time.time() - t0) * 1000))
                return None, search_query, returned_uids

            all_ids = data[0].split()
            ids = all_ids[-MAX_SEARCH_IDS_SCANNED:]
            returned_uids = [x.decode("ascii", errors="replace") for x in ids]
            logger.info(
                "mfa/IMAP search returned %d total uid(s), scanning last %d: %s",
                len(all_ids), len(ids), returned_uids,
            )
            rec("imap_search_ok", query=search_query,
                total_uids=len(all_ids), scanning_uids=len(ids),
                uids=returned_uids,
                duration_ms=int((time.time() - t0) * 1000))

            # --- walk newest-first through returned IDs ---
            for num in reversed(ids):
                uid_s = num.decode("ascii", errors="replace")

                t0 = time.time()
                try:
                    status, msg_data = mail.fetch(num, "(RFC822)")
                except Exception as e:
                    logger.warning("mfa/uid=%s fetch errored: %s: %s",
                                   uid_s, type(e).__name__, e)
                    bump(REASON_FETCH_FAILED)
                    rec("imap_fetch_failed", uid=uid_s,
                        duration_ms=int((time.time() - t0) * 1000),
                        error=f"{type(e).__name__}: {e}"[:200])
                    continue
                if status != "OK" or not msg_data or not msg_data[0]:
                    logger.warning("mfa/uid=%s fetch non-OK: status=%s",
                                   uid_s, status)
                    bump(REASON_FETCH_FAILED)
                    rec("imap_fetch_failed", uid=uid_s, status=status,
                        duration_ms=int((time.time() - t0) * 1000))
                    continue

                raw_bytes = msg_data[0][1] or b""
                rec("imap_fetch_ok", uid=uid_s,
                    bytes=len(raw_bytes),
                    duration_ms=int((time.time() - t0) * 1000))
                # Persist every raw email considered (accepted OR
                # rejected). A regex break on an unanticipated template
                # is diagnosable only with the actual body in hand.
                if collector is not None:
                    collector.record_mfa_email(uid_s, raw_bytes)

                try:
                    msg = email.message_from_bytes(raw_bytes)
                except Exception as e:  # pragma: no cover — email lib is lenient
                    logger.warning("mfa/uid=%s message parse failed: %s",
                                   uid_s, e)
                    bump(REASON_PARSE_FAILED)
                    rec("email_parse_failed", uid=uid_s,
                        error=f"{type(e).__name__}: {e}"[:200])
                    continue

                # Gate 2: subject match
                subject = (msg["Subject"] or "").strip()
                if not subject.lower().startswith(USCIS_MFA_SUBJECT.lower()):
                    logger.info("mfa/uid=%s reject subject_mismatch: %r",
                                uid_s, subject[:80])
                    bump(REASON_SUBJECT_MISMATCH)
                    rec("gate_subject_match", uid=uid_s, passed=False,
                        subject=subject[:200])
                    continue
                rec("gate_subject_match", uid=uid_s, passed=True,
                    subject=subject[:200])

                # Gate 3: freshness (second-granularity)
                raw_date = msg["Date"]
                try:
                    msg_date = parsedate_to_datetime(raw_date)
                except (TypeError, ValueError) as e:
                    logger.info("mfa/uid=%s reject bad_date_header: %r (%s)",
                                uid_s, raw_date, e)
                    bump(REASON_BAD_DATE_HEADER)
                    rec("gate_date_header", uid=uid_s, passed=False,
                        raw_date=str(raw_date)[:200],
                        error=f"{type(e).__name__}: {e}"[:200])
                    continue
                if msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=timezone.utc)
                if msg_date < since:
                    logger.info(
                        "mfa/uid=%s reject stale: msg_date=%s < since=%s",
                        uid_s, msg_date.isoformat(), since.isoformat(),
                    )
                    bump(REASON_STALE)
                    rec("gate_freshness", uid=uid_s, passed=False,
                        msg_date=msg_date.isoformat(),
                        since=since.isoformat())
                    continue
                rec("gate_freshness", uid=uid_s, passed=True,
                    msg_date=msg_date.isoformat(),
                    since=since.isoformat())

                # Gate 4: extract the 6-digit code
                body = _extract_body(msg)
                code = _extract_code(body)
                if not code:
                    logger.warning(
                        "mfa/uid=%s reject no_code_extracted: "
                        "subject=%r date=%s body_len=%d",
                        uid_s, subject[:80],
                        msg_date.isoformat(), len(body),
                    )
                    bump(REASON_NO_CODE_EXTRACTED)
                    rec("gate_code_extraction", uid=uid_s, passed=False,
                        body_length=len(body))
                    continue
                rec("gate_code_extraction", uid=uid_s, passed=True,
                    body_length=len(body), code_length=len(code))

                logger.info(
                    "mfa/uid=%s ACCEPTED: date=%s body_len=%d",
                    uid_s, msg_date.isoformat(), len(body),
                )
                bump(REASON_ACCEPTED)
                rec("code_accepted", uid=uid_s,
                    subject=subject[:200],
                    from_=(msg.get("From") or "")[:200],
                    date=msg_date.isoformat(),
                    body_length=len(body),
                    code_length=len(code))
                return code, search_query, returned_uids

            return None, search_query, returned_uids

    except Exception as e:  # pragma: no cover — last-line defensive
        logger.exception("mfa/unexpected exception during inbox scan: %s", e)
        bump(REASON_UNEXPECTED_EXCEPTION)
        return None, search_query, returned_uids


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
