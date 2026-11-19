import { NAV_SECTIONS, state } from "./state.js";
import { STATUS_HELP, clockTime, esc, icon, statusBadge, tz } from "./format.js";
import { connection, marketStatus, quoteView, quotesProblemText } from "./status.js";
import { syncAnalysisPolling } from "./analysis.js";
import { accountActive } from "./account.js";
import { riskActive } from "./risk.js";
import { decisionsActive } from "./decisions.js";
import { loadTrades } from "./trades.js";
import { loadShadow } from "./shadow.js";
import { OPS_PAGES, loadOps } from "./control.js";
import { loadOverview } from "./overview.js";
import { render } from "./pages.js";
import { livePolling } from "./polling.js";

// B7.2 grouped navigation: eight sections; a section with several pages lists them as items.
// Every page keeps its own [data-page] id, so saved links and existing handlers keep working.
export function renderNavigation() {
  const link = (id, label, cls, iconPath) => `<button class="nav-link ${cls} ${state.page === id ? "active" : ""}" data-page="${id}" ${state.page === id ? 'aria-current="page"' : ""}>${iconPath ? icon(iconPath) : ""}<span>${label}</span></button>`;
  document.querySelector("#primary-nav").innerHTML = NAV_SECTIONS.map((section) => {
    const current = section.pages.some(([id]) => id === state.page);
    if (section.pages.length === 1) {
      const [id] = section.pages[0];
      return `<div class="nav-section ${section.secondary ? "secondary" : ""} ${current ? "current" : ""}" data-nav-section="${section.id}">${link(id, section.label, "nav-section-link", section.icon)}</div>`;
    }
    return `<div class="nav-section ${current ? "current" : ""}" data-nav-section="${section.id}" role="group" aria-label="${section.label}">
      <div class="nav-section-label">${icon(section.icon)}<span>${section.label}</span></div>
      ${section.pages.map(([id, label]) => link(id, label, "nav-sub-link")).join("")}</div>`;
  }).join("");
}

export function renderTopbar() {
  const status = state.data?.status;
  const appOnline = status?.state === "running";
  // B7.2: execution is shown as the backend reports it (never inferred from a saved preference).
  const executionLabel = status?.autonomous_execution_active === true ? "ACTIVE" : "Disabled";
  const executionNote = status?.autonomous_execution_preference
    ? "A previously saved preference exists; autonomous execution is not available and stays disabled."
    : "Johanna does not execute trades.";
  const healthDot = status?.health === "ok" ? "online" : status?.health === "degraded" ? "error" : "";
  const market = marketStatus();
  const server = connection().server?.server;
  const lastQuote = quoteView()?.quotes?.map((row) => row.fetched_at_utc).filter(Boolean).sort().at(-1);
  document.querySelector("#top-status").innerHTML = `
    <div class="status-item app-status"><span class="state-dot ${appOnline ? healthDot : ""}"></span>Application <strong>${appOnline ? (status?.health === "ok" ? "Online" : "Degraded") : "Offline"}</strong></div>
    <div class="status-item market-status" title="${esc(STATUS_HELP[market] ?? "")}">Market data ${statusBadge(market, quotesProblemText())}${server ? `<strong class="server-name">${esc(server)}</strong>` : ""}</div>
    <div class="status-item execution-status" title="${esc(executionNote)}">Execution <strong>${esc(executionLabel)}</strong></div>
    <div class="status-item last-update">Last quote <strong>${lastQuote ? esc(clockTime(lastQuote)) : "—"}</strong></div>`;
  // Effective mode is manual: autonomous operation is not implemented (a saved preference has no effect).
  const modeLabel = document.querySelector("#mode-label");
  modeLabel.textContent = "MANUAL";
  modeLabel.title = status?.trading_mode && status.trading_mode !== "manual" ? `Saved preference "${status.trading_mode}" has no effect: autonomous operation is not available.` : "Manual operation";
  document.querySelector("#sidebar-dot").className = `state-dot ${appOnline ? healthDot : ""}`;
  document.querySelector("#sidebar-state").textContent = !appOnline
    ? "Application offline"
    : status?.health === "ok" ? "Application online" : "Application degraded";
  document.querySelector("#footer-clock").textContent = new Intl.DateTimeFormat(undefined, { timeZone: tz(), dateStyle: "medium", timeStyle: "short" }).format(new Date());
}

export function showPage(page) {
  state.page = page;
  render();
  syncAnalysisPolling();
  // Account polling exists only while the Account & Broker page is open.
  if (accountActive()) livePolling.account.now();
  else livePolling.account.suspend();
  if (riskActive()) livePolling.risk.now();
  else livePolling.risk.suspend();
  if (decisionsActive()) livePolling.decisions.now();
  else livePolling.decisions.suspend();
  loadPageData(page);
  window.scrollTo({ top: 0, behavior: "smooth" });
}

// On-demand loads for pages without a poller (one request set per page open; never periodic).
export function loadPageData(page) {
  if (page === "history") loadTrades();
  if (page === "analytics") loadShadow();
  if (OPS_PAGES.has(page)) loadOps();
  if (page === "overview") loadOverview();
}
