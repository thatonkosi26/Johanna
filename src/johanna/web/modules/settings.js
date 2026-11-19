import { TIMEFRAMES, state } from "./state.js";
import { esc, pageHeading, prefs } from "./format.js";
import { collectorSummary } from "./status.js";
import { healthMarkup } from "./panels.js";

export function timezoneOptions(selected) {
  let zones = [];
  try { zones = Intl.supportedValuesOf("timeZone"); } catch { zones = []; }
  if (!zones.includes("UTC")) zones = ["UTC", ...zones];
  if (selected && !zones.includes(selected)) zones = [selected, ...zones];
  return zones.map((zone) => `<option value="${esc(zone)}" ${zone === selected ? "selected" : ""}>${esc(zone)}</option>`).join("");
}

export function settingsPage() {
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
        <div class="settings-row" id="settings-operating-mode"><div><div class="settings-name">Operating mode</div><div class="settings-description">Johanna operates in manual mode: it analyses and records, and you execute trades yourself in MT5. Autonomous operation is not available.${mode !== "manual" ? ` A previously saved preference (${esc(mode)}) has no effect.` : ""}</div></div><strong class="settings-readonly">Manual</strong></div>
        <div class="settings-row" id="settings-autonomous-execution"><div><div class="settings-name">Autonomous execution</div><div class="settings-description">Autonomous execution is disabled. Johanna does not have authorisation to execute trades autonomously.</div></div><strong class="settings-readonly">Disabled</strong></div>
        <div class="settings-row"><div><div class="settings-name">Background collector (optional) · ${esc(collector.label)}</div><div class="settings-description">${esc(collector.detail)}</div></div><button class="toggle ${status?.market_monitoring_enabled ? "on" : ""}" id="monitor-toggle" role="switch" aria-checked="${Boolean(status?.market_monitoring_enabled)}" aria-label="Toggle background collector"></button></div>
      </div></section>
      <section class="panel"><div class="panel-head"><span class="panel-title">Application services</span><span class="panel-meta">OPERATIONAL HEALTH</span></div>${healthMarkup()}<div class="settings-section"><div class="settings-note">Secrets are read from process environment variables and are not part of workstation preferences. This local backend does not authenticate remote users.</div><div class="settings-row"><div><div class="settings-name">Watchlist</div><div class="settings-description">${esc(watchlist || "Empty")}</div></div><button class="button" data-page="markets">Edit</button></div></div></section>
    </div><div id="save-message" class="save-message" aria-live="polite"></div>`;
}
