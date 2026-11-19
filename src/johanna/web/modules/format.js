import { sectionOf, state } from "./state.js";

export const main = document.querySelector("#main-content");
export const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
export const prefs = () => state.data?.preferences ?? {};
export const services = () => state.data?.status?.services ?? {};
export const tz = () => prefs().user_timezone || "UTC";
export const price = (value, digits) => value == null || !Number.isFinite(Number(value)) ? "—" : Number(value).toFixed(digits ?? 5);
export const stamp = (value, timezone = tz()) => {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, { timeZone: timezone, dateStyle: "medium", timeStyle: "short" }).format(date);
};
export const clockTime = (value) => {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, { timeZone: tz(), hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
};
export function ageText(isoTime) {
  if (!isoTime) return "—";
  const seconds = Math.max(0, (Date.now() - new Date(isoTime).getTime()) / 1000);
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${Math.round(seconds % 60)}s`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`;
  return `${Math.floor(seconds / 86400)}d ${Math.floor((seconds % 86400) / 3600)}h`;
}

export const REASONS = {
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
export const reasonText = (code) => REASONS[code] ?? (code ? String(code).replaceAll("_", " ") : "");
export const STATUS_HELP = {
  LIVE: "Fetched from MT5 just now",
  STALE: "Fetched from MT5 just now, but the last tick is old (market closed or quiet)",
  CACHED: "MT5 unavailable: last value fetched earlier, not live",
  UNAVAILABLE: "No data available",
  LOADING: "Waiting for the first answer from MT5",
};
export function statusBadge(status, extra = "") {
  const value = status || "UNAVAILABLE";
  return `<span class="status-badge ${value.toLowerCase()}" title="${esc(STATUS_HELP[value] ?? "")}">${esc(value)}${extra ? ` · ${esc(extra)}` : ""}</span>`;
}

export function icon(path) { return `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="${path}"/></svg>`; }
// Backend service states (Settings > Application services). Only "running"/"connected" are green.
export const STATE_LABELS = {
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
export const FRESHNESS_LABELS = {
  fresh: ["Fresh", "online"],
  stale: ["Stale", "warning"],
  no_data: ["No data", ""],
  no_snapshot: ["No snapshot", ""],
  error: ["Unreadable", "error"],
};
export const stateLabel = (value) => STATE_LABELS[value] ?? [String(value || "Unknown").replaceAll("_", " "), ""];
export const freshnessLabel = (value) => FRESHNESS_LABELS[value] ?? ["Unknown", ""];
export function pageHeading(title, subtitle, extra = "") {
  // The eyebrow names the navigation section (B7.2), e.g. "Decisions & Risk" above "Decisions".
  const eyebrow = sectionOf(state.page).label;
  return `<div class="page-heading"><div><div class="eyebrow">${esc(eyebrow)}</div><h1>${esc(title)}</h1><div class="page-subtitle">${esc(subtitle)}</div></div>${extra ? `<div class="heading-actions">${extra}</div>` : ""}</div>`;
}

export function stat(label, value, isEmpty = false, raw = false) {
  return `<div class="stat"><div class="stat-label">${esc(label)}</div><div class="stat-value ${isEmpty ? "empty" : ""}">${raw ? value : esc(value)}</div></div>`;
}

export function emptyState(title, message, className = "") {
  return `<div class="empty-state ${className}"><strong>${esc(title)}</strong>${esc(message)}</div>`;
}

// Targeted updates: an element is rewritten only when its markup actually changed.
export function setInner(element, markup) {
  if (element && element.__markup !== markup) {
    element.innerHTML = markup;
    element.__markup = markup;
  }
}
export const stampSeconds = (value) => {
  const date = new Date(value ?? "");
  if (!value || Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat(undefined, { timeZone: tz(), dateStyle: "medium", timeStyle: "medium" }).format(date);
};
export const pretty = (value) => String(value ?? "—").replaceAll("_", " ");
