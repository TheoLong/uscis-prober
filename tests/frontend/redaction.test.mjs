// Unit tests for redaction mode (src/static/app.js): the deep snapshot
// redactor, the display-mask helper, and the toggle wiring.

import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, makeStubEl } from "./_appsandbox.mjs";

const EXPOSE = [
  "redactSnapshot", "redactDisplay", "redactDetailValue", "redactMaybe", "scrubText",
  "REDACT_KEYS", "REDACTION_MASK", "state", "wireRedactionPill",
  "loadRedactionState", "_detailKvHtml", "renderSystemLogRow", "renderUpdateRecord",
];

function load(extra = {}) {
  return loadApp(EXPOSE, extra);
}

const MASK = "••••••••"; // ••••••••

// ---------------- redactSnapshot (deep PII masking) ----------------

test("redactSnapshot masks the three PII keys at the top level", () => {
  const { T } = load();
  const out = T.redactSnapshot({
    receiptNumber: "IOE0000000000",
    applicantName: "DOE, JANE",
    representativeName: "SMITH, JOHN",
    formName: "I-485, Application to Register Permanent Residence",
    formType: "I485",
    closed: false,
  });
  assert.equal(out.receiptNumber, T.REDACTION_MASK);
  assert.equal(out.applicantName, T.REDACTION_MASK);
  assert.equal(out.representativeName, T.REDACTION_MASK);
  // non-PII fields are untouched
  assert.equal(out.formName, "I-485, Application to Register Permanent Residence");
  assert.equal(out.formType, "I485");
  assert.equal(out.closed, false);
});

test("redactSnapshot masks PII nested in arrays/objects too", () => {
  const { T } = load();
  const out = T.redactSnapshot({
    receiptNumber: "IOE1",
    concurrentCases: [
      { receiptNumber: "IOE2", formType: "I765" },
      { receiptNumber: "IOE3", formType: "I131" },
    ],
    nested: { deep: { applicantName: "X, Y" } },
  });
  assert.equal(out.receiptNumber, MASK);
  assert.equal(out.concurrentCases[0].receiptNumber, MASK);
  assert.equal(out.concurrentCases[1].receiptNumber, MASK);
  assert.equal(out.concurrentCases[0].formType, "I765"); // sibling untouched
  assert.equal(out.nested.deep.applicantName, MASK);
});

test("redactSnapshot does not mutate the input (pure clone)", () => {
  const { T } = load();
  const input = { receiptNumber: "IOE0000000000", events: [{ code: "ABC" }] };
  const out = T.redactSnapshot(input);
  assert.equal(input.receiptNumber, "IOE0000000000", "input must be unchanged");
  assert.equal(out.receiptNumber, MASK);
  assert.notEqual(out, input);
  assert.notEqual(out.events, input.events);
});

test("redactSnapshot leaves null/missing values alone", () => {
  const { T } = load();
  const out = T.redactSnapshot({ receiptNumber: null, applicantName: "A, B" });
  assert.equal(out.receiptNumber, null);   // nothing to mask
  assert.equal(out.applicantName, MASK);
});

test("REDACT_KEYS covers both receipt key spellings + the two names", () => {
  const { T } = load();
  const keys = [...T.REDACT_KEYS].sort();
  assert.deepEqual(keys, ["applicantName", "receipt", "receiptNumber", "representativeName"]);
});

test("redactSnapshot masks any identifier key (eventId, letterId, pid)", () => {
  const { T } = load();
  const out = T.redactSnapshot({
    events: [{ eventId: "abc-123", eventCode: "RFE", eventDateTime: "2026-03-01" }],
    notices: [{ letterId: "L-998877", actionType: "ISSUED" }],
    pid: "P-42",
    formType: "I485",
  });
  assert.equal(out.events[0].eventId, MASK, "eventId masked");
  assert.equal(out.notices[0].letterId, MASK, "letterId masked");
  assert.equal(out.pid, MASK, "pid masked");
  // Non-identifier siblings are untouched.
  assert.equal(out.events[0].eventCode, "RFE");
  assert.equal(out.notices[0].actionType, "ISSUED");
  assert.equal(out.formType, "I485");
});

test("redactDetailValue masks an identifier-keyed row when redaction is on", () => {
  const { T } = load();
  T.state.redacted = true;
  assert.equal(T.redactDetailValue("eventId", "abc-123"), MASK);
  assert.equal(T.redactDetailValue("letterId", "L-1"), MASK);
  assert.equal(T.redactDetailValue("eventCode", "RFE"), "RFE"); // non-id kept
  T.state.redacted = false;
  assert.equal(T.redactDetailValue("eventId", "abc-123"), "abc-123"); // no-op when off
});

test("redactSnapshot masks the system-log style `receipt` key", () => {
  const { T } = load();
  const out = T.redactSnapshot({ receipt: "IOE0000000000", label: "I-485" });
  assert.equal(out.receipt, T.REDACTION_MASK);
  assert.equal(out.label, "I-485"); // form type kept
});

test("redactSnapshot scrubs receipt numbers embedded in free-text strings", () => {
  const { T } = load();
  const out = T.redactSnapshot({
    url: "https://egov.uscis.gov/casestatus/IOE0000000000/detail",
    note: "no identifiers here",
  });
  assert.ok(!out.url.includes("IOE0000000000"), "embedded receipt should be scrubbed");
  assert.ok(out.url.includes(T.REDACTION_MASK));
  assert.equal(out.note, "no identifiers here");
});

// ---------------- scrubText (pure pattern scrub) ----------------

test("scrubText removes receipt numbers and emails, keeps form types/dates", () => {
  const { T } = load();
  assert.equal(T.scrubText("case IOE0000000000 updated"), `case ${T.REDACTION_MASK} updated`);
  assert.equal(T.scrubText("ping me at a.b@example.com please").includes("@"), false);
  assert.equal(T.scrubText("form I-485 on 2026-06-16"), "form I-485 on 2026-06-16");
  assert.equal(T.scrubText(42), 42); // non-strings pass through
});

// ---------------- redactDetailValue (system-log chokepoint logic) ----------------

test("redactDetailValue masks PII keys, scrubs strings, no-ops when off", () => {
  const { T } = load();
  T.state.redacted = false;
  assert.equal(T.redactDetailValue("receipt", "IOE0000000000"), "IOE0000000000");

  T.state.redacted = true;
  assert.equal(T.redactDetailValue("receipt", "IOE0000000000"), T.REDACTION_MASK);
  assert.equal(T.redactDetailValue("level", 12345), 12345);          // non-PII number kept
  assert.equal(T.redactDetailValue("label", "I-485"), "I-485");      // form type kept
  assert.ok(!T.redactDetailValue("url", "x/IOE0000000000").includes("IOE0000000000"));
});

// ---------------- _detailKvHtml integration (System tab) ----------------

test("_detailKvHtml masks a receipt detail row when redaction is on", () => {
  const { T } = load();
  T.state.redacted = true;
  const html = T._detailKvHtml("receipt", "IOE0000000000");
  assert.ok(!html.includes("IOE0000000000"), "raw receipt must not render");
  assert.ok(html.includes(T.REDACTION_MASK));
});

test("_detailKvHtml leaves non-PII rows untouched and is a no-op when off", () => {
  const { T } = load();
  T.state.redacted = true;
  assert.ok(T._detailKvHtml("level", 4242).includes("4242"));

  T.state.redacted = false;
  assert.ok(T._detailKvHtml("receipt", "IOE0000000000").includes("IOE0000000000"));
});

// ---------------- redactDisplay (single-value mask, state-gated) ----------------

test("redactDisplay passes values through when redaction is off", () => {
  const { T } = load();
  T.state.redacted = false;
  assert.equal(T.redactDisplay("IOE0000000000"), "IOE0000000000");
});

test("redactDisplay returns the mask when redaction is on", () => {
  const { T } = load();
  T.state.redacted = true;
  assert.equal(T.redactDisplay("IOE0000000000"), T.REDACTION_MASK);
});

// ---------------- wireRedactionPill / loadRedactionState (server-backed) ----------------

function makePill() {
  return {
    disabled: false,
    _checked: "false",
    addEventListener(ev, fn) { (this._h = this._h || {})[ev] = fn; },
    setAttribute(k, v) { if (k === "aria-checked") this._checked = v; },
    getAttribute(k) { return k === "aria-checked" ? this._checked : null; },
    _click() { return this._h.click(); },
  };
}

function harness(view = "cases") {
  const calls = { fetch: [], toast: [], refresh: 0, pwPrompts: 0 };
  const { T, sandbox } = load();
  T.state.view = view;
  sandbox.refreshAll = async () => { calls.refresh += 1; };
  sandbox.renderUpdates = () => {};
  sandbox.renderSystemLog = () => {};
  sandbox.toast = (msg, kind) => calls.toast.push({ msg, kind });
  // Toggling redaction always challenges for the admin password — stub the
  // prompt so the headless test can drive it. Override per-test for cancel /
  // wrong-password cases.
  sandbox.requestAdminPassword = async () => { calls.pwPrompts += 1; return "pw"; };
  sandbox.fetch = async (url, opts) => {
    const enabled = opts && opts.body ? JSON.parse(opts.body).enabled : false;
    const adminPw = opts && opts.headers ? opts.headers["X-Admin-Password"] : undefined;
    calls.fetch.push({ url, method: opts && opts.method, enabled, adminPw });
    return { ok: true, status: 200, json: async () => ({ ok: true, enabled }) };
  };
  const pill = makePill();
  sandbox.document.getElementById = (id) => (id === "redaction-pill" ? pill : null);
  return { T, sandbox, pill, calls };
}

test("wireRedactionPill ON: POSTs enabled=true to the server, refreshes, warns", async () => {
  const h = harness();
  h.T.state.redacted = false;
  h.T.wireRedactionPill();
  await h.pill._click();

  assert.equal(h.calls.fetch[0].url, "/api/redaction-mode");
  assert.equal(h.calls.fetch[0].method, "POST");
  assert.equal(h.calls.fetch[0].enabled, true);
  assert.equal(h.calls.pwPrompts, 1, "challenges for the admin password");
  assert.equal(h.calls.fetch[0].adminPw, "pw", "sends X-Admin-Password header");
  assert.equal(h.T.state.redacted, true);
  assert.equal(h.pill.getAttribute("aria-checked"), "true");
  assert.equal(h.calls.refresh, 1, "re-fetches now-masked data");
  assert.equal(h.calls.toast.at(-1).kind, "warn");
});

test("wireRedactionPill: cancelling the password prompt aborts (no fetch)", async () => {
  const h = harness();
  h.sandbox.requestAdminPassword = async () => null;  // user hits Cancel
  h.T.state.redacted = false;
  h.T.wireRedactionPill();
  await h.pill._click();

  assert.equal(h.calls.fetch.length, 0, "no request when cancelled");
  assert.equal(h.T.state.redacted, false, "latch unchanged");
});

test("wireRedactionPill OFF: POSTs enabled=false, refreshes, neutral toast", async () => {
  const h = harness();
  h.T.state.redacted = true;
  h.T.wireRedactionPill();
  await h.pill._click();

  assert.equal(h.calls.fetch[0].enabled, false);
  assert.equal(h.T.state.redacted, false);
  assert.equal(h.pill.getAttribute("aria-checked"), "false");
  assert.equal(h.calls.refresh, 1);
  assert.equal(h.calls.toast.at(-1).kind, "");
});

test("loadRedactionState GETs the server flag and reflects it on the pill", async () => {
  const h = harness();
  h.T.state.redacted = false;
  h.sandbox.fetch = async () => ({ ok: true, json: async () => ({ enabled: true }) });
  await h.T.loadRedactionState();
  assert.equal(h.T.state.redacted, true);
  assert.equal(h.pill.getAttribute("aria-checked"), "true");
});

// ---------------- redactMaybe (state-gated free-text scrub) ----------------

test("redactMaybe scrubs only when redaction is on", () => {
  const { T } = load();
  T.state.redacted = false;
  assert.equal(T.redactMaybe("id IOE0000000000"), "id IOE0000000000");
  T.state.redacted = true;
  assert.ok(!T.redactMaybe("id IOE0000000000").includes("IOE0000000000"));
});

// ---------------- pull-row expansion lock (System tab, most-PII steps) ----------------

function pullEntry() {
  return {
    ts: "2026-06-16T23:59:52Z", event: "pull", level: "info", source: "scheduler",
    exit_code: 0,
    steps: [
      { ts: "2026-06-16T23:59:50Z", event: "case_snapshot_appended", level: "info", receipt: "IOE0000000000" },
      { ts: "2026-06-16T23:59:51Z", event: "auth_submit_result", level: "info" },
    ],
  };
}

test("redacted pull row is locked and its steps are never rendered into the DOM", () => {
  const { T, sandbox } = load();
  let created = 0;
  sandbox.document.createElement = () => { created += 1; return makeStubEl(); };

  T.state.redacted = true;
  const lockedBlock = T.renderSystemLogRow(pullEntry());
  const lockedCount = created;
  assert.ok(lockedBlock.classList.contains("syslog-nested-locked"),
    "redacted pull envelope must be marked locked");

  created = 0;
  T.state.redacted = false;
  T.renderSystemLogRow(pullEntry());
  const unlockedCount = created;

  // Unlocked path builds a node per step (via _renderNestedStepRow); the
  // locked path returns early before that loop, so it creates strictly fewer.
  assert.ok(unlockedCount > lockedCount,
    `locked render should skip step nodes (locked=${lockedCount}, unlocked=${unlockedCount})`);
});

// ---------------- Updates tab masking (renderUpdateRecord) ----------------

test("renderUpdateRecord masks the receipt in its header when redacted", () => {
  const { T } = load();
  const u = {
    caseLabel: "I-485", receiptNumber: "IOE0000000000",
    to: "2026-06-16T12:00:00Z", kind: "status", scalars: {},
  };
  T.state.redacted = false;
  assert.ok(T.renderUpdateRecord(u).innerHTML.includes("IOE0000000000"));

  T.state.redacted = true;
  const html = T.renderUpdateRecord(u).innerHTML;
  assert.ok(!html.includes("IOE0000000000"), "receipt must be masked");
  assert.ok(html.includes(T.REDACTION_MASK));
  assert.ok(html.includes("I-485"), "non-PII form label kept");
});
