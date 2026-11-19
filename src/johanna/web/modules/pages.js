import { hooks } from "./hooks.js";
import { NAV_ITEMS, state } from "./state.js";
import { emptyState, esc, icon, main, pageHeading, prefs, statusBadge } from "./format.js";
import { marketStatus } from "./status.js";
import { marketInfoMarkup, quoteStripMarkup, watchlistMarkup } from "./quotes.js";
import { chartMarkup, drawChart } from "./chart.js";
import { analysisPanelFull, renderAnalysisAge } from "./analysis.js";
import { calendarMarkup, calendarStatusPanel, newsMarkup } from "./panels.js";
import { accountPage } from "./account.js";
import { riskPage } from "./risk.js";
import { decisionsPage } from "./decisions.js";
import { tradesPage } from "./trades.js";
import { shadowPage } from "./shadow.js";
import { settingsPage } from "./settings.js";
import { controlPage } from "./control.js";
import { positionsPage } from "./positions.js";
import { evidencePage } from "./evidence.js";
import { overviewPage } from "./overview.js";

// Placeholder copy for pages without a dedicated screen. Trading Rules and News left the
// navigation in B7.2 (not implemented in the backend); their saved links are redirected
// (state.ROUTE_ALIASES), and the copy is kept for completeness.
export const PAGE_INFO = {
  calendar: ["Economic Calendar", "Upcoming scheduled events", ""],
  news: ["News", "Market headlines and source status", "The news service is not implemented."],
  rules: ["Trading Rules", "Rule configuration", "Rule evaluation has not been implemented. No strategy or trading rules are active."],
  history: ["Trade Journal", "Manual records of trades executed in MT5", "Record trades you have already executed in MT5. Johanna does not place, modify or close orders."],
  analytics: ["Shadow Research", "Shadow evaluation and signal-frequency diagnostics", "Background shadow evaluation: read-only, no orders."],
};

export function dataPage(id) {
  if (id === "signals") return decisionsPage();
  if (id === "history") return tradesPage();
  if (id === "analytics") return shadowPage();
  if (id === "account") return accountPage();
  if (id === "risk") return riskPage();
  if (id === "control") return controlPage();
  if (id === "positions") return positionsPage();
  if (id === "evidence") return evidencePage();
  if (id === "charts") return `${pageHeading("Charts", "Candlesticks fetched from MT5 on demand")}${quoteStripMarkup()}${chartMarkup(true)}${analysisPanelFull()}`;
  if (id === "markets") return `${pageHeading("Watchlist", "Live quotes from MT5", `<span id="market-heading-badge">${statusBadge(marketStatus())}</span>`)}
    <section class="panel"><div class="panel-head"><span class="panel-title">Currency pairs</span><span class="panel-meta">BID · ASK · SPREAD IN PIPS</span></div>${watchlistMarkup(true)}</section>
    <section class="panel markets-info"><div class="panel-head"><span class="panel-title">Market information</span><span class="panel-meta">CONNECTION &amp; TIME BASE</span></div>${marketInfoMarkup()}</section>`;
  if (id === "calendar") return `${pageHeading("Economic Calendar", "This week · saved provider snapshot")}${calendarStatusPanel()}<section class="panel"><div class="panel-head"><span class="panel-title">Scheduled events</span><span class="panel-meta">${state.data?.calendar?.events?.length ?? 0} EVENTS</span></div>${calendarMarkup(100)}</section>`;
  if (id === "news") return `${pageHeading("News", "Headlines from configured sources")}<section class="panel"><div class="panel-head"><span class="panel-title">Latest headlines</span><span class="panel-meta">PROVIDER CACHE</span></div>${newsMarkup(20)}</section>`;
  return placeholderPage(id);
}

export function placeholderPage(id) {
  const [title, subtitle, copy] = PAGE_INFO[id] ?? ["Unavailable", "Page not found", "This workspace page is unavailable."];
  const details = id === "positions" || id === "history"
    ? [["Broker", "Not connected"], ["Execution", "Disabled"], ["Records", "No data"]]
    : id === "signals" || id === "rules" || id === "risk"
      ? [["Service", "Not implemented"], ["Trading mode", esc(prefs().operating_mode || "manual")], ["Execution", "Disabled"]]
      : [["Data source", "Not connected"], ["Last update", "—"], ["Status", "Waiting"]];
  return `${pageHeading(title, subtitle)}<section class="panel placeholder-layout"><div class="placeholder-banner"><div class="placeholder-icon">${icon(NAV_ITEMS.find((item) => item[0] === id)?.[2] || "M4 4h16v16H4z")}</div><div><h2>${esc(title)} workspace</h2><p>${esc(copy)}</p></div></div><div class="placeholder-details">${details.map(([label, value]) => `<div><strong>${esc(label)}</strong>${value}</div>`).join("")}</div></section>`;
}

export function render() {
  applyTheme(); // first, as the former themed wrapper did
  if (state.loading && !state.data && !state.error) return;
  hooks.renderNavigation();
  hooks.renderTopbar();
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

export const systemDark = window.matchMedia("(prefers-color-scheme: dark)");
export function applyTheme() {
  const theme = prefs().theme;
  const dark = theme === "system" ? systemDark.matches : theme !== "light";
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}
