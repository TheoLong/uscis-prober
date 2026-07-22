#!/usr/bin/env python3
"""Install a fake event-link scenario on the live site for visual inspection.

POSTs a synthetic link set to /api/cases/<label>/fake-links so the timeline
renders link shapes the real data doesn't have yet (crossing spans, fan-out
from one origin, dense stacks). The override is in-memory on the server and is
cleared by ANY diff recompute (startup, pull, or the recompute button).

Usage:
  python scripts/install_fake_links.py complex   # dense 9-link stress test
  python scripts/install_fake_links.py cross
  python scripts/install_fake_links.py clear      # remove the override
  python scripts/install_fake_links.py --label I-485 fanout

The eventIds below are the real I-485 latest-snapshot rows, so the links point
at actual timeline rows. Re-derive them with scripts/probe_event_relationships
if the snapshot changes.
"""
from __future__ import annotations
import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8731"

# Real I-485 eventIds, oldest -> newest.
E = {
    "iaf":   "47d237e1-4229-459e-a5c4-e52c0ee90796",  # IAF  03/05
    "fta0a": "8301af65-0bcd-4a40-a38a-197f87e5cdc6",  # FTA0 03/10
    "fta0b": "28aa2e58-228e-42ff-9c4e-41ab4b8d8bd0",  # FTA0 03/10
    "fta0c": "e4cd4609-061c-4678-93bf-2e930f7e5078",  # FTA0 06/05
    "fta1a": "080449f4-1ca4-4704-95fe-b56a7eb70858",  # FTA1 06/05
    "fta1b": "f9e2854d-3c22-4976-9688-aff26c351851",  # FTA1 06/24
    "fta1c": "5c2547a8-fbcd-4348-9bea-3631780bd3ef",  # FTA1 06/25
}


def mk(origin, reemit, code, days):
    return {"kind": "reemit", "eventCode": code, "eventTimestamp": "synthetic",
            "originId": E[origin], "reemitId": E[reemit], "daysApart": days}


SCENARIOS = {
    # Two links that cross.
    "cross": [mk("fta0a", "fta1c", "X", 100), mk("iaf", "fta1a", "Y", 60)],
    # One origin, three re-emits (fan-out).
    "fanout": [mk("fta0a", "fta0c", "FTA0", 86), mk("fta0a", "fta1a", "FTA0", 87),
               mk("fta0a", "fta1c", "FTA0", 107)],
    # Non-overlapping stack — all share lane 0 (same level).
    "stack": [mk("fta1b", "fta1c", "A", 1), mk("fta0a", "fta0b", "B", 0),
              mk("iaf", "fta0a", "C", 5)],
    # Dense stress test: many shapes at once.
    "complex": [
        mk("iaf", "fta1c", "L1", 110), mk("fta0a", "fta1b", "L2", 95),
        mk("fta0b", "fta1a", "L3", 60), mk("iaf", "fta0c", "L4", 80),
        mk("iaf", "fta0b", "L5", 5), mk("fta0c", "fta1c", "L6", 20),
        mk("fta0a", "fta0c", "L7", 86), mk("fta1a", "fta1b", "L8", 19),
        mk("fta0b", "fta1c", "L9", 100),
    ],
    # Color stress test: 14 links to show the 7-hue rainbow then its dissected
    # second round (seq 0-6 = signature spaced colors, 7-13 = gap-filling).
    # Pairs are arbitrary (every distinct origin/reemit combo) — this exercises
    # the color sequence, not a realistic relationship shape.
    "rainbow": [
        mk("iaf", "fta0a", "C0", 5), mk("iaf", "fta0b", "C1", 5),
        mk("iaf", "fta0c", "C2", 80), mk("iaf", "fta1a", "C3", 90),
        mk("iaf", "fta1b", "C4", 100), mk("iaf", "fta1c", "C5", 110),
        mk("fta0a", "fta0b", "C6", 0), mk("fta0a", "fta0c", "C7", 86),
        mk("fta0a", "fta1a", "C8", 87), mk("fta0a", "fta1b", "C9", 95),
        mk("fta0a", "fta1c", "C10", 100), mk("fta0b", "fta0c", "C11", 86),
        mk("fta0b", "fta1a", "C12", 87), mk("fta0b", "fta1c", "C13", 100),
    ],
    "clear": [],
}


def post(label: str, links: list) -> dict:
    req = urllib.request.Request(
        f"{BASE}/api/cases/{label}/fake-links",
        data=json.dumps(links).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def main() -> int:
    args = [a for a in sys.argv[1:]]
    label = "I-485"
    if "--label" in args:
        i = args.index("--label")
        label = args[i + 1]
        del args[i:i + 2]
    scenario = args[0] if args else "complex"
    if scenario not in SCENARIOS:
        print(f"unknown scenario '{scenario}'; try: {', '.join(SCENARIOS)}")
        return 1
    result = post(label, SCENARIOS[scenario])
    print(f"{scenario} -> {result}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
