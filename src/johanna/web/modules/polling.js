import { BAR_COUNT, POLL, state } from "./state.js";
import { prefs } from "./format.js";
import { barsExpired, quotesExpired } from "./status.js";
import { fetchJson, malformed } from "./api.js";
import { renderLive } from "./quotes.js";
import { chartKey, currentBarsError, mergeBars, renderChartArea } from "./chart.js";
import { analysisActive, analysisInterval, analysisSignature, refreshAnalysis, renderAnalysis, renderAnalysisAge } from "./analysis.js";
import { accountActive, accountExpired, accountInterval, refreshAccount, renderAccount } from "./account.js";
import { refreshRisk, renderRisk, riskActive, riskExpired, riskInterval } from "./risk.js";
import { decisionsActive, decisionsExpired, decisionsInterval, refreshDecisions, renderDecisions } from "./decisions.js";
import { loadDashboard } from "./data.js";

// ---------------------------------------------------------------- live polling
// One poller per stream. A poll is scheduled only after the previous one has finished, so
// requests never overlap or pile up. Failures back off exponentially (bounded); success
// restores the normal interval. While the tab is hidden nothing is scheduled.
// `active` (optional): while it returns false the poller neither requests nor schedules; call
// now() when it may have become true again.
export function createPoller(name, run, interval, active = () => true) {
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

export const quotesLive = () => state.quotes?.status === "LIVE" && !state.quotesError;

export async function refreshQuotes(poller) {
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

export async function refreshBars(poller) {
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

export function quotesInterval() {
  return quotesLive() ? POLL.quotesMs : POLL.quietQuotesMs;
}

export function barsInterval() {
  const data = state.barsKey === chartKey() ? state.bars : null;
  if (!data || data.status !== "LIVE" || currentBarsError()) return POLL.quietBarsMs;
  const tick = Date.parse(data.latest_tick_utc ?? "");
  return Number.isFinite(tick) && Date.now() - tick < POLL.quietTickMs ? POLL.barsMs : POLL.quietBarsMs;
}

// Expiry needs no response to happen: re-render the moment stored data stops counting as live.
export let expiryShown = { quotes: false, bars: false, analysis: "", account: false, risk: false, decisions: false };
export function checkExpiry() {
  const now = { quotes: quotesExpired(), bars: barsExpired(), analysis: analysisSignature(), account: accountExpired(), risk: riskExpired(), decisions: decisionsExpired() };
  if (now.account !== expiryShown.account) renderAccount();
  if (now.decisions !== expiryShown.decisions) renderDecisions();
  if (now.risk !== expiryShown.risk) renderRisk();
  if (now.quotes !== expiryShown.quotes) renderLive();
  else if (now.bars !== expiryShown.bars) renderChartArea();
  if (now.analysis !== expiryShown.analysis) renderAnalysis();
  else renderAnalysisAge();
  expiryShown = now;
}

export const livePolling = {
  quotes: createPoller("quotes", refreshQuotes, quotesInterval),
  bars: createPoller("bars", refreshBars, barsInterval),
  dashboard: createPoller("dashboard", () => loadDashboard({ background: true }), () => POLL.dashboardMs),
  analysis: createPoller("analysis", refreshAnalysis, analysisInterval, analysisActive),
  account: createPoller("account", refreshAccount, accountInterval, accountActive),
  risk: createPoller("risk", refreshRisk, riskInterval, riskActive),
  decisions: createPoller("decisions", refreshDecisions, decisionsInterval, decisionsActive),
  started: false,
  expiryTimer: null,
  listeners: null,
};
export const pollers = () => [livePolling.quotes, livePolling.bars, livePolling.dashboard, livePolling.analysis, livePolling.account, livePolling.risk, livePolling.decisions];

export function onVisibilityChange() {
  if (document.hidden) {
    pollers().forEach((poller) => poller.suspend()); // a running request may finish; no new ones
  } else {
    livePolling.quotes.now();
    livePolling.bars.now();
    livePolling.dashboard.now();
    livePolling.analysis.now(); // no-op unless analysis applies to this page and timeframe
    livePolling.account.now(); // no-op unless the Account & Broker page is open
    livePolling.risk.now(); // no-op unless the Risk Management page is open
    livePolling.decisions.now(); // no-op unless the Decisions page is open
    checkExpiry();
  }
}

// Idempotent: calling it again never adds timers or listeners.
export function startLivePolling() {
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
  livePolling.account.start(0);
  livePolling.risk.start(0);
  livePolling.decisions.start(0);
  livePolling.expiryTimer = setInterval(checkExpiry, 1000);
}

export function stopLivePolling() {
  if (!livePolling.started) return;
  livePolling.started = false;
  pollers().forEach((poller) => poller.stop());
  clearInterval(livePolling.expiryTimer);
  livePolling.expiryTimer = null;
  livePolling.listeners?.abort();
  livePolling.listeners = null;
}

// Introspection for diagnostics and tests.
export function livePollingState() {
  const timers = pollers().filter((poller) => poller.timer !== null).length + (livePolling.expiryTimer ? 1 : 0);
  const info = (poller) => ({ inFlight: poller.inFlight, requests: poller.requests, failures: poller.failures, scheduled: poller.timer !== null, delay: poller.delay });
  return { started: livePolling.started, timers, quotes: info(livePolling.quotes), bars: info(livePolling.bars), dashboard: info(livePolling.dashboard), analysis: info(livePolling.analysis), account: info(livePolling.account), risk: info(livePolling.risk), decisions: info(livePolling.decisions) };
}

// Kept for callers that ask for an immediate refresh.
export const loadQuotes = () => livePolling.quotes.now();
export const loadBars = () => livePolling.bars.now();
