import { hooks } from "./hooks.js";
import { MAX_WATCHLIST } from "./state.js";
import { ageText, clockTime, esc, prefs, price, reasonText, setInner, stamp, stat, stateLabel, statusBadge, tz } from "./format.js";
import { connection, marketStatus, quoteFor, quoteView, quotesProblemText } from "./status.js";

// ---------------------------------------------------------------- live quotes
export function quoteStripMarkup() {
  const symbol = prefs().selected_currency_pair;
  const quote = quoteFor(symbol);
  const ok = quote && quote.status !== "UNAVAILABLE";
  return `<div class="stats-strip quote-strip">
    ${stat("Instrument", quote?.broker_symbol && quote.broker_symbol !== symbol ? `${symbol} (${quote.broker_symbol})` : symbol || "—")}
    ${stat("Bid", ok ? price(quote.bid, quote.digits) : "—", !ok)}
    ${stat("Ask", ok ? price(quote.ask, quote.digits) : "—", !ok)}
    ${stat("Spread", ok && quote.spread_pips != null ? `${Number(quote.spread_pips).toFixed(1)} pips` : "—", !ok)}
    ${stat("Last quote", ok ? clockTime(quote.quote_time_utc) : "—", !ok)}
    ${stat("Age", ok ? ageText(quote.quote_time_utc) : "—", !ok)}
    ${stat("Status", statusBadge(quote?.status ?? marketStatus(), quote?.status === "UNAVAILABLE" || quote?.status === "CACHED" ? reasonText(quote.reason) : quotesProblemText()), false, true)}
  </div>`;
}

export function watchlistRows(full) {
  const symbols = prefs().currency_pair_watchlist ?? [];
  const selected = prefs().selected_currency_pair;
  if (!symbols.length) return `<tr><td class="table-empty" colspan="${full ? 8 : 6}">No pairs in the watchlist.</td></tr>`;
  return symbols.map((symbol) => {
    const quote = quoteFor(symbol);
    const ok = quote && quote.status !== "UNAVAILABLE";
    const view = quoteView();
    const status = quote?.status ?? (view ? "UNAVAILABLE" : "…");
    const reason = quote ? (quote.status === "LIVE" ? "" : reasonText(quote.reason)) : quotesProblemText();
    return `<tr class="${symbol === selected ? "selected-row" : ""}">
      <td class="watch-symbol" data-select-symbol="${esc(symbol)}" title="${esc(quote?.broker_symbol ? `Broker symbol: ${quote.broker_symbol}` : "")}">${esc(symbol)}</td>
      <td class="num">${ok ? price(quote.bid, quote.digits) : "—"}</td>
      <td class="num">${ok ? price(quote.ask, quote.digits) : "—"}</td>
      <td class="num">${ok && quote.spread_pips != null ? Number(quote.spread_pips).toFixed(1) : "—"}</td>
      ${full ? `<td class="num">${ok ? esc(clockTime(quote.quote_time_utc)) : "—"}</td>` : ""}
      <td class="num">${ok ? esc(ageText(quote.quote_time_utc)) : "—"}</td>
      <td title="${esc(reason)}">${status === "…" ? '<span class="panel-meta">…</span>' : statusBadge(status)}</td>
      ${full ? `<td><button class="remove-pair" data-remove-symbol="${esc(symbol)}" aria-label="Remove ${esc(symbol)}">Remove</button></td>` : ""}
    </tr>`;
  }).join("");
}

export function watchlistMarkup(full = false) {
  return `<div class="table-scroll"><table class="watchlist-table live-watchlist"><thead><tr><th>Pair</th><th class="num">Bid</th><th class="num">Ask</th><th class="num">Spread</th>${full ? '<th class="num">Updated</th>' : ""}<th class="num">Age</th><th>Status</th>${full ? "<th></th>" : ""}</tr></thead><tbody id="watchlist-body" data-full="${full ? "1" : "0"}">${watchlistRows(full)}</tbody></table></div>${full ? `<div class="watchlist-add"><input id="watchlist-input" class="text-input" placeholder="Add pair, e.g. EURGBP" aria-label="Currency pair"><button class="button" id="add-pair">Add pair</button><span class="panel-meta">${(prefs().currency_pair_watchlist ?? []).length} / ${MAX_WATCHLIST}</span></div><div id="save-message" class="save-message" aria-live="polite"></div>` : ""}`;
}

// The server-clock check describes the current connection only; history is labelled as such.
export function clockCheckText(check) {
  const last = check.last_verified_utc ? ` · last verified ${stamp(check.last_verified_utc)}` : "";
  switch (check.result) {
    case "verified": return `Verified against a live tick (${ageText(check.checked_at_utc)} ago)`;
    case "stale_ticks": return `Not verifiable now: no recent ticks (market closed or quiet)${last}`;
    case "unavailable": return `Not verified now: ${reasonText(check.reason) || "MT5 unavailable"}${last}`;
    case "mismatch": return "MISMATCH: timestamps ahead of UTC";
    default: return "Not checked yet";
  }
}

export function marketInfoMarkup() {
  const conn = connection();
  const server = conn.server;
  const check = conn.time_check ?? {};
  const checkText = clockCheckText(check);
  const rows = [
    ["Source", server?.server ? `MT5 · ${server.server}` : "MT5"],
    ["Broker", server?.company ?? "—"],
    ["Account type", server?.account_type ?? "—"],
    ["Connection", conn.state === "connected" ? "Connected" : conn.message || reasonText(conn.unavailable_reason) || stateLabel(conn.state)[0]],
    ["Server clock", `${conn.server_clock ?? "UTC"} · ${checkText}`],
    ["Last MT5 response", conn.last_success_utc ? stamp(conn.last_success_utc) : "—"],
    ["Refresh", "On demand · every 2 s while open"],
    ["Display timezone", tz()],
  ];
  return `<div class="info-list" id="market-info">${rows.map(([label, value]) => `<div class="info-row"><span>${esc(label)}</span><strong class="${label === "Server clock" && check.result === "mismatch" ? "error-state" : ""}">${esc(value)}</strong></div>`).join("")}</div>`;
}

export function replaceOuter(selector, markup) {
  const element = document.querySelector(selector);
  if (element && element.__markup !== markup) {
    element.outerHTML = markup;
    const replacement = document.querySelector(selector);
    if (replacement) replacement.__markup = markup;
  }
}

export function renderLive() {
  hooks.renderTopbar();
  replaceOuter(".quote-strip", quoteStripMarkup());
  const body = document.querySelector("#watchlist-body");
  if (body) setInner(body, watchlistRows(body.dataset.full === "1"));
  replaceOuter("#market-info", marketInfoMarkup());
  setInner(document.querySelector("#market-heading-badge"), statusBadge(marketStatus()));
  hooks.renderChartArea(); // the chart's live line and time-base notice follow the quotes
}
