import { ANALYSIS_TIMEFRAMES, POLL, state } from "./state.js";
import { emptyState, esc, pageHeading, prefs, reasonText, setInner, stamp, stampSeconds } from "./format.js";
import { fetchJson, malformed } from "./api.js";

// ---------------------------------------------------------------- risk management (Phase 4)
// Proposed trade parameters from /api/risk/proposal for the selected pair and timeframe.
// Read-only: there are no order, buy or sell controls, and nothing here implies execution.
export const RISK_PAIRS = ["EURUSD", "GBPUSD", "USDJPY"]; // the backend's supported instruments
export const RISK_EXPIRE_MS = 65000;
export const RISK_STATUS = {
  APPROVED_FOR_MANUAL_REVIEW: ["Approved for manual review", "verified"],
  REJECTED: ["Rejected", "invalid"],
  UNVERIFIED: ["Unverified", "unverified"],
};
export const RISK_AREAS = [["data", "Data & setup"], ["construction", "Entry, SL & TP"], ["per_trade_risk", "Per-trade risk"], ["daily_risk", "Daily risk"], ["broker", "Broker"], ["margin", "Margin"], ["costs", "Costs (net 1:2)"]];

export const riskKey = () => `${prefs().selected_currency_pair}|${prefs().selected_chart_timeframe}`;
export function riskProblem() {
  if (!RISK_PAIRS.includes(prefs().selected_currency_pair)) return `Proposals are available for ${RISK_PAIRS.join(", ")}; the selected pair is ${prefs().selected_currency_pair || "none"}.`;
  if (!ANALYSIS_TIMEFRAMES.includes(prefs().selected_chart_timeframe)) return `Proposals use the T6 analysis timeframes ${ANALYSIS_TIMEFRAMES.join(", ")}; the selected timeframe is ${prefs().selected_chart_timeframe}.`;
  return null;
}
export const riskActive = () => state.page === "risk" && !riskProblem() && !state.error;
export const riskExpired = () => !state.risk.receivedAt || Date.now() - state.risk.receivedAt > RISK_EXPIRE_MS;
export const riskInterval = () => POLL.riskMs;
export const num = (value, digits = 2) => (typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "—");
// Net reward:risk is "estimated" (every cost incl. a slippage bound established) or "unverified".
export function netText(net) {
  if (!net) return "—";
  if (net.status === "estimated" && net.value != null) return `1 : ${num(net.value)} · estimated (commission, swap, slippage bound)`;
  return "Unverified · costs not established";
}
// A volume is shown as proposed only when sizing is verified (costs included).
export function volumeText(sizing) {
  if (sizing.volume != null) return `${plainNum(sizing.volume)} lots (includes transaction costs)`;
  const ill = sizing.illustrative_volume_price_risk_only;
  if (ill && ill.value != null) return `No actionable volume · ${plainNum(ill.value)} lots illustrative (UNVERIFIED, price risk only)`;
  return "—";
}
export const plainNum = (value) => (typeof value === "number" && Number.isFinite(value) ? String(value) : "—");

export function riskStatusChip(status) {
  const [label, tone] = RISK_STATUS[status] ?? ["Unavailable", "unavailable"];
  return `<span class="field-chip validation ${tone}" id="risk-status-chip" data-risk-status="${esc(status ?? "none")}">${esc(label)}</span>`;
}
export function riskRows(rows) {
  return `<div class="info-list">${rows.map(([label, value, key]) => `<div class="info-row"${key ? ` data-risk-field="${esc(key)}"` : ""}><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("")}</div>`;
}

export function riskContent() {
  const problem = riskProblem();
  if (problem) return `<section class="panel">${emptyState("No proposal for this selection", problem)}</section>`;
  const { payload, error, key } = state.risk;
  const data = key === riskKey() ? payload : null;
  if (!data) {
    if (error) return `<section class="panel">${emptyState("Proposal unavailable", error.message || reasonText(error.reason), "error-state")}</section>`;
    return `<section class="panel">${emptyState("Evaluating", "Waiting for the first risk evaluation from the backend.")}</section>`;
  }
  const stale = Boolean(error) || riskExpired();
  const setup = data.setup ?? {};
  const p = data.proposal;
  const sizing = data.sizing ?? {};
  const daily = data.daily_risk ?? {};
  const margin = data.margin ?? {};
  const cur = sizing.currency ?? "USC";
  const freshness = stale
    ? `<div class="risk-stale" id="risk-stale">Not current: evaluated ${esc(stampSeconds(data.evaluated_at_utc))}${error ? ` · ${esc(error.message || reasonText(error.reason))}` : ""}. Do not use these parameters.</div>`
    : "";
  const reasons = (list, cls) => list.length ? `<ul class="risk-list ${cls}">${list.map((item) => `<li><strong>${esc(item.area.replaceAll("_", " "))}</strong> ${esc(item.reason)}</li>`).join("")}</ul>` : "";
  const setupRows = [
    ["Setup", setup.setup ? `${setup.setup.replaceAll("_", " ")}${setup.direction ? ` · ${setup.direction}` : ""}` : "—", "setup"],
    ["T6 decision", setup.t6_outcome ? `${setup.t6_outcome} · rule ${setup.t6_rule}` : "—", "t6"],
    ["Snapshot", setup.snapshot_id ?? "—"],
    ["T6 evaluated", stampSeconds(setup.evaluated_at_utc)],
    ["Risk evaluated", stampSeconds(data.evaluated_at_utc)],
  ];
  const proposalRows = p ? [
    ["Direction (proposed)", data.direction ?? "—", "direction"],
    ["Entry (proposed)", `${plainNum(p.entry_price)} · ${p.entry_side} side`, "entry"],
    ["Entry condition", p.entry_condition, "entry_condition"],
    ["Stop-loss (proposed)", `${plainNum(p.stop_loss)} · ${p.stop_loss_basis}`, "stop_loss"],
    ["Take-profit (proposed)", `${plainNum(p.take_profit)} · ${p.take_profit_basis}`, "take_profit"],
    ["SL / TP distance", `${num(p.stop_loss_distance_points, 0)} / ${num(p.take_profit_distance_points, 0)} points`],
    ["Reward : risk (gross)", p.gross_reward_risk != null ? `1 : ${num(p.gross_reward_risk)} · spread included; excludes commission, swap, slippage` : "—", "gross_reward_risk"],
    ["Reward : risk (net)", netText(data.net_reward_risk), "net_reward_risk"],
    ["Volume (proposed)", volumeText(sizing), "volume"],
    ["Estimated loss at SL (incl. costs)", sizing.estimated_loss != null ? `${num(sizing.estimated_loss)} ${cur} · price ${num(sizing.estimated_price_loss)} + costs ${num(sizing.estimated_costs)}` : "Not established (costs unverified)", "estimated_loss"],
    ["Estimated reward at TP (gross / net)", sizing.estimated_reward_gross != null ? `${num(sizing.estimated_reward_gross)} / ${num(sizing.estimated_reward_net)} ${cur}` : "Not established (costs unverified)", "estimated_reward"],
  ] : [];
  const riskRowsList = [
    ["Reference capital", sizing.equity != null ? `equity ${num(sizing.equity)} ${cur}` : "—"],
    ["Max risk per trade", sizing.max_risk_per_trade != null ? `${num(sizing.max_risk_per_trade)} ${cur} (${num(data.policy.max_risk_per_trade * 100, 1)}%)` : `${num(data.policy.max_risk_per_trade * 100, 1)}% of equity`, "max_risk"],
    ["Sizing status", sizing.status ? `${sizing.status} · ${sizing.includes_costs ? "includes applicable transaction costs" : "transaction costs not established: no actionable volume"}` : "—", "sizing_status"],
    ["Risk budget", sizing.risk_budget != null ? `${num(sizing.risk_budget)} ${cur} (${sizing.risk_budget_basis})` : "—", "risk_budget"],
    ["Price loss per lot (tick / contract)", sizing.loss_per_lot ? `${num(sizing.loss_per_lot.tick_value_method)} / ${num(sizing.loss_per_lot.contract_size_method)} ${cur}` : "—"],
    ["Costs per lot (commission / swap / slippage)", sizing.loss_per_lot ? [sizing.loss_per_lot.commission, sizing.loss_per_lot.swap, sizing.loss_per_lot.slippage].map((value) => (value == null ? "unverified" : num(value, 4))).join(" / ") : "—", "costs_per_lot"],
    ["Total loss per lot (price + costs)", sizing.loss_per_lot?.total != null ? `${num(sizing.loss_per_lot.total, 4)} ${cur}` : "Not established", "total_per_lot"],
    ["Volume (theoretical → rounded down)", sizing.theoretical_volume != null ? `${num(sizing.theoretical_volume, 4)} → ${sizing.volume ?? "below minimum"}` : "—"],
    ["Broker volume", sizing.broker_volume ? `min ${sizing.broker_volume.min} · max ${sizing.broker_volume.max} · step ${sizing.broker_volume.step} · contract ${sizing.broker_volume.contract_size}` : "—"],
    ["MT5 calculator (loss / reward)", sizing.mt5_calculator ? `${num(sizing.mt5_calculator.loss_at_stop)} / ${num(sizing.mt5_calculator.reward_at_target)} ${cur}` : "—"],
  ];
  const dailyRows = [
    ["Status", daily.status ?? "—", "daily_status"],
    ["Reference capital", daily.reference_capital ? `${num(daily.reference_capital.value)} ${cur} · min(balance, equity) = ${daily.reference_capital.from}` : "Unverified", "daily_reference"],
    ["Trading day (broker server)", daily.trading_day ? `${stamp(daily.trading_day.start_utc)} → ${stamp(daily.trading_day.end_utc)}` : "—"],
    ["Daily limit", daily.limit != null ? `${num(daily.limit)} ${cur} (${num(data.policy.max_daily_risk * 100, 1)}%)` : "—", "daily_limit"],
    ["Realised losses today", daily.realised_losses != null ? `${num(daily.realised_losses)} ${cur}` : "Unverified"],
    ["Open position risk", daily.open_position_risk != null ? `${num(daily.open_position_risk)} ${cur} (${daily.positions} open)` : "Unverified"],
    ["Pending order risk", daily.pending_order_risk != null ? `${num(daily.pending_order_risk)} ${cur} (${daily.pending_orders} pending)` : "Unverified"],
    ["Remaining allowance", daily.remaining != null ? `${num(daily.remaining)} ${cur}` : "Unverified", "daily_remaining"],
  ];
  const marginRows = [
    ["MT5 margin calculator", margin.mt5_calculator != null ? `${num(margin.mt5_calculator, 4)} ${cur}` : "—"],
    ["Formula (contract × volume × rate / leverage)", margin.formula != null ? `${num(margin.formula, 4)} ${cur}` : "—"],
    ["Free margin after", margin.free_margin_after != null ? `${num(margin.free_margin_after)} ${cur}` : "—", "free_margin_after"],
    ["Margin level after", margin.margin_level_after_percent != null ? `${num(margin.margin_level_after_percent, 0)} %` : "—"],
  ];
  const validations = RISK_AREAS.map(([areaKey, label]) => {
    const v = data.validations?.[areaKey] ?? { status: "not_evaluated", checks: [] };
    return `<div class="info-row" data-validation="${areaKey}"><span>${esc(label)}</span><strong class="check-${v.status === "passed" ? "consistent" : v.status === "failed" ? "discrepancy" : "not_checkable"}">${esc(v.status.replaceAll("_", " "))}${v.checks.length ? `<small> · ${esc(v.checks.map((c) => c.detail).join("; "))}</small>` : ""}</strong></div>`;
  }).join("");
  const costs = data.costs ? riskRows([
    ["Spread", `${data.costs.spread.points ?? "—"} points · ${data.costs.spread.treatment}`],
    ["Commission", `${data.costs.commission.status} · ${data.costs.commission.treatment}${data.costs.commission.per_lot_round_trip != null ? ` (${data.costs.commission.per_lot_round_trip} ${cur}/lot round trip; ${data.costs.commission.source})` : ""}`, "cost_commission"],
    ["Swap", `${data.costs.swap.status} · ${data.costs.swap.summary} (mode ${data.costs.swap.mode ?? "—"}; long ${plainNum(data.costs.swap.long_rate)}, short ${plainNum(data.costs.swap.short_rate)}; triple on ${data.costs.swap.triple_swap_day ?? "—"})`, "cost_swap"],
    ["Slippage", `${data.costs.slippage.status} · ${data.costs.slippage.treatment}`, "cost_slippage"],
    ["Net assumptions", (data.net_reward_risk?.assumptions ?? []).join(" · ")],
  ]) : "";
  return `${freshness}
    <div class="risk-banner" id="risk-banner">${esc(data.note)}</div>
    ${data.net_reward_risk?.status === "unverified" ? `<div class="risk-stale" id="risk-net-unverified">Net costs unverified: the 1:2 reward:risk requirement after costs is NOT established${data.costs?.slippage?.status === "unmodelled" ? " because slippage is unmodelled (no verified estimate or bound)" : ""}${data.costs?.commission?.status === "unverified" ? "; no verified commission rate" : ""}. The gross ratio alone does not qualify this proposal, and no volume is verified against the 0.5% limit.</div>` : ""}
    ${data.rejection_reasons.length ? `<section class="panel"><div class="panel-head"><span class="panel-title">Rejection reasons</span><span class="panel-meta">${data.rejection_reasons.length}</span></div>${reasons(data.rejection_reasons, "rejections")}</section>` : ""}
    ${data.unverified.length ? `<section class="panel"><div class="panel-head"><span class="panel-title">Unverified</span><span class="panel-meta">BLOCKS APPROVAL</span></div>${reasons(data.unverified, "unverified-list")}</section>` : ""}
    <div class="account-grid">
      <section class="panel"><div class="panel-head"><span class="panel-title">T6 technical setup</span><span class="panel-meta">${esc(data.symbol)}${data.broker_symbol ? ` · ${esc(data.broker_symbol)}` : ""} · ${esc(data.timeframe)}</span></div><div id="risk-setup">${riskRows(setupRows)}</div></section>
      <section class="panel"><div class="panel-head"><span class="panel-title">Proposed trade parameters</span><span class="panel-meta">NOT AN ORDER</span></div><div id="risk-proposal">${p ? riskRows(proposalRows) : emptyState("No proposed parameters", "No trade was constructed; see the reasons above.")}</div></section>
      <section class="panel"><div class="panel-head"><span class="panel-title">Daily risk</span><span class="panel-meta">${esc(String(daily.status ?? "—").toUpperCase())}</span></div><div id="risk-daily">${riskRows(dailyRows)}</div></section>
    </div>
    <div class="account-grid">
      <section class="panel"><div class="panel-head"><span class="panel-title">Position sizing</span><span class="panel-meta">${esc(cur)}</span></div><div id="risk-sizing">${riskRows(riskRowsList)}</div></section>
      <section class="panel"><div class="panel-head"><span class="panel-title">Margin</span><span class="panel-meta">READ-ONLY CALCULATION</span></div><div id="risk-margin">${riskRows(marginRows)}</div></section>
      <section class="panel"><div class="panel-head"><span class="panel-title">Validations</span><span class="panel-meta">ALL MUST PASS</span></div><div class="info-list" id="risk-validations">${validations}</div></section>
    </div>
    ${costs ? `<section class="panel"><div class="panel-head"><span class="panel-title">Costs &amp; assumptions</span></div><div id="risk-costs">${costs}</div></section>` : ""}
    ${data.warnings.length ? `<div class="panel-note account-note" id="risk-warnings">${data.warnings.map(esc).join(" · ")}</div>` : ""}`;
}

export function riskPage() {
  const status = state.risk.key === riskKey() ? state.risk.payload?.status : null;
  return `${pageHeading("Risk Management", "Proposed trade parameters for manual review · read-only · no order execution", `<span id="risk-heading-badge">${riskStatusChip(status)}</span>`)}<div id="risk-content">${riskContent()}</div>`;
}

export function renderRisk() {
  if (state.page !== "risk") return;
  const status = state.risk.key === riskKey() ? state.risk.payload?.status : null;
  setInner(document.querySelector("#risk-heading-badge"), riskStatusChip(status));
  setInner(document.querySelector("#risk-content"), riskContent());
}

export async function refreshRisk(poller) {
  const key = riskKey();
  const [symbol, timeframe] = key.split("|");
  const seq = ++state.risk.seq;
  const query = new URLSearchParams({ symbol, timeframe });
  const result = await fetchJson(`/api/risk/proposal?${query}`, poller, POLL.riskTimeoutMs);
  if (result.aborted || seq !== state.risk.seq || key !== riskKey()) return "aborted";
  const payload = result.payload;
  const error = result.error ?? (typeof payload?.status === "string" && payload?.symbol === symbol && Array.isArray(payload?.rejection_reasons) ? null : malformed());
  if (error) {
    state.risk.error = error;
  } else {
    state.risk.payload = payload;
    state.risk.key = key;
    state.risk.receivedAt = Date.now();
    state.risk.error = null;
  }
  renderRisk();
  return !error;
}
