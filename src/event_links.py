# Copyright (C) 2026 the USCIS Prober contributors
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Detect relationships between USCIS case events.

USCIS sometimes re-files the same logical event under a NEW `eventId`
carrying the IDENTICAL `eventTimestamp`. The newer row is a *re-emit* of
the original. This module surfaces those links so the UI can draw a
connector between an event and the row it re-emits.

The link rule (exact, validated against the snapshot history):

  key      = (eventCode, eventTimestamp)     # exact match, ms precision
  group    = all events sharing that key
  link     = any group with >= 2 distinct eventIds
  origin   = the group member with the earliest createdAtTimestamp
  re-emits = the rest; each points back to the origin

`eventTimestamp` (not `eventDateTime`) is the key because the date alone is
too coarse — a case can carry several distinct events on the same calendar
day, distinguished only by the millisecond timestamp. The match is exact:
distinct events sit either at the identical timestamp (a real re-emit) or
milliseconds-to-days apart (genuinely separate events), so any tolerance
window would wrongly merge separate events. Direction comes from
`createdAtTimestamp` (USCIS's write time), not observation order — a re-emit
can first appear in the same poll as its origin when both predate our seed.
"""

from __future__ import annotations

from collections import OrderedDict


def _key(event: dict) -> tuple | None:
    """Join key for an event, or None when it can't participate in a link.

    Both parts must be present: an event with no `eventCode` or no
    `eventTimestamp` can't be matched and is excluded.
    """
    code = event.get("eventCode")
    ts = event.get("eventTimestamp")
    if not code or not ts:
        return None
    return (code, ts)


# A composite NATURAL key that uniquely identifies one event ROW without any
# identifier value. (eventCode, eventTimestamp, createdAtTimestamp): code+ts
# groups re-emits together, and createdAtTimestamp separates the origin from
# each re-emit (they share code+ts but differ in write time). Rendered as a
# single delimited string so it can live in a DOM data-attribute. Because it
# contains only fields already shown on the timeline, it is safe to keep in
# redacted / demo output — letting every *Id be fully masked.
def _row_key(event: dict) -> str:
    return "|".join((
        str(event.get("eventCode") or ""),
        str(event.get("eventTimestamp") or ""),
        str(event.get("createdAtTimestamp") or ""),
    ))


def event_links(events: list[dict] | None) -> list[dict]:
    """Return the re-emit links among `events` (one snapshot's event array).

    Each link is::

        {
          "kind": "reemit",
          "eventCode": "FTA0",
          "eventTimestamp": "2026-03-10T16:59:51.837Z",
          "originId": "<eventId of the earliest-written row>",
          "reemitId": "<eventId of a later-written row>",
          "daysApart": <int days between origin and re-emit createdAt, or None>,
        }

    One link record per re-emit (a group of N members yields N-1 links, all
    pointing at the single origin). Groups with a single member produce no
    link. Links are ordered by APPEARANCE TIME — the re-emit's
    `createdAtTimestamp`, i.e. when USCIS wrote the row that made the link
    detectable. This is the stable, monotonic order: the first link ever
    observed stays first, and a newly-detected link always appends to the end,
    so a consumer keying a color (or any per-link assignment) off the index
    keeps that assignment consistent as new links appear. `eventTimestamp`
    is NOT used for ordering — USCIS backdates it, which would let a new link
    insert in the middle and reshuffle every later index.
    """
    groups: "OrderedDict[tuple, list[dict]]" = OrderedDict()
    for e in events or []:
        k = _key(e)
        if k is None:
            continue
        # Only distinct eventIds count toward a link; the same row appearing
        # twice in one payload would be a server bug, not a re-emit.
        ids = {m.get("eventId") for m in groups.get(k, [])}
        if e.get("eventId") in ids:
            continue
        groups.setdefault(k, []).append(e)

    links: list[tuple[str, dict]] = []
    for (code, ts), members in groups.items():
        if len(members) < 2:
            continue
        members = sorted(members, key=lambda m: m.get("createdAtTimestamp") or "")
        origin = members[0]
        for reemit in members[1:]:
            # The re-emit's write time is when this link became detectable —
            # the appearance-time sort key (kept out of the link record so the
            # API response carries only the link's own fields).
            appeared_at = reemit.get("createdAtTimestamp") or ""
            links.append((appeared_at, {
                "kind": "reemit",
                "eventCode": code,
                "eventTimestamp": ts,
                "originId": origin.get("eventId"),
                "reemitId": reemit.get("eventId"),
                # Composite NATURAL keys for the two endpoints — (code,
                # eventTimestamp, createdAtTimestamp). These carry no PII, so
                # the frontend can wire the overlay off them and every *Id can
                # be fully masked in redaction / demo mode. createdAtTimestamp
                # disambiguates the origin from its re-emit (same code+ts).
                "originKey": _row_key(origin),
                "reemitKey": _row_key(reemit),
                "daysApart": _days_apart(
                    origin.get("createdAtTimestamp"),
                    reemit.get("createdAtTimestamp"),
                ),
            }))
    # Order by appearance time so the first-observed link is always index 0 and
    # later links append without renumbering the earlier ones.
    links.sort(key=lambda pair: pair[0])
    return [link for _appeared_at, link in links]


def _days_apart(origin_iso: str | None, reemit_iso: str | None) -> int | None:
    """Whole-day gap between two ISO write-timestamps, or None if unparseable."""
    if not origin_iso or not reemit_iso:
        return None
    try:
        from datetime import datetime
        a = datetime.fromisoformat(origin_iso.replace("Z", "+00:00"))
        b = datetime.fromisoformat(reemit_iso.replace("Z", "+00:00"))
        return abs((b - a).days)
    except (ValueError, AttributeError):
        return None
