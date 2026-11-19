import { state } from "./state.js";
import { emptyState, esc, pageHeading, pretty, reasonText, setInner, stampSeconds } from "./format.js";
import { malformed, tradeGet } from "./api.js";

// ---------------------------------------------------------------- shadow evaluation (P6.1)
// The #analytics route shows the background shadow evaluation and its signal-frequency
// diagnostics. Read-only: the only control enables or disables the scheduler (a persisted,
// audited preference, not an execution permission). Loaded on demand; the scheduler itself
// runs in the backend whether or not this page is open.
export const SHADOW_BANNER = "SHADOW MODE — READ-ONLY — NO ORDERS WILL BE PLACED";
export const SHADOW_TONE = { COMPLETED: "verified", SKIPPED: "unverified", MISSED: "unverified", FAILED: "invalid", PENDING: "not_applicable", RUNNING: "not_applicable", APPROVED: "verified", REJECTED: "invalid", UNVERIFIED: "unverified" };
export const shadowChip = (value, field) => `<span class="field-chip ${SHADOW_TONE[value] ?? ""}" data-shadow-chip="${esc(field)}">${esc(pretty(value))}</span>`;
export const shadowStamp = (value) => (value ? stampSeconds(value) : "—");
export const countText = (value) => (value === null || value === undefined ? "Not available" : String(value));

export function shadowRows(rows) {
  return `<div class="info-list">${rows.map(([label, value, id]) => `<div class="info-row"${id ? ` id="${id}"` : ""}><span>${esc(label)}</span><strong>${esc(value ?? "—")}</strong></div>`).join("")}</div>`;
}

export function shadowSchedulerMarkup(status) {
  if (!status) return emptyState("Loading", "Waiting for the shadow evaluation status.");
  const enabled = status.enabled === true;
  const counts = status.cycle_counts ?? {};
  return `${shadowRows([
    ["Scheduler", `${enabled ? "Enabled" : status.enabled === false ? "Disabled" : "Unavailable"} · ${pretty(status.state)}`, "shadow-state"],
    ["Last pass", shadowStamp(status.last_pass_utc)],
    ["Last completed evaluation", shadowStamp(status.last_completed_utc), "shadow-last-completed"],
    ["Next scheduled evaluation", enabled ? shadowStamp(status.next_scheduled_utc) : "— (disabled)", "shadow-next"],
    ["Service health", `${String(status.health ?? "unavailable").toUpperCase()}${status.health_reason ? ` · ${status.health_reason}` : ""}${status.consecutive_errors ? ` (${status.consecutive_errors} consecutive errors)` : ""}${status.last_error ? ` · last error ${status.last_error} at ${shadowStamp(status.last_error_utc)}` : ""}`, "shadow-health"],
    ["Cycles", ["COMPLETED", "SKIPPED", "MISSED", "FAILED", "PENDING", "RUNNING"].map((s) => `${pretty(s).toLowerCase()} ${counts[s] ?? 0}`).join(" · "), "shadow-cycle-counts"],
    ["Scope", `${(status.scope?.instruments ?? []).join(", ")} · ${(status.scope?.timeframes ?? []).join(", ")}`],
  ])}<div class="panel-note account-note" id="shadow-scheduler-readonly">Read-only. Turning the background scheduler on or off requires separate approval; this application has no control for it.</div>
    <div class="panel-note account-note">Evaluates each closed candle about ${esc(status.schedule?.start_delay_seconds ?? 25)} s after it closes, on the broker server clock. The enabled state is saved in the journal and survives backend restarts. It never places, modifies or closes orders.</div>`;
}

export function shadowDiagnosticsMarkup(diag) {
  if (!diag) return "";
  const pops = diag.populations;
  const risk = diag.risk;
  const reasonRows = (items, label) => items.length
    ? `<table class="data-table"><thead><tr><th>${label}</th><th>Result</th><th class="num">Count</th></tr></thead><tbody>${items.map((r) => `<tr><td>${esc(r.gate ?? `${pretty(r.status)} · ${r.reason_code}`)}</td><td>${esc(r.result ?? pretty(r.status))}</td><td class="num">${esc(r.count)}</td></tr>`).join("")}</tbody></table>`
    : emptyState("None recorded", "No entries yet.");
  const skipRows = diag.skip_reasons.map((r) => ({ status: r.status, reason_code: r.reason_code, count: r.count }));
  const matrix = diag.by_instrument_timeframe.map((m) => `<tr><td>${esc(m.symbol)}c</td><td>${esc(m.timeframe)}</td><td class="num">${esc(m.windows)}</td><td class="num">${esc(m.completed)}</td><td class="num">${esc(m.skipped)}</td><td class="num">${esc(m.missed)}</td><td class="num">${esc(m.failed)}</td><td class="num">${esc(m.candidates)}</td><td class="num">${esc(m.risk_approved)}</td></tr>`).join("");
  return `<div class="account-grid">
      <section class="panel"><div class="panel-head"><span class="panel-title">Populations</span><span class="panel-meta">KEPT SEPARATE</span></div>${shadowRows([
        ["Evaluation windows", pops.evaluation_windows, "shadow-windows"],
        ["Completed evaluations", pops.completed_evaluations, "shadow-completed"],
        ["Market candidates", pops.market_candidates, "shadow-candidates"],
        ["Candidates per 100 completed", diag.candidate_rate_per_100_completed ?? "Not available (no completed evaluations)", "shadow-rate"],
        ["Risk proposals", pops.risk_proposals],
        ["Risk-approved proposals", pops.risk_approved_proposals, "shadow-approved"],
        ["Executable opportunities", countText(pops.executable_opportunities), "shadow-executable"],
        ["Actual trades", countText(pops.actual_trades), "shadow-trades"],
      ])}<div class="panel-note account-note">${esc(pops.unavailable_note)}</div></section>
      <section class="panel"><div class="panel-head"><span class="panel-title">Risk outcomes</span><span class="panel-meta">CANDIDATES ONLY</span></div>${shadowRows([
        ["Approved for manual review", risk.approved],
        ["Rejected", risk.rejected],
        ["Unverified", risk.unverified],
        ["Blocked by unverified costs", risk.blocked_by_unverified_costs, "shadow-blocked-costs"],
        ["Blocked by missing target structure", risk.blocked_by_target_structure, "shadow-blocked-target"],
      ])}</section>
      <section class="panel"><div class="panel-head"><span class="panel-title">Data and contention</span><span class="panel-meta">WHY WINDOWS WERE SKIPPED</span></div>${shadowRows([
        ["Closure not confirmed", diag.data_and_closure.closure_unconfirmed],
        ["Candle not available", diag.data_and_closure.candle_not_available],
        ["Market data unavailable", diag.data_and_closure.market_data_unavailable],
        ["Market closed", diag.data_and_closure.market_closed],
        ["Skipped: MT5 worker busy", diag.contention.skipped_mt5_worker_busy, "shadow-contention"],
        ["Skipped: worker failure pause", diag.contention.skipped_worker_backoff],
        ["Total contention deferrals", diag.contention.total_deferrals],
      ])}</section>
    </div>
    <section class="panel"><div class="panel-head"><span class="panel-title">By instrument and timeframe</span><span class="panel-meta">SIGNAL FREQUENCY</span></div>
      ${matrix ? `<div id="shadow-matrix-wrap" class="shadow-scroll"><table class="data-table" id="shadow-matrix"><thead><tr><th>Instrument</th><th>TF</th><th class="num">Windows</th><th class="num">Completed</th><th class="num">Skipped</th><th class="num">Missed</th><th class="num">Failed</th><th class="num">Candidates</th><th class="num">Risk approved</th></tr></thead><tbody>${matrix}</tbody></table></div>` : emptyState("No evaluations yet", "Enable shadow evaluation to start collecting evidence.")}</section>
    <div class="account-grid">
      <section class="panel"><div class="panel-head"><span class="panel-title">Skip, miss and failure reasons</span></div><div class="shadow-scroll">${reasonRows(skipRows, "Reason")}</div></section>
      <section class="panel"><div class="panel-head"><span class="panel-title">Risk rejection and block reasons</span></div><div class="shadow-scroll" id="shadow-risk-reasons">${reasonRows(risk.rejection_and_block_reasons, "Phase 4 check")}</div></section>
      <section class="panel"><div class="panel-head"><span class="panel-title">Strategy and data gate failures</span></div><div class="shadow-scroll">${reasonRows(diag.strategy_and_data_failures, "Gate")}</div>
        <div class="panel-note account-note" id="shadow-t4-verdicts">T4 verdicts: ${esc(Object.entries(diag.t4_verdicts ?? {}).map(([k, v]) => `${pretty(k)} ${v}`).join(" · ") || "none yet")}</div>
        ${diag.historical_recording_defects?.t4_gate_empty_reason_code?.count ? `<div class="panel-note account-note" id="shadow-t4-defects">Historical recording defect: ${esc(diag.historical_recording_defects.t4_gate_empty_reason_code.count)} T4 gate record(s) from the first activation were saved without their verdict. They are kept unchanged, shown here, and excluded from the failure counts; their original T4 verdict is unknown.</div>` : ""}
        ${diag.history_sync ? `<div class="panel-note account-note" id="shadow-history-sync">MT5 history sync: ${esc(diag.history_sync.retries_scheduled)} retr${diag.history_sync.retries_scheduled === 1 ? "y" : "ies"} in ${esc(diag.history_sync.windows_with_retries)} window(s) · recovered ${esc(diag.history_sync.recovered_after_retry)} · still incomplete at the last attempt ${esc(diag.history_sync.rejected_after_retries)} · no time to retry ${esc(diag.history_sync.rejected_no_time_to_retry)}. A recovered window is the same evaluation of the same candle, not an extra opportunity.</div>` : ""}</section>
    </div>`;
}

export function shadowCyclesMarkup(list) {
  if (!list) return "";
  const rows = list.evaluations.map((c) => `<tr data-evaluation="${esc(c.id)}"><td>${esc(stampSeconds(c.window_close_utc))}</td><td>${esc(c.broker_symbol ?? c.symbol)}</td><td>${esc(c.timeframe)}</td><td>${shadowChip(c.status, "status")}</td><td>${esc(c.reason_code ? pretty(c.reason_code) : "—")}</td><td>${esc(c.t6_outcome ? `${pretty(c.t6_outcome)}${c.t6_rule ? ` · ${c.t6_rule}` : ""}` : "—")}</td><td>${c.risk_status ? shadowChip(c.risk_status, "risk") : "—"}</td><td class="num">${esc(c.attempts)}</td></tr>`).join("");
  return `<section class="panel"><div class="panel-head"><span class="panel-title">Latest evaluations</span><span class="panel-meta">${list.evaluations.length} SHOWN</span></div>
    ${rows ? `<div class="shadow-scroll"><table class="data-table" id="shadow-cycles"><thead><tr><th>Candle close</th><th>Instrument</th><th>TF</th><th>Status</th><th>Reason</th><th>T6 outcome</th><th>Risk</th><th class="num">Attempts</th></tr></thead><tbody>${rows}</tbody></table></div>` : emptyState("No evaluations yet", "Nothing has been evaluated.")}</section>`;
}

// ---------------------------------------------------------------- shadow trade intents (P6.2a)
// HYPOTHETICAL — NOT AN ORDER. Read-only: filters and a detail view only; there is no control
// that could execute, submit, approve, modify or delete an intent. Missing financial values are
// shown as "Not available", never as zero.
export const INTENT_LABEL = "HYPOTHETICAL — NOT AN ORDER";
export const INTENT_TONE = { RISK_APPROVED: "verified", REJECTED: "invalid", UNVERIFIED: "unverified", established: "verified", unverified: "unverified", passed: "verified", failed: "invalid", not_evaluated: "not_applicable" };
export const REASON_CATEGORY = { policy_rejection: "Policy rejection", data_unverified: "Data could not be verified", cost_unverified: "Costs not established" };
export const intentChip = (value, field) => `<span class="field-chip ${INTENT_TONE[value] ?? ""}" data-intent-chip="${esc(field)}">${esc(pretty(value))}</span>`;
export const intentNumber = (value, digits) => (value === null || value === undefined ? null : Number(value).toFixed(digits));

export function intentMissing(intent) {
  if (intent.cost_status !== "established") return "Not available — costs not established";
  return `Not available — ${pretty(intent.eligibility).toLowerCase()}`;
}

export function intentAmount(intent, value, digits, suffix = "") {
  const text = intentNumber(value, digits);
  return text === null ? intentMissing(intent) : `${text}${suffix}`;
}

export function intentReason(intent) {
  const primary = intent.reasons?.primary;
  if (!primary) return intent.eligibility === "RISK_APPROVED" ? "All implemented checks passed" : "—";
  return `${REASON_CATEGORY[primary.category] ?? pretty(primary.category)}: ${primary.reason}`;
}

export function intentFiltersMarkup() {
  const f = state.shadow.intentFilters;
  const select = (name, label, values) => `<label class="trade-field"><span>${label}</span><select class="select" data-intent-filter="${name}" aria-label="${label}"><option value="">All</option>${values.map((v) => `<option value="${v}"${f[name] === v ? " selected" : ""}>${esc(name === "symbol" ? `${v}c` : pretty(v))}</option>`).join("")}</select></label>`;
  const day = (name, label) => `<label class="trade-field"><span>${label}</span><input class="text-input" type="date" data-intent-filter="${name}" aria-label="${label}" value="${esc(f[name])}"></label>`;
  return `<div class="trade-form shadow-intent-filters" id="shadow-intent-filters">${select("symbol", "Instrument", ["EURUSD", "GBPUSD", "USDJPY"])}${select("timeframe", "Timeframe", ["H1", "H4", "D1"])}${select("eligibility", "Eligibility", ["RISK_APPROVED", "REJECTED", "UNVERIFIED"])}${day("from", "Candle close from (UTC)")}${day("to", "Candle close to (UTC)")}</div>`;
}

export function intentDetailMarkup() {
  const s = state.shadow;
  if (!s.selectedIntent) return "";
  const d = s.intentDetail;
  if (!d) return `<div class="panel-note account-note" id="shadow-intent-detail">Loading intent ${esc(s.selectedIntent)}…</div>`;
  if (d.error) return `<div class="panel-note account-note" id="shadow-intent-detail">Intent unavailable: ${esc(d.error.message)}</div>`;
  const i = d.intent;
  const acct = i.account_evidence ?? {};
  const costs = i.cost_evidence ?? {};
  const component = (name) => { const c = costs.components?.[name]; return c ? pretty(c.status ?? "included in prices") : "—"; };
  const reasons = (i.reasons?.items ?? []).map((r) => `${REASON_CATEGORY[r.category] ?? r.category}: ${r.reason}`).join(" · ") || "None";
  return `<div id="shadow-intent-detail" class="shadow-intent-detail"><div class="risk-banner shadow-banner">${esc(INTENT_LABEL)} · intent ${esc(i.id)}</div>${shadowRows([
    ["Instrument / timeframe", `${i.broker_symbol} · ${i.timeframe} · candle close ${stampSeconds(i.window_close_utc)}`, "shadow-intent-d-instrument"],
    ["Direction / entry", `${i.direction} at ${i.entry_price} (${i.entry_side}, ${stampSeconds(i.entry_reference_utc)}); valid until ${i.valid_until_utc ? stampSeconds(i.valid_until_utc) : "—"}`],
    ["Stop-loss / take-profit", `${i.stop_loss} / ${i.take_profit} (target: structural opposing level)`],
    ["Volume", intentAmount(i, i.volume, 2, " lots"), "shadow-intent-d-volume"],
    ["Risk amount", intentAmount(i, i.risk_amount_usc, 2, " USC"), "shadow-intent-d-risk"],
    ["Risk of equity", intentAmount(i, i.risk_percent_equity, 3, "%")],
    ["Margin estimate", intentAmount(i, i.margin_estimate_usc, 2, " USC")],
    ["Reward:risk", `gross ${intentNumber(i.gross_reward_risk, 2)} · net ${i.net_reward_risk == null ? "Not available — costs not established" : intentNumber(i.net_reward_risk, 2)}`],
    ["Eligibility", pretty(i.eligibility), "shadow-intent-d-eligibility"],
    ["Costs", `${pretty(i.cost_status)} · terms ${costs.cost_terms_version ?? "—"} · commission ${component("commission")} · swap ${component("swap")} · slippage ${component("slippage")}`, "shadow-intent-d-costs"],
    ["Margin check", pretty(i.margin_status)],
    ["Account evidence", `equity ${acct.equity ?? "Not available"} ${acct.currency ?? ""} · read ${acct.retrieved_at_utc ? stampSeconds(acct.retrieved_at_utc) : "—"}`],
    ["Reasons", reasons, "shadow-intent-d-reasons"],
    ["Evaluation", `${d.evaluation?.status ?? "—"} · ${d.evaluation?.t6_outcome ?? "—"} ${d.evaluation?.t6_rule ?? ""} · risk ${d.risk_assessment?.risk_status ?? "—"}`],
    ["Versions", `risk engine v${i.risk_engine_version} · policy ${String(i.policy_fingerprint).slice(0, 19)}…`],
  ])}</div>`;
}

export function shadowIntentsMarkup() {
  const s = state.shadow;
  const list = s.intents;
  let body;
  if (s.intentError && !list) body = emptyState("Shadow trade intents unavailable", s.intentError.message || "The journal did not answer.", "error-state");
  else if (!list) body = emptyState("Loading", "Waiting for shadow trade intents.");
  else if (!list.intents.length) {
    const filtered = Object.values(s.intentFilters).some(Boolean);
    body = emptyState(filtered ? "No intents match these filters" : "No shadow trade intents yet",
      filtered ? "Change or clear the filters." : "An intent is recorded only when a scheduled evaluation finds a T6 trade candidate and the risk engine constructs its trade. Shadow evaluation is off or has not produced one.");
  } else {
    const rows = list.intents.map((i) => `<tr data-intent="${esc(i.id)}" tabindex="0" class="${s.selectedIntent === i.id ? "selected" : ""}"><td>${esc(stampSeconds(i.window_close_utc))}</td><td>${esc(i.broker_symbol)}</td><td>${esc(i.timeframe)}</td><td>${esc(i.direction)}</td><td class="num">${esc(i.entry_price)}</td><td class="num">${esc(i.stop_loss)}</td><td class="num">${esc(i.take_profit)}</td><td data-intent-cell="volume">${esc(intentAmount(i, i.volume, 2))}</td><td data-intent-cell="risk">${esc(intentAmount(i, i.risk_amount_usc, 2, " USC"))}</td><td data-intent-cell="risk-pct">${esc(intentAmount(i, i.risk_percent_equity, 3, "%"))}</td><td class="num">${esc(intentNumber(i.gross_reward_risk, 2))}</td><td data-intent-cell="net">${esc(i.net_reward_risk == null ? "Not available — costs not established" : intentNumber(i.net_reward_risk, 2))}</td><td>${intentChip(i.eligibility, "eligibility")}</td><td>${intentChip(i.cost_status, "costs")}</td><td>${intentChip(i.margin_status, "margin")}</td><td data-intent-cell="reason">${esc(intentReason(i))}</td></tr>`).join("");
    body = `<div class="shadow-scroll"><table class="data-table" id="shadow-intents"><thead><tr><th>Candle close</th><th>Instrument</th><th>TF</th><th>Direction</th><th class="num">Entry</th><th class="num">Stop-loss</th><th class="num">Take-profit</th><th>Volume</th><th>Risk</th><th>Risk %</th><th class="num">Gross R:R</th><th>Net R:R</th><th>Eligibility</th><th>Costs</th><th>Margin</th><th>Reason</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  }
  return `<section class="panel" id="shadow-intents-panel"><div class="panel-head"><span class="panel-title">Shadow trade intents</span><span class="panel-meta">${esc(INTENT_LABEL)}</span></div>
    <div class="risk-banner shadow-banner" id="shadow-intent-warning">${esc(INTENT_LABEL)} — Johanna has not placed and will not place these trades. "Risk approved" means every implemented risk check passed; it is not an instruction to trade. Candidates without a constructed trade appear in the funnel, not here.</div>
    ${intentFiltersMarkup()}${body}${intentDetailMarkup()}</section>`;
}

export function shadowFunnelMarkup() {
  const f = state.shadow.funnel;
  if (!f) return "";
  const pct = (value) => (value === null || value === undefined ? "—" : `${(value * 100).toFixed(1)}%`);
  const labels = { windows_scheduled: "Windows scheduled", completed_evaluations: "Completed evaluations", trade_candidates: "Trade candidates", risk_assessed: "Risk assessed", constructed_trade_intents: "Constructed-trade intents", risk_approved_shadow_intents: "Risk-approved shadow intents" };
  const stages = f.stages.map((s) => `<tr data-funnel-stage="${esc(s.id)}"><td>${esc(labels[s.id] ?? s.id)}</td><td class="num">${esc(s.count)}</td><td class="num">${esc(pct(s.of_previous))}</td><td class="num">${esc(pct(s.of_completed))}</td></tr>`).join("");
  const outcomes = Object.entries(f.outcomes).map(([k, v]) => `${pretty(k)} ${v.count}`).join(" · ");
  const risk = Object.entries(f.risk_outcomes).map(([k, v]) => `${pretty(k)} ${v.count}`).join(" · ");
  const ne = f.not_evaluated;
  const cats = f.reason_categories;
  const reasons = f.primary_reasons.items.length
    ? `<table class="data-table" id="shadow-funnel-reasons"><thead><tr><th>Primary reason</th><th>Category</th><th class="num">Evaluations</th></tr></thead><tbody>${f.primary_reasons.items.map((r) => `<tr><td>${esc(r.reason)}</td><td>${esc(REASON_CATEGORY[r.category] ?? r.category)}</td><td class="num">${esc(r.count)}</td></tr>`).join("")}</tbody></table>`
    : emptyState("No risk-assessed candidates", "No primary reasons recorded yet.");
  return `<section class="panel" id="shadow-funnel-panel"><div class="panel-head"><span class="panel-title">Opportunity funnel</span><span class="panel-meta">SCHEDULED WINDOWS · EACH COUNTED ONCE</span></div>
    <div class="shadow-scroll"><table class="data-table" id="shadow-funnel"><thead><tr><th>Stage</th><th class="num">Count</th><th class="num">Of previous</th><th class="num">Of completed</th></tr></thead><tbody>${stages}</tbody></table></div>
    ${shadowRows([
      ["Completed outcomes", outcomes, "shadow-funnel-outcomes"],
      ["Risk outcomes", risk, "shadow-funnel-risk"],
      ["Not evaluated (never NO_TRADE)", `skipped ${ne.SKIPPED} · missed ${ne.MISSED} · failed ${ne.FAILED}${f.in_progress ? ` · in progress ${f.in_progress}` : ""}`, "shadow-funnel-not-evaluated"],
      ["Primary reason categories", `policy rejections ${cats.policy_rejection} · data could not be verified ${cats.data_unverified} · costs not established ${cats.cost_unverified}`, "shadow-funnel-categories"],
      ["Executed trades / realised result", "Not available — shadow intents are hypothetical", "shadow-funnel-execution"],
    ])}
    <div class="shadow-scroll">${reasons}</div>
    <div class="panel-note account-note" id="shadow-funnel-note">One window = instrument, timeframe and candle close; retries count once and dashboard requests are excluded. Data-quality and cost problems are kept apart from policy rejections.${f.historical_recording_defects?.t4_gate_empty_reason_code ? ` ${esc(f.historical_recording_defects.t4_gate_empty_reason_code)} historical recording defect(s) are counted as the completed evaluations they were, not as failures.` : ""}</div></section>`;
}

export function shadowContent() {
  const s = state.shadow;
  if (s.error && !s.status) return `<section class="panel">${emptyState("Shadow evaluation unavailable", s.error.message || reasonText(s.error.reason), "error-state")}</section>`;
  return `${s.message ? `<div class="risk-stale" id="shadow-message" role="status">${esc(s.message)}</div>` : ""}
    <section class="panel"><div class="panel-head"><span class="panel-title">Background scheduler</span><span class="panel-meta">SHADOW · READ-ONLY</span></div><div id="shadow-scheduler">${shadowSchedulerMarkup(s.status)}</div></section>
    ${shadowDiagnosticsMarkup(s.diagnostics)}${shadowIntentsMarkup()}${shadowFunnelMarkup()}${shadowCyclesMarkup(s.cycles)}`;
}

export function shadowPage() {
  return `${pageHeading("Shadow Research", "Shadow evaluation and signal-frequency diagnostics · read-only", '<button class="button" type="button" id="shadow-refresh">Refresh</button>')}
    <div class="risk-banner shadow-banner" id="shadow-banner">${esc(SHADOW_BANNER)}</div>
    <div id="shadow-content">${shadowContent()}</div>`;
}

export function renderShadow() {
  if (state.page !== "analytics") return;
  setInner(document.querySelector("#shadow-content"), shadowContent());
}

export async function loadShadow() {
  const seq = ++state.shadow.seq;
  const [status, diagnostics, cycles] = await Promise.all([
    tradeGet("/api/shadow/status"),
    tradeGet("/api/shadow/diagnostics"),
    tradeGet("/api/shadow/evaluations?limit=20"),
  ]);
  await loadShadowIntents(false);
  if (seq !== state.shadow.seq) return;
  if (status.error || typeof status.payload?.state !== "string") state.shadow.error = status.error ?? malformed();
  else { state.shadow.status = status.payload; state.shadow.error = null; }
  state.shadow.diagnostics = diagnostics.payload?.populations ? diagnostics.payload : null;
  state.shadow.cycles = Array.isArray(cycles.payload?.evaluations) ? cycles.payload : null;
  if (!state.shadow.error && (diagnostics.error || cycles.error)) {
    state.shadow.message = `Diagnostics unavailable: ${(diagnostics.error ?? cycles.error).message}`;
  }
  renderShadow();
}

export function intentQuery() {
  const params = new URLSearchParams();
  for (const [name, value] of Object.entries(state.shadow.intentFilters)) if (value) params.set(name, value);
  return params;
}

export async function loadShadowIntents(rerender = true) {
  const seq = ++state.shadow.intentSeq;
  const params = intentQuery();
  const range = new URLSearchParams();
  for (const name of ["from", "to"]) if (params.get(name)) range.set(name, params.get(name));
  params.set("limit", "50");
  const [intents, funnel] = await Promise.all([
    tradeGet(`/api/shadow/intents?${params}`),
    tradeGet(`/api/shadow/funnel?${range}`),
  ]);
  if (seq !== state.shadow.intentSeq) return;
  state.shadow.intents = Array.isArray(intents.payload?.intents) ? intents.payload : null;
  state.shadow.intentError = state.shadow.intents ? null : (intents.error ?? malformed());
  state.shadow.funnel = Array.isArray(funnel.payload?.stages) ? funnel.payload : null;
  if (rerender) renderShadow();
}

export async function selectIntent(id) {
  if (state.shadow.selectedIntent === id) {
    state.shadow.selectedIntent = null;
    state.shadow.intentDetail = null;
    return renderShadow();
  }
  state.shadow.selectedIntent = id;
  state.shadow.intentDetail = null;
  renderShadow();
  const result = await tradeGet(`/api/shadow/intents/${encodeURIComponent(id)}`);
  if (state.shadow.selectedIntent !== id) return;
  state.shadow.intentDetail = result.payload?.intent ? result.payload : { error: result.error ?? malformed() };
  renderShadow();
}

