import { hooks } from "./hooks.js";
import { ANALYSIS_EXPIRE_MS, ANALYSIS_TIMEFRAMES, POLL, state } from "./state.js";
import { emptyState, esc, prefs, setInner, stamp, stampSeconds } from "./format.js";
import { fetchJson, malformed } from "./api.js";
import { chartData, chartKey } from "./chart.js";

// ---------------------------------------------------------------- market analysis (read-only)
// Presentation only. Every value shown comes from GET /api/market/analysis: the backend's T6
// result (authoritative), its T4 window diagnostics (window_validation, a verbatim projection
// of the T4 object that evaluation used), the snapshot summary and transport metadata.
// Nothing is calculated, inferred or re-classified here, and none of this touches the chart.
export const ANALYSIS_PAGES = new Set(["overview", "charts"]);
export const ANALYSIS_STATE_LABELS = {
  unsupported: "Unsupported", loading: "Loading", current: "Current", outdated: "Outdated",
  provisional: "Provisional", unavailable: "Unavailable", error: "Error",
};
export const ANALYSIS_STATE_CLASS = { current: "good", outdated: "warn", provisional: "warn", unavailable: "bad", error: "bad" };
export const OUTCOME_LABELS = { NO_TRADE: "No trade", MARKET_CONDITION: "Market condition only", TRADE_CANDIDATE: "Research candidate" };
export const OUTCOME_CLASS = { NO_TRADE: "outcome-none", MARKET_CONDITION: "outcome-condition", TRADE_CANDIDATE: "outcome-candidate" };
export const SETUP_STATUS_LABELS = { qualified: "qualified", not_qualified: "not qualified", not_evaluable: "not evaluable" };
export const CHECK_RESULT_LABELS = { pass: "Pass", fail: "Fail", unavailable: "Unavailable", not_applicable: "Not applicable" };
export const CHECK_LABELS = {
  TC1_regime: "TC1 · Trend regime is trending",
  TC2_bias: "TC2 · Directional bias is aligned",
  TC3_long_term: "TC3 · Long-term alignment with EMA200",
  TC4_pullback: "TC4 · Pullback to EMA20",
  TC5_resumption: "TC5 · Close back beyond EMA20",
  TC6_clearance: "TC6 · Clearance to the opposing level",
};
export const VETO_LABELS = {
  V1_ema50_structure_break: "V1 · Close beyond EMA50 in the pullback window",
  V2_swing_context_conflict: "V2 · Complete opposite swing structure",
};
export const DIMENSION_LABELS = {
  trend_regime: "Market condition (trend regime)",
  directional_bias: "Directional bias",
  trend_strength: "Trend strength (ADX)",
  volatility: "Volatility (ATR percentile)",
  momentum: "Momentum (RSI)",
  location: "Support / resistance location",
  swing_structure: "Swing structure",
};
export const VALIDATION_DIMENSIONS = ["price", "timestamps", "closure", "continuity", "completeness", "snapshot", "session_candles", "recency"];
// Readable labels for known T4 / T2 codes; the actual code is always shown next to the label,
// and an unknown code is shown as-is (never dropped).
export const VALIDATION_CODE_LABELS = {
  insufficient_history: "Not enough closed candles",
  insufficient_warmup: "Not enough warm-up candles",
  empty_window: "No candles in the window",
  snapshot_refused: "Market data snapshot refused",
  snapshot_provisional: "Market data snapshot provisional",
  snapshot_metadata_missing: "Snapshot metadata missing",
  snapshot_state_unknown: "Snapshot state unknown",
  clock_only_closure: "Candle closed by clock only (provisional)",
  forming_candle_inside_window: "Forming candle inside the window",
  closure_unknown: "Candle closure unknown",
  invalid_ohlc: "Invalid prices",
  invalid_timestamps: "Invalid timestamps",
  out_of_order: "Candles out of order",
  conflicting_duplicates: "Conflicting duplicate candles",
  identical_duplicates_collapsed: "Identical duplicates collapsed",
  duplicate_metadata_differs: "Duplicate metadata differs",
  quality_unknown: "Data quality unknown",
  gap_market_wide_unverified: "Gap not verified as market-wide",
  unexpected_candle_in_closure: "Candle inside a configured closure",
  snapshot_too_old: "Market data older than the 5 s limit at evaluation",
  not_live_source: "Not live MT5 data",
  no_bars: "MT5 returned no bars",
  tick_missing: "No usable snapshot tick",
  timestamps_missing: "Retrieval time missing",
  inconsistent_timestamps: "Inconsistent timestamps",
  tick_bars_skew: "Tick and bars retrieved too far apart",
  clock_bars_skew: "Clock check too far from bars retrieval",
  clock_mismatch: "Server clock mismatch",
  clock_stale_ticks: "No recent ticks (market closed or quiet)",
  clock_unverified: "Server clock not verified",
  invalid_bars_present: "Invalid bars present",
  legacy_time_basis: "Legacy server-time bars",
  bars_unavailable: "Bars unavailable",
  mt5_unavailable_cached: "MT5 unavailable: cached bars",
  // E1 market-data recency
  recency_reference_missing: "Recency cannot be established",
  candle_after_reference: "Candle newer than the market data reference",
  forming_candle_behind_tick: "Candle marked forming after its close (tick)",
  forming_candle_past_clock_close: "Candle marked forming after its close (clock)",
  tick_closure_unsupported: "Candle marked closed before the tick reached its close",
  clock_closure_premature: "Candle marked closed before it could close",
  closure_precedence_mismatch: "Candle closure label inconsistent",
  closure_flag_mismatch: "Candle completion flag inconsistent",
  close_time_mismatch: "Candle close time inconsistent",
  closed_history_gap: "Missing closed candle inside the analysis window",
  closed_history_gap_market_wide_unverified: "Missing closed candle, not verified as market-wide",
  closed_history_behind_tick: "History stale: expected closed candles missing",
  history_behind_clock: "History stale: expected candles missing (clock)",
  recency_relies_on_unverified_closure: "Recent coverage relies on unverified candles",
  expected_slot_excluded: "Expected latest candle analytically excluded",
  tick_stale_in_open_session: "No recent ticks while the market should be open",
  session_schedule_contradiction: "Market data contradicts the session schedule",
};
export const codeLabel = (code) => VALIDATION_CODE_LABELS[code] ?? String(code ?? "unknown");
export const humanize = (value) => (value == null ? "—" : String(value).replaceAll("_", " "));

export const analysisSupported = () => ANALYSIS_TIMEFRAMES.includes(prefs().selected_chart_timeframe);
// Whether analysis may be requested right now (the poller neither requests nor schedules otherwise).
export function analysisActive() {
  return ANALYSIS_PAGES.has(state.page) && analysisSupported() && Boolean(prefs().selected_currency_pair) && !state.error;
}
export const analysisInterval = () => (chartData()?.status === "LIVE" ? POLL.analysisMs : POLL.quietAnalysisMs);

// Open time of the newest closed (complete) candle in the chart data for the current selection.
export function latestClosedChartT() {
  const data = chartData();
  return data?.bars?.filter((bar) => bar.complete === true).at(-1)?.t ?? null;
}

// Status priority: unsupported, loading, error (no usable response), R1 unavailable, R2
// provisional, then current/outdated for R3-R7. R1 and R2 are never current.
export function analysisStatus() {
  if (!analysisSupported()) return { state: "unsupported", reasons: [] };
  const key = chartKey();
  const envelope = state.analysisKey === key ? state.analysis : null;
  const error = state.analysisError?.key === key ? state.analysisError : null;
  if (!envelope) return error && !state.analysisInFlight ? { state: "error", error, reasons: [] } : { state: "loading", reasons: [] };
  const rule = envelope.result.decision.rule;
  if (rule === "R1") return { state: "unavailable", envelope, error, reasons: [] };
  if (rule === "R2") return { state: "provisional", envelope, error, reasons: [] };
  const reasons = [];
  if (error) reasons.push(`the last refresh failed (${error.message || error.reason})`);
  if (Date.now() - state.analysisReceivedAt > ANALYSIS_EXPIRE_MS) reasons.push(`no new evaluation for over ${ANALYSIS_EXPIRE_MS / 1000} s`);
  const closed = latestClosedChartT();
  const lastT = envelope.result.inputs?.window?.last_t;
  if (closed && lastT && Date.parse(closed) > Date.parse(lastT)) reasons.push("a newer candle has closed since this evaluation");
  if (chartData()?.status !== "LIVE") reasons.push("the chart is not LIVE");
  return { state: reasons.length ? "outdated" : "current", envelope, error, reasons };
}

export const viewingHistory = () => state.view?.key === chartKey() && state.view?.follow === false;
export const analysisSignature = () => {
  const status = analysisStatus();
  return JSON.stringify([status.state, status.reasons, state.analysisReceivedAt, state.analysisKey, viewingHistory()]);
};

// ---- value formatting (presentation only)
export function analysisValue(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return Number.isFinite(value) ? value.toLocaleString("en-US", { maximumFractionDigits: 5, useGrouping: false }) : "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (Array.isArray(value)) return value.length ? value.map(analysisValue).join(", ") : "—";
  if (typeof value === "object") return Object.entries(value).map(([k, v]) => `${humanize(k)} ${analysisValue(v)}`).join("; ") || "—";
  return String(value);
}
export function evidenceText(item) {
  if (!item) return "—";
  const compared = item.threshold !== null && item.threshold !== undefined ? ` (${item.comparison ? `${item.comparison} ` : ""}${analysisValue(item.threshold)})` : "";
  return `${humanize(item.id)}: ${analysisValue(item.observed)}${compared}`;
}
export const kv = (label, value, extraClass = "") => `<div class="analysis-kv ${extraClass}"><span>${esc(label)}</span> <span>${value}</span></div>`;
export const analysisList = (items) => (items.length ? `<ul class="analysis-list">${items.map((item) => `<li>${item}</li>`).join("")}</ul>` : "");

export function analysisStateBadge(status) {
  return `<span class="badge analysis-state ${ANALYSIS_STATE_CLASS[status.state] ?? ""}" data-analysis-state="${esc(status.state)}">${esc(ANALYSIS_STATE_LABELS[status.state])}</span>`;
}
export function outcomeBadge(decision) {
  const outcome = decision?.outcome;
  return `<span class="badge analysis-outcome ${OUTCOME_CLASS[outcome] ?? ""}" data-outcome="${esc(outcome)}">${esc(OUTCOME_LABELS[outcome] ?? outcome ?? "—")} · ${esc(decision?.rule ?? "—")}</span>`;
}
export function stateExplanation(status) {
  if (status.state === "unavailable") return "Unavailable: the backend could not produce a valid analysis for this data (rule R1).";
  if (status.state === "provisional") return "Provisional: the data window is provisional, so this is diagnostic only and not a reliable result (rule R2).";
  if (status.state === "outdated") return `Outdated: ${status.reasons.join("; ")}.`;
  return "";
}
// The concise explanation: for a T4 rejection the actual T4 reason, otherwise T6's first reason.
export function analysisExplanation(status) {
  const { result, window_validation: wv } = status.envelope;
  const t4 = wv?.available === true ? wv.verdict?.reasons?.[0] : null;
  if (result.decision.rule === "R1" && t4) return `${codeLabel(t4.code)}: ${t4.detail ?? ""}`;
  if (result.decision.rule === "R1" && wv?.available === false) return wv.reason ?? result.decision.reasons?.[0] ?? "";
  return result.decision.reasons?.[0] ?? "";
}
export function setupLine(opportunity) {
  if (!opportunity) return "Not evaluated";
  const direction = opportunity.direction ? ` (${opportunity.direction})` : "";
  return `Trend continuation · ${SETUP_STATUS_LABELS[opportunity.status] ?? humanize(opportunity.status)}${direction}`;
}
export const candidateNotice = (decision) => (decision?.outcome === "TRADE_CANDIDATE"
  ? `<div class="analysis-notice candidate">Research candidate only: an analytical finding, not an instruction or recommendation to trade. Nothing is executed (actionable: ${analysisValue(decision.actionable)}).</div>`
  : "");

// ---- compact panel (Overview)
export function analysisCompactMarkup() {
  const status = analysisStatus();
  const head = `<div class="analysis-compact-head">${analysisStateBadge(status)}<span class="analysis-target">${esc(prefs().selected_currency_pair || "—")} · ${esc(prefs().selected_chart_timeframe || "—")}</span></div>`;
  if (status.state === "unsupported") return `${head}${emptyState("Analysis not available for this timeframe", `Market analysis covers ${ANALYSIS_TIMEFRAMES.join(", ")}; nothing is requested for ${prefs().selected_chart_timeframe}.`)}`;
  if (status.state === "loading") return `${head}${emptyState("Loading analysis", "Requesting a new backend evaluation…")}`;
  if (status.state === "error") return `${head}${emptyState("Analysis unavailable", `${status.error.message || status.error.reason}`, "error-state")}`;
  const { result } = status.envelope;
  const summary = result.market_condition?.summary;
  const explanation = stateExplanation(status);
  return `${head}<div class="analysis-compact">
    ${kv("Decision", outcomeBadge(result.decision))}
    ${kv("Market condition", esc(humanize(summary?.trend_regime)))}
    ${kv("Directional bias", esc(humanize(summary?.directional_bias)))}
    ${kv("Setup", esc(setupLine(result.opportunity)))}
    <p class="analysis-explanation">${esc(analysisExplanation(status))}</p>
    ${explanation ? `<p class="analysis-state-note ${esc(status.state)}">${esc(explanation)}</p>` : ""}
    ${kv("Evaluated", `${esc(stampSeconds(result.evaluated_at_utc))} · received <span class="analysis-age">—</span>`)}
    ${candidateNotice(result.decision)}
    <div class="analysis-compact-foot"><span>Analysis only; no trade instructions.</span><button class="text-link" data-page="charts">Full analysis</button></div>
  </div>`;
}

// ---- full panel (Charts)
export function conditionRows(marketCondition) {
  if (!marketCondition) return `<p class="analysis-muted">No market conditions: the analysis was not calculated.</p>`;
  const dims = marketCondition.dimensions ?? {};
  const rows = Object.entries(DIMENSION_LABELS).map(([name, label]) => {
    const dim = dims[name];
    if (!dim) return "";
    const value = Array.isArray(dim.state) ? dim.state.map(humanize).join(", ") : humanize(dim.state);
    const details = [];
    const evidence = [...(dim.supporting ?? []), ...(dim.contradicting ?? [])];
    if (name === "directional_bias" && dim.votes?.length) details.push(...dim.votes.map((vote) => `${esc(humanize(vote.id))}: ${esc(humanize(vote.vote))}`));
    else if (evidence.length) details.push(esc(evidenceText(evidence[0])));
    if (name === "location") {
      if (dim.zones_containing_price?.length) details.push(`inside ${esc(dim.zones_containing_price.join(", "))}`);
      if (dim.nearest_below) details.push(`nearest below ${esc(dim.nearest_below.id)} (${esc(analysisValue(dim.nearest_below.distance_atr))} ATR)`);
      if (dim.nearest_above) details.push(`nearest above ${esc(dim.nearest_above.id)} (${esc(analysisValue(dim.nearest_above.distance_atr))} ATR)`);
    }
    if (name === "swing_structure" && dim.pivots?.length) details.push(...dim.pivots.map((pivot) => `${esc(pivot.role)} ${esc(analysisValue(pivot.price))} at ${esc(stamp(pivot.pivot_t))}`));
    details.push(...(dim.unavailable ?? []).map((item) => `unavailable: ${esc(humanize(item.input))} (${esc(item.reason ?? "no reason given")})`));
    return `<tr><th scope="row">${esc(label)}</th><td>${esc(value)}${dim.trusted ? "" : ' <span class="analysis-muted">(not trusted)</span>'}</td><td>${details.join("<br>") || "—"}</td></tr>`;
  }).join("");
  return `<div class="table-scroll"><table class="data-table analysis-table"><tbody>${rows}</tbody></table></div>`;
}

export function setupMarkup(opportunity, decision) {
  if (!opportunity) return `<p class="analysis-muted">Setup not evaluated: no analysis was calculated (rule ${esc(decision?.rule ?? "—")}).</p>`;
  const head = `<p><strong>${esc(setupLine(opportunity))}</strong></p>`;
  if (!opportunity.checklist?.length) {
    const why = (opportunity.required_inputs?.unavailable ?? []).map((item) => `${esc(humanize(item.input))}: ${esc(item.reason ?? "")}`);
    return `${head}<p class="analysis-muted">Checks not evaluated.</p>${analysisList(why)}`;
  }
  const checks = opportunity.checklist.map((check) => {
    const extra = [];
    if (check.evidence_id) extra.push(esc(humanize(check.evidence_id)));
    if (check.price_inside_zone === true) extra.push("price inside the raw level interval");
    if (check.detail) extra.push(esc(check.detail));
    const compared = check.threshold !== null && check.threshold !== undefined ? ` vs ${esc(analysisValue(check.threshold))}` : "";
    return `<tr data-check="${esc(check.id)}"><th scope="row">${esc(CHECK_LABELS[check.id] ?? check.id)}</th><td class="check-${esc(check.result)}">${esc(CHECK_RESULT_LABELS[check.result] ?? check.result)}</td><td>${esc(analysisValue(check.observed))}${compared}${extra.length ? `<br><span class="analysis-muted">${extra.join(" · ")}</span>` : ""}</td></tr>`;
  }).join("");
  const vetoes = (opportunity.vetoes ?? []).map((veto) => {
    const label = veto.triggered === true ? "Triggered" : veto.triggered === false ? "Not triggered" : "Not evaluated";
    return `<tr data-veto="${esc(veto.id)}"><th scope="row">${esc(VETO_LABELS[veto.id] ?? veto.id)}</th><td class="veto-${veto.triggered === true ? "on" : "off"}">${label}</td><td>${esc(veto.detail ?? "—")}</td></tr>`;
  }).join("");
  const cautions = (opportunity.cautions ?? []).map((item) => `${esc(humanize(item.id))}: caution only, not a veto`);
  const unavailable = [...(opportunity.required_inputs?.unavailable ?? []), ...(opportunity.unavailable_context ?? [])]
    .map((item) => `${esc(humanize(item.input))}: ${esc(item.reason ?? "no reason given")}`);
  return `${head}<div class="table-scroll"><table class="data-table analysis-table"><tbody>${checks}${vetoes}</tbody></table></div>
    ${cautions.length ? `<h4>Cautions</h4>${analysisList(cautions)}` : ""}
    ${unavailable.length ? `<h4>Unavailable inputs</h4>${analysisList(unavailable)}` : ""}`;
}

export function validationMarkup(wv) {
  if (!wv) return `<p class="analysis-muted">No window validation was returned.</p>`;
  if (wv.available !== true) return `<p><strong>T4 validation did not run.</strong></p><p class="analysis-muted">${esc(wv.reason ?? "No reason was given.")}</p>`;
  const verdict = wv.verdict ?? {};
  const reasons = (verdict.reasons ?? []).map((reason) => {
    const sub = Array.isArray(reason.snapshot_reasons) && reason.snapshot_reasons.length
      ? `<br><span class="analysis-muted">Snapshot: ${reason.snapshot_reasons.map((code) => `${esc(codeLabel(code))} <code>${esc(code)}</code>`).join(", ")}</span>`
      : "";
    return `<strong>${esc(codeLabel(reason.code))}</strong>${reason.detail ? `: ${esc(reason.detail)}` : ""} <span class="analysis-muted">(${esc(humanize(reason.dimension))} · ${esc(reason.severity ?? "—")} · <code>${esc(reason.code)}</code>)</span>${sub}`;
  });
  const dimensions = VALIDATION_DIMENSIONS.filter((name) => wv.dimensions?.[name])
    .concat(Object.keys(wv.dimensions ?? {}).filter((name) => !VALIDATION_DIMENSIONS.includes(name)))
    .map((name) => `<span class="badge dimension-${esc(wv.dimensions[name].status)}">${esc(humanize(name))}: ${esc(wv.dimensions[name].status ?? "—")}</span>`).join(" ");
  return `${kv("Verdict", `<span class="badge validation-${esc(verdict.state)}">${esc(humanize(verdict.state))}</span>`)}
    ${kv("Window", esc(`${analysisValue(wv.window?.count)} of ${analysisValue(wv.requested?.expected)} candles · ${stamp(wv.window?.first_t)} to ${stamp(wv.window?.last_t)}`))}
    ${kv("Reason codes", wv.reason_codes?.length ? wv.reason_codes.map((code) => `<code>${esc(code)}</code>`).join(" ") : "none reported")}
    ${analysisList(reasons)}
    <div class="analysis-dimensions">${dimensions}</div>
    ${"d1_convention" in wv ? kv("D1 convention", esc(analysisValue(wv.d1_convention))) : ""}`;
}

export function timingMarkup(envelope) {
  const { result, snapshot, service } = envelope;
  const age = service?.data_age_at_evaluation_ms;
  return `${kv("Market data retrieved (MT5)", `${esc(stampSeconds(snapshot?.bars_retrieved_at_utc))}${snapshot?.bars_served_from_cache ? " · repeat of a cached MT5 read" : ""}`)}
    ${kv("Analysis covers candles up to", esc(result.as_of_utc ? stampSeconds(result.as_of_utc) : "No analysis window"))}
    ${kv("Evaluated", `${esc(stampSeconds(result.evaluated_at_utc))}${service?.joined_in_flight ? " · shared computation (original evaluation time)" : ""}`)}
    ${kv("Data age at evaluation (backend)", esc(typeof age === "number" && Number.isFinite(age) ? `${(age / 1000).toFixed(3)} s` : "—"))}
    ${kv("Response served", esc(stampSeconds(envelope.served_at_utc)))}
    ${kv("Received by this browser", '<span class="analysis-age">—</span>')}`;
}

export function analysisFullMarkup() {
  const status = analysisStatus();
  if (status.state === "unsupported") return `<div class="analysis-head">${analysisStateBadge(status)}</div>${emptyState("Analysis not available for this timeframe", `Market analysis covers ${ANALYSIS_TIMEFRAMES.join(", ")}. No analysis is requested for ${prefs().selected_chart_timeframe}.`)}`;
  if (status.state === "loading") return `<div class="analysis-head">${analysisStateBadge(status)}</div>${emptyState("Loading analysis", "Requesting a new backend evaluation…")}`;
  if (status.state === "error") return `<div class="analysis-head">${analysisStateBadge(status)}</div>${emptyState("Analysis unavailable", `${status.error.reason}: ${status.error.message}`, "error-state")}`;
  const envelope = status.envelope;
  const { result } = envelope;
  const decision = result.decision;
  const explanation = stateExplanation(status);
  const analysedWindow = result.inputs?.window;
  const history = viewingHistory() ? " You are viewing older candles on the chart: this analysis does not describe the visible range." : "";
  return `<div class="analysis-head">${analysisStateBadge(status)}${outcomeBadge(decision)}<span class="analysis-target">${esc(result.symbol ?? envelope.symbol)} · ${esc(result.timeframe ?? envelope.timeframe)}</span></div>
    ${explanation ? `<p class="analysis-state-note ${esc(status.state)}">${esc(explanation)}</p>` : ""}
    ${candidateNotice(decision)}
    <p class="analysis-window-note">This analysis uses the latest backend evaluation window (${esc(analysisValue(analysedWindow?.count))} closed candles up to ${esc(result.as_of_utc ? stampSeconds(result.as_of_utc) : "—")}), not necessarily the candles visible on the chart.${esc(history)}</p>
    <div class="analysis-grid">
      <section class="analysis-section"><h3>Decision</h3>${kv("Outcome", outcomeBadge(decision))}${kv("Rule", esc(decision.rule))}${analysisList((decision.reasons ?? []).map(esc))}<p class="analysis-muted">${esc(result.note ?? "")}</p></section>
      <section class="analysis-section"><h3>Time and freshness</h3>${timingMarkup(envelope)}</section>
      <section class="analysis-section"><h3>Window validation (T4)</h3>${validationMarkup(envelope.window_validation)}</section>
    </div>
    <section class="analysis-section"><h3>Market conditions</h3>${conditionRows(result.market_condition)}</section>
    <section class="analysis-section"><h3>Trend-continuation setup</h3>${setupMarkup(result.opportunity, decision)}</section>
    <details class="analysis-details"><summary>Technical details</summary>
      ${kv("Provenance", esc(`${analysisValue(result.inputs?.provenance?.mode)} · ${analysisValue(result.inputs?.provenance?.content_binding)}`))}
      ${kv("Snapshot", esc(`${analysisValue(envelope.snapshot?.snapshot_id)} · ${analysisValue(envelope.snapshot?.upstream_status)} · ${analysisValue(envelope.snapshot?.source)}`))}
      ${(result.gate?.reasons ?? []).length ? `<h4>Gate reasons</h4>${analysisList(result.gate.reasons.map(esc))}` : ""}
      <h4>Limitations</h4>${analysisList((result.limitations ?? []).map(esc))}
      <h4>Original T6 result (JSON, unmodified)</h4><pre class="analysis-json">${esc(JSON.stringify(result, null, 2))}</pre>
      <h4>Window validation (JSON, unmodified)</h4><pre class="analysis-json">${esc(JSON.stringify(envelope.window_validation, null, 2))}</pre>
    </details>`;
}

// Updates only the analysis containers (setInner skips unchanged markup); never the chart.
export function renderAnalysis() {
  const compact = document.querySelector("#analysis-compact-body");
  if (compact) setInner(compact, analysisCompactMarkup());
  const full = document.querySelector("#analysis-full-body");
  if (full) setInner(full, analysisFullMarkup());
  renderAnalysisAge();
  if (state.overlay.enabled) hooks.drawOverlay();
}
// The browser-receipt age changes every second; it is kept out of the panel markup so that
// re-rendering never collapses open details.
export function renderAnalysisAge() {
  const text = state.analysis && state.analysisKey === chartKey()
    ? `${Math.max(0, Math.round((Date.now() - state.analysisReceivedAt) / 1000))} s ago`
    : "—";
  document.querySelectorAll(".analysis-age").forEach((element) => {
    if (element.textContent !== text) element.textContent = text;
  });
}

export const analysisPanelCompact = () => `<section class="panel lower-panel analysis-panel" id="analysis-compact-panel"><div class="panel-head"><span class="panel-title">Analysis &amp; setups</span><span class="panel-meta">BACKEND ANALYSIS · READ-ONLY</span></div><div id="analysis-compact-body">${analysisCompactMarkup()}</div></section>`;
export const analysisPanelFull = () => `<section class="panel analysis-panel analysis-full" id="analysis-full-panel"><div class="panel-head"><span class="panel-title">Market analysis</span><span class="panel-meta">BACKEND T6 EVALUATION · READ-ONLY · NOT TRADE INSTRUCTIONS</span></div><div id="analysis-full-body">${analysisFullMarkup()}</div></section>`;

export function validAnalysisPayload(payload, symbol, timeframe) {
  return payload && payload.symbol === symbol && payload.timeframe === timeframe
    && typeof payload.result?.decision?.outcome === "string" && typeof payload.result?.decision?.rule === "string"
    && payload.window_validation && typeof payload.window_validation === "object";
}

export async function refreshAnalysis(poller) {
  const symbol = prefs().selected_currency_pair;
  const timeframe = prefs().selected_chart_timeframe;
  if (!analysisActive()) return true;
  const key = `${symbol}|${timeframe}`;
  const seq = ++state.analysisSeq;
  if (state.analysisKey !== key && state.analysis) { state.analysis = null; state.analysisKey = null; }
  state.analysisInFlight = true;
  renderAnalysis();
  const query = new URLSearchParams({ symbol, timeframe });
  const result = await fetchJson(`/api/market/analysis?${query}`, poller, POLL.analysisTimeoutMs);
  if (seq === state.analysisSeq) state.analysisInFlight = false;
  // An answer for an older request or another selection never replaces the shown analysis.
  if (result.aborted || seq !== state.analysisSeq || key !== chartKey()) return "aborted";
  const payload = result.payload;
  const error = result.error ?? (validAnalysisPayload(payload, symbol, timeframe) ? null : malformed());
  if (error) {
    state.analysisError = { key, ...error }; // an earlier result stays, shown as not current
  } else {
    state.analysis = payload;
    state.analysisKey = key;
    state.analysisReceivedAt = Date.now();
    state.analysisError = null;
  }
  renderAnalysis();
  return !error;
}

// Start or pause analysis polling after a page or selection change.
export function syncAnalysisPolling() {
  const poller = hooks.livePolling.analysis;
  if (!hooks.livePolling.started) return;
  if (!analysisActive()) poller.suspend();
  else if (!poller.inFlight && poller.timer === null) poller.now();
}
