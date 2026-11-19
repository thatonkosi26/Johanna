import { hooks } from "./modules/hooks.js";
import { ANALYSIS_EXPIRE_MS, MAX_WATCHLIST, POLL, ROUTE_ALIASES, pageFromHash, state } from "./modules/state.js";
import { prefs, price, stampSeconds } from "./modules/format.js";
import { CHART_PAD, candleAtX, candleEvidence, chartData, chartGeometry, chartKey, chartViewState, drawChart, goLive, mergeBars, renderChartArea } from "./modules/chart.js";
import { analysisInterval, analysisStatus, refreshAnalysis } from "./modules/analysis.js";
import { ANNOTATION_KEYS, annotationMenuMarkup, drawOverlay, overlayModel, overlayState, overlayToggleMarkup, setAnnotationMenu, setAnnotations, setOverlay } from "./modules/overlay.js";
import { confirmDecisionLink, decisionLinkMarkup, loadTrades, lots, openDecisionLink, renderDecisionLink, renderTrades, tradeAction } from "./modules/trades.js";
import { loadShadow, loadShadowIntents, selectIntent } from "./modules/shadow.js";
import { loadOps } from "./modules/control.js";
import { loadOverview } from "./modules/overview.js";
import { applyTheme, render, systemDark } from "./modules/pages.js";
import { loadDashboard, savePreferences, showSaveMessage } from "./modules/data.js";
import { checkExpiry, livePolling, livePollingState, loadBars, loadQuotes, startLivePolling, stopLivePolling } from "./modules/polling.js";
import { loadPageData, renderNavigation, renderTopbar, showPage } from "./modules/navigation.js";

// Late-bound references keeping the module graph acyclic (modules/hooks.js).
Object.assign(hooks, { annotationMenuMarkup, decisionLinkMarkup, drawOverlay, livePolling, overlayToggleMarkup, renderChartArea, renderNavigation, renderTopbar });
// Diagnostics used by the browser tests (formerly page globals); read-only use.
Object.assign(window, { ANALYSIS_EXPIRE_MS, analysisStatus, loadQuotes, POLL, state, prefs, price, chartKey, candleEvidence, mergeBars, chartData, renderChartArea, CHART_PAD, chartGeometry, drawChart, candleAtX, chartViewState, overlayModel, overlayState, analysisInterval, stampSeconds, refreshAnalysis, lots, savePreferences, loadDashboard, checkExpiry, livePolling, startLivePolling, stopLivePolling, livePollingState, loadBars });

// ---------------------------------------------------------------- events
document.addEventListener("click", async (event) => {
  // Annotations dropdown: toggle, select/clear all, close on any click outside it.
  if (event.target.closest("[data-annotation-menu]")) return setAnnotationMenu(!state.overlay.menuOpen);
  const all = event.target.closest("[data-annotation-all]");
  if (all) return setAnnotations(Object.fromEntries(ANNOTATION_KEYS.map((key) => [key, all.dataset.annotationAll === "1"])));
  if (state.overlay.menuOpen && !event.target.closest(".annotation-menu")) setAnnotationMenu(false);
  const overlayMode = event.target.closest("[data-overlay-mode]");
  if (overlayMode) return setOverlay(overlayMode.dataset.overlayMode === "analysis");
  const nav = event.target.closest("[data-page]");
  if (nav) {
    document.querySelector("#sidebar").classList.remove("open");
    document.querySelector("#mobile-scrim").classList.remove("visible");
    if (window.location.hash !== `#${nav.dataset.page}`) window.location.hash = nav.dataset.page;
    else showPage(nav.dataset.page);
    return;
  }
  // B7.2: the shadow scheduler is read-only in this application (no POST is ever sent).
  if (event.target.closest("[data-ops-refresh]")) {
    loadOps();
    if (state.page === "overview") loadOverview();
    return;
  }
  if (event.target.closest("#shadow-refresh")) { state.shadow.message = ""; return loadShadow(); }
  const intentRow = event.target.closest("[data-intent]");
  if (intentRow) return selectIntent(intentRow.dataset.intent);
  const tradeButton = event.target.closest("[data-trade-action]");
  if (tradeButton) return tradeAction(tradeButton);
  const tradeOpen = event.target.closest("[data-trade-open]");
  if (tradeOpen) {
    state.trades.selected = tradeOpen.dataset.tradeOpen;
    state.trades.detail = null;
    state.trades.message = "";
    renderTrades();
    return loadTrades();
  }
  if (event.target.closest("#trades-refresh")) { state.trades.message = ""; return loadTrades(); }
  const linkDecision = event.target.closest("[data-link-decision]");
  if (linkDecision) return openDecisionLink(linkDecision);
  if (event.target.closest("[data-link-confirm]")) return confirmDecisionLink();
  if (event.target.closest("[data-link-cancel]")) { state.trades.link = null; return renderDecisionLink(); }
  const selectSymbol = event.target.closest("[data-select-symbol]");
  if (selectSymbol) return savePreferences({ selected_currency_pair: selectSymbol.dataset.selectSymbol });
  if (event.target.closest("#go-live")) return goLive();
  if (event.target.closest("#retry")) return loadDashboard();
  if (event.target.closest("#monitor-toggle")) return savePreferences({ market_monitoring_enabled: !prefs().market_monitoring_enabled });
  // B7.2: the autonomous-execution preference is never written by the UI (no control exists).
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

// The only keyboard shortcut: Escape closes the Annotations dropdown (one listener, never added
// again; the page's CSP forbids inline handlers).
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && state.overlay.menuOpen) {
    setAnnotationMenu(false);
    document.querySelector("[data-annotation-menu]")?.focus();
  }
});

document.addEventListener("change", (event) => {
  const annotation = event.target.closest?.("[data-annotation-key]");
  if (annotation) return setAnnotations({ [annotation.dataset.annotationKey]: annotation.checked });
  if (event.target.id === "pair-select") savePreferences({ selected_currency_pair: event.target.value });
  if (event.target.id === "timeframe-select" || event.target.id === "settings-timeframe") savePreferences({ selected_chart_timeframe: event.target.value });
  if (event.target.id === "theme-select") savePreferences({ theme: event.target.value });
  if (event.target.id === "timezone-select") savePreferences({ user_timezone: event.target.value });
  const intentFilter = event.target.closest?.("[data-intent-filter]");
  if (intentFilter) {
    state.shadow.intentFilters[intentFilter.dataset.intentFilter] = intentFilter.value;
    state.shadow.selectedIntent = null;
    state.shadow.intentDetail = null;
    loadShadowIntents();
  }
});

systemDark.addEventListener("change", () => { if (prefs().theme === "system") render(); });
// Saved links to removed or section-level ids land on their target page; the address shows it.
function canonicalPage() {
  const page = pageFromHash();
  const raw = window.location.hash.replace(/^#/, "");
  if (raw && raw !== page && ROUTE_ALIASES[raw]) window.history.replaceState(null, "", `#${page}`);
  return page;
}
window.addEventListener("hashchange", () => showPage(canonicalPage()));
window.addEventListener("resize", () => drawChart());
// Live data is requested only while this page is open and visible (see startLivePolling).
window.addEventListener("pageshow", (event) => { if (event.persisted) startLivePolling(); });
state.page = canonicalPage();
loadDashboard().then(() => { startLivePolling(); loadPageData(state.page); });
