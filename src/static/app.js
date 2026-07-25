// Copyright (C) 2026 the USCIS Prober contributors
// SPDX-License-Identifier: AGPL-3.0-or-later
// USCIS Prober UI — vanilla JS, no framework.

const state = {
  cases: [],
  histories: {},           // label → history payload
  activeTab: {},           // receiptNumber → tab id
  rawSource: {},           // receiptNumber → "case" (raw sub-tab)
  rawSelection: {},        // "{receipt}:{source}" → capturedAt for raw view
  nextRun: null,
  pullRunning: false,
  eventCodeLabels: {},     // server sends {} — USCIS event codes are undocumented, shown raw
  view: "cases",           // "cases" | "systemlog"
  updates: [],             // flat diff feed
  systemLog: [],           // current page of the event log (oldest-first, as returned by server)
  systemLogTotal: 0,       // total entries on disk (all pages combined)
  systemLogPage: 1,        // 1-indexed page (page 1 = newest)
  systemLogPageSize: 100,
  versionSha: null,        // last-seen short SHA from /api/pull/status
  redacted: false,         // when true, mask PII across the dashboard (share/screenshot mode)
  accessLockout: false,    // when true, the server gates the site behind the admin-password login
};

// USCIS API endpoints, mirroring the ones the backend pulls from
// (uscis_status.py → case_status, uscis_api.py → cases). Each returns raw
// JSON and requires an authenticated my.uscis.gov session in the browser.
const USCIS_API = {
  status: (receipt) =>
    `https://my.uscis.gov/account/case-service/api/case_status/${encodeURIComponent(receipt)}`,
  case: (receipt) =>
    `https://my.uscis.gov/account/case-service/api/cases/${encodeURIComponent(receipt)}`,
};

// Small "API" pill linking to the raw USCIS endpoint a section is sourced
// from. `kind` is "status" or "case".
//
// Normal mode: a plain <a> to the endpoint (receipt is not secret), opens in
// a new tab. Redaction mode: the receipt is masked server-side, so a client-
// built URL would carry the masked number and be useless. Instead the pill
// becomes a locked control that, on click, asks for the admin password and
// resolves the true URL via /api/case-api-link, then opens it. Returns null
// when there's nothing to link to.
function apiLinkButton(receipt, kind, label) {
  const TOOLTIP = "Link to USCIS API link with raw json response, login required at https://myaccount.uscis.gov/sign-in";

  if (state.redacted === true) {
    // Locked variant — the real receipt lives only on the server now.
    if (!label) return null;
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "api-link api-link-locked";
    btn.textContent = "API";
    btn.title = TOOLTIP;
    btn.setAttribute("aria-label", btn.title);
    btn.dataset.guard = "redaction";
    btn.addEventListener("click", () => resolveAndOpenApiLink(btn, kind, label));
    return btn;
  }

  if (!receipt) return null;
  const href = (USCIS_API[kind] || USCIS_API.case)(receipt);
  const a = document.createElement("a");
  a.className = "api-link";
  a.href = href;
  a.target = "_blank";
  a.rel = "noopener noreferrer";
  a.textContent = "API";
  a.title = TOOLTIP;
  a.setAttribute("aria-label", a.title);
  return a;
}

// Redaction-mode click handler for a locked API pill: prompt for the admin
// password, ask the server for the true URL, and open it in a new tab. The
// server only unmasks the receipt for a valid password (see /api/case-api-link).
async function resolveAndOpenApiLink(btn, kind, label) {
  const pw = await adminChallenge({ action: "open the USCIS API link" });
  if (pw === null) return;  // cancelled
  try {
    const res = await fetch("/api/case-api-link", withAdminHeader({
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label, kind }),
    }, pw));
    if (res.status === 401) { toast("Wrong password — link locked.", "bad"); return; }
    if (res.status === 429) { toast("Too many attempts — try again shortly.", "bad"); return; }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (!data.ok || !data.url) throw new Error(data.error || "no_url");
    // Open in a new tab. window.open keeps us on the dashboard.
    window.open(data.url, "_blank", "noopener,noreferrer");
  } catch (e) {
    toast(`Couldn't open API link: ${e.message}`, "bad");
  }
}


// ---------- redaction (share/screenshot privacy mode) ----------
//
// Client-side masking so the dashboard can be screenshotted or screen-shared
// without exposing private data. Sensitive *values* are replaced (not just
// blurred) wherever they render, and the snapshot download/copy actions are
// disabled so full data can't leak out while the mode is on.

// Object keys whose values are PII, matched anywhere in any payload tree:
// the case/receipt number (both the snapshot's `receiptNumber` and the
// system-log's `receipt`), the applicant's name, and the representative's
// name. Nested occurrences (e.g. concurrentCases[].receiptNumber) are caught.
const REDACT_KEYS = new Set([
  "receiptNumber", "receipt", "applicantName", "representativeName",
]);

// Defense-in-depth mirror of the server (redaction.py): any OTHER key ending
// in "name" is treated as PII too, so a future USCIS name field is masked
// before it's ever displayed. A small allowlist keeps known-safe name-suffixed
// keys visible (the form type, status/event labels).
const NAME_KEY_ALLOW = new Set(["formname", "statusname", "eventname"]);
function isNameKey(k) {
  if (typeof k !== "string") return false;
  const lk = k.toLowerCase();
  return lk.endsWith("name") && !NAME_KEY_ALLOW.has(lk) && !REDACT_KEYS.has(k);
}

// Any identifier key (eventId, letterId, pid, …) is also masked in displayed
// copies. These are USCIS-internal IDs, so they only need hiding on screen —
// not withholding from the browser, where the timeline still keys on the real
// eventId. Display-layer only; never applied to data the renderer keys on.
function isRedactKey(k) {
  return REDACT_KEYS.has(k) || isNameKey(k) || /id$/i.test(k);
}

// A fixed mask — fixed width so it leaks nothing about the original length.
const REDACTION_MASK = "••••••••";

// PII embedded in otherwise-free text (URLs, titles, log messages): USCIS
// receipt numbers (e.g. IOE0000000000) and email addresses.
const REDACT_PATTERNS = [
  /\b[A-Z]{3}\d{7,}\b/g,
  /\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b/g,
];

// Pure: scrub PII *patterns* out of a free-text string. Non-strings pass
// through untouched. Always scrubs — callers decide when redaction is on.
function scrubText(s) {
  if (typeof s !== "string") return s;
  return REDACT_PATTERNS.reduce((acc, re) => acc.replace(re, REDACTION_MASK), s);
}

// Pure: deep-clone a payload, masking PII-keyed values outright and scrubbing
// PII embedded in any remaining string. Keys are preserved so structure still
// reads normally. Never mutates the input.
function redactSnapshot(value) {
  if (Array.isArray(value)) return value.map(redactSnapshot);
  if (value && typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      out[k] = (isRedactKey(k) && v != null && typeof v !== "object")
        ? REDACTION_MASK
        : redactSnapshot(v);
    }
    return out;
  }
  return scrubText(value);
}

// State-gated: mask a single display value (e.g. a receipt number in a
// header) when redaction is on; otherwise pass it through.
function redactDisplay(value) {
  return state.redacted ? REDACTION_MASK : value;
}

// State-gated: redact a key/value detail (system-log rows, etc.) — mask the
// value outright if its key is PII, deep-redact nested objects, else scrub
// any PII embedded in the string. No-op when redaction is off.
function redactDetailValue(key, value) {
  if (!state.redacted) return value;
  if (isRedactKey(key)) return REDACTION_MASK;
  if (value && typeof value === "object") return redactSnapshot(value);
  return scrubText(value);
}

// State-gated convenience: scrub free text only when redaction is on.
function redactMaybe(s) {
  return state.redacted ? scrubText(s) : s;
}

// ---------- admin-password gating ----------
// One admin password gates the two latch toggles (always) and, while redaction
// is latched, every action button. The server (X-Admin-Password header) is the
// real enforcer; this layer collects the password and shows the lock overlay.

// Mark <body> so CSS can gray + lock-overlay every [data-guard] action.
function applyRedactionLatch() {
  document.body?.classList?.toggle("redaction-latched", state.redacted === true);
}

// The shared password prompt. Resolves with the typed password, or null on
// cancel. Standalone so tests can stub it.
function requestAdminPassword({ action = "continue" } = {}) {
  return new Promise((resolve) => {
    if (document.querySelector(".modal-overlay[data-modal='admin-pw']")) { resolve(null); return; }
    const overlay = document.createElement("div");
    overlay.className = "modal-overlay";
    overlay.dataset.modal = "admin-pw";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");
    overlay.setAttribute("aria-labelledby", "admin-pw-title");
    overlay.innerHTML =
      `<div class="modal-card">` +
      `<h3 id="admin-pw-title" class="modal-title">Admin password required</h3>` +
      `<p class="modal-body">to ${escapeHtml(action)}</p>` +
      `<input type="password" class="admin-pw-input" autocomplete="off" ` +
      `spellcheck="false" placeholder="site admin password" aria-label="Admin password">` +
      `<div class="modal-actions">` +
      `<button type="button" class="modal-btn modal-btn-cancel">Cancel</button>` +
      `<button type="button" class="modal-btn modal-btn-danger">Confirm</button>` +
      `</div></div>`;

    let done = false;
    const finish = (value) => {
      if (done) return;
      done = true;
      document.removeEventListener("keydown", onKey);
      overlay.remove();
      resolve(value);
    };
    const input = overlay.querySelector(".admin-pw-input");
    const submit = () => finish(input.value ? input.value : null);
    const onKey = (e) => {
      if (e.key === "Escape") finish(null);
      else if (e.key === "Enter") { e.preventDefault(); submit(); }
    };
    overlay.querySelector(".modal-btn-danger").addEventListener("click", submit);
    overlay.querySelector(".modal-btn-cancel").addEventListener("click", () => finish(null));
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) finish(null); });
    document.addEventListener("keydown", onKey);
    document.body.appendChild(overlay);
    requestAnimationFrame(() => input.focus());
  });
}

// A button's visible label, whitespace-collapsed, used as the prompt's [action].
function btnLabel(el) {
  return (el?.textContent || "").replace(/\s+/g, " ").trim();
}

// Returns "" if no challenge is needed, the password to send, or null on cancel.
// `always:true` forces the prompt (the latch toggles always require it).
async function adminChallenge({ always = false, action } = {}) {
  // In the static demo export there is no backend to authenticate against, so
  // every guarded action is inert. Show one universal notice instead of the
  // password prompt and abort (null = treated as cancel by every caller).
  if (window.__DEMO_MODE__) {
    toast("Action not available in demo site", "warn");
    return null;
  }
  if (!always && state.redacted !== true) return "";
  return await requestAdminPassword({ action });
}

// Clone a fetch init with the X-Admin-Password header attached (no-op for "").
function withAdminHeader(init = {}, pw = "") {
  if (!pw) return init;
  return { ...init, headers: { ...(init.headers || {}), "X-Admin-Password": pw } };
}

// A bare <a> can't carry the password header, so while redaction is latched
// fetch the archive with the header and save the blob; otherwise let the href go.
async function guardedDownload(evt, url) {
  if (state.redacted !== true) return;  // let the plain href download proceed
  evt.preventDefault();
  const pw = await adminChallenge({ action: btnLabel(evt.currentTarget) });
  if (pw === null) return;
  try {
    const res = await fetch(url, withAdminHeader({}, pw));
    if (res.status === 401) { toast("Wrong password — export blocked.", "bad"); return; }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const m = /filename="?([^"]+)"?/.exec(cd);
    const name = (m && m[1]) || url.split("/").pop() + ".zip";
    const href = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = href;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(href);
  } catch (e) {
    toast(`Export failed: ${e.message}`, "bad");
  }
}

// ---------- boot ----------

document.addEventListener("DOMContentLoaded", async () => {
  document.getElementById("pull-btn").addEventListener("click", triggerPull);
  // Export data is an <a href> — intercept for the guarded blob path.
  document.getElementById("export-btn")
    ?.addEventListener("click", (e) => guardedDownload(e, "/api/export"));
  document.getElementById("export-demo-btn")
    ?.addEventListener("click", (e) => guardedDownload(e, "/api/export-demo"));
  wireExportInfo();
  wireDebugPill();
  wireRecomputeButton();
  wireRedactionPill();
  wireAccessLockoutPill();
  wireMfaModal();
  _wireSyslogFit();
  _wireTopbarFlat();
  _wireSysCardCollapse();
  _wireTooltips();
  wireEventLinkResize();
  document.querySelectorAll(".view-tab").forEach(btn =>
    btn.addEventListener("click", () => setView(btn.dataset.view))
  );
  // Restore the previously selected tab BEFORE refreshAll() so the
  // right view paints on first frame instead of flickering through
  // the default "cases" view first.
  const savedView = persistState.get("view");
  if (savedView && ["cases", "systemlog"].includes(savedView)) {
    setView(savedView);
  }
  // Sync redaction + lockout state before the first render so the masking and
  // lock overlay land on the initial paint.
  await Promise.all([loadRedactionState(), loadAccessLockoutState()]);
  await refreshAll();
  setInterval(updateCountdown, 1000);

  // Status poll cadence: fast (2s) only while a pull is running,
  // otherwise 30s. The idle poll exists to catch externally-
  // triggered pulls (another browser tab or a scheduler fire) and
  // to refresh the version chip after a deploy — neither needs
  // sub-30s reaction time.
  setInterval(() => {
    const now = Date.now();
    const interval = state.pullRunning ? 2_000 : 30_000;
    if (now - (state._lastStatusPoll || 0) >= interval) {
      state._lastStatusPoll = now;
      pollPullStatus();
    }
  }, 1_000);

  // Storage is event-driven, NOT polled. /api/storage walks the
  // entire data/ tree on every call (os.walk + stat per file), so
  // a periodic poll = constant disk I/O for a value that only
  // changes when the disk actually changes. Refreshes are
  // triggered from the events that move the bar:
  //   - boot (below, once)
  //   - tab switch into System (setView)
  //   - pull finish (pollPullStatus, line ~589)
  //   - clear log button (line ~1512)
  updateStorageBar();
});


async function wireDebugPill() {
  const pill = document.getElementById("debug-mode-pill");
  if (!pill) return;
  // Sync initial state from the server so a config edit is reflected
  // without a restart.
  try {
    const r = await fetch("/api/debug-mode");
    if (r.ok) {
      const { enabled } = await r.json();
      pill.setAttribute("aria-checked", enabled ? "true" : "false");
    }
  } catch (_e) { /* pill stays off; server may be warming up */ }

  pill.addEventListener("click", async () => {
    const currently = pill.getAttribute("aria-checked") === "true";
    const desired = !currently;
    const pw = await adminChallenge({ action: btnLabel(pill) });
    if (pw === null) return;
    pill.disabled = true;
    try {
      const r = await fetch("/api/debug-mode", withAdminHeader({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: desired }),
      }, pw));
      if (r.status === 401) { toast("Wrong password — debug mode unchanged.", "bad"); return; }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const body = await r.json();
      pill.setAttribute("aria-checked", body.enabled ? "true" : "false");
      toast(
        body.enabled
          ? "Debug mode ON — next pull will write full traces"
          : "Debug mode OFF — traces only on failure",
        body.enabled ? "warn" : "",
      );
    } catch (e) {
      toast(`Debug toggle failed: ${e.message}`, "error");
    } finally {
      pill.disabled = false;
    }
  });
}

// Nudge a popover back into the viewport when its natural anchor
// (`right: 0` relative to the badge's wrapper) would push either
// edge past the screen. Called every time a popover opens.
// Reads the current bounding rect, computes the overflow, and
// applies a horizontal translate to snap the popover inside the
// viewport with an 8px margin.
function positionPopover(pop) {
  pop.style.transform = "";   // reset before measuring
  const r = pop.getBoundingClientRect();
  const margin = 28;          // gap between popover and viewport edge
  const vw = window.innerWidth || document.documentElement.clientWidth;
  if (r.left < margin) {
    pop.style.transform = `translateX(${margin - r.left}px)`;
  } else if (r.right > vw - margin) {
    pop.style.transform = `translateX(-${r.right - (vw - margin)}px)`;
  }
}

function wireExportInfo() {
  // Wire each (info-badge, popover) pair on the System tab. Both the
  // "Export data" badge and the "debug" badge share the same toggle
  // semantics: click to open, click outside / Escape to dismiss.
  // Only one popover may be open at a time — opening one closes the
  // others so they can't visually block each other.
  const pairs = [
    ["export-info-btn",         "export-info-popover"],
    ["export-demo-info-btn",    "export-demo-info-popover"],
    ["debug-info-btn",          "debug-info-popover"],
    ["recompute-info-btn",      "recompute-info-popover"],
    ["redaction-info-btn",      "redaction-info-popover"],
    ["access-lockout-info-btn", "access-lockout-info-popover"],
  ];
  const popovers = pairs
    .map(([btnId, popId]) => ({
      btn: document.getElementById(btnId),
      pop: document.getElementById(popId),
    }))
    .filter(p => p.btn && p.pop);

  const closeAll = () => {
    for (const p of popovers) {
      p.pop.hidden = true;
      p.pop.style.transform = "";
      p.btn.setAttribute("aria-expanded", "false");
    }
  };

  for (const p of popovers) {
    const { btn, pop } = p;
    btn.addEventListener("click", e => {
      e.stopPropagation();
      const open = pop.hidden;
      // Always close every other popover first so only one is
      // visible at a time.
      closeAll();
      pop.hidden = !open;
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      // After the browser has rendered the un-hidden popover, snap
      // it back inside the viewport if either edge spilled out.
      if (!pop.hidden) requestAnimationFrame(() => positionPopover(pop));
    });
  }

  // Re-position any open popover on resize / orientation change so
  // a phone rotation doesn't leave it stranded off-screen.
  window.addEventListener("resize", () => {
    for (const { pop } of popovers) {
      if (!pop.hidden) positionPopover(pop);
    }
  });

  // Single document-level handlers for outside-click + Escape so
  // every popover dismisses together.
  document.addEventListener("click", e => {
    for (const { btn, pop } of popovers) {
      if (!pop.hidden && !pop.contains(e.target) && e.target !== btn) {
        pop.hidden = true;
        btn.setAttribute("aria-expanded", "false");
      }
    }
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") closeAll();
  });
}

// Recompute diff button (System tab). POSTs to the recompute endpoint, which
// regenerates the diff feed across every case and appends a diff_recomputed
// event, then repaints via the shared refreshAfterRecompute() — the same path
// the scheduled recompute uses.
function wireRecomputeButton() {
  const btn = document.getElementById("recompute-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const pw = await adminChallenge({ action: btnLabel(btn) });
    if (pw === null) return;
    const original = btn.textContent;
    btn.disabled = true;
    btn.textContent = "Recomputing…";
    try {
      const res = await fetch("/api/system-log/recompute", withAdminHeader({ method: "POST" }, pw));
      if (res.status === 401) { toast("Wrong password — recompute blocked.", "bad"); return; }
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const body = await res.json().catch(() => ({}));
      if (body && body.ok === false) throw new Error("recompute reported failure");
      // diff_recomputed is the newest entry — jump to page 1 so it's in view.
      state.systemLogPage = 1;
      await refreshAfterRecompute();
      toast("Diff recomputed — views refreshed.", "ok");
    } catch (e) {
      console.error("Recompute diff failed:", e);
      toast("Recompute failed — diff feed not refreshed.", "bad");
    } finally {
      btn.disabled = false;
      btn.textContent = original;
    }
  });
}

// Sync the redaction pill + state.redacted from the server. Called in boot
// before the first render and reused by the pill wiring.
async function loadRedactionState() {
  try {
    const r = await fetch("/api/redaction-mode");
    if (r.ok) state.redacted = (await r.json()).enabled === true;
  } catch (_e) { /* leave as-is; server may be warming up */ }
  const pill = document.getElementById("redaction-pill");
  if (pill) pill.setAttribute("aria-checked", state.redacted ? "true" : "false");
  applyRedactionLatch();
}

// Redaction toggle: server-side switch that masks PII and password-gates every
// action. Toggling requires the password; on success re-fetch so data repaints.
function wireRedactionPill() {
  const pill = document.getElementById("redaction-pill");
  if (!pill) return;
  pill.setAttribute("aria-checked", state.redacted ? "true" : "false");
  pill.addEventListener("click", async () => {
    const desired = !state.redacted;
    const pw = await adminChallenge({ always: true, action: `toggle ${btnLabel(pill)}` });
    if (pw === null) return;  // cancelled
    pill.disabled = true;
    try {
      const r = await fetch("/api/redaction-mode", withAdminHeader({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: desired }),
      }, pw));
      if (r.status === 401) { toast("Wrong password — redaction unchanged.", "bad"); return; }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      state.redacted = (await r.json()).enabled === true;
      pill.setAttribute("aria-checked", state.redacted ? "true" : "false");
      applyRedactionLatch();
      // Re-fetch every view's data (now masked / unmasked at the source) and
      // repaint. refreshAll re-renders cases; the visible feed is repainted
      // below (a later tab switch re-renders the others via setView()).
      await refreshAll();
      if (state.view === "systemlog") renderSystemLog();
      toast(
        state.redacted
          ? "Redaction ON — PII masked, actions locked behind the password"
          : "Redaction OFF — sensitive data visible, actions unlocked",
        state.redacted ? "warn" : "",
      );
    } catch (e) {
      toast(`Redaction toggle failed: ${e.message}`, "error");
    } finally {
      pill.disabled = false;
    }
  });
}

// Sync the access-lockout pill + state.accessLockout from the server.
async function loadAccessLockoutState() {
  try {
    const r = await fetch("/api/access-lockout");
    if (r.ok) state.accessLockout = (await r.json()).enabled === true;
  } catch (_e) { /* leave as-is; server may be warming up */ }
  const pill = document.getElementById("access-lockout-pill");
  if (pill) pill.setAttribute("aria-checked", state.accessLockout ? "true" : "false");
}

// Access Lock toggle: when ON, the server gates the site behind the login.
// Toggling requires the password; no data re-fetch (it changes access, not data).
function wireAccessLockoutPill() {
  const pill = document.getElementById("access-lockout-pill");
  if (!pill) return;
  pill.setAttribute("aria-checked", state.accessLockout ? "true" : "false");
  pill.addEventListener("click", async () => {
    const desired = !state.accessLockout;
    const pw = await adminChallenge({ always: true, action: `toggle ${btnLabel(pill)}` });
    if (pw === null) return;
    pill.disabled = true;
    try {
      const r = await fetch("/api/access-lockout", withAdminHeader({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: desired }),
      }, pw));
      if (r.status === 401) { toast("Wrong password — lockout unchanged.", "bad"); return; }
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      state.accessLockout = (await r.json()).enabled === true;
      pill.setAttribute("aria-checked", state.accessLockout ? "true" : "false");
      toast(
        state.accessLockout
          ? "Access Lock ON — visitors must sign in with the password"
          : "Access Lock OFF — dashboard open to view",
        state.accessLockout ? "warn" : "",
      );
    } catch (e) {
      toast(`Access lockout toggle failed: ${e.message}`, "error");
    } finally {
      pill.disabled = false;
    }
  });
}

// ============================================================
// Topbar per-row alignment — when a cluster ends up alone on a
// row, expand it to fill the row so its trailing item snaps to
// the right edge via auto-margin. Driven by vertical centerline
// comparison (align-items: center keeps row members on the same
// centerline regardless of their individual heights, so offsetTop
// alone gives false negatives for "same row").
// ============================================================
function _rowCenter(el) {
  return el.offsetTop + el.offsetHeight / 2;
}
function _sameRow(a, b) {
  if (!a || !b) return false;
  return Math.abs(_rowCenter(a) - _rowCenter(b)) < 6;
}
function _wireTopbarFlat() {
  const topbar = document.querySelector(".topbar");
  const left = topbar?.querySelector(".topbar-left");
  const right = topbar?.querySelector(".topbar-right");
  if (!topbar || !window.ResizeObserver) return;
  let raf = 0;
  const recheck = () => {
    cancelAnimationFrame(raf);
    raf = requestAnimationFrame(() => {
      left?.classList.remove("is-isolated");
      right?.classList.remove("is-isolated");

      // Single-row vs split: if the two clusters' centerlines
      // disagree, the topbar has been forced into a 2-row split.
      // Mark BOTH clusters isolated so each spans its full row
      // and stretches its two children 50/50 — the user wants the
      // 4 boxes to lay out as a clean 2×2 grid in that state.
      if (left && right && !_sameRow(left, right)) {
        left.classList.add("is-isolated");
        right.classList.add("is-isolated");
      }
    });
  };
  recheck();
  new ResizeObserver(recheck).observe(topbar);
  // The topbar's content length changes whenever the subtitle
  // re-renders (every 3s), so re-check on a timer too.
  setInterval(recheck, 3000);
}

// ============================================================
// System-log row fit detection — same pattern as _wireTopbarFlat.
// The desktop .syslog-head grid (220px 110px 1fr auto) reserves
// ~360px of fixed columns; on narrow widths a long event token
// like `scheduler_configured` overflows the 1fr cell and visually
// collides with the right-aligned timestamp. Rather than picking
// a static viewport breakpoint (which fires too early or too late
// depending on the longest event in view), measure each row's
// natural single-row width. If any row would overflow, add
// `.syslog-rows-wrapped` to the feed so all rows reflow into the
// 2-row layout — keeping every row visually consistent.
// ============================================================
let _syslogFitObserver = null;
let _syslogFitRaf = 0;
function _recheckSyslogFit() {
  const feed = document.getElementById("systemlog-feed");
  if (!feed) return;
  cancelAnimationFrame(_syslogFitRaf);
  _syslogFitRaf = requestAnimationFrame(() => {
    feed.classList.remove("syslog-rows-wrapped");
    const heads = feed.querySelectorAll(".syslog-head");
    if (!heads.length) return;
    // Sample any head for its grid gap + container padding. All heads
    // share the same .syslog-head rule so one is representative.
    const sample = heads[0];
    const cs = getComputedStyle(sample);
    const gap = parseFloat(cs.columnGap || cs.gap || 10) || 10;
    // Available row width = the head's own clientWidth minus its
    // horizontal padding. The head is the grid container.
    const padX = parseFloat(cs.paddingLeft || 0) + parseFloat(cs.paddingRight || 0);
    const available = sample.clientWidth - padX;
    if (available <= 0) return;
    let maxNeeded = 0;
    for (const head of heads) {
      let w = 0;
      let n = 0;
      for (const child of head.children) {
        // Skip elements that don't render (e.g. empty source span).
        if (!child.scrollWidth && !child.offsetWidth) continue;
        w += child.scrollWidth;
        n++;
      }
      w += Math.max(0, n - 1) * gap;
      if (w > maxNeeded) maxNeeded = w;
    }
    if (maxNeeded > available) {
      feed.classList.add("syslog-rows-wrapped");
    }
  });
}
function _wireSyslogFit() {
  const feed = document.getElementById("systemlog-feed");
  if (!feed || !window.ResizeObserver) return;
  if (_syslogFitObserver) return;
  _syslogFitObserver = new ResizeObserver(_recheckSyslogFit);
  _syslogFitObserver.observe(feed);
  _recheckSyslogFit();
}

function setView(view) {
  state.view = view;
  persistState.set("view", view);
  document.querySelectorAll(".view-tab").forEach(btn =>
    btn.classList.toggle("active", btn.dataset.view === view)
  );
  document.getElementById("case-list").hidden = view !== "cases";
  document.getElementById("systemlog-feed").hidden = view !== "systemlog";
  if (view === "cases") {
    // Cards were hidden (zero-width) while another view was active, so any
    // event-link overlay that tried to draw in the meantime bailed out.
    // Redraw from the stashed links now that the wraps have width again.
    redrawAllEventLinks();
  }
  if (view === "systemlog") {
    loadAndRenderSystemLog();
    // Force-refresh the storage bar the moment the tab opens so
    // stats are current, not up-to-30s stale from the slow-poll.
    updateStorageBar();
    state._lastStoragePoll = Date.now();
  }
}

// ============================================================
// Tiny localStorage state bag — survives page refresh. Used for
// active tab, system-card collapse states, etc. Errors swallowed
// so a disabled localStorage (private browsing, quota exceeded)
// degrades silently instead of breaking the page.
// ============================================================
const PERSIST_STATE_KEY = "uscis_prober_state_v1";
const persistState = {
  _read() {
    try {
      return JSON.parse(localStorage.getItem(PERSIST_STATE_KEY) || "{}") || {};
    } catch (_) { return {}; }
  },
  _write(obj) {
    try { localStorage.setItem(PERSIST_STATE_KEY, JSON.stringify(obj)); }
    catch (_) { /* ignore */ }
  },
  get(key) {
    return this._read()[key];
  },
  set(key, value) {
    const cur = this._read();
    cur[key] = value;
    this._write(cur);
  },
};

// ============================================================
// System-tab card collapse — every .sys-card with a data-card-id
// gets a clickable title that toggles its body. Collapse state
// persists across refresh via persistState. Each card's body
// (everything except the .sys-card-title) is hidden by the
// `.is-collapsed` CSS rule. Idempotent: safe to call repeatedly
// after re-renders (the System log card replaces its title node
// on every renderSystemLog call).
// ============================================================
function _wireSysCardCollapse() {
  const cards = document.querySelectorAll(".sys-card[data-card-id]");
  for (const card of cards) {
    const id = card.dataset.cardId;
    const collapsed = persistState.get(`card_${id}_collapsed`) === true;
    card.classList.toggle("is-collapsed", collapsed);

    // Inject / refresh the title chrome on whatever title node is
    // currently in the card. Idempotent: skips if the chevron is
    // already there. Re-applying aria-expanded picks up state
    // changes since the last call.
    const title = card.querySelector(".sys-card-title");
    if (title) {
      if (!title.querySelector(".sys-card-toggle")) {
        const tog = document.createElement("span");
        tog.className = "sys-card-toggle";
        tog.setAttribute("aria-hidden", "true");
        tog.textContent = "▼";
        title.insertBefore(tog, title.firstChild);
      }
      title.setAttribute("role", "button");
      title.setAttribute("tabindex", "0");
      title.setAttribute("aria-expanded", collapsed ? "false" : "true");
    }

    // Wire one delegated click listener per card (not per title)
    // so re-rendering the title node doesn't lose handlers and
    // doesn't pile up duplicates.
    if (card.dataset.collapseWired === "true") continue;
    card.dataset.collapseWired = "true";
    const toggle = (e) => {
      if (!e.target.closest(".sys-card-title")) return;
      const nowCollapsed = !card.classList.contains("is-collapsed");
      card.classList.toggle("is-collapsed", nowCollapsed);
      const t = card.querySelector(".sys-card-title");
      if (t) t.setAttribute("aria-expanded", nowCollapsed ? "false" : "true");
      persistState.set(`card_${id}_collapsed`, nowCollapsed);
    };
    card.addEventListener("click", toggle);
    card.addEventListener("keydown", (e) => {
      if (!e.target.closest(".sys-card-title")) return;
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        toggle(e);
      }
    });
  }
}

// ============================================================
// ONE site-wide hover tooltip. Every tooltip on the site flows through this
// single instant popup so they look and behave identically — no native
// ~700ms `title` delay, no separate CSS-pseudo variant. Any element carrying
// `data-tooltip` qualifies; a native `title` is upgraded in place (moved to
// `data-tooltip`, dropped from `title`) on first hover so the browser's own
// tooltip never fires. iframes are exempt (their `title` is an a11y label).
//
// Why a JS popup and not a CSS pseudo-element: pseudos can't be repositioned,
// so a trigger near the right edge pushes the popup off-screen. A real <body>
// node run through positionPopover() stays on-screen and never gets clipped by
// an ancestor's overflow. On hover devices it shows on pointer-enter; on touch
// it tap-toggles.
// ============================================================
const _TIP_SELECTOR = "[data-tooltip], [title]";

function _wireTooltips() {
  if (document.body.dataset.tipsWired === "true") return;
  document.body.dataset.tipsWired = "true";

  const pop = document.createElement("div");
  pop.className = "events-popup";
  pop.hidden = true;
  pop.setAttribute("role", "tooltip");
  document.body.appendChild(pop);

  let active = null;
  const hoverCapable = window.matchMedia("(hover: hover) and (pointer: fine)").matches;

  // Resolve the tooltip text, migrating a native `title` to `data-tooltip` so
  // the native tooltip is suppressed. Preserve the accessible name for
  // icon-only triggers by copying it to aria-label when nothing else labels them.
  const tipText = (el) => {
    if (el.hasAttribute("title")) {
      const t = el.getAttribute("title");
      if (t) {
        if (!el.dataset.tooltip) el.dataset.tooltip = t;
        if (!el.getAttribute("aria-label") && !el.textContent.trim()) {
          el.setAttribute("aria-label", t);
        }
      }
      el.removeAttribute("title");
    }
    return el.dataset.tooltip || "";
  };

  const match = (target) => {
    const el = target?.closest?.(_TIP_SELECTOR);
    return el && el.tagName !== "IFRAME" ? el : null;
  };

  const closePopup = () => {
    pop.hidden = true;
    pop.style.transform = "";
    pop.style.top = "";
    pop.style.left = "";
    active = null;
  };

  const openFor = (el) => {
    const text = tipText(el);
    if (!text) return;
    pop.textContent = text;
    pop.hidden = false;
    active = el;
    // Anchor above the trigger; positionPopover snaps it back on-screen.
    requestAnimationFrame(() => {
      const r = el.getBoundingClientRect();
      const popR = pop.getBoundingClientRect();
      const above = r.top - popR.height - 6;
      const below = r.bottom + 6;
      const top = above >= 8 ? above : below;
      pop.style.top = `${top + window.scrollY}px`;
      pop.style.left = `${r.left + window.scrollX}px`;
      positionPopover(pop);
    });
  };

  // Hover (mouse + pen, not touch). pointerenter/leave fire per element, so
  // close only when the trigger ITSELF is left (not a child).
  if (hoverCapable) {
    document.addEventListener("pointerenter", (e) => {
      const el = match(e.target);
      if (el) openFor(el);
    }, true);
    document.addEventListener("pointerleave", (e) => {
      if (active && e.target === active) closePopup();
    }, true);
    // Tooltips are advisory — never let one swallow a real click on a button.
    document.addEventListener("click", (e) => {
      if (active && !pop.contains(e.target)) closePopup();
    });
  } else {
    // Touch: tap toggles (the only way to reveal a tooltip without hover).
    document.addEventListener("click", (e) => {
      const el = match(e.target);
      if (el) { el === active ? closePopup() : openFor(el); return; }
      if (active && !pop.contains(e.target)) closePopup();
    });
  }

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && active) closePopup();
  });
  window.addEventListener("resize", () => {
    if (active) openFor(active);   // re-anchor cleanly
  });
}

// ---------- data loading ----------

async function refreshAll() {
  await Promise.all([loadCases(), loadUpdates(), loadSystemLog(), pollPullStatus()]);
}

// Repaint the UI after a diff recompute. A recompute regenerates the diff
// feed behind the dashboard, the Updates feed, and the system log, and writes
// a diff_recomputed event that moves the storage bar — so reload all four and
// repaint the active view (the others repaint on their next tab switch). Both
// the manual button and the scheduled post-pull path call this, so the trigger
// never changes the behavior. Excludes pollPullStatus: the scheduled caller is
// pollPullStatus itself, and the manual caller has no pull to poll.
async function refreshAfterRecompute() {
  await Promise.all([loadCases(), loadUpdates(), loadSystemLog(), updateStorageBar()]);
  if (state.view === "systemlog") renderSystemLog();
}

async function loadSystemLog(page = state.systemLogPage) {
  try {
    const perPage = state.systemLogPageSize;
    // Page 1 = newest. Server counts offset from the newest end.
    const offset = Math.max(0, (page - 1) * perPage);
    const url = `/api/system-log?limit=${perPage}&offset=${offset}`;
    const res = await fetch(url);
    const j = await res.json();
    state.systemLog = j.events || [];
    state.systemLogTotal = typeof j.total === "number" ? j.total : state.systemLog.length;
    // If the user's current page falls past the end (e.g. after clear / truncation),
    // snap back to page 1. This keeps the Prev/Next controls sensible.
    const totalPages = Math.max(1, Math.ceil(state.systemLogTotal / perPage));
    state.systemLogPage = Math.min(Math.max(1, page), totalPages);

    const countEl = document.getElementById("systemlog-count");
    if (state.systemLogTotal) {
      countEl.hidden = false;
      // Cap the badge label at "999+" — a 4-digit number crowds the tab.
      // Exact total still appears in the subtitle + pagination controls.
      countEl.textContent = state.systemLogTotal > 999
        ? "999+"
        : String(state.systemLogTotal);
    } else {
      countEl.hidden = true;
    }
  } catch (e) {
    console.warn("loadSystemLog failed:", e);
  }
}

async function gotoSystemLogPage(page) {
  await loadSystemLog(page);
  renderSystemLog();
}

async function loadAndRenderSystemLog() {
  await loadSystemLog();
  renderSystemLog();
}

async function loadUpdates() {
  try {
    const res = await fetch("/api/updates");
    const j = await res.json();
    state.updates = j.updates || [];
    state.eventCodeLabels = { ...state.eventCodeLabels, ...(j.eventCodeLabels || {}) };
    // The global Updates tab was removed — no updates-count badge to maintain.
    // We still load the feed so state.eventCodeLabels stays current.
  } catch (e) {
    console.warn("loadUpdates failed:", e);
  }
}

async function loadCases() {
  const res = await fetch("/api/cases");
  const j = await res.json();
  state.cases = j.cases || [];
  state.eventCodeLabels = j.eventCodeLabels || {};
  await Promise.all(state.cases.map(c => loadHistory(c.label)));
  renderSummary();
  renderCases();
}

async function loadHistory(label) {
  const res = await fetch(`/api/cases/${encodeURIComponent(label)}/history`);
  state.histories[label] = await res.json();
}

// Short SHA rendered in the topbar + tooltip with the full build metadata.
// Updated every 3s from /api/pull/status so a deploy that restarts the VM
// flips the chip automatically without a hard-refresh. If the SHA changes
// between polls, we show a small toast so the operator knows the code
// running in their tab no longer matches what's on the server.
function updateVersionChip(version) {
  const chip = document.getElementById("version-chip");
  if (!chip || !version) return;

  const label = version.label;          // sortable date-time like `2026-04-22.2032`
  const sha = version.sha || "unknown";
  const full = version.full_sha || sha;
  const commitDate = version.commit_date || "";
  const bootTime = version.boot_time || "";

  // Detect rollover by SHA (authoritative) not label (two commits in the
  // same minute could share a label).
  const prev = state.versionSha;
  state.versionSha = sha;

  // Chip text: "Version: <local-stamp>" on a single line. Lives in
  // the System tab's Actions row now (was: topbar) — kept compact
  // so it sits inline next to the DEBUG / Export-data buttons.
  const localStamp = _formatVersionChipLocal(commitDate);
  chip.textContent = localStamp
    ? `Version: ${localStamp}`
    : (label || sha);
  chip.hidden = false;
  chip.title = [
    commitDate ? `Version:  ${_formatVersionChipLocal(commitDate)}` : "",
    label     ? `UTC:      ${label}` : "",
    `Commit:   ${full}`,
    commitDate ? `Authored: ${commitDate}` : "",
    bootTime ? `Booted:   ${bootTime}` : "",
    "",
    "Click to copy full SHA",
  ].filter(Boolean).join("\n");
  chip.href = "#";
  chip.onclick = async (ev) => {
    ev.preventDefault();
    const ok = await copyToClipboard(full);
    const originalText = chip.textContent;
    chip.textContent = ok ? "copied ✓" : "copy failed";
    setTimeout(() => { chip.textContent = originalText; }, 1500);
  };

  if (prev && prev !== sha) {
    toast(`Server rolled over → ${label || sha}`, "ok");
  }
}


// Render an ISO commit timestamp as `YYYY-MM-DDTHH:MM:SS TZ` in the
// browser's local timezone, where TZ is the short zone abbreviation
// (e.g. EDT, EST, PDT). Returns null if the input can't be parsed so
// the caller can fall back to the server's UTC label.
function _formatVersionChipLocal(commitIso) {
  if (!commitIso) return null;
  const d = new Date(commitIso);
  if (Number.isNaN(d.getTime())) return null;
  try {
    // formatToParts gives us each component so we can assemble the
    // ISO-like "YYYY-MM-DDTHH:MM:SS" stub + a human-readable TZ
    // abbreviation without pulling in a date-fns-tz-sized dependency.
    //
    // We grab the Y/M/D/H/M/S components from one en-GB formatter
    // (reliably 24-hour + 2-digit) and the TZ abbreviation from a
    // separate en-US formatter (reliably gives `EDT`, `PST`, etc.
    // on ICU; en-GB tends to emit `GMT-4` which is less readable).
    const numeric = new Intl.DateTimeFormat("en-GB", {
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false,
    }).formatToParts(d);
    const get = (t) => numeric.find(p => p.type === t)?.value || "";
    const hour = get("hour") === "24" ? "00" : get("hour");  // Intl midnight quirk
    const stamp = `${get("year")}-${get("month")}-${get("day")}T${hour}:${get("minute")}:${get("second")}`;
    let tz = "";
    try {
      const zoneParts = new Intl.DateTimeFormat("en-US", {
        hour: "numeric", timeZoneName: "short",
      }).formatToParts(d);
      tz = zoneParts.find(p => p.type === "timeZoneName")?.value || "";
    } catch (_) { /* browser without TZ-names support — stamp only */ }
    return tz ? `${stamp} ${tz}` : stamp;
  } catch (_) {
    return null;
  }
}


async function pollPullStatus() {
  try {
    const res = await fetch("/api/pull/status");
    const s = await res.json();
    state.nextRun = s.next_run ? new Date(s.next_run) : null;
    if (s.version) updateVersionChip(s.version);
    const wasRunning = state.pullRunning;
    state.pullRunning = !!s.running;
    const btn = document.getElementById("pull-btn");
    btn.disabled = state.pullRunning;
    // Button label mirrors the state — the countdown box is the
    // ambient running indicator (was: separate "running…" spinner).
    // Two-line label at wide widths ("Manual" / "Pull Update");
    // CSS collapses it to one line at narrow widths. When the pull
    // is running, swap to a single "Pulling…" status so the button
    // reads as state, not action.
    // Space between the spans so textContent reads "Manual Pull Update"
    // (display:block, so it collapses visually).
    btn.innerHTML = state.pullRunning
      ? `<span class="pull-btn-line">Pulling…</span>`
      : `<span class="pull-btn-line">Manual</span> ` +
        `<span class="pull-btn-line">Pull Update</span>`;
    document.getElementById("next-when").textContent =
      state.nextRun ? formatLocal(state.nextRun) : "—";
    updateCountdown();

    // A finished pull always runs a post-pull diff recompute, so repaint with
    // the same refreshAfterRecompute() the manual button uses.
    if (wasRunning && !state.pullRunning) {
      if (s.ok === false) {
        toast(`Pull failed: ${s.last_error || "see logs"}`, "bad");
      } else {
        toast("Pull complete — data refreshed", "ok");
      }
      await refreshAfterRecompute();
    }
  } catch (e) {
    console.warn("status poll failed:", e);
  }
}

async function triggerPull() {
  const btn = document.getElementById("pull-btn");
  const pw = await adminChallenge({ action: btnLabel(btn) });
  if (pw === null) return;
  try {
    const res = await fetch("/api/pull", withAdminHeader({ method: "POST" }, pw));
    if (res.status === 401) {
      toast("Wrong password — pull blocked.", "bad");
      return;
    }
    if (res.status === 409) {
      toast("A pull is already running…", "bad");
      return;
    }
    const j = await res.json();
    if (!j.ok) {
      toast(j.error || "pull failed", "bad");
      return;
    }
    toast("Pull started…", "ok");
    // Optimistically flip the button so there's no 3-second gap before
    // the next status poll catches up. pollPullStatus() will reconcile
    // against the server state on the next tick.
    btn.disabled = true;
    btn.textContent = "Pulling…";
    pollPullStatus();
  } catch (e) {
    toast("Network error triggering pull", "bad");
  }
}

// ---------- rendering ----------

function renderSummary() {
  const totalCaptures = state.cases.reduce((n, c) => n + (c.captures || 0), 0);
  const last = state.cases
    .map(c => c.capturedAt)
    .filter(Boolean)
    .sort()
    .slice(-1)[0];
  // Announce the tz once here so individual timestamps can stay compact.
  const tz = getLocalTimezoneAbbrev();
  // Mirror the NEXT PULL countdown box layout — label / value / sub
  // on 3 stacked lines at wide widths, single thin line when the
  // topbar wraps. CSS collapses via media query.
  //
  // Label says LAST PULL so the value row drops the redundant
  // "last pull" prefix. The timezone moved to its own dedicated
  // pill (left of LAST PULL) so the value row stays clean.
  const summaryEl = document.getElementById("summary-line");
  const lastPullValue = last ? formatLocal(new Date(last)) : "—";
  const subParts = [`${state.cases.length} cases`, `${totalCaptures} snapshots`];
  summaryEl.innerHTML =
    `<span class="chip-label">Last pull</span>` +
    `<span class="chip-value">${escapeHtml(lastPullValue)}</span>` +
    `<span class="chip-sub">${escapeHtml(subParts.join(" · "))}</span>`;

  // Timezone chip lives in the System info card now (used to be a
  // 3-line topbar pill — moved out so the topbar holds only at-a-
  // glance live state). One line: "Timezone: EDT · America/New_York".
  const tzEl = document.getElementById("tz-info");
  if (tzEl) {
    let ianaName = "";
    try {
      ianaName = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (_) { /* ignore */ }
    const text = ianaName
      ? `Timezone: ${tz} · ${ianaName}`
      : `Timezone: ${tz}`;
    tzEl.textContent = text;
  }
}

function renderCases() {
  const root = document.getElementById("case-list");
  root.innerHTML = "";
  const tmpl = document.getElementById("case-card-template");
  for (const c of state.cases) {
    const node = tmpl.content.cloneNode(true);
    const article = node.querySelector(".case-card");
    // Key the card by its (non-PII) form label, not the receipt number, so
    // the receipt never lands in a DOM attribute (redaction-safe). `label`
    // is already the canonical per-case key (state.histories[label] etc.).
    article.dataset.label = c.label;

    article.querySelector(".case-label").textContent = c.label;
    const receiptEl = article.querySelector(".case-receipt");
    receiptEl.textContent = redactDisplay(c.receiptNumber);
    receiptEl.classList.toggle("redacted-text", state.redacted);

    const meta = article.querySelector(".case-meta");
    meta.innerHTML = "";
    if (c.applicantName) {
      const a = document.createElement("span");
      a.className = "big";
      if (state.redacted) {
        a.textContent = REDACTION_MASK;
        a.classList.add("redacted-text");
      } else {
        a.textContent = formatApplicant(c.applicantName);
      }
      meta.appendChild(a);
    }
    if (c.formName) {
      const f = document.createElement("span");
      f.textContent = c.formName;
      meta.appendChild(f);
    }

    // Status badges: only surface meaningful signals, skip default falses.
    const badges = article.querySelector(".case-badges");
    const latest = c.latest || {};
    if (c.closed === true) badges.appendChild(badge("closed", "bad"));
    if (c.actionRequired) badges.appendChild(badge("action required", "warn"));
    if (latest.isPremiumProcessed) badges.appendChild(badge("premium", "warn"));
    if (latest.cmsFailure) badges.appendChild(badge("Case Management System failure", "bad"));
    if (!badges.children.length && c.closed === false) {
      badges.appendChild(badge("pending", ""));
    }

    // Tabs wiring
    const tabs = article.querySelectorAll(".tab");
    const panels = article.querySelectorAll(".tab-panel");
    const active = state.activeTab[c.receiptNumber] || "overview";
    tabs.forEach(btn => {
      btn.classList.toggle("active", btn.dataset.tab === active);
      btn.addEventListener("click", () => switchTab(c, btn.dataset.tab));
    });
    panels.forEach(p => (p.hidden = p.dataset.tab !== active));

    updateTabCounts(article, c);

    // Initial panel content
    renderPanel(article, c, active);

    root.appendChild(node);
  }
}

function switchTab(caseObj, tabId) {
  state.activeTab[caseObj.receiptNumber] = tabId;
  const article = document.querySelector(
    `.case-card[data-label="${CSS.escape(caseObj.label)}"]`
  );
  if (!article) return;
  article.querySelectorAll(".tab").forEach(btn => {
    btn.classList.toggle("active", btn.dataset.tab === tabId);
  });
  article.querySelectorAll(".tab-panel").forEach(p => {
    p.hidden = p.dataset.tab !== tabId;
  });
  renderPanel(article, caseObj, tabId);
}

function renderPanel(article, c, tabId) {
  const panel = article.querySelector(`.tab-panel[data-tab="${tabId}"]`);
  if (!panel) return;
  if (tabId === "overview") renderOverview(panel, c);
  if (tabId === "changes")  renderChanges(panel, c);
  if (tabId === "raw")      renderRaw(panel, c);
}

// ---------- overview ----------

// Build the verbatim status body: title, paragraph, jurisdiction, and the
// raw action-code fields. Shared by the current status and each historic
// entry. Everything is exactly what USCIS returned; the only transform is
// stripping HTML tags from statusText and converting the action-code
// timestamp to local wall-clock for display.
function buildStatusBody(st) {
  const frag = document.createDocumentFragment();
  if (st.statusTitle) {
    const title = document.createElement("div");
    title.className = "status-title";
    title.textContent = st.statusTitle;
    frag.appendChild(title);
  }
  if (st.statusText) {
    const body = document.createElement("p");
    body.className = "status-text";
    body.textContent = stripTags(st.statusText);
    frag.appendChild(body);
  }
  const rawList = [
    ["Jurisdiction", st.jurisdictionDescription],
    ["Current action code", st.currentActionCode],
    // Action-code date is a UTC ISO timestamp; show it in local time.
    ["Action code date", st.currentActionCodeDate
      ? formatLocalDateTime(st.currentActionCodeDate, { withSeconds: true })
      : null],
  ].filter(([, v]) => v);
  if (rawList.length) {
    const dl = document.createElement("dl");
    dl.className = "status-raw";
    for (const [k, v] of rawList) {
      const dt = document.createElement("dt");
      dt.textContent = k;
      const dd = document.createElement("dd");
      dd.textContent = v;
      dl.appendChild(dt);
      dl.appendChild(dd);
    }
    frag.appendChild(dl);
  }
  return frag;
}

function renderOverview(panel, c) {
  const latest = c.latest || {};
  const s = c.summary || {};
  panel.innerHTML = "";

  // --- Hero metrics: raw data signals only (no inference) ---
  // Once the case is approved (H008/APR0), the pending window is over:
  // "Days pending" (received -> today, still climbing) is replaced by a
  // frozen, green "Approved in days" (received -> approval date). The monitor
  // keeps running because the case isn't done until the card is produced and
  // mailed — this only reframes the one bounded metric.
  const isApproved = s.approvedInDays != null;
  const pendingMetric = isApproved
    ? {
        label: "Approved in days",
        value: s.approvedInDays,
        sub: s.approvedOn ? `approved ${formatDate(s.approvedOn)}` : "",
        tone: "approved",
      }
    : {
        label: "Days pending",
        value: s.daysPending ?? "—",
        sub: latest.submissionDate ? `since ${formatDate(latest.submissionDate)}` : "",
        tone: "",
      };
  const metrics = [
    pendingMetric,
    {
      label: "Days since last activity",
      value: s.daysSinceUpdate ?? "—",
      sub: latest.updatedAt ? `last activity ${formatDate(latest.updatedAt)}` : "",
      tone: s.daysSinceUpdate == null
        ? ""
        : s.daysSinceUpdate <= 7 ? "ok"
        : s.daysSinceUpdate <= 30 ? ""
        : "warn",
    },
    {
      label: "All events",
      value: s.allEvents ?? 0,
      sub: "Event + Silent Events",
      tone: "",
    },
    {
      label: "Silent events",
      value: s.silentUpdates ?? 0,
      sub: "Silent timestamp touch",
      tone: (s.silentUpdates ?? 0) > 0 ? "ok" : "",
    },
  ];
  const metricRow = document.createElement("div");
  metricRow.className = "hero-metrics";
  for (const m of metrics) {
    const box = document.createElement("div");
    box.className = `metric ${m.tone || ""}`;
    // Always render the sub slot (even empty) so every card reserves
    // the same vertical space and card heights line up in the 2x2
    // mobile grid and the 4-wide desktop row.
    box.innerHTML =
      `<div class="metric-label">${escapeHtml(m.label)}</div>` +
      `<div class="metric-value">${escapeHtml(String(m.value))}</div>` +
      `<div class="metric-sub">${escapeHtml(m.sub || "")}</div>`;
    metricRow.appendChild(box);
  }
  panel.appendChild(metricRow);

  // --- Current Status: the plain-English statusTitle/statusText USCIS
  // shows on its public case-status tool, fetched from the dashboard
  // status endpoint. Rendered strictly verbatim — only the exact fields
  // USCIS returned, nothing composed or interpreted (tags in the
  // statusText are stripped to plain text — no raw HTML injection).
  // Timestamps are converted to local wall-clock for display only.
  const st = c.status;
  if (st && (st.statusTitle || st.statusText)) {
    const block = document.createElement("div");
    block.className = "status-block";

    const head = document.createElement("div");
    head.className = "status-head section-head-row";
    const headLabel = document.createElement("span");
    headLabel.textContent = "Current Status";
    head.appendChild(headLabel);
    const statusApiBtn = apiLinkButton(c.receiptNumber, "status", c.label);
    if (statusApiBtn) head.appendChild(statusApiBtn);
    block.appendChild(head);

    block.appendChild(buildStatusBody(st));

    // Status history: the API's own historicalCaseStatuses array, shown
    // verbatim in a dropdown. Each entry has only date / actionCode /
    // statusTitle — exactly what USCIS returns, nothing more.
    const history = Array.isArray(st.history) ? st.history : [];
    if (history.length) {
      const details = document.createElement("details");
      details.className = "status-history";
      const summary = document.createElement("summary");
      summary.textContent = `Status history (${history.length})`;
      details.appendChild(summary);
      for (const h of history) {
        const item = document.createElement("div");
        item.className = "status-history-item";
        const meta = document.createElement("div");
        meta.className = "status-history-when";
        // `date` is USCIS's own plain wall-clock string; the historical
        // entries are always midnight (00:00:00), so strip the time and
        // show just the date. Everything else stays verbatim.
        const histDate = (h.date || "").replace(/\s+00:00:00$/, "");
        meta.textContent = [histDate, h.actionCode].filter(Boolean).join(" · ");
        item.appendChild(meta);
        if (h.statusTitle) {
          const title = document.createElement("div");
          title.className = "status-history-title";
          title.textContent = h.statusTitle;
          item.appendChild(title);
        }
        details.appendChild(item);
      }
      block.appendChild(details);
    }

    panel.appendChild(block);
  }

  // --- Factual callouts: ONLY things pulled verbatim from USCIS fields. ---
  // Anything derived or community-interpreted belongs in the Inferred block
  // at the end of the overview, not here.
  const factCallouts = document.createElement("div");
  factCallouts.className = "callouts";

  if (latest.actionRequired === true) {
    factCallouts.appendChild(
      callout(
        "bad",
        "Action required",
        "USCIS set actionRequired on this case — check the notices / evidence requests in the raw JSON for what's being asked."
      )
    );
  }
  if (latest.closed === true) {
    factCallouts.appendChild(
      callout(
        "ok",
        "Case closed",
        "USCIS marked the case closed. Look at the latest event code to see the outcome (APR0/H008 = approval, DNY0 = denial, CRD0 = card mailed)."
      )
    );
  }
  if (s.upcomingAppointment) {
    const appt = s.upcomingAppointment;
    const when = appt.appointmentDateTime
      ? formatLocalDateTime(appt.appointmentDateTime)
      : "";
    const daysUntil = appt.daysUntil;
    const tail = daysUntil != null ? ` (in ${daysUntil} day${daysUntil === 1 ? "" : "s"})` : "";
    factCallouts.appendChild(
      callout(
        "info",
        `Upcoming: ${appt.actionType || "appointment"}`,
        when ? `Scheduled for ${when}${tail}` : "Scheduled — see notices."
      )
    );
  }
  if (factCallouts.children.length) panel.appendChild(factCallouts);

  // --- Secondary facts: a compact strip, no duplication with header ---
  panel.appendChild(_renderSubFacts(c, latest));

  // --- Observed event codes (factual only — no inference, no stage
  // guessing, no community folklore; form-agnostic). ---
  panel.appendChild(renderObservedEventCodes(c));
}

// Generic info-badge / popover wiring. Mirrors #export-info-btn behaviour
// so the UX is consistent: click toggles, outside click or Escape dismisses.
// Also runs positionPopover after opening so the popup never spills past
// the viewport edge (matters on mobile where most badges sit near an edge).
function wireInfoPopover(btn, pop) {
  const close = () => {
    pop.hidden = true;
    pop.style.transform = "";
    btn.setAttribute("aria-expanded", "false");
  };
  btn.addEventListener("click", e => {
    e.stopPropagation();
    const open = pop.hidden;
    pop.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (!pop.hidden) requestAnimationFrame(() => positionPopover(pop));
  });
  document.addEventListener("click", e => {
    if (!pop.hidden && !pop.contains(e.target) && e.target !== btn) close();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") close();
  });
  // Reposition on resize so an orientation change doesn't strand it.
  window.addEventListener("resize", () => {
    if (!pop.hidden) positionPopover(pop);
  });
}

function _renderSubFacts(c, latest) {
  const sub = document.createElement("div");
  sub.className = "sub-facts";
  let reps = latest.representativeName ? formatApplicant(latest.representativeName) : "";
  // Representative is PII — mask it (keeping the "—" when there is none).
  if (state.redacted && reps) reps = REDACTION_MASK;
  const facts = [
    { k: "Submitted",      v: latest.submissionDate ? formatDate(latest.submissionDate) : "—" },
    { k: "Channel",        v: latest.elisChannelType || "—" },
    { k: "Representative", v: reps || "—", redacted: state.redacted && !!latest.representativeName },
    { k: "Snapshots",      v: `${c.captures ?? 0}` },
    { k: "Days logged",    v: `${c.days ?? 0}` },
    { k: "Last pulled",    v: c.capturedAt ? formatLocalDateTime(c.capturedAt) : "—", mono: true },
  ];
  for (const f of facts) {
    const el = document.createElement("div");
    el.className = "sub-fact";
    const vClass = (f.mono ? " mono" : "") + (f.redacted ? " redacted-text" : "");
    el.innerHTML =
      `<span class="sub-fact-k">${escapeHtml(f.k)}</span>` +
      `<span class="sub-fact-v${vClass}">${escapeHtml(String(f.v))}</span>`;
    sub.appendChild(el);
  }
  return sub;
}

// ---------- changes ----------

function updateTabCounts(article, c) {
  // The Updates tab badge counts exactly the rows renderChanges paints —
  // the case diff feed — so badge and tab content can't drift.
  const hist = state.histories[c.label];
  const n = ((hist && hist.changes) || []).length;
  const badge = article.querySelector('.tab-count[data-count="changes"]');
  if (!badge) return;
  badge.textContent = String(n);
  badge.hidden = n === 0;
}

function renderChanges(panel, c) {
  panel.innerHTML = "";
  const hist = state.histories[c.label];
  const changes = (hist && hist.changes) || [];
  if (!changes.length) {
    panel.innerHTML = `<div class="no-changes">No differences detected between consecutive captures.</div>`;
    return;
  }
  // Show newest first
  for (const ch of [...changes].reverse()) {
    panel.appendChild(renderChangeBlock(ch));
  }
}

const KIND_INFO = {
  silent_update:  { label: "silent update",  tone: "silent",
                 desc: "Case update timestamp advanced; no visible event or notice." },
  event:       { label: "new event",      tone: "ok" },
  notice:      { label: "new notice",     tone: "warn" },
  appointment: { label: "appointment",    tone: "warn" },
  status:      { label: "status change",  tone: "" },
};


function renderChangeBlock(ch) {
  const block = document.createElement("div");
  block.className = "change-block";
  const info = KIND_INFO[ch.kind] || KIND_INFO.status;
  const kindTag =
    `<span class="kind-tag kind-${info.tone || "n"}" ` +
    `title="${escapeHtml(info.desc || info.label)}">${escapeHtml(info.label)}</span>`;
  block.innerHTML =
    `<div class="change-block-head">` +
      // Show only the detection time — the `to` capturedAt, i.e. the pull that
      // first saw this change. The `from` side (the prior pull's time) is not
      // meaningful to the reader; what matters is when the change was detected.
      `<span class="change-range">Diff Detected: ${escapeHtml(formatLocalDateTime(ch.to))}</span>` +
      kindTag +
    `</div>`;

  if (ch.scalars && Object.keys(ch.scalars).length) {
    const sec = document.createElement("div");
    sec.className = "change-section";
    sec.innerHTML = `<h5>Field changes</h5>`;
    for (const [k, v] of Object.entries(ch.scalars)) {
      const row = document.createElement("div");
      row.className = "change-scalar";
      // Mask the before/after values when the changed field is PII.
      const mask = state.redacted && REDACT_KEYS.has(k);
      const fromHtml = mask ? escapeHtml(REDACTION_MASK) : formatScalarValueHtml(v.from);
      const toHtml = mask ? escapeHtml(REDACTION_MASK) : formatScalarValueHtml(v.to);
      const valClass = mask ? " redacted-text" : "";
      row.innerHTML =
        `<span class="field">${escapeHtml(k)}</span>` +
        `<span class="from${valClass}">${fromHtml}</span>` +
        `<span class="to${valClass}">${toHtml}</span>`;
      sec.appendChild(row);
    }
    block.appendChild(sec);
  }

  // Named collections rendered explicitly; any other list-of-dicts field the
  // engine diffed lands under ch.collections and is rendered generically below.
  const collections = [
    ["events", "Events"],
    ["notices", "Notices"],
    ["documents", "Documents"],
    ["addendums", "Addendums"],
    ["evidenceRequests", "Evidence requests"],
  ];
  for (const [key, title] of collections) {
    const c = ch[key] || {};
    if (!(c.added?.length || c.removed?.length || c.changed?.length)) continue;
    const sec = document.createElement("div");
    sec.className = "change-section";
    sec.innerHTML = `<h5>${title}</h5>`;
    // events/notices have a compact one-line describer; evidenceRequests and
    // documents/addendums are richer objects, so render them as pretty
    // key/value rows instead of a JSON clump.
    const pretty = key === "evidenceRequests";
    for (const a of c.added || []) {
      sec.appendChild(pretty
        ? renderDictItem(a, "added")
        : chipItem("change-item-added", "+ " + redactMaybe(describeItem(key, a))));
    }
    for (const r of c.removed || []) {
      sec.appendChild(pretty
        ? renderDictItem(r, "removed")
        : chipItem("change-item-removed", "− " + redactMaybe(describeItem(key, r))));
    }
    for (const ce of c.changed || []) {
      sec.appendChild(renderChangedItem(key, ce));
    }
    block.appendChild(sec);
  }

  // Generic bucket: any other list-of-dicts field (e.g. concurrentCases) the
  // comprehensive diff caught. Keyed by field name. Added/removed entries are
  // arbitrary objects, so always render them pretty (key/value rows).
  const extra = ch.collections || {};
  for (const key of Object.keys(extra)) {
    const c = extra[key] || {};
    if (!(c.added?.length || c.removed?.length || c.changed?.length)) continue;
    const sec = document.createElement("div");
    sec.className = "change-section";
    sec.innerHTML = `<h5>${escapeHtml(key)}</h5>`;
    for (const a of c.added || []) {
      sec.appendChild(renderDictItem(a, "added"));
    }
    for (const r of c.removed || []) {
      sec.appendChild(renderDictItem(r, "removed"));
    }
    for (const ce of c.changed || []) {
      sec.appendChild(renderChangedItem(key, ce));
    }
    block.appendChild(sec);
  }
  return block;
}

// A simple text chip (used for the compact event/notice describers).
function chipItem(cls, html) {
  const chip = document.createElement("span");
  chip.className = cls;
  chip.innerHTML = html;
  return chip;
}

// Pretty-render a whole added/removed collection entry as key/value rows
// instead of a one-line JSON clump. `mode` is "added" | "removed" and drives
// the +/− marker and colour. Nested objects/arrays are shown as indented
// JSON so a property that is itself a JSON blob stays readable.
function renderDictItem(obj, mode) {
  const wrap = document.createElement("div");
  wrap.className = mode === "removed" ? "change-item-removed-block" : "change-item-added-block";
  const marker = mode === "removed" ? "−" : "+";
  const head = document.createElement("div");
  head.className = "dict-item-head";
  head.textContent = marker;
  wrap.appendChild(head);
  if (obj === null || typeof obj !== "object") {
    const row = document.createElement("div");
    row.className = "change-scalar";
    row.innerHTML = `<span class="to">${redactMaybe(escapeHtml(String(obj)))}</span>`;
    wrap.appendChild(row);
    return wrap;
  }
  for (const [k, v] of Object.entries(obj)) {
    if (k === "_delta") continue;
    const mask = state.redacted && REDACT_KEYS.has(k);
    let valHtml;
    if (mask) {
      valHtml = escapeHtml(REDACTION_MASK);
    } else if (v !== null && typeof v === "object") {
      // Nested object/array — pretty-print indented so it stays readable.
      valHtml = `<pre class="dict-nested">${escapeHtml(JSON.stringify(v, null, 2))}</pre>`;
    } else {
      valHtml = formatScalarValueHtml(v);
    }
    const row = document.createElement("div");
    row.className = "change-scalar";
    row.innerHTML =
      `<span class="field">${escapeHtml(k)}</span>` +
      `<span class="to${mask ? " redacted-text" : ""}">${valHtml}</span>`;
    wrap.appendChild(row);
  }
  return wrap;
}

// Render an in-place changed collection entry: show the field-level delta so
// an operator sees exactly which properties flipped (e.g. an RFE's
// isRespondedTo True→False). No header line — just the field deltas, styled
// like the scalar "Field changes" rows.
function renderChangedItem(kind, entry) {
  const wrap = document.createElement("div");
  wrap.className = "change-item-changed";
  const delta = entry._delta || {};
  for (const [k, v] of Object.entries(delta)) {
    if (k === "_delta") continue;
    const mask = state.redacted && REDACT_KEYS.has(k);
    const fromHtml = mask ? escapeHtml(REDACTION_MASK) : formatScalarValueHtml(v.from);
    const toHtml = mask ? escapeHtml(REDACTION_MASK) : formatScalarValueHtml(v.to);
    const row = document.createElement("div");
    row.className = "change-scalar";
    row.innerHTML =
      `<span class="field">${escapeHtml(k)}</span>` +
      `<span class="from">${fromHtml}</span>` +
      `<span class="to">${toHtml}</span>`;
    wrap.appendChild(row);
  }
  return wrap;
}

function describeItem(kind, obj) {
  if (kind === "events") {
    const code = obj.eventCode || "?";
    const caption = state.eventCodeLabels[code];
    const bestTimestamp = obj.createdAtTimestamp || obj.createdAt || obj.eventDateTime;
    const whenFormatted = bestTimestamp || "—";
    const whenHover = bestTimestamp && String(bestTimestamp).includes("T") ? ` data-tooltip="${escapeHtml(formatLocalDateTime(bestTimestamp))}"` : "";
    const whenClass = bestTimestamp && String(bestTimestamp).includes("T") ? ` class="utc-ts"` : "";
    const whenStr = `<span${whenClass}${whenHover}>${escapeHtml(whenFormatted)}</span>`;
    return caption ? `${code} (${caption}) @ ${whenStr}` : `${code} @ ${whenStr}`;
  }
  if (kind === "notices") {
    const appt = obj.appointmentDateTime
      ? ` (appt ${formatLocalDateTime(obj.appointmentDateTime)})`
      : "";
    const letter = state.redacted ? REDACTION_MASK : (obj.letterId || "?");
    return `${obj.actionType || "?"} — letter ${letter}${appt}`;
  }
  return JSON.stringify(obj);
}

// ---------- updates feed ----------

// ---------- system log view ----------

// Well-known events and their visual tone. Any event not listed falls
// through to "info" (or "error"/"warning" if the entry carries that level).
// Well-known events and their visual tone.
const SYSTEMLOG_EVENT_INFO = {
  // Top-level envelope + server lifecycle
  pull:                        { tone: "info",  label: "Pull" },
  server_startup:              { tone: "info",  label: "Server started" },
  scheduler_configured:        { tone: "info",  label: "Scheduler configured" },
  pull_skipped_already_running:{ tone: "warn",  label: "Pull skipped (already running)" },
  system_log_cleared:          { tone: "info",  label: "System log cleared",
    summarize: e => {
      const n = Number(e.prior_entry_count);
      if (!Number.isFinite(n)) return null;
      return n === 0
        ? "log was already empty"
        : `${n} ${n === 1 ? "entry" : "entries"} cleared`;
    } },

  // Subprocess lifecycle wrapping the pull
  subprocess_exit_nonzero:     { tone: "bad",   label: "Subprocess exit non-zero" },
  subprocess_timeout:          { tone: "bad",   label: "Subprocess timeout" },
  subprocess_crashed:          { tone: "bad",   label: "Subprocess crashed" },

  // Notifications (step-only after the log-consolidation refactor)
  notify_sent:                 { tone: "ok",    label: "Notification sent" },
  notify_skipped:              { tone: "warn",  label: "Notification skipped" },
  notify_failed:               { tone: "bad",   label: "Notification failed" },
  notify_dispatcher_crashed:   { tone: "bad",   label: "Notification dispatcher crashed" },

  // session-fetch CLI lifecycle
  cli_run_start:               { tone: "info",  label: "CLI run start" },
  cli_run_finished:            { tone: "ok",    label: "CLI run finished" },
  cli_run_no_cases:            { tone: "bad",   label: "CLI run — no cases configured" },
  cli_run_session_expired_retry: { tone: "warn", label: "Session expired — retrying" },
  cli_run_session_expired_twice: { tone: "bad", label: "Session expired twice — giving up" },
  cli_uncaught_exception:      { tone: "bad",   label: "CLI uncaught exception" },

  // Case-API snapshot lifecycle events
  case_fetch_start:            { tone: "info",  label: "Case fetch start" },
  case_fetch_api_error:        { tone: "bad",   label: "Case fetch API error" },
  case_fetch_session_expired:  { tone: "warn",  label: "Case fetch session expired" },
  case_snapshot_appended:      { tone: "ok",    label: "Case snapshot appended" },
  case_snapshot_append_failed: { tone: "bad",   label: "Case snapshot append failed" },
  post_fetch_rewarm_failed:    { tone: "warn",  label: "Post-fetch dashboard rewarm failed" },

  // Generic, used by the case snapshot path via _append_to_log_file
  snapshot_log_not_array:      { tone: "warn",  label: "Snapshot log wasn't an array" },
  snapshot_log_invalid_json:   { tone: "warn",  label: "Snapshot log was malformed JSON" },

  // Comprehensive auth-phase events (added 2026-04-24)
  auth_ensure_started:         { tone: "info",  label: "Auth — ensure started" },
  auth_ensure_result:          { tone: "info",  label: "Auth — ensure result" },
  auth_goto_login_result:      { tone: "info",  label: "Auth — navigated to login" },
  auth_email_form_result:      { tone: "info",  label: "Auth — email form ready" },
  auth_credentials_filled:     { tone: "info",  label: "Auth — credentials filled" },
  auth_submit_result:          { tone: "info",  label: "Auth — submit result" },
  auth_http_response:          { tone: "info",  label: "Auth — HTTP response" },
  auth_landing_result:         { tone: "info",  label: "Auth — landing result" },
  auth_bridge_result:          { tone: "info",  label: "Auth — bridge nav result" },
  trace_saved:                 { tone: "info",  label: "Trace saved",
    summarize: e => {
      const bits = [];
      if (e.dir) bits.push(e.dir);
      if (e.has_mfa_trace) {
        const events = Number(e.mfa_event_count) || 0;
        const emails = Number(e.mfa_email_count) || 0;
        bits.push(`MFA: ${events} event${events === 1 ? "" : "s"}, ${emails} email${emails === 1 ? "" : "s"}`);
      }
      return bits.length ? bits.join(" · ") : null;
    } },
  tracing_start_failed:        { tone: "warn",  label: "Tracing — start failed" },
  tracing_stop_failed:         { tone: "warn",  label: "Tracing — stop failed" },
  auth_retry_waiting:          { tone: "warn",  label: "Auth — waiting before retry" },
  auth_retry_starting:         { tone: "warn",  label: "Auth — starting retry attempt" },
  auth_mfa_submit_did_not_advance: { tone: "bad", label: "MFA submit did not advance" },
  login_storage_cleared:       { tone: "info",  label: "Login — session state wiped" },
  login_started:               { tone: "info",  label: "Login — started" },
  login_mfa_result:            { tone: "info",  label: "Login — MFA result" },
  login_result:                { tone: "info",  label: "Login — final result" },
  login_bridge_warning:        { tone: "warn",  label: "Login — bridge warning" },
  login_landing_timeout:       { tone: "warn",  label: "Login — landing timeout" },
  probe_session_result:        { tone: "info",  label: "Probe — session result" },
  mfa_fetch_started:           { tone: "info",  label: "MFA — fetch started" },
  mfa_fetch_succeeded:         { tone: "ok",    label: "MFA — code received" },
  mfa_fetch_timeout:           { tone: "bad",   label: "MFA — fetch timeout" },
  mfa_fetch_cycle_error:       { tone: "warn",  label: "MFA — IMAP cycle error" },

  // Widened case-fetch net
  case_fetch_unexpected_error: { tone: "bad",   label: "Case fetch — unexpected error" },

  // Config + debug mode
  pull_config_error:           { tone: "bad",   label: "Pull config error" },

  // Manual / startup diff recompute. The raw `cases` array is suppressed
  // from the kv detail dump (hideKeys) and rendered instead as a compact
  // per-case breakdown below the header (renderContent), with a one-line
  // roll-up next to the label (summarize).
  diff_recomputed:             { tone: "info",  label: "Diff recomputed",
    hideKeys: ["cases"],
    summarize: e => {
      if (!Array.isArray(e.cases) || !e.cases.length) {
        return e.error ? "recompute failed" : "no cases configured";
      }
      const n = e.cases.length;
      const total = e.cases.reduce(
        (s, c) => s + (Number(c.case_changes) || 0), 0);
      return `${n} case${n === 1 ? "" : "s"} · ${total} update${total === 1 ? "" : "s"}`;
    },
    renderContent: e => {
      if (!Array.isArray(e.cases) || !e.cases.length) return "";
      const rows = e.cases.map(c => {
        const total = Number(c.case_changes) || 0;
        return `<div class="diffrc-row">` +
          `<span class="diffrc-label">${escapeHtml(c.label ?? "?")}</span>` +
          `<span class="diffrc-metric${total ? "" : " is-zero"}">` +
            `<span class="diffrc-num">${total}</span>` +
            `<span class="diffrc-unit">update${total === 1 ? "" : "s"}</span>` +
          `</span>` +
        `</div>`;
      }).join("");
      return `<div class="diffrc-table">${rows}</div>`;
    } },
};

function _eventInfo(entry) {
  const known = SYSTEMLOG_EVENT_INFO[entry.event];
  if (known) return known;
  // Fallback: humanize `snake_case_event_names` into "Snake case event names"
  // so unknown events still read as a category in the pill instead of a
  // debug-style identifier. The SYSTEMLOG_EVENT_INFO table is a curated
  // override for events we want to wordsmith; this default keeps new ones
  // legible without having to be cataloged first.
  const tone = _toneForLevel(entry.level);
  const raw = entry.event || "?";
  const label = raw
    .replace(/_/g, " ")
    .replace(/^./, c => c.toUpperCase());
  return { tone, label };
}

function renderSystemLog() {
  // The feed's parent contains TWO sections: the storage bar
  // (static HTML in the template) and this dynamically-rendered
  // log content. We only wipe / refill the log content — leaving
  // the bar untouched so it isn't re-painted on every page flip.
  const root = document.getElementById("systemlog-content");
  if (!root) return;
  root.innerHTML = "";

  if (!state.systemLogTotal) {
    const empty = document.createElement("div");
    empty.className = "updates-empty";
    empty.innerHTML =
      `<h3>System log is empty.</h3>` +
      `<p>The system log records what the tracker did (and when) — ` +
      `server startups, scheduler fires, pull lifecycle, case fetches, ` +
      `snapshot appends, email notifications. Events will appear here as ` +
      `the app runs.</p>`;
    root.appendChild(empty);
    return;
  }

  const perPage = state.systemLogPageSize;
  const total = state.systemLogTotal;
  const totalPages = Math.max(1, Math.ceil(total / perPage));
  const page = Math.min(Math.max(1, state.systemLogPage), totalPages);
  const shown = state.systemLog.length;
  // Compute the visible window in human 1-based terms: page 1 = newest,
  // so the window on page 1 is [total-shown+1 .. total] when sorted newest-first.
  const windowEnd = total - (page - 1) * perPage;         // highest index (newest in window)
  const windowStart = windowEnd - shown + 1;              // lowest index
  const countLine = totalPages > 1
    ? `Page ${page} of ${totalPages} · showing events ${windowStart}–${windowEnd} of ${total}`
    : `${total} event${total === 1 ? "" : "s"}`;

  // System log card header: title + subtitle. Title is rendered as
  // a DIRECT child of the .sys-card section so the collapse rule
  // (.sys-card.is-collapsed > *:not(.sys-card-title) { display:none })
  // can hide the body while keeping the title visible. The subtitle
  // is its own sibling and collapses with the rest.
  const title = document.createElement("h2");
  title.className = "sys-card-title";
  title.textContent = "System log";
  root.appendChild(title);
  // Re-wire collapse handlers since the title was freshly rendered
  // (the previous title node — if any — was wiped by root.innerHTML).
  _wireSysCardCollapse();

  const sub = document.createElement("div");
  sub.className = "updates-sub syslog-card-sub";
  sub.innerHTML =
    `<span class="syslog-storage-line" id="syslog-storage-line"></span>` +
    `${escapeHtml(countLine)} · ` +
    `Persisted to <code>data/system_log.json</code>. Newest first.`;
  root.appendChild(sub);

  // Mount Export log + Clear log into the Actions card (if present).
  // Render lazily here so the controls exist whenever the System log
  // is rendered, even though the Actions card is in the static HTML.
  const actionsMount = document.getElementById("syslog-controls-mount");
  if (actionsMount && !actionsMount.firstChild) {
    actionsMount.appendChild(renderSystemLogControls());
  }
  // Paint the size line with whatever the latest /api/storage poll
  // returned. If no poll has landed yet, a lightweight fire-and-forget
  // fetch triggers one.
  if (LAST_STORAGE_DATA) {
    renderSyslogStorageLine(LAST_STORAGE_DATA);
  } else {
    updateStorageBar();
  }

  // Pagination — rendered BOTH above and below the list so operators
  // don't have to scroll 100 rows to flip pages.
  if (totalPages > 1) {
    root.appendChild(renderSystemLogPagination(page, totalPages, "top"));
  }

  // Newest first within the current page.
  const rowsNewestFirst = [...state.systemLog].reverse();
  for (const e of rowsNewestFirst) {
    root.appendChild(renderSystemLogRow(e));
  }

  if (totalPages > 1) {
    root.appendChild(renderSystemLogPagination(page, totalPages, "bottom"));
  }

  // Re-measure rows now that they're in the DOM. The ResizeObserver
  // wired in _wireSyslogFit() handles width changes, but a fresh
  // render replaces all .syslog-head nodes — so we trigger a check
  // immediately so the wrapped class lands on first paint, not after
  // the first resize event.
  _recheckSyslogFit();
}

function renderSystemLogPagination(page, totalPages, position) {
  const wrap = document.createElement("nav");
  wrap.className = `syslog-pagination syslog-pagination-${position}`;
  wrap.setAttribute("aria-label", "System log pagination");

  const mk = (label, targetPage, opts = {}) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `syslog-page-btn${opts.active ? " active" : ""}`;
    btn.textContent = label;
    btn.disabled = !!opts.disabled;
    if (opts.title) btn.title = opts.title;
    if (!opts.disabled && !opts.active) {
      btn.addEventListener("click", () => gotoSystemLogPage(targetPage));
    }
    return btn;
  };

  wrap.appendChild(mk("« First", 1, { disabled: page === 1, title: "First page (newest)" }));
  wrap.appendChild(mk("‹ Newer", page - 1, { disabled: page === 1 }));

  // Numbered page buttons — show a compact window around the current page
  // so very large logs (hundreds of pages) don't produce an unreadable bar.
  const windowSize = 5;
  let from = Math.max(1, page - Math.floor(windowSize / 2));
  const to = Math.min(totalPages, from + windowSize - 1);
  from = Math.max(1, to - windowSize + 1);
  if (from > 1) {
    wrap.appendChild(mk("1", 1));
    if (from > 2) {
      const ell = document.createElement("span");
      ell.className = "syslog-page-ellipsis";
      ell.textContent = "…";
      wrap.appendChild(ell);
    }
  }
  for (let n = from; n <= to; n++) {
    wrap.appendChild(mk(String(n), n, { active: n === page }));
  }
  if (to < totalPages) {
    if (to < totalPages - 1) {
      const ell = document.createElement("span");
      ell.className = "syslog-page-ellipsis";
      ell.textContent = "…";
      wrap.appendChild(ell);
    }
    wrap.appendChild(mk(String(totalPages), totalPages));
  }

  wrap.appendChild(mk("Older ›", page + 1, { disabled: page === totalPages }));
  wrap.appendChild(mk("Last »", totalPages, {
    disabled: page === totalPages, title: "Last page (oldest)",
  }));
  return wrap;
}

// Render the two System-log tab controls: "Export log" (one-click download
// of the current log as JSON) and "Clear log" (two-step destructive wipe).
// Both are deliberately scoped to the System log view so they can't be
// confused with the "Export data" button in the topbar (which exports
// cases only — not the log).
function renderSystemLogControls() {
  // Wrap as `display: contents` so the buttons sit as direct
  // siblings inside the parent `.sys-actions-row` flex container
  // (matching DEBUG + Export data placement).
  const wrap = document.createElement("span");
  wrap.className = "syslog-controls";
  wrap.style.display = "contents";

  const exportBtn = document.createElement("a");
  exportBtn.href = "/api/system-log/export";
  exportBtn.dataset.guard = "redaction";
  // .action-btn for unified action-button geometry; the .syslog-export-btn
  // class is preserved for any specialised rules (none currently).
  exportBtn.className = "action-btn syslog-export-btn";
  exportBtn.textContent = "Export log";
  exportBtn.title = "Download this log as JSON";
  exportBtn.addEventListener("click", (e) => guardedDownload(e, "/api/system-log/export"));

  wrap.appendChild(exportBtn);
  wrap.appendChild(renderClearLogControl());
  return wrap;
}

// Clear log: password prompt first (while redaction is latched), then a
// "Clear system log?" confirm dialog, then POST /api/system-log/clear.
function renderClearLogControl() {
  // Sit as a direct sibling in the parent flex row (no wrapper) so
  // it lines up with DEBUG / Export data / Export log.
  const idle = document.createElement("button");
  idle.type = "button";
  idle.className = "action-btn clear-log-btn";
  idle.dataset.guard = "redaction";
  idle.textContent = "Clear log";
  idle.title = "Permanently delete every event in this log";
  idle.addEventListener("click", () => requestClearLog(idle));
  return idle;
}

// Challenge for the password before the warning, so it takes priority.
async function requestClearLog(btn) {
  const pw = await adminChallenge({ action: btnLabel(btn) });
  if (pw === null) return;
  openClearLogDialog(pw);
}

function openClearLogDialog(pw = "") {
  // Prevent stacking multiple dialogs on fast double-clicks.
  if (document.querySelector(".modal-overlay[data-modal='clear-log']")) return;

  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";
  overlay.dataset.modal = "clear-log";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-labelledby", "clear-log-title");

  overlay.innerHTML =
    `<div class="modal-card modal-card-danger">` +
      `<h3 id="clear-log-title" class="modal-title">Clear system log?</h3>` +
      `<div class="modal-body">` +
        `<p><strong>This is irreversible.</strong> Clearing wipes:</p>` +
        `<ul class="modal-list">` +
          `<li>Every entry in <code>data/system_log.json</code></li>` +
          `<li>Every preserved pull trace under <code>data/full_traces/</code> ` +
              `— each one contains a Playwright <code>trace.zip</code> ` +
              `(DOM + network + screenshots) plus the <code>mfa_trace/</code> ` +
              `sidecar (<code>events.jsonl</code> + archived ` +
              `<code>.eml</code>s)</li>` +
        `</ul>` +
        `<p>The log is the only record of scheduler fires, pull failures, ` +
        `and notification history. Traces are the only forensic evidence ` +
        `of what USCIS and Gmail returned on each pull.</p>` +
        `<p class="modal-hint">If you might need the log later, click ` +
        `<em>Export log</em> first.</p>` +
      `</div>` +
      `<div class="modal-actions">` +
        `<button type="button" class="modal-btn modal-btn-cancel">Cancel</button>` +
        `<button type="button" class="modal-btn modal-btn-danger">Yes, delete everything</button>` +
      `</div>` +
    `</div>`;

  const close = () => {
    document.removeEventListener("keydown", onKey);
    overlay.remove();
  };
  const onKey = (e) => { if (e.key === "Escape") close(); };
  document.addEventListener("keydown", onKey);

  overlay.addEventListener("click", (e) => {
    // Click on the backdrop (not inside the card) closes.
    if (e.target === overlay) close();
  });
  overlay.querySelector(".modal-btn-cancel").addEventListener("click", close);

  const confirmBtn = overlay.querySelector(".modal-btn-danger");
  confirmBtn.addEventListener("click", async () => {
    confirmBtn.disabled = true;
    confirmBtn.textContent = "Clearing…";
    try {
      const res = await fetch("/api/system-log/clear", withAdminHeader({
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      }, pw));
      if (res.status === 401) { toast("Wrong password — log not cleared.", "bad"); throw new Error("admin_required"); }
      if (!res.ok) throw new Error(`status ${res.status}`);
      // Combined toast: "Cleared N entries + M trace files."
      const body = await res.json().catch(() => ({}));
      const n = Number(body.priorEntryCount) || 0;
      const traces = Number(body.tracesRemoved) || 0;
      const parts = [];
      if (n > 0) parts.push(`${n} ${n === 1 ? "entry" : "entries"}`);
      if (traces > 0) parts.push(`${traces} trace file${traces === 1 ? "" : "s"}`);
      const msg = parts.length
        ? `Cleared ${parts.join(" + ")}.`
        : "Log was already empty — nothing to clear.";
      toast(msg);
      await loadSystemLog();
      renderSystemLog();
      // Clear also wiped the full_traces directory on disk, which
      // the storage bar tracks. Kick an immediate re-fetch so the
      // bar shrinks right away instead of waiting for the 30s poll.
      updateStorageBar();
      close();
    } catch (e) {
      console.warn("clear log failed:", e);
      confirmBtn.disabled = false;
      confirmBtn.textContent = "Retry delete";
    }
  });

  document.body.appendChild(overlay);
  // Give the red-destructive button focus so Enter = confirm, but not
  // before the browser has painted (otherwise a trailing Enter keystroke
  // from the idle button can accidentally confirm).
  requestAnimationFrame(() => confirmBtn.focus());
}

// Skeleton fields present on every entry. These are rendered via the
// header (not the kv detail list).
const _SYSLOG_SKELETON = new Set(["ts", "event", "level", "pid", "source"]);

// Envelope-only fields that belong in the collapsed top row of a nested
// entry (pull, etc.), not repeated in the kv detail block.
const _SYSLOG_ENVELOPE_EXTRA = new Set([
  "trigger", "duration_seconds", "exit_code", "timed_out",
  "started_at", "finished_at", "summary", "steps",
]);

function renderSystemLogRow(entry) {
  // Nested envelopes (currently: `pull`) have a `steps` array. These get
  // a disclosure triangle and an indented sub-log below.  Flat entries
  // fall through to the original single-row layout.
  if (Array.isArray(entry.steps) && entry.steps.length > 0) {
    return _renderNestedSystemLogRow(entry);
  }
  return _renderFlatSystemLogRow(entry);
}

// The raw event id (e.g. `system_log_cleared`) shown in the header's
// third column is pure duplication when the pill label is just its
// humanized form ("System log cleared"). In that case return "" so the
// row shows the label once; callers keep an empty span so the grid
// columns stay aligned. A curated label that differs from the event
// (e.g. "MFA — code received" for `mfa_fetch_succeeded`) is preserved.
function _syslogEventId(info, entry) {
  const ev = entry.event || "";
  const evNorm = ev.replace(/_/g, " ").trim().toLowerCase();
  const labelNorm = (info.label || "").trim().toLowerCase();
  return evNorm && evNorm === labelNorm ? "" : ev;
}

function _renderFlatSystemLogRow(entry) {
  const info = _eventInfo(entry);
  const block = document.createElement("div");
  block.className = `change-block syslog-block syslog-${info.tone}`;

  // Events may opt specific keys out of the raw kv detail dump (e.g.
  // diff_recomputed hides its `cases` array, rendering it as a styled
  // breakdown via renderContent instead of a JSON blob).
  const hideKeys = Array.isArray(info.hideKeys) ? new Set(info.hideKeys) : null;
  const detailKeys = Object.keys(entry).filter(
    k => !_SYSLOG_SKELETON.has(k) && !(hideKeys && hideKeys.has(k)));
  const when = formatLocalDateTime(new Date(entry.ts), { withSeconds: true });
  const sourceTag = entry.source
    ? `<span class="syslog-source">${escapeHtml(entry.source)}</span>`
    : "";

  let detailsHtml = "";
  if (detailKeys.length) {
    detailsHtml = `<div class="syslog-details">` +
      detailKeys.map(k => _detailKvHtml(k, entry[k])).join("") +
      `</div>`;
  }

  // Optional one-line summary next to the label. Used e.g. for
  // system_log_cleared to show "5 entries cleared" inline.
  const summary = typeof info.summarize === "function"
    ? info.summarize(entry)
    : null;
  const summaryHtml = summary
    ? `<span class="syslog-summary">${escapeHtml(summary)}</span>`
    : "";

  // Optional rich content band — structured HTML the event renders
  // itself (e.g. diff_recomputed's per-case breakdown). Returns a
  // string; trusted to escape its own interpolated values.
  const richHtml = typeof info.renderContent === "function"
    ? (info.renderContent(entry) || "")
    : "";

  // Header is a 5-column grid so every row aligns:
  //   [disc-spacer] [pill] [source] [event] [ts-right]
  // Summary (when present — e.g. "343 entries cleared" on
  // system_log_cleared) lives in a content band BELOW the header so
  // it can't disturb column alignment.
  const contentHtml = (summaryHtml || richHtml)
    ? `<div class="syslog-envelope-content">${summaryHtml}${richHtml}</div>`
    : "";
  block.innerHTML =
    `<div class="syslog-head">` +
      `<span class="kind-tag kind-${info.tone}">${escapeHtml(info.label)}</span>` +
      sourceTag +
      `<span class="syslog-event">${escapeHtml(_syslogEventId(info, entry))}</span>` +
      `<span class="syslog-ts">${escapeHtml(when)}</span>` +
    `</div>` +
    contentHtml +
    detailsHtml;

  return block;
}

function _renderNestedSystemLogRow(entry) {
  // Trust the envelope's `level` field directly — server-side
  // computes it with the authoritative three-tier rule (error / warning
  // / info, see _run_pull_subprocess_inner in server.py). Previously
  // we re-aggregated `_worstLevelAcross([entry, ...steps])` on the
  // client, which ignored the server's downgrade of "recovered retry"
  // from error → warning (an error-level step inside a zero-exit pull
  // would force the pill red). That made the three-tier rule
  // unreachable — any env with error steps tipped to red regardless of
  // operation outcome. Client just renders what the server decided.
  const topLevel = entry.level || "info";
  // A pull that completed CLEANLY — no errors, no warnings, exit 0, not
  // timed out — gets the green "ok" tone so the operator can distinguish
  // "the pull happened and everything worked" from a merely informational
  // row like `server_startup`. Anything with a warn/error step, a non-zero
  // exit, or a timeout falls through the standard severity map.
  const isCleanCompletedPull = (
    entry.event === "pull"
    && topLevel === "info"
    && (entry.exit_code === 0)
    && !entry.timed_out
  );
  const tone = isCleanCompletedPull ? "ok" : _toneForLevel(topLevel);
  // The pill's label is the event's descriptive name ("Pull"), not the
  // raw level ("info"). The tone conveys severity; the label conveys
  // category. This keeps the pill consistent with flat rows, which also
  // show a descriptive label (e.g. "Server started").
  const eventInfo = _eventInfo(entry);
  const info = { tone, label: eventInfo.label || entry.event };

  const block = document.createElement("div");
  block.className = `change-block syslog-block syslog-${tone} syslog-nested`;

  const when = formatLocalDateTime(new Date(entry.ts), { withSeconds: true });
  const sourceTag = entry.source
    ? `<span class="syslog-source">${escapeHtml(entry.source)}</span>`
    : "";

  // A compact summary strip so the collapsed row answers "did this pull
  // succeed, how long did it take, did anything new happen?" at a glance.
  const summary = entry.summary || {};
  const bits = [];
  if (entry.trigger) bits.push(entry.trigger);
  if (typeof entry.duration_seconds === "number")
    bits.push(`${entry.duration_seconds}s`);
  if (typeof summary.case_snapshots === "number")
    bits.push(`${summary.case_snapshots} case`);
  if (summary.new_diffs_emailed)
    bits.push(`${summary.new_diffs_emailed} email${summary.new_diffs_emailed === 1 ? "" : "s"}`);
  if (summary.case_fetch_failures)
    bits.push(`${summary.case_fetch_failures} case-fail`);
  if (summary.notify_failures)
    bits.push(`${summary.notify_failures} email-fail`);
  if (entry.timed_out) bits.push("timed out");
  if (entry.exit_code && entry.exit_code !== 0) bits.push(`exit ${entry.exit_code}`);
  bits.push(`${entry.steps.length} step${entry.steps.length === 1 ? "" : "s"}`);

  const summaryLine = bits.length
    ? `<span class="syslog-summary">${escapeHtml(bits.join(" · "))}</span>`
    : "";

  // Envelope-level fields the summary didn't already surface (e.g. started_at,
  // finished_at) render as a normal kv strip above the steps.
  const envelopeKvKeys = Object.keys(entry).filter(k =>
    !_SYSLOG_SKELETON.has(k) && !_SYSLOG_ENVELOPE_EXTRA.has(k),
  );
  const envelopeKvHtml = envelopeKvKeys.length
    ? `<div class="syslog-details syslog-envelope-kv">` +
      envelopeKvKeys.map(k => _detailKvHtml(k, entry[k])).join("") +
      `</div>`
    : "";

  // Detect persisted trace(s) via the `trace_saved` step(s) the
  // subprocess emits after writing trace.zip + mfa_trace/. There can
  // be MORE than one when the pull retried — each attempt that saved
  // its trace has its own step with its own `dir`. We must render a
  // button pair per trace so the operator can open each attempt's
  // zip and MFA events independently.
  const tracesSaved = (entry.steps || []).filter(
    s => s && s.event === "trace_saved" && s.dir
  );

  const disclosureId = `syslog-steps-${Math.random().toString(36).slice(2, 8)}`;

  // Summary + trace buttons are moved OUT of the header and into a
  // content band between the header and the kv strip. Keeps the
  // header row a uniform 5-column grid (pill | source | event | ts)
  // so every envelope aligns identically regardless of how much
  // per-attempt summary text there is.
  const traceButtonsHtml = tracesSaved.length
    ? _renderTraceButtonRows(tracesSaved)
    : "";
  const contentHtml = (summaryLine || traceButtonsHtml)
    ? `<div class="syslog-envelope-content">` +
        (summaryLine || "") +
        traceButtonsHtml +
      `</div>`
    : "";

  // The disclosure chevron is rendered INLINE with the `attempts`
  // kv row, so the row reads `▶ attempts 2`. Click target is the
  // chevron itself; the rest of the kv stays as-is.
  const renderEnvelopeKvRow = (k) => {
    const v = entry[k];
    if (k === "attempts") {
      // Replace the row's outer <div> opener with a flex variant
      // and inject the chevron as the first child so the chevron,
      // key, and value sit on a single horizontal line.
      const inner = _detailKvHtml(k, v);
      const chevron =
        `<button type="button" class="syslog-disclosure" ` +
        `aria-label="Expand steps" aria-controls="${disclosureId}">▶</button>`;
      return inner.replace(
        '<div class="syslog-detail">',
        `<div class="syslog-detail syslog-detail-with-disclosure">${chevron}`,
      );
    }
    return _detailKvHtml(k, v);
  };
  const envelopeKvWithDisclosureHtml = envelopeKvKeys.length
    ? `<div class="syslog-details syslog-envelope-kv">` +
        envelopeKvKeys.map(renderEnvelopeKvRow).join("") +
      `</div>`
    : `<div class="syslog-details syslog-envelope-kv">` +
        `<div class="syslog-detail syslog-detail-with-disclosure">` +
          `<button type="button" class="syslog-disclosure" ` +
            `aria-label="Expand steps" aria-controls="${disclosureId}">▶</button>` +
          `<span class="syslog-detail-k">steps</span>` +
          `<span class="syslog-detail-v">${entry.steps?.length || 0}</span>` +
        `</div>` +
      `</div>`;

  block.innerHTML =
    `<button type="button" class="syslog-head syslog-head-expandable"` +
        ` aria-expanded="false" aria-controls="${disclosureId}">` +
      `<span class="kind-tag kind-${tone}">${escapeHtml(info.label)}</span>` +
      sourceTag +
      `<span class="syslog-event syslog-event-envelope">${escapeHtml(_syslogEventId(info, entry))}</span>` +
      `<span class="syslog-ts">${escapeHtml(when)}</span>` +
    `</button>` +
    contentHtml +
    envelopeKvWithDisclosureHtml +
    `<div class="syslog-steps" id="${disclosureId}" hidden></div>`;

  const stepsContainer = block.querySelector(".syslog-steps");
  const headBtn = block.querySelector(".syslog-head-expandable");
  const triangle = block.querySelector(".syslog-disclosure");

  // Redaction: a pull's steps carry the most PII (auth / MFA / case-fetch
  // events). Lock the row shut — never render the steps into the DOM at all,
  // and disable the expand controls so they can't be revealed. The server
  // also redacts step values, but not rendering them is belt-and-braces.
  if (state.redacted) {
    block.classList.add("syslog-nested-locked");
    headBtn.setAttribute("aria-disabled", "true");
    headBtn.setAttribute("title", "Steps hidden while redaction is on");
    if (triangle) {
      triangle.textContent = "🔒";
      triangle.disabled = true;
      triangle.classList.add("is-disabled");
      triangle.setAttribute("aria-label", "Steps hidden while redaction is on");
    }
    return block;
  }

  // Populate the expandable body lazily — cheap, but keeps the first
  // paint light for pages with many nested rows.
  for (const step of entry.steps) {
    stepsContainer.appendChild(_renderNestedStepRow(step));
  }

  // Wire the disclosure toggle. The chevron is now a button at the
  // right edge of the envelope-kv strip; clicking it (or anywhere on
  // the header row) toggles the steps panel below.
  const toggle = () => {
    const isOpen = !stepsContainer.hidden;
    stepsContainer.hidden = isOpen;
    headBtn.setAttribute("aria-expanded", String(!isOpen));
    triangle.textContent = isOpen ? "▶" : "▼";
  };
  headBtn.addEventListener("click", toggle);
  triangle.addEventListener("click", (e) => { e.stopPropagation(); toggle(); });

  return block;
}

function _renderNestedStepRow(step) {
  const info = _eventInfo(step);
  const row = document.createElement("div");
  row.className = `syslog-step syslog-${info.tone}`;

  const when = step.ts
    ? formatLocalDateTime(new Date(step.ts), { withSeconds: true })
    : "";
  const sourceTag = step.source
    ? `<span class="syslog-source">${escapeHtml(step.source)}</span>`
    : "";

  const detailKeys = Object.keys(step).filter(k => !_SYSLOG_SKELETON.has(k));
  const detailsHtml = detailKeys.length
    ? `<div class="syslog-details syslog-step-details">` +
      detailKeys.map(k => _detailKvHtml(k, step[k])).join("") +
      `</div>`
    : "";

  row.innerHTML =
    `<div class="syslog-step-head">` +
      `<span class="kind-tag kind-${info.tone}">${escapeHtml(info.label)}</span>` +
      sourceTag +
      (when ? `<span class="syslog-ts">${escapeHtml(when)}</span>` : "") +
      `<span class="syslog-event">${escapeHtml(step.event || "?")}</span>` +
    `</div>` +
    detailsHtml;
  return row;
}

// Single renderer for a kv pair — used by both flat rows and nested steps.
// When the key points to a persisted trace artefact (html_file, png_file,
// screenshot, trace_dir), render as a clickable link that opens the file
// via /api/full-trace/.
function _detailKvHtml(k, v) {
  const link = _traceLinkHref(k, v);
  if (link) {
    return (
      `<div class="syslog-detail">` +
        `<span class="syslog-detail-k">${escapeHtml(k)}</span>` +
        `<a class="syslog-detail-link" href="${escapeHtml(link.href)}" ` +
           `target="_blank" rel="noopener noreferrer" ` +
           `title="Open ${escapeHtml(link.kind)} in new tab">` +
          `${escapeHtml(link.label)}` +
        `</a>` +
      `</div>`
    );
  }
  // Redaction chokepoint for every system-log detail value: mask PII-keyed
  // values and scrub PII embedded in any string. No-op when redaction is off.
  v = redactDetailValue(k, v);
  const shown = typeof v === "object" && v !== null ? JSON.stringify(v) : String(v);
  return (
    `<div class="syslog-detail">` +
      `<span class="syslog-detail-k">${escapeHtml(k)}</span>` +
      `<span class="syslog-detail-v">${escapeHtml(shown)}</span>` +
    `</div>`
  );
}

// Render one row of trace buttons per preserved attempt. A pull that
// retried and preserved both attempts produces two rows; a single-
// attempt pull produces one row without the attempt label.
//
// Backend contract: each attempt's subprocess emits its own
// `trace_saved` step. The step's `attempt` field (added by server.py
// when folding subprocess events) tells us which attempt it belongs
// to, and `outcome` tells us whether that attempt was fail/ok — we
// surface both on the label so the operator can tell at a glance
// which button opens the failed trace vs the successful retry.
function _renderTraceButtonRows(steps) {
  // Sort by attempt number so "Attempt 1" is always above "Attempt 2".
  // Fall back to timestamp if the attempt field is missing.
  const sorted = [...steps].sort((a, b) => {
    const an = a.attempt ?? 999;
    const bn = b.attempt ?? 999;
    if (an !== bn) return an - bn;
    return (a.ts || "").localeCompare(b.ts || "");
  });
  const multi = sorted.length > 1;
  const rows = sorted.map(s => _renderTraceButtonRow(s, multi));
  return `<div class="trace-open-rows">${rows.join("")}</div>`;
}

function _renderTraceButtonRow(step, showAttemptLabel) {
  const dir = step.dir;
  if (!dir) return "";
  const traceUrl = `/api/full-trace/${encodeURIComponent(dir)}/trace.zip`;
  const viewerUrl =
    `/trace-viewer/index.html?trace=${encodeURIComponent(traceUrl)}`;
  const buttons = [
    `<a class="trace-open-btn" href="${escapeHtml(viewerUrl)}" ` +
        `target="_blank" rel="noopener noreferrer" ` +
        `title="Open full Playwright trace viewer (auto-loads the zip)">` +
      `Open trace` +
    `</a>`,
  ];
  if (step.has_mfa_trace) {
    buttons.push(
      `<button type="button" class="trace-open-btn trace-open-btn-sub" ` +
          `data-mfa-dir="${escapeHtml(dir)}" ` +
          `title="Show MFA wire-level events + archived emails">` +
        `MFA events` +
      `</button>`,
    );
  }
  const buttonGroup = `<span class="trace-open-group">${buttons.join("")}</span>`;
  if (!showAttemptLabel) {
    // Single-attempt pull: compact inline row, no attempt label / pill.
    return `<div class="trace-open-row trace-open-row-single">${buttonGroup}</div>`;
  }
  // Multi-attempt pull: attempt label + outcome pill in their own
  // grid columns so successive rows line up regardless of pill width.
  return (
    `<div class="trace-open-row trace-open-row-multi">` +
      `<span class="trace-attempt-label">Attempt ${escapeHtml(String(step.attempt ?? "?"))}</span>` +
      (step.outcome
        ? `<span class="trace-outcome trace-outcome-${escapeHtml(step.outcome)}">${escapeHtml(step.outcome)}</span>`
        : `<span class="trace-outcome"></span>`) +
      buttonGroup +
    `</div>`
  );
}

// Delegate click-handler for the "MFA events" buttons — opens the
// modal. Wired once at boot; new buttons added to the DOM on every
// re-render are picked up automatically.
function wireMfaModal() {
  document.addEventListener("click", e => {
    const btn = e.target.closest("[data-mfa-dir]");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    openMfaModal(btn.getAttribute("data-mfa-dir"));
  });
}

async function openMfaModal(dir) {
  closeMfaModal();
  const overlay = document.createElement("div");
  overlay.className = "mfa-modal-overlay";
  overlay.setAttribute("role", "dialog");
  overlay.setAttribute("aria-modal", "true");
  overlay.setAttribute("aria-label", "MFA events");
  overlay.innerHTML =
    `<div class="mfa-modal">` +
      `<header class="mfa-modal-header">` +
        `<h3>MFA trace — <code>${escapeHtml(dir)}</code></h3>` +
        `<div class="mfa-modal-tabs" role="tablist">` +
          `<button type="button" class="mfa-tab active" ` +
              `data-tab="events" role="tab">Events</button>` +
          `<button type="button" class="mfa-tab" ` +
              `data-tab="emails" role="tab">Emails</button>` +
        `</div>` +
        `<button type="button" class="mfa-modal-close" ` +
            `aria-label="Close">×</button>` +
      `</header>` +
      `<div class="mfa-modal-body">` +
        `<div class="mfa-modal-loading">Loading…</div>` +
      `</div>` +
    `</div>`;
  document.body.appendChild(overlay);

  overlay.addEventListener("click", e => {
    if (e.target === overlay) closeMfaModal();
  });
  overlay.querySelector(".mfa-modal-close")
    .addEventListener("click", closeMfaModal);
  const escHandler = (e) => { if (e.key === "Escape") closeMfaModal(); };
  document.addEventListener("keydown", escHandler);
  overlay._escHandler = escHandler;

  let data;
  try {
    const r = await fetch(
      `/api/mfa-trace/${encodeURIComponent(dir)}/summary`,
    );
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    data = await r.json();
  } catch (e) {
    const body = overlay.querySelector(".mfa-modal-body");
    body.innerHTML =
      `<div class="mfa-modal-error">Failed to load: ${escapeHtml(e.message)}</div>`;
    return;
  }

  const tabs = overlay.querySelectorAll(".mfa-tab");
  tabs.forEach(tab => {
    tab.addEventListener("click", () => {
      tabs.forEach(t => t.classList.toggle(
        "active", t === tab,
      ));
      _renderMfaTab(overlay, dir, tab.dataset.tab, data);
    });
  });
  _renderMfaTab(overlay, dir, "events", data);
}

function closeMfaModal() {
  const existing = document.querySelector(".mfa-modal-overlay");
  if (!existing) return;
  const h = existing._escHandler;
  existing.remove();
  if (h) document.removeEventListener("keydown", h);
}

function _renderMfaTab(overlay, dir, which, data) {
  const body = overlay.querySelector(".mfa-modal-body");
  body.innerHTML = "";
  if (which === "events") {
    body.appendChild(_renderMfaEventsTable(data.events || []));
  } else {
    body.appendChild(_renderMfaEmailsList(dir, data.emails || []));
  }
}

function _renderMfaEventsTable(events) {
  const wrap = document.createElement("div");
  wrap.className = "mfa-events-wrap";
  if (!events.length) {
    wrap.innerHTML = `<div class="mfa-empty">No events captured.</div>`;
    return wrap;
  }
  wrap.innerHTML =
    `<div class="mfa-events-sub">${events.length} event${events.length === 1 ? "" : "s"}` +
    ` across ${(new Set(events.map(e => e.cycle))).size} cycle${(new Set(events.map(e => e.cycle))).size === 1 ? "" : "s"}</div>` +
    `<table class="mfa-events-table">` +
      `<thead><tr>` +
        `<th>ts</th><th>cycle</th><th>event</th><th>details</th>` +
      `</tr></thead>` +
      `<tbody>` +
        events.map(e => {
          const skel = new Set(["ts", "cycle", "event"]);
          const details = Object.entries(e)
            .filter(([k]) => !skel.has(k))
            .map(([k, v]) => `<span class="mfa-kv"><b>${escapeHtml(k)}</b>` +
              `=<span>${escapeHtml(_shortJson(v))}</span></span>`)
            .join(" ");
          const toneClass = _mfaEventToneClass(e.event || "");
          return (
            `<tr class="${toneClass}">` +
              `<td class="mfa-ts">${escapeHtml((e.ts || "").slice(11, 23))}</td>` +
              `<td class="mfa-cycle">${escapeHtml(String(e.cycle ?? ""))}</td>` +
              `<td class="mfa-event">${escapeHtml(e.event || "?")}</td>` +
              `<td class="mfa-details">${details || ""}</td>` +
            `</tr>`
          );
        }).join("") +
      `</tbody>` +
    `</table>`;
  return wrap;
}

function _mfaEventToneClass(evName) {
  if (!evName) return "";
  if (evName.endsWith("_failed") || evName.includes("error")) return "mfa-row-error";
  if (evName === "code_accepted" || evName.endsWith("_ok")) return "mfa-row-ok";
  return "";
}

function _shortJson(v) {
  if (v == null) return "";
  if (typeof v === "object") {
    const j = JSON.stringify(v);
    return j.length > 120 ? j.slice(0, 117) + "…" : j;
  }
  const s = String(v);
  return s.length > 180 ? s.slice(0, 177) + "…" : s;
}

function _renderMfaEmailsList(dir, emails) {
  const wrap = document.createElement("div");
  wrap.className = "mfa-emails-wrap";
  if (!emails.length) {
    wrap.innerHTML = `<div class="mfa-empty">No emails archived.</div>`;
    return wrap;
  }
  wrap.innerHTML =
    `<div class="mfa-emails-sub">${emails.length} email${emails.length === 1 ? "" : "s"} archived</div>`;
  const list = document.createElement("div");
  list.className = "mfa-emails-list";
  for (const em of emails) {
    const card = document.createElement("details");
    card.className = "mfa-email-card";
    card.innerHTML =
      `<summary>` +
        `<span class="mfa-email-subject">${escapeHtml(em.subject || "(no subject)")}</span>` +
        `<span class="mfa-email-meta">` +
          `from ${escapeHtml(em.from || "?")} · ` +
          `${escapeHtml(em.date || "?")} · ` +
          `uid ${escapeHtml(em.uid)} · ` +
          `${formatBytes(em.size || 0)}` +
        `</span>` +
      `</summary>` +
      `<div class="mfa-email-body-placeholder">Click to load body…</div>`;
    // Lazy-load the body the first time the card is opened.
    card.addEventListener("toggle", async () => {
      if (!card.open) return;
      const placeholder = card.querySelector(".mfa-email-body-placeholder");
      if (!placeholder || placeholder.dataset.loaded === "1") return;
      placeholder.dataset.loaded = "1";
      placeholder.textContent = "Loading…";
      try {
        const r = await fetch(
          `/api/mfa-trace/${encodeURIComponent(dir)}` +
          `/email/${encodeURIComponent(em.uid)}`,
        );
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const body = await r.json();
        placeholder.innerHTML = _renderEmailBody(body);
        _wireEmailBody(placeholder);
      } catch (e) {
        placeholder.textContent = `Load failed: ${e.message}`;
      }
    });
    list.appendChild(card);
  }
  wrap.appendChild(list);
  return wrap;
}


// Render the body of one archived MFA email: headers strip + a
// sandboxed iframe showing the HTML part (so styles render as a
// mail client would), plus a toggle to view the raw RFC822 source
// for regex-anchor debugging.
function _renderEmailBody(body) {
  const headers = body.headers || {};
  const html = body.html || null;
  const raw = body.raw || "";

  // USCIS's MFA email is a 600px-fixed-width table centered in the
  // body — that's fine in a mail client, but in our full-width modal
  // it leaves the pop-out feeling cramped. Prepend an override style
  // that strips the hard width caps so the rendered content fills
  // whatever horizontal space our iframe has available. We keep the
  // override behind a high-specificity `html body` selector and use
  // !important so the inline USCIS <table width="600"> still yields.
  //
  // Sanitization is enforced via the iframe's sandbox="" attribute:
  // null origin, no scripts, no forms, no top-level navigation. The
  // iframe can't reach the dashboard's cookies or DOM.
  const overrideStyle =
    `<style>` +
      `html,body{margin:0!important;padding:12px 16px!important;` +
      `width:100%!important;max-width:100%!important;` +
      `box-sizing:border-box!important;font-family:system-ui,-apple-system,` +
      `"Segoe UI",sans-serif;}` +
      // body * disables every inline / attribute-driven hard width so
      // USCIS's 600px-wide table layout collapses to whatever space
      // the iframe has. min-width:0 is critical — without it nested
      // tables can refuse to shrink below their content width and
      // overflow horizontally on phones.
      `body *{max-width:100%!important;min-width:0!important;` +
      `box-sizing:border-box!important;}` +
      `table{width:100%!important;max-width:100%!important;` +
      `table-layout:auto!important;}` +
      `table[width]{width:100%!important;}` +
      `td[width],th[width]{width:auto!important;}` +
      `img{max-width:100%!important;height:auto!important;}` +
      // Word-break for long unbreakable strings (URLs, codes, etc.)
      // so they wrap instead of forcing horizontal scroll.
      `body{word-wrap:break-word!important;overflow-wrap:break-word!important;}` +
    `</style>`;
  const wrappedHtml = overrideStyle + (html || "");
  const encoded = wrappedHtml
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;");
  const iframeHtml = html
    ? `<iframe class="mfa-email-iframe" sandbox="" srcdoc="${encoded}" ` +
        `title="Rendered email body"></iframe>`
    : `<div class="mfa-email-no-html">No HTML part in this email.</div>`;
  const rawHtml =
    `<pre class="mfa-email-raw">${escapeHtml(raw || "(empty)")}</pre>`;

  return (
    `<div class="mfa-email-headers">` +
      Object.entries(headers).map(
        ([k, v]) => `<div><b>${escapeHtml(k)}:</b> ${escapeHtml(v || "")}</div>`,
      ).join("") +
    `</div>` +
    `<div class="mfa-email-view-switch" role="tablist">` +
      `<button type="button" class="mfa-email-view-btn active" ` +
          `data-view="rendered" role="tab">Rendered</button>` +
      `<button type="button" class="mfa-email-view-btn" ` +
          `data-view="raw" role="tab">Raw source</button>` +
    `</div>` +
    `<div class="mfa-email-view-wrap">` +
      `<div class="mfa-email-view-pane mfa-email-view-rendered active">${iframeHtml}</div>` +
      `<div class="mfa-email-view-pane mfa-email-view-raw">${rawHtml}</div>` +
    `</div>`
  );
}

function _wireEmailBody(root) {
  const buttons = root.querySelectorAll(".mfa-email-view-btn");
  const wrap = root.querySelector(".mfa-email-view-wrap");
  if (!wrap) return;
  // NOTE: we deliberately DON'T auto-size the iframe to content
  // height here. USCIS's MFA email is short — auto-sizing squeezes
  // the preview into ~200px and leaves the rest of the pop-out blank.
  // Instead the iframe gets a viewport-relative height from CSS
  // (see .mfa-email-iframe) so short emails render in a comfortable
  // canvas and long ones get their own internal scrollbar.
  buttons.forEach(btn => {
    btn.addEventListener("click", () => {
      buttons.forEach(b => b.classList.toggle("active", b === btn));
      const which = btn.dataset.view;
      wrap.querySelectorAll(".mfa-email-view-pane").forEach(p => {
        p.classList.toggle(
          "active", p.classList.contains(`mfa-email-view-${which}`),
        );
      });
    });
  });
}


// Map a step field into a {href, label, kind} for direct-open links.
// Returns null when the field isn't a linkable artefact. Currently
// handles only `trace_dir` — points at the dir's _meta.json, which
// indexes the phase list and is a reasonable "landing page" for a
// reader inspecting a trace from a failed pull.
function _traceLinkHref(key, value) {
  if (value == null) return null;
  const val = String(value);
  if (key === "trace_dir") {
    const tail = val.split("/").filter(Boolean).pop();
    if (!tail) return null;
    return {
      href: `/api/full-trace/${encodeURIComponent(tail)}/_meta.json`,
      label: `${tail}/_meta.json`,
      kind: "trace index",
    };
  }
  return null;
}

function _worstLevelAcross(items) {
  const order = { error: 3, warning: 2, warn: 2, info: 1 };
  let worst = "info";
  for (const it of items) {
    const lvl = (it && it.level) || "info";
    if ((order[lvl] || 0) > (order[worst] || 0)) worst = lvl;
  }
  return worst === "warn" ? "warning" : worst;
}

function _toneForLevel(level) {
  if (level === "error") return "bad";
  if (level === "warning" || level === "warn") return "warn";
  return "info";
}

function renderUpdates() {
  const root = document.getElementById("updates-feed");
  root.innerHTML = "";
  if (!state.updates.length) {
    root.innerHTML =
      `<div class="updates-empty">` +
      `<h3>No updates yet.</h3>` +
      `<p>An update is created whenever a pull discovers something new: ` +
      `a silent update, a new event code, an appointment change, ` +
      `or a new notice. Records are computed directly from the capture history, so ` +
      `restarting the server never loses or duplicates them.</p>` +
      `</div>`;
    return;
  }

  const head = document.createElement("div");
  head.className = "updates-head";
  head.innerHTML =
    `<div>` +
      `<h2>Updates</h2>` +
      `<div class="updates-sub">` +
        `${state.updates.length} record${state.updates.length === 1 ? "" : "s"}` +
        ` across ${new Set(state.updates.map(u => u.receiptNumber)).size} case(s).` +
        ` Derived from capture history — never stored.` +
      `</div>` +
    `</div>`;
  root.appendChild(head);

  for (const u of state.updates) {
    root.appendChild(renderUpdateRecord(u));
  }
}

function renderUpdateRecord(u) {
  const info = KIND_INFO[u.kind] || KIND_INFO.status;
  // Prefer the full capturedAt timestamp (`u.to`) for "Detected" so the
  // operator sees the wall-clock time of the pull that spotted this diff.
  // `detectedOn` (YYYY-MM-DD) is the calendar day we observed it — kept for
  // display fallback; the dedup key is the full `to` timestamp on `u.id`.
  const detectedDisplay = u.to
    ? formatLocalDateTime(u.to)
    : formatDate(u.detectedOn || "");
  // `realUpdateDate` is sourced from USCIS's `updatedAt`, which is a date
  // (no time) — formatDate is still correct here.
  const realDisplay = u.realUpdateDate ? formatDate(u.realUpdateDate) : "";
  // Only show the "Update date" tail when it genuinely differs from the
  // detection date — compare at the day level since realUpdateDate has
  // no time component.
  const detectedDay = (u.to || u.detectedOn || "").slice(0, 10);
  const dateLine = realDisplay && realDisplay !== formatDate(detectedDay)
    ? `Detected ${detectedDisplay} · Update date ${realDisplay}`
    : `Detected ${detectedDisplay}`;

  const block = document.createElement("article");
  block.className = "update-record";
  // The update id is "{receipt}:{source}:{date}" — scrub the receipt out of
  // the DOM attribute when redaction is on (nothing reads it functionally).
  block.dataset.id = redactMaybe(u.id || "");
  const sourceBadge = "";
  block.innerHTML =
    `<header class="update-head">` +
      `<div class="update-head-left">` +
        `<span class="update-case">${escapeHtml(u.caseLabel || "?")}</span>` +
        `<span class="update-receipt${state.redacted ? " redacted-text" : ""}">` +
          `${escapeHtml(redactDisplay(u.receiptNumber || ""))}</span>` +
        sourceBadge +
      `</div>` +
      `<span class="kind-tag kind-${info.tone || "n"}" ` +
           `title="${escapeHtml(info.desc || info.label)}">${escapeHtml(info.label)}</span>` +
    `</header>` +
    `<div class="update-dates">${escapeHtml(dateLine)}</div>`;

  // Scalar changes
  const scalars = u.scalars || {};
  if (Object.keys(scalars).length) {
    const sec = document.createElement("div");
    sec.className = "change-section";
    sec.innerHTML = `<h5>Field changes</h5>`;
    for (const [k, v] of Object.entries(scalars)) {
      const row = document.createElement("div");
      row.className = "change-scalar";
      const mask = state.redacted && REDACT_KEYS.has(k);
      const fromHtml = mask ? escapeHtml(REDACTION_MASK) : formatScalarValueHtml(v.from);
      const toHtml = mask ? escapeHtml(REDACTION_MASK) : formatScalarValueHtml(v.to);
      const valClass = mask ? " redacted-text" : "";
      row.innerHTML =
        `<span class="field">${escapeHtml(k)}</span>` +
        `<span class="from${valClass}">${fromHtml}</span>` +
        `<span class="to${valClass}">${toHtml}</span>`;
      sec.appendChild(row);
    }
    block.appendChild(sec);
  }

  // Collection deltas
  const collections = [
    ["events", "Events"],
    ["notices", "Notices"],
    ["documents", "Documents"],
    ["addendums", "Addendums"],
  ];
  for (const [key, title] of collections) {
    const coll = u[key] || {};
    if (!(coll.added?.length || coll.removed?.length)) continue;
    const sec = document.createElement("div");
    sec.className = "change-section";
    sec.innerHTML = `<h5>${title}</h5>`;
    for (const a of coll.added || []) {
      const chip = document.createElement("span");
      chip.className = "change-item-added";
      chip.innerHTML = "+ " + redactMaybe(describeItem(key, a));
      sec.appendChild(chip);
    }
    for (const r of coll.removed || []) {
      const chip = document.createElement("span");
      chip.className = "change-item-removed";
      chip.innerHTML = "− " + redactMaybe(describeItem(key, r));
      sec.appendChild(chip);
    }
    block.appendChild(sec);
  }

  return block;
}

// Pure: build the timeline's rows from a snapshot's events + the diff feed,
// sorted newest-first by each row's REAL timestamp (full precision).
//
// One consistent rule: rank by USCIS's WRITE/MODIFICATION time. For an event
// that's createdAtTimestamp (when USCIS wrote the row); for a silent update
// that's the updatedAtTimestamp USCIS moved the case to. Both are the same
// "when USCIS touched it" clock — NOT the claimed event time (eventTimestamp,
// which is backdated by weeks for re-emits) and NOT our detection time (which
// is an accident of pull cadence). So events and silent updates interleave in
// true chronological order, and a re-emit lands when observed, not buried at
// its stale claimed date. Events are deduped by eventId.
function buildTimelineRows(events, changes, entries) {
  const rows = [];
  const seen = new Set();
  // Map each eventId to the capturedAt of the snapshot that FIRST contained
  // it — that's the detection time we surface on hover ("Detected at"). This
  // is derived from the raw capture history so it works identically for every
  // case: events present at the very first snapshot are "detected" at that
  // baseline capture, and events that appear later are detected on the pull
  // that first saw them. (The events.added diff feed only covers post-baseline
  // arrivals, so it can't be the sole source — that's what made detection look
  // case-specific.)
  const detectedByEventId = new Map();
  const sortedEntries = [...(entries || [])].sort((a, b) =>
    (a.capturedAt || "").localeCompare(b.capturedAt || ""));
  for (const entry of sortedEntries) {
    const data = entry.data || entry;
    const evs = Array.isArray(data && data.events) ? data.events : [];
    for (const ev of evs) {
      const eid = ev.eventId;
      if (eid && !detectedByEventId.has(eid)) {
        detectedByEventId.set(eid, entry.capturedAt || null);
      }
    }
  }
  for (const e of events || []) {
    const eid = e.eventId;
    if (eid) {
      if (seen.has(eid)) continue;
      seen.add(eid);
    }
    // Write-time fields first; fall back to claimed-time only if absent.
    const ts = e.createdAtTimestamp || e.createdAt || e.eventTimestamp || e.eventDateTime || "";
    rows.push({ date: (ts || "").slice(0, 10) || "—", ts, code: e.eventCode || "?",
                event: e, eventId: eid || null,
                detectedAt: (eid && detectedByEventId.get(eid)) || null });
  }
  for (const ch of changes || []) {
    if (ch.kind !== "silent_update") continue;
    const sc = ch.scalars || {};
    const ts = (sc.updatedAtTimestamp || {}).to || (sc.updatedAt || {}).to || ch.to || "";
    rows.push({ date: (ts || "").slice(0, 10) || "—", ts, code: "silent update",
                silent: true, change: ch, detectedAt: ch.to || null });
  }
  // Fall back to the day when a row has no timestamp at all.
  rows.sort((a, b) => (b.ts || b.date).localeCompare(a.ts || a.date));
  return rows;
}

// Factual combined timeline: every USCIS event on the case plus every silent
// update we've detected, merged chronologically (newest first). Date + code
// only — no interpretation, no stage inference. Form-agnostic.
function renderObservedEventCodes(c) {
  const section = document.createElement("section");
  section.className = "events-section";

  const heading = document.createElement("h4");
  heading.className = "events-heading section-head-row";
  const headingLabel = document.createElement("span");
  headingLabel.textContent = "Timeline";
  heading.appendChild(headingLabel);
  const caseApiBtn = apiLinkButton(c.receiptNumber, "case", c.label);
  if (caseApiBtn) heading.appendChild(caseApiBtn);
  section.appendChild(heading);

  const hist = state.histories[c.label];
  const changes = (hist && hist.changes) || [];
  const links = (hist && hist.links) || [];
  const entries = (hist && hist.entries) || [];

  // 1. Raw events from the latest snapshot, deduped by eventId.
  // eventId is USCIS's own natural key — collisions across the same
  // payload would be a server bug, not something to paper over here.
  //
  // Dated by createdAtTimestamp — USCIS's "we wrote this row" timestamp.
  // This is the honest record of when USCIS actually committed the row,
  // separate from eventDateTime (which is the date USCIS claims the event
  // occurred and which USCIS occasionally backdates). When a row is
  // re-emitted with a new eventId at a later date, createdAt reflects
  // the re-emit day directly, no editorial layer required.
  const events = Array.isArray((c.latest || {}).events) ? c.latest.events : [];
  const rows = buildTimelineRows(events, changes, entries);

  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "no-changes";
    empty.textContent = "No activity observed yet.";
    section.appendChild(empty);
    return section;
  }

  // The list lives in a positioned wrapper so the SVG link overlay can be
  // absolutely positioned over the rows after layout.
  const wrap = document.createElement("div");
  wrap.className = "events-list-wrap";

  const list = document.createElement("ul");
  list.className = "events-list";
  for (const r of rows) {
    const item = document.createElement("li");
    item.className = "events-item";
    if (r.eventId) item.dataset.eventId = r.eventId;
    const tooltip = r.silent
      ? buildSilentUpdateTooltip(r.change, r.detectedAt)
      : buildEventTooltip(r.event, r.detectedAt);
    const codeClasses =
      `events-code${r.silent ? " events-code-silent" : ""}` +
      (tooltip ? " events-tooltip" : "");
    const tooltipAttr = tooltip ? ` data-tooltip="${escapeHtml(tooltip)}"` : "";
    item.innerHTML =
      `<span class="events-date">${escapeHtml(formatDate(r.date))}</span>` +
      `<span class="${codeClasses}"${tooltipAttr}>${escapeHtml(r.code)}</span>`;
    list.appendChild(item);
  }
  wrap.appendChild(list);
  section.appendChild(wrap);

  // Re-emit links always render (right gutter). Stash the links on the wrap
  // so the shared resize handler can redraw without re-rendering the card.
  if (links.length) {
    wrap._eventLinks = links;
    requestAnimationFrame(() => drawEventLinks(wrap, list, links));
  }
  return section;
}

// Per-link colors: the Notion light-mode "Text" palette, in a deliberately
// staggered order (red, blue, orange, green, purple, black, gray, …) so the
// first few links — the common case — are maximally contrasting. Black and gray
// are the highest-contrast neutrals on the light card, so they sit early rather
// than at the tail. The blue (#337EA9, a steel blue) is intentionally a
// DIFFERENT tone from the event pills' indigo blue (--accent #3a5cd8), so it
// reads as its own color. Keyed by a STABLE sequence index (the link's position
// in the engine's appearance-ordered output) — NOT render order or link
// count — so an existing link keeps its color when a newer link is added.
// Cycles if links exceed the palette length.
const EVENT_LINK_PALETTE = [
  "#D44C47", "#337EA9", "#D9730D", "#448361", "#9065B0",
  "#37352F", "#787774", "#C14C8A", "#CB912F", "#9F6B53",
];
function eventLinkColor(seq) {
  return EVENT_LINK_PALETTE[seq % EVENT_LINK_PALETTE.length];
}

// Draw a bracket connector from each re-emit row back to its origin row, in
// the RIGHT gutter just past the event content. Pure DOM measurement →
// absolutely-positioned SVG; the only layout assumption is "each linked row
// carries data-event-id". Brackets share a lane unless their vertical spans
// overlap (greedy interval coloring), so non-crossing links sit flush at the
// same level and only genuinely-conflicting ones step out. Each link has its
// own color; the leg pointing at the origin carries an arrowhead.
function drawEventLinks(wrap, list, links) {
  const wrapBox = wrap.getBoundingClientRect();
  // Bail BEFORE removing the existing overlay: a zero-width box means the wrap
  // is hidden (off-view nav tab), and measuring would collapse every edge to
  // (0,0). Leave any existing overlay intact and wait for a redraw once the
  // container is visible again (setView / resize both re-invoke this).
  if (!wrapBox.width) return;
  wrap.querySelectorAll(".events-link-overlay").forEach((el) => el.remove());

  const rowFor = (eid) =>
    list.querySelector(`.events-item[data-event-id="${CSS.escape(eid)}"]`);
  // .events-item uses `display: contents`, so the <li> has no box of its own
  // and getBoundingClientRect() returns zeros. Measure the code cell (2nd
  // child) — its right edge is where a right-gutter bracket should anchor.
  const codeCell = (el) => el.children[1] || el.firstElementChild || el;
  const rightOf = (el) => codeCell(el).getBoundingClientRect().right - wrapBox.left;
  const midY = (el) => {
    const b = codeCell(el).getBoundingClientRect();
    return b.top - wrapBox.top + b.height / 2;
  };

  const drawable = links
    // seq = stable index in the engine's (appearance-ordered) output, so a
    // link's color is fixed by its position in that sequence and never shifts
    // when a newer link is appended or an unmappable one is filtered out.
    .map((l, seq) => ({ link: l, seq, o: rowFor(l.originId), r: rowFor(l.reemitId) }))
    .filter((d) => d.o && d.r)
    .map((d) => {
      const yReemit = midY(d.r);
      const yOrigin = midY(d.o);
      return {
        ...d,
        yReemit,
        yOrigin,
        top: Math.min(yReemit, yOrigin),
        bottom: Math.max(yReemit, yOrigin),
      };
    });
  if (!drawable.length) return;

  // Anchor the rail past the widest pill of ANY row in the list, not just the
  // linked rows. A bracket's spine spans OVER the rows between origin and
  // re-emit (often wider "silent update" pills), so anchoring only past the
  // narrow FTA pills would cut through that text. Clear everything.
  const allRowsRight = [...list.querySelectorAll(".events-item")]
    .map((li) => codeCell(li).getBoundingClientRect().right - wrapBox.left);
  const widest = allRowsRight.length ? Math.max(...allRowsRight) : 0;
  const railX = widest + 14;
  const laneGap = 20;  // horizontal spacing between staggered lanes

  // Lane assignment — nesting-aware so the layout is crossing-free BY
  // CONSTRUCTION (no detect-then-fix pass). Three crossing types are possible
  // in this gutter model, each eliminated here or in endpoint ordering below:
  //   • riser×riser — impossible: every overlapping link gets its own lane.
  //   • leg×riser   — a link attaching at a row must sit SHALLOWER (lower lane,
  //     nearer the pills) than any link whose riser merely spans over that row,
  //     so its short leg stops before reaching the deeper riser. Guaranteed by
  //     assigning lanes SHORTEST-SPAN-FIRST: a link nested inside another's
  //     span is processed first and takes the shallower lane.
  //   • leg×leg at a shared row — handled by endpoint ordering (next block).
  // Greedy interval coloring over span-sorted links: each link takes the
  // lowest lane whose current occupant doesn't vertically overlap it, so
  // non-overlapping links still share lane 0 (sit flush at the same level).
  const EPS = 2;
  const laneRanges = [];  // laneRanges[k] = {top, bottom} occupying lane k
  const ordered = [...drawable].sort((a, b) =>
    (a.bottom - a.top) - (b.bottom - b.top) || a.top - b.top);
  ordered.forEach((d) => {
    let lane = 0;
    while (lane < laneRanges.length) {
      const r = laneRanges[lane];
      const overlaps = d.top < r.bottom - EPS && d.bottom > r.top + EPS;
      if (!overlaps) break;
      lane++;
    }
    laneRanges[lane] = { top: d.top, bottom: d.bottom };
    d.lane = lane;
  });

  // Endpoint ordering: when several links attach to the SAME row, assign each
  // a distinct Y slot. Naive ordering (by lane) still lets a link's short
  // horizontal leg cross another link's vertical riser right at the corner.
  // The crossing-free order is provable from the geometry: a leg runs from the
  // pill out to its lane, where the riser then climbs UP (other endpoint is
  // above this row) or DOWN (below). Order the slots top→bottom as:
  //   1. up-risers first, then down-risers  — the two groups can't cross.
  //   2. within up-risers: shallow lane → deep   (nearer pill sits higher)
  //   3. within down-risers: deep lane → shallow  (nearer pill sits lower)
  // This makes every leg "peel off" toward its riser without cutting another.
  drawable.forEach((d) => { d.originY = d.yOrigin; d.reemitY = d.yReemit; });
  const endpointsByRow = new Map();
  const addEndpoint = (rowEl, d, end) => {
    if (!endpointsByRow.has(rowEl)) endpointsByRow.set(rowEl, []);
    endpointsByRow.get(rowEl).push({ d, end });
  };
  drawable.forEach((d) => { addEndpoint(d.o, d, "origin"); addEndpoint(d.r, d, "reemit"); });
  const SLOT_GAP = 5;
  endpointsByRow.forEach((eps, rowEl) => {
    if (eps.length < 2) return;  // single endpoint stays on the row mid
    const baseY = midY(rowEl);
    const h = codeCell(rowEl).getBoundingClientRect().height || 18;
    // Direction this endpoint's riser travels: sign of (other end Y − this Y).
    // < 0 → other end is above → riser goes UP; > 0 → DOWN.
    const dirOf = (ep) => {
      const otherY = ep.end === "origin" ? ep.d.yReemit : ep.d.yOrigin;
      return Math.sign(otherY - baseY) || 1;
    };
    eps.sort((a, b) => {
      const da = dirOf(a), db = dirOf(b);
      if (da !== db) return da - db;               // up-risers (−1) before down (+1)
      // same direction: up → shallow-first (lane asc); down → deep-first (lane desc)
      return da < 0 ? a.d.lane - b.d.lane : b.d.lane - a.d.lane;
    });
    const gap = Math.min(SLOT_GAP, (h * 0.72) / (eps.length - 1));
    eps.forEach((ep, k) => {
      const y = baseY + (k - (eps.length - 1) / 2) * gap;
      if (ep.end === "origin") ep.d.originY = y; else ep.d.reemitY = y;
    });
  });

  const svgNS = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(svgNS, "svg");
  svg.classList.add("events-link-overlay");
  svg.setAttribute("width", "100%");
  svg.setAttribute("height", String(Math.ceil(wrapBox.height)));

  // One <defs> with a per-color arrowhead marker (markers can't inherit the
  // path's stroke via currentColor on all engines, so mint one per hue used).
  const defs = document.createElementNS(svgNS, "defs");
  const markerIds = new Map();
  const ensureMarker = (color) => {
    if (markerIds.has(color)) return markerIds.get(color);
    const id = `evlink-arrow-${markerIds.size}`;
    const m = document.createElementNS(svgNS, "marker");
    m.setAttribute("id", id);
    m.setAttribute("markerWidth", "7");
    m.setAttribute("markerHeight", "7");
    m.setAttribute("refX", "5.5");
    m.setAttribute("refY", "3");
    m.setAttribute("orient", "auto");
    m.setAttribute("markerUnits", "userSpaceOnUse");
    const tip = document.createElementNS(svgNS, "path");
    tip.setAttribute("d", "M0,0 L6,3 L0,6 Z");
    tip.setAttribute("fill", color);
    m.appendChild(tip);
    defs.appendChild(m);
    markerIds.set(color, id);
    return id;
  };

  // Color comes from each link's stable seq, not this render index, so it's
  // identity-stable across additions.
  drawable.forEach((d) => {
    const laneX = railX + d.lane * laneGap;
    const color = eventLinkColor(d.seq);
    const arrowId = ensureMarker(color);
    // Prong roots sit just past each row's own pill so the bracket touches its
    // events; the riser rides the assigned lane, clearing all spanned text.
    const prongReemit = rightOf(d.r) + 4;
    const prongOrigin = rightOf(d.o) + 4;
    const yR = d.reemitY;   // fanned attach Y at the re-emit row
    const yO = d.originY;   // fanned attach Y at the origin row

    // Group the spine + origin leg so hovering either dims every OTHER link —
    // the readability fix for dense/crossing cases: highlight one path at a
    // time instead of asking the eye to trace through a multicolor band.
    const g = document.createElementNS(svgNS, "g");
    g.setAttribute("class", "events-link-group");

    // One continuous rounded-orthogonal path: re-emit prong → corner → riser
    // down/up the lane → corner → origin leg, arrowhead at the origin tip.
    // Rounded corners (quadratic) make near-corners read as distinct curves
    // instead of mashed right angles. Direction-aware so the arc bends the
    // correct way whether the origin is below (newer→older, the usual case).
    const dir = yO >= yR ? 1 : -1;        // +1 = origin below re-emit
    // Corner radius stays small (<= 5px) and well under half the lane gap so a
    // rounded corner never bulges into the neighbouring lane.
    const rr = Math.min(5, Math.abs(yO - yR) / 2, laneGap / 3);
    const dStr =
      `M ${prongReemit} ${yR}` +
      ` H ${laneX - rr}` +
      ` Q ${laneX} ${yR} ${laneX} ${yR + dir * rr}` +
      ` V ${yO - dir * rr}` +
      ` Q ${laneX} ${yO} ${laneX - rr} ${yO}` +
      ` H ${prongOrigin}`;

    // Casing: a thick background-colored stroke drawn UNDER the colored line so
    // wherever two lines pass within a pixel or two, the one on top cleanly
    // breaks the one beneath instead of merging into it. Standard graph-edge
    // technique; keeps near-parallel lanes legible without forcing huge gaps.
    const casing = document.createElementNS(svgNS, "path");
    casing.setAttribute("d", dStr);
    casing.setAttribute("class", "events-link-casing");

    // Invisible fat hit-area so hovering NEAR a line (not pixel-perfect on the
    // 1.5px stroke) still selects it. It carries the pointer events for the
    // group; the visible strokes stay thin. Drawn first so it sits beneath.
    const hit = document.createElementNS(svgNS, "path");
    hit.setAttribute("d", dStr);
    hit.setAttribute("class", "events-link-hit");

    const path = document.createElementNS(svgNS, "path");
    path.setAttribute("d", dStr);
    path.setAttribute("class", "events-link-path");
    path.style.stroke = color;
    path.setAttribute("marker-end", `url(#${arrowId})`);

    // Instant tooltip via the shared events-popup instead of an SVG <title>,
    // whose native hover delay (~700ms) feels sluggish. The hit path carries
    // the pointer events, so it carries the data-tooltip for _wireTooltips.
    const d_ = d.link.daysApart;
    hit.classList.add("events-tooltip");
    hit.setAttribute("data-tooltip",
      `${d.link.eventCode} re-emitted` +
      (d_ != null ? ` ${d_} day${d_ === 1 ? "" : "s"} later` : "") +
      ` (same eventTimestamp ${d.link.eventTimestamp})`);
    g.appendChild(hit);
    g.appendChild(casing);
    g.appendChild(path);
    svg.appendChild(g);
  });

  svg.insertBefore(defs, svg.firstChild);
  wrap.appendChild(svg);
}

// Redraw every visible event-link overlay on resize so brackets track the
// rows as the layout reflows. Wired once.
let _eventLinkResizeWired = false;
// Redraw every timeline's event-link overlay from its stashed link list.
// Used after a wrap regains width (returning to the Cases view) and on
// window resize. Debounced through a single rAF so a burst of resize events
// (drag-resize) coalesces into one redraw after layout settles.
let _eventLinkRedrawRaf = null;
function redrawAllEventLinks() {
  if (_eventLinkRedrawRaf) cancelAnimationFrame(_eventLinkRedrawRaf);
  _eventLinkRedrawRaf = requestAnimationFrame(() => {
    _eventLinkRedrawRaf = null;
    document.querySelectorAll(".events-list-wrap").forEach((wrap) => {
      const list = wrap.querySelector(".events-list");
      if (wrap._eventLinks && list) drawEventLinks(wrap, list, wrap._eventLinks);
    });
  });
}

function wireEventLinkResize() {
  if (_eventLinkResizeWired) return;
  _eventLinkResizeWired = true;
  window.addEventListener("resize", redrawAllEventLinks);
}

// Format an ISO timestamp for an event/silent-update hover tooltip in the
// user's local timezone (e.g. "MM/DD/YYYY h:mm:ssAM EDT"). Returns the raw
// value if it doesn't look like a timestamp so we never silently swallow
// non-ISO data.
function formatTimestampLocal(v) {
  if (!v) return "—";
  const s = String(v);
  if (!/^\d{4}-\d{2}-\d{2}T/.test(s)) return s;
  try {
    return formatLocalDateTime(s, { withSeconds: true });
  } catch (_) {
    return s;
  }
}

// Event hover: surface USCIS's two timestamps (when *they* wrote it vs
// when *they* claim it happened). Localized so the user sees their own
// wall-clock time. The first line states the timezone explicitly so a
// hover-only reader knows the times below are local, not UTC.
function buildEventTooltip(e, detectedAt) {
  if (!e) return "";
  const lines = [`Times shown in ${getLocalTimezoneAbbrev()}`];
  if (detectedAt) lines.push(`Detected at: ${formatTimestampLocal(detectedAt)}`);
  if (e.eventCode) lines.push(`Code:    ${e.eventCode}`);
  if (e.updatedAtTimestamp) {
    lines.push(`Updated: ${formatTimestampLocal(e.updatedAtTimestamp)}`);
  } else if (e.updatedAt) {
    lines.push(`Updated: ${e.updatedAt}`);
  }
  if (e.eventTimestamp) {
    lines.push(`Event:   ${formatTimestampLocal(e.eventTimestamp)}`);
  } else if (e.eventDateTime) {
    lines.push(`Event:   ${e.eventDateTime}`);
  }
  return lines.join("\n");
}

// Silent-update hover: render each scalar diff as a labeled block with
// Before/After on indented lines so the two values line up vertically
// and the eye can compare them at a glance. "After:" is padded with an
// extra space so the colons (and thus the value column) align with
// "Before:". First line states the timezone so the hover is
// self-explanatory. Always leads with the detection time — even when the
// timestamp itself didn't move (JSON changed but updatedAt didn't), the
// "Detected at" line still tells the reader when we saw it.
function buildSilentUpdateTooltip(ch, detectedAt) {
  if (!ch) return "";
  const scalars = ch.scalars || {};
  const keys = Object.keys(scalars);
  const lines = [`Times shown in ${getLocalTimezoneAbbrev()}`];
  const det = detectedAt || ch.to;
  if (det) lines.push(`Detected at: ${formatTimestampLocal(det)}`);
  for (const key of keys) {
    const { from, to } = scalars[key];
    lines.push(`${key}:`);
    lines.push(`    Before: ${formatTimestampLocal(from)}`);
    lines.push(`    After:  ${formatTimestampLocal(to)}`);
  }
  return lines.join("\n");
}

function callout(tone, title, body) {
  const el = document.createElement("div");
  el.className = `callout callout-${tone}`;
  el.innerHTML =
    `<div class="callout-title">${escapeHtml(title)}</div>` +
    `<div class="callout-body">${escapeHtml(body)}</div>`;
  return el;
}

// ---------- raw ----------

const RAW_SOURCES = {
  case: {
    label: "Case API",
    historyKey: "entries",
    fileSuffix: "_case.json",
    emptyMsg: "No captures yet.",
  },
};

function renderRaw(panel, c) {
  panel.innerHTML = "";
  const hist = state.histories[c.label] || {};

  // Per-capture selection state (state.rawSelection) is keyed by source so it
  // stays stable across re-renders.
  const activeSource = "case";
  state.rawSource[c.receiptNumber] = activeSource;

  const src = RAW_SOURCES[activeSource];
  const entries = (hist[src.historyKey] || []);
  if (!entries.length) {
    const empty = document.createElement("div");
    empty.className = "no-changes";
    empty.textContent = src.emptyMsg;
    panel.appendChild(empty);
    return;
  }

  const controls = document.createElement("div");
  controls.className = "raw-controls";

  // Snapshot picker: label each option with the user's local wall-clock time.
  const label = document.createElement("label");
  label.textContent = "Snapshot: ";
  const select = document.createElement("select");
  for (const e of [...entries].reverse()) {
    const opt = document.createElement("option");
    opt.value = e.capturedAt;
    opt.textContent = formatSnapshotLabel(e.capturedAt);
    select.appendChild(opt);
  }
  // Selection is tracked per (receipt, source) so switching sub-tabs
  // doesn't wipe the user's picked capture on the other source.
  const selKey = `${c.receiptNumber}:${activeSource}`;
  const current =
    state.rawSelection[selKey] ||
    entries[entries.length - 1].capturedAt;
  select.value = current;
  state.rawSelection[selKey] = current;
  select.addEventListener("change", () => {
    state.rawSelection[selKey] = select.value;
    updateRawBody();
  });
  label.appendChild(select);
  controls.appendChild(label);

  // Actions: download the full log file for this case.
  const actions = document.createElement("span");
  actions.className = "raw-actions";

  const dlAll = document.createElement("button");
  dlAll.type = "button";
  dlAll.className = "raw-btn";
  dlAll.textContent = `Download full history (${entries.length})`;
  dlAll.title = "Download every capture for this case as a single JSON file";
  dlAll.addEventListener("click", () => {
    if (state.redacted) return; // guarded: disabled while redaction is on
    const num = (c.label || "").match(/(\d+)/);
    const fname = num
      ? `${num[1]}${src.fileSuffix}`
      : `${c.receiptNumber}${src.fileSuffix}`;
    downloadJson(fname, entries);
  });
  actions.appendChild(dlAll);

  const copyOne = document.createElement("button");
  copyOne.type = "button";
  copyOne.className = "raw-btn raw-btn-ghost";
  const COPY_IDLE_LABEL = "Copy this snapshot";
  copyOne.textContent = COPY_IDLE_LABEL;
  copyOne.title = "Copy the currently selected snapshot as JSON to the clipboard";
  copyOne.addEventListener("click", async () => {
    if (state.redacted) return; // guarded: disabled while redaction is on
    const ca = state.rawSelection[selKey];
    const entry = entries.find(e => e.capturedAt === ca) || entries[entries.length - 1];
    const payload = entry.data || entry; // extract data envelope if present
    const text = JSON.stringify(payload, null, 2);
    const ok = await copyToClipboard(text);
    copyOne.textContent = ok ? "Copied ✓" : "Copy failed";
    copyOne.classList.toggle("raw-btn-copied", ok);
    setTimeout(() => {
      copyOne.textContent = COPY_IDLE_LABEL;
      copyOne.classList.remove("raw-btn-copied");
    }, 1600);
  });
  actions.appendChild(copyOne);

  // Redaction mode: gray out + disable both data-exfil actions so full,
  // unmasked data can't leave the page while you're sharing/screenshotting.
  if (state.redacted) {
    for (const b of [dlAll, copyOne]) {
      b.disabled = true;
      b.classList.add("is-disabled");
      b.title = "Disabled while redaction is on";
    }
  }

  controls.appendChild(actions);
  panel.appendChild(controls);

  const pre = document.createElement("pre");
  pre.className = "raw";
  panel.appendChild(pre);
  updateRawBody();

  function updateRawBody() {
    const ca = state.rawSelection[selKey];
    const entry = entries.find(e => e.capturedAt === ca) || entries[entries.length - 1];
    let payload = entry.data || entry; // extract data envelope if present
    // Mask PII values in-place (clone) when redaction is on, so the rendered
    // JSON is screenshot-safe while keeping its structure readable.
    if (state.redacted) payload = redactSnapshot(payload);
    // 4-space indent + syntax highlight — written as HTML so colours work.
    pre.innerHTML = highlightJson(JSON.stringify(payload, null, 4));
  }
}

// Token-based JSON syntax highlight. Returns HTML safe to assign to
// innerHTML — the source is always JSON.stringify output, so there are
// no surprises beyond the string/number/boolean/null/key tokens.
function highlightJson(src) {
  const esc = s => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  // Matches (in order): keys, string values, numbers, booleans, null.
  const re = /("(?:\\.|[^"\\])*")(\s*:)?|\b(true|false|null)\b|-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?/g;
  return esc(src).replace(re, (m, str, colon, bool) => {
    if (str) {
      return colon
        ? `<span class="json-key">${str}</span>${colon}`
        : `<span class="json-string">${str}</span>`;
    }
    if (bool === "true" || bool === "false") {
      return `<span class="json-bool">${bool}</span>`;
    }
    if (bool === "null") {
      return `<span class="json-null">null</span>`;
    }
    return `<span class="json-number">${m}</span>`;
  });
}

// ---------- helpers ----------

function updateCountdown() {
  const el = document.getElementById("countdown");
  if (!state.nextRun) {
    el.textContent = "—";
    return;
  }
  const ms = state.nextRun.getTime() - Date.now();
  if (ms <= 0) {
    el.textContent = "due";
    return;
  }
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  el.textContent =
    (h ? `${h}h ` : "") + `${String(m).padStart(2, "0")}m ${String(sec).padStart(2, "0")}s`;
}

// Single canonical datetime renderer — every displayed timestamp goes
// through this. Output is compact 12-hour local wall-clock WITHOUT the
// timezone abbreviation (e.g. "2026-04-18 8:33PM"). The timezone is
// surfaced once in the topbar subtitle via getLocalTimezoneAbbrev(), so
// each individual timestamp doesn't have to repeat it.
//
// Accepts a Date or an ISO-8601 string. Pass { withSeconds: true } for
// the raw-JSON snapshot picker, which needs second-level precision.
// Display format is MM/DD/YYYY — the stored timestamp is untouched;
// this only affects rendering.
function formatLocalDateTime(input, opts) {
  if (input == null) return "—";
  const d = input instanceof Date ? input : new Date(input);
  if (isNaN(d.getTime())) return String(input);
  const pad = n => String(n).padStart(2, "0");
  const date = `${pad(d.getMonth() + 1)}/${pad(d.getDate())}/${d.getFullYear()}`;
  let hours = d.getHours();
  const ampm = hours >= 12 ? "PM" : "AM";
  hours = hours % 12 || 12;
  const seconds = opts && opts.withSeconds ? `:${pad(d.getSeconds())}` : "";
  // No space before AM/PM — user-requested compact form.
  const time = `${hours}:${pad(d.getMinutes())}${seconds}${ampm}`;
  return `${date} ${time}`;
}

// Render a calendar date as MM/DD/YYYY.  Accepts:
//   - "YYYY-MM-DD"            (e.g. USCIS submissionDate)
//   - "YYYY-MM-DDTHH:MM:SSZ"  (will be truncated to the date portion)
//   - a Date object
// Returns the original value when it can't be parsed, so operators
// still see a legible fallback rather than "NaN/NaN/NaN".
function formatDate(input) {
  if (input == null || input === "") return "—";
  if (input instanceof Date) {
    if (isNaN(input.getTime())) return "—";
    const pad = n => String(n).padStart(2, "0");
    return `${pad(input.getMonth() + 1)}/${pad(input.getDate())}/${input.getFullYear()}`;
  }
  const s = String(input);
  // Match a leading YYYY-MM-DD (year, month, day) before any "T" or space.
  const m = s.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (m) return `${m[2]}/${m[3]}/${m[1]}`;
  return s;
}

// Current browser's short timezone abbreviation (e.g. "EDT", "PST", "JST").
// Falls back to the UTC offset when the runtime doesn't surface a name.
function getLocalTimezoneAbbrev() {
  const d = new Date();
  try {
    const parts = new Intl.DateTimeFormat(undefined, {
      timeZoneName: "short",
    }).formatToParts(d);
    const tzPart = parts.find(p => p.type === "timeZoneName");
    if (tzPart && tzPart.value) return tzPart.value;
  } catch (_) { /* fall through */ }
  const off = -d.getTimezoneOffset();
  const sign = off >= 0 ? "+" : "-";
  const h = Math.floor(Math.abs(off) / 60);
  const m = Math.abs(off) % 60;
  return `UTC${sign}${h}${m ? `:${String(m).padStart(2, "0")}` : ""}`;
}

// Back-compat aliases: existing call sites can stay as-is.
const formatLocal = d => formatLocalDateTime(d);
const formatSnapshotLabel = iso => formatLocalDateTime(iso, { withSeconds: true });

function downloadJson(filename, payload) {
  const text = JSON.stringify(payload, null, 2);
  const blob = new Blob([text], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  requestAnimationFrame(() => {
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  });
}

// Copy the given text to the clipboard. Returns true on success.
// Prefers navigator.clipboard; falls back to a hidden textarea +
// document.execCommand("copy") for browsers / contexts (e.g. http
// origins that aren't localhost) where the async API is gated.
async function copyToClipboard(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (_) { /* fall through to legacy path */ }
  try {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    document.body.removeChild(ta);
    return ok;
  } catch (_) {
    return false;
  }
}

function formatApplicant(s) {
  if (!s) return "";
  // USCIS returns "LASTNAME, FIRSTNAME" → display as "Firstname Lastname".
  const m = s.match(/^([A-Z]+),\s*([A-Z]+)/);
  if (!m) return s;
  const cap = x => x[0] + x.slice(1).toLowerCase();
  return `${cap(m[2])} ${cap(m[1])}`;
}

function formatValue(v) {
  if (v === undefined) return "—";
  if (v === null) return "null";
  if (typeof v === "boolean") return v ? "true" : "false";
  return String(v);
}

// Format a scalar diff value (the `from` / `to` side of a `change-scalar`
// row) as HTML. Identical to escapeHtml(formatValue(v)) except that any
// ISO-8601 UTC timestamp embedded in the value is wrapped in a
// `<span class="utc-ts">` carrying a `data-tooltip` with the same instant
// rendered in the browser's local timezone — so the operator can hover over a
// raw `updatedAtTimestamp` and read the wall-clock time without converting.
//
// The browser's timezone comes from
// Intl.DateTimeFormat().resolvedOptions().timeZone, computed once per
// page load. The displayed string stays UTC (preserves USCIS's exact
// payload byte-for-byte) — only the tooltip is localised.
function formatScalarValueHtml(v) {
  const raw = formatValue(v);
  // ISO-8601 UTC: YYYY-MM-DDTHH:MM:SS(.fraction)?Z. We match the whole
  // string (formatValue never embeds these inside other text) so the
  // split-and-wrap below is simple — full match → wrap, otherwise →
  // escape verbatim.
  const ISO_UTC = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/;
  if (!ISO_UTC.test(raw)) return escapeHtml(raw);
  const d = new Date(raw);
  if (isNaN(d.getTime())) return escapeHtml(raw);
  const local = formatLocalDateTime(d, { withSeconds: true });
  const tz = _localTimezoneAbbrev(d);
  const title = tz ? `${local} ${tz}` : local;
  // `data-tooltip` is picked up by the one site-wide tooltip (_wireTooltips);
  // `.utc-ts` only carries the dotted-underline affordance.
  return `<span class="utc-ts" data-tooltip="${escapeHtml(title)}">${escapeHtml(raw)}</span>`;
}

// Best-effort short timezone abbreviation for the browser's locale —
// "EDT", "PST", "JST" etc. Falls back to the IANA name (e.g.
// "America/New_York") when the runtime can't produce a short form.
let _LOCAL_TZ_NAME = null;
function _localTimezoneAbbrev(d) {
  try {
    // `timeZoneName: "short"` includes the abbreviation in the formatted
    // output. We extract it from `formatToParts` so we don't have to
    // string-search the rendered date.
    const parts = new Intl.DateTimeFormat(undefined, {
      hour: "numeric", timeZoneName: "short",
    }).formatToParts(d);
    const tz = parts.find(p => p.type === "timeZoneName");
    if (tz && tz.value) return tz.value;
  } catch (_) { /* fall through */ }
  if (_LOCAL_TZ_NAME === null) {
    try {
      _LOCAL_TZ_NAME = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    } catch (_) {
      _LOCAL_TZ_NAME = "";
    }
  }
  return _LOCAL_TZ_NAME;
}

function badge(text, kind) {
  const el = document.createElement("span");
  el.className = "badge" + (kind ? " " + kind : "");
  el.textContent = text;
  return el;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}

// Strip HTML tags to plain text. USCIS statusText embeds <a> links; we
// render it as textContent, so tags must be removed first (collapsing the
// surrounding whitespace the tags left behind).
function stripTags(s) {
  return String(s ?? "")
    .replace(/<[^>]*>/g, "")
    .replace(/\s+/g, " ")
    .trim();
}

// ----------------------------------------------------------------------
// Storage bar (topbar) + System-log tab size line.
// Shared state: LAST_STORAGE_DATA lets the systemlog view read the
// latest categories without a second fetch.
// ----------------------------------------------------------------------

let LAST_STORAGE_DATA = null;

async function updateStorageBar() {
  const wrap = document.getElementById("storage-bar");
  if (!wrap) return;
  let data;
  try {
    const r = await fetch("/api/storage");
    if (!r.ok) return;
    data = await r.json();
  } catch (_e) {
    return;
  }
  LAST_STORAGE_DATA = data;
  renderStorageBar(data);
  renderSyslogStorageLine(data);
}

function renderStorageBar(data) {
  const track = document.getElementById("storage-bar-track");
  const labelsEl = document.getElementById("storage-bar-labels");
  const totalEl = document.getElementById("storage-bar-total");
  if (!track || !totalEl) return;

  const cats = (data.categories || []).filter(c => c.bytes > 0);
  // Stacked breakdown: the denominator is the sum of the displayed
  // categories, so the bar always fills the track and each segment is
  // its share of the whole.
  const shown = cats.reduce((s, c) => s + c.bytes, 0);

  track.innerHTML = "";
  if (labelsEl) labelsEl.innerHTML = "";

  totalEl.textContent = formatBytes(shown);

  // Cases first, in the same order as the main case list; system log
  // last.
  const caseOrder = new Map();
  (state.cases || []).forEach((c, i) => caseOrder.set(c.label, i));
  const ordered = [...cats].sort((a, b) => {
    const aSys = a.key === "system_log";
    const bSys = b.key === "system_log";
    if (aSys !== bSys) return aSys ? 1 : -1;
    const ai = caseOrder.has(a.label) ? caseOrder.get(a.label) : Infinity;
    const bi = caseOrder.has(b.label) ? caseOrder.get(b.label) : Infinity;
    if (ai !== bi) return ai - bi;
    return b.bytes - a.bytes;
  });

  for (const cat of ordered) {
    const pct = shown > 0 ? (cat.bytes / shown) * 100 : 0;
    const pctLabel = `${Math.round(pct)}%`;
    const sizeLabel = formatBytes(cat.bytes);
    // `flex: 0 0 X%` pins the basis so segments stack proportionally
    // and together fill the track.
    const seg = document.createElement("div");
    seg.className = "storage-bar-seg events-tooltip";
    seg.dataset.key = cat.key;
    seg.style.flex = `0 0 ${pct}%`;
    // Hover/tap shows the size via the shared body-level popup.
    const tip =
      `${cat.label} — ${sizeLabel} · ${pctLabel} ` +
      `(${cat.file_count} file${cat.file_count === 1 ? "" : "s"})`;
    seg.setAttribute("data-tooltip", tip);
    seg.title = tip;
    track.appendChild(seg);
    if (labelsEl) {
      const lbl = document.createElement("div");
      lbl.className = "storage-bar-label";
      lbl.dataset.key = cat.key;
      lbl.title = tip;
      lbl.innerHTML =
        `<span class="storage-bar-label-swatch" data-key="${cat.key}"></span>` +
        `<span class="storage-bar-label-name">${escapeHtml(cat.label)}</span> ` +
        `<span class="storage-bar-label-pct">${pctLabel}</span>`;
      labelsEl.appendChild(lbl);
    }
  }
}

function renderSyslogStorageLine(data) {
  const el = document.getElementById("syslog-storage-line");
  if (!el) return;
  // The system_log bucket already aggregates events + traces +
  // session state — one number tells the whole story.
  const sl = (data.categories || []).find(c => c.key === "system_log");
  const slBytes = sl ? sl.bytes : 0;
  // Title above already says "System log", so drop the prefix here —
  // the subtitle just shows the size value followed by the standard
  // separator dot.
  el.textContent = `${formatBytes(slBytes)} · `;
}

function formatBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function toast(msg, kind) {
  let t = document.getElementById("toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "toast";
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.className = `show ${kind || ""}`;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    t.className = "";
  }, 3500);
}
