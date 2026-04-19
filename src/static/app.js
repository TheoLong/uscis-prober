// Copyright (C) 2026 the USCIS Prober contributors
// SPDX-License-Identifier: AGPL-3.0-or-later
// USCIS Prober UI — vanilla JS, no framework.

const state = {
  cases: [],
  histories: {},           // label → history payload
  activeTab: {},           // receiptNumber → tab id
  rawSelection: {},        // receiptNumber → capturedAt for raw view
  nextRun: null,
  pullRunning: false,
  eventCodeLabels: {},     // e.g. { FTA0: "Database checks received..." }
  view: "cases",           // "cases" | "updates" | "systemlog"
  updates: [],             // flat diff feed
  systemLog: [],           // flat event log from /api/system-log
};

// ---------- boot ----------

document.addEventListener("DOMContentLoaded", async () => {
  document.getElementById("pull-btn").addEventListener("click", triggerPull);
  wireExportInfo();
  document.querySelectorAll(".view-tab").forEach(btn =>
    btn.addEventListener("click", () => setView(btn.dataset.view))
  );
  await refreshAll();
  setInterval(updateCountdown, 1000);
  setInterval(pollPullStatus, 3000);
});

function wireExportInfo() {
  const btn = document.getElementById("export-info-btn");
  const pop = document.getElementById("export-info-popover");
  if (!btn || !pop) return;
  const close = () => {
    pop.hidden = true;
    btn.setAttribute("aria-expanded", "false");
  };
  btn.addEventListener("click", e => {
    e.stopPropagation();
    const open = pop.hidden;
    pop.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
  });
  // Dismiss when the user clicks anywhere else or presses Escape.
  document.addEventListener("click", e => {
    if (!pop.hidden && !pop.contains(e.target) && e.target !== btn) close();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") close();
  });
}

function setView(view) {
  state.view = view;
  document.querySelectorAll(".view-tab").forEach(btn =>
    btn.classList.toggle("active", btn.dataset.view === view)
  );
  document.getElementById("case-list").hidden = view !== "cases";
  document.getElementById("updates-feed").hidden = view !== "updates";
  document.getElementById("systemlog-feed").hidden = view !== "systemlog";
  if (view === "updates") renderUpdates();
  if (view === "systemlog") loadAndRenderSystemLog();
}

// ---------- data loading ----------

async function refreshAll() {
  await Promise.all([loadCases(), loadUpdates(), loadSystemLog(), pollPullStatus()]);
}

async function loadSystemLog() {
  try {
    const res = await fetch("/api/system-log?limit=500");
    const j = await res.json();
    state.systemLog = j.events || [];
    const countEl = document.getElementById("systemlog-count");
    if (state.systemLog.length) {
      countEl.hidden = false;
      countEl.textContent = String(state.systemLog.length);
    } else {
      countEl.hidden = true;
    }
  } catch (e) {
    console.warn("loadSystemLog failed:", e);
  }
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
    const countEl = document.getElementById("updates-count");
    if (state.updates.length) {
      countEl.hidden = false;
      countEl.textContent = String(state.updates.length);
    } else {
      countEl.hidden = true;
    }
    if (state.view === "updates") renderUpdates();
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

async function pollPullStatus() {
  try {
    const res = await fetch("/api/pull/status");
    const s = await res.json();
    state.nextRun = s.next_run ? new Date(s.next_run) : null;
    const wasRunning = state.pullRunning;
    state.pullRunning = !!s.running;
    const btn = document.getElementById("pull-btn");
    const ind = document.getElementById("pull-indicator");
    btn.disabled = state.pullRunning;
    // Button label mirrors the state so it reads correctly whether the
    // pull was triggered manually or fired by the scheduler.
    btn.textContent = state.pullRunning ? "Pulling…" : "Pull update";
    ind.hidden = !state.pullRunning;
    document.getElementById("next-when").textContent =
      state.nextRun ? formatLocal(state.nextRun) : "—";
    updateCountdown();

    // If a pull just finished, refresh data
    if (wasRunning && !state.pullRunning) {
      if (s.ok === false) {
        toast(`Pull failed: ${s.last_error || "see logs"}`, "bad");
      } else {
        toast("Pull complete — data refreshed", "ok");
      }
      // Reload cases/updates AND the system log — the pull lifecycle
      // emits `pull_triggered_manually` / `pull_started` / `pull_finished`
      // etc. that the System-log view should surface without needing a
      // tab-switch.
      await Promise.all([loadCases(), loadUpdates(), loadSystemLog()]);
      if (state.view === "systemlog") renderSystemLog();
    }
  } catch (e) {
    console.warn("status poll failed:", e);
  }
}

async function triggerPull() {
  const btn = document.getElementById("pull-btn");
  try {
    const res = await fetch("/api/pull", { method: "POST" });
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
    document.getElementById("pull-indicator").hidden = false;
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
  document.getElementById("summary-line").textContent =
    `${state.cases.length} cases · ${totalCaptures} snapshots` +
    (last ? ` · last ${formatLocal(new Date(last))}` : "") +
    ` · times in ${tz}`;
}

function renderCases() {
  const root = document.getElementById("case-list");
  root.innerHTML = "";
  const tmpl = document.getElementById("case-card-template");
  for (const c of state.cases) {
    const node = tmpl.content.cloneNode(true);
    const article = node.querySelector(".case-card");
    article.dataset.label = c.label;
    article.dataset.receipt = c.receiptNumber;

    article.querySelector(".case-label").textContent = c.label;
    article.querySelector(".case-receipt").textContent = c.receiptNumber;

    const meta = article.querySelector(".case-meta");
    meta.innerHTML = "";
    if (c.applicantName) {
      const a = document.createElement("span");
      a.className = "big";
      a.textContent = formatApplicant(c.applicantName);
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

    // Initial panel content
    renderPanel(article, c, active);

    root.appendChild(node);
  }
}

function switchTab(caseObj, tabId) {
  state.activeTab[caseObj.receiptNumber] = tabId;
  const article = document.querySelector(
    `.case-card[data-receipt="${CSS.escape(caseObj.receiptNumber)}"]`
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

function renderOverview(panel, c) {
  const latest = c.latest || {};
  const s = c.summary || {};
  panel.innerHTML = "";

  // --- Hero metrics: raw data signals only (no inference) ---
  const metrics = [
    {
      label: "Days pending",
      value: s.daysPending ?? "—",
      sub: latest.submissionDate ? `since ${formatDate(latest.submissionDate)}` : "",
      tone: "",
    },
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
      label: "All updates",
      value: s.allUpdates ?? 0,
      sub: "",
      tone: "",
    },
    {
      label: "Silent updates",
      value: s.silentUpdates ?? 0,
      sub: "updates without event",
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

  // --- Factual callouts: ONLY things pulled verbatim from USCIS fields. ---
  // Anything derived or community-interpreted belongs in the Inferred block
  // at the end of the overview, not here.
  const factCallouts = document.createElement("div");
  factCallouts.className = "callouts";

  if (latest.actionRequired || (s.evidenceRequestCount ?? 0) > 0) {
    factCallouts.appendChild(
      callout(
        "bad",
        "Action required",
        (s.evidenceRequestCount ?? 0) > 0
          ? `evidenceRequests has ${s.evidenceRequestCount} entr${s.evidenceRequestCount === 1 ? "y" : "ies"} — an Request for Evidence / Notice of Intent to Deny has been issued. Check the raw JSON.`
          : "USCIS set actionRequired — look for a Request for Evidence or similar in the notices."
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

function _renderSubFacts(c, latest) {
  const sub = document.createElement("div");
  sub.className = "sub-facts";
  const reps = latest.representativeName ? formatApplicant(latest.representativeName) : "";
  const facts = [
    { k: "Submitted",      v: latest.submissionDate ? formatDate(latest.submissionDate) : "—" },
    { k: "Channel",        v: latest.elisChannelType || "—" },
    { k: "Representative", v: reps || "—" },
    { k: "Snapshots",      v: `${c.captures ?? 0}` },
    { k: "Days logged",    v: `${c.days ?? 0}` },
    { k: "Last pulled",    v: c.capturedAt ? formatLocalDateTime(c.capturedAt) : "—", mono: true },
  ];
  for (const f of facts) {
    const el = document.createElement("div");
    el.className = "sub-fact";
    el.innerHTML =
      `<span class="sub-fact-k">${escapeHtml(f.k)}</span>` +
      `<span class="sub-fact-v${f.mono ? " mono" : ""}">${escapeHtml(String(f.v))}</span>`;
    sub.appendChild(el);
  }
  return sub;
}

// ---------- changes ----------

function renderChanges(panel, c) {
  panel.innerHTML = "";
  const hist = state.histories[c.label];
  const changes = (hist && hist.changes) || [];
  if (!changes.length) {
    panel.innerHTML = `<div class="no-changes">No differences detected between day-binned captures.</div>`;
    return;
  }
  // Show newest first
  for (const ch of [...changes].reverse()) {
    panel.appendChild(renderChangeBlock(ch));
  }
}

const KIND_INFO = {
  silent_update:  { label: "silent update",  tone: "silent",
                 desc: "updatedAt date advanced; no visible event or notice." },
  same_day_refresh: { label: "same-day re-stamp", tone: "silent",
                 desc: "updatedAtTimestamp moved within the same day — usually a sync artifact." },
  event:       { label: "new event",      tone: "ok" },
  notice:      { label: "new notice",     tone: "warn" },
  appointment: { label: "appointment",    tone: "warn" },
  decision:    { label: "decision flag",  tone: "ok" },
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
      `<span class="change-range">${escapeHtml(formatDate(ch.from))} ` +
      `<span class="change-arrow">→</span> ${escapeHtml(formatDate(ch.to))}</span>` +
      kindTag +
    `</div>`;

  if (ch.scalars && Object.keys(ch.scalars).length) {
    const sec = document.createElement("div");
    sec.className = "change-section";
    sec.innerHTML = `<h5>Field changes</h5>`;
    for (const [k, v] of Object.entries(ch.scalars)) {
      const row = document.createElement("div");
      row.className = "change-scalar";
      row.innerHTML =
        `<span class="field">${escapeHtml(k)}</span>` +
        `<span class="from">${escapeHtml(formatValue(v.from))}</span>` +
        `<span class="to">${escapeHtml(formatValue(v.to))}</span>`;
      sec.appendChild(row);
    }
    block.appendChild(sec);
  }

  const collections = [
    ["events", "Events"],
    ["notices", "Notices"],
    ["documents", "Documents"],
    ["addendums", "Addendums"],
  ];
  for (const [key, title] of collections) {
    const c = ch[key] || {};
    if (!(c.added?.length || c.removed?.length)) continue;
    const sec = document.createElement("div");
    sec.className = "change-section";
    sec.innerHTML = `<h5>${title}</h5>`;
    for (const a of c.added || []) {
      const chip = document.createElement("span");
      chip.className = "change-item-added";
      chip.textContent = "+ " + describeItem(key, a);
      sec.appendChild(chip);
    }
    for (const r of c.removed || []) {
      const chip = document.createElement("span");
      chip.className = "change-item-removed";
      chip.textContent = "− " + describeItem(key, r);
      sec.appendChild(chip);
    }
    block.appendChild(sec);
  }
  return block;
}

function describeItem(kind, obj) {
  if (kind === "events") {
    const code = obj.eventCode || "?";
    const caption = state.eventCodeLabels[code];
    const when = obj.eventDateTime ? formatDate(obj.eventDateTime) : "—";
    return caption ? `${code} (${caption}) @ ${when}` : `${code} @ ${when}`;
  }
  if (kind === "notices") {
    const appt = obj.appointmentDateTime
      ? ` (appt ${formatLocalDateTime(obj.appointmentDateTime)})`
      : "";
    return `${obj.actionType || "?"} — letter ${obj.letterId || "?"}${appt}`;
  }
  return JSON.stringify(obj);
}

// ---------- updates feed ----------

// ---------- system log view ----------

// Well-known events and their visual tone. Any event not listed falls
// through to "info" (or "error"/"warning" if the entry carries that level).
const SYSTEMLOG_EVENT_INFO = {
  server_startup:              { tone: "info",  label: "Server started" },
  scheduler_configured:        { tone: "info",  label: "Scheduler configured" },
  pull_triggered_manually:     { tone: "info",  label: "Pull triggered (manual)" },
  pull_started:                { tone: "info",  label: "Pull started" },
  pull_finished:               { tone: "ok",    label: "Pull finished" },
  pull_failed:                 { tone: "bad",   label: "Pull failed" },
  pull_timeout:                { tone: "bad",   label: "Pull timed out" },
  pull_crashed:                { tone: "bad",   label: "Pull crashed" },
  pull_skipped_already_running:{ tone: "warn",  label: "Pull skipped (already running)" },
  notify_sent:                 { tone: "ok",    label: "Notification sent" },
  notify_skipped:              { tone: "warn",  label: "Notification skipped" },
  notify_failed:               { tone: "bad",   label: "Notification failed" },
  notify_dispatcher_crashed:   { tone: "bad",   label: "Notification dispatcher crashed" },
  cli_run_start:               { tone: "info",  label: "session_fetch: run start" },
  cli_run_finished:            { tone: "ok",    label: "session_fetch: run finished" },
  cli_run_no_cases:            { tone: "bad",   label: "session_fetch: no cases configured" },
  case_fetch_start:            { tone: "info",  label: "Case fetch start" },
  case_fetch_api_error:        { tone: "bad",   label: "Case fetch API error" },
  case_fetch_session_expired:  { tone: "warn",  label: "Case fetch session expired" },
  snapshot_appended:           { tone: "ok",    label: "Snapshot appended" },
  snapshot_append_failed:      { tone: "bad",   label: "Snapshot append failed" },
};

function _eventInfo(entry) {
  const known = SYSTEMLOG_EVENT_INFO[entry.event];
  if (known) return known;
  // Unknown event — fall back on the entry's own level.
  const tone = entry.level === "error" ? "bad"
    : entry.level === "warning" ? "warn"
    : "info";
  return { tone, label: entry.event };
}

function renderSystemLog() {
  const root = document.getElementById("systemlog-feed");
  root.innerHTML = "";

  if (!state.systemLog.length) {
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

  const head = document.createElement("div");
  head.className = "updates-head syslog-head-row";
  head.innerHTML =
    `<div>` +
      `<h2>System log</h2>` +
      `<div class="updates-sub">` +
        `${state.systemLog.length} event${state.systemLog.length === 1 ? "" : "s"} · ` +
        `Persisted to <code>data/system_log.json</code>. Newest first.` +
      `</div>` +
    `</div>`;
  head.appendChild(renderSystemLogControls());
  root.appendChild(head);

  // Newest first.
  const rowsNewestFirst = [...state.systemLog].reverse();
  for (const e of rowsNewestFirst) {
    root.appendChild(renderSystemLogRow(e));
  }
}

// Render the two System-log tab controls: "Export log" (one-click download
// of the current log as JSON) and "Clear log" (two-step destructive wipe).
// Both are deliberately scoped to the System log view so they can't be
// confused with the "Export data" button in the topbar (which exports
// cases only — not the log).
function renderSystemLogControls() {
  const wrap = document.createElement("div");
  wrap.className = "syslog-controls";

  const exportBtn = document.createElement("a");
  exportBtn.href = "/api/system-log/export";
  exportBtn.className = "syslog-export-btn";
  exportBtn.textContent = "Export log";
  exportBtn.title = "Download this log as JSON";

  wrap.appendChild(exportBtn);
  wrap.appendChild(renderClearLogControl());
  return wrap;
}

// Two-step destructive flow for wiping the system log.
//
// Step 1 — user clicks "Clear log" in the System log tab header. That
//   click opens a fixed-position confirmation DIALOG (overlay + centered
//   modal) layered above the page. Because the dialog is position:fixed
//   it does not reflow the underlying layout — the event list, the
//   other controls, and the rest of the page stay exactly where they
//   were.
//
// Step 2 — user clicks the red "Yes, delete all events" action inside
//   the dialog. That is the only control that POSTs
//   /api/system-log/clear. The server ALSO requires {"confirm": true}
//   in the body as a second gate. Cancel, Escape, and backdrop-click
//   all close the dialog safely.
function renderClearLogControl() {
  const wrap = document.createElement("div");
  wrap.className = "clear-log-control";

  const idle = document.createElement("button");
  idle.type = "button";
  idle.className = "clear-log-btn";
  idle.textContent = "Clear log";
  idle.title = "Permanently delete every event in this log";
  idle.addEventListener("click", openClearLogDialog);

  wrap.appendChild(idle);
  return wrap;
}

function openClearLogDialog() {
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
        `<p><strong>This is irreversible.</strong> The log is the only ` +
        `record of scheduler fires, pull failures, and notification ` +
        `history. Clearing it will destroy the audit trail used to ` +
        `debug silent failures — missed pulls, MFA errors, email ` +
        `delivery issues.</p>` +
        `<p class="modal-hint">If you might need the log later, click ` +
        `<em>Export log</em> first.</p>` +
      `</div>` +
      `<div class="modal-actions">` +
        `<button type="button" class="modal-btn modal-btn-cancel">Cancel</button>` +
        `<button type="button" class="modal-btn modal-btn-danger">Yes, delete all events</button>` +
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
      const res = await fetch("/api/system-log/clear", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirm: true }),
      });
      if (!res.ok) throw new Error(`status ${res.status}`);
      await loadSystemLog();
      renderSystemLog();
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

function renderSystemLogRow(entry) {
  const info = _eventInfo(entry);
  const block = document.createElement("div");
  block.className = `change-block syslog-block syslog-${info.tone}`;

  // Detail fields = everything that isn't the skeleton (ts, event, level, pid, source).
  const skeleton = new Set(["ts", "event", "level", "pid", "source"]);
  const detailKeys = Object.keys(entry).filter(k => !skeleton.has(k));

  const when = formatLocalDateTime(new Date(entry.ts), { withSeconds: true });
  const sourceTag = entry.source
    ? `<span class="syslog-source">${escapeHtml(entry.source)}</span>`
    : "";

  let detailsHtml = "";
  if (detailKeys.length) {
    detailsHtml = `<div class="syslog-details">` +
      detailKeys.map(k => {
        const v = entry[k];
        const shown = typeof v === "object" ? JSON.stringify(v) : String(v);
        return (
          `<div class="syslog-detail">` +
            `<span class="syslog-detail-k">${escapeHtml(k)}</span>` +
            `<span class="syslog-detail-v">${escapeHtml(shown)}</span>` +
          `</div>`
        );
      }).join("") +
      `</div>`;
  }

  block.innerHTML =
    `<div class="syslog-head">` +
      `<span class="kind-tag kind-${info.tone}">${escapeHtml(info.label)}</span>` +
      sourceTag +
      `<span class="syslog-ts">${escapeHtml(when)}</span>` +
      `<span class="syslog-event">${escapeHtml(entry.event)}</span>` +
    `</div>` +
    detailsHtml;

  return block;
}

function renderUpdates() {
  const root = document.getElementById("updates-feed");
  root.innerHTML = "";
  if (!state.updates.length) {
    root.innerHTML =
      `<div class="updates-empty">` +
      `<h3>No updates yet.</h3>` +
      `<p>An update is created whenever a pull discovers something new: ` +
      `a silent update, a new event code, an appointment change, a decision flag flip, ` +
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
  const detected = u.detectedOn || (u.to || "").slice(0, 10);
  const real = u.realUpdateDate;
  const detectedDisplay = formatDate(detected);
  const realDisplay = real ? formatDate(real) : "";
  const dateLine = realDisplay && realDisplay !== detectedDisplay
    ? `Detected ${detectedDisplay} · Update date ${realDisplay}`
    : `Detected ${detectedDisplay}`;

  const block = document.createElement("article");
  block.className = "update-record";
  block.dataset.id = u.id || "";
  block.innerHTML =
    `<header class="update-head">` +
      `<div class="update-head-left">` +
        `<span class="update-case">${escapeHtml(u.caseLabel || "?")}</span>` +
        `<span class="update-receipt">${escapeHtml(u.receiptNumber || "")}</span>` +
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
      row.innerHTML =
        `<span class="field">${escapeHtml(k)}</span>` +
        `<span class="from">${escapeHtml(formatValue(v.from))}</span>` +
        `<span class="to">${escapeHtml(formatValue(v.to))}</span>`;
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
      chip.textContent = "+ " + describeItem(key, a);
      sec.appendChild(chip);
    }
    for (const r of coll.removed || []) {
      const chip = document.createElement("span");
      chip.className = "change-item-removed";
      chip.textContent = "− " + describeItem(key, r);
      sec.appendChild(chip);
    }
    block.appendChild(sec);
  }

  return block;
}

// Factual combined timeline: every USCIS event on the case plus every
// silent update we've detected, merged chronologically (newest first).
// Date + code only — no interpretation, no community folklore, no stage
// inference. Form-agnostic: works the same for I-140, I-485, I-765, I-131…
function renderObservedEventCodes(c) {
  const section = document.createElement("section");
  section.className = "events-section";

  const heading = document.createElement("h4");
  heading.className = "events-heading";
  heading.textContent = "Timeline";
  section.appendChild(heading);

  // 1. Raw events from the latest snapshot.
  const events = Array.isArray((c.latest || {}).events) ? c.latest.events : [];
  const rows = events.map(e => ({
    date: (e.eventDateTime || e.createdAt || "").slice(0, 10) || "—",
    code: e.eventCode || "?",
  }));

  // 2. Silent updates from the diff history. Each one is an `updatedAt`
  // bump USCIS made with no event / notice change — surface it as its
  // own row dated by the advanced updatedAt value.
  const hist = state.histories[c.label];
  for (const ch of (hist && hist.changes) || []) {
    if (ch.kind !== "silent_update") continue;
    const when = (ch.scalars?.updatedAt?.to || ch.to || "").slice(0, 10) || "—";
    rows.push({ date: when, code: "silent update", silent: true });
  }

  if (!rows.length) {
    const empty = document.createElement("div");
    empty.className = "no-changes";
    empty.textContent = "No activity observed yet.";
    section.appendChild(empty);
    return section;
  }

  // Newest first.
  rows.sort((a, b) => b.date.localeCompare(a.date));

  const list = document.createElement("ul");
  list.className = "events-list";
  for (const r of rows) {
    const item = document.createElement("li");
    item.className = "events-item";
    item.innerHTML =
      `<span class="events-date">${escapeHtml(formatDate(r.date))}</span>` +
      `<span class="events-code${r.silent ? " events-code-silent" : ""}">${escapeHtml(r.code)}</span>`;
    list.appendChild(item);
  }
  section.appendChild(list);
  return section;
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

function renderRaw(panel, c) {
  panel.innerHTML = "";
  const hist = state.histories[c.label];
  const entries = (hist && hist.entries) || [];
  if (!entries.length) {
    panel.innerHTML = `<div class="no-changes">No captures yet.</div>`;
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
  const current =
    state.rawSelection[c.receiptNumber] ||
    entries[entries.length - 1].capturedAt;
  select.value = current;
  state.rawSelection[c.receiptNumber] = current;
  select.addEventListener("change", () => {
    state.rawSelection[c.receiptNumber] = select.value;
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
    const num = (c.label || "").match(/(\d+)/);
    const fname = num ? `${num[1]}_logs.json` : `${c.receiptNumber}.json`;
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
    const ca = state.rawSelection[c.receiptNumber];
    const entry = entries.find(e => e.capturedAt === ca) || entries[entries.length - 1];
    const text = JSON.stringify(entry, null, 2);
    const ok = await copyToClipboard(text);
    copyOne.textContent = ok ? "Copied ✓" : "Copy failed";
    copyOne.classList.toggle("raw-btn-copied", ok);
    setTimeout(() => {
      copyOne.textContent = COPY_IDLE_LABEL;
      copyOne.classList.remove("raw-btn-copied");
    }, 1600);
  });
  actions.appendChild(copyOne);

  controls.appendChild(actions);
  panel.appendChild(controls);

  const pre = document.createElement("pre");
  pre.className = "raw";
  panel.appendChild(pre);
  updateRawBody();

  function updateRawBody() {
    const ca = state.rawSelection[c.receiptNumber];
    const entry = entries.find(e => e.capturedAt === ca) || entries[entries.length - 1];
    // 4-space indent + syntax highlight — written as HTML so colours work.
    pre.innerHTML = highlightJson(JSON.stringify(entry, null, 4));
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
