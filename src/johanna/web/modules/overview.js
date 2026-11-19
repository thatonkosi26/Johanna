// B7.2 Overview: the application home. Market summary (existing quote strip, chart, watchlist and
// compact analysis -- the same pollers as before, no new MT5 request), account / decisions /
// journal summaries built only from data the application already holds or reads from the journal
// (no MT5), the global status band and system-state notices from the readiness snapshot.
import { state } from "./state.js";
import { emptyState, esc, pageHeading, prefs, pretty, setInner, stampSeconds, statusBadge } from "./format.js";
import { marketStatus } from "./status.js";
import { malformed, tradeGet } from "./api.js";
import { marketInfoMarkup, quoteStripMarkup, watchlistMarkup } from "./quotes.js";
import { chartMarkup } from "./chart.js";
import { analysisPanelCompact } from "./analysis.js";
import { calendarMarkup } from "./panels.js";
import { accountExpired, fieldMarkup } from "./account.js";
import { RISK_STATUS, riskKey } from "./risk.js";
import { dimensionChip } from "./decisions.js";
import { opsChip, statusBand } from "./components.js";
import { opsViews } from "./control.js";

// Decisions: a journal read only (no MT5), loaded when Overview opens, never polled. The trade
// list is NOT requested here: GET /api/trades values open trades against live quotes (an MT5
// read), so Overview uses the list already loaded by the Trade Journal screen, if any.
export async function loadOverview() {
  const seq = ++state.overview.seq;
  const decisions = await tradeGet("/api/journal/decisions?limit=6");
  if (seq !== state.overview.seq) return;
  if (Array.isArray(decisions.payload?.decisions)) { state.overview.decisions = decisions.payload; state.overview.decisionsError = null; }
  else state.overview.decisionsError = decisions.error ?? malformed();
  state.overview.receivedAt = Date.now();
  if (state.page === "overview") setInner(document.querySelector("#overview-cards"), overviewCards());
}

const card = (title, meta, body, id) => `<section class="panel overview-card" id="${esc(id)}"><div class="panel-head"><span class="panel-title">${esc(title)}</span><span class="panel-meta">${esc(meta)}</span></div>${body}</section>`;
const line = (label, value, id = "") => `<div class="info-row"${id ? ` id="${esc(id)}"` : ""}><span>${esc(label)}</span><strong>${value}</strong></div>`;

function accountCard() {
  const summary = state.account.summary;
  if (!summary) {
    return card("Account", "NOT LOADED", `<div class="inactive-copy">Account values are read from MT5 only on the Account &amp; Broker screen; none have been read in this session. <button class="text-link" type="button" data-page="account">Open Account &amp; Broker</button></div>`, "overview-account");
  }
  const fields = summary.account ?? {};
  const live = summary.status === "LIVE" && !state.account.error && !accountExpired();
  const freshness = live ? "LIVE" : "CACHED";
  return card("Account", `${freshness} · RETRIEVED ${stampSeconds(summary.retrieved_at_utc)}`, `<div class="info-list">
    ${line("Balance", fieldMarkup(fields.balance), "overview-balance")}
    ${line("Equity", fieldMarkup(fields.equity), "overview-equity")}
    ${line("Free margin", fieldMarkup(fields.margin_free), "overview-free-margin")}
    ${line("Validation", esc(pretty(summary.validation ?? "unavailable")))}
    ${line("Data status", statusBadge(freshness))}</div>`, "overview-account");
}

function riskLine() {
  const payload = state.risk.key === riskKey() ? state.risk.payload : null;
  if (!payload) return line("Current risk proposal", `${opsChip("NOT CHECKED")} <span class="muted">not evaluated in this session (open Decisions &amp; Risk)</span>`, "overview-risk");
  const [label] = RISK_STATUS[payload.status] ?? ["Unavailable"];
  const volume = payload.sizing?.volume;
  const blocking = volume == null
    ? `No actionable volume: ${esc(payload.rejection_reasons?.[0]?.reason ?? payload.unverified?.[0]?.reason ?? (payload.sizing?.includes_costs === false ? "transaction costs not established" : "not established"))}`
    : `Proposed ${esc(volume)} lots — a proposal for manual review, not an order`;
  return line("Current risk proposal", `<span class="field-chip" data-overview-risk="${esc(payload.status)}">${esc(label)}</span> <span class="muted">${esc(payload.symbol)} · ${esc(payload.timeframe ?? "")} · ${blocking}</span>`, "overview-risk");
}

function decisionsCard() {
  const data = state.overview.decisions;
  let body;
  if (!data) {
    body = state.overview.decisionsError
      ? emptyState("Decisions unavailable", state.overview.decisionsError.message || "The journal request failed.", "error-state")
      : emptyState("Loading", "Reading the decision journal.");
  } else if (!data.decisions.length) {
    body = emptyState("No decisions recorded yet", "Decisions are recorded when an analysis or a risk proposal is requested.");
  } else {
    body = `<div class="table-scroll"><table class="data-table compact-table" id="overview-decisions"><thead><tr><th>Last seen</th><th>Market</th><th>Analysis</th><th>Risk</th><th>Eligibility</th><th>Execution</th></tr></thead><tbody>${data.decisions.map((d) => `<tr data-decision="${esc(d.id)}"><td>${esc(stampSeconds(d.last_seen_utc))}</td><td>${esc(d.symbol)} · ${esc(d.timeframe)}</td><td>${esc(pretty(d.analysis_outcome))}</td><td>${dimensionChip(d.risk_status, "risk")}</td><td>${dimensionChip(d.trade_eligibility, "eligibility")}</td><td>${dimensionChip(d.execution_status, "execution")}</td></tr>`).join("")}</tbody></table></div>`;
  }
  return card("Decisions & risk", "ANALYSIS · RISK · ELIGIBILITY · EXECUTION (SEPARATE)", `${body}<div class="info-list">${riskLine()}</div>
    <div class="panel-note account-note">A market-analysis candidate is not an approved trade, and a risk proposal is not an order. <button class="text-link" type="button" data-page="signals">Decision history</button></div>`, "overview-decisions-card");
}

function todayKey(iso) {
  try {
    return new Intl.DateTimeFormat("en-CA", { timeZone: prefs().user_timezone || "UTC" }).format(new Date(iso));
  } catch {
    return null;
  }
}

// Only trades fully closed today with a calculated net result are summed; anything else with an
// exit today is reported as "not included" -- never estimated.
function journalCard() {
  const data = state.trades.list;
  if (!Array.isArray(data?.trades)) {
    return card("Trade journal", "NOT LOADED", `<div class="inactive-copy">The trade list has not been loaded in this session (it values open trades against live MT5 quotes, so it is read only on the Trade Journal screen). <button class="text-link" type="button" data-page="history">Open Trade Journal</button></div>`, "overview-journal");
  }
  const trades = data.trades;
  const count = (status) => trades.filter((t) => t.status === status).length;
  const today = todayKey(new Date().toISOString());
  let sum = 0;
  let included = 0;
  let excluded = 0;
  let currency = null;
  let unknownCosts = 0;
  for (const t of trades) {
    const acc = t.accounting ?? {};
    const exits = acc.exits ?? [];
    if (exits.length && acc.net_realised == null) unknownCosts += 1;
    if (!exits.some((e) => todayKey(e.time_utc) === today)) continue;
    const lastExit = exits.map((e) => e.time_utc).sort().at(-1);
    if (t.status === "CLOSED" && acc.net_realised != null && todayKey(lastExit) === today && (currency == null || currency === acc.currency)) {
      sum += Number(acc.net_realised);
      currency = acc.currency ?? currency;
      included += 1;
    } else {
      excluded += 1;
    }
  }
  const realised = included
    ? `${esc(sum.toFixed(2))} ${esc(currency ?? "")} net realised · ${included} trade${included === 1 ? "" : "s"} closed today`
    : "No trade fully closed today with a calculated net result";
  return card("Trade journal", "MANUAL RECORDS · NOT BROKER-VERIFIED", `<div class="info-list">
    ${line("Open", esc(count("OPEN")), "overview-open")}
    ${line("Partially closed", esc(count("PARTIALLY_CLOSED")))}
    ${line("Closed", esc(count("CLOSED")), "overview-closed")}
    ${line("Voided", esc(count("VOIDED")))}
    ${line("Today's realised result", esc(realised), "overview-today")}
    ${line("Not included today", excluded ? `${excluded} trade${excluded === 1 ? "" : "s"} with exits today (partial or not calculable)` : "none", "overview-excluded")}
    ${line("Exits with incomplete accounting", unknownCosts ? `${unknownCosts} trade${unknownCosts === 1 ? "" : "s"} (net result not calculated)` : "none", "overview-incomplete")}</div>
    <div class="panel-note account-note">Unrealised results are estimates and are not included. <button class="text-link" type="button" data-page="history">Open Trade Journal</button></div>`, "overview-journal");
}

// System-state notices from the readiness snapshot (not notifications; nothing is delivered).
function alertsCard() {
  const r = state.ops.readiness;
  if (!r) {
    const body = state.ops.error
      ? emptyState("Readiness unavailable", state.ops.error.message || "The readiness request failed.", "error-state")
      : emptyState("Checking", "Waiting for the readiness assessment.");
    return card("Operational notices", "SYSTEM STATE", body, "overview-alerts");
  }
  const notices = [];
  if (r.unattended_operation_permitted !== true) notices.push(["NOT PERMITTED", "Unattended operation is not permitted.", "unattended"]);
  // Overview wording for a known code (the full backend reason is on the Control Centre); the
  // execution notice states the safety fact without naming a feature (placeholder guard).
  const wording = { execution_disabled: "Order placement is disabled by design: Johanna is manual-only." };
  for (const blocker of r.unattended_blocking_reasons ?? []) notices.push([{ costs_unverified: "NOT CONFIGURED", scheduler_disabled: "OFF", execution_disabled: "DISABLED" }[blocker.code] ?? "INACTIVE", wording[blocker.code] ?? blocker.reason, blocker.code]);
  notices.push(["DISABLED", "Automatic backups are disabled (no backup schedule exists).", "backups_disabled"]);
  for (const check of r.checks ?? []) if (check.status !== "ok") notices.push(["ATTENTION", `${pretty(check.check)} needs attention.`, `check_${check.check}`]);
  return card("Operational notices", "SYSTEM STATE · NOT NOTIFICATIONS", `<ul class="notice-list">${notices.map(([value, text, code]) => `<li data-notice="${esc(code)}">${opsChip(value)}<span>${esc(text)}</span></li>`).join("")}</ul>
    <div class="panel-note account-note"><button class="text-link" type="button" data-page="control">Open System Control Centre</button></div>`, "overview-alerts");
}

export function overviewCards() {
  return `${accountCard()}${decisionsCard()}${journalCard()}${alertsCard()}`;
}

export function overviewPage() {
  return `${pageHeading("Overview", "Market, decisions, journal and system state", `<span id="market-heading-badge">${statusBadge(marketStatus())}</span>`)}
    <div id="overview-status">${statusBand()}</div>
    ${quoteStripMarkup()}
    <div class="overview-grid">
      ${chartMarkup()}
      <section class="panel watchlist-panel"><div class="panel-head"><span class="panel-title">Watchlist</span><span class="panel-meta">${(prefs().currency_pair_watchlist ?? []).length} PAIRS · MT5</span></div>${watchlistMarkup()}</section>
    </div>
    <div class="overview-cards" id="overview-cards">${overviewCards()}</div>
    <div class="lower-grid">
      <section class="panel lower-panel"><div class="panel-head"><span class="panel-title">Market information</span><span class="panel-meta">DATA FRESHNESS</span></div>${marketInfoMarkup()}</section>
      <section class="panel lower-panel"><div class="panel-head"><span class="panel-title">Economic calendar</span><button class="text-link" data-page="calendar">View calendar</button></div>${calendarMarkup(4)}</section>
      ${analysisPanelCompact()}
    </div>`;
}

opsViews.overview = () => {
  setInner(document.querySelector("#overview-status"), statusBand());
  setInner(document.querySelector("#overview-cards"), overviewCards());
};
