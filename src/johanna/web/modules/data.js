import { hooks } from "./hooks.js";
import { state } from "./state.js";
import { main } from "./format.js";
import { renderChartArea } from "./chart.js";
import { syncAnalysisPolling } from "./analysis.js";
import { render } from "./pages.js";

// ---------------------------------------------------------------- data loading
export async function savePreferences(values) {
  try {
    const response = await fetch("/api/preferences", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(values) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.message || payload.error || "Preference save failed");
    await loadDashboard();
    // Restart rather than wait: an answer for the old watchlist/selection must not land.
    if ("currency_pair_watchlist" in values) hooks.livePolling.quotes.restart();
    if ("selected_currency_pair" in values || "selected_chart_timeframe" in values) {
      hooks.livePolling.bars.restart();
      hooks.livePolling.analysis.restart();
      syncAnalysisPolling();
    }
    if ("user_timezone" in values) renderChartArea();
  } catch (error) {
    showSaveMessage(`Could not save: ${error.message}`);
  }
}

export function showSaveMessage(text) {
  const target = document.querySelector("#save-message");
  if (target) target.textContent = text;
}

export function isEditing() {
  const active = document.activeElement;
  return Boolean(active && main.contains(active) && active.matches("input, select, textarea"));
}

// Everything that, when changed, needs a full page render (live data is updated separately).
export const dashboardSignature = (data) => JSON.stringify([data?.preferences, data?.calendar, data?.news, data?.service_configuration]);

export async function loadDashboard({ background = false } = {}) {
  state.loading = true;
  const before = dashboardSignature(state.data);
  const hadError = Boolean(state.error);
  let ok = false;
  try {
    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) throw new Error(`Backend returned ${response.status}`);
    state.data = await response.json();
    state.dataReceivedAt = Date.now();
    state.error = null;
    ok = true;
  } catch (error) {
    state.error = error.message;
  } finally {
    state.loading = false;
    // A background refresh never re-renders under the user's cursor, and never rebuilds a
    // live page (and its chart) when nothing on it changed; the top bar always updates.
    const unchanged = ok && !hadError && dashboardSignature(state.data) === before && state.page !== "settings";
    if (background && (isEditing() || unchanged)) hooks.renderTopbar();
    else render();
  }
  return ok;
}
