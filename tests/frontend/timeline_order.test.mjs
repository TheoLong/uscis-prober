// Unit tests for buildTimelineRows (src/static/app.js): the combined
// event + silent-update timeline must order by each row's REAL timestamp
// (events by createdAtTimestamp, silent updates by the updatedAtTimestamp
// USCIS moved to) — NOT by the day we detected the change.

import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp } from "./_appsandbox.mjs";

const { T } = loadApp(["buildTimelineRows"]);

// The real FTA1 scenario: a fresh FTA1, an older FTA0, and two silent updates.
// The 14:26 silent was DETECTED at 15:42 (a later pull) but its real update
// time is 14:26:50.903; the 13:41 silent was detected earlier.
const EVENTS = [
  { eventCode: "FTA1", eventId: "fta1-new",
    eventTimestamp: "2026-06-25T14:26:50.949Z",
    createdAtTimestamp: "2026-06-25T14:26:52.215Z" },
  { eventCode: "FTA0", eventId: "fta0-old",
    eventTimestamp: "2026-03-10T16:59:51.837Z",
    createdAtTimestamp: "2026-03-10T17:08:49.146Z" },
];
const CHANGES = [
  // detected at 15:42 — but real update time is 14:26:50.903 (FTA1 footprint)
  { kind: "silent_update", to: "2026-06-25T15:42:40Z",
    scalars: { updatedAtTimestamp: { from: "2026-06-25T13:41:55.953Z",
                                     to: "2026-06-25T14:26:50.903Z" } } },
  // detected at 14:00 — real update time 13:41:55.953
  { kind: "silent_update", to: "2026-06-25T14:00:00Z",
    scalars: { updatedAt: { from: "2026-06-24", to: "2026-06-25" },
               updatedAtTimestamp: { from: "2026-06-24T17:18:27.472Z",
                                     to: "2026-06-25T13:41:55.953Z" } } },
];

test("buildTimelineRows orders by real timestamp, newest first", () => {
  const rows = T.buildTimelineRows(EVENTS, CHANGES);
  // Array.from pulls the cross-realm (vm sandbox) result into this realm.
  const got = Array.from(rows, r => (r.silent ? `silent@${r.ts}` : r.code));
  assert.deepEqual(got, [
    "FTA1",                                  // createdAtTimestamp 14:26:52
    "silent@2026-06-25T14:26:50.903Z",       // footprint, just below FTA1
    "silent@2026-06-25T13:41:55.953Z",       // the OLDER silent, correctly below
    "FTA0",                                  // months earlier
  ]);
});

test("silent updates anchor on updatedAtTimestamp, NOT detection time", () => {
  const rows = T.buildTimelineRows(EVENTS, CHANGES);
  // The 14:26 silent was detected at 15:42 (newer than FTA1's 14:26:52). If it
  // were anchored on detection it would sort ABOVE FTA1 — it must not.
  const fta1 = rows.findIndex(r => r.code === "FTA1");
  const footprint = rows.findIndex(r => r.ts === "2026-06-25T14:26:50.903Z");
  assert.ok(fta1 < footprint, "footprint sits below FTA1 by real time");
  // The two silent updates must be in real-time order (older one last).
  const older = rows.findIndex(r => r.ts === "2026-06-25T13:41:55.953Z");
  assert.ok(footprint < older, "older silent ranks below the newer silent");
});

test("buildTimelineRows dedups events by eventId and tolerates empties", () => {
  const dupes = [EVENTS[0], { ...EVENTS[0] }];  // same eventId twice
  assert.equal(T.buildTimelineRows(dupes, []).length, 1);
  assert.equal(T.buildTimelineRows([], []).length, 0);
  assert.equal(T.buildTimelineRows(undefined, undefined).length, 0);
});

// Detection time ("Detected at") must derive from the raw capture history so
// it behaves identically for every case — including cases whose events were
// all present at the very first snapshot (which never appear in events.added
// diffs). This is the bug where detection only showed on the I-485 case.
test("detectedAt comes from the first snapshot containing the event, for ALL cases", () => {
  // Case where BOTH events exist at the baseline snapshot (no post-baseline
  // additions at all — the 765/131 situation). Detection must still resolve.
  // Snapshot events carry the same eventTimestamp/createdAtTimestamp as the
  // EVENTS fixture so their composite row keys match (real snapshots always
  // carry these; detection is keyed on the composite row key, not eventId).
  const entries = [
    { capturedAt: "2026-03-05T17:00:00Z",
      data: { events: [{ eventCode: "FTA0", eventId: "fta0-old",
                         eventTimestamp: "2026-03-10T16:59:51.837Z",
                         createdAtTimestamp: "2026-03-10T17:08:49.146Z" }] } },
    { capturedAt: "2026-06-25T15:00:00Z",
      data: { events: [{ eventCode: "FTA0", eventId: "fta0-old",
                         eventTimestamp: "2026-03-10T16:59:51.837Z",
                         createdAtTimestamp: "2026-03-10T17:08:49.146Z" },
                       { eventCode: "FTA1", eventId: "fta1-new",
                         eventTimestamp: "2026-06-25T14:26:50.949Z",
                         createdAtTimestamp: "2026-06-25T14:26:52.215Z" }] } },
  ];
  const rows = T.buildTimelineRows(EVENTS, [], entries);
  const byCode = Object.fromEntries(Array.from(rows, r => [r.code, r.detectedAt]));
  // FTA0 was present at the first capture → detected then.
  assert.equal(byCode["FTA0"], "2026-03-05T17:00:00Z");
  // FTA1 first appeared at the second capture → detected then.
  assert.equal(byCode["FTA1"], "2026-06-25T15:00:00Z");
});

test("detectedAt is null when there's no capture history to source it from", () => {
  const rows = T.buildTimelineRows(EVENTS, [], []);
  assert.ok(rows.every(r => r.detectedAt === null),
    "no entries → no detection time (never fabricated)");
});
