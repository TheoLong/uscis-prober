#!/usr/bin/env python3
"""
Probe event relationships across USCIS case snapshots (research / re-runnable).

Surfaces the candidate relationships an event-link engine could render:

  RE-EMIT  — two event rows share the same (eventCode, eventTimestamp) to the
             millisecond but have different eventId, and one was written
             (createdAtTimestamp) later than the other. The later row is a
             re-emit of the earlier; USCIS re-files the same logical event
             under a new id (often backdated). This is the strongest,
             exact-match link.

  BACKDATED — a single event whose eventTimestamp/eventDateTime is far older
             than its createdAtTimestamp (we observed it written long after
             its claimed occurrence). Every re-emit is backdated, but a
             backdated event need not be a re-emit (e.g. the seed IAF).

Run against the live data files to confirm the link rules still hold and to
re-derive the join key before changing the engine or the UI.

Usage:
  python scripts/probe_event_relationships.py            # all configured cases
  python scripts/probe_event_relationships.py data/485_case.json
"""
from __future__ import annotations
import json
import sys
from collections import defaultdict
from datetime import datetime


BACKDATE_DAYS = 1.0  # claimed vs written gap beyond which we call it backdated


def parse(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")) if s else None


def latest_events(path: str) -> list[dict]:
    snaps = sorted(json.load(open(path)), key=lambda e: e.get("capturedAt", ""))
    return (snaps[-1]["data"].get("events") or []) if snaps else []


def probe(path: str) -> None:
    evs = latest_events(path)
    print(f"\n=== {path}: {len(evs)} events ===")

    # RE-EMIT: group by (code, eventTimestamp); >1 member with distinct ids.
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for e in evs:
        groups[(e.get("eventCode"), e.get("eventTimestamp"))].append(e)

    reemits = 0
    for (code, ets), members in sorted(groups.items(), key=lambda kv: kv[0][1] or ""):
        if len(members) < 2:
            continue
        reemits += 1
        members.sort(key=lambda x: x.get("createdAtTimestamp") or "")
        origin = members[0]
        print(f"  RE-EMIT  {code} @ eventTimestamp={ets}")
        for i, m in enumerate(members):
            role = "origin" if i == 0 else "re-emit"
            written = (m.get("createdAtTimestamp") or "")[:19]
            o_w = parse(origin.get("createdAtTimestamp"))
            m_w = parse(m.get("createdAtTimestamp"))
            back = ""
            if i > 0 and o_w and m_w:
                back = f"  (+{(m_w - o_w).days}d after origin)"
            print(f"      {role:7} id={m['eventId'][:8]} written {written}{back}")

    # BACKDATED: claimed occurrence far older than the day we saw it written.
    print("  backdated singletons (claimed >> written):")
    any_bd = False
    for e in evs:
        ets, cts = parse(e.get("eventTimestamp")), parse(e.get("createdAtTimestamp"))
        if ets and cts and (cts - ets).total_seconds() / 86400 > BACKDATE_DAYS:
            gap = (cts - ets).days
            print(f"      {e.get('eventCode'):5} id={e['eventId'][:8]} "
                  f"claims {e.get('eventDateTime')} written {(e.get('createdAtTimestamp') or '')[:10]} "
                  f"({gap}d back)")
            any_bd = True
    if not any_bd:
        print("      (none)")
    if not reemits:
        print("  (no re-emit groups)")


def main() -> int:
    paths = sys.argv[1:] or [
        "data/485_case.json", "data/765_case.json", "data/131_case.json",
    ]
    for p in paths:
        try:
            probe(p)
        except FileNotFoundError:
            print(f"\n=== {p}: NOT FOUND ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
