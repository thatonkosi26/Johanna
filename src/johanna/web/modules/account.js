import { POLL, state } from "./state.js";
import { emptyState, esc, pageHeading, reasonText, setInner, stampSeconds, statusBadge } from "./format.js";
import { fetchJson, malformed } from "./api.js";

// ---------------------------------------------------------------- account & broker (Phase 3)
// Read-only account metrics and broker instrument specifications from /api/account/*. Every
// value arrives with a validation status; "unavailable", "invalid", "not applicable" and
// "unverified" are always shown as labelled chips, never as 0 or a blank. No execution controls.
export const ACCOUNT_EXPIRE_MS = 30000; // no fresh answer for this long: shown as not live
export const FIELD_STATUS_LABELS = { unavailable: "Unavailable", invalid: "Invalid", not_applicable: "N/A", unverified: "Unverified" };
export const ACCOUNT_CARDS = [
  ["currency", "Account currency"], ["balance", "Balance"], ["equity", "Equity"], ["margin_free", "Free margin"],
  ["margin", "Used margin"], ["margin_level", "Margin level"], ["leverage", "Leverage"],
];
export const ACCOUNT_PERMISSIONS = [
  ["trade_allowed", "Account trading allowed"], ["trade_expert", "Expert (API) trading allowed"],
  ["terminal_trade_allowed", "Terminal trading allowed"], ["terminal_tradeapi_disabled", "Terminal trade API disabled"],
  ["trade_mode", "Account mode"], ["margin_mode", "Margin mode"], ["margin_so_mode", "Stop-out mode"],
  ["margin_so_call", "Margin call level"], ["margin_so_so", "Stop-out level"], ["limit_orders", "Pending order limit"],
  ["profit", "Floating profit"], ["credit", "Credit"],
];
export const INSTRUMENT_ROWS = [
  ["Contract", [["broker_symbol", "Broker symbol"], ["currency_base", "Base currency"], ["currency_profit", "Profit currency"], ["currency_margin", "Margin currency"], ["contract_size", "Contract size"], ["digits", "Digits"], ["point", "Point"]]],
  ["Tick", [["tick_size", "Tick size"], ["tick_value", "Tick value"], ["tick_value_profit", "Tick value (profit)"], ["tick_value_loss", "Tick value (loss)"]]],
  ["Volume", [["volume_min", "Minimum volume"], ["volume_max", "Maximum volume"], ["volume_step", "Volume step"], ["volume_limit", "Aggregate limit"]]],
  ["Trading", [["trade_mode", "Trade permission"], ["calculation_mode", "Calculation mode"], ["execution_mode", "Execution mode"], ["order_types", "Order types"], ["filling_modes", "Filling modes"], ["stops_level", "Stops level"], ["freeze_level", "Freeze level"]]],
  ["Swap", [["swap_mode", "Swap mode"], ["swap_long", "Swap long"], ["swap_short", "Swap short"], ["swap_rollover3days", "Triple-swap day"]]],
];
export const QUOTE_ROWS = [["bid", "Bid"], ["ask", "Ask"], ["spread_points", "Spread (points)"], ["spread_reported_points", "Spread reported by MT5"]];

export const accountExpired = () => !state.account.receivedAt || Date.now() - state.account.receivedAt > ACCOUNT_EXPIRE_MS;
// What the page shows: the backend's own status, downgraded when a refresh failed or expired.
export function accountView() {
  const { summary, instruments, error } = state.account;
  let status = summary?.status ?? "UNAVAILABLE";
  if (summary && (error || accountExpired())) status = "CACHED";
  if (!summary && !error && !state.account.receivedAt) status = "LOADING";
  return { summary, instruments, status, error };
}

export function fieldText(report) {
  if (!report || typeof report !== "object") return { text: "Unavailable", chip: "unavailable", title: "No value" };
  const { value, status, unit, reason } = report;
  // Never show a number next to these labels: MT5 reports 0 for an undefined margin level.
  if (status === "unavailable" || status === "invalid" || status === "not_applicable") {
    return { text: FIELD_STATUS_LABELS[status], chip: status, title: reason ?? "" };
  }
  let text;
  if (typeof value === "boolean") text = value ? "Yes" : "No";
  else if (Array.isArray(value)) text = value.length ? value.map((item) => String(item).replaceAll("_", " ")).join(", ") : "None";
  else if (typeof value === "number") text = unit === "1:x" ? `1:${value}` : String(value);
  else text = String(value ?? "").replaceAll("_", " ");
  if (typeof value === "number" && unit && unit !== "1:x") text += ` ${unit}`;
  const chip = status === "verified" ? null : status;
  return { text, chip, title: reason ?? (report.source ? `Source: ${report.source}` : "") };
}
export function fieldMarkup(report) {
  const { text, chip, title } = fieldText(report);
  const chipMarkup = chip ? `<span class="field-chip ${esc(chip)}">${esc(FIELD_STATUS_LABELS[chip] ?? chip)}</span>` : "";
  if (chip && FIELD_STATUS_LABELS[chip] === text) return `<span class="field-value missing" data-field-status="${esc(chip)}" title="${esc(title)}">${chipMarkup}</span>`;
  return `<span class="field-value" data-field-status="${esc(report?.status ?? "unavailable")}" title="${esc(title)}">${esc(text)}${chipMarkup ? ` ${chipMarkup}` : ""}</span>`;
}
export const validationChip = (validation) => `<span class="field-chip validation ${esc(validation ?? "unavailable")}">${esc(String(validation ?? "unavailable").replaceAll("_", " "))}</span>`;

export function currencyNote(currency) {
  const unit = currency?.unit;
  if (!unit || currency.status !== "verified") return "";
  if (unit.kind === "cent") {
    const confirmed = unit.confirmation === "consistent" ? "confirmed by tick values" : `not confirmed (${String(unit.confirmation ?? "not checked").replaceAll("_", " ")})`;
    return `1 ${currency.value} = 1/${unit.units_per_major} ${unit.major} (cent unit) · ${confirmed}`;
  }
  return `${currency.value} (ISO currency)`;
}

export function accountContent() {
  const { summary, instruments, status, error } = accountView();
  if (!summary) {
    const message = status === "LOADING" ? "Waiting for the first answer from MT5." : (error?.message || "MT5 account information is unavailable.");
    return `<section class="panel">${emptyState(status === "LOADING" ? "Loading account information" : "Account information unavailable", message, status === "LOADING" ? "" : "error-state")}</section>`;
  }
  const fields = summary.account ?? {};
  const provenance = summary.provenance ?? {};
  const age = state.account.receivedAt ? Math.round((Date.now() - state.account.receivedAt) / 1000) : null;
  const freshness = status === "LIVE"
    ? "Live: fetched from MT5 just now"
    : `Not live: last retrieved ${stampSeconds(summary.retrieved_at_utc)}${error ? ` · ${error.message || reasonText(error.reason)}` : summary.reason ? ` · ${reasonText(summary.reason)}` : ""}`;
  const cards = ACCOUNT_CARDS.map(([key, label]) => `<div class="stat" data-account-field="${key}"><div class="stat-label">${esc(label)}</div><div class="stat-value">${fieldMarkup(fields[key])}</div></div>`).join("")
    + `<div class="stat" data-account-field="freshness"><div class="stat-label">Data status</div><div class="stat-value">${statusBadge(status)}</div></div>`;
  const permissions = ACCOUNT_PERMISSIONS.map(([key, label]) => `<div class="info-row" data-account-field="${key}"><span>${esc(label)}</span><strong>${fieldMarkup(fields[key])}</strong></div>`).join("");
  const checks = (summary.checks ?? []).map((check) => `<div class="info-row" data-check="${esc(check.check)}"><span>${esc(check.check.replaceAll("_", " "))}</span><strong class="check-${esc(check.status)}">${esc(check.status.replaceAll("_", " "))}<small> · ${esc(check.detail)}</small></strong></div>`).join("");
  const sourceRows = [
    ["Broker", provenance.company ?? "—"], ["Trade server", provenance.server ?? "—"], ["Account type", provenance.account_type ?? "—"],
    ["Provenance", provenance.status === "established" ? "Established (identity re-checked after the read)" : `Not established: ${provenance.reason ?? "unknown"}`],
    ["Retrieved", `${stampSeconds(summary.retrieved_at_utc)}${age !== null ? ` · received ${age} s ago` : ""}`],
    ["Freshness", freshness],
    ["Account validation", String(summary.validation ?? "—").replaceAll("_", " ")],
  ];
  const sources = sourceRows.map(([label, value]) => `<div class="info-row"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("");
  const note = currencyNote(fields.currency);
  return `<div class="stats-strip account-strip" id="account-overview">${cards}</div>
    <div class="account-grid">
      <section class="panel"><div class="panel-head"><span class="panel-title">Source &amp; freshness</span><span class="panel-meta">${provenance.status === "established" ? "PROVENANCE ESTABLISHED" : "PROVENANCE NOT ESTABLISHED"}</span></div><div class="info-list" id="account-source">${sources}${note ? `<div class="info-row"><span>Currency unit</span><strong id="account-currency-note">${esc(note)}</strong></div>` : ""}</div></section>
      <section class="panel"><div class="panel-head"><span class="panel-title">Permissions &amp; margin rules</span><span class="panel-meta">AS REPORTED BY MT5</span></div><div class="info-list" id="account-permissions">${permissions}</div></section>
      <section class="panel"><div class="panel-head"><span class="panel-title">Consistency checks</span><span class="panel-meta">ACCOUNT</span></div><div class="info-list" id="account-checks">${checks || '<div class="info-row"><span>No checks</span><strong>—</strong></div>'}</div></section>
    </div>
    ${instrumentsMarkup(instruments, status)}
    <div class="panel-note account-note">Read-only. Account and symbol values are read one after another on one MT5 connection (not atomically); each has its own retrieval time. Tick values are point-in-time, in the account currency. Johanna places no orders.</div>`;
}

export function instrumentsMarkup(payload, status) {
  const items = payload?.instruments ?? [];
  if (!items.length) return `<section class="panel"><div class="panel-head"><span class="panel-title">Instrument specifications</span></div>${emptyState("No instrument data", "The broker specifications are unavailable.", "error-state")}</section>`;
  const head = `<tr><th>Field</th>${items.map((item) => `<th data-instrument="${esc(item.symbol)}">${esc(item.broker_symbol ?? item.symbol)} ${validationChip(item.validation)}</th>`).join("")}</tr>`;
  const row = (label, key, cell) => `<tr data-row="${esc(key)}"><td class="field-name">${esc(label)}</td>${items.map((item) => `<td>${cell(item)}</td>`).join("")}</tr>`;
  const groups = INSTRUMENT_ROWS.map(([group, rows]) => `<tr class="group-row"><td colspan="${items.length + 1}">${esc(group)}</td></tr>${rows.map(([key, label]) => row(label, key, (item) => (item.fields && Object.keys(item.fields).length ? fieldMarkup(item.fields[key]) : fieldMarkup(null)))).join("")}`).join("");
  const quotes = `<tr class="group-row"><td colspan="${items.length + 1}">Current quote</td></tr>${QUOTE_ROWS.map(([key, label]) => row(label, key, (item) => fieldMarkup(item.quote?.[key]))).join("")}`
    + row("Quote freshness", "quote_freshness", (item) => statusBadge(status === "LIVE" ? (item.quote?.freshness ?? "UNAVAILABLE") : status === "LOADING" ? "LOADING" : "CACHED", item.quote?.quote_age_seconds != null ? `${item.quote.quote_age_seconds} s` : ""))
    + row("Retrieved", "retrieved", (item) => esc(stampSeconds(item.retrieved_at_utc)))
    + row("Checks", "checks", (item) => esc((item.checks ?? []).filter((check) => check.status !== "consistent").map((check) => `${check.check.replaceAll("_", " ")}: ${check.status.replaceAll("_", " ")}`).join("; ") || "all consistent"));
  return `<section class="panel"><div class="panel-head"><span class="panel-title">Instrument specifications</span><span class="panel-meta">FROM THE CONNECTED BROKER · ${items.length} SYMBOLS</span></div><div class="table-scroll"><table class="data-table instrument-table" id="instrument-table"><thead>${head}</thead><tbody>${groups}${quotes}</tbody></table></div></section>`;
}

export function accountPage() {
  return `${pageHeading("Account & Broker", "Read-only account metrics and broker specifications from MT5", `<span id="account-heading-badge">${statusBadge(accountView().status)}</span>`)}<div id="account-content">${accountContent()}</div>`;
}

export function renderAccount() {
  if (state.page !== "account") return;
  setInner(document.querySelector("#account-heading-badge"), statusBadge(accountView().status));
  setInner(document.querySelector("#account-content"), accountContent());
}

export async function refreshAccount(poller) {
  const seq = ++state.account.seq;
  const summary = await fetchJson("/api/account/summary", poller, POLL.accountTimeoutMs);
  if (summary.aborted || seq !== state.account.seq) return "aborted";
  const instruments = summary.error ? summary : await fetchJson("/api/account/instruments", poller, POLL.accountTimeoutMs);
  if (instruments.aborted || seq !== state.account.seq) return "aborted";
  const valid = (payload) => typeof payload?.status === "string" && "provenance" in payload;
  const error = summary.error ?? instruments.error
    ?? (valid(summary.payload) && valid(instruments.payload) && Array.isArray(instruments.payload.instruments) ? null : malformed());
  if (error) {
    state.account.error = error; // earlier values stay, shown as not live
  } else {
    state.account.summary = summary.payload;
    state.account.instruments = instruments.payload;
    state.account.receivedAt = Date.now();
    state.account.error = null;
  }
  renderAccount();
  return !error;
}

export const accountInterval = () => (accountView().status === "LIVE" ? POLL.accountMs : POLL.quietAccountMs);
export const accountActive = () => state.page === "account";
