// Headless DOM render tests for the redaction masking that needs a *real*
// DOM (the <template>-cloned case card, appendChild-built panels). We load the
// actual index.html + app.js into jsdom and drive the real render functions,
// then assert the rendered DOM carries no PII when redaction is on.
//
// jsdom is a dev-only dependency; if it isn't installed the whole file degrades
// to a single skipped test so a pure-pip environment still goes green.

import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const STATIC = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../src/static");
const MASK = "••••••••";
const RECEIPT = "IOE0935749409";

let JSDOM;
try { ({ JSDOM } = await import("jsdom")); } catch { /* not installed */ }

if (!JSDOM) {
  test("DOM render tests skipped (jsdom not installed)", { skip: "jsdom unavailable" }, () => {});
} else {
  // Build a fresh app instance: real index.html DOM + app.js evaluated in it.
  // We append an export hook so the test can reach the module-scope `state`
  // const and the render functions (const bindings don't attach to window).
  function freshApp() {
    const html = fs.readFileSync(path.join(STATIC, "index.html"), "utf8");
    const dom = new JSDOM(html, { runScripts: "outside-only" });
    const { window } = dom;
    // Neutralize the DOMContentLoaded boot — it would fire fetch()/timers
    // after the test ends. We drive the render functions directly instead.
    const realAdd = window.document.addEventListener.bind(window.document);
    window.document.addEventListener = (type, ...rest) => {
      if (type !== "DOMContentLoaded") realAdd(type, ...rest);
    };
    window.requestAnimationFrame = () => 0;
    window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
    const src = fs.readFileSync(path.join(STATIC, "app.js"), "utf8") +
      "\n;window.__T = { state, REDACTION_MASK, renderCases, renderChanges, renderRaw," +
      " _renderSubFacts };";
    window.eval(src);
    return { window, doc: window.document, T: window.__T };
  }

  const caseFixture = () => ({
    id: RECEIPT, label: "I-485", receiptNumber: RECEIPT,
    applicantName: "DOE, JANE", formName: "Application to Register Permanent Residence",
    closed: false, actionRequired: false, captures: 5, days: 3,
    capturedAt: "2026-06-16T12:00:00Z", summary: {},
    latest: {
      closed: false, actionRequired: false, submissionDate: "2026-02-20",
      updatedAt: "2026-03-01", elisChannelType: "Lockbox",
      representativeName: "SMITH, JOHN",
    },
  });

  // ---------- Cases tab: card header (receipt + applicant) ----------

  test("card header masks receipt + applicant when redacted", () => {
    const { doc, T } = freshApp();
    T.state.cases = [caseFixture()];
    // Render the raw tab (avoids the heavy Overview renderer) — header masking
    // is independent of the active panel.
    T.state.activeTab[RECEIPT] = "raw";
    T.state.histories["I-485"] = { entries: [], locationEntries: [], changes: [] };

    T.state.redacted = false;
    T.renderCases();
    assert.equal(doc.querySelector(".case-receipt").textContent, RECEIPT);
    assert.equal(doc.querySelector(".case-meta .big").textContent, "Jane Doe");

    T.state.redacted = true;
    T.renderCases();
    const html = doc.getElementById("case-list").innerHTML;
    assert.ok(!html.includes(RECEIPT), "receipt must not appear in the card DOM");
    assert.equal(doc.querySelector(".case-receipt").textContent, MASK);
    assert.ok(doc.querySelector(".case-receipt").classList.contains("redacted-text"));
    assert.equal(doc.querySelector(".case-meta .big").textContent, MASK);
  });

  // ---------- Cases tab: Overview "Representative" ----------

  test("_renderSubFacts masks the representative name when redacted", () => {
    const { T } = freshApp();
    const c = caseFixture();

    T.state.redacted = false;
    let sub = T._renderSubFacts(c, c.latest);
    assert.ok(sub.textContent.includes("John Smith"), "representative shown when not redacted");

    T.state.redacted = true;
    sub = T._renderSubFacts(c, c.latest);
    assert.ok(!/SMITH|John Smith/.test(sub.textContent), "representative must be masked");
    assert.ok(sub.textContent.includes(MASK));
  });

  // ---------- Cases tab: Changes scalar diffs ----------

  test("renderChanges masks PII scalar diffs, keeps non-PII ones", () => {
    const { window, T } = freshApp();
    T.state.histories["I-485"] = {
      changes: [{
        kind: "status", to: "2026-06-16T12:00:00Z",
        scalars: {
          applicantName: { from: "DOE, JANE", to: "DOE, J" },
          closed: { from: false, to: true },
        },
      }],
    };
    const panel = window.document.createElement("div");

    T.state.redacted = true;
    T.renderChanges(panel, caseFixture());
    assert.ok(!panel.textContent.includes("JANE"), "PII scalar value must be masked");
    assert.ok(panel.textContent.includes(MASK));
    assert.ok(panel.textContent.includes("true"), "non-PII scalar (closed) kept");
  });

  // ---------- Cases tab: Raw JSON view + disabled exfil buttons ----------

  test("renderRaw masks the snapshot JSON and disables download/copy when redacted", () => {
    const { window, T } = freshApp();
    T.state.histories["I-485"] = {
      entries: [{ capturedAt: "2026-06-16T12:00:00Z", data: {
        receiptNumber: RECEIPT, applicantName: "DOE, JANE",
        representativeName: "SMITH, JOHN", formType: "I-485",
      } }],
      locationEntries: [],
    };
    const panel = window.document.createElement("div");

    T.state.redacted = true;
    T.renderRaw(panel, caseFixture());

    const pre = panel.querySelector("pre.raw");
    assert.ok(pre, "raw JSON pre rendered");
    assert.ok(!pre.textContent.includes(RECEIPT), "receipt must be masked in JSON");
    assert.ok(!pre.textContent.includes("JANE"), "applicant must be masked in JSON");
    assert.ok(pre.textContent.includes(MASK));
    assert.ok(pre.textContent.includes("I-485"), "non-PII formType kept");

    const buttons = [...panel.querySelectorAll(".raw-actions .raw-btn")];
    assert.ok(buttons.length >= 2, "download + copy buttons present");
    assert.ok(buttons.every(b => b.disabled), "both exfil buttons disabled while redacted");

    // Sanity: not redacted → real data shown, buttons enabled.
    T.state.redacted = false;
    const panel2 = window.document.createElement("div");
    T.renderRaw(panel2, caseFixture());
    assert.ok(panel2.querySelector("pre.raw").textContent.includes(RECEIPT));
    assert.ok([...panel2.querySelectorAll(".raw-actions .raw-btn")].every(b => !b.disabled));
  });
}
