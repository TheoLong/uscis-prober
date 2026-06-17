// Unit tests for redaction mode (src/static/app.js): the deep snapshot
// redactor, the display-mask helper, and the toggle wiring.

import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp } from "./_appsandbox.mjs";

const EXPOSE = [
  "redactSnapshot", "redactDisplay", "REDACT_KEYS", "REDACTION_MASK",
  "state", "wireRedactionPill",
];

function load(extra = {}) {
  return loadApp(EXPOSE, extra);
}

const MASK = "••••••••"; // ••••••••

// ---------------- redactSnapshot (deep PII masking) ----------------

test("redactSnapshot masks the three PII keys at the top level", () => {
  const { T } = load();
  const out = T.redactSnapshot({
    receiptNumber: "IOE0935749409",
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
  const input = { receiptNumber: "IOE0935749409", events: [{ code: "ABC" }] };
  const out = T.redactSnapshot(input);
  assert.equal(input.receiptNumber, "IOE0935749409", "input must be unchanged");
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

test("REDACT_KEYS holds exactly the three PII keys", () => {
  const { T } = load();
  const keys = [...T.REDACT_KEYS].sort();
  assert.deepEqual(keys, ["applicantName", "receiptNumber", "representativeName"]);
});

// ---------------- redactDisplay (single-value mask, state-gated) ----------------

test("redactDisplay passes values through when redaction is off", () => {
  const { T } = load();
  T.state.redacted = false;
  assert.equal(T.redactDisplay("IOE0935749409"), "IOE0935749409");
});

test("redactDisplay returns the mask when redaction is on", () => {
  const { T } = load();
  T.state.redacted = true;
  assert.equal(T.redactDisplay("IOE0935749409"), T.REDACTION_MASK);
});

// ---------------- wireRedactionPill (toggle flow) ----------------

function harness() {
  const store = new Map();
  const localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
  };
  const calls = { render: 0, toast: [] };
  const { T, sandbox } = load({ localStorage });
  sandbox.renderCases = () => { calls.render += 1; };
  sandbox.toast = (msg, kind) => calls.toast.push({ msg, kind });
  const pill = {
    _checked: "false",
    addEventListener(ev, fn) { this._handlers = this._handlers || {}; this._handlers[ev] = fn; },
    setAttribute(k, v) { if (k === "aria-checked") this._checked = v; },
    getAttribute(k) { return k === "aria-checked" ? this._checked : null; },
    _click() { return this._handlers.click(); },
  };
  sandbox.document.getElementById = (id) => (id === "redaction-pill" ? pill : null);
  return { T, sandbox, pill, calls, store };
}

test("wireRedactionPill toggles state ON: persists, re-renders, warns", () => {
  const h = harness();
  h.T.state.redacted = false;
  h.T.wireRedactionPill();
  h.pill._click();

  assert.equal(h.T.state.redacted, true);
  assert.equal(h.store.get("uscis_prober_state_v1") !== undefined, true);
  assert.match(h.store.get("uscis_prober_state_v1"), /"redacted":true/);
  assert.equal(h.pill.getAttribute("aria-checked"), "true");
  assert.equal(h.calls.render, 1, "re-renders cards so masking applies");
  assert.equal(h.calls.toast.length, 1);
  assert.equal(h.calls.toast[0].kind, "warn");
});

test("wireRedactionPill toggles OFF again: persists false, re-renders", () => {
  const h = harness();
  h.T.state.redacted = true;
  h.T.wireRedactionPill();
  h.pill._click();

  assert.equal(h.T.state.redacted, false);
  assert.match(h.store.get("uscis_prober_state_v1"), /"redacted":false/);
  assert.equal(h.pill.getAttribute("aria-checked"), "false");
  assert.equal(h.calls.render, 1);
  assert.equal(h.calls.toast[0].kind, "");
});

test("wireRedactionPill reflects the restored preference on wire", () => {
  const h = harness();
  h.T.state.redacted = true;       // as boot would set from persistState
  h.T.wireRedactionPill();
  assert.equal(h.pill.getAttribute("aria-checked"), "true");
});
