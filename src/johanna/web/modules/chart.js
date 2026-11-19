import { hooks } from "./hooks.js";
import { BARS_EXPIRE_MS, BAR_HISTORY_LIMIT, TIMEFRAMES, state } from "./state.js";
import { ageText, emptyState, esc, prefs, reasonText, setInner, stamp, statusBadge, tz } from "./format.js";
import { barsExpired, connection, quoteFor, quoteView } from "./status.js";

// ---------------------------------------------------------------- candlestick chart
export function chartMarkup(full = false) {
  const pairOptions = (prefs().currency_pair_watchlist ?? []).map((symbol) => `<option value="${esc(symbol)}" ${symbol === prefs().selected_currency_pair ? "selected" : ""}>${esc(symbol)}</option>`).join("");
  const tfOptions = TIMEFRAMES.map((tf) => `<option value="${tf}" ${tf === prefs().selected_chart_timeframe ? "selected" : ""}>${tf}</option>`).join("");
  const overlayToggle = full ? `${hooks.overlayToggleMarkup()}${hooks.annotationMenuMarkup()}` : "";
  const controls = `${overlayToggle}<select class="select" id="pair-select" aria-label="Selected currency pair" ${pairOptions ? "" : "disabled"}>${pairOptions || "<option>No pair in watchlist</option>"}</select><select class="select" id="timeframe-select" aria-label="Chart timeframe">${tfOptions}</select>`;
  return `<section class="panel chart-panel ${full ? "chart-large" : ""}"><div class="panel-head"><div class="chart-title-main"><span class="chart-symbol">${esc(prefs().selected_currency_pair)}</span><span class="chart-timeframe">${esc(prefs().selected_chart_timeframe)}</span><span id="chart-status">${chartStatusMarkup()}</span></div><div class="chart-tools">${controls}</div></div><div id="chart-notice">${chartNoticeMarkup()}</div>${full ? `<div class="overlay-panel" id="overlay-panel"${state.overlay.enabled ? "" : " hidden"}></div>` : ""}<div class="chart-canvas-wrap"><canvas class="chart-canvas" id="price-chart" aria-label="Candlestick chart for ${esc(prefs().selected_currency_pair)}"></canvas>${full ? `<canvas class="overlay-canvas" id="analysis-overlay" aria-hidden="true"${state.overlay.enabled ? "" : " hidden"}></canvas>` : ""}<div class="chart-view-controls" id="chart-view-controls"></div><div class="chart-empty" id="chart-empty">${chartEmptyMarkup()}</div></div><div class="chart-hint">Wheel: zoom · Drag: pan · Drag price axis: price scale (double-click resets) · Go Live: back to the latest candles</div><div class="panel-foot" id="chart-foot">${chartFootMarkup()}</div></section>`;
}

export const chartKey = () => `${prefs().selected_currency_pair}|${prefs().selected_chart_timeframe}`;

// How well a candle's prices are established, from the backend's fields only:
// "forming"   complete=false: still changing;
// "tick"      complete, and a server tick at/after its close was seen (prices confirmed);
// "clock"     complete only because its time has passed; no tick after the close yet, so a
//             feed stall cannot be ruled out: NOT confirmed price data;
// "unproven"  complete but without close/tick information (archived bars).
export const CLOSURE_EVIDENCE = { forming: "forming", tick_observed: "tick", clock_only: "clock", unknown: "unproven" };

export function candleEvidence(bar, latestTickUtc) {
  // The backend's per-candle "closure" is authoritative when present (T1); older responses
  // fall back to deriving it from complete / close_t / latest_tick_utc.
  if (bar.closure in CLOSURE_EVIDENCE) return CLOSURE_EVIDENCE[bar.closure];
  if (bar.complete === false) return "forming";
  const close = Date.parse(bar.close_t ?? "");
  const tick = Date.parse(latestTickUtc ?? "");
  if (!Number.isFinite(close) || !Number.isFinite(tick)) return "unproven";
  return tick >= close ? "tick" : "clock";
}

export const sameBar = (a, b) => a.o === b.o && a.h === b.h && a.l === b.l && a.c === b.c && a.v === b.v
  && a.complete === b.complete && a.close_t === b.close_t && a.closure === b.closure && a.valid === b.valid;

// A candle can be drawn only if all four prices are numbers (non-finite values arrive as null
// and are reported as invalid, never drawn or invented).
export const drawable = (bar) => [bar.o, bar.h, bar.l, bar.c].every((value) => typeof value === "number" && Number.isFinite(value));

// Merge a refreshed bar window into the chart's current one, keyed by open time. MT5's answer
// is authoritative: the forming candle takes its new OHLC, a new candle is appended when the
// timeframe advances, and nothing is invented or interpolated. Unchanged candles keep their
// existing objects, and no open time can appear twice. Candles older than the refreshed
// window (they drop out of the backend's fixed-size window as new ones arrive) are kept, up
// to BAR_HISTORY_LIMIT, so a user inspecting history never loses them.
export function mergeBars(previous, incoming) {
  const prior = new Map((previous ?? []).map((bar) => [bar.t, bar]));
  const seen = new Set();
  const merged = [];
  for (const bar of incoming) {
    if (seen.has(bar.t)) continue;
    seen.add(bar.t);
    const old = prior.get(bar.t);
    merged.push(old && sameBar(old, bar) ? old : bar);
  }
  const oldestIncoming = Math.min(...incoming.map((bar) => Date.parse(bar.t)));
  for (const bar of previous ?? []) {
    if (!seen.has(bar.t) && Date.parse(bar.t) < oldestIncoming) {
      seen.add(bar.t);
      merged.push(bar);
    }
  }
  return merged.sort((a, b) => Date.parse(a.t) - Date.parse(b.t)).slice(-BAR_HISTORY_LIMIT);
}
export const currentBarsError = () => (state.barsError?.key === chartKey() ? state.barsError : null);

// What the chart may show right now: bars fetched LIVE become CACHED once a refresh for the
// same pair/timeframe fails or no fresh response arrives in time. The bars stay visible.
export function chartData() {
  if (state.barsKey !== chartKey() || !state.bars) return null;
  const data = state.bars;
  // A failed/overdue quote refresh means the live connection is not confirmed either.
  const problem = currentBarsError()
    ?? (barsExpired() ? { reason: "no_recent_response", message: `No chart update for over ${BARS_EXPIRE_MS / 1000} s.` } : null)
    ?? quoteView()?.error
    ?? null;
  if (data.status !== "LIVE" || !problem) return data;
  return { ...data, status: "CACHED", reason: problem.reason, message: problem.message, refresh_failed: true };
}

export function chartStatusMarkup() {
  const data = chartData();
  if (!data) {
    const error = currentBarsError();
    if (error && !state.barsInFlight) return statusBadge("UNAVAILABLE", reasonText(error.reason));
    return `<span class="badge">${state.barsInFlight ? "Loading…" : "No data"}</span>`;
  }
  const bars = data.bars ?? [];
  const evidence = bars.map((bar) => candleEvidence(bar, data.latest_tick_utc));
  const forming = evidence.filter((kind) => kind === "forming").length;
  const unconfirmed = evidence.filter((kind) => kind === "clock").length;
  const invalid = bars.filter((bar) => bar.valid === false).length + (data.rejected_bars?.length ?? 0);
  return `${statusBadge(data.status)} <span class="badge">${bars.length} bars</span>${forming ? ` <span class="badge forming-badge">${forming} forming</span>` : ""}${unconfirmed ? ` <span class="badge unconfirmed-badge" title="Closed by time only: no tick after the close has been seen yet, so these prices are not confirmed complete">${unconfirmed} unconfirmed</span>` : ""}${invalid ? ` <span class="badge invalid-badge" title="Bars that failed the data checks (inconsistent or missing prices, duplicate or out-of-order times). Reported, never corrected.">${invalid} invalid</span>` : ""}`;
}

export function chartNoticeMarkup() {
  const data = chartData();
  if (data?.status === "CACHED") {
    const legacy = data.time_basis === "legacy_server_time";
    const source = data.source === "parquet_archive"
      ? "archived Parquet history"
      : `bars fetched ${data.fetched_at_utc ? `${ageText(data.fetched_at_utc)} ago` : "earlier"}`;
    const why = data.refresh_failed && data.message ? `${reasonText(data.reason)}: ${data.message}` : reasonText(data.reason);
    return `<div class="chart-notice warning">CACHED: showing ${esc(source)}, not live (${esc(why)}).${legacy ? " These archived bars were recorded in MT5 server time, not UTC, so their times are shifted." : ""}</div>`;
  }
  const check = connection().time_check ?? {};
  if (data?.status === "LIVE" && check.result !== "verified") {
    const detail = check.result === "stale_ticks"
      ? " (no recent ticks, market closed or quiet)"
      : check.result === "unavailable" ? ` (${reasonText(check.reason) || "MT5 unavailable"})` : "";
    return `<div class="chart-notice info">Bar times assume the configured server clock (${esc(connection().server_clock ?? "UTC")}); not yet verified against a live tick${esc(detail)}.</div>`;
  }
  return "";
}

export function chartEmptyMarkup() {
  const data = chartData();
  if (data?.bars?.length) return "";
  const error = currentBarsError();
  if (!data && error) return emptyState("Chart unavailable", `${reasonText(error.reason)}: ${error.message}`, "error-state");
  if (!data) return emptyState("Loading chart", "Requesting bars from MT5…");
  return emptyState("No chart data", data.message || reasonText(data.reason) || "MT5 returned no bars.");
}

export function chartFootMarkup() {
  const data = chartData();
  if (!data?.bars?.length) return `<span>Bars are fetched from MT5 on demand.</span>`;
  const last = data.bars.at(-1);
  const source = data.source === "mt5" ? `MT5${data.broker_symbol && data.broker_symbol !== data.symbol ? ` (${data.broker_symbol})` : ""}` : "Parquet archive";
  // Completion comes from each bar's "complete" flag (backend), never from its position.
  const forming = data.bars.filter((bar) => bar.complete === false).at(-1);
  const candles = forming
    ? `forming candle (hollow) opened ${stamp(forming.t)}${forming.close_t ? `, closes ${stamp(forming.close_t)}` : ""}`
    : "all candles complete";
  const unconfirmed = data.bars.filter((bar) => candleEvidence(bar, data.latest_tick_utc) === "clock").length;
  const caution = unconfirmed
    ? ` · ${unconfirmed} closed by clock only (faded): no tick after the close yet, prices not confirmed`
    : "";
  return `<span class="chart-candles">Source: ${esc(source)} · ${esc(data.timeframe)} · ${esc(candles)}${esc(caution)}</span><span>Last bar opened ${esc(stamp(last.t))} (${esc(tz())})</span>`;
}

export function renderChartArea() {
  const status = document.querySelector("#chart-status");
  if (status) status.innerHTML = chartStatusMarkup();
  const notice = document.querySelector("#chart-notice");
  if (notice) notice.innerHTML = chartNoticeMarkup();
  const empty = document.querySelector("#chart-empty");
  if (empty) {
    empty.innerHTML = chartEmptyMarkup();
    empty.hidden = Boolean(chartData()?.bars?.length);
  }
  const foot = document.querySelector("#chart-foot");
  if (foot) foot.innerHTML = chartFootMarkup();
  drawChart();
}

export function timeLabel(iso, timeframe) {
  const options = timeframe === "D1"
    ? { timeZone: tz(), month: "short", day: "numeric" }
    : timeframe === "H4" || timeframe === "H1"
      ? { timeZone: tz(), month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" }
      : { timeZone: tz(), hour: "2-digit", minute: "2-digit" };
  return new Intl.DateTimeFormat(undefined, options).format(new Date(iso));
}

// ---------------------------------------------------------------- chart viewport (zoom & pan)
// The viewport is a view onto the candles, never a change to them. It is anchored to the open
// time of its right-most candle (unless it follows the newest candle), so candles arriving or
// leaving the fetched window never move what the user is looking at.
export const CHART_PAD = { left: 6, right: 70, top: 10, bottom: 22 };
export const MIN_VISIBLE_CANDLES = 10; // most zoomed in
export const MIN_SLOT_PX = 2; // most zoomed out: candle centres at least 2 px apart, so no overlap
export const DEFAULT_SLOT_PX = 5;
export const PRICE_SCALE_MIN = 0.1; // price range shown, relative to the automatic fit
export const PRICE_SCALE_MAX = 10;
export const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

export const defaultView = (key) => ({ key, follow: true, count: null, endT: null, priceScale: 1 });

// Viewport for the selected pair/timeframe; another dataset always starts at the latest candles.
export function chartView() {
  const key = chartKey();
  if (state.view?.key !== key) state.view = defaultView(key);
  return state.view;
}

// Index of the last candle opened at or before `time` (-1 if none). Candles are sorted.
export function lastIndexAtOrBefore(bars, time) {
  let low = 0, high = bars.length - 1, found = -1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (Date.parse(bars[mid].t) <= time) { found = mid; low = mid + 1; } else { high = mid - 1; }
  }
  return found;
}

// Visible candle range [start, end] for `bars` drawn `plotW` px wide.
export function resolveViewport(bars, plotW) {
  const view = chartView();
  const total = bars.length;
  const maxCount = Math.max(1, Math.min(total, Math.floor(plotW / MIN_SLOT_PX)));
  const minCount = Math.min(MIN_VISIBLE_CANDLES, maxCount);
  const automatic = Math.min(total, Math.max(20, Math.floor(plotW / DEFAULT_SLOT_PX)));
  const count = clamp(view.count ?? automatic, minCount, maxCount);
  let end = total - 1;
  if (!view.follow && view.endT) end = Math.max(0, lastIndexAtOrBefore(bars, Date.parse(view.endT)));
  end = clamp(end, count - 1, total - 1);
  return { start: end - count + 1, end, count, minCount, maxCount, total, follow: view.follow };
}

// Record a new visible range. Reaching the newest candle resumes following it.
export function setViewport(bars, start, count) {
  const view = chartView();
  const end = start + count - 1;
  view.count = count;
  if (end >= bars.length - 1) {
    view.follow = true;
    view.endT = null;
  } else {
    view.follow = false;
    view.endT = bars[end].t;
  }
}

export function goLive() {
  const view = chartView();
  view.follow = true;
  view.endT = null;
  drawChart();
}

// Canvas position and drawing geometry shared by drawing and pointer handling.
export function chartGeometry() {
  const canvas = document.querySelector("#price-chart");
  if (!canvas) return null;
  const rect = canvas.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) return null;
  const plotW = rect.width - CHART_PAD.left - CHART_PAD.right;
  const plotH = rect.height - CHART_PAD.top - CHART_PAD.bottom;
  return { canvas, rect, plotW, plotH, bars: (chartData()?.bars ?? []).filter(drawable) };
}

export function viewControlsMarkup(viewport) {
  if (!viewport) return "";
  if (viewport.follow) return '<span class="view-state following" title="The chart follows the newest candle">Following latest</span>';
  return '<span class="view-state history">Viewing history</span><button class="go-live" id="go-live" type="button" title="Return to the newest candles and follow them">Go Live</button>';
}

export function drawChart() {
  state.drawChartCalls += 1;
  const canvas = document.querySelector("#price-chart");
  const data = chartData();
  const all = (data?.bars ?? []).filter(drawable);
  if (!canvas) return;
  const bounds = canvas.getBoundingClientRect();
  if (bounds.width === 0 || bounds.height === 0) return;
  const ratio = window.devicePixelRatio || 1;
  const width = bounds.width, height = bounds.height;
  const pad = CHART_PAD;
  const plotW = width - pad.left - pad.right, plotH = height - pad.top - pad.bottom;
  const viewport = all.length >= 2 ? resolveViewport(all, plotW) : null;
  const priceScale = chartView().priceScale;
  setInner(document.querySelector("#chart-view-controls"), viewControlsMarkup(viewport));
  const bars = viewport ? all.slice(viewport.start, viewport.end + 1) : [];
  const quote = data?.status === "LIVE" ? quoteFor(data.symbol) : null;
  const livePrice = quote && (quote.status === "LIVE" || quote.status === "STALE") ? quote.bid : null;
  // Repaint only when something visible changed (data, viewport, live line, size, theme, ...).
  const signature = JSON.stringify([
    width, height, ratio, document.documentElement.dataset.theme, tz(), data?.timeframe, data?.digits,
    livePrice, viewport && [viewport.start, viewport.end], priceScale,
    bars.map((bar) => [bar.t, bar.o, bar.h, bar.l, bar.c, candleEvidence(bar, data.latest_tick_utc)]),
  ]);
  // What is in view is recorded on every call (new candles can arrive outside the view);
  // the price range only changes on an actual repaint.
  state.chartViewport = viewport
    ? { ...state.chartViewport, ...viewport, firstT: bars[0].t, lastT: bars.at(-1).t, newestT: all.at(-1).t, priceScale }
    : null;
  if (canvas.__signature === signature) return;
  canvas.__signature = signature;
  state.chartDraws += 1;
  canvas.width = Math.floor(width * ratio);
  canvas.height = Math.floor(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.scale(ratio, ratio);
  ctx.clearRect(0, 0, width, height);
  if (!viewport) {
    if (state.overlay.enabled) hooks.drawOverlay();
    return;
  }
  const style = getComputedStyle(document.documentElement);
  const color = (name) => style.getPropertyValue(name).trim();
  const digits = data.digits ?? 5;
  // Automatic fit to the visible candles (and live price), then the user's price scale around
  // its centre. Only the mapping from price to pixels changes; OHLC values never do.
  let low = Math.min(...bars.map((bar) => bar.l)), high = Math.max(...bars.map((bar) => bar.h));
  if (livePrice != null && viewport.follow) { low = Math.min(low, livePrice); high = Math.max(high, livePrice); }
  const margin = (high - low) * 0.06 || Math.abs(high) * 0.0005 || 1;
  const centre = (low + high) / 2;
  const half = ((high - low) / 2 + margin) * priceScale;
  low = centre - half; high = centre + half;
  const y = (value) => pad.top + plotH * (high - value) / (high - low);
  const slot = plotW / bars.length;
  const body = Math.max(1, Math.min(12, slot * 0.64));
  state.chartViewport = { ...state.chartViewport, low, high };

  ctx.font = "10px Consolas, monospace";
  ctx.lineWidth = 1;
  for (let step = 0; step <= 5; step++) {
    const value = high - (high - low) * step / 5;
    const gy = Math.round(y(value)) + 0.5;
    ctx.strokeStyle = color("--line-soft");
    ctx.beginPath(); ctx.moveTo(pad.left, gy); ctx.lineTo(width - pad.right, gy); ctx.stroke();
    ctx.fillStyle = color("--dim");
    ctx.fillText(value.toFixed(digits), width - pad.right + 6, gy + 3);
  }
  const labelEvery = Math.max(1, Math.ceil(bars.length / Math.max(2, Math.floor(plotW / 110))));
  bars.forEach((bar, index) => {
    const cx = pad.left + slot * index + slot / 2;
    if (index % labelEvery === 0) {
      ctx.fillStyle = color("--dim");
      const label = timeLabel(bar.t, data.timeframe);
      const labelW = ctx.measureText(label).width;
      ctx.fillText(label, clamp(cx - labelW / 2, pad.left, width - pad.right - labelW), height - 6);
    }
  });
  // Candles and the live line are clipped to the plot area (a zoomed price scale may cut them).
  ctx.save();
  ctx.beginPath();
  ctx.rect(pad.left, pad.top, plotW, plotH);
  ctx.clip();
  bars.forEach((bar, index) => {
    const cx = pad.left + slot * index + slot / 2;
    const up = bar.c >= bar.o;
    const tone = up ? color("--green") : color("--red");
    const forming = bar.complete === false; // drawn hollow with a dashed wick: still changing
    // Closed by clock only (no tick after its close yet): drawn faded, prices unconfirmed.
    ctx.globalAlpha = candleEvidence(bar, data.latest_tick_utc) === "clock" ? 0.45 : 1;
    ctx.strokeStyle = tone;
    ctx.fillStyle = tone;
    if (forming) ctx.setLineDash([2, 2]);
    ctx.beginPath(); ctx.moveTo(Math.round(cx) + 0.5, y(bar.h)); ctx.lineTo(Math.round(cx) + 0.5, y(bar.l)); ctx.stroke();
    ctx.setLineDash([]);
    const top = y(Math.max(bar.o, bar.c));
    const h = Math.max(1, y(Math.min(bar.o, bar.c)) - top);
    const left = Math.round(cx - body / 2), bodyW = Math.max(1, Math.round(body));
    if (forming) {
      ctx.fillStyle = color("--bg");
      ctx.fillRect(left, top, bodyW, h);
      ctx.strokeRect(left + 0.5, top + 0.5, Math.max(1, bodyW - 1), Math.max(1, h - 1));
    } else {
      ctx.fillRect(left, top, bodyW, h);
    }
    ctx.globalAlpha = 1;
  });
  const lineValue = livePrice ?? all.at(-1).c;
  const ly = Math.round(y(lineValue)) + 0.5;
  ctx.strokeStyle = color("--accent");
  ctx.setLineDash([4, 3]);
  ctx.beginPath(); ctx.moveTo(pad.left, ly); ctx.lineTo(width - pad.right, ly); ctx.stroke();
  ctx.setLineDash([]);
  ctx.restore();
  if (ly >= pad.top && ly <= pad.top + plotH) { // price tag only while the line is in view
    ctx.fillStyle = color("--accent");
    ctx.fillRect(width - pad.right + 2, ly - 8, pad.right - 4, 16);
    ctx.fillStyle = color("--bg");
    ctx.fillText(lineValue.toFixed(digits), width - pad.right + 6, ly + 3);
  }
  if (state.overlay.enabled) hooks.drawOverlay(); // after every chart repaint; never otherwise
}

// ---------------------------------------------------------------- chart interaction
// Wheel: zoom around the cursor. Drag on the plot: pan (left = older, right = newer).
// Drag on the price axis: price scale (down = wider range); double-click it to reset.
export let chartDrag = null;

export const onPriceAxis = (geometry, clientX) => clientX - geometry.rect.left > geometry.rect.width - CHART_PAD.right;

// The candle under a screen x position (also used by tests).
export function candleAtX(clientX) {
  const geometry = chartGeometry();
  const viewport = state.chartViewport;
  if (!geometry || !viewport) return null;
  const fraction = clamp((clientX - geometry.rect.left - CHART_PAD.left) / geometry.plotW, 0, 0.999999);
  return geometry.bars[viewport.start + Math.floor(fraction * viewport.count)]?.t ?? null;
}

export function zoomChart(clientX, zoomIn) {
  const geometry = chartGeometry();
  if (!geometry || geometry.bars.length < 2) return;
  const viewport = resolveViewport(geometry.bars, geometry.plotW);
  const fraction = clamp((clientX - geometry.rect.left - CHART_PAD.left) / geometry.plotW, 0, 1);
  const anchor = viewport.start + fraction * viewport.count; // candle position under the cursor
  let count = Math.round(viewport.count * (zoomIn ? 1 / 1.25 : 1.25));
  if (count === viewport.count) count += zoomIn ? -1 : 1;
  count = clamp(count, viewport.minCount, viewport.maxCount);
  if (count === viewport.count) return;
  // Same floor convention as candleAtX, so the candle under the cursor stays exactly in place
  // at any canvas width (rounding here drifted by up to a candle per wheel step).
  const start = clamp(Math.floor(anchor) - Math.floor(fraction * count), 0, geometry.bars.length - count);
  setViewport(geometry.bars, start, count);
  drawChart();
}

document.addEventListener("wheel", (event) => {
  if (event.target.id !== "price-chart" || !event.deltaY) return;
  event.preventDefault(); // zoom the chart, not the page
  zoomChart(event.clientX, event.deltaY < 0);
}, { passive: false });

document.addEventListener("pointerdown", (event) => {
  if (event.target.id !== "price-chart" || event.button !== 0) return;
  const geometry = chartGeometry();
  if (!geometry || geometry.bars.length < 2) return;
  event.preventDefault(); // no text selection or native drag
  const viewport = resolveViewport(geometry.bars, geometry.plotW);
  chartDrag = {
    pointerId: event.pointerId,
    canvas: geometry.canvas,
    mode: onPriceAxis(geometry, event.clientX) ? "price" : "pan",
    x: event.clientX,
    y: event.clientY,
    start: viewport.start,
    count: viewport.count,
    slot: geometry.plotW / viewport.count,
    priceScale: chartView().priceScale,
  };
  geometry.canvas.setPointerCapture(event.pointerId); // release outside the chart still ends it
  geometry.canvas.style.cursor = chartDrag.mode === "price" ? "ns-resize" : "grabbing";
});

document.addEventListener("pointermove", (event) => {
  const canvas = document.querySelector("#price-chart");
  if (!chartDrag) {
    if (event.target === canvas) {
      const geometry = chartGeometry();
      if (geometry) canvas.style.cursor = onPriceAxis(geometry, event.clientX) ? "ns-resize" : "grab";
    }
    return;
  }
  if (event.pointerId !== chartDrag.pointerId) return;
  if (chartDrag.canvas !== canvas) { chartDrag = null; return; } // page re-rendered mid-drag
  const geometry = chartGeometry();
  if (!geometry || geometry.bars.length < 2) return;
  if (chartDrag.mode === "price") {
    const scale = chartDrag.priceScale * Math.exp((event.clientY - chartDrag.y) * 0.01);
    chartView().priceScale = clamp(scale, PRICE_SCALE_MIN, PRICE_SCALE_MAX);
  } else {
    // Dragging left moves towards older candles, dragging right towards newer ones.
    const shift = Math.round((event.clientX - chartDrag.x) / chartDrag.slot);
    const count = Math.min(chartDrag.count, geometry.bars.length);
    setViewport(geometry.bars, clamp(chartDrag.start + shift, 0, geometry.bars.length - count), count);
  }
  drawChart();
});

export function endChartDrag(event) {
  if (!chartDrag || (event.pointerId !== undefined && event.pointerId !== chartDrag.pointerId)) return;
  const canvas = chartDrag.canvas;
  chartDrag = null;
  if (canvas.hasPointerCapture?.(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
  canvas.style.cursor = "";
}
document.addEventListener("pointerup", endChartDrag);
document.addEventListener("pointercancel", endChartDrag);
document.addEventListener("lostpointercapture", endChartDrag);

document.addEventListener("dblclick", (event) => {
  if (event.target.id !== "price-chart") return;
  const geometry = chartGeometry();
  if (!geometry || !onPriceAxis(geometry, event.clientX)) return;
  chartView().priceScale = 1; // back to the automatic price fit
  drawChart();
});

// Introspection for diagnostics and tests: the range currently drawn.
export function chartViewState() {
  return state.chartViewport ? { ...state.chartViewport } : null;
}
