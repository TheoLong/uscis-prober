// Unit tests for the Schedule modal form renderer (src/static/app.js):
// _renderScheduleForm builds the hours input + per-case checkboxes from the
// /api/schedule payload. Verifies enabled cases render checked, disabled
// unchecked, and the hours prefill.

import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, makeStubEl } from "./_appsandbox.mjs";

const EXPOSE = ["_renderScheduleForm"];

// An overlay stub whose `.mfa-modal-body` querySelector returns a single
// captured body element, so the test can read the HTML the renderer wrote.
function makeOverlayWithBody() {
  const body = makeStubEl();
  // The renderer calls body.querySelector(...) to wire Cancel/Save buttons;
  // the default stub returns a fresh element with a no-op addEventListener,
  // which is fine here — we only assert on innerHTML.
  const overlay = makeStubEl();
  overlay.querySelector = (sel) => (sel === ".mfa-modal-body" ? body : makeStubEl());
  return { overlay, body };
}

test("_renderScheduleForm prefills hours and renders a checkbox per case", () => {
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
  // Hours are sorted ascending in the input value.
  assert.match(html, /value="0, 6, 18"/);
  // A checkbox row per case with its label.
  assert.equal((html.match(/class="schedule-case-cb"/g) || []).length, 3);
  assert.match(html, /data-label="I-485"[^>]* checked/);
  assert.match(html, /data-label="I-131"[^>]* checked/);
  // The disabled case is NOT checked.
  assert.match(html, /data-label="I-765"(?![^>]*checked)/);
  // Save + Cancel controls present.
  assert.match(html, /data-schedule-save/);
  assert.match(html, /data-schedule-cancel/);
});

test("_renderScheduleForm handles an empty case list", () => {
  const { T } = loadApp(EXPOSE, { escapeHtml: (s) => String(s) });
  const { overlay, body } = makeOverlayWithBody();
  T._renderScheduleForm(overlay, { hours: [12], timezone: "America/New_York", cases: [] });
  assert.match(body.innerHTML, /No cases configured/);
});
