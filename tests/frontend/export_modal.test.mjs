// Unit tests for the Export modal (src/static/app.js). The single Export
// button opens a chooser with three options — case data, shareable demo,
// system log — each with a title, description, and its own download button.

import { test } from "node:test";
import assert from "node:assert/strict";
import { loadApp, makeStubEl } from "./_appsandbox.mjs";

const EXPOSE = ["EXPORT_OPTIONS", "openExportModal", "closeExportModal"];

// Minimal DOM: capture appended overlay so we can read its innerHTML. The
// renderer builds the whole panel via innerHTML then queries for buttons —
// the generic stub's querySelector/querySelectorAll return no-op stubs, which
// is fine because these tests assert on structure, not on click behaviour.
function makeDom() {
  const appended = [];
  const doc = {
    addEventListener() {},
    removeEventListener() {},
    querySelector: () => null,
    createElement: () => makeStubEl(),
    body: { appendChild(el) { appended.push(el); } },
  };
  return { doc, appended };
}

test("EXPORT_OPTIONS lists the three exports with correct endpoints", () => {
  const { T } = loadApp(EXPOSE);
  const byKey = Object.fromEntries(T.EXPORT_OPTIONS.map(o => [o.key, o]));
  assert.equal(byKey.data.url, "/api/export");
  assert.equal(byKey.demo.url, "/api/export-demo");
  assert.equal(byKey.log.url, "/api/system-log/export");
  // Every option has a human title, a description, and a button label.
  for (const o of T.EXPORT_OPTIONS) {
    assert.ok(o.title && o.desc && o.button, `option ${o.key} missing fields`);
  }
});

test("openExportModal renders one row per option with a download button", () => {
  const { doc, appended } = makeDom();
  const { T } = loadApp(EXPOSE, { document: doc, escapeHtml: (s) => String(s) });
  T.openExportModal();
  assert.equal(appended.length, 1, "one overlay appended");
  const html = appended[0].innerHTML;
  // Modal titled Export.
  assert.match(html, /<h3>Export<\/h3>/);
  // A row + download button per option, keyed on data-export-key.
  for (const o of T.EXPORT_OPTIONS) {
    assert.match(html, new RegExp(`data-export-key="${o.key}"`));
    assert.ok(html.includes(o.title), `missing title ${o.title}`);
  }
  assert.equal((html.match(/class="[^"]*export-option-btn/g) || []).length, 3);
});
