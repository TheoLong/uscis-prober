// Unit tests for the system-log rendering helpers in src/static/app.js.
//
// app.js is a plain browser script (not a module), so we load it inside a
// node:vm sandbox with a stubbed `document` (the only browser global touched
// at load time is `document.addEventListener` for DOMContentLoaded). We then
// expose the pure helpers by appending an assignment that runs in the script's
// own top-level scope — no changes to app.js required.

import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, makeStubEl } from "./_appsandbox.mjs";

const EXPOSE = [
  "_syslogEventId", "escapeHtml", "SYSTEMLOG_EVENT_INFO",
  "_renderFlatSystemLogRow", "renderSystemLogRow", "wireRecomputeButton", "state",
];

const { T } = loadApp(EXPOSE);
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

test("summarize() sums case diffs into a single 'updates' total", () => {
  const e = { cases: [
    { label: "I-485", case_changes: 8 },
    { label: "I-765", case_changes: 1 },
    { label: "I-131", case_changes: 1 },
  ] };
  assert.equal(diffRecomputed().summarize(e), "3 cases · 10 updates");
});

test("summarize() singularizes correctly", () => {
  assert.equal(
    diffRecomputed().summarize({ cases: [{ label: "X", case_changes: 1 }] }),
    "1 case · 1 update",
  );
  assert.equal(
    diffRecomputed().summarize({ cases: [{ label: "X", case_changes: 5 }] }),
    "1 case · 5 updates",
  );
});

test("summarize() handles the empty and error cases", () => {
  assert.equal(diffRecomputed().summarize({ cases: [] }), "no cases configured");
  assert.equal(diffRecomputed().summarize({ cases: [], error: "boom" }), "recompute failed");
});

// ---------------- diff_recomputed.renderContent (the per-case table) ----------------

test("renderContent() renders one 'updates' total per case, no change columns", () => {
  const html = diffRecomputed().renderContent({ cases: [
    { label: "I-485", case_changes: 5 }, // total 5
    { label: "I-131", case_changes: 0 }, // total 0
  ] });
  assert.match(html, /I-485/);
  // 5 updates
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
  const html = diffRecomputed().renderContent({ cases: [{ label: "I-765", case_changes: 1 }] });
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

// ---------------- _renderFlatSystemLogRow (end-to-end row integration) ----------------

test("flat row wires hideKeys + renderContent for a diff_recomputed entry", () => {
  const block = T._renderFlatSystemLogRow({
    ts: "2026-06-16T23:59:52Z",
    event: "diff_recomputed",
    level: "info",
    source: "server",
    cases: [{ label: "I-485", case_changes: 8 }],
  });
  const html = block.innerHTML;
  // renderContent's per-case table is injected into the content band...
  assert.match(html, /diffrc-table/);
  assert.ok(html.includes('<span class="diffrc-num">8</span><span class="diffrc-unit">updates</span>'));
  // ...the one-line summary is present...
  assert.match(html, /1 case · 8 updates/);
  // ...and the raw cases[] array is NOT dumped as a kv detail row.
  assert.ok(!html.includes('syslog-detail-k">cases'), "cases[] must be hidden from the kv dump");
  assert.ok(!html.includes("[{"), "no raw JSON array should appear");
});

test("flat row blanks the duplicate event id in the header", () => {
  const block = T._renderFlatSystemLogRow({
    ts: "2026-06-16T23:59:52Z",
    event: "system_log_cleared",
    level: "info",
    source: "server",
    prior_entry_count: 5,
  });
  const html = block.innerHTML;
  assert.match(html, /kind-tag[^>]*>System log cleared</);          // pill label shown once
  assert.ok(html.includes('<span class="syslog-event"></span>'),   // raw id blanked
            "duplicate event id should render as an empty span");
  assert.ok(!html.includes("system_log_cleared"), "raw event id must not appear in the header");
});

test("flat row keeps a non-duplicate event id (server_startup)", () => {
  const block = T._renderFlatSystemLogRow({
    ts: "2026-06-16T23:59:52Z",
    event: "server_startup",
    level: "info",
    source: "server",
  });
  assert.ok(block.innerHTML.includes('<span class="syslog-event">server_startup</span>'),
            "a curated-label event keeps its id");
});

// ---------------- nested (pull envelope) header dedup ----------------

test("nested pull row blanks its duplicate event id too", () => {
  // entry.steps[] makes renderSystemLogRow dispatch to the nested renderer.
  const block = T.renderSystemLogRow({
    ts: "2026-06-16T23:59:52Z",
    event: "pull",
    level: "info",
    source: "scheduler",
    exit_code: 0,
    steps: [{ ts: "2026-06-16T23:59:50Z", event: "case_snapshot_appended", level: "info" }],
  });
  const html = block.innerHTML;
  assert.match(html, /kind-tag[^>]*>Pull</);                       // "Pull" pill present
  assert.ok(html.includes('<span class="syslog-event syslog-event-envelope"></span>'),
            "nested header's duplicate 'pull' id should be blank");
});

// ---------------- wireRecomputeButton (click flow) ----------------

function setupButtonHarness() {
  const { T: t, sandbox } = loadApp(EXPOSE);
  const btn = makeStubEl();
  btn.disabled = false;
  btn.textContent = "Recompute diff";
  sandbox.document.getElementById = (id) => (id === "recompute-btn" ? btn : null);
  const calls = { fetch: [], toast: [], reload: 0 };
  sandbox.toast = (msg, kind) => calls.toast.push({ msg, kind });
  // A recompute rewrites the diff feed behind every view, so the button reloads
  // them all via the shared refreshAfterRecompute() — the SAME routine the
  // scheduled (post-pull) path uses, so the trigger doesn't change the behavior.
  sandbox.refreshAfterRecompute = async () => { calls.reload += 1; };
  t.wireRecomputeButton();
  return { t, sandbox, btn, calls, click: () => btn._click() };
}

test("recompute click POSTs, refreshes every view, and toasts success", async () => {
  const h = setupButtonHarness();
  h.sandbox.fetch = async (url, opts) => {
    h.calls.fetch.push({ url, opts });
    return { ok: true, json: async () => ({ ok: true }) };
  };
  await h.btn._click();

  // Only the recompute POST goes through fetch directly; refreshAfterRecompute
  // (stubbed) owns the cases/updates/log reloads, so the button fires one fetch.
  assert.equal(h.calls.fetch.length, 1);
  assert.equal(h.calls.fetch[0].url, "/api/system-log/recompute");
  assert.equal(h.calls.fetch[0].opts.method, "POST");
  assert.equal(h.t.state.systemLogPage, 1, "jumps to newest page");
  assert.equal(h.calls.reload, 1, "reloads every view via refreshAfterRecompute");
  assert.equal(h.calls.toast.length, 1);
  assert.equal(h.calls.toast[0].kind, "ok");
  // Button restored after the run.
  assert.equal(h.btn.disabled, false);
  assert.equal(h.btn.textContent, "Recompute diff");
});

test("recompute click surfaces a bad toast on HTTP error and restores the button", async () => {
  const h = setupButtonHarness();
  h.sandbox.fetch = async () => ({ ok: false, status: 500, json: async () => ({}) });
  await h.btn._click();

  assert.equal(h.calls.reload, 0, "no refresh on failure");
  assert.equal(h.calls.toast.length, 1);
  assert.equal(h.calls.toast[0].kind, "bad");
  assert.equal(h.btn.disabled, false, "button re-enabled even on error");
  assert.equal(h.btn.textContent, "Recompute diff");
});

test("recompute click treats {ok:false} body as a failure", async () => {
  const h = setupButtonHarness();
  h.sandbox.fetch = async () => ({ ok: true, json: async () => ({ ok: false }) });
  await h.btn._click();
  assert.equal(h.calls.toast[0].kind, "bad");
  assert.equal(h.calls.reload, 0);
});
