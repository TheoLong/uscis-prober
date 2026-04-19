# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compute per-day differences between USCIS case captures.

Day-binning rule: when multiple captures exist for a single calendar day
(UTC), keep only the last one. The daily series is the chronological list
of those "last of day" snapshots.

Change extraction: for each consecutive pair of day-binned snapshots,
record which scalar flags flipped, which events/notices/documents were
added or removed, and whether `updatedAt` advanced. Each change is then
*classified* into a category (silent, event, notice, appointment,
decision, status) so the UI can emphasise signal over noise.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any


# Scalar fields surfaced in the diff output.
_WATCHED_SCALARS: tuple[str, ...] = (
    "updatedAt",
    "updatedAtTimestamp",
    "closed",
    "actionRequired",
    "ackedByAdjudicatorAndCms",
    "areAllGroupStatusesComplete",
    "areAllGroupMembersAuthorizedForTravel",
    "isPremiumProcessed",
    "cmsFailure",
    "statusTitle",
    "statusText",
)

# Timestamp-only fields: when ONLY these change (and nothing else), the
# change is a "silent update" — internal USCIS activity with no visible
# event or notice. Community (Lawfully/1point3acres) treats this as a
# positive sign that someone is actively working the case.
_TIMESTAMP_ONLY: frozenset[str] = frozenset({"updatedAt", "updatedAtTimestamp"})

# Decision-readiness scalar flips the community tracks.
_DECISION_FLAGS: frozenset[str] = frozenset(
    {"ackedByAdjudicatorAndCms", "closed", "actionRequired", "isPremiumProcessed"}
)

# Short labels for USCIS event codes we see in practice.
#
# Caveats from the research:
#   - Codes are two-or-three letter action abbreviations + a digit variant.
#     `0` is the default template; `1`/`2` are alternates for specific
#     form types or situations (e.g. FTA1 vs FTA0).
#   - Typical I-485 happy path: RCV0 → FTA0 → PRB0 → INT0 → APR0 → PRD0 → WCD0.
#     Many I-485s skip INT0 (72% of EB cases qualify for interview waiver).
#   - FTA0 is not itself a decision — it is a database-check workflow flag.
#     Community lore: a third FTA0 is often followed by approval, but this
#     is folklore, not guarantee.
EVENT_CODE_LABELS: dict[str, str] = {
    "RCV0": "Case received",
    "IAF":  "Initial acknowledgement — form received",
    "FTA0": "Biometrics received / fingerprints submitted to FBI",
    "FTA1": "Biometrics database check (variant)",
    "PRB0": "Pre-brief / pre-adjudication review",
    "INT0": "Interview scheduled",
    "RFE0": "Request for Evidence issued",
    "NTR0": "Notice to Requester",
    "NOID": "Notice of Intent to Deny",
    "APR0": "Approval",
    "H008": "Case approved (variant)",
    "DNY0": "Denial",
    "C1SC": "New card being produced",
    "PRD0": "Card/document production",
    "CRD0": "Card mailed",
    "WCD0": "Welcome letter sent",
}

# Canonical happy-path sequence for I-485 / I-765 / I-131.
#
# Deliberately *not* included: a separate "Biometrics scheduled" step.
# The USCIS JSON exposes appointment notices with actionType == "Appointment
# Scheduled" but gives no way to distinguish biometrics from interview from
# biometrics re-takes, so a scheduled-biometrics stage can't be inferred
# reliably from notices alone. Instead, a single FTA0 event means USCIS
# received the fingerprints and started the database checks — by the time
# it appears, biometrics has already happened. Collapsing the two into
# "Biometrics & checks done" is what's actually observable.
#
# Each entry is (label, trigger_codes) — a stage is reached when ANY of
# its trigger codes appears in the case's event list. The latest reached
# is taken as the current stage.
_HAPPY_PATH: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Received",                 ("RCV0", "IAF")),
    ("Biometrics & checks done", ("FTA0", "FTA1")),
    ("Pre-adjudication",         ("PRB0",)),
    ("Interview scheduled",      ("INT0",)),
    ("Approved",                 ("APR0", "H008")),
    ("Card being produced",      ("C1SC", "PRD0")),
    ("Card mailed",              ("CRD0", "WCD0")),
)

# Off-path stages that indicate divergence from the happy path.
_DIVERGENCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Denied",           ("DNY0",)),
    ("Notice of Intent to Deny outstanding", ("NOID",)),
    ("Request for Evidence outstanding",  ("RFE0",)),
)

CANONICAL_STAGES: tuple[str, ...] = tuple(label for label, _ in _HAPPY_PATH)


def infer_stage(events: list[dict]) -> str:
    """Pick the latest milestone reached from the observed event codes.

    Divergences (Request for Evidence / Notice of Intent to Deny/Denied) take precedence when present — they are
    action-critical states even if later codes also exist on the record.
    """
    codes = {(e or {}).get("eventCode") for e in (events or [])}
    for label, triggers in _DIVERGENCES:
        if any(t in codes for t in triggers):
            return label
    # Walk the happy path from latest to earliest.
    for label, triggers in reversed(_HAPPY_PATH):
        if any(t in codes for t in triggers):
            return label
    return "Pending receipt"


def stage_progression(events: list[dict]) -> dict:
    """Describe where the case sits on the canonical happy path.

    Returns:
        {
          "current": "<stage label>",
          "divergence": "<divergence label>" | None,
          "steps": [
            { "label": "...", "state": "passed" | "current" | "upcoming" },
            ...
          ]
        }
    """
    codes = {(e or {}).get("eventCode") for e in (events or [])}

    divergence = None
    for label, triggers in _DIVERGENCES:
        if any(t in codes for t in triggers):
            divergence = label
            break

    # Find the highest-index happy-path stage actually reached.
    reached_index = -1
    for i, (_, triggers) in enumerate(_HAPPY_PATH):
        if any(t in codes for t in triggers):
            reached_index = i

    steps = []
    for i, (label, _) in enumerate(_HAPPY_PATH):
        if i < reached_index:
            state = "passed"
        elif i == reached_index:
            state = "current"
        else:
            state = "upcoming"
        steps.append({"label": label, "state": state})

    current = (
        divergence
        if divergence
        else (_HAPPY_PATH[reached_index][0] if reached_index >= 0 else "Pending receipt")
    )
    return {"current": current, "divergence": divergence, "steps": steps}


def day_of(iso_ts: str) -> str:
    """Calendar day (UTC) from an ISO-8601 timestamp like '2026-04-18T22:43:21Z'."""
    return (iso_ts or "")[:10]


def bin_by_day(entries: list[dict]) -> list[dict]:
    """Return the latest entry for each calendar day, in chronological order.

    Each entry must have a `capturedAt` ISO timestamp. Entries sharing a
    day are collapsed to the one with the most recent `capturedAt`.
    """
    by_day: "OrderedDict[str, dict]" = OrderedDict()
    for entry in sorted(entries, key=lambda e: e.get("capturedAt", "")):
        day = day_of(entry.get("capturedAt", ""))
        if not day:
            continue
        by_day[day] = entry  # overwrite → keeps latest for that day
    return list(by_day.values())


def _key_event(ev: dict) -> str:
    return ev.get("eventId") or f"{ev.get('eventCode')}@{ev.get('eventDateTime')}"


def _key_notice(n: dict) -> str:
    return n.get("letterId") or f"{n.get('actionType')}@{n.get('generationDate')}"


def _key_document(d: dict) -> str:
    return d.get("id") or d.get("documentId") or repr(sorted(d.items()))[:64]


def _diff_collection(
    prev: list[dict], curr: list[dict], key_fn
) -> dict[str, list[dict]]:
    prev_map = {key_fn(x): x for x in (prev or [])}
    curr_map = {key_fn(x): x for x in (curr or [])}
    added = [curr_map[k] for k in curr_map if k not in prev_map]
    removed = [prev_map[k] for k in prev_map if k not in curr_map]
    return {"added": added, "removed": removed}


def compute_change(prev: dict, curr: dict) -> dict:
    """Describe what's different from `prev` to `curr` (both day-binned entries).

    Returns a dict with the shape the UI expects:
      {
        "from": capturedAt, "to": capturedAt,
        "scalars": { field: {"from": ..., "to": ...}, ... },
        "events":    {"added": [...], "removed": [...]},
        "notices":   {"added": [...], "removed": [...]},
        "documents": {"added": [...], "removed": [...]},
        "addendums": {"added": [...], "removed": [...]},
      }
    """
    prev_d = prev.get("data") or {}
    curr_d = curr.get("data") or {}

    scalars: dict[str, dict[str, Any]] = {}
    for k in _WATCHED_SCALARS:
        if prev_d.get(k) != curr_d.get(k) and (k in prev_d or k in curr_d):
            scalars[k] = {"from": prev_d.get(k), "to": curr_d.get(k)}

    return {
        "from": prev.get("capturedAt"),
        "to": curr.get("capturedAt"),
        "scalars": scalars,
        "events": _diff_collection(
            prev_d.get("events"), curr_d.get("events"), _key_event
        ),
        "notices": _diff_collection(
            prev_d.get("notices"), curr_d.get("notices"), _key_notice
        ),
        "documents": _diff_collection(
            prev_d.get("documents"), curr_d.get("documents"), _key_document
        ),
        "addendums": _diff_collection(
            prev_d.get("addendums"), curr_d.get("addendums"), _key_document
        ),
    }


def day_changes(entries: list[dict]) -> list[dict]:
    """Full change feed: one entry per day-pair where something differed.

    Each item is enriched with a `kind` classification (see classify_change)
    so the UI can label silent updates vs. new events vs. appointment moves.
    """
    days = bin_by_day(entries)
    feed: list[dict] = []
    for prev, curr in zip(days, days[1:]):
        change = compute_change(prev, curr)
        if not _has_any_diff(change):
            continue
        change["kind"] = classify_change(change)
        feed.append(change)
    return feed


def classify_change(change: dict) -> str:
    """Bucket a diff into the single most specific signal.

    Priority order (most specific first):
      decision    — ackedByAdjudicatorAndCms/closed/actionRequired/isPremiumProcessed flip
      event       — new case event appeared (FTA0, APR0, etc.)
      appointment — a notice with appointmentDateTime appeared or changed
      notice      — a non-appointment notice appeared
      silent_update  — `updatedAt` date advanced, no events/notices. Real silent update.
      same_day_refresh — only `updatedAtTimestamp` advanced within the same day.
                    Often a sync artifact or weak touch.
      status      — fallback: tracked scalar changed that isn't covered above
    """
    scalars = change.get("scalars") or {}
    events = change.get("events") or {}
    notices = change.get("notices") or {}

    if any(k in _DECISION_FLAGS for k in scalars):
        return "decision"
    if events.get("added"):
        return "event"

    notices_added = notices.get("added") or []
    notices_removed = notices.get("removed") or []
    if any((n or {}).get("appointmentDateTime") for n in notices_added + notices_removed):
        return "appointment"
    if notices_added:
        return "notice"

    timestamp_only = (
        scalars
        and all(k in _TIMESTAMP_ONLY for k in scalars)
        and not events.get("added") and not events.get("removed")
        and not notices_added and not notices_removed
        and not (change.get("documents") or {}).get("added")
        and not (change.get("documents") or {}).get("removed")
    )
    if timestamp_only:
        return "silent_update" if "updatedAt" in scalars else "same_day_refresh"

    return "status"


def _has_any_diff(change: dict) -> bool:
    if change.get("scalars"):
        return True
    for key in ("events", "notices", "documents", "addendums"):
        coll = change.get(key) or {}
        if coll.get("added") or coll.get("removed"):
            return True
    return False


# ---------------------------------------------------------------------------
# Aggregate per-case signals (computed server-side so the UI can render fast)
# ---------------------------------------------------------------------------

def _days_between(a_iso: str | None, b_iso: str | None) -> int | None:
    """Whole-day delta |b - a|, from YYYY-MM-DD(T...) strings."""
    if not a_iso or not b_iso:
        return None
    try:
        from datetime import date
        a = date.fromisoformat(a_iso[:10])
        b = date.fromisoformat(b_iso[:10])
        return abs((b - a).days)
    except ValueError:
        return None


def summarize_case(entries: list[dict], *, today_iso: str) -> dict:
    """Compute the aggregate signals the UI's Overview shows.

    Pure function — `today_iso` is passed in so tests are deterministic.
    """
    days = bin_by_day(entries)
    latest = (days[-1] if days else {}) or {}
    latest_data = latest.get("data") or {}

    # Distinct `updatedAt` dates observed over the pull history. Every move
    # is something USCIS did on their side. Does NOT count same-day timestamp
    # fluctuations (those are tracked separately as same-day refreshes).
    distinct_updated_at = []
    seen = set()
    for d in days:
        u = (d.get("data") or {}).get("updatedAt")
        if u and u not in seen:
            seen.add(u)
            distinct_updated_at.append(u)

    changes = day_changes(entries)
    silent_update_count = sum(1 for c in changes if c.get("kind") == "silent_update")
    silent_ping_count = sum(1 for c in changes if c.get("kind") == "same_day_refresh")

    # Event-code metrics the community specifically tracks.
    events = latest_data.get("events") or []
    fta0_count = sum(1 for e in events if (e or {}).get("eventCode") == "FTA0")
    event_codes = sorted({(e or {}).get("eventCode") for e in events if e} - {None})

    # "Last activity" — the most recent classified diff, with BOTH the
    # detection date (when we noticed) and the actual USCIS-side date of
    # record (`to` updatedAt). For silent_update, these can differ by weeks.
    last_activity: dict | None = None
    if changes:
        last = changes[-1]
        last_to_data = (last.get("to") or "")[:10]
        # actual update date from the updatedAt scalar transition, if available
        updated_at_change = (last.get("scalars") or {}).get("updatedAt") or {}
        real_update_date = updated_at_change.get("to")
        last_activity = {
            "kind": last.get("kind"),
            "detectedOn": last_to_data,
            "realUpdateDate": real_update_date,
        }

    # Upcoming appointment: the nearest future-dated notice appointment.
    upcoming: dict | None = None
    for n in latest_data.get("notices") or []:
        appt = n.get("appointmentDateTime")
        if not appt or appt[:10] < today_iso:
            continue
        if upcoming is None or appt < upcoming["appointmentDateTime"]:
            upcoming = n
    if upcoming:
        upcoming = dict(upcoming)
        upcoming["daysUntil"] = _days_between(upcoming["appointmentDateTime"][:10], today_iso)

    # High-signal collection sizes — a Request for Evidence shows up in evidenceRequests;
    # approval notices show up in documents.
    evidence_count = len(latest_data.get("evidenceRequests") or [])
    document_count = len(latest_data.get("documents") or [])

    # "All updates" = what the UI's combined Timeline actually shows:
    # every event row on the latest snapshot, plus every silent-update
    # diff we've observed across the capture history.
    all_updates = len(events) + silent_update_count

    return {
        "daysPending": _days_between(latest_data.get("submissionDate"), today_iso),
        "daysSinceUpdate": _days_between(latest_data.get("updatedAt"), today_iso),
        "uscisUpdates": len(distinct_updated_at),
        "allUpdates": all_updates,
        "silentUpdates": silent_update_count,
        "sameDayRefreshes": silent_ping_count,
        "fta0Count": fta0_count,
        "eventCodes": event_codes,
        "stage": infer_stage(events),
        "progression": stage_progression(events),
        "evidenceRequestCount": evidence_count,
        "documentCount": document_count,
        "upcomingAppointment": upcoming,
        "lastActivity": last_activity,
    }
