// Copyright (C) 2026 the USCIS Prober contributors
// SPDX-License-Identifier: AGPL-3.0-or-later
// USCIS Prober UI — vanilla JS, no framework.

const state = {
  cases: [],
  histories: {},           // label → history payload
  activeTab: {},           // receiptNumber → tab id
  rawSource: {},           // receiptNumber → "case" | "location" (raw sub-tab)
  rawSelection: {},        // "{receipt}:{source}" → capturedAt for raw view
  nextRun: null,
  pullRunning: false,
  eventCodeLabels: {},     // e.g. { FTA0: "Database checks received..." }
  view: "cases",           // "cases" | "updates" | "systemlog"
  updates: [],             // flat diff feed
  systemLog: [],           // current page of the event log (oldest-first, as returned by server)
  systemLogTotal: 0,       // total entries on disk (all pages combined)
  systemLogPage: 1,        // 1-indexed page (page 1 = newest)
  systemLogPageSize: 100,
  versionSha: null,        // last-seen short SHA from /api/pull/status
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

  // Primary chip text: the date-time label (`2026-04-22.2032`) if we
  // have it. Lexicographic comparison between two labels tells the
  // operator which is newer — e.g. `2026-04-23.1105` > `2026-04-22.2032`.
  // Falls back to short SHA on a box without git.
  chip.textContent = label || sha;
  chip.hidden = false;
  chip.title = [
    label ? `Version:  ${label} (UTC)` : "",
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


async function pollPullStatus() {
  try {
    const res = await fetch("/api/pull/status");
    const s = await res.json();
    state.nextRun = s.next_run ? new Date(s.next_run) : null;
    if (s.version) updateVersionChip(s.version);
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

  // --- Current location (dedicated row so it's visible at a glance). ---
  panel.appendChild(renderLocationRow(c));

  // --- Secondary facts: a compact strip, no duplication with header ---
  panel.appendChild(_renderSubFacts(c, latest));

  // --- Observed event codes (factual only — no inference, no stage
  // guessing, no community folklore; form-agnostic). ---
  panel.appendChild(renderObservedEventCodes(c));
}

// Renders "Current location: <SCD — 147-C9>" or a "TBD" badge + info popover
// explaining what TBD means. The popover mirrors the existing `#export-info-*`
// pattern (wireInfoBadge) so styling stays consistent with the header.
function renderLocationRow(c) {
  const loc = c.location || {};
  const info = loc.info;
  const wrap = document.createElement("div");
  wrap.className = "location-row";

  const k = document.createElement("span");
  k.className = "location-key";
  k.textContent = "Current location";
  wrap.appendChild(k);

  const val = document.createElement("span");
  val.className = "location-val";

  if (info && (info.location || info.subtype || info.form)) {
    const parts = [];
    if (info.location) parts.push(info.location);
    if (info.subtype)  parts.push(info.subtype);
    val.textContent = parts.join(" · ");
    wrap.appendChild(val);

    // Small meta (form / receipt date) as faded sub-text.
    if (info.receipt_date || info.form) {
      const sub = document.createElement("span");
      sub.className = "location-sub";
      const bits = [];
      if (info.form) bits.push(info.form);
      if (info.receipt_date) {
        // receipt_date comes back as RFC 2822 ("Fri, 20 Feb 2026 00:00:00 GMT"),
        // which formatDate can't parse on its own — wrap it in a Date first.
        const parsed = new Date(info.receipt_date);
        const rendered = isNaN(parsed.getTime())
          ? info.receipt_date
          : formatDate(parsed);
        bits.push(`received ${rendered}`);
      }
      sub.textContent = bits.join(" · ");
      wrap.appendChild(sub);
    }
  } else {
    const tbd = document.createElement("span");
    tbd.className = "location-tbd";
    tbd.textContent = "TBD";
    wrap.appendChild(tbd);

    // Info popover: USCIS returned a null payload (hasn't assigned a
    // service center yet) — explain *why* so "TBD" is never mysterious.
    const group = document.createElement("span");
    group.className = "location-info-group";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "info-badge";
    btn.textContent = "i";
    btn.setAttribute("aria-label", "What does TBD mean?");
    btn.setAttribute("aria-expanded", "false");
    const pop = document.createElement("div");
    // Left-anchored variant — the badge sits on the left side of the card,
    // so the default right-anchor would push the popover off-viewport.
    pop.className = "info-popover popover-left";
    pop.hidden = true;
    pop.setAttribute("role", "tooltip");
    const capturedAt = loc.capturedAt
      ? formatLocalDateTime(loc.capturedAt)
      : null;
    pop.innerHTML =
      `<strong>No location available yet.</strong>` +
      `USCIS's location endpoint returned <code>{"data": null}</code> — typically ` +
      `this means a service center hasn't been assigned to this case yet. ` +
      `I-765 (EAD) cases are usually populated first; I-485 and I-131 tend ` +
      `to stay null until later in the pipeline.` +
      (capturedAt ? `<br/><br/>Last checked: ${escapeHtml(capturedAt)}.` : "") +
      (loc.captures
        ? ` <span class="muted">(${loc.captures} snapshot${loc.captures === 1 ? "" : "s"} on file.)</span>`
        : "");
    wireInfoPopover(btn, pop);
    group.appendChild(btn);
    group.appendChild(pop);
    wrap.appendChild(group);
  }

  return wrap;
}

// Generic info-badge / popover wiring. Mirrors #export-info-btn behaviour
// so the UX is consistent: click toggles, outside click or Escape dismisses.
function wireInfoPopover(btn, pop) {
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
  document.addEventListener("click", e => {
    if (!pop.hidden && !pop.contains(e.target) && e.target !== btn) close();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape") close();
  });
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
  location_assigned: { label: "location assigned", tone: "ok",
                 desc: "USCIS assigned a service center to this case — location API flipped from null to populated." },
  location_changed:  { label: "location changed",  tone: "warn",
                 desc: "Service center, subtype, or receipt-level field on the location API changed." },
  location_cleared:  { label: "location cleared",  tone: "warn",
                 desc: "Location API stopped returning receipt_details — rare; may indicate a reassignment in flight." },
};


function renderChangeBlock(ch) {
  const block = document.createElement("div");
  block.className = `change-block${ch.source === "location" ? " change-block-location" : ""}`;
  const info = KIND_INFO[ch.kind] || KIND_INFO.status;
  const kindTag =
    `<span class="kind-tag kind-${info.tone || "n"}" ` +
    `title="${escapeHtml(info.desc || info.label)}">${escapeHtml(info.label)}</span>`;
  // Badge marks which USCIS endpoint generated this diff. Case diffs are
  // the common case, so the badge only renders for location entries to
  // keep the timeline visually quiet.
  const sourceBadge = ch.source === "location"
    ? `<span class="source-badge source-location" title="From the location API (/receipt_info)">Location API</span>`
    : "";
  block.innerHTML =
    `<div class="change-block-head">` +
      // `from` / `to` are the full ISO capturedAt timestamps of the LAST
      // pull on each day-binned side — show the wall-clock time so the
      // operator can correlate a diff with the specific pull that saw it.
      `<span class="change-range">${escapeHtml(formatLocalDateTime(ch.from))} ` +
      `<span class="change-arrow">→</span> ${escapeHtml(formatLocalDateTime(ch.to))}</span>` +
      sourceBadge +
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
// Well-known events and their visual tone. Paired case/location events
// deliberately share the same tone so the operator sees them as a matching
// family in the expanded pull row — "case snapshot appended" (ok) should
// look identical to "location snapshot appended" (ok), etc.
const SYSTEMLOG_EVENT_INFO = {
  // Top-level envelope + server lifecycle
  pull:                        { tone: "info",  label: "Pull" },
  server_startup:              { tone: "info",  label: "Server started" },
  scheduler_configured:        { tone: "info",  label: "Scheduler configured" },
  pull_skipped_already_running:{ tone: "warn",  label: "Pull skipped (already running)" },
  system_log_cleared:          { tone: "info",  label: "System log cleared" },

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

  // Case-API events (paired with location-API events below — same tones)
  case_fetch_start:            { tone: "info",  label: "Case fetch start" },
  case_fetch_api_error:        { tone: "bad",   label: "Case fetch API error" },
  case_fetch_session_expired:  { tone: "warn",  label: "Case fetch session expired" },
  case_snapshot_appended:      { tone: "ok",    label: "Case snapshot appended" },
  case_snapshot_append_failed: { tone: "bad",   label: "Case snapshot append failed" },

  // Location-API events (mirror of the case-API events above)
  location_fetch_failed:       { tone: "warn",  label: "Location fetch failed" },
  location_fetch_session_expired: { tone: "warn", label: "Location fetch session expired" },
  location_snapshot_appended:  { tone: "ok",    label: "Location snapshot appended" },
  location_snapshot_append_failed: { tone: "warn", label: "Location snapshot append failed" },
  location_post_fetch_rewarm_failed: { tone: "warn", label: "Location post-fetch dashboard rewarm failed" },

  // Generic, used by both case + location paths via _append_to_log_file
  snapshot_log_not_array:      { tone: "warn",  label: "Snapshot log wasn't an array" },
  snapshot_log_invalid_json:   { tone: "warn",  label: "Snapshot log was malformed JSON" },
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
  const root = document.getElementById("systemlog-feed");
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

  const head = document.createElement("div");
  head.className = "updates-head syslog-head-row";
  head.innerHTML =
    `<div>` +
      `<h2>System log</h2>` +
      `<div class="updates-sub">` +
        `${escapeHtml(countLine)} · ` +
        `Persisted to <code>data/system_log.json</code>. Newest first.` +
      `</div>` +
    `</div>`;
  head.appendChild(renderSystemLogControls());
  root.appendChild(head);

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

function _renderFlatSystemLogRow(entry) {
  const info = _eventInfo(entry);
  const block = document.createElement("div");
  block.className = `change-block syslog-block syslog-${info.tone}`;

  const detailKeys = Object.keys(entry).filter(k => !_SYSLOG_SKELETON.has(k));
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

  // `.syslog-disclosure-spacer` is an invisible placeholder that reserves
  // the same width as the real disclosure triangle on nested entries, so
  // the pill column lines up flat-vs-nested without needing grid layout.
  block.innerHTML =
    `<div class="syslog-head">` +
      `<span class="syslog-disclosure-spacer" aria-hidden="true"></span>` +
      `<span class="kind-tag kind-${info.tone}">${escapeHtml(info.label)}</span>` +
      sourceTag +
      `<span class="syslog-ts">${escapeHtml(when)}</span>` +
      `<span class="syslog-event">${escapeHtml(entry.event)}</span>` +
    `</div>` +
    detailsHtml;

  return block;
}

function _renderNestedSystemLogRow(entry) {
  // Top-level tone takes the worst severity between the envelope itself
  // and every nested step, so a pull that looks info-level but contains
  // an error step still visibly surfaces as red at a glance.
  const topLevel = _worstLevelAcross([entry, ...entry.steps]);
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
  // "case" and "location" snapshot counters are rendered as a paired
  // pair (`3 case · 3 location`) when both are present, so the summary
  // visibly balances the two APIs instead of treating one as primary.
  if (typeof summary.case_snapshots === "number")
    bits.push(`${summary.case_snapshots} case`);
  if (typeof summary.location_snapshots === "number")
    bits.push(`${summary.location_snapshots} location`);
  if (summary.new_diffs_emailed)
    bits.push(`${summary.new_diffs_emailed} email${summary.new_diffs_emailed === 1 ? "" : "s"}`);
  if (summary.case_fetch_failures)
    bits.push(`${summary.case_fetch_failures} case-fail`);
  if (summary.location_fetch_failures)
    bits.push(`${summary.location_fetch_failures} location-fail`);
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

  // Disclosure: toggled by clicking the header. Default collapsed — a
  // pull with 17 steps would otherwise swamp the feed when paged through.
  const disclosureId = `syslog-steps-${Math.random().toString(36).slice(2, 8)}`;

  block.innerHTML =
    `<button type="button" class="syslog-head syslog-head-expandable"` +
        ` aria-expanded="false" aria-controls="${disclosureId}">` +
      `<span class="syslog-disclosure" aria-hidden="true">▶</span>` +
      `<span class="kind-tag kind-${tone}">${escapeHtml(info.label)}</span>` +
      sourceTag +
      `<span class="syslog-ts">${escapeHtml(when)}</span>` +
      `<span class="syslog-event syslog-event-envelope">${escapeHtml(entry.event)}</span>` +
      summaryLine +
    `</button>` +
    envelopeKvHtml +
    `<div class="syslog-steps" id="${disclosureId}" hidden></div>`;

  // Populate the expandable body lazily — cheap, but keeps the first
  // paint light for pages with many nested rows.
  const stepsContainer = block.querySelector(".syslog-steps");
  for (const step of entry.steps) {
    stepsContainer.appendChild(_renderNestedStepRow(step));
  }

  // Wire the disclosure toggle.
  const headBtn = block.querySelector(".syslog-head-expandable");
  const triangle = block.querySelector(".syslog-disclosure");
  headBtn.addEventListener("click", () => {
    const isOpen = !stepsContainer.hidden;
    stepsContainer.hidden = isOpen;
    headBtn.setAttribute("aria-expanded", String(!isOpen));
    triangle.textContent = isOpen ? "▶" : "▼";
  });

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
function _detailKvHtml(k, v) {
  const shown = typeof v === "object" && v !== null ? JSON.stringify(v) : String(v);
  return (
    `<div class="syslog-detail">` +
      `<span class="syslog-detail-k">${escapeHtml(k)}</span>` +
      `<span class="syslog-detail-v">${escapeHtml(shown)}</span>` +
    `</div>`
  );
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
  // Prefer the full capturedAt timestamp (`u.to`) for "Detected" so the
  // operator sees the wall-clock time of the pull that spotted this diff.
  // `detectedOn` (YYYY-MM-DD) is the legacy day-only field — kept as the
  // deduplication key but not used for display when `to` is present.
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
  block.className = `update-record${u.source === "location" ? " update-record-location" : ""}`;
  block.dataset.id = u.id || "";
  const sourceBadge = u.source === "location"
    ? `<span class="source-badge source-location" title="From the location API (/receipt_info)">Location API</span>`
    : "";
  block.innerHTML =
    `<header class="update-head">` +
      `<div class="update-head-left">` +
        `<span class="update-case">${escapeHtml(u.caseLabel || "?")}</span>` +
        `<span class="update-receipt">${escapeHtml(u.receiptNumber || "")}</span>` +
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

const RAW_SOURCES = {
  case: {
    label: "Case API",
    historyKey: "entries",
    fileSuffix: "_case.json",
    emptyMsg: "No captures yet.",
  },
  location: {
    label: "Location API",
    historyKey: "locationEntries",
    fileSuffix: "_location.json",
    emptyMsg:
      "No location snapshots yet — the next pull will start recording this endpoint.",
  },
};

function renderRaw(panel, c) {
  panel.innerHTML = "";
  const hist = state.histories[c.label] || {};

  // Sub-tab nav. Default to "case" but remember the user's choice per card.
  const activeSource = state.rawSource[c.receiptNumber] || "case";
  state.rawSource[c.receiptNumber] = activeSource;

  const nav = document.createElement("nav");
  nav.className = "raw-sources";
  nav.setAttribute("role", "tablist");
  for (const [id, meta] of Object.entries(RAW_SOURCES)) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `raw-source${id === activeSource ? " active" : ""}`;
    btn.dataset.source = id;
    const count = (hist[meta.historyKey] || []).length;
    btn.textContent = count ? `${meta.label} (${count})` : meta.label;
    btn.addEventListener("click", () => {
      state.rawSource[c.receiptNumber] = id;
      renderRaw(panel, c);
    });
    nav.appendChild(btn);
  }
  panel.appendChild(nav);

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
    const ca = state.rawSelection[selKey];
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
    const ca = state.rawSelection[selKey];
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
