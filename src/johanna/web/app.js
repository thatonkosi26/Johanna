const NAV_ITEMS = [
  ["overview", "Overview", "M3 3h7v7H3zM14 3h7v5h-7zM14 11h7v10h-7zM3 13h7v8H3z"],
  ["charts", "Charts", "M3 3v18h18M7 15l4-4 4 3 6-8"],
  ["markets", "Markets", "M4 19V5m0 14h17M8 16V9m4 7V6m4 10v-5m4 5V4"],
  ["calendar", "Economic Calendar", "M4 5h16v16H4zM8 3v4m8-4v4M4 10h16M8 14h2m4 0h2m-8 4h2"],
  ["news", "News", "M5 4h14a2 2 0 0 1 2 2v14H5a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2Zm3 5h8m-8 4h8m-8 4h5"],
  ["signals", "Trading Signals", "M3 17h4l3-10 4 12 3-8h4"],
  ["rules", "Trading Rules", "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"],
  ["risk", "Risk Management", "M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11Zm-3-11 2 2 4-4"],
  ["positions", "Positions", "M4 7h16M4 12h10M4 17h7"],
  ["history", "Trade History", "M3 12a9 9 0 1 0 3-6.7L3 8m0-5v5h5m4-1v5l3 2"],
  ["analytics", "Analytics", "M4 19V5m0 14h17M8 16l3-4 3 2 5-7"],
  ["settings", "Settings", "M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7ZM19 13l2-1-2-1-.5-2 1-2-2-1-2 1-2-.5-1-2-1 2-2 .5-2-1-1 2 1 2-.5 2-2 1 2 1 .5 2-1 2 2 1 2-1 2 .5 1 2 1-2 2-.5 2 1 1-2-1-2z"],
];
const TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];
const BAR_COUNT = 300;
// Candles kept per chart: the newest fetched window plus older ones it has moved past.
const BAR_HISTORY_LIMIT = 1000;
// Live polling targets (not guarantees). Each poll is scheduled only after the previous one
// has finished, so requests never overlap; failures back off; a hidden tab suspends polling.
const POLL = {
  quotesMs: 3000,
  barsMs: 5000,
  quietQuotesMs: 6000, // no LIVE quote (market closed, MT5 unavailable): fewer requests
  quietBarsMs: 20000, // chart not LIVE, or no tick for 5 minutes
  quietTickMs: 300000,
  maxBackoffMs: 30000,
  // A request still unanswered after this long is abandoned and counts as a failure. Longer
  // than the backend's own worst case (200-pair quotes in 8 batches; bars wait + timeout).
  quotesTimeoutMs: 45000,
  barsTimeoutMs: 45000,
  dashboardMs: 30000,
  // Market analysis (T6 via /api/market/analysis): a new backend evaluation per request.
  analysisMs: 30000, // chart LIVE
  quietAnalysisMs: 60000, // chart not LIVE
  analysisTimeoutMs: 45000,
};
// A stored response stops counting as live when no fresh one has arrived for this long,
// whatever the reason (request failed, hung, backend gone, tab was hidden).
const QUOTES_EXPIRE_MS = 10000;
const BARS_EXPIRE_MS = 65000;
const MAX_WATCHLIST = 200; // same limit as the backend (persistence.MAX_WATCHLIST)
// Presentation policy only: after this long without a new evaluation, a displayed analysis
// stops being called current. It never relaxes the backend's 5 s data-freshness rule (T2).
const ANALYSIS_EXPIRE_MS = 65000;
const ANALYSIS_TIMEFRAMES = ["H1", "H4", "D1"]; // the backend's T6 scope; others are never requested

const PAGE_IDS = new Set(NAV_ITEMS.map(([id]) => id));
const pageFromHash = () => {
  const id = window.location.hash.replace(/^#/, "");
  return PAGE_IDS.has(id) ? id : "overview";
};
const state = {
  data: null,
  page: pageFromHash(),
  loading: true,
  error: null,
  // Live MT5 data from /api/market/*; never mixed with the dashboard snapshot. These hold
  // the last *successful* responses; what is displayed is derived by quoteView()/chartData(),
  // which downgrade them once a refresh fails or they expire. Never render them directly.
  quotes: null,
  quotesReceivedAt: 0,
  quotesError: null, // { reason, message } of the latest failed refresh
  quotesSeq: 0, // only the newest request may update state (older answers are discarded)
  bars: null,
  barsKey: null,
  barsReceivedAt: 0,
  barsError: null, // { key, reason, message }
  barsInFlight: false,
  barsSeq: 0,
  chartDraws: 0, // canvas repaints (unchanged data is not repainted)
  view: null, // chart viewport (zoom, pan, price scale) for one pair/timeframe; see chartView()
  chartViewport: null, // the candle range last drawn
  // Market analysis: the last *successful* /api/market/analysis response and its request
  // bookkeeping. Kept apart from chart and viewport state; displayed via analysisStatus().
  analysis: null,
  analysisKey: null,
  analysisReceivedAt: 0, // browser receipt time (Date.now()); never compared with backend times
  analysisError: null, // { key, reason, message } of the latest failed refresh
  analysisInFlight: false,
  analysisSeq: 0,
  analysisCandle: null, // { key, t } latest closed chart candle seen (early-refresh trigger)
};
const main = document.querySelector("#main-content");
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
const prefs = () => state.data?.preferences ?? {};
const services = () => state.data?.status?.services ?? {};
const tz = () => prefs().user_timezone || "UTC";
const price = (value, digits) => value == null || !Number.isFinite(Number(value)) ? "—" : Number(value).toFixed(digits ?? 5);
const stamp = (value, timezone = tz()) => {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, { timeZone: timezone, dateStyle: "medium", timeStyle: "short" }).format(date);
};
const clockTime = (value) => {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, { timeZone: tz(), hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
};
function ageText(isoTime) {
  if (!isoTime) return "—";
  const seconds = Math.max(0, (Date.now() - new Date(isoTime).getTime()) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

const REASONS = {
  terminal_not_running: "MT5 terminal is not running",
  connect_failed: "Cannot connect to MT5",
  not_connected_to_server: "MT5 is offline from its trade server",
  no_account: "No account is logged in to MT5",
  server_clock_mismatch: "MT5 server clock does not match UTC",
  timeout: "MT5 did not respond in time",
  mt5_busy: "MT5 is busy or frozen",
  mt5_error: "MT5 request failed",
  connection_lost: "Connection to MT5 was lost",
  symbol_not_offered: "Not offered by this broker",
  symbol_loading: "Waiting for MT5 to load this symbol",
  no_data: "No data",
  terminal_path_mismatch: "A different MT5 terminal is running",
  multiple_terminals: "Several MT5 terminals are running",
  terminal_unverified: "Cannot confirm the MT5 terminal is running",
  worker_failed: "MT5 worker could not start",
  refresh_failed: "Refresh failed",
  backend_unreachable: "Backend not reachable",
  no_recent_response: "No recent update",
  invalid_symbols: "Invalid watchlist request",
  request_timeout: "Request timed out",
  malformed_response: "Unreadable response",
  internal_error: "Backend error",
  not_rechecked: "not rechecked recently",
  no_usable_tick: "no usable tick",
};
const reasonText = (code) => REASONS[code] ?? (code ? String(code).replaceAll("_", " ") : "");
const STATUS_HELP = {
  LIVE: "Fetched from MT5 just now",
  STALE: "Fetched from MT5 just now, but the last tick is old (market closed or quiet)",
  CACHED: "MT5 unavailable: last value fetched earlier, not live",
  UNAVAILABLE: "No data available",
  LOADING: "Waiting for the first answer from MT5",
};
function statusBadge(status, extra = "") {
  const value = status || "UNAVAILABLE";
  return `<span class="status-badge ${value.toLowerCase()}" title="${esc(STATUS_HELP[value] ?? "")}">${esc(value)}${extra ? ` · ${esc(extra)}` : ""}</span>`;
}

function icon(path) { return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${path}"/></svg>`; }
function renderNavigation() {
  document.querySelector("#primary-nav").innerHTML = NAV_ITEMS.map(([id, label, path]) => `
    <button class="nav-link ${state.page === id ? "active" : ""}" data-page="${id}" ${state.page === id ? 'aria-current="page"' : ""}>${icon(path)}<span>${label}</span></button>`).join("");
}

// Backend service states (Settings > Application services). Only "running"/"connected" are green.
const STATE_LABELS = {
  running: ["Running", "online"],
  connected: ["Connected", "online"],
  idle: ["Idle", ""],
  unavailable: ["Unavailable", "warning"],
  degraded: ["Degraded", "warning"],
  error: ["Failed", "error"],
  exited: ["Stopped unexpectedly", "error"],
  stopped: ["Stopped", ""],
  paused: ["Paused", ""],
  not_configured: ["Not configured", ""],
  not_implemented: ["Not implemented", ""],
  disabled: ["Disabled", ""],
};
const FRESHNESS_LABELS = {
  fresh: ["Fresh", "online"],
  stale: ["Stale", "warning"],
  no_data: ["No data", ""],
  no_snapshot: ["No snapshot", ""],
  error: ["Unreadable", "error"],
};
const stateLabel = (value) => STATE_LABELS[value] ?? [String(value || "Unknown").replaceAll("_", " "), ""];
const freshnessLabel = (value) => FRESHNESS_LABELS[value] ?? ["Unknown", ""];
const calendarStatus = () => state.data?.calendar?.status ?? services().economic_calendar ?? {};

function overallStatus(rows) {
  const statuses = new Set(rows.map((row) => row.status));
  return ["LIVE", "STALE", "CACHED"].find((status) => statuses.has(status)) ?? "UNAVAILABLE";
}
const quotesExpired = () => Boolean(state.quotes) && Date.now() - state.quotesReceivedAt > QUOTES_EXPIRE_MS;
const barsExpired = () => Boolean(state.bars) && Date.now() - state.barsReceivedAt > BARS_EXPIRE_MS;

// What the dashboard may show for quotes right now. After a failed or overdue refresh,
// previously fetched prices stay visible but become CACHED (with their age), the reason is
// shown, and the clock check is no longer "verified".
function quoteView() {
  const payload = state.quotes;
  const problem = state.quotesError
    ?? (quotesExpired() ? { reason: "no_recent_response", message: `No quote update for over ${QUOTES_EXPIRE_MS / 1000} s.` } : null);
  if (!payload) {
    if (!problem) return null;
    return {
      status: "UNAVAILABLE",
      quotes: [],
      error: problem,
      connection: { state: "unavailable", unavailable_reason: problem.reason, message: problem.message, time_check: { result: "unavailable", reason: problem.reason } },
    };
  }
  if (!problem) return payload;
  const quotes = (payload.quotes ?? []).map((row) => (row.status === "LIVE" || row.status === "STALE" ? { ...row, status: "CACHED", reason: problem.reason } : row));
  const conn = payload.connection ?? {};
  const check = conn.time_check ?? {};
  return {
    ...payload,
    quotes,
    status: overallStatus(quotes),
    error: problem,
    connection: {
      ...conn,
      state: "unavailable",
      unavailable_reason: problem.reason,
      message: problem.message,
      time_check: {
        result: check.result === "mismatch" ? "mismatch" : "unavailable",
        reason: problem.reason,
        last_verified_utc: check.last_verified_utc ?? (check.result === "verified" ? check.checked_at_utc : null),
      },
    },
  };
}
const connection = () => quoteView()?.connection ?? services().live_market ?? {};
const quoteFor = (symbol) => quoteView()?.quotes?.find((row) => row.symbol === symbol);
// Before the first answer (success or failure) nothing is known yet: LOADING, not UNAVAILABLE.
const marketStatus = () => quoteView()?.status ?? "LOADING";
const quotesProblemText = () => (quoteView()?.error ? reasonText(quoteView().error.reason) : "");

function collectorSummary() {
  const status = state.data?.status ?? {};
  const value = status.market_monitoring_state ?? "not_configured";
  const [label] = stateLabel(value);
  const monitor = services().market_data?.monitor;
  const detail = {
    running: "Optional background collector is archiving bars to Parquet.",
    paused: "Optional background collector is off. Live data does not need it.",
    not_configured: "No collector configured (integrations.collectors_config). Live data does not need one.",
    error: monitor?.error === "CollectorBusyError"
      ? "Another MT5 collector (python -m johanna.collectors) is already running for this source."
      : `Collector failed (${monitor?.error ?? "error"}). See backend logs.`,
    exited: "Collector stopped without a shutdown request. See backend logs.",
    stopped: "Collector is stopped.",
  }[value] ?? "";
  return { value, label, detail };
}

function renderTopbar() {
  const status = state.data?.status;
  const appOnline = status?.state === "running";
  const executionLabel = status?.autonomous_execution_preference ? "Preference on · inactive" : "Disabled";
  const healthDot = status?.health === "ok" ? "online" : status?.health === "degraded" ? "error" : "";
  const market = marketStatus();
  const server = connection().server?.server;
  const lastQuote = quoteView()?.quotes?.map((row) => row.fetched_at_utc).filter(Boolean).sort().at(-1);
  document.querySelector("#top-status").innerHTML = `
    <div class="status-item app-status"><span class="state-dot ${appOnline ? healthDot : ""}"></span>Application <strong>${appOnline ? (status?.health === "ok" ? "Online" : "Degraded") : "Offline"}</strong></div>
    <div class="status-item market-status" title="${esc(STATUS_HELP[market] ?? "")}">Market data ${statusBadge(market, quotesProblemText())}${server ? `<strong class="server-name">${esc(server)}</strong>` : ""}</div>
    <div class="status-item execution-status">Execution <strong>${esc(executionLabel)}</strong></div>
    <div class="status-item last-update">Last quote <strong>${lastQuote ? esc(clockTime(lastQuote)) : "—"}</strong></div>`;
  document.querySelector("#mode-label").textContent = String(status?.trading_mode ?? "manual").toUpperCase();
  document.querySelector("#sidebar-dot").className = `state-dot ${appOnline ? healthDot : ""}`;
  document.querySelector("#sidebar-state").textContent = !appOnline
    ? "Application offline"
    : status?.health === "ok" ? "Application online" : "Application degraded";
  document.querySelector("#footer-clock").textContent = new Intl.DateTimeFormat(undefined, { timeZone: tz(), dateStyle: "medium", timeStyle: "short" }).format(new Date());
}

function pageHeading(title, subtitle, extra = "") {
  return `<div class="page-heading"><div><div class="eyebrow">${esc(title)}</div><h1>${esc(title)}</h1><div class="page-subtitle">${esc(subtitle)}</div></div>${extra ? `<div class="heading-actions">${extra}</div>` : ""}</div>`;
}

function stat(label, value, isEmpty = false, raw = false) {
  return `<div class="stat"><div class="stat-label">${esc(label)}</div><div class="stat-value ${isEmpty ? "empty" : ""}">${raw ? value : esc(value)}</div></div>`;
}

function emptyState(title, message, className = "") {
  return `<div class="empty-state ${className}"><strong>${esc(title)}</strong>${esc(message)}</div>`;
}

// ---------------------------------------------------------------- live quotes
function quoteStripMarkup() {
  const symbol = prefs().selected_currency_pair;
  const quote = quoteFor(symbol);
  const ok = quote && quote.status !== "UNAVAILABLE";
  return `<div class="stats-strip quote-strip">
    ${stat("Instrument", quote?.broker_symbol && quote.broker_symbol !== symbol ? `${symbol} (${quote.broker_symbol})` : symbol || "—")}
    ${stat("Bid", ok ? price(quote.bid, quote.digits) : "—", !ok)}
    ${stat("Ask", ok ? price(quote.ask, quote.digits) : "—", !ok)}
    ${stat("Spread", ok && quote.spread_pips != null ? `${Number(quote.spread_pips).toFixed(1)} pips` : "—", !ok)}
    ${stat("Last quote", ok ? clockTime(quote.quote_time_utc) : "—", !ok)}
    ${stat("Age", ok ? ageText(quote.quote_time_utc) : "—", !ok)}
    ${stat("Status", statusBadge(quote?.status ?? marketStatus(), quote?.status === "UNAVAILABLE" || quote?.status === "CACHED" ? reasonText(quote.reason) : quotesProblemText()), false, true)}
  </div>`;
}

function watchlistRows(full) {
  const symbols = prefs().currency_pair_watchlist ?? [];
  const selected = prefs().selected_currency_pair;
  if (!symbols.length) return `<tr><td class="table-empty" colspan="${full ? 8 : 6}">No pairs in the watchlist.</td></tr>`;
  return symbols.map((symbol) => {
    const quote = quoteFor(symbol);
    const ok = quote && quote.status !== "UNAVAILABLE";
    const view = quoteView();
    const status = quote?.status ?? (view ? "UNAVAILABLE" : "…");
    const reason = quote ? (quote.status === "LIVE" ? "" : reasonText(quote.reason)) : quotesProblemText();
    return `<tr class="${symbol === selected ? "selected-row" : ""}">
      <td class="watch-symbol" data-select-symbol="${esc(symbol)}" title="${esc(quote?.broker_symbol ? `Broker symbol: ${quote.broker_symbol}` : "")}">${esc(symbol)}</td>
      <td class="num">${ok ? price(quote.bid, quote.digits) : "—"}</td>
      <td class="num">${ok ? price(quote.ask, quote.digits) : "—"}</td>
      <td class="num">${ok && quote.spread_pips != null ? Number(quote.spread_pips).toFixed(1) : "—"}</td>
      ${full ? `<td class="num">${ok ? esc(clockTime(quote.quote_time_utc)) : "—"}</td>` : ""}
      <td class="num">${ok ? esc(ageText(quote.quote_time_utc)) : "—"}</td>
      <td title="${esc(reason)}">${status === "…" ? '<span class="panel-meta">…</span>' : statusBadge(status)}</td>
      ${full ? `<td><button class="remove-pair" data-remove-symbol="${esc(symbol)}" aria-label="Remove ${esc(symbol)}">Remove</button></td>` : ""}
    </tr>`;
  }).join("");
}

function watchlistMarkup(full = false) {
  return `<div class="table-scroll"><table class="watchlist-table live-watchlist"><thead><tr><th>Pair</th><th class="num">Bid</th><th class="num">Ask</th><th class="num">Spread</th>${full ? '<th class="num">Updated</th>' : ""}<th class="num">Age</th><th>Status</th>${full ? "<th></th>" : ""}</tr></thead><tbody id="watchlist-body" data-full="${full ? "1" : "0"}">${watchlistRows(full)}</tbody></table></div>${full ? `<div class="watchlist-add"><input id="watchlist-input" class="text-input" placeholder="Add pair, e.g. EURGBP" aria-label="Currency pair"><button class="button" id="add-pair">Add pair</button><span class="panel-meta">${(prefs().currency_pair_watchlist ?? []).length} / ${MAX_WATCHLIST}</span></div><div id="save-message" class="save-message" aria-live="polite"></div>` : ""}`;
}

// The server-clock check describes the current connection only; history is labelled as such.
function clockCheckText(check) {
  const last = check.last_verified_utc ? ` · last verified ${stamp(check.last_verified_utc)}` : "";
  switch (check.result) {
    case "verified": return `Verified against a live tick (${ageText(check.checked_at_utc)} ago)`;
    case "stale_ticks": return `Not verifiable now: no recent ticks (market closed or quiet)${last}`;
    case "unavailable": return `Not verified now: ${reasonText(check.reason) || "MT5 unavailable"}${last}`;
    case "mismatch": return "MISMATCH: timestamps ahead of UTC";
    default: return "Not checked yet";
  }
}

function marketInfoMarkup() {
  const conn = connection();
  const server = conn.server;
  const check = conn.time_check ?? {};
  const checkText = clockCheckText(check);
  const rows = [
    ["Source", server?.server ? `MT5 · ${server.server}` : "MT5"],
    ["Broker", server?.company ?? "—"],
    ["Account type", server?.account_type ?? "—"],
    ["Connection", conn.state === "connected" ? "Connected" : conn.message || reasonText(conn.unavailable_reason) || stateLabel(conn.state)[0]],
    ["Server clock", `${conn.server_clock ?? "UTC"} · ${checkText}`],
    ["Last MT5 response", conn.last_success_utc ? stamp(conn.last_success_utc) : "—"],
    ["Refresh", "On demand · every 2 s while open"],
    ["Display timezone", tz()],
  ];
  return `<div class="info-list" id="market-info">${rows.map(([label, value]) => `<div class="info-row"><span>${esc(label)}</span><strong class="${label === "Server clock" && check.result === "mismatch" ? "error-state" : ""}">${esc(value)}</strong></div>`).join("")}</div>`;
}

// Targeted updates: an element is rewritten only when its markup actually changed.
function setInner(element, markup) {
  if (element && element.__markup !== markup) {
    element.innerHTML = markup;
    element.__markup = markup;
  }
}
function replaceOuter(selector, markup) {
  const element = document.querySelector(selector);
  if (element && element.__markup !== markup) {
    element.outerHTML = markup;
    const replacement = document.querySelector(selector);
    if (replacement) replacement.__markup = markup;
  }
}

function renderLive() {
  renderTopbar();
  replaceOuter(".quote-strip", quoteStripMarkup());
  const body = document.querySelector("#watchlist-body");
  if (body) setInner(body, watchlistRows(body.dataset.full === "1"));
  replaceOuter("#market-info", marketInfoMarkup());
  setInner(document.querySelector("#market-heading-badge"), statusBadge(marketStatus()));
  renderChartArea(); // the chart's live line and time-base notice follow the quotes
}

// ---------------------------------------------------------------- candlestick chart
function chartMarkup(full = false) {
  const pairOptions = (prefs().currency_pair_watchlist ?? []).map((symbol) => `<option value="${esc(symbol)}" ${symbol === prefs().selected_currency_pair ? "selected" : ""}>${esc(symbol)}</option>`).join("");
  const tfOptions = TIMEFRAMES.map((tf) => `<option value="${tf}" ${tf === prefs().selected_chart_timeframe ? "selected" : ""}>${tf}</option>`).join("");
  const controls = `<select class="select" id="pair-select" aria-label="Selected currency pair" ${pairOptions ? "" : "disabled"}>${pairOptions || "<option>No pair in watchlist</option>"}</select><select class="select" id="timeframe-select" aria-label="Chart timeframe">${tfOptions}</select>`;
  return `<section class="panel chart-panel ${full ? "chart-large" : ""}"><div class="panel-head"><div class="chart-title-main"><span class="chart-symbol">${esc(prefs().selected_currency_pair)}</span><span class="chart-timeframe">${esc(prefs().selected_chart_timeframe)}</span><span id="chart-status">${chartStatusMarkup()}</span></div><div class="chart-tools">${controls}</div></div><div id="chart-notice">${chartNoticeMarkup()}</div><div class="chart-canvas-wrap"><canvas class="chart-canvas" id="price-chart" aria-label="Candlestick chart for ${esc(prefs().selected_currency_pair)}"></canvas><div class="chart-view-controls" id="chart-view-controls"></div><div class="chart-empty" id="chart-empty">${chartEmptyMarkup()}</div></div><div class="chart-hint">Wheel: zoom · Drag: pan · Drag price axis: price scale (double-click resets) · Go Live: back to the latest candles</div><div class="panel-foot" id="chart-foot">${chartFootMarkup()}</div></section>`;
}

const chartKey = () => `${prefs().selected_currency_pair}|${prefs().selected_chart_timeframe}`;

// How well a candle's prices are established, from the backend's fields only:
// "forming"   complete=false: still changing;
// "tick"      complete, and a server tick at/after its close was seen (prices confirmed);
// "clock"     complete only because its time has passed; no tick after the close yet, so a
//             feed stall cannot be ruled out: NOT confirmed price data;
// "unproven"  complete but without close/tick information (archived bars).
const CLOSURE_EVIDENCE = { forming: "forming", tick_observed: "tick", clock_only: "clock", unknown: "unproven" };

function candleEvidence(bar, latestTickUtc) {
  // The backend's per-candle "closure" is authoritative when present (T1); older responses
  // fall back to deriving it from complete / close_t / latest_tick_utc.
  if (bar.closure in CLOSURE_EVIDENCE) return CLOSURE_EVIDENCE[bar.closure];
  if (bar.complete === false) return "forming";
  const close = Date.parse(bar.close_t ?? "");
  const tick = Date.parse(latestTickUtc ?? "");
  if (!Number.isFinite(close) || !Number.isFinite(tick)) return "unproven";
  return tick >= close ? "tick" : "clock";
}

const sameBar = (a, b) => a.o === b.o && a.h === b.h && a.l === b.l && a.c === b.c && a.v === b.v
  && a.complete === b.complete && a.close_t === b.close_t && a.closure === b.closure && a.valid === b.valid;

// A candle can be drawn only if all four prices are numbers (non-finite values arrive as null
// and are reported as invalid, never drawn or invented).
const drawable = (bar) => [bar.o, bar.h, bar.l, bar.c].every((value) => typeof value === "number" && Number.isFinite(value));

// Merge a refreshed bar window into the chart's current one, keyed by open time. MT5's answer
// is authoritative: the forming candle takes its new OHLC, a new candle is appended when the
// timeframe advances, and nothing is invented or interpolated. Unchanged candles keep their
// existing objects, and no open time can appear twice. Candles older than the refreshed
// window (they drop out of the backend's fixed-size window as new ones arrive) are kept, up
// to BAR_HISTORY_LIMIT, so a user inspecting history never loses them.
function mergeBars(previous, incoming) {
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
const currentBarsError = () => (state.barsError?.key === chartKey() ? state.barsError : null);

// What the chart may show right now: bars fetched LIVE become CACHED once a refresh for the
// same pair/timeframe fails or no fresh response arrives in time. The bars stay visible.
function chartData() {
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

function chartStatusMarkup() {
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

function chartNoticeMarkup() {
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

function chartEmptyMarkup() {
  const data = chartData();
  if (data?.bars?.length) return "";
  const error = currentBarsError();
  if (!data && error) return emptyState("Chart unavailable", `${reasonText(error.reason)}: ${error.message}`, "error-state");
  if (!data) return emptyState("Loading chart", "Requesting bars from MT5…");
  return emptyState("No chart data", data.message || reasonText(data.reason) || "MT5 returned no bars.");
}

function chartFootMarkup() {
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

function renderChartArea() {
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

function timeLabel(iso, timeframe) {
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
const CHART_PAD = { left: 6, right: 70, top: 10, bottom: 22 };
const MIN_VISIBLE_CANDLES = 10; // most zoomed in
const MIN_SLOT_PX = 2; // most zoomed out: candle centres at least 2 px apart, so no overlap
const DEFAULT_SLOT_PX = 5;
const PRICE_SCALE_MIN = 0.1; // price range shown, relative to the automatic fit
const PRICE_SCALE_MAX = 10;
const clamp = (value, low, high) => Math.min(high, Math.max(low, value));

const defaultView = (key) => ({ key, follow: true, count: null, endT: null, priceScale: 1 });

// Viewport for the selected pair/timeframe; another dataset always starts at the latest candles.
function chartView() {
  const key = chartKey();
  if (state.view?.key !== key) state.view = defaultView(key);
  return state.view;
}

// Index of the last candle opened at or before `time` (-1 if none). Candles are sorted.
function lastIndexAtOrBefore(bars, time) {
  let low = 0, high = bars.length - 1, found = -1;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (Date.parse(bars[mid].t) <= time) { found = mid; low = mid + 1; } else { high = mid - 1; }
  }
  return found;
}

// Visible candle range [start, end] for `bars` drawn `plotW` px wide.
function resolveViewport(bars, plotW) {
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
function setViewport(bars, start, count) {
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

function goLive() {
  const view = chartView();
  view.follow = true;
  view.endT = null;
  drawChart();
}

// Canvas position and drawing geometry shared by drawing and pointer handling.
function chartGeometry() {
  const canvas = document.querySelector("#price-chart");
  if (!canvas) return null;
  const rect = canvas.getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) return null;
  const plotW = rect.width - CHART_PAD.left - CHART_PAD.right;
  const plotH = rect.height - CHART_PAD.top - CHART_PAD.bottom;
  return { canvas, rect, plotW, plotH, bars: (chartData()?.bars ?? []).filter(drawable) };
}

function viewControlsMarkup(viewport) {
  if (!viewport) return "";
  if (viewport.follow) return '<span class="view-state following" title="The chart follows the newest candle">Following latest</span>';
  return '<span class="view-state history">Viewing history</span><button class="go-live" id="go-live" type="button" title="Return to the newest candles and follow them">Go Live</button>';
}

function drawChart() {
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
  if (!viewport) return;
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
}

// ---------------------------------------------------------------- chart interaction
// Wheel: zoom around the cursor. Drag on the plot: pan (left = older, right = newer).
// Drag on the price axis: price scale (down = wider range); double-click it to reset.
let chartDrag = null;

const onPriceAxis = (geometry, clientX) => clientX - geometry.rect.left > geometry.rect.width - CHART_PAD.right;

// The candle under a screen x position (also used by tests).
function candleAtX(clientX) {
  const geometry = chartGeometry();
  const viewport = state.chartViewport;
  if (!geometry || !viewport) return null;
  const fraction = clamp((clientX - geometry.rect.left - CHART_PAD.left) / geometry.plotW, 0, 0.999999);
  return geometry.bars[viewport.start + Math.floor(fraction * viewport.count)]?.t ?? null;
}

function zoomChart(clientX, zoomIn) {
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

function endChartDrag(event) {
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
function chartViewState() {
  return state.chartViewport ? { ...state.chartViewport } : null;
}

// ---------------------------------------------------------------- market analysis (read-only)
// Presentation only. Every value shown comes from GET /api/market/analysis: the backend's T6
// result (authoritative), its T4 window diagnostics (window_validation, a verbatim projection
// of the T4 object that evaluation used), the snapshot summary and transport metadata.
// Nothing is calculated, inferred or re-classified here, and none of this touches the chart.
const ANALYSIS_PAGES = new Set(["overview", "charts"]);
const ANALYSIS_STATE_LABELS = {
  unsupported: "Unsupported", loading: "Loading", current: "Current", outdated: "Outdated",
  provisional: "Provisional", unavailable: "Unavailable", error: "Error",
};
const ANALYSIS_STATE_CLASS = { current: "good", outdated: "warn", provisional: "warn", unavailable: "bad", error: "bad" };
const OUTCOME_LABELS = { NO_TRADE: "No trade", MARKET_CONDITION: "Market condition only", TRADE_CANDIDATE: "Research candidate" };
const OUTCOME_CLASS = { NO_TRADE: "outcome-none", MARKET_CONDITION: "outcome-condition", TRADE_CANDIDATE: "outcome-candidate" };
const SETUP_STATUS_LABELS = { qualified: "qualified", not_qualified: "not qualified", not_evaluable: "not evaluable" };
const CHECK_RESULT_LABELS = { pass: "Pass", fail: "Fail", unavailable: "Unavailable", not_applicable: "Not applicable" };
const CHECK_LABELS = {
  TC1_regime: "TC1 · Trend regime is trending",
  TC2_bias: "TC2 · Directional bias is aligned",
  TC3_long_term: "TC3 · Long-term alignment with EMA200",
  TC4_pullback: "TC4 · Pullback to EMA20",
  TC5_resumption: "TC5 · Close back beyond EMA20",
  TC6_clearance: "TC6 · Clearance to the opposing level",
};
const VETO_LABELS = {
  V1_ema50_structure_break: "V1 · Close beyond EMA50 in the pullback window",
  V2_swing_context_conflict: "V2 · Complete opposite swing structure",
};
const DIMENSION_LABELS = {
  trend_regime: "Market condition (trend regime)",
  directional_bias: "Directional bias",
  trend_strength: "Trend strength (ADX)",
  volatility: "Volatility (ATR percentile)",
  momentum: "Momentum (RSI)",
  location: "Support / resistance location",
  swing_structure: "Swing structure",
};
const VALIDATION_DIMENSIONS = ["price", "timestamps", "closure", "continuity", "completeness", "snapshot", "session_candles", "recency"];
// Readable labels for known T4 / T2 codes; the actual code is always shown next to the label,
// and an unknown code is shown as-is (never dropped).
const VALIDATION_CODE_LABELS = {
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
const codeLabel = (code) => VALIDATION_CODE_LABELS[code] ?? String(code ?? "unknown");
const humanize = (value) => (value == null ? "—" : String(value).replaceAll("_", " "));

const analysisSupported = () => ANALYSIS_TIMEFRAMES.includes(prefs().selected_chart_timeframe);
// Whether analysis may be requested right now (the poller neither requests nor schedules otherwise).
function analysisActive() {
  return ANALYSIS_PAGES.has(state.page) && analysisSupported() && Boolean(prefs().selected_currency_pair) && !state.error;
}
const analysisInterval = () => (chartData()?.status === "LIVE" ? POLL.analysisMs : POLL.quietAnalysisMs);

// Open time of the newest closed (complete) candle in the chart data for the current selection.
function latestClosedChartT() {
  const data = chartData();
  return data?.bars?.filter((bar) => bar.complete === true).at(-1)?.t ?? null;
}

// Status priority: unsupported, loading, error (no usable response), R1 unavailable, R2
// provisional, then current/outdated for R3-R7. R1 and R2 are never current.
function analysisStatus() {
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

const viewingHistory = () => state.view?.key === chartKey() && state.view?.follow === false;
const analysisSignature = () => {
  const status = analysisStatus();
  return JSON.stringify([status.state, status.reasons, state.analysisReceivedAt, state.analysisKey, viewingHistory()]);
};

// ---- value formatting (presentation only)
function analysisValue(value) {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") return Number.isFinite(value) ? value.toLocaleString("en-US", { maximumFractionDigits: 5, useGrouping: false }) : "—";
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (Array.isArray(value)) return value.length ? value.map(analysisValue).join(", ") : "—";
  if (typeof value === "object") return Object.entries(value).map(([k, v]) => `${humanize(k)} ${analysisValue(v)}`).join("; ") || "—";
  return String(value);
}
const stampSeconds = (value) => {
  const date = new Date(value ?? "");
  if (!value || Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, { timeZone: tz(), dateStyle: "medium", timeStyle: "medium" }).format(date);
};
function evidenceText(item) {
  if (!item) return "—";
  const compared = item.threshold !== null && item.threshold !== undefined ? ` (${item.comparison ? `${item.comparison} ` : ""}${analysisValue(item.threshold)})` : "";
  return `${humanize(item.id)}: ${analysisValue(item.observed)}${compared}`;
}
const kv = (label, value, extraClass = "") => `<div class="analysis-kv ${extraClass}"><span>${esc(label)}</span> <span>${value}</span></div>`;
const analysisList = (items) => (items.length ? `<ul class="analysis-list">${items.map((item) => `<li>${item}</li>`).join("")}</ul>` : "");

function analysisStateBadge(status) {
  return `<span class="badge analysis-state ${ANALYSIS_STATE_CLASS[status.state] ?? ""}" data-analysis-state="${esc(status.state)}">${esc(ANALYSIS_STATE_LABELS[status.state])}</span>`;
}
function outcomeBadge(decision) {
  const outcome = decision?.outcome;
  return `<span class="badge analysis-outcome ${OUTCOME_CLASS[outcome] ?? ""}" data-outcome="${esc(outcome)}">${esc(OUTCOME_LABELS[outcome] ?? outcome ?? "—")} · ${esc(decision?.rule ?? "—")}</span>`;
}
function stateExplanation(status) {
  if (status.state === "unavailable") return "Unavailable: the backend could not produce a valid analysis for this data (rule R1).";
  if (status.state === "provisional") return "Provisional: the data window is provisional, so this is diagnostic only and not a reliable result (rule R2).";
  if (status.state === "outdated") return `Outdated: ${status.reasons.join("; ")}.`;
  return "";
}
// The concise explanation: for a T4 rejection the actual T4 reason, otherwise T6's first reason.
function analysisExplanation(status) {
  const { result, window_validation: wv } = status.envelope;
  const t4 = wv?.available === true ? wv.verdict?.reasons?.[0] : null;
  if (result.decision.rule === "R1" && t4) return `${codeLabel(t4.code)}: ${t4.detail ?? ""}`;
  if (result.decision.rule === "R1" && wv?.available === false) return wv.reason ?? result.decision.reasons?.[0] ?? "";
  return result.decision.reasons?.[0] ?? "";
}
function setupLine(opportunity) {
  if (!opportunity) return "Not evaluated";
  const direction = opportunity.direction ? ` (${opportunity.direction})` : "";
  return `Trend continuation · ${SETUP_STATUS_LABELS[opportunity.status] ?? humanize(opportunity.status)}${direction}`;
}
const candidateNotice = (decision) => (decision?.outcome === "TRADE_CANDIDATE"
  ? `<div class="analysis-notice candidate">Research candidate only: an analytical finding, not an instruction or recommendation to trade. Nothing is executed (actionable: ${analysisValue(decision.actionable)}).</div>`
  : "");

// ---- compact panel (Overview)
function analysisCompactMarkup() {
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
function conditionRows(marketCondition) {
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

function setupMarkup(opportunity, decision) {
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

function validationMarkup(wv) {
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

function timingMarkup(envelope) {
  const { result, snapshot, service } = envelope;
  const age = service?.data_age_at_evaluation_ms;
  return `${kv("Market data retrieved (MT5)", `${esc(stampSeconds(snapshot?.bars_retrieved_at_utc))}${snapshot?.bars_served_from_cache ? " · repeat of a cached MT5 read" : ""}`)}
    ${kv("Analysis covers candles up to", esc(result.as_of_utc ? stampSeconds(result.as_of_utc) : "No analysis window"))}
    ${kv("Evaluated", `${esc(stampSeconds(result.evaluated_at_utc))}${service?.joined_in_flight ? " · shared computation (original evaluation time)" : ""}`)}
    ${kv("Data age at evaluation (backend)", esc(typeof age === "number" && Number.isFinite(age) ? `${(age / 1000).toFixed(3)} s` : "—"))}
    ${kv("Response served", esc(stampSeconds(envelope.served_at_utc)))}
    ${kv("Received by this browser", '<span class="analysis-age">—</span>')}`;
}

function analysisFullMarkup() {
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
function renderAnalysis() {
  const compact = document.querySelector("#analysis-compact-body");
  if (compact) setInner(compact, analysisCompactMarkup());
  const full = document.querySelector("#analysis-full-body");
  if (full) setInner(full, analysisFullMarkup());
  renderAnalysisAge();
}
// The browser-receipt age changes every second; it is kept out of the panel markup so that
// re-rendering never collapses open details.
function renderAnalysisAge() {
  const text = state.analysis && state.analysisKey === chartKey()
    ? `${Math.max(0, Math.round((Date.now() - state.analysisReceivedAt) / 1000))} s ago`
    : "—";
  document.querySelectorAll(".analysis-age").forEach((element) => {
    if (element.textContent !== text) element.textContent = text;
  });
}

const analysisPanelCompact = () => `<section class="panel lower-panel analysis-panel" id="analysis-compact-panel"><div class="panel-head"><span class="panel-title">Analysis &amp; setups</span><span class="panel-meta">BACKEND ANALYSIS · READ-ONLY</span></div><div id="analysis-compact-body">${analysisCompactMarkup()}</div></section>`;
const analysisPanelFull = () => `<section class="panel analysis-panel analysis-full" id="analysis-full-panel"><div class="panel-head"><span class="panel-title">Market analysis</span><span class="panel-meta">BACKEND T6 EVALUATION · READ-ONLY · NOT TRADE INSTRUCTIONS</span></div><div id="analysis-full-body">${analysisFullMarkup()}</div></section>`;

function validAnalysisPayload(payload, symbol, timeframe) {
  return payload && payload.symbol === symbol && payload.timeframe === timeframe
    && typeof payload.result?.decision?.outcome === "string" && typeof payload.result?.decision?.rule === "string"
    && payload.window_validation && typeof payload.window_validation === "object";
}

async function refreshAnalysis(poller) {
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
function syncAnalysisPolling() {
  const poller = livePolling.analysis;
  if (!livePolling.started) return;
  if (!analysisActive()) poller.suspend();
  else if (!poller.inFlight && poller.timer === null) poller.now();
}

// ---------------------------------------------------------------- other panels
function calendarMarkup(limit = 4) {
  const calendar = state.data?.calendar ?? { events: [] };
  if (calendar.error) return emptyState("Calendar unavailable", calendar.error, "error-state");
  const events = (calendar.events ?? []).slice(0, limit);
  if (!events.length) {
    const status = calendarStatus();
    const previous = status.latest_snapshot_week && status.latest_snapshot_week !== status.week_start
      ? ` The most recent saved snapshot is for the week of ${status.latest_snapshot_week}.`
      : "";
    const refresh = status.enabled ? "Calendar refresh is enabled but has not saved this week yet." : "Calendar refresh is disabled.";
    return emptyState("No cached events for this week", `${refresh}${previous}`);
  }
  return events.map((event) => `<div class="event-row"><div class="event-time">${esc(event.scheduled_at_utc ? new Intl.DateTimeFormat(undefined, { timeZone: tz(), hour: "2-digit", minute: "2-digit" }).format(new Date(event.scheduled_at_utc)) : "TBD")}</div><div><div class="event-name"><i class="impact ${esc(String(event.impact || "").toLowerCase())}"></i>${esc(event.event_name)}</div><div class="event-detail">${esc(event.currency || "—")} · ${esc(event.freshness || "cached")} · ${esc(event.source || "")}</div></div></div>`).join("");
}

function newsMarkup(limit = 3) {
  const news = state.data?.news ?? [];
  if (!news.length) return emptyState("News feed not connected", "No news provider is configured. Headlines will appear here when an authorized source is connected.");
  return news.slice(0, limit).map((entry) => {
    const item = Array.isArray(entry.payload) ? entry.payload[0] : entry.payload;
    const title = item?.title || item?.headline;
    if (!title) return "";
    return `<div class="news-row"><div class="event-time">${esc(entry.provider || "SOURCE")}</div><div><div class="event-name">${esc(title)}</div><div class="event-detail">${esc(entry.updated_at_utc || "Cached item")}</div></div></div>`;
  }).join("") || emptyState("No headlines available", "Saved news cache has no displayable headlines.");
}

function healthMarkup() {
  const all = Object.values(services());
  if (!all.length) return emptyState("Service status unavailable", "Backend has not provided service health.");
  const real = all.filter((service) => service.implemented !== false);
  const planned = all.filter((service) => service.implemented === false);
  return `<div class="health-list">${real.map((service) => {
    const stateName = service.state || "unknown";
    const [label] = stateLabel(stateName);
    const title = service.message || service.error || service.monitor?.error || service.refresh?.error || "";
    return `<div class="health-row" title="${esc(title)}"><span>${esc(service.name.replaceAll("_", " "))}</span><span class="health-state ${esc(stateName)}">${esc(label)}</span></div>`;
  }).join("")}</div>${planned.length ? `<div class="settings-note">Planned, not yet implemented: ${esc(planned.map((service) => service.name.replaceAll("_", " ")).join(", "))}.</div>` : ""}`;
}

function overviewPage() {
  return `${pageHeading("Overview", "Live market workspace", `<span id="market-heading-badge">${statusBadge(marketStatus())}</span>`)}
    ${quoteStripMarkup()}
    <div class="overview-grid">
      ${chartMarkup()}
      <section class="panel watchlist-panel"><div class="panel-head"><span class="panel-title">Watchlist</span><span class="panel-meta">${(prefs().currency_pair_watchlist ?? []).length} PAIRS · MT5</span></div>${watchlistMarkup()}</section>
    </div>
    <div class="lower-grid">
      <section class="panel lower-panel"><div class="panel-head"><span class="panel-title">Market information</span><span class="panel-meta">DATA FRESHNESS</span></div>${marketInfoMarkup()}</section>
      <section class="panel lower-panel"><div class="panel-head"><span class="panel-title">Economic calendar</span><button class="text-link" data-page="calendar">View calendar</button></div>${calendarMarkup(4)}</section>
      ${analysisPanelCompact()}
    </div>`;
}

const PAGE_INFO = {
  calendar: ["Economic Calendar", "Upcoming scheduled events", ""],
  news: ["News", "Market headlines and source status", ""],
  signals: ["Trading Signals", "Signal workspace", "Signal generation has not been implemented. No signals are displayed or implied."],
  rules: ["Trading Rules", "Rule configuration", "Rule evaluation has not been implemented. No strategy or trading rules are active."],
  risk: ["Risk Management", "Exposure and risk controls", "Risk calculations are not implemented. This page does not report positions or account exposure."],
  positions: ["Positions", "Open broker positions", "Broker position synchronization is not connected. No position data is available."],
  history: ["Trade History", "Orders and completed trades", "Execution history is not connected. No trades or performance are reported."],
  analytics: ["Analytics", "Performance and market analysis", "Analytics will be available after trading records exist."],
};

function dataPage(id) {
  if (id === "charts") return `${pageHeading("Charts", "Candlesticks fetched from MT5 on demand")}${quoteStripMarkup()}${chartMarkup(true)}${analysisPanelFull()}`;
  if (id === "markets") return `${pageHeading("Markets", "Live quotes from MT5", `<span id="market-heading-badge">${statusBadge(marketStatus())}</span>`)}
    <section class="panel"><div class="panel-head"><span class="panel-title">Currency pairs</span><span class="panel-meta">BID · ASK · SPREAD IN PIPS</span></div>${watchlistMarkup(true)}</section>
    <section class="panel markets-info"><div class="panel-head"><span class="panel-title">Market information</span><span class="panel-meta">CONNECTION &amp; TIME BASE</span></div>${marketInfoMarkup()}</section>`;
  if (id === "calendar") return `${pageHeading("Economic Calendar", "This week · saved provider snapshot")}${calendarStatusPanel()}<section class="panel"><div class="panel-head"><span class="panel-title">Scheduled events</span><span class="panel-meta">${state.data?.calendar?.events?.length ?? 0} EVENTS</span></div>${calendarMarkup(100)}</section>`;
  if (id === "news") return `${pageHeading("News", "Headlines from configured sources")}<section class="panel"><div class="panel-head"><span class="panel-title">Latest headlines</span><span class="panel-meta">PROVIDER CACHE</span></div>${newsMarkup(20)}</section>`;
  return placeholderPage(id);
}

function statusStrip(items) {
  return `<div class="stats-strip status-strip">${items.map(([label, value, empty]) => stat(label, value, empty)).join("")}</div>`;
}

function calendarStatusPanel() {
  const calendar = calendarStatus();
  return statusStrip([
    ["Freshness", freshnessLabel(calendar.freshness)[0], calendar.freshness !== "fresh"],
    ["Refresh", stateLabel(calendar.state)[0], calendar.state !== "running"],
    ["Snapshot retrieved", calendar.snapshot_retrieved_at_utc ? stamp(calendar.snapshot_retrieved_at_utc) : "—", !calendar.snapshot_retrieved_at_utc],
    ["Week starting", calendar.week_start || "—", !calendar.week_start],
    ["Source", calendar.snapshot_source || "—", !calendar.snapshot_source],
  ]);
}

function timezoneOptions(selected) {
  let zones = [];
  try { zones = Intl.supportedValuesOf("timeZone"); } catch { zones = []; }
  if (!zones.includes("UTC")) zones = ["UTC", ...zones];
  if (selected && !zones.includes(selected)) zones = [selected, ...zones];
  return zones.map((zone) => `<option value="${esc(zone)}" ${zone === selected ? "selected" : ""}>${esc(zone)}</option>`).join("");
}

function placeholderPage(id) {
  const [title, subtitle, copy] = PAGE_INFO[id] ?? ["Unavailable", "Page not found", "This workspace page is unavailable."];
  const details = id === "positions" || id === "history"
    ? [["Broker", "Not connected"], ["Execution", "Disabled"], ["Records", "No data"]]
    : id === "signals" || id === "rules" || id === "risk"
      ? [["Service", "Not implemented"], ["Trading mode", esc(prefs().operating_mode || "manual")], ["Execution", "Disabled"]]
      : [["Data source", "Not connected"], ["Last update", "—"], ["Status", "Waiting"]];
  return `${pageHeading(title, subtitle)}<section class="panel placeholder-layout"><div class="placeholder-banner"><div class="placeholder-icon">${icon(NAV_ITEMS.find((item) => item[0] === id)?.[2] || "M4 4h16v16H4z")}</div><div><h2>${esc(title)} workspace</h2><p>${esc(copy)}</p></div></div><div class="placeholder-details">${details.map(([label, value]) => `<div><strong>${esc(label)}</strong>${value}</div>`).join("")}</div></section>`;
}

function settingsPage() {
  const theme = prefs().theme || "dark";
  const mode = prefs().operating_mode || "manual";
  const watchlist = (prefs().currency_pair_watchlist || []).join(", ");
  const status = state.data?.status;
  const collector = collectorSummary();
  return `${pageHeading("Settings", "Workstation preferences and integration status")}
    <div class="settings-grid">
      <section class="panel"><div class="panel-head"><span class="panel-title">Workstation preferences</span><span class="panel-meta">SAVED BY BACKEND</span></div><div class="settings-section">
        <div class="settings-row"><div><div class="settings-name">Appearance</div><div class="settings-description">System follows the operating-system colour scheme.</div></div><select class="select" id="theme-select" aria-label="Theme">${["dark", "light", "system"].map((value) => `<option value="${value}" ${value === theme ? "selected" : ""}>${value[0].toUpperCase()}${value.slice(1)}</option>`).join("")}</select></div>
        <div class="settings-row"><div><div class="settings-name">User timezone</div><div class="settings-description">Times are kept in UTC and displayed in this zone.</div></div><select class="select" id="timezone-select" aria-label="User timezone">${timezoneOptions(prefs().user_timezone)}</select></div>
        <div class="settings-row"><div><div class="settings-name">Selected timeframe</div><div class="settings-description">Applied to the chart.</div></div><select class="select" id="settings-timeframe">${TIMEFRAMES.map((tf) => `<option ${tf === prefs().selected_chart_timeframe ? "selected" : ""}>${tf}</option>`).join("")}</select></div>
        <div class="settings-row"><div><div class="settings-name">Operating mode</div><div class="settings-description">Autonomous analysis is not implemented; order execution stays inactive.</div></div><select class="select" id="mode-select"><option value="manual" ${mode === "manual" ? "selected" : ""}>Manual</option><option value="autonomous" ${mode === "autonomous" ? "selected" : ""}>Autonomous preference</option></select></div>
        <div class="settings-row"><div><div class="settings-name">Autonomous execution</div><div class="settings-description">Preference can be saved. Broker execution is not available.</div></div><button class="toggle ${prefs().autonomous_execution_enabled ? "on" : ""}" id="execution-toggle" role="switch" aria-checked="${Boolean(prefs().autonomous_execution_enabled)}" aria-label="Toggle autonomous execution preference"></button></div>
        <div class="settings-row"><div><div class="settings-name">Background collector (optional) · ${esc(collector.label)}</div><div class="settings-description">${esc(collector.detail)}</div></div><button class="toggle ${status?.market_monitoring_enabled ? "on" : ""}" id="monitor-toggle" role="switch" aria-checked="${Boolean(status?.market_monitoring_enabled)}" aria-label="Toggle background collector"></button></div>
      </div></section>
      <section class="panel"><div class="panel-head"><span class="panel-title">Application services</span><span class="panel-meta">OPERATIONAL HEALTH</span></div>${healthMarkup()}<div class="settings-section"><div class="settings-note">Secrets are read from process environment variables and are not part of workstation preferences. This local backend does not authenticate remote users.</div><div class="settings-row"><div><div class="settings-name">Watchlist</div><div class="settings-description">${esc(watchlist || "Empty")}</div></div><button class="button" data-page="markets">Edit</button></div></div></section>
    </div><div id="save-message" class="save-message" aria-live="polite"></div>`;
}

function render() {
  if (state.loading && !state.data && !state.error) return;
  renderNavigation();
  renderTopbar();
  if (state.error) {
    main.innerHTML = `${pageHeading("Overview", "Workstation status")}<section class="panel">${emptyState("Backend unavailable", state.error, "error-state")}<div class="panel-foot"><button class="button" id="retry">Retry connection</button></div></section>`;
    return;
  }
  if (state.page === "overview") main.innerHTML = overviewPage();
  else if (state.page === "settings") main.innerHTML = settingsPage();
  else main.innerHTML = dataPage(state.page);
  renderAnalysisAge();
  drawChart();
}

// ---------------------------------------------------------------- data loading
async function savePreferences(values) {
  try {
    const response = await fetch("/api/preferences", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.message || payload.error || "Preference save failed");
    await loadDashboard();
    // Restart rather than wait: an answer for the old watchlist/selection must not land.
    if ("currency_pair_watchlist" in values) livePolling.quotes.restart();
    if ("selected_currency_pair" in values || "selected_chart_timeframe" in values) {
      livePolling.bars.restart();
      livePolling.analysis.restart();
      syncAnalysisPolling();
    }
    if ("user_timezone" in values) renderChartArea();
  } catch (error) {
    showSaveMessage(`Could not save: ${error.message}`);
  }
}

function showSaveMessage(text) {
  const target = document.querySelector("#save-message");
  if (target) target.textContent = text;
}

// A failed market request as { reason, message }: the backend's own error when it sent one.
async function requestFailure(response) {
  let payload = null;
  try { payload = await response.json(); } catch { payload = null; }
  return { reason: payload?.error || "refresh_failed", message: payload?.message || `Backend returned ${response.status}` };
}
const networkFailure = () => ({ reason: "backend_unreachable", message: "The Johanna backend did not answer." });

function isEditing() {
  const active = document.activeElement;
  return Boolean(active && main.contains(active) && active.matches("input, select, textarea"));
}

// Everything that, when changed, needs a full page render (live data is updated separately).
const dashboardSignature = (data) => JSON.stringify([data?.preferences, data?.calendar, data?.news, data?.service_configuration]);

async function loadDashboard({ background = false } = {}) {
  state.loading = true;
  const before = dashboardSignature(state.data);
  const hadError = Boolean(state.error);
  let ok = false;
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error(`Backend returned ${response.status}`);
    state.data = await response.json();
    state.error = null;
    ok = true;
  } catch (error) {
    state.error = error.message;
  } finally {
    state.loading = false;
    // A background refresh never re-renders under the user's cursor, and never rebuilds a
    // live page (and its chart) when nothing on it changed; the top bar always updates.
    const unchanged = ok && !hadError && dashboardSignature(state.data) === before && state.page !== "settings";
    if (background && (isEditing() || unchanged)) renderTopbar();
    else render();
  }
  return ok;
}

// ---------------------------------------------------------------- live polling
// One poller per stream. A poll is scheduled only after the previous one has finished, so
// requests never overlap or pile up. Failures back off exponentially (bounded); success
// restores the normal interval. While the tab is hidden nothing is scheduled.
// `active` (optional): while it returns false the poller neither requests nor schedules; call
// now() when it may have become true again.
function createPoller(name, run, interval, active = () => true) {
  const poller = { name, timer: null, delay: null, scheduledAt: 0, running: false, inFlight: false, pending: false, failures: 0, requests: 0, controller: null };
  function schedule(delay) {
    clearTimeout(poller.timer);
    poller.timer = null;
    poller.delay = null;
    if (poller.running && !document.hidden && active()) {
      poller.timer = setTimeout(tick, delay);
      poller.delay = delay;
      poller.scheduledAt = Date.now();
    }
  }
  async function tick() {
    poller.timer = null;
    poller.delay = null;
    if (!poller.running || document.hidden || !active()) return;
    if (poller.inFlight) { poller.pending = true; return; }
    poller.inFlight = true;
    poller.pending = false;
    poller.requests += 1;
    let outcome = false;
    try {
      outcome = await run(poller);
    } catch {
      outcome = false; // a rendering bug must not stop polling
    } finally {
      poller.inFlight = false;
      poller.controller = null;
    }
    if (outcome !== "aborted") poller.failures = outcome ? 0 : poller.failures + 1;
    const base = interval();
    const delay = poller.pending ? 0 : poller.failures ? Math.min(POLL.maxBackoffMs, base * 2 ** poller.failures) : base;
    schedule(delay);
  }
  poller.start = (delay = 0) => {
    if (poller.running) return;
    poller.running = true;
    schedule(delay);
  };
  poller.stop = () => {
    poller.running = false;
    clearTimeout(poller.timer);
    poller.timer = null;
    poller.controller?.abort();
  };
  // Poll as soon as possible (after the current request, if one is running).
  poller.now = () => {
    if (!poller.running || document.hidden || !active()) return;
    if (poller.inFlight) poller.pending = true;
    else schedule(0);
  };
  // Like now(), but abandon a running request first (its answer is for an old selection).
  poller.restart = () => {
    if (poller.inFlight) poller.controller?.abort();
    poller.now();
  };
  poller.suspend = () => {
    clearTimeout(poller.timer);
    poller.timer = null;
    poller.delay = null;
  };
  // If the interval has become shorter (e.g. the chart turned LIVE), bring the pending poll
  // forward to the new interval; never later, never an extra request.
  poller.retime = () => {
    if (poller.timer === null || poller.inFlight || poller.failures) return;
    const wanted = interval();
    if (poller.delay > wanted) schedule(Math.max(0, wanted - (Date.now() - poller.scheduledAt)));
  };
  return poller;
}

// GET a JSON endpoint with a timeout; returns { payload } | { error } | { aborted }.
async function fetchJson(url, poller, timeoutMs) {
  const controller = new AbortController();
  poller.controller = controller;
  let timedOut = false;
  const timer = setTimeout(() => { timedOut = true; controller.abort(); }, timeoutMs);
  try {
    const response = await fetch(url, { cache: "no-store", signal: controller.signal });
    if (!response.ok) return { error: await requestFailure(response) };
    try {
      return { payload: await response.json() };
    } catch {
      if (controller.signal.aborted && !timedOut) return { aborted: true };
      return { error: { reason: "malformed_response", message: "The backend sent an unreadable response." } };
    }
  } catch {
    if (timedOut) return { error: { reason: "request_timeout", message: `No answer within ${timeoutMs / 1000} s.` } };
    if (controller.signal.aborted) return { aborted: true };
    return { error: networkFailure() };
  } finally {
    clearTimeout(timer);
  }
}

const malformed = () => ({ reason: "malformed_response", message: "The backend sent an unexpected response." });
const quotesLive = () => state.quotes?.status === "LIVE" && !state.quotesError;

async function refreshQuotes(poller) {
  const seq = ++state.quotesSeq;
  const result = await fetchJson("/api/market/quotes", poller, POLL.quotesTimeoutMs);
  if (result.aborted || seq !== state.quotesSeq) return "aborted";
  const payload = result.payload;
  const error = result.error ?? (Array.isArray(payload?.quotes) && typeof payload?.status === "string" ? null : malformed());
  const wasLive = quotesLive();
  if (error) {
    state.quotesError = error; // earlier quotes stay, shown CACHED with their age
  } else {
    state.quotes = payload;
    state.quotesReceivedAt = Date.now();
    state.quotesError = null;
  }
  renderLive();
  if (!wasLive && quotesLive()) livePolling.bars.now(); // MT5 is back: refresh the chart too
  return !error;
}

async function refreshBars(poller) {
  const symbol = prefs().selected_currency_pair;
  const timeframe = prefs().selected_chart_timeframe;
  if (!symbol || !timeframe) return true;
  const key = `${symbol}|${timeframe}`;
  const seq = ++state.barsSeq;
  if (state.barsKey !== key && state.bars) { state.bars = null; renderChartArea(); }
  state.barsInFlight = true;
  const query = new URLSearchParams({ symbol, timeframe, count: String(BAR_COUNT) });
  const result = await fetchJson(`/api/market/bars?${query}`, poller, POLL.barsTimeoutMs);
  if (seq === state.barsSeq) state.barsInFlight = false;
  // An answer for an older request or another selection never overwrites the chart.
  if (result.aborted || seq !== state.barsSeq || key !== chartKey()) return "aborted";
  const payload = result.payload;
  const error = result.error
    ?? (Array.isArray(payload?.bars) && payload?.symbol === symbol && payload?.timeframe === timeframe ? null : malformed());
  if (error) {
    state.barsError = { key, ...error }; // earlier bars stay, shown CACHED
  } else {
    const previous = state.barsKey === key ? state.bars?.bars : null;
    state.bars = { ...payload, bars: mergeBars(previous, payload.bars) };
    state.barsKey = key;
    state.barsReceivedAt = Date.now();
    state.barsError = null;
    const closed = state.bars.bars.filter((bar) => bar.complete === true).at(-1)?.t ?? null;
    if (closed && state.analysisCandle?.key === key && state.analysisCandle.t && state.analysisCandle.t !== closed) {
      livePolling.analysis.now(); // a new candle closed: evaluate again without waiting
    }
    if (closed) state.analysisCandle = { key, t: closed };
  }
  renderChartArea();
  renderAnalysis();
  livePolling.analysis.retime(); // a chart that just turned LIVE shortens the analysis interval
  return !error;
}

function quotesInterval() {
  return quotesLive() ? POLL.quotesMs : POLL.quietQuotesMs;
}

function barsInterval() {
  const data = state.barsKey === chartKey() ? state.bars : null;
  if (!data || data.status !== "LIVE" || currentBarsError()) return POLL.quietBarsMs;
  const tick = Date.parse(data.latest_tick_utc ?? "");
  return Number.isFinite(tick) && Date.now() - tick < POLL.quietTickMs ? POLL.barsMs : POLL.quietBarsMs;
}

// Expiry needs no response to happen: re-render the moment stored data stops counting as live.
let expiryShown = { quotes: false, bars: false, analysis: "" };
function checkExpiry() {
  const now = { quotes: quotesExpired(), bars: barsExpired(), analysis: analysisSignature() };
  if (now.quotes !== expiryShown.quotes) renderLive();
  else if (now.bars !== expiryShown.bars) renderChartArea();
  if (now.analysis !== expiryShown.analysis) renderAnalysis();
  else renderAnalysisAge();
  expiryShown = now;
}

const livePolling = {
  quotes: createPoller("quotes", refreshQuotes, quotesInterval),
  bars: createPoller("bars", refreshBars, barsInterval),
  dashboard: createPoller("dashboard", () => loadDashboard({ background: true }), () => POLL.dashboardMs),
  analysis: createPoller("analysis", refreshAnalysis, analysisInterval, analysisActive),
  started: false,
  expiryTimer: null,
  listeners: null,
};
const pollers = () => [livePolling.quotes, livePolling.bars, livePolling.dashboard, livePolling.analysis];

function onVisibilityChange() {
  if (document.hidden) {
    pollers().forEach((poller) => poller.suspend()); // a running request may finish; no new ones
  } else {
    livePolling.quotes.now();
    livePolling.bars.now();
    livePolling.dashboard.now();
    livePolling.analysis.now(); // no-op unless analysis applies to this page and timeframe
    checkExpiry();
  }
}

// Idempotent: calling it again never adds timers or listeners.
function startLivePolling() {
  if (livePolling.started) return;
  livePolling.started = true;
  livePolling.listeners = new AbortController();
  const { signal } = livePolling.listeners;
  document.addEventListener("visibilitychange", onVisibilityChange, { signal });
  window.addEventListener("pagehide", stopLivePolling, { signal });
  livePolling.quotes.start(0);
  livePolling.bars.start(0);
  livePolling.dashboard.start(POLL.dashboardMs);
  livePolling.analysis.start(0);
  livePolling.expiryTimer = setInterval(checkExpiry, 1000);
}

function stopLivePolling() {
  if (!livePolling.started) return;
  livePolling.started = false;
  pollers().forEach((poller) => poller.stop());
  clearInterval(livePolling.expiryTimer);
  livePolling.expiryTimer = null;
  livePolling.listeners?.abort();
  livePolling.listeners = null;
}

// Introspection for diagnostics and tests.
function livePollingState() {
  const timers = pollers().filter((poller) => poller.timer !== null).length + (livePolling.expiryTimer ? 1 : 0);
  const info = (poller) => ({ inFlight: poller.inFlight, requests: poller.requests, failures: poller.failures, scheduled: poller.timer !== null, delay: poller.delay });
  return { started: livePolling.started, timers, quotes: info(livePolling.quotes), bars: info(livePolling.bars), dashboard: info(livePolling.dashboard), analysis: info(livePolling.analysis) };
}

// Kept for callers that ask for an immediate refresh.
const loadQuotes = () => livePolling.quotes.now();
const loadBars = () => livePolling.bars.now();

// ---------------------------------------------------------------- events
document.addEventListener("click", async (event) => {
  const nav = event.target.closest("[data-page]");
  if (nav) {
    document.querySelector("#sidebar").classList.remove("open");
    document.querySelector("#mobile-scrim").classList.remove("visible");
    if (window.location.hash !== `#${nav.dataset.page}`) window.location.hash = nav.dataset.page;
    else showPage(nav.dataset.page);
    return;
  }
  const selectSymbol = event.target.closest("[data-select-symbol]");
  if (selectSymbol) return savePreferences({ selected_currency_pair: selectSymbol.dataset.selectSymbol });
  if (event.target.closest("#go-live")) return goLive();
  if (event.target.closest("#retry")) return loadDashboard();
  if (event.target.closest("#monitor-toggle")) return savePreferences({ market_monitoring_enabled: !prefs().market_monitoring_enabled });
  if (event.target.closest("#execution-toggle")) return savePreferences({ autonomous_execution_enabled: !prefs().autonomous_execution_enabled });
  if (event.target.closest("#add-pair")) {
    const input = document.querySelector("#watchlist-input");
    const pair = input?.value.trim().toUpperCase();
    if (!pair) return;
    const current = prefs().currency_pair_watchlist ?? [];
    if (current.includes(pair)) input.value = "";
    else if (current.length >= MAX_WATCHLIST) showSaveMessage(`Could not add ${pair}: the watchlist is limited to ${MAX_WATCHLIST} pairs. Remove one first.`);
    else await savePreferences({ currency_pair_watchlist: [...current, pair] });
  }
  const removePair = event.target.closest("[data-remove-symbol]");
  if (removePair) {
    const current = prefs().currency_pair_watchlist ?? [];
    const watchlist = current.filter((symbol) => symbol !== removePair.dataset.removeSymbol);
    const update = { currency_pair_watchlist: watchlist };
    if (prefs().selected_currency_pair === removePair.dataset.removeSymbol && watchlist.length) {
      update.selected_currency_pair = watchlist[0];
    }
    await savePreferences(update);
  }
  if (event.target.closest("#mobile-menu")) {
    const open = document.querySelector("#sidebar").classList.toggle("open");
    document.querySelector("#mobile-menu").setAttribute("aria-expanded", String(open));
    document.querySelector("#mobile-scrim").classList.toggle("visible", open);
  }
  if (event.target.closest("#mobile-scrim")) {
    document.querySelector("#sidebar").classList.remove("open");
    document.querySelector("#mobile-scrim").classList.remove("visible");
  }
});

document.addEventListener("change", (event) => {
  if (event.target.id === "pair-select") savePreferences({ selected_currency_pair: event.target.value });
  if (event.target.id === "timeframe-select" || event.target.id === "settings-timeframe") savePreferences({ selected_chart_timeframe: event.target.value });
  if (event.target.id === "mode-select") savePreferences({ operating_mode: event.target.value });
  if (event.target.id === "theme-select") savePreferences({ theme: event.target.value });
  if (event.target.id === "timezone-select") savePreferences({ user_timezone: event.target.value });
});

function showPage(page) {
  state.page = page;
  render();
  syncAnalysisPolling();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
function applyTheme() {
  const theme = prefs().theme;
  const dark = theme === "system" ? systemDark.matches : theme !== "light";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}

const renderOriginal = render;
render = function themedRender() { applyTheme(); renderOriginal(); };
systemDark.addEventListener("change", () => { if (prefs().theme === "system") render(); });
window.addEventListener("hashchange", () => showPage(pageFromHash()));
window.addEventListener("resize", () => drawChart());
// Live data is requested only while this page is open and visible (see startLivePolling).
window.addEventListener("pageshow", (event) => { if (event.persisted) startLivePolling(); });
loadDashboard().then(startLivePolling);
