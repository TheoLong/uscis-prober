// Unit tests for the Schedule modal form renderer + hours parser
// (src/static/app.js). _renderScheduleForm builds the hours input + per-case
// checkboxes (keyed on case id) from the /api/schedule payload;
// parseScheduleHours enforces the comma-separated, no-space, 0–23 contract.

import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, makeStubEl } from "./_appsandbox.mjs";

const EXPOSE = ["_renderScheduleForm", "parseScheduleHours"];

// An overlay stub whose `.mfa-modal-body` querySelector returns a single
// captured body element, so the test can read the HTML the renderer wrote.
function makeOverlayWithBody() {
  const body = makeStubEl();
  const overlay = makeStubEl();
  overlay.querySelector = (sel) => (sel === ".mfa-modal-body" ? body : makeStubEl());
  return { overlay, body };
}

test("_renderScheduleForm prefills hours (no spaces) and keys checkboxes on id", () => {
  const { T } = loadApp(EXPOSE, { escapeHtml: (s) => String(s) });
  const { overlay, body } = makeOverlayWithBody();
  T._renderScheduleForm(overlay, {
    hours: [18, 6, 0],
    timezone: "America/New_York",
    cases: [
      { label: "I-485", id: "IOE1", enabled: true },
      { label: "I-765", id: "IOE2", enabled: false },
      { label: "I-131", id: "IOE3", enabled: true },
    ],
  });
  const html = body.innerHTML;
  // Hours sorted ascending, comma-separated, NO spaces.
  assert.match(html, /value="0,6,18"/);
  // A checkbox row per case, keyed on data-id (not data-label).
  assert.equal((html.match(/class="schedule-case-cb"/g) || []).length, 3);
  assert.match(html, /data-id="IOE1"[^>]* checked/);
  assert.match(html, /data-id="IOE3"[^>]* checked/);
  assert.match(html, /data-id="IOE2"(?![^>]*checked)/);
  // Label still shown for humans.
  assert.match(html, /I-485/);
  assert.match(html, /data-schedule-save/);
  assert.match(html, /data-schedule-cancel/);
});

test("_renderScheduleForm handles an empty case list", () => {
  const { T } = loadApp(EXPOSE, { escapeHtml: (s) => String(s) });
  const { overlay, body } = makeOverlayWithBody();
  T._renderScheduleForm(overlay, { hours: [12], timezone: "America/New_York", cases: [] });
  assert.match(body.innerHTML, /No cases configured/);
});

// ---------------- parseScheduleHours contract ----------------

test("parseScheduleHours accepts comma-separated 0-23 and sorts/dedupes", () => {
  const { T } = loadApp(EXPOSE);
  // Compare via JSON to sidestep cross-vm Array prototype identity in deepEqual.
  const j = (v) => JSON.stringify(T.parseScheduleHours(v));
  assert.equal(j("0,6,10,14,18"), JSON.stringify({ ok: true, hours: [0, 6, 10, 14, 18] }));
  assert.equal(j("18,6,0"), JSON.stringify({ ok: true, hours: [0, 6, 18] }));
  assert.equal(j("23"), JSON.stringify({ ok: true, hours: [23] }));
});

test("parseScheduleHours rejects spaces", () => {
  const { T } = loadApp(EXPOSE);
  assert.equal(T.parseScheduleHours("0, 6, 10").ok, false);
  assert.equal(T.parseScheduleHours(" 0,6").ok, false);
});

test("parseScheduleHours rejects out-of-range, non-numeric, empty, and dupes", () => {
  const { T } = loadApp(EXPOSE);
  assert.equal(T.parseScheduleHours("24").ok, false);
  assert.equal(T.parseScheduleHours("-1").ok, false);      // '-' is non-digit
  assert.equal(T.parseScheduleHours("6,foo").ok, false);
  assert.equal(T.parseScheduleHours("").ok, false);
  assert.equal(T.parseScheduleHours("6,,10").ok, false);   // empty slot
  assert.equal(T.parseScheduleHours("6,6").ok, false);     // duplicate
});
