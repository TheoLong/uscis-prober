# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Compute differences between consecutive USCIS case captures.

Every capture is diffed against the one immediately before it (ordered by
`capturedAt`). For each pair we record which scalar flags flipped, which
events/notices/documents were added or removed, and whether `updatedAt`
advanced. Each change is then *classified* into a category (silent, event,
notice, appointment, decision, status) so the UI can emphasise signal over
noise.

The feed is lossless: every pair that differs at all produces exactly one
record (only byte-identical pairs are skipped). The classification is a label
for the UI — it never causes a real change to be dropped — so the feed plus an
initial snapshot fully reconstructs every later snapshot's tracked fields.
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

# Timestamp-only fields: when ONLY these change, the diff is a silent update —
# the case-level update timestamp advanced with nothing else.
_TIMESTAMP_ONLY: frozenset[str] = frozenset({"updatedAt", "updatedAtTimestamp"})

# Decision-readiness scalar flips the community tracks.
_DECISION_FLAGS: frozenset[str] = frozenset(
    {"ackedByAdjudicatorAndCms", "closed", "actionRequired", "isPremiumProcessed"}
)

# USCIS ELIS event codes observed on real I-485 / I-765 / I-131 records.
#
# IMPORTANT: USCIS does not publish the meaning of these internal event codes,
# and there is no authoritative source for them. Earlier versions of this file
# asserted English descriptions (e.g. "FTA1 = biometrics database check"), but
# those were in-house guesses based on a "the trailing digit is just a variant
# of the base code" heuristic — and at least one proved wrong in practice: a
# case's public status flipped to "Case Is Being Actively Reviewed By USCIS"
# immediately after an FTA1, which is not a biometrics step. Rather than annotate
# codes with undocumented meanings, we surface the raw code (e.g. "FTA1 @ <date>")
# and let the reader interpret it.
#
# The set below records only WHICH codes we have actually seen — no meaning is
# claimed for any of them.
OBSERVED_EVENT_CODES: frozenset[str] = frozenset({
    "RCV0", "IAF", "FTA0", "FTA1", "PRB0", "INT0", "RFE0", "NTR0",
    "NOID", "APR0", "H008", "DNY0", "C1SC", "PRD0", "CRD0", "WCD0",
})

# No human-readable annotations are asserted for event codes (see note above).
# Kept as an (empty) mapping because the API responses and the mailer reference
# it; empty means the UI and emails render the bare code with no caption.
EVENT_CODE_LABELS: dict[str, str] = {}

# Canonical happy-path sequence for I-485 / I-765 / I-131.
#
# Deliberately *not* included: a separate "Biometrics scheduled" step.
# The USCIS JSON exposes appointment notices with actionType == "Appointment
# Scheduled" but gives no way to distinguish biometrics from interview from
# biometrics re-takes, so a scheduled-biometrics stage can't be inferred
# reliably from notices alone. Instead, a single FTA0 event reliably appears
# right after the biometrics appointment, so we use FTA0 as the observable
# "Biometrics & checks done" marker.
#
# FTA1 is deliberately NOT a trigger here. Its meaning is undocumented, and in
# practice it has appeared much later than FTA0 (post-transfer) — in one case
# the public status flipped to "Case Is Being Actively Reviewed By USCIS" right
# after an FTA1, i.e. it is NOT a biometrics step. FTA0 always precedes FTA1 on
# real records, so dropping FTA1 here does not change any inferred stage.
#
# Each entry is (label, trigger_codes) — a stage is reached when ANY of
# its trigger codes appears in the case's event list. The latest reached
# is taken as the current stage.
_HAPPY_PATH: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Received",                 ("RCV0", "IAF")),
    ("Biometrics & checks done", ("FTA0",)),
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


def _key_evidence(er: dict) -> str:
    # An evidence request (RFE / NOID) is identified by its notice id; fall
    # back to a content hash so a keyless entry still diffs stably.
    return er.get("noticeId") or er.get("letterId") or repr(sorted(er.items()))[:64]


def _key_generic(x: dict) -> str:
    """Best-effort stable identity for an arbitrary list-of-dicts entry.

    Tries the common id-ish keys any USCIS sub-object might carry, then falls
    back to a hash of the sorted items so two structurally-identical entries
    collapse and any field change surfaces as a `changed` (not add+remove).
    """
    for k in ("id", "eventId", "letterId", "noticeId", "documentId", "noticeCode"):
        v = x.get(k)
        if v:
            return f"{k}:{v}"
    return "hash:" + repr(sorted((k, repr(v)) for k, v in x.items()))[:96]


# Named collections get a purpose-built key function; every other list-of-dicts
# field is diffed with the generic key so the feed stays comprehensive without
# a hand-maintained allowlist.
_COLLECTION_KEYS: dict[str, Any] = {
    "events": _key_event,
    "notices": _key_notice,
    "documents": _key_document,
    "addendums": _key_document,
    "evidenceRequests": _key_evidence,
}


def _is_scalar(v: Any) -> bool:
    return v is None or isinstance(v, (str, bool, int, float))


def _flatten_scalars(data: Any, prefix: str = "") -> dict[str, Any]:
    """Flatten every scalar leaf of `data` to a dotted-path → value map.

    Recurses into nested dicts (e.g. `elisBeneficiaryAddendum.foo`). Lists are
    NOT flattened here — list-of-dicts fields are diffed as collections, and a
    list of scalars is compared as a whole under its own key so it still counts
    toward reconstruction completeness. The result is exhaustive over the JSON's
    scalar content, so `initial snapshot + every scalar diff` reproduces every
    later snapshot's scalar fields with no allowlist.
    """
    out: dict[str, Any] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            path = f"{prefix}{k}"
            if isinstance(v, dict):
                out.update(_flatten_scalars(v, prefix=f"{path}."))
            elif isinstance(v, list):
                # Skip the whole-list scalar snapshot when the collection diff
                # fully covers the list: an EMPTY list (nothing to lose, and it
                # may later hold dicts — snapshotting [] would spuriously churn
                # to null once it does) or a PURE list-of-dicts. Any list with a
                # non-dict element (pure scalars, MIXED, nested lists) still gets
                # the scalar backstop so no non-dict element change is dropped.
                if len(v) == 0 or _is_pure_list_of_dicts(v):
                    continue
                out[path] = v
            else:
                out[path] = v
    elif _is_scalar(data):
        out[prefix.rstrip(".")] = data
    return out


def _is_list_of_dicts(v: Any) -> bool:
    """True if the list contains at least one dict (may be mixed)."""
    return isinstance(v, list) and any(isinstance(x, dict) for x in v)


def _is_pure_list_of_dicts(v: Any) -> bool:
    """True only if the non-empty list contains dicts and NOTHING else.

    A pure list-of-dicts is fully reconstructable from the collection diff, so
    it's excluded from the whole-list scalar snapshot. A mixed list (dicts +
    scalars) is NOT pure, so it also gets the whole-list scalar backstop — the
    collection diff alone would silently drop its non-dict elements.
    """
    return (
        isinstance(v, list)
        and len(v) > 0
        and all(isinstance(x, dict) for x in v)
    )


def _diff_scalar_map(prev: dict, curr: dict) -> dict[str, dict[str, Any]]:
    """Diff two flattened scalar maps into {path: {from, to}}."""
    scalars: dict[str, dict[str, Any]] = {}
    for k in prev.keys() | curr.keys():
        pv = prev.get(k)
        cv = curr.get(k)
        if pv != cv:
            scalars[k] = {"from": pv, "to": cv}
    return scalars


def _diff_collection(
    prev: list[dict] | None, curr: list[dict] | None, key_fn
) -> dict[str, list[dict]]:
    """Add / remove / change diff for a keyed list-of-dicts.

    `added`   — entries whose key is only in curr.
    `removed` — entries whose key is only in prev.
    `changed` — entries present in BOTH whose contents differ; each carries the
                current entry plus a `_delta` of {field: {from, to}} so an
                in-place field flip (e.g. isRespondedTo True→False on a standing
                RFE) is captured. This is what makes the feed reconstruction-
                complete for collections, not just membership.
    """
    prev_map = {key_fn(x): x for x in (prev or []) if isinstance(x, dict)}
    curr_map = {key_fn(x): x for x in (curr or []) if isinstance(x, dict)}
    added = [curr_map[k] for k in curr_map if k not in prev_map]
    removed = [prev_map[k] for k in prev_map if k not in curr_map]
    changed = []
    for k in curr_map.keys() & prev_map.keys():
        pe, ce = prev_map[k], curr_map[k]
        if pe != ce:
            delta = {
                f: {"from": pe.get(f), "to": ce.get(f)}
                for f in pe.keys() | ce.keys()
                if pe.get(f) != ce.get(f)
            }
            entry = dict(ce)
            entry["_delta"] = delta
            changed.append(entry)
    return {"added": added, "removed": removed, "changed": changed}


def compute_change(prev: dict, curr: dict) -> dict:
    """Describe what's different from `prev` to `curr` (consecutive captures).

    The diff is COMPREHENSIVE: it compares the entire `data` object, not a
    hand-picked allowlist. Every scalar leaf (including nested-dict leaves) is
    flattened and diffed, and every list-of-dicts field is diffed with
    add/remove/change semantics. Together with an initial snapshot, the full
    diff feed can reconstruct every later snapshot exactly — which the old
    allowlist approach could not (it was blind to, e.g., `evidenceRequests`).

    Returns a dict with the shape the UI expects:
      {
        "from": capturedAt, "to": capturedAt,
        "scalars": { path: {"from": ..., "to": ...}, ... },   # all scalar leaves
        "events":    {"added": [...], "removed": [...], "changed": [...]},
        "notices":   {"added": [...], "removed": [...], "changed": [...]},
        "documents": {"added": [...], "removed": [...], "changed": [...]},
        "addendums": {"added": [...], "removed": [...], "changed": [...]},
        "evidenceRequests": {"added": [...], "removed": [...], "changed": [...]},
        "collections": { <any other list-of-dicts field>: {...} },
      }
    """
    prev_d = prev.get("data") or {}
    curr_d = curr.get("data") or {}

    scalars = _diff_scalar_map(_flatten_scalars(prev_d), _flatten_scalars(curr_d))

    result: dict[str, Any] = {
        "from": prev.get("capturedAt"),
        "to": curr.get("capturedAt"),
        "scalars": scalars,
    }

    # Named collections keep their dedicated top-level key + purpose-built key fn.
    for name, key_fn in _COLLECTION_KEYS.items():
        result[name] = _diff_collection(prev_d.get(name), curr_d.get(name), key_fn)

    # Any OTHER list-of-dicts field on the record is diffed generically so the
    # feed misses nothing. These land under `collections` to avoid colliding
    # with the named keys the UI renders explicitly.
    extra_keys = {
        k
        for k in (prev_d.keys() | curr_d.keys())
        if k not in _COLLECTION_KEYS
        and (_is_list_of_dicts(prev_d.get(k)) or _is_list_of_dicts(curr_d.get(k)))
    }
    extra: dict[str, dict] = {}
    for k in extra_keys:
        d = _diff_collection(prev_d.get(k), curr_d.get(k), _key_generic)
        if d["added"] or d["removed"] or d["changed"]:
            extra[k] = d
    if extra:
        result["collections"] = extra

    return result


def _sorted_by_capture(entries: list[dict]) -> list[dict]:
    """Captures in chronological order by `capturedAt` (stable, ascending)."""
    return sorted(entries or [], key=lambda e: e.get("capturedAt", ""))


def snapshot_changes(entries: list[dict]) -> list[dict]:
    """Change feed: one entry per consecutive-capture pair that differs.

    Captures are diffed in `capturedAt` order. Only byte-identical pairs produce
    no record — every real change is surfaced, including a timestamp-only bump
    that trails a new event (the case-level update timestamp catching up to the
    event's own time in a later pull). That catch-up is a distinct observable
    change, so dropping it would make the feed unable to reconstruct the latest
    timestamps. Each item carries a `kind` classification (see classify_change).
    """
    ordered = _sorted_by_capture(entries)
    feed: list[dict] = []
    for prev, curr in zip(ordered, ordered[1:]):
        change = compute_change(prev, curr)
        if not _has_any_diff(change):
            continue
        change["kind"] = classify_change(change)
        change["source"] = "case"
        feed.append(change)
    return feed


def _any_collection_diff(change: dict) -> bool:
    """True if any keyed collection (named or generic) has add/remove/change."""
    for name in _COLLECTION_KEYS:
        coll = change.get(name) or {}
        if coll.get("added") or coll.get("removed") or coll.get("changed"):
            return True
    for coll in (change.get("collections") or {}).values():
        if coll.get("added") or coll.get("removed") or coll.get("changed"):
            return True
    return False


def _is_timestamp_only_change(change: dict) -> bool:
    """True when the only thing that moved is `updatedAt` / `updatedAtTimestamp`.

    No collection (event/notice/document/evidenceRequest/…) was added, removed,
    or changed, and the sole scalar diffs are the case-level update timestamps.
    classify_change uses this to label the row `silent_update`.
    """
    scalars = change.get("scalars") or {}
    return (
        bool(scalars)
        and all(k in _TIMESTAMP_ONLY for k in scalars)
        and not _any_collection_diff(change)
    )


def classify_change(change: dict) -> str:
    """Bucket a diff into the single most specific signal.

    The governing rule (per the case owner): a NEW case event is the defining
    signal, and we do NOT assert what any event *means* — every new event is
    simply a "new event". A scalar flag like `actionRequired` is just "action
    needed", not a decision, so there is deliberately NO "decision" bucket:
    a flag flip with no new event is a silent update like any other scalar bump.

    Priority order (most specific first):
      event       — a NEW case event appeared (any code — no meaning asserted).
      appointment — a notice with appointmentDateTime appeared or changed.
      notice      — a new non-appointment notice appeared.
      silent_update  — anything else (no new event): scalar/timestamp bumps,
                       actionRequired/closed/… flips, in-place evidenceRequests
                       churn, generic collection churn.
    """
    events = change.get("events") or {}
    notices = change.get("notices") or {}

    # A new event is the defining signal.
    if events.get("added"):
        return "event"

    notices_added = notices.get("added") or []
    notices_removed = notices.get("removed") or []
    if any((n or {}).get("appointmentDateTime") for n in notices_added + notices_removed):
        return "appointment"
    if notices_added:
        return "notice"

    # No new event/notice/appointment → silent update (incl. flag flips).
    return "silent_update"


def _has_any_diff(change: dict) -> bool:
    if change.get("scalars"):
        return True
    return _any_collection_diff(change)


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
    # is something USCIS did on their side.
    distinct_updated_at = []
    seen = set()
    for d in days:
        u = (d.get("data") or {}).get("updatedAt")
        if u and u not in seen:
            seen.add(u)
            distinct_updated_at.append(u)

    changes = snapshot_changes(entries)
    silent_update_count = sum(1 for c in changes if c.get("kind") == "silent_update")

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
    evidence_requests = latest_data.get("evidenceRequests") or []
    evidence_count = len(evidence_requests)
    # An evidence request still demands a response only while it is neither
    # responded-to nor finalized. Once USCIS logs the response the record stays
    # in the array but no longer requires action.
    outstanding_evidence_count = sum(
        1
        for er in evidence_requests
        if not er.get("isRespondedTo") and not er.get("finalized")
    )
    document_count = len(latest_data.get("documents") or [])

    # "All events" = the timeline pill count: every event row on the latest
    # snapshot plus every silent event detected across the capture history.
    all_events = len(events) + silent_update_count

    # Approval detection. Once an approval event (H008 / APR0) lands, the case
    # has passed the pending window (received -> approval). We surface the
    # approval date and the received->approval span so the UI can switch the
    # "Days pending" metric to a frozen "Approved in days" figure. The case is
    # NOT finished at approval (card production + mailing still follow), so the
    # monitor keeps running — this only reframes the one bounded metric.
    _APPROVAL_CODES = ("H008", "APR0")
    approval_timestamps = sorted(
        e.get("eventTimestamp")
        for e in events
        if e and e.get("eventCode") in _APPROVAL_CODES and e.get("eventTimestamp")
    )
    approved_on = approval_timestamps[0] if approval_timestamps else None
    # Received -> approval, in whole days. Frozen at the approval date, so it
    # stops climbing even as the monitor keeps polling for the card.
    approved_in_days = (
        _days_between(latest_data.get("submissionDate"), approved_on)
        if approved_on else None
    )

    return {
        "daysPending": _days_between(latest_data.get("submissionDate"), today_iso),
        "approvedOn": approved_on,
        "approvedInDays": approved_in_days,
        "daysSinceUpdate": _days_between(latest_data.get("updatedAt"), today_iso),
        "uscisUpdates": len(distinct_updated_at),
        "allEvents": all_events,
        "silentUpdates": silent_update_count,
        "fta0Count": fta0_count,
        "eventCodes": event_codes,
        "stage": infer_stage(events),
        "progression": stage_progression(events),
        "evidenceRequestCount": evidence_count,
        "outstandingEvidenceCount": outstanding_evidence_count,
        "documentCount": document_count,
        "upcomingAppointment": upcoming,
        "lastActivity": last_activity,
    }
