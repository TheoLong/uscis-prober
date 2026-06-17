// Shared helper: load the non-modular browser script src/static/app.js inside
// a node:vm sandbox so its pure helpers can be unit-tested without a browser.
// Only `document.addEventListener` runs at load time, so a tiny DOM stub is
// enough; we expose the requested top-level symbols by appending an assignment
// that runs in the script's own scope (no changes to app.js).

import vm from "node:vm";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const APP_JS = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "../../src/static/app.js",
);

// Generic DOM-element stand-in. The renderers create a node, set
// className/innerHTML/textContent, and (for nested rows) querySelector +
// wire listeners. This captures the interesting properties and tolerates the
// rest as no-ops. addEventListener records handlers so tests can dispatch.
export function makeStubEl() {
  let _class = "";
  let _html = "";
  let _text = "";
  const handlers = {};
  return {
    set className(v) { _class = v; },
    get className() { return _class; },
    set innerHTML(v) { _html = v; },
    get innerHTML() { return _html; },
    set textContent(v) { _text = v; },
    get textContent() { return _text; },
    hidden: false,
    disabled: false,
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c, on) { (on === undefined ? !this._set.has(c) : on) ? this._set.add(c) : this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    querySelector: () => makeStubEl(),
    querySelectorAll: () => [],
    appendChild() {},
    setAttribute() {},
    getAttribute() { return null; },
    addEventListener(ev, fn) { handlers[ev] = fn; },
    // test helper: invoke a registered listener (returns its promise/result)
    _click() { return handlers.click && handlers.click(); },
  };
}

// Load app.js and return { T, sandbox }. `names` is the list of top-level
// identifiers to expose on T. `extraSandbox` overrides/extends the globals.
export function loadApp(names, extraSandbox = {}) {
  const src = fs.readFileSync(APP_JS, "utf8");
  const sandbox = {
    document: { addEventListener() {}, createElement: () => makeStubEl() },
    window: {},
    console: { ...console, error() {} },
    ...extraSandbox,
  };
  vm.createContext(sandbox);
  const exposed = src + `\n;globalThis.__T = { ${names.join(", ")} };`;
  vm.runInContext(exposed, sandbox, { filename: "app.js" });
  return { T: sandbox.__T, sandbox };
}
