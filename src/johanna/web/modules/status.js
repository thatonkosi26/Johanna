import { BARS_EXPIRE_MS, QUOTES_EXPIRE_MS, state } from "./state.js";
import { reasonText, services, stateLabel } from "./format.js";

export const calendarStatus = () => state.data?.calendar?.status ?? services().economic_calendar ?? {};

export function overallStatus(rows) {
  const statuses = new Set(rows.map((row) => row.status));
  return ["LIVE", "STALE", "CACHED"].find((status) => statuses.has(status)) ?? "UNAVAILABLE";
}
export const quotesExpired = () => Boolean(state.quotes) && Date.now() - state.quotesReceivedAt > QUOTES_EXPIRE_MS;
export const barsExpired = () => Boolean(state.bars) && Date.now() - state.barsReceivedAt > BARS_EXPIRE_MS;

// What the dashboard may show for quotes right now. After a failed or overdue refresh,
// previously fetched prices stay visible but become CACHED (with their age), the reason is
// shown, and the clock check is no longer "verified".
export function quoteView() {
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
export const connection = () => quoteView()?.connection ?? services().live_market ?? {};
export const quoteFor = (symbol) => quoteView()?.quotes?.find((row) => row.symbol === symbol);
// Before the first answer (success or failure) nothing is known yet: LOADING, not UNAVAILABLE.
export const marketStatus = () => quoteView()?.status ?? "LOADING";
export const quotesProblemText = () => (quoteView()?.error ? reasonText(quoteView().error.reason) : "");

export function collectorSummary() {
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
