import { state } from "./state.js";
import { esc, prefs, setInner, stamp, stampSeconds, tz } from "./format.js";
import { CHART_PAD, chartData, chartGeometry, chartKey, clamp, drawable, timeLabel } from "./chart.js";
import { CHECK_RESULT_LABELS, OUTCOME_CLASS, OUTCOME_LABELS, analysisStatus, analysisValue, candidateNotice, codeLabel, humanize } from "./analysis.js";

// ---------------------------------------------------------------- analysis overlay (V1)
// A second canvas layered over #price-chart (pointer-events: none) showing the backend's existing
// analysis on the Charts page. Presentation only: every value comes from the /api/market/analysis
// envelope; nothing is calculated, inferred or classified here. overlayModel() validates the
// envelope against the displayed chart and fails closed; drawOverlay() renders only what the
// model accepted, with the chart's own recorded geometry. Disabled: nothing here runs.
export const OVERLAY_MODE_LABELS = {
  current: "CURRENT", provisional: "PROVISIONAL", outdated: "OUTDATED", unavailable: "ANALYSIS UNAVAILABLE", none: "NO OVERLAY",
};
export const OVERLAY_MODE_CLASS = { current: "good", provisional: "warn", outdated: "warn", unavailable: "bad" };
export const OVERLAY_EMA = { EMA20: { color: "--accent", dash: [] }, EMA50: { color: "--amber", dash: [] }, EMA200: { color: "--muted", dash: [5, 3] } };
export const OVERLAY_CHECKS = ["TC3_long_term", "TC4_pullback", "TC5_resumption"];
export const OVERLAY_STATUS_HEIGHT = 15; // px: the analysis-state label box
export const OVERLAY_LABEL_GAP = 14; // px: pinned zone labels keep this baseline distance below it
// Pivot labels come only from the backend's swing state (features.swings.state).
export const SWING_STATE_LABELS = {
  higher_highs_higher_lows: { H_last: "HH", L_last: "HL" },
  lower_highs_lower_lows: { H_last: "LH", L_last: "LL" },
};
export const PIVOT_ROLE_LABELS = { H_prev: "prev H", L_prev: "prev L", H_last: "H", L_last: "L" }; // compact

// Annotation visibility (per layer). Presentation preferences only: stored in this browser's
// localStorage, never sent to the backend; hiding a layer never removes its analysis data.
export const ANNOTATION_STORAGE_KEY = "johanna.chart.annotationPreferences.v1";
export const ANNOTATION_GROUPS = [
  ["Trend indicators", [["ema20", "EMA 20"], ["ema50", "EMA 50"], ["ema200", "EMA 200"]]],
  ["Market structure", [["swingHighs", "Swing highs"], ["swingLows", "Swing lows"], ["decisionPivots", "Decision pivots"]]],
  ["Price levels", [["supportZones", "Support zones"], ["resistanceZones", "Resistance zones"], ["containsZones", "Zones containing current price"]]],
  ["Setup annotations", [["pullback", "Pullback region"], ["tc3", "TC3 marker"], ["tc4", "TC4 marker"], ["tc5", "TC5 marker"], ["tc4Outline", "TC4 outline"]]],
  ["Analysis context", [["statusLabel", "Analysis status label"], ["endpointMarker", "Latest analysed candle"]]],
];
export const ANNOTATION_KEYS = ANNOTATION_GROUPS.flatMap(([, items]) => items.map(([key]) => key));
export const EMA_ANNOTATION = { EMA20: "ema20", EMA50: "ema50", EMA200: "ema200" };
export const CHECK_ANNOTATION = { TC3_long_term: "tc3", TC4_pullback: "tc4", TC5_resumption: "tc5" };

// Defaults (all on) overlaid with valid stored booleans; unknown keys and malformed values are
// ignored. Storage that throws leaves the preferences working for this session only.
export function annotationState() {
  if (state.overlay.annotations) return state.overlay.annotations;
  const prefs = Object.fromEntries(ANNOTATION_KEYS.map((key) => [key, true]));
  let storage = "ok";
  let raw = null;
  try {
    raw = window.localStorage.getItem(ANNOTATION_STORAGE_KEY);
  } catch {
    storage = "unavailable";
  }
  if (raw != null) {
    try {
      const stored = JSON.parse(raw);
      if (stored && typeof stored === "object" && !Array.isArray(stored)) {
        for (const key of ANNOTATION_KEYS) if (typeof stored[key] === "boolean") prefs[key] = stored[key];
      }
    } catch {
      // malformed: defaults stay; the next change rewrites a valid record
    }
  }
  state.overlay.annotations = { prefs, storage };
  return state.overlay.annotations;
}
export const annotationOn = (key) => annotationState().prefs[key] !== false;

export function saveAnnotationState() {
  const record = annotationState();
  try {
    window.localStorage.setItem(ANNOTATION_STORAGE_KEY, JSON.stringify(record.prefs));
    record.storage = "ok";
  } catch {
    record.storage = "unavailable"; // keep working for this session
  }
}

export function setAnnotations(changes) {
  const record = annotationState();
  for (const [key, value] of Object.entries(changes)) if (ANNOTATION_KEYS.includes(key)) record.prefs[key] = Boolean(value);
  saveAnnotationState();
  document.querySelectorAll("[data-annotation-key]").forEach((box) => { box.checked = record.prefs[box.dataset.annotationKey] !== false; });
  const note = document.querySelector("#annotation-storage-note");
  if (note) note.hidden = record.storage !== "unavailable";
  if (state.overlay.enabled) drawOverlay(); // redraw only: the model is reused
}

export function setAnnotationMenu(open) {
  state.overlay.menuOpen = Boolean(open);
  const panel = document.querySelector("#annotation-panel");
  const button = document.querySelector("[data-annotation-menu]");
  if (panel) panel.hidden = !state.overlay.menuOpen;
  if (button) button.setAttribute("aria-expanded", String(state.overlay.menuOpen));
}

export function annotationMenuMarkup() {
  const { prefs, storage } = annotationState();
  const groups = ANNOTATION_GROUPS.map(([title, items]) => `<fieldset class="annotation-group"><legend>${esc(title)}</legend>${items
    .map(([key, label]) => `<label class="annotation-option"><input type="checkbox" data-annotation-key="${key}"${prefs[key] !== false ? " checked" : ""}> ${esc(label)}</label>`)
    .join("")}</fieldset>`).join("");
  return `<div class="annotation-menu"><button type="button" class="annotation-button" data-annotation-menu aria-haspopup="true" aria-expanded="${state.overlay.menuOpen}" aria-controls="annotation-panel">Annotations</button><div class="annotation-panel" id="annotation-panel" role="group" aria-label="Chart annotations"${state.overlay.menuOpen ? "" : " hidden"}><div class="annotation-actions"><button type="button" data-annotation-all="1">Select all</button><button type="button" data-annotation-all="0">Clear all</button></div>${groups}<p class="annotation-note" id="annotation-storage-note"${storage === "unavailable" ? "" : " hidden"}>This browser can't save these choices; they apply to this session only.</p><p class="annotation-note">Shown only while Analysis Overlay is on.</p></div></div>`;
}
export const isoMs = (iso) => (typeof iso === "string" ? Date.parse(iso) : NaN);
export const isFiniteNumber = (value) => typeof value === "number" && Number.isFinite(value);

export function overlayToggleMarkup() {
  const on = state.overlay.enabled;
  return `<div class="overlay-toggle" role="group" aria-label="Chart mode"><button type="button" class="overlay-mode" data-overlay-mode="clean" aria-pressed="${!on}">Clean chart</button><button type="button" class="overlay-mode" data-overlay-mode="analysis" aria-pressed="${on}">Analysis Overlay</button></div>`;
}

// Toggle (in memory only: no request, no preference write, no new listener or timer).
export function setOverlay(enabled) {
  state.overlay.enabled = Boolean(enabled);
  document.querySelectorAll("[data-overlay-mode]").forEach((button) => {
    button.setAttribute("aria-pressed", String((button.dataset.overlayMode === "analysis") === state.overlay.enabled));
  });
  const panel = document.querySelector("#overlay-panel");
  const canvas = document.querySelector("#analysis-overlay");
  if (panel) panel.hidden = !state.overlay.enabled;
  if (canvas) canvas.hidden = !state.overlay.enabled;
  if (state.overlay.enabled) drawOverlay();
  else clearOverlay();
}

export function clearOverlay() {
  const canvas = document.querySelector("#analysis-overlay");
  if (canvas) {
    canvas.width = canvas.width; // clears the bitmap
    canvas.__signature = null;
  }
  const panel = document.querySelector("#overlay-panel");
  if (panel) setInner(panel, "");
  state.overlay.last = null;
}

// Validate an analysis envelope against the displayed chart and build the visual model.
// Pure: (status from analysisStatus(), chartData(), selected pair/timeframe) -> model.
// Fails closed: any inconsistency gives { layers: null } and a reason; nothing is drawn.
export function overlayModel(status, data, selection) {
  state.overlay.modelCalls += 1;
  const select = selection ?? { symbol: prefs().selected_currency_pair, timeframe: prefs().selected_chart_timeframe };
  const fail = (mode, reason) => ({ mode, reason, layers: null, skipped: [], envelope: status?.envelope ?? null });
  if (!data?.bars?.length) return fail("none", "No chart data is loaded.");
  const kind = status?.state;
  if (kind === "unsupported") return fail("none", "Analysis supports H1, H4 and D1 only.");
  if (kind === "loading") return fail("none", "Waiting for the analysis of this pair and timeframe.");
  if (kind === "error") return fail("unavailable", "The analysis could not be loaded.");
  const envelope = status?.envelope;
  const result = envelope?.result;
  if (!result) return fail("none", "No analysis is available.");
  const wv = envelope.window_validation ?? {};
  const inputs = result.inputs ?? {};
  // Identity: the analysis must be for exactly this chart.
  if (envelope.symbol !== select.symbol || result.symbol !== select.symbol || data.symbol !== select.symbol) {
    return fail("unavailable", "The analysis is for a different symbol than the chart.");
  }
  if (envelope.timeframe !== select.timeframe || result.timeframe !== select.timeframe || data.timeframe !== select.timeframe) {
    return fail("unavailable", "The analysis is for a different timeframe than the chart.");
  }
  if (result.broker_symbol && data.broker_symbol && result.broker_symbol !== data.broker_symbol) {
    return fail("unavailable", "The broker symbol differs between the chart and the analysis.");
  }
  const ids = [envelope.snapshot?.snapshot_id, inputs.snapshot_id, wv.snapshot_id];
  if (!ids[0] || ids.some((id) => id !== ids[0])) {
    return fail("unavailable", "Snapshot identity differs between the snapshot, its validation and the analysis.");
  }
  if (wv.available !== true) return fail("unavailable", "Window validation is not available for this analysis.");
  if (inputs.provenance?.passed !== true) return fail("unavailable", "The analysis provenance checks did not pass.");
  const win = inputs.window ?? {};
  const first = isoMs(win.first_t), last = isoMs(win.last_t);
  if (!Number.isFinite(first) || !Number.isFinite(last) || wv.window?.first_t !== win.first_t || wv.window?.last_t !== win.last_t) {
    return fail("unavailable", "The analysis window is missing or differs from its validation.");
  }
  // State (analysisStatus(): R1 unavailable, R2 provisional, R3-R7 current or outdated).
  if (kind === "unavailable") {
    const reason = wv.verdict?.reasons?.[0];
    return fail("unavailable", reason ? `${codeLabel(reason.code)} (${reason.code}).` : `${result.decision?.reasons?.[0] ?? "The analysis was rejected"}.`);
  }
  if (kind === "outdated") return fail("outdated", `Overlay cleared: ${(status.reasons ?? []).join("; ") || "the analysis is outdated"}.`);
  if (kind !== "current" && kind !== "provisional") return fail("none", "No displayable analysis.");
  const verdict = wv.verdict?.state;
  if ((kind === "current" && verdict !== "accepted") || (kind === "provisional" && verdict !== "provisional")) {
    return fail("unavailable", `The window validation verdict (${verdict}) does not match the analysis state.`);
  }
  const provisional = kind === "provisional";
  // Exact timestamp mapping onto the chart's own (drawable) candles, inside the analysis window.
  const bars = data.bars.filter(drawable);
  const indexOf = new Map(bars.map((bar, i) => [isoMs(bar.t), i]));
  const inWindow = (t) => { const ms = isoMs(t); return Number.isFinite(ms) && ms >= first && ms <= last; };
  const at = (t) => (inWindow(t) ? indexOf.get(isoMs(t)) ?? null : null);
  const endpoint = at(win.last_t);
  if (endpoint == null) return fail("unavailable", "The last analysed candle is not in the chart data.");
  const features = result.features ?? {};
  const rows = Array.isArray(features.pullback?.candles) ? features.pullback.candles : [];
  for (const row of rows) { // chart and analysis come from separate MT5 reads: closed candles must agree
    const i = at(row.t);
    if (i == null) continue;
    if (bars[i].l !== row.low || bars[i].h !== row.high || bars[i].c !== row.close) {
      return fail("unavailable", `The chart candle ${row.t} differs from the analysed candle.`);
    }
  }
  const skipped = [];
  const layers = { endpoint: { index: endpoint, t: win.last_t }, ema: [], pullback: null, pivots: [], zones: null, checks: [] };
  for (const name of Object.keys(OVERLAY_EMA)) {
    const entry = features.latest?.[name];
    if (entry && entry.usable === true && entry.status === "available" && isFiniteNumber(entry.value) && entry.t === win.last_t) {
      layers.ema.push({ name, value: entry.value, t: entry.t, index: endpoint });
    } else {
      skipped.push({ kind: "ema", name, reason: !entry ? "not provided" : entry.t !== win.last_t ? "not at the last analysed candle" : entry.reason || entry.status || "not usable" });
    }
  }
  if (!provisional && features.pullback?.usable === true && rows.length) {
    const mapped = rows.map((row) => ({ t: row.t, index: at(row.t), low: row.low, high: row.high, close: row.close, ema20: isFiniteNumber(row.ema20) ? row.ema20 : null, ema50: isFiniteNumber(row.ema50) ? row.ema50 : null }));
    const missing = mapped.filter((row) => row.index == null).length;
    if (missing) skipped.push({ kind: "pullback", reason: `${missing} pullback candle(s) outside the window or not on the chart` });
    const shown = mapped.filter((row) => row.index != null);
    if (shown.length) layers.pullback = { rows: shown };
  }
  const dims = result.market_condition?.dimensions ?? {};
  const swings = features.swings;
  if (!provisional && swings?.usable === true && dims.swing_structure?.trusted === true) {
    const labels = SWING_STATE_LABELS[swings.state] ?? {};
    for (const pivot of swings.pivots ?? []) {
      const i = at(pivot.pivot_t);
      const confirmed = isoMs(pivot.confirmed_at_utc) <= isoMs(result.as_of_utc);
      if (i == null || !confirmed || !isFiniteNumber(pivot.price) || !["high", "low"].includes(pivot.type)) {
        skipped.push({ kind: "pivot", name: pivot.role, reason: "outside the window, not on the chart or not confirmed" });
        continue;
      }
      layers.pivots.push({ role: pivot.role, type: pivot.type, price: pivot.price, t: pivot.pivot_t, confirmed_at_utc: pivot.confirmed_at_utc, index: i, label: labels[pivot.role] ?? PIVOT_ROLE_LABELS[pivot.role] ?? pivot.role });
    }
  }
  const location = dims.location;
  if (location && Array.isArray(location.zones)) {
    const zones = [];
    location.zones.forEach((zone, n) => {
      const reason = overlayZoneProblem(zone);
      if (reason) skipped.push({ kind: "zone", name: typeof zone?.id === "string" && zone.id ? zone.id : `zone #${n + 1}`, reason });
      else zones.push(zone);
    });
    layers.zones = { trusted: location.trusted === true && !provisional, nearest: [location.nearest_below?.id, location.nearest_above?.id].filter(Boolean), zones };
  } else {
    skipped.push({ kind: "zones", reason: "location not provided" });
  }
  if (!provisional) {
    for (const check of result.opportunity?.checklist ?? []) {
      if (!OVERLAY_CHECKS.includes(check.id) || !check.t) continue;
      const i = at(check.t);
      if (i == null) { skipped.push({ kind: "check", name: check.id, reason: "candle outside the window or not on the chart" }); continue; }
      layers.checks.push({ id: check.id, result: check.result, t: check.t, index: i, observed: check.observed, threshold: check.threshold, comparison: check.comparison, detail: check.detail, source: check.source });
    }
  }
  // Phase 2: historical EMA lines and confirmed swings from envelope.overlay (T5 values, copied
  // by the backend). Any inconsistency suppresses only these layers, with a reason.
  layers.lines = [];
  layers.swings = [];
  layers.coverage = null;
  const payload = envelope.overlay;
  const payloadProblem = overlayPayloadProblem(payload, { id: ids[0], select, result, win, verdict });
  if (payloadProblem) {
    skipped.push({ kind: "overlay", reason: payloadProblem });
  } else {
    const coverage = payload.coverage;
    const covFirst = isoMs(coverage.first_t), covLast = isoMs(coverage.last_t);
    const inCoverage = (t) => { const ms = isoMs(t); return Number.isFinite(ms) && ms >= covFirst && ms <= covLast; };
    layers.coverage = { first_t: coverage.first_t, last_t: coverage.last_t, count: coverage.count, truncated: coverage.truncated === true };
    // The chart loads BAR_COUNT bars including the forming candle, so the coverage's oldest
    // analysed candle(s) may precede the chart's first loaded candle: expected, not a data fault.
    const chartFirst = isoMs(bars[0]?.t);
    const beforeChart = (t) => inCoverage(t) && isoMs(t) < chartFirst;
    for (const name of Object.keys(OVERLAY_EMA)) {
      const series = payload.ema?.[name];
      if (!series || !Array.isArray(series.points)) { skipped.push({ kind: "ema_line", name, reason: "series not provided" }); continue; }
      const convergedFrom = isoMs(series.converged_from_t);
      const points = [];
      let dropped = 0, older = 0;
      for (const point of series.points) {
        const [t, value] = Array.isArray(point) ? point : [];
        const i = inCoverage(t) ? at(t) : null; // exact chart candle inside the window, or nothing
        if (i == null) { if (beforeChart(t)) older += 1; else dropped += 1; continue; }
        points.push({ t, index: i, value: isFiniteNumber(value) ? value : null, converged: Number.isFinite(convergedFrom) && isoMs(t) >= convergedFrom });
      }
      if (dropped) skipped.push({ kind: "ema_line", name, reason: `${dropped} point(s) outside the coverage or not on the chart` });
      if (older) skipped.push({ kind: "chart_range", name, expected: true, reason: `${older} point(s) older than the chart's first loaded candle` });
      points.sort((a, b) => a.index - b.index);
      layers.lines.push({ name, status: series.status, points });
    }
    if (!provisional && payload.swings?.status === "available" && payload.swings?.trusted === true && dims.swing_structure?.trusted === true) {
      const labelled = new Set(layers.pivots.map((pivot) => `${pivot.type}@${pivot.t}`));
      for (const swing of payload.swings.confirmed ?? []) {
        if (swing && beforeChart(swing.pivot_t) && at(swing.pivot_t) == null) {
          skipped.push({ kind: "chart_range", name: swing.id ?? "swing", expected: true, reason: "pivot older than the chart's first loaded candle" });
          continue;
        }
        const problem = overlaySwingProblem(swing, inCoverage, at, win.last_t, result.as_of_utc);
        if (problem) { skipped.push({ kind: "swing", name: swing?.id ?? "swing", reason: problem }); continue; }
        if (labelled.has(`${swing.type}@${swing.pivot_t}`)) continue; // already a labelled decision pivot
        layers.swings.push({ id: swing.id, type: swing.type, price: swing.price, t: swing.pivot_t, confirmed_at_utc: swing.confirmed_at_utc, index: at(swing.pivot_t) });
      }
    }
  }
  return { mode: provisional ? "provisional" : "current", reason: provisional ? "Provisional analysis: diagnostic only, nothing trusted." : null, layers, skipped, envelope };
}

// Why envelope.overlay cannot be used with this analysis and chart (null when it can).
export function overlayPayloadProblem(payload, { id, select, result, win, verdict }) {
  if (!payload || typeof payload !== "object") return "no overlay payload in this analysis";
  if (payload.status !== "available" && payload.status !== "partial") {
    return `overlay ${payload.status ?? "status missing"}${payload.reasons?.length ? `: ${payload.reasons.join("; ")}` : ""}`;
  }
  if (payload.snapshot_id !== id) return "overlay snapshot differs from the analysis";
  if (payload.symbol !== select.symbol || payload.timeframe !== select.timeframe) return "overlay symbol or timeframe differs from the chart";
  if (payload.broker_symbol !== result.broker_symbol) return "overlay broker symbol differs from the analysis";
  if (payload.window?.first_t !== win.first_t || payload.window?.last_t !== win.last_t || payload.last_analysed_t !== win.last_t) {
    return "overlay window differs from the analysis window";
  }
  if (payload.validation_verdict !== verdict) return "overlay validation verdict differs from the analysis";
  if (payload.as_of_utc !== result.as_of_utc) return "overlay as-of time differs from the analysis";
  const coverage = payload.coverage;
  const first = isoMs(coverage?.first_t), last = isoMs(coverage?.last_t);
  if (!Number.isFinite(first) || !Number.isFinite(last) || first > last || coverage.last_t !== win.last_t || first < isoMs(win.first_t)) {
    return "overlay coverage is missing or outside the analysis window";
  }
  return null;
}

// Why a confirmed swing cannot be drawn (null when it can). Never repaired or re-timed.
export function overlaySwingProblem(swing, inCoverage, at, lastT, asOf) {
  if (!swing || typeof swing !== "object") return "invalid swing record";
  if (swing.confirmation_status !== "confirmed") return "not confirmed";
  if (swing.type !== "high" && swing.type !== "low") return "invalid swing type";
  if (!isFiniteNumber(swing.price)) return "invalid price";
  if (!inCoverage(swing.pivot_t) || at(swing.pivot_t) == null) return "pivot candle outside the coverage or not on the chart";
  const pivot = isoMs(swing.pivot_t), confirmedBy = isoMs(swing.confirmed_by_t), confirmedAt = isoMs(swing.confirmed_at_utc);
  if (!Number.isFinite(confirmedBy) || confirmedBy <= pivot || confirmedBy > isoMs(lastT)) return "invalid confirming candle";
  if (!Number.isFinite(confirmedAt) || confirmedAt <= pivot) return "invalid confirmation time";
  if (confirmedAt > isoMs(asOf)) return "not yet confirmed at the analysed close";
  return null;
}

// The backend's three zone positions relative to the price (features.location_zones): a zone is
// entirely above it, entirely below it, or contains it.
export const ZONE_POSITIONS = ["above", "below", "contains"];

// Why a backend zone record cannot be drawn (null when it can). Nothing is repaired or invented.
export function overlayZoneProblem(zone) {
  if (!zone || typeof zone !== "object") return "invalid zone record";
  if (typeof zone.id !== "string" || !zone.id) return "invalid zone identifier";
  if (!isFiniteNumber(zone.zone_low) || !isFiniteNumber(zone.zone_high)) return "invalid price boundary";
  if (zone.zone_low > zone.zone_high) return "reversed price boundaries";
  if (!ZONE_POSITIONS.includes(zone.position)) return "invalid position";
  return null;
}

// Deterministic zone choice: the backend's nearest zone below and above price, then up to two
// more per side inside the visible price range, by backend distance (then id).
export function overlayZones(layer, low, high) {
  const byId = new Map(layer.zones.map((zone) => [zone.id, zone]));
  const chosen = layer.nearest.map((id) => byId.get(id)).filter(Boolean);
  const order = (a, b) => (a.distance_atr ?? Infinity) - (b.distance_atr ?? Infinity) || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0);
  // Zones containing the price are the most relevant: all of them, by id.
  layer.zones.filter((zone) => zone.position === "contains" && !chosen.includes(zone))
    .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0)).forEach((zone) => chosen.push(zone));
  for (const position of ["below", "above"]) {
    layer.zones
      .filter((zone) => zone.position === position && !chosen.includes(zone) && zone.zone_high >= low && zone.zone_low <= high)
      .sort(order).slice(0, 2).forEach((zone) => chosen.push(zone));
  }
  return chosen;
}

export function overlayPanelMarkup(model) {
  const badge = `<span class="badge ${OVERLAY_MODE_CLASS[model.mode] ?? ""}" data-overlay-state="${esc(model.mode)}">${esc(OVERLAY_MODE_LABELS[model.mode] ?? model.mode)}</span>`;
  if (!model.layers) {
    return `<div class="overlay-strip">${badge}<span class="overlay-message">${esc(model.reason ?? "")} No annotations are shown; the clean chart remains available.</span></div>`;
  }
  const result = model.envelope.result;
  const summary = result.market_condition?.summary ?? {};
  const decision = result.decision ?? {};
  const facts = [["Trend", summary.trend_regime], ["Bias", summary.directional_bias], ["Strength", summary.trend_strength], ["Volatility", summary.volatility]]
    .map(([label, value]) => `<span class="overlay-fact"><span>${label}</span> ${esc(humanize(value))}</span>`).join("");
  const outcome = `<span class="badge ${OUTCOME_CLASS[decision.outcome] ?? ""}" data-overlay-outcome="${esc(decision.outcome)}">${esc(OUTCOME_LABELS[decision.outcome] ?? decision.outcome)} · ${esc(decision.rule)}</span>`;
  const tick = model.envelope.window_validation?.recency?.reference?.tick_time_utc;
  const times = `Analysed through the candle opened ${esc(stamp(model.layers.endpoint.t))} · evaluated ${esc(stampSeconds(result.evaluated_at_utc))} · latest tick ${esc(tick ? stampSeconds(tick) : "—")}`;
  const zones = model.layers.zones;
  const zoneNote = zones && !zones.trusted ? '<span class="overlay-fact untrusted">Zones untrusted (muted)</span>' : "";
  const items = [
    ...model.layers.ema.map((e) => `${e.name} ${analysisValue(e.value)}${model.mode === "provisional" ? " (PROVISIONAL)" : ""} at the last analysed candle (${stamp(e.t)}) · features.latest.${e.name}`),
    ...model.layers.pivots.map((p) => `Swing ${p.label} (${p.role}, ${p.type}) ${analysisValue(p.price)} at ${stamp(p.t)}, confirmed ${stamp(p.confirmed_at_utc)} · features.swings`),
    ...(zones ? zones.nearest.map((id) => zones.zones.find((z) => z.id === id)).filter(Boolean).map((z) => `Nearest ${z.position} price: ${z.side} ${z.id} ${analysisValue(z.zone_low)}–${analysisValue(z.zone_high)}, strength ${z.strength}${zones.trusted ? "" : " (untrusted)"} · market_condition.location`) : []),
    ...(model.layers.pullback ? [`Pullback window: ${model.layers.pullback.rows.length} candle(s) from ${stamp(model.layers.pullback.rows[0].t)} · features.pullback`] : []),
    ...model.layers.checks.map((c) => `${c.id}: ${CHECK_RESULT_LABELS[c.result] ?? c.result} at ${stamp(c.t)} · observed ${analysisValue(c.observed)} vs ${analysisValue(c.threshold)}${c.detail ? ` · ${c.detail}` : ""}`),
    ...(model.layers.lines ?? []).map((line) => `${line.name} line: ${line.points.filter((p) => p.value != null).length} point(s)${model.mode === "provisional" ? " (PROVISIONAL)" : ""}, ${line.points.filter((p) => p.value != null && !p.converged).length} before convergence (dashed) · overlay.ema.${line.name}`),
    ...(model.layers.swings?.length ? [`Confirmed swings: ${model.layers.swings.length} (pivot time shown; each confirmed by the analysed close) · overlay.swings`] : []),
    ...(model.layers.coverage ? [`Historical lines and swings cover ${model.layers.coverage.count} analysed candle(s) from ${stamp(model.layers.coverage.first_t)}${model.layers.coverage.truncated ? " (the most recent part of the analysis window)" : ""}`] : []),
  ];
  const problems = model.skipped.filter((s) => !s.expected);
  const outside = model.skipped.filter((s) => s.expected);
  const skipped = (problems.length ? `<li class="overlay-skipped">${problems.length} element(s) not shown: ${esc(problems.map((s) => `${s.name ?? s.kind} (${s.reason})`).join("; "))}</li>` : "")
    + (outside.length ? `<li class="overlay-outside">Outside the loaded chart range (expected, not a data problem): ${esc(outside.map((s) => `${s.name ?? s.kind} (${s.reason})`).join("; "))}</li>` : "")
    + (state.overlay.last?.hiddenLabels?.length ? `<li class="overlay-outside">${state.overlay.last.hiddenLabels.length} label(s) hidden to avoid overlap; their markers and the entries above remain.</li>` : "");
  const list = `<details class="overlay-details"><summary>Annotations (${items.length})</summary><ul>${items.map((text) => `<li>${esc(text)}</li>`).join("")}${skipped}</ul></details>`;
  const message = model.reason ? `<span class="overlay-message">${esc(model.reason)}</span>` : "";
  return `<div class="overlay-strip">${badge}${outcome}${facts}${zoneNote}${message}</div>${candidateNotice(decision)}<div class="overlay-times">${times}</div>${list}`;
}

export function drawOverlay() {
  const canvas = document.querySelector("#analysis-overlay");
  const chart = document.querySelector("#price-chart");
  if (!canvas || !chart) return;
  if (!state.overlay.enabled) { clearOverlay(); return; }
  // The visual model is recomputed only when its inputs change (a new analysis, new chart data,
  // a state change or another selection); annotation toggles and viewport moves reuse it.
  const status = analysisStatus();
  const data = chartData();
  const cacheKey = JSON.stringify([chartKey(), status.state, status.reasons, state.analysisKey, state.analysisReceivedAt,
    status.envelope?.served_at_utc, status.envelope?.snapshot?.snapshot_id, state.barsKey, state.barsReceivedAt, data?.status, data?.bars?.length]);
  let model = state.overlay.modelCache?.key === cacheKey ? state.overlay.modelCache.model : null;
  if (!model) {
    model = overlayModel(status, data);
    state.overlay.modelRuns += 1;
    state.overlay.modelCache = { key: cacheKey, model };
  }
  const panel = document.querySelector("#overlay-panel");
  const bounds = chart.getBoundingClientRect();
  const width = bounds.width, height = bounds.height;
  canvas.style.left = `${chart.offsetLeft}px`;
  canvas.style.top = `${chart.offsetTop}px`;
  canvas.style.width = `${width}px`;
  canvas.style.height = `${height}px`;
  const viewport = state.chartViewport;
  const ratio = window.devicePixelRatio || 1;
  const prefs = annotationState().prefs;
  const signature = JSON.stringify([width, height, ratio, document.documentElement.dataset.theme, tz(),
    viewport && [viewport.start, viewport.end, viewport.count, viewport.low, viewport.high],
    model.mode, model.reason, model.envelope?.served_at_utc, model.envelope?.snapshot?.snapshot_id, prefs]);
  if (canvas.__signature === signature) {
    if (panel) setInner(panel, overlayPanelMarkup(model));
    return;
  }
  canvas.__signature = signature;
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);
  state.overlay.draws += 1;
  const drawn = [];
  const hiddenLabels = [];
  state.overlay.last = {
    mode: model.mode, reason: model.reason, annotations: drawn, skipped: model.skipped ?? [],
    snapshotId: model.envelope?.snapshot?.snapshot_id ?? null, hiddenLabels,
    hiddenLayers: ANNOTATION_KEYS.filter((key) => prefs[key] === false),
  };
  const style = getComputedStyle(document.documentElement);
  const color = (name) => style.getPropertyValue(name).trim();
  const pad = CHART_PAD;
  const plotW = width - pad.left - pad.right, plotH = height - pad.top - pad.bottom;
  ctx.font = "10px Consolas, monospace";
  // ---- deterministic label placement: markers stay at their exact coordinates; label text is
  // placed in priority order at the first free candidate position inside the plot, or hidden
  // (and listed) when no candidate is free. No data is moved or dropped.
  const occupied = [];
  const LABEL_H = 11;
  const overlaps = (a) => occupied.some((b) => a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y);
  const inside = (box) => ({
    ...box,
    x: clamp(box.x, pad.left + 2, pad.left + plotW - box.w - 2),
    y: clamp(box.y, pad.top + 2, pad.top + plotH - box.h - 2),
  });
  const reserve = (box) => { occupied.push(box); return box; };
  const STATUS_BOX = { x: pad.left + 4, y: pad.top + 4, w: 0, h: OVERLAY_STATUS_HEIGHT };
  const STATUS_BOTTOM = pad.top + 4 + OVERLAY_STATUS_HEIGHT;
  const statusText = OVERLAY_MODE_LABELS[model.mode] ?? model.mode;
  STATUS_BOX.w = Math.ceil(ctx.measureText(statusText).width) + 10;
  if (prefs.statusLabel !== false) reserve({ ...STATUS_BOX, h: STATUS_BOX.h + OVERLAY_LABEL_GAP - LABEL_H });
  // place(text, candidates: [{x, y}] top-left of the text box) -> box or null
  const place = (kind, text, candidates) => {
    const w = Math.ceil(ctx.measureText(text).width) + 4;
    for (const candidate of candidates) {
      const box = inside({ x: candidate.x, y: candidate.y, w, h: LABEL_H });
      if (!overlaps(box)) return reserve(box);
    }
    hiddenLabels.push({ kind, text });
    return null;
  };
  const labels = []; // [{priority, kind, text, candidates, draw(box)}], resolved after the shapes
  const drawStatus = () => {
    if (prefs.statusLabel === false) return;
    const tone = { current: "--green", provisional: "--amber", outdated: "--amber", unavailable: "--red" }[model.mode] ?? "--dim";
    ctx.save();
    ctx.globalAlpha = 1;
    ctx.setLineDash([]);
    ctx.fillStyle = color("--panel");
    ctx.fillRect(STATUS_BOX.x, STATUS_BOX.y, STATUS_BOX.w, STATUS_BOX.h);
    ctx.strokeStyle = color(tone);
    ctx.strokeRect(STATUS_BOX.x + 0.5, STATUS_BOX.y + 0.5, STATUS_BOX.w - 1, STATUS_BOX.h - 1);
    ctx.fillStyle = color(tone);
    ctx.fillText(statusText, STATUS_BOX.x + 5, STATUS_BOX.y + 11);
    ctx.restore();
    state.overlay.last.status = { text: statusText, ...STATUS_BOX, order: drawn.length };
  };
  const finish = () => {
    drawStatus(); // last: above every annotation layer
    if (panel) setInner(panel, overlayPanelMarkup(model));
  };
  if (!model.layers || !viewport || !Number.isFinite(viewport.low) || !Number.isFinite(viewport.high) || viewport.high === viewport.low) {
    finish();
    return;
  }
  const bars = chartGeometry()?.bars ?? [];
  const slot = plotW / viewport.count;
  const visible = (i) => i >= viewport.start && i <= viewport.end;
  const x = (i) => pad.left + slot * (i - viewport.start) + slot / 2;
  const y = (value) => pad.top + plotH * (viewport.high - value) / (viewport.high - viewport.low);
  const { layers } = model;
  const end = layers.endpoint.index;
  const endX = end > viewport.end ? pad.left + plotW : x(end) + slot / 2; // analysis never extends past its last candle
  const provisional = model.mode === "provisional";
  ctx.save();
  ctx.beginPath();
  ctx.rect(pad.left, pad.top, plotW, plotH);
  ctx.clip();
  if (provisional) ctx.globalAlpha = 0.6;
  // Support / resistance zones, up to the last analysed candle. Zones containing the price are
  // governed by their own toggle; the others by their side.
  if (layers.zones && end >= viewport.start) {
    for (const zone of overlayZones(layers.zones, viewport.low, viewport.high)) {
      if (zone.zone_high < viewport.low || zone.zone_low > viewport.high) continue;
      const containing = zone.position === "contains";
      const key = containing ? "containsZones" : zone.side === "resistance" ? "resistanceZones" : "supportZones";
      if (prefs[key] === false) continue;
      const top = y(zone.zone_high), bottom = y(zone.zone_low);
      const trusted = layers.zones.trusted;
      const toneVar = trusted ? (zone.side === "resistance" ? "--red" : "--green") : "--dim"; // untrusted: neutral grey
      const tone = color(toneVar);
      ctx.save();
      ctx.globalAlpha = (provisional ? 0.6 : 1) * (trusted ? (containing ? 0.18 : 0.12) : 0.06);
      ctx.fillStyle = tone;
      ctx.fillRect(pad.left, top, endX - pad.left, Math.max(1, bottom - top));
      ctx.globalAlpha = (provisional ? 0.6 : 1) * (trusted ? 0.55 : 0.35);
      ctx.strokeStyle = tone;
      ctx.lineWidth = containing ? 1.5 : 1; // price inside: a slightly firmer border
      ctx.setLineDash(trusted ? [] : [3, 3]);
      ctx.beginPath(); ctx.moveTo(pad.left, Math.round(top) + 0.5); ctx.lineTo(endX, Math.round(top) + 0.5);
      ctx.moveTo(pad.left, Math.round(bottom) + 0.5); ctx.lineTo(endX, Math.round(bottom) + 0.5); ctx.stroke();
      ctx.restore();
      const text = `${zone.side} ${zone.id} · ${zone.strength}${containing ? " · price inside zone" : ""}${trusted ? "" : " · untrusted"}`;
      const record = { kind: "zone", id: zone.id, side: zone.side, position: zone.position, trusted, color: toneVar, zone_low: zone.zone_low, zone_high: zone.zone_high, top, bottom, right: endX, label: text, labelY: null, labelBox: null };
      drawn.push(record);
      const firstY = Math.max(STATUS_BOTTOM + OVERLAY_LABEL_GAP, top - 3) - 9; // never under the status label
      labels.push({
        priority: 5, kind: "zone", text,
        candidates: [firstY, firstY + LABEL_H + 1, firstY - LABEL_H - 1, Math.min(bottom, firstY + 2 * (LABEL_H + 1))].map((ly) => ({ x: pad.left + 4, y: ly })),
        draw: (box) => {
          ctx.save();
          ctx.globalAlpha = (provisional ? 0.6 : 1) * (trusted ? 0.9 : 0.6);
          ctx.fillStyle = tone;
          ctx.fillText(text, box.x, box.y + 9);
          ctx.restore();
          record.labelY = box.y + 9;
          record.labelBox = box;
        },
      });
    }
  }
  // Historical EMA lines (phase 2): exact candle positions only; a line breaks at a missing,
  // unavailable or off-chart point and never extends past the last analysed candle. Points
  // before T5's convergence point are dashed and faint (explained by the legend).
  let warmupShown = false;
  const linesShown = [];
  for (const line of layers.lines ?? []) {
    if (prefs[EMA_ANNOTATION[line.name]] === false) continue;
    const runs = [];
    let run = [], previous = null;
    for (const point of line.points) {
      const ok = point.value != null && visible(point.index);
      if (!ok || (previous && point.index !== previous.index + 1)) { if (run.length) runs.push(run); run = []; }
      if (ok) { run.push(point); previous = point; } else { previous = null; }
    }
    if (run.length) runs.push(run);
    const tone = color(OVERLAY_EMA[line.name].color);
    ctx.save();
    ctx.strokeStyle = tone;
    ctx.lineWidth = 1.2;
    for (const segment of runs) {
      for (let k = 1; k < segment.length; k++) {
        const a = segment[k - 1], b = segment[k];
        const converged = a.converged && b.converged;
        if (!converged) warmupShown = true;
        ctx.globalAlpha = (provisional ? 0.6 : 1) * (converged ? 0.85 : 0.4);
        ctx.setLineDash(converged ? [] : [2, 3]);
        ctx.beginPath(); ctx.moveTo(x(a.index), y(a.value)); ctx.lineTo(x(b.index), y(b.value)); ctx.stroke();
      }
    }
    ctx.restore();
    if (runs.some((segment) => segment.length > 1)) linesShown.push(line.name);
    drawn.push({ kind: "ema_line", name: line.name, runs: runs.map((segment) => segment.map((p) => ({ t: p.t, x: x(p.index), y: y(p.value), converged: p.converged }))) });
  }
  // Pullback window (its own toggle), its EMA20/EMA50 values (the EMA toggles) and the TC4 candle.
  if (layers.pullback) {
    if (prefs.pullback !== false) {
      ctx.fillStyle = color("--accent");
      for (const row of layers.pullback.rows) {
        if (!visible(row.index)) continue;
        ctx.save(); ctx.globalAlpha = 0.07; ctx.fillRect(x(row.index) - slot / 2, pad.top, slot, plotH); ctx.restore();
        drawn.push({ kind: "pullback", t: row.t, x: x(row.index) });
      }
    }
    for (const name of ["ema20", "ema50"]) {
      if (prefs[name] === false) continue;
      const points = layers.pullback.rows.filter((row) => row[name] != null && visible(row.index)).sort((a, b) => a.index - b.index);
      if (points.length < 1) continue;
      ctx.strokeStyle = color(OVERLAY_EMA[name.toUpperCase()].color);
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      points.forEach((row, k) => (k ? ctx.lineTo(x(row.index), y(row[name])) : ctx.moveTo(x(row.index), y(row[name]))));
      ctx.stroke();
      ctx.lineWidth = 1;
      drawn.push({ kind: "ema_segment", name: name.toUpperCase(), points: points.map((row) => ({ t: row.t, x: x(row.index), y: y(row[name]) })) });
    }
  }
  // Setup checks: TC4 outline and TC3/TC4/TC5 markers, each with its own toggle.
  let pinStack = 0;
  for (const check of layers.checks) {
    if (!visible(check.index) || !bars[check.index]) continue;
    const bar = bars[check.index];
    const cx = x(check.index);
    if (check.id === "TC4_pullback" && prefs.tc4Outline !== false) {
      ctx.strokeStyle = color("--text");
      ctx.strokeRect(Math.round(cx - slot / 2) + 0.5, y(bar.h) - 3, Math.max(3, Math.round(slot) - 1), y(bar.l) - y(bar.h) + 6);
      drawn.push({ kind: "check_outline", id: check.id, t: check.t, x: cx });
    }
    if (prefs[CHECK_ANNOTATION[check.id]] === false) continue;
    const record = { kind: "check", id: check.id, result: check.result, t: check.t, x: cx, labelBox: null };
    drawn.push(record);
    const text = `${check.id.slice(0, 3)} ${CHECK_RESULT_LABELS[check.result] ?? check.result}`;
    const w = Math.ceil(ctx.measureText(text).width) + 4;
    const above = y(bar.h) - 8 - LABEL_H, below = y(bar.l) + 8;
    const k = pinStack++;
    labels.push({
      priority: 2, kind: "check", text,
      candidates: [above - k * (LABEL_H + 1), above - (k + 1) * (LABEL_H + 1), below + k * (LABEL_H + 1), above - (k + 2) * (LABEL_H + 1)].map((ly) => ({ x: cx - w / 2, y: ly })),
      draw: (box) => {
        ctx.fillStyle = color(check.result === "pass" ? "--green" : check.result === "fail" ? "--red" : "--dim");
        ctx.fillText(text, box.x + 2, box.y + 9);
        record.labelBox = box;
      },
    });
  }
  // Confirmed swings (phase 2): small unlabelled rings at the pivot candle, above highs and
  // below lows (swing highs and swing lows toggle separately); decision pivots are not repeated.
  ctx.strokeStyle = color("--muted");
  for (const swing of layers.swings ?? []) {
    if (!visible(swing.index)) continue;
    if (prefs[swing.type === "high" ? "swingHighs" : "swingLows"] === false) continue;
    const sx = x(swing.index), sy = y(swing.price) + (swing.type === "high" ? -6 : 6);
    ctx.beginPath(); ctx.arc(sx, sy, 2.5, 0, Math.PI * 2); ctx.stroke();
    drawn.push({ kind: "swing", id: swing.id, type: swing.type, t: swing.t, confirmed_at_utc: swing.confirmed_at_utc, x: sx, y: sy });
  }
  // Confirmed decision pivots.
  if (prefs.decisionPivots !== false) {
    for (const pivot of layers.pivots) {
      if (!visible(pivot.index)) continue;
      const px = x(pivot.index), py = y(pivot.price);
      const up = pivot.type === "low";
      ctx.fillStyle = color("--text");
      ctx.beginPath();
      if (up) { ctx.moveTo(px, py + 4); ctx.lineTo(px - 4, py + 10); ctx.lineTo(px + 4, py + 10); }
      else { ctx.moveTo(px, py - 4); ctx.lineTo(px - 4, py - 10); ctx.lineTo(px + 4, py - 10); }
      ctx.closePath(); ctx.fill();
      const record = { kind: "pivot", role: pivot.role, label: pivot.label, t: pivot.t, x: px, y: py, labelBox: null };
      drawn.push(record);
      const w = Math.ceil(ctx.measureText(pivot.label).width) + 4;
      const near = up ? py + 12 : py - 12 - LABEL_H;
      labels.push({
        priority: 4, kind: "pivot", text: pivot.label,
        candidates: [near, up ? near + LABEL_H + 1 : near - LABEL_H - 1].map((ly) => ({ x: px - w / 2, y: ly })),
        draw: (box) => { ctx.fillStyle = color("--muted"); ctx.fillText(pivot.label, box.x + 2, box.y + 9); record.labelBox = box; },
      });
    }
  }
  // The last analysed candle (not the evaluation time, not the latest tick).
  if (visible(end)) {
    if (prefs.endpointMarker !== false) {
      ctx.strokeStyle = color("--dim");
      ctx.setLineDash([2, 3]);
      ctx.beginPath(); ctx.moveTo(Math.round(endX) + 0.5, pad.top); ctx.lineTo(Math.round(endX) + 0.5, pad.top + plotH); ctx.stroke();
      ctx.setLineDash([]);
      const record = { kind: "endpoint", t: layers.endpoint.t, x: endX, labelBox: null };
      drawn.push(record);
      const text = `analysed through ${timeLabel(layers.endpoint.t, chartData()?.timeframe)}`;
      const w = Math.ceil(ctx.measureText(text).width) + 4;
      const base = pad.top + plotH - LABEL_H - 3;
      labels.push({
        priority: 3, kind: "endpoint", text,
        candidates: [base, base - LABEL_H - 1, base - 2 * (LABEL_H + 1)].map((ly) => ({ x: endX - w - 4, y: ly })),
        draw: (box) => { ctx.fillStyle = color("--dim"); ctx.fillText(text, box.x + 2, box.y + 9); record.labelBox = box; },
      });
    }
    // EMA tags anchored at the last analysed candle, labelled to its left (the analysed side).
    for (const ema of [...layers.ema].sort((a, b) => y(a.value) - y(b.value))) {
      if (prefs[EMA_ANNOTATION[ema.name]] === false) continue;
      const ey = y(ema.value);
      ctx.fillStyle = color(OVERLAY_EMA[ema.name].color);
      ctx.beginPath(); ctx.arc(x(end), ey, 2.5, 0, Math.PI * 2); ctx.fill();
      const text = `${ema.name} ${ema.value.toFixed(chartData()?.digits ?? 5)}${provisional ? " (PROVISIONAL)" : ""}`;
      const record = { kind: "ema", name: ema.name, value: ema.value, t: ema.t, x: x(end), y: ey, label: text, labelBox: null };
      drawn.push(record);
      const w = Math.ceil(ctx.measureText(text).width) + 4;
      const top = ey - 7;
      labels.push({
        priority: 1, kind: "ema", text,
        candidates: [0, LABEL_H + 1, -(LABEL_H + 1), 2 * (LABEL_H + 1), -2 * (LABEL_H + 1), 3 * (LABEL_H + 1)].map((dy) => ({ x: x(end) - w - 5, y: top + dy })),
        draw: (box) => {
          ctx.save();
          ctx.globalAlpha = 1; // readable on its own, not only through opacity
          ctx.fillStyle = color("--panel");
          ctx.fillRect(box.x, box.y, box.w, box.h);
          ctx.fillStyle = color(OVERLAY_EMA[ema.name].color);
          ctx.fillText(text, box.x + 2, box.y + 9);
          ctx.restore();
          record.labelBox = box;
        },
      });
    }
  }
  // Legend for the EMA lines shown, explaining the dashed warm-up history (T5's convergence rule).
  if (linesShown.length) {
    const parts = linesShown.map((name) => ({ name, tone: color(OVERLAY_EMA[name].color) }));
    const warm = warmupShown ? "dashed = warm-up, not yet converged (T5 1% rule)" : "";
    const sample = 14;
    const textW = parts.reduce((sum, part) => sum + sample + 4 + ctx.measureText(part.name).width + 10, 0) + (warm ? ctx.measureText(warm).width + 4 : 0);
    const w = Math.ceil(textW) + 6, h = LABEL_H + 2;
    const legendBox = [pad.top + plotH - h - 3, pad.top + plotH - 2 * h - 5]
      .map((ly) => inside({ x: pad.left + 4, y: ly, w, h }))
      .find((candidate) => !overlaps(candidate));
    {
      if (legendBox) {
        reserve(legendBox);
        ctx.save();
        ctx.globalAlpha = 1;
        ctx.fillStyle = color("--panel");
        ctx.fillRect(legendBox.x, legendBox.y, legendBox.w, legendBox.h);
        let lx = legendBox.x + 3;
        const ly = legendBox.y + legendBox.h / 2;
        for (const part of parts) {
          ctx.strokeStyle = part.tone;
          ctx.setLineDash(part.name === "EMA200" && warmupShown ? [2, 3] : []);
          ctx.beginPath(); ctx.moveTo(lx, ly); ctx.lineTo(lx + sample, ly); ctx.stroke();
          ctx.setLineDash([]);
          ctx.fillStyle = part.tone;
          ctx.fillText(part.name, lx + sample + 4, legendBox.y + 10);
          lx += sample + 4 + ctx.measureText(part.name).width + 10;
        }
        if (warm) { ctx.fillStyle = color("--dim"); ctx.fillText(warm, lx, legendBox.y + 10); }
        ctx.restore();
        drawn.push({ kind: "legend", lines: linesShown, warmup: warmupShown, text: warm, labelBox: legendBox });
      } else {
        hiddenLabels.push({ kind: "legend", text: warm || "EMA legend" });
      }
    }
  }
  // Resolve the labels: priority order, then stable input order.
  labels
    .map((label, index) => ({ ...label, index }))
    .sort((a, b) => a.priority - b.priority || a.index - b.index)
    .forEach((label) => {
      const box = place(label.kind, label.text, label.candidates);
      if (box) label.draw(box);
    });
  ctx.restore();
  finish();
}

// Introspection for tests: what the overlay last drew (null when cleared).
export function overlayState() {
  return { enabled: state.overlay.enabled, draws: state.overlay.draws, last: state.overlay.last ? JSON.parse(JSON.stringify(state.overlay.last)) : null };
}
