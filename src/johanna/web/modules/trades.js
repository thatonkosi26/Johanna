import { hooks } from "./hooks.js";
import { state } from "./state.js";
import { emptyState, esc, pageHeading, pretty, reasonText, setInner, stampSeconds } from "./format.js";
import { malformed, tradeGet } from "./api.js";

// ---------------------------------------------------------------- trade history (P5.2)
// Manual records of trades the user already executed in MT5. Johanna places, modifies and
// closes nothing: there are no execution controls here. Every write carries a client request
// id (reused while a request's outcome is unknown, so a retry can never record twice).
export const TRADE_BANNER = "Record trades you have already executed in MT5. Johanna does not place, modify or close orders.";
export const TRADE_SYMBOLS = ["EURUSDc", "GBPUSDc", "USDJPYc"];
export const TRADE_TONE = { OPEN: "unverified", PARTIALLY_CLOSED: "unverified", CLOSED: "not_applicable", VOIDED: "invalid", COMPLETE: "verified", PARTIAL: "unverified", MISSING: "invalid", MANUAL_UNVERIFIED: "unverified" };
export const tradeChip = (value, field) => `<span class="field-chip ${TRADE_TONE[value] ?? ""}" data-trade-chip="${esc(field)}">${esc(pretty(value))}</span>`;
export const nowUtcInput = () => new Date().toISOString().slice(0, 19);
export const lots = (value) => (value === null || value === undefined ? "—" : String(Number(Number(value).toFixed(8))));

export function newRequestId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  const b = window.crypto.getRandomValues(new Uint8Array(16));
  b[6] = (b[6] & 0x0f) | 0x40;
  b[8] = (b[8] & 0x3f) | 0x80;
  const h = [...b].map((x) => x.toString(16).padStart(2, "0")).join("");
  return `${h.slice(0, 8)}-${h.slice(8, 12)}-${h.slice(12, 16)}-${h.slice(16, 20)}-${h.slice(20)}`;
}

export function money(value, currency) {
  if (value === null || value === undefined) return null;
  return `${Number(value).toFixed(2)}${currency ? ` ${currency}` : ""}`;
}

// Unknown costs are never shown as zero.
export function costText(cost, currency) {
  if (cost.complete) return money(cost.known_total, currency);
  const fills = `${cost.unknown_fills} fill${cost.unknown_fills === 1 ? "" : "s"}`;
  return `${money(cost.known_total, currency)} known · UNKNOWN on ${fills}`;
}

export function tradeRows(rows) {
  return `<div class="info-list">${rows.map(([label, value, id]) => `<div class="info-row"${id ? ` id="${id}"` : ""}><span>${esc(label)}</span><strong>${esc(value ?? "—")}</strong></div>`).join("")}</div>`;
}

export function fieldInput(name, label, { type = "number", value = "", placeholder = "", step = "any", required = false } = {}) {
  const attrs = type === "number" ? ` step="${step}" inputmode="decimal"` : type === "datetime-local" ? ' step="1"' : "";
  return `<label class="trade-field"><span>${esc(label)}${required ? "" : " <em>optional</em>"}</span><input class="text-input" name="${esc(name)}" type="${type}"${attrs} value="${esc(value)}" placeholder="${esc(placeholder)}"></label>`;
}

export function costInputs(withPnl) {
  return `${fieldInput("commission", "Commission", { placeholder: "unknown if blank" })}${fieldInput("swap", "Swap", { placeholder: "unknown if blank" })}${fieldInput("fee", "Other fees", { placeholder: "unknown if blank" })}${withPnl ? fieldInput("realised_pnl", "Realised P&L from MT5", { placeholder: "not entered if blank" }) : ""}`;
}

export function recordFormMarkup() {
  return `<section class="panel"><div class="panel-head"><span class="panel-title">Record an executed trade</span><span class="panel-meta">MANUAL RECORD · NOT AN ORDER</span></div>
    <div class="trade-form" data-trade-form="create" id="trade-record-form">
      <label class="trade-field"><span>Symbol</span><select class="select" name="symbol">${TRADE_SYMBOLS.map((symbol) => `<option>${symbol}</option>`).join("")}</select></label>
      <label class="trade-field"><span>Direction</span><select class="select" name="direction"><option>BUY</option><option>SELL</option></select></label>
      ${fieldInput("time_utc", "Entry time (UTC)", { type: "datetime-local", value: nowUtcInput(), required: true })}
      ${fieldInput("price", "Entry price", { required: true })}
      ${fieldInput("volume", "Volume (lots)", { required: true, step: "0.01" })}
      ${fieldInput("stop_loss", "Stop-loss", { placeholder: "not set if blank" })}
      ${fieldInput("take_profit", "Take-profit", { placeholder: "not set if blank" })}
      ${costInputs(false)}
      ${fieldInput("strategy_tag", "Strategy tag", { type: "text" })}
      ${fieldInput("note", "Note", { type: "text" })}
      <div class="trade-actions"><button class="button primary" type="button" data-trade-action="create">Record trade</button></div>
    </div>
    <div class="panel-note account-note">Blank costs are stored as unknown, never as zero. A missing stop-loss or take-profit is recorded and shown as incomplete protection.</div></section>`;
}

export function tradeListMarkup() {
  const { list, error } = state.trades;
  if (!list) return `<section class="panel">${error ? emptyState("Trade history unavailable", error.message || reasonText(error.reason), "error-state") : emptyState("Loading", "Waiting for the trade logbook.")}</section>`;
  const rows = list.trades.map((t) => {
    const acc = t.accounting;
    const realised = acc.net_realised !== null && acc.exits.length
      ? `${money(acc.net_realised, acc.currency)} net`
      : acc.gross_realised_calculated !== null && acc.exits.length ? `${money(acc.gross_realised_calculated, acc.currency)} gross` : "—";
    return `<tr data-trade="${esc(t.id)}" class="${t.id === state.trades.selected ? "selected" : ""}">
      <td>${esc(t.first_entry_utc ? stampSeconds(t.first_entry_utc) : "—")}</td>
      <td>${esc(t.symbol)}</td><td>${esc(t.direction)}</td>
      <td data-col="status">${tradeChip(t.status, "status")}</td>
      <td data-col="protection">${tradeChip(t.protection.state, "protection")}</td>
      <td class="num">${esc(lots(acc.open_volume))}</td>
      <td class="num">${esc(acc.average_entry_price ?? "—")}</td>
      <td class="num">${esc(realised)}</td>
      <td>${tradeChip(t.verification, "verification")}</td>
      <td><button class="button small" type="button" data-trade-open="${esc(t.id)}">Details</button></td></tr>`;
  }).join("");
  return `<section class="panel"><div class="panel-head"><span class="panel-title">Recorded trades</span><span class="panel-meta">${list.trades.length} SHOWN · MANUAL · NOT BROKER-VERIFIED</span></div>
    ${list.trades.length ? `<div class="table-scroll"><table class="data-table trades-table" id="trades-table"><thead><tr><th>Opened</th><th>Symbol</th><th>Side</th><th>Status</th><th>Protection</th><th class="num">Open lots</th><th class="num">Avg entry</th><th class="num">Realised</th><th>Verification</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`
      : emptyState("No trades recorded", "Record a trade after you have executed it in MT5.")}</section>`;
}

export function accountingMarkup(t) {
  const acc = t.accounting;
  const cur = acc.currency;
  const estimated = String(acc.gross_realised_calculated_method).startsWith("calculated_estimate");
  const rows = [
    ["Entry / exit / open volume", `${lots(acc.entry_volume)} / ${lots(acc.exit_volume)} / ${lots(acc.open_volume)} lots`],
    ["Average entry price (all entries)", acc.average_entry_price],
    ["Average entry of the open volume", acc.open_average_entry_price],
    ["Average exit price", acc.average_exit_price],
    ["Gross realised P&L (calculated)", !acc.exits.length ? "No exits" : acc.gross_realised_calculated === null ? `Not calculated · ${pretty(acc.gross_realised_calculated_method)}` : `${money(acc.gross_realised_calculated, cur)} · calculated${estimated ? " · ESTIMATED (converted at the exit price)" : ""}`, "trade-gross-calculated"],
    ["Gross realised P&L (user-entered)", !acc.exits.length ? "No exits" : acc.gross_realised_user_entered === null ? "Not entered for every exit" : `${money(acc.gross_realised_user_entered, cur)} · user-entered`, "trade-gross-user"],
    ["Commission", costText(acc.costs.commission, cur), "trade-cost-commission"],
    ["Swap", costText(acc.costs.swap, cur), "trade-cost-swap"],
    ["Other fees", costText(acc.costs.fee, cur), "trade-cost-fee"],
    ["Net realised P&L", !acc.exits.length ? "No exits" : acc.net_realised === null ? `Not calculated · ${acc.net_realised_status}` : `${money(acc.net_realised, cur)} · calculated from recorded fills`, "trade-net"],
    ["Unrealised P&L", acc.unrealised.value === null ? pretty(acc.unrealised.status) : `${money(acc.unrealised.value, cur)} · ESTIMATE from a live quote, not broker-verified`, "trade-unrealised"],
  ];
  const exitRows = acc.exits.map((e) => `<tr><td>${esc(stampSeconds(e.time_utc))}</td><td class="num">${esc(lots(e.volume))}</td><td class="num">${esc(e.exit_price)}</td><td class="num">${esc(e.average_entry_price)}</td><td class="num">${esc(e.gross_calculated === null ? "—" : money(e.gross_calculated, cur))}${String(e.gross_calculated_method).startsWith("calculated_estimate") ? " (est.)" : ""}</td><td class="num">${esc(e.gross_user_entered === null ? "not entered" : money(e.gross_user_entered, cur))}</td></tr>`).join("");
  const exits = acc.exits.length ? `<div class="table-scroll"><table class="data-table" id="trade-exits"><thead><tr><th>Exit time</th><th class="num">Lots</th><th class="num">Exit</th><th class="num">Avg entry at exit</th><th class="num">Calculated</th><th class="num">User-entered</th></tr></thead><tbody>${exitRows}</tbody></table></div>` : "";
  return `<section class="panel"><div class="panel-head"><span class="panel-title">P&amp;L and costs</span><span class="panel-meta">${esc(cur ?? "CURRENCY UNVERIFIED")} · COSTS SIGNED AS IN MT5</span></div><div id="trade-accounting">${tradeRows(rows)}</div>${exits}
    <div class="panel-note account-note">Each exit is valued against the average price of the entries recorded before it. Unknown costs are never treated as zero: net P&amp;L is withheld until every cost is known.</div></section>`;
}

export function tradeDetailMarkup() {
  const { detail, detailError, selected } = state.trades;
  if (!selected) return "";
  if (!detail) return `<section class="panel">${detailError ? emptyState("Trade unavailable", detailError.message || reasonText(detailError.reason), "error-state") : emptyState("Loading", "Loading the trade.")}</section>`;
  const t = detail;
  const open = t.status !== "VOIDED";
  const fills = t.fills.map((f) => `<tr data-fill="${esc(f.id)}" class="${f.status === "VOIDED" ? "voided" : ""}">
      <td>${esc(stampSeconds(f.time_utc))}</td><td>${esc(f.kind)}</td><td class="num">${esc(f.price)}</td><td class="num">${esc(lots(f.volume))}</td>
      <td class="num">${esc(f.commission ?? "unknown")}</td><td class="num">${esc(f.swap ?? "unknown")}</td><td class="num">${esc(f.fee ?? "unknown")}</td>
      <td class="num">${esc(f.kind === "exit" ? (f.realised_pnl_user ?? "not entered") : "—")}</td>
      <td>${esc(f.status)}${f.voided_reason ? ` · ${esc(f.voided_reason)}` : ""}${f.corrects_fill_id ? " · correction" : ""}</td>
      <td>${open && f.status === "RECORDED" ? `<div class="trade-inline" data-trade-form="void-fill-${esc(f.id)}"><input class="text-input" name="reason" placeholder="Reason to void" aria-label="Reason to void this fill"><button class="button small" type="button" data-trade-action="void-fill" data-fill-id="${esc(f.id)}">Void</button></div>` : ""}</td></tr>`).join("");
  const protection = t.protection_history.map((p) => `<tr><td>${esc(stampSeconds(p.effective_utc))}</td><td class="num">${esc(p.sl ?? "not set")}</td><td class="num">${esc(p.tp ?? "not set")}</td><td>${esc(p.reason ?? "—")}</td></tr>`).join("");
  const links = t.links.map((l) => `<tr data-link="${esc(l.id)}"><td>${esc(l.symbol)} · ${esc(l.timeframe)}</td><td>${esc(pretty(l.analysis_outcome))}${l.t6_rule ? ` · ${esc(l.t6_rule)}` : ""}</td><td>${esc(l.as_of_utc ? stampSeconds(l.as_of_utc) : "—")}</td><td>${esc(l.status)}${l.voided_reason ? ` · ${esc(l.voided_reason)}` : ""}</td>
      <td>${open && l.status === "ACTIVE" ? `<div class="trade-inline" data-trade-form="void-link-${esc(l.id)}"><input class="text-input" name="reason" placeholder="Reason to unlink" aria-label="Reason to unlink this decision"><button class="button small" type="button" data-trade-action="void-link" data-link-id="${esc(l.id)}">Unlink</button></div>` : ""}</td></tr>`).join("");
  const notes = t.notes.map((n) => `<div class="info-row"><span>${esc(stampSeconds(n.created_utc))}</span><strong class="trade-note">${esc(n.text)}</strong></div>`).join("");
  const summary = [
    ["Trade id", t.id], ["Source", t.execution_source], ["Recorded", stampSeconds(t.created_utc)], ["Strategy tag", t.strategy_tag],
    ["Instrument specification", t.specification],
    ["Protection", `${pretty(t.protection.state)} · SL ${t.protection.stop_loss ?? "not set"} · TP ${t.protection.take_profit ?? "not set"}`, "trade-protection"],
    ...(t.voided_utc ? [["Voided", `${stampSeconds(t.voided_utc)} · ${t.voided_reason}`, "trade-voided"]] : []),
  ];
  return `<div id="trade-detail" data-trade-id="${esc(t.id)}">
    <section class="panel"><div class="panel-head"><span class="panel-title">${esc(t.symbol)} · ${esc(t.direction)}</span><span class="panel-meta">${tradeChip(t.status, "status")} ${tradeChip(t.verification, "verification")}</span></div>${tradeRows(summary)}</section>
    ${accountingMarkup(t)}
    <section class="panel"><div class="panel-head"><span class="panel-title">Fills</span><span class="panel-meta">VOID-ONLY · CORRECT BY VOIDING</span></div>
      <div class="table-scroll"><table class="data-table" id="trade-fills"><thead><tr><th>Time</th><th>Kind</th><th class="num">Price</th><th class="num">Lots</th><th class="num">Commission</th><th class="num">Swap</th><th class="num">Fees</th><th class="num">MT5 P&amp;L</th><th>Status</th><th></th></tr></thead><tbody>${fills}</tbody></table></div>
      ${open ? `<div class="trade-form" data-trade-form="fill"><label class="trade-field"><span>Kind</span><select class="select" name="kind"><option value="exit">exit (partial or full close)</option><option value="entry">entry (add to the position)</option></select></label>${fieldInput("time_utc", "Time (UTC)", { type: "datetime-local", value: nowUtcInput(), required: true })}${fieldInput("price", "Price", { required: true })}${fieldInput("volume", "Volume (lots)", { required: true, step: "0.01" })}${costInputs(true)}<div class="trade-actions"><button class="button" type="button" data-trade-action="fill">Add fill</button></div></div>` : ""}</section>
    <section class="panel"><div class="panel-head"><span class="panel-title">Protection history</span><span class="panel-meta">APPEND-ONLY</span></div>
      <div class="table-scroll"><table class="data-table" id="trade-protection-history"><thead><tr><th>Effective</th><th class="num">Stop-loss</th><th class="num">Take-profit</th><th>Reason</th></tr></thead><tbody>${protection}</tbody></table></div>
      ${open ? `<div class="trade-form" data-trade-form="protection">${fieldInput("stop_loss", "Stop-loss", { placeholder: "not set if blank" })}${fieldInput("take_profit", "Take-profit", { placeholder: "not set if blank" })}${fieldInput("effective_utc", "Effective (UTC)", { type: "datetime-local", value: nowUtcInput(), required: true })}${fieldInput("reason", "Reason", { type: "text" })}<div class="trade-actions"><button class="button" type="button" data-trade-action="protection">Record protection change</button></div></div>` : ""}</section>
    <section class="panel"><div class="panel-head"><span class="panel-title">Notes</span><span class="panel-meta">APPEND-ONLY</span></div><div class="info-list" id="trade-notes">${notes || '<div class="info-row"><span>No notes</span></div>'}</div>
      ${open ? `<div class="trade-form" data-trade-form="note">${fieldInput("text", "Note", { type: "text", required: true })}<div class="trade-actions"><button class="button" type="button" data-trade-action="note">Add note</button></div></div>` : ""}</section>
    <section class="panel"><div class="panel-head"><span class="panel-title">Linked decisions</span><span class="panel-meta">LINK FROM THE DECISIONS PAGE</span></div>
      ${t.links.length ? `<div class="table-scroll"><table class="data-table" id="trade-links"><thead><tr><th>Decision</th><th>Outcome</th><th>As of</th><th>Status</th><th></th></tr></thead><tbody>${links}</tbody></table></div>` : emptyState("No linked decisions", "Use “Link to trade” on the Decisions page.")}</section>
    ${open ? `<section class="panel"><div class="panel-head"><span class="panel-title">Void this record</span><span class="panel-meta">KEEPS THE HISTORY · NOTHING IS DELETED</span></div><div class="trade-form" data-trade-form="void-trade">${fieldInput("reason", "Reason", { type: "text", required: true })}<div class="trade-actions"><button class="button" type="button" data-trade-action="void-trade">Void trade record</button></div></div></section>` : ""}
  </div>`;
}

export function tradesContent() {
  return `${state.trades.message ? `<div class="risk-stale" id="trades-message" role="status">${esc(state.trades.message)}</div>` : ""}
    ${recordFormMarkup()}${tradeListMarkup()}${tradeDetailMarkup()}`;
}

export function tradesJournalMarkup() {
  const journal = state.trades.list?.journal;
  return journal ? `Journal: ${esc(pretty(journal.state))} · schema v${esc(journal.schema_version)} · monitoring and reconciliation ${esc(pretty(journal.monitoring_and_reconciliation))}` : "";
}

export function tradesPage() {
  return `${pageHeading("Trade Journal", "Manual records of trades executed in MT5 · not broker-verified", '<button class="button" type="button" id="trades-refresh">Refresh</button>')}
    <div class="risk-banner" id="trades-banner">${esc(TRADE_BANNER)}</div>
    <div class="panel-note account-note" id="trades-journal">${tradesJournalMarkup()}</div>
    <div id="trades-content">${tradesContent()}</div>`;
}

export function renderTrades() {
  if (state.page !== "history") return;
  setInner(document.querySelector("#trades-journal"), tradesJournalMarkup());
  setInner(document.querySelector("#trades-content"), tradesContent());
}

export async function loadTrades() {
  const seq = ++state.trades.seq;
  const selected = state.trades.selected;
  const [list, detail] = await Promise.all([
    tradeGet("/api/trades?limit=100"),
    selected ? tradeGet(`/api/trades/${encodeURIComponent(selected)}`) : Promise.resolve(null),
  ]);
  if (seq !== state.trades.seq) return;
  if (list.error || !Array.isArray(list.payload?.trades)) state.trades.error = list.error ?? malformed();
  else {
    state.trades.list = list.payload;
    state.trades.error = null;
  }
  if (detail) {
    if (detail.error || !detail.payload?.trade) {
      state.trades.detailError = detail.error ?? malformed();
      state.trades.detail = null;
    } else {
      state.trades.detail = detail.payload.trade;
      state.trades.detailError = null;
    }
  }
  renderTrades();
}

export function formValues(form) {
  const values = {};
  form.querySelectorAll("input, select, textarea").forEach((input) => { values[input.name] = input.value.trim(); });
  return values;
}

export function optionalNumber(values, name, label) {
  const text = values[name];
  if (text === undefined || text === "") return null;
  const value = Number(text);
  if (!Number.isFinite(value)) throw new Error(`${label} must be a number`);
  return value;
}

// datetime-local values are entered in UTC (labelled so) and sent with an explicit Z.
export const utcValue = (text) => (text ? `${text.length === 16 ? `${text}:00` : text}Z` : null);

export function fillPayload(values, kind) {
  const body = {
    time_utc: utcValue(values.time_utc),
    price: optionalNumber(values, "price", "Price"),
    volume: optionalNumber(values, "volume", "Volume"),
    commission: optionalNumber(values, "commission", "Commission"),
    swap: optionalNumber(values, "swap", "Swap"),
    fee: optionalNumber(values, "fee", "Other fees"),
  };
  if (kind === "exit") body.realised_pnl = optionalNumber(values, "realised_pnl", "Realised P&L");
  return body;
}

// POST with a request id that is kept until the outcome is known: a retry after a timeout or
// network failure reuses it, so the backend returns the original result instead of a duplicate.
export async function tradeWrite(key, path, body) {
  if (state.trades.busy) return null;
  const requestId = state.trades.pending[key] ?? newRequestId();
  state.trades.pending[key] = requestId;
  state.trades.busy = true;
  try {
    const response = await fetch(path, { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: requestId, ...body }) });
    let payload = null;
    try { payload = await response.json(); } catch { payload = null; }
    if (response.status < 500) delete state.trades.pending[key]; // the outcome is known
    if (!response.ok) {
      state.trades.message = `Not recorded: ${payload?.message || payload?.reason || payload?.error || `backend returned ${response.status}`}`;
      return null;
    }
    return payload;
  } catch {
    state.trades.message = "Outcome unknown: the backend did not answer. Retrying the same action reuses its request id, so it cannot be recorded twice.";
    return null;
  } finally {
    state.trades.busy = false;
  }
}

export function tradeRequest(action, button, values) {
  const tradeId = state.trades.selected;
  const base = tradeId ? `/api/trades/${encodeURIComponent(tradeId)}` : "";
  if (action === "create") {
    return ["create", "/api/trades", {
      symbol: values.symbol, direction: values.direction, entry: fillPayload(values, "entry"),
      stop_loss: optionalNumber(values, "stop_loss", "Stop-loss"), take_profit: optionalNumber(values, "take_profit", "Take-profit"),
      strategy_tag: values.strategy_tag || null, note: values.note || null,
    }];
  }
  if (action === "fill") return [`fill-${tradeId}`, `${base}/fills`, { kind: values.kind, ...fillPayload(values, values.kind) }];
  if (action === "void-fill") return [`void-fill-${button.dataset.fillId}`, `${base}/fills/${encodeURIComponent(button.dataset.fillId)}/void`, { reason: values.reason }];
  if (action === "protection") {
    return [`protection-${tradeId}`, `${base}/protection`, {
      stop_loss: optionalNumber(values, "stop_loss", "Stop-loss"), take_profit: optionalNumber(values, "take_profit", "Take-profit"),
      effective_utc: utcValue(values.effective_utc), reason: values.reason || null,
    }];
  }
  if (action === "note") return [`note-${tradeId}`, `${base}/notes`, { text: values.text }];
  if (action === "void-link") return [`void-link-${button.dataset.linkId}`, `${base}/links/${encodeURIComponent(button.dataset.linkId)}/void`, { reason: values.reason }];
  if (action === "void-trade") return [`void-trade-${tradeId}`, `${base}/void`, { reason: values.reason }];
  return null;
}

export async function tradeAction(button) {
  const form = button.closest("[data-trade-form]");
  let request;
  try {
    request = tradeRequest(button.dataset.tradeAction, button, form ? formValues(form) : {});
  } catch (error) {
    state.trades.message = `Not recorded: ${error.message}`;
    renderTrades();
    return;
  }
  if (!request) return;
  const result = await tradeWrite(...request);
  if (result) {
    state.trades.message = "Recorded.";
    if (result.trade_id) state.trades.selected = result.trade_id;
    await loadTrades();
  } else {
    renderTrades();
  }
}

// Decisions page: link a decision to a trade the user already recorded (never creates one).
export function decisionLinkMarkup() {
  const link = state.trades.link;
  if (!link) return "";
  const trades = (state.trades.linkTrades ?? []).filter((t) => t.status !== "VOIDED");
  const choices = trades.map((t) => `<option value="${esc(t.id)}">${esc(t.symbol)} ${esc(t.direction)} · opened ${esc(t.first_entry_utc ? stampSeconds(t.first_entry_utc) : "—")} · ${esc(pretty(t.status))}</option>`).join("");
  const body = state.trades.linkTrades === null
    ? emptyState("Loading", "Loading recorded trades.")
    : trades.length
      ? `<div class="trade-form" data-trade-form="link"><label class="trade-field wide"><span>Recorded trade</span><select class="select" name="trade_id">${choices}</select></label><div class="trade-actions"><button class="button primary" type="button" data-link-confirm>Link</button><button class="button" type="button" data-link-cancel>Cancel</button></div></div>`
      : `${emptyState("No recorded trades for this symbol", "Record the trade on Trade History first. A decision never becomes a trade automatically.")}<div class="trade-actions"><button class="button" type="button" data-link-cancel>Close</button></div>`;
  return `<section class="panel" id="decision-link-panel"><div class="panel-head"><span class="panel-title">Link decision to a recorded trade</span><span class="panel-meta">${esc(link.label)}</span></div>${body}
    ${link.message ? `<div class="panel-note account-note" role="status" id="decision-link-message">${esc(link.message)}</div>` : ""}</section>`;
}

export function renderDecisionLink() {
  if (state.page !== "signals") return;
  setInner(document.querySelector("#decision-link"), decisionLinkMarkup());
}

export async function openDecisionLink(button) {
  const decisionId = button.dataset.linkDecision;
  state.trades.link = { decisionId, symbol: button.dataset.linkSymbol, label: button.dataset.linkLabel, message: "" };
  state.trades.linkTrades = null;
  renderDecisionLink();
  const result = await tradeGet(`/api/trades?limit=100&symbol=${encodeURIComponent(button.dataset.linkSymbol)}`);
  if (state.trades.link?.decisionId !== decisionId) return;
  if (result.error || !Array.isArray(result.payload?.trades)) {
    state.trades.linkTrades = [];
    state.trades.link.message = `Recorded trades unavailable: ${(result.error ?? malformed()).message}`;
  } else {
    state.trades.linkTrades = result.payload.trades;
  }
  renderDecisionLink();
}

export async function confirmDecisionLink() {
  const link = state.trades.link;
  const select = document.querySelector('#decision-link-panel select[name="trade_id"]');
  if (!link || !select?.value) return;
  const result = await tradeWrite(`link-${link.decisionId}-${select.value}`, `/api/trades/${encodeURIComponent(select.value)}/links`, { decision_id: link.decisionId });
  if (result) {
    state.trades.link = null;
    renderDecisionLink();
    hooks.livePolling.decisions.now();
  } else {
    link.message = state.trades.message;
    state.trades.message = "";
    renderDecisionLink();
  }
}
