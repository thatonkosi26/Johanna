import { hooks } from "./hooks.js";
import { POLL, state } from "./state.js";
import { emptyState, esc, pageHeading, pretty, reasonText, setInner, stampSeconds } from "./format.js";
import { fetchJson, malformed } from "./api.js";

// ---------------------------------------------------------------- decisions journal (P5.1)
// The #signals route shows the decision journal (label "Decisions"). Read-only: the five
// dimensions are shown separately, a candidate is never shown as approved or executed, and the
// request-time coverage disclosure is always visible.
export const DECISIONS_EXPIRE_MS = 65000;
export const decisionsActive = () => state.page === "signals" && !state.error;
export const decisionsInterval = () => POLL.decisionsMs;
export const decisionsExpired = () => !state.decisions.receivedAt || Date.now() - state.decisions.receivedAt > DECISIONS_EXPIRE_MS;
export const DIMENSION_TONE = { APPROVED: "verified", REJECTED: "invalid", UNVERIFIED: "unverified", NOT_EVALUATED: "not_applicable", NOT_APPROVED: "not_applicable", NONE: "not_applicable" };

export function dimensionChip(value, field) {
  return `<span class="field-chip ${DIMENSION_TONE[value] ?? ""}" data-dimension="${esc(field)}">${esc(pretty(value))}</span>`;
}

export function journalStatusText(journal) {
  if (!journal) return "—";
  // A verified key is a prerequisite only.
  const hmac = journal.hmac_prerequisite === "verified"
    ? "HMAC prerequisite verified"
    : `HMAC prerequisite refused (key ${pretty(journal.key_status)})`;
  // B7.2: monitoring/reconciliation state is shown in the System Control Centre from the
  // monitoring service itself (not from the journal, which only stores its records).
  return `${pretty(journal.state)} · schema v${journal.schema_version} · ${hmac} · ${journal.captured ?? 0} captured this session · ${journal.capture_failures ?? 0} capture failures`;
}

export function decisionsContent() {
  const { payload, error } = state.decisions;
  if (!payload) {
    if (error) return `<section class="panel">${emptyState("Decision journal unavailable", `${error.message || reasonText(error.reason)}`, "error-state")}</section>`;
    return `<section class="panel">${emptyState("Loading", "Waiting for the decision journal.")}</section>`;
  }
  const stale = Boolean(error) || decisionsExpired();
  const rows = payload.decisions.map((d) => `<tr data-decision="${esc(d.id)}">
      <td>${esc(stampSeconds(d.last_seen_utc))}</td>
      <td>${esc(d.symbol)} · ${esc(d.timeframe)}</td>
      <td data-col="outcome">${esc(pretty(d.analysis_outcome))}${d.t6_rule ? ` · ${esc(d.t6_rule)}` : ""}</td>
      <td data-col="direction">${esc(d.market_direction)}</td>
      <td data-col="risk">${dimensionChip(d.risk_status, "risk")}</td>
      <td data-col="eligibility">${dimensionChip(d.trade_eligibility, "eligibility")}</td>
      <td data-col="execution">${dimensionChip(d.execution_status, "execution")}</td>
      <td class="num">${esc(d.observations)}</td>
      <td data-col="state">${esc(pretty(d.state))}</td>
      <td><button class="button small" type="button" data-link-decision="${esc(d.id)}" data-link-symbol="${esc(d.broker_symbol || d.symbol)}" data-link-label="${esc(`${d.symbol} · ${d.timeframe} · ${pretty(d.analysis_outcome)}`)}" title="Link this decision to a trade you already recorded">Link to trade</button></td>
    </tr>`).join("");
  return `${stale ? `<div class="risk-stale" id="decisions-stale">Not current: last refreshed ${esc(stampSeconds(new Date(state.decisions.receivedAt).toISOString()))}${error ? ` · ${esc(error.message || reasonText(error.reason))}` : ""}.</div>` : ""}
    <div class="risk-banner" id="decisions-coverage">${esc(payload.coverage?.note ?? "")}</div>
    <div class="panel-note account-note" id="decisions-journal">Journal: ${esc(journalStatusText(payload.journal))}</div>
    <section class="panel"><div class="panel-head"><span class="panel-title">Recorded decisions</span><span class="panel-meta">ANALYSIS · RISK · ELIGIBILITY · EXECUTION (SEPARATE)</span></div>
      ${payload.decisions.length ? `<div class="table-scroll"><table class="data-table decisions-table" id="decisions-table"><thead><tr><th>Last seen</th><th>Market</th><th>Analysis outcome</th><th>Direction</th><th>Risk</th><th>Eligibility</th><th>Execution</th><th class="num">Seen</th><th>State</th><th>Trade link</th></tr></thead><tbody>${rows}</tbody></table></div>`
        : emptyState("No decisions recorded yet", "Decisions are recorded when the application requests an analysis or a risk proposal.")}
    </section>
    <div class="panel-note account-note">A market-analysis candidate is not an approved trade. Eligibility is APPROVED only for a candidate whose latest risk assessment is APPROVED. Execution is MANUALLY RECORDED only while the decision is linked to a trade you recorded on Trade History; linking never creates a trade, and nothing here is broker-verified. Johanna places no orders.</div>`;
}

export function decisionsPage() {
  return `${pageHeading("Decisions", "Recorded market-analysis decisions and risk assessments · read-only")}<div id="decision-link">${hooks.decisionLinkMarkup()}</div><div id="decisions-content">${decisionsContent()}</div>`;
}

export function renderDecisions() {
  if (state.page !== "signals") return;
  setInner(document.querySelector("#decisions-content"), decisionsContent());
}

export async function refreshDecisions(poller) {
  const seq = ++state.decisions.seq;
  const result = await fetchJson("/api/journal/decisions?limit=50", poller, POLL.decisionsTimeoutMs);
  if (result.aborted || seq !== state.decisions.seq) return "aborted";
  const payload = result.payload;
  const error = result.error ?? (Array.isArray(payload?.decisions) && payload?.coverage ? null : malformed());
  if (error) {
    state.decisions.error = error;
  } else {
    state.decisions.payload = payload;
    state.decisions.receivedAt = Date.now();
    state.decisions.error = null;
  }
  renderDecisions();
  return !error;
}
