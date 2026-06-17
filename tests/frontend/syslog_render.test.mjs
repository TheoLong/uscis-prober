// Unit tests for the system-log rendering helpers in src/static/app.js.
//
// app.js is a plain browser script (not a module), so we load it inside a
// node:vm sandbox with a stubbed `document` (the only browser global touched
// at load time is `document.addEventListener` for DOMContentLoaded). We then
// expose the pure helpers by appending an assignment that runs in the script's
// own top-level scope — no changes to app.js required.

import { test } from "node:test";
import assert from "node:assert/strict";
import vm from "node:vm";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const APP_JS = path.resolve(__dirname, "../../src/static/app.js");

function loadAppHelpers() {
  const src = fs.readFileSync(APP_JS, "utf8");
  const sandbox = { document: { addEventListener() {} }, window: {}, console };
  vm.createContext(sandbox);
  const exposed =
    src + "\n;globalThis.__T = { _syslogEventId, escapeHtml, SYSTEMLOG_EVENT_INFO };";
  vm.runInContext(exposed, sandbox, { filename: "app.js" });
  return sandbox.__T;
}

const T = loadAppHelpers();
const diffRecomputed = () => T.SYSTEMLOG_EVENT_INFO.diff_recomputed;

// ---------------- _syslogEventId (duplicate-id suppression) ----------------

test("_syslogEventId() blanks an event id that just duplicates the pill label", () => {
  assert.equal(T._syslogEventId({ label: "System log cleared" }, { event: "system_log_cleared" }), "");
  assert.equal(T._syslogEventId({ label: "Diff recomputed" }, { event: "diff_recomputed" }), "");
  assert.equal(T._syslogEventId({ label: "Scheduler configured" }, { event: "scheduler_configured" }), "");
});

test("_syslogEventId() keeps a curated label that differs from the event id", () => {
  assert.equal(T._syslogEventId({ label: "Server started" }, { event: "server_startup" }), "server_startup");
  assert.equal(T._syslogEventId({ label: "MFA — code received" }, { event: "mfa_fetch_succeeded" }), "mfa_fetch_succeeded");
});

test("_syslogEventId() returns '' for an empty/missing event", () => {
  assert.equal(T._syslogEventId({ label: "Whatever" }, {}), "");
  assert.equal(T._syslogEventId({ label: "" }, { event: "" }), "");
});

// ---------------- diff_recomputed.summarize (the one-line roll-up) ----------------

test("summarize() sums case + location diffs into a single 'updates' total", () => {
  const e = { cases: [
    { label: "I-485", case_changes: 8, location_changes: 0 },
    { label: "I-765", case_changes: 1, location_changes: 0 },
    { label: "I-131", case_changes: 1, location_changes: 0 },
  ] };
  assert.equal(diffRecomputed().summarize(e), "3 cases · 10 updates");
});

test("summarize() counts location diffs in the total and singularizes correctly", () => {
  assert.equal(
    diffRecomputed().summarize({ cases: [{ label: "X", case_changes: 0, location_changes: 1 }] }),
    "1 case · 1 update",
  );
  assert.equal(
    diffRecomputed().summarize({ cases: [{ label: "X", case_changes: 2, location_changes: 3 }] }),
    "1 case · 5 updates",
  );
});

test("summarize() handles the empty and error cases", () => {
  assert.equal(diffRecomputed().summarize({ cases: [] }), "no cases configured");
  assert.equal(diffRecomputed().summarize({ cases: [], error: "boom" }), "recompute failed");
});

// ---------------- diff_recomputed.renderContent (the per-case table) ----------------

test("renderContent() renders one summed 'updates' total per case, no change columns", () => {
  const html = diffRecomputed().renderContent({ cases: [
    { label: "I-485", case_changes: 3, location_changes: 2 }, // total 5
    { label: "I-131", case_changes: 0, location_changes: 0 }, // total 0
  ] });
  assert.match(html, /I-485/);
  // 3 + 2 = 5 updates
  assert.ok(
    html.includes('<span class="diffrc-num">5</span><span class="diffrc-unit">updates</span>'),
    "expected '5 updates' for I-485",
  );
  // zero-total case is dimmed via is-zero and still reads 'updates' (plural)
  assert.ok(
    html.includes('<span class="diffrc-metric is-zero"><span class="diffrc-num">0</span><span class="diffrc-unit">updates</span>'),
    "expected dimmed '0 updates' for I-131",
  );
  // the old per-column wording must be gone
  assert.ok(!/case change/.test(html), "must not mention 'case change'");
  assert.ok(!/location/.test(html), "must not mention 'location'");
});

test("renderContent() singularizes a total of exactly 1", () => {
  const html = diffRecomputed().renderContent({ cases: [{ label: "I-765", case_changes: 1, location_changes: 0 }] });
  assert.ok(html.includes('<span class="diffrc-unit">update</span>'), "expected singular 'update'");
});

test("renderContent() returns '' when there are no cases", () => {
  assert.equal(diffRecomputed().renderContent({ cases: [] }), "");
  assert.equal(diffRecomputed().renderContent({}), "");
});

test("diff_recomputed suppresses the raw cases[] array from the kv dump", () => {
  // Compare element-wise: the array originates in the vm realm, so a
  // deepStrictEqual prototype check would spuriously fail across realms.
  const hk = diffRecomputed().hideKeys;
  assert.equal(hk.length, 1);
  assert.equal(hk[0], "cases");
});
