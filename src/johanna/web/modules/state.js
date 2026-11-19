// B7.2 information architecture: eight primary sections; the existing pages keep their ids
// (saved links keep working) and appear as items of their section.
const ICONS = {
  overview: "M3 3h7v7H3zM14 3h7v5h-7zM14 11h7v10h-7zM3 13h7v8H3z",
  market: "M3 3v18h18M7 15l4-4 4 3 6-8",
  decisions: "M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11Zm-3-11 2 2 4-4",
  journal: "M3 12a9 9 0 1 0 3-6.7L3 8m0-5v5h5m4-1v5l3 2",
  positions: "M4 7h16M4 12h10M4 17h7",
  evidence: "M9 12h6M9 16h6M7 3h7l5 5v13H7zM14 3v5h5",
  control: "M4 6h16M4 12h16M4 18h16M8 4v4M16 10v4M11 16v4",
  settings: "M12 8.5a3.5 3.5 0 1 0 0 7 3.5 3.5 0 0 0 0-7ZM19 13l2-1-2-1-.5-2 1-2-2-1-2 1-2-.5-1-2-1 2-2 .5-2-1-1 2 1 2-.5 2-2 1 2 1 .5 2-1 2 2 1 2-1 2 .5 1 2 1-2 2-.5 2 1 1-2-1-2z",
};
export const NAV_SECTIONS = [
  { id: "overview", label: "Overview", icon: ICONS.overview, pages: [["overview", "Overview"]] },
  { id: "market", label: "Market Analysis", icon: ICONS.market, pages: [["charts", "Charts"], ["markets", "Watchlist"], ["calendar", "Economic Calendar"]] },
  { id: "decisions", label: "Decisions & Risk", icon: ICONS.decisions, pages: [["risk", "Current proposal"], ["signals", "Decision history"], ["analytics", "Shadow research"]] },
  { id: "journal", label: "Trade Journal", icon: ICONS.journal, pages: [["history", "Trade Journal"]] },
  { id: "positions", label: "Positions & Reconciliation", icon: ICONS.positions, pages: [["positions", "Reconciliation"], ["account", "Account & Broker"]] },
  { id: "evidence", label: "Evidence & Costs", icon: ICONS.evidence, pages: [["evidence", "Evidence & Costs"]] },
  { id: "control", label: "System Control Centre", icon: ICONS.control, pages: [["control", "System Control Centre"]] },
  { id: "settings", label: "Settings", icon: ICONS.settings, pages: [["settings", "Settings"]], secondary: true },
];
// [page id, label, icon path, section id] for every page.
export const NAV_ITEMS = NAV_SECTIONS.flatMap((section) => section.pages.map(([id, label]) => [id, label, section.icon, section.id]));
// Former or section-level ids. Trading Rules and News are not implemented in the backend, so they
// left the navigation; their saved links land on the nearest real screen.
export const ROUTE_ALIASES = { rules: "control", news: "calendar", market: "charts", decisions: "risk", journal: "history" };
export const sectionOf = (page) => NAV_SECTIONS.find((section) => section.pages.some(([id]) => id === page)) ?? NAV_SECTIONS[0];
export const TIMEFRAMES = ["M1", "M5", "M15", "M30", "H1", "H4", "D1"];
export const BAR_COUNT = 300;
// Candles kept per chart: the newest fetched window plus older ones it has moved past.
export const BAR_HISTORY_LIMIT = 1000;
// Live polling targets (not guarantees). Each poll is scheduled only after the previous one
// has finished, so requests never overlap; failures back off; a hidden tab suspends polling.
export const POLL = {
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
  // Account & Broker page (/api/account/*): only while that page is open.
  accountMs: 10000,
  quietAccountMs: 30000, // not LIVE (MT5 unavailable or answering from cache)
  accountTimeoutMs: 20000,
  // Risk Management page (/api/risk/proposal): a new T6 + risk evaluation per request.
  riskMs: 30000,
  riskTimeoutMs: 45000,
  // Decisions page (/api/journal/decisions): reads the journal only; no MT5 access.
  decisionsMs: 30000,
  decisionsTimeoutMs: 20000,
};
// A stored response stops counting as live when no fresh one has arrived for this long,
// whatever the reason (request failed, hung, backend gone, tab was hidden).
export const QUOTES_EXPIRE_MS = 10000;
export const BARS_EXPIRE_MS = 65000;
export const MAX_WATCHLIST = 200; // same limit as the backend (persistence.MAX_WATCHLIST)
// Presentation policy only: after this long without a new evaluation, a displayed analysis
// stops being called current. It never relaxes the backend's 5 s data-freshness rule (T2).
export const ANALYSIS_EXPIRE_MS = 65000;
export const ANALYSIS_TIMEFRAMES = ["H1", "H4", "D1"]; // the backend's T6 scope; others are never requested

export const PAGE_IDS = new Set(NAV_ITEMS.map(([id]) => id));
export const pageFromHash = () => {
  const raw = window.location.hash.replace(/^#/, "");
  const id = ROUTE_ALIASES[raw] ?? raw;
  return PAGE_IDS.has(id) ? id : "overview";
};
export const state = {
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
  drawChartCalls: 0, // drawChart() invocations, repaint or not (diagnostic)
  view: null, // chart viewport (zoom, pan, price scale) for one pair/timeframe; see chartView()
  chartViewport: null, // the candle range last drawn
  // Analysis Overlay (Charts page only): transient, in memory, off by default, never saved.
  overlay: { enabled: false, draws: 0, last: null, menuOpen: false, annotations: null, modelCache: null, modelRuns: 0, modelCalls: 0 },
  // Account & Broker page: last successful /api/account/* answers (never shown as live once a
  // refresh fails or they expire; see accountView()).
  account: { summary: null, instruments: null, receivedAt: 0, error: null, seq: 0 },
  // Risk Management page: last successful /api/risk/proposal answer and its selection key.
  risk: { payload: null, key: null, receivedAt: 0, error: null, seq: 0 },
  // Decisions page: last successful /api/journal/decisions answer.
  decisions: { payload: null, receivedAt: 0, error: null, seq: 0 },
  // Trade History (P5.2): manual records of trades executed in MT5. Loaded on demand (no poller).
  // Analytics (P6.1): shadow evaluation status and diagnostics. Loaded on demand (no poller).
  shadow: { status: null, diagnostics: null, cycles: null, error: null, message: "", busy: false, seq: 0,
    // P6.2a shadow trade intents (HYPOTHETICAL -- NOT AN ORDER) and the opportunity funnel
    intents: null, funnel: null, intentError: null, intentFilters: { symbol: "", timeframe: "", eligibility: "", from: "", to: "" },
    selectedIntent: null, intentDetail: null, intentSeq: 0,
    // P6.R operations readiness (diagnostic; enables nothing)
    readiness: null },
  // B7.2 operations snapshot (System Control Centre, Overview, Positions, Evidence): the last
  // successful /api/operations/readiness answer. Loaded when one of those pages opens or on
  // Refresh -- never polled. Read-only; nothing here enables anything.
  ops: { readiness: null, error: null, receivedAt: 0, seq: 0 },
  // B7.2 Overview decision summary (journal read only, no MT5): loaded when Overview opens.
  overview: { decisions: null, decisionsError: null, receivedAt: 0, seq: 0 },
  // Browser receipt time of the last successful /api/dashboard answer (freshness display).
  dataReceivedAt: 0,
  trades: { list: null, error: null, selected: null, detail: null, detailError: null, message: "", busy: false, seq: 0, pending: {}, link: null, linkTrades: null },
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
