// B7.2 shared operational components: one fixed status vocabulary and the global status band.
// Every value comes from a backend response already held in `state`; nothing is assumed. A value
// that has not been loaded is NOT CHECKED, a failed request is UNAVAILABLE, and no item ever
// defaults to a positive state.
import { state } from "./state.js";
import { esc, pretty, services, stampSeconds } from "./format.js";
import { marketStatus } from "./status.js";

// Tone classes are deliberately restrained: inactive / disabled / off are expected states and stay
// neutral; amber marks something to look at; red marks missing evidence or an unexpected state.
const TONES = {
  OK: "ok", READY: "ok", ONLINE: "ok", CURRENT: "ok", CONFIGURED: "ok",
  ATTENTION: "attention", DEGRADED: "attention", STALE: "attention", CACHED: "attention",
  "NOT CONFIGURED": "attention", PERMITTED: "attention",
  UNAVAILABLE: "bad", OFFLINE: "bad", ACTIVE: "bad-active",
  ON: "active", INACTIVE: "neutral", DISABLED: "neutral", OFF: "neutral", "NOT PERMITTED": "neutral",
  "NOT AVAILABLE": "neutral", "NOT CHECKED": "neutral", LOADING: "neutral",
};

export function opsChip(value, { tone, field, title } = {}) {
  const text = String(value ?? "UNAVAILABLE");
  const cls = tone ?? TONES[text] ?? "neutral";
  return `<span class="ops-chip ${esc(cls)}"${field ? ` data-ops-field="${esc(field)}"` : ""} data-ops-state="${esc(text)}"${title ? ` title="${esc(title)}"` : ""}>${esc(text)}</span>`;
}

// Readiness check vocabulary (B7.2 correction): OK / ATTENTION / UNAVAILABLE / NOT CHECKED.
export function checkVocabulary(status) {
  if (status === "ok") return "OK";
  if (status === "attention") return "ATTENTION";
  if (status === "unavailable" || status === "error") return "UNAVAILABLE";
  return "NOT CHECKED";
}

const readiness = () => state.ops.readiness;
// Not loaded yet -> NOT CHECKED; request failed, or loaded but the value could not be
// established (e.g. a refused journal reports the scheduler state as null) -> UNAVAILABLE.
const notLoaded = () => (state.ops.error || state.ops.readiness ? "UNAVAILABLE" : "NOT CHECKED");

export function schedulerValue() {
  const enabled = readiness()?.scheduler?.enabled;
  return enabled === true ? "ON" : enabled === false ? "OFF" : notLoaded();
}

export function executionValue() {
  const status = state.data?.status;
  const service = readiness()?.services?.execution;
  if (status?.autonomous_execution_active === true || service?.enabled === true) return "ACTIVE";
  if (status?.autonomous_execution_active === false || service?.enabled === false) return "DISABLED";
  return "UNAVAILABLE";
}

export function unattendedValue() {
  const permitted = readiness()?.unattended_operation_permitted;
  return permitted === false ? "NOT PERMITTED" : permitted === true ? "PERMITTED" : notLoaded();
}

export function costTermsValue() {
  const configured = readiness()?.services?.cost_terms_configured;
  return configured === false ? "NOT CONFIGURED" : configured === true ? "CONFIGURED" : notLoaded();
}

export function journalValue() {
  const journal = services().trading_journal;
  if (!journal) return { value: state.data ? "UNAVAILABLE" : "NOT CHECKED", detail: "not reported" };
  if (journal.state === "ready") {
    return journal.key_status === "verified"
      ? { value: "READY", detail: "key verified" }
      : { value: "ATTENTION", detail: `key ${pretty(journal.key_status)}` };
  }
  if (journal.state === "refused") return { value: "ATTENTION", detail: pretty(journal.reason ?? "refused") };
  return { value: "UNAVAILABLE", detail: pretty(journal.state) };
}

export function schemaValue() {
  const r = readiness();
  const installed = r?.journal?.installed_schema_version;
  const registered = r?.journal?.registered_schema_version ?? services().trading_journal?.schema_version;
  if (installed == null) {
    return { value: registered != null ? `v${registered} registered` : notLoaded(), tone: "neutral", detail: "installed version not checked" };
  }
  const match = installed === registered;
  return {
    value: `v${installed}`,
    tone: match ? "ok" : "attention",
    detail: match ? `installed v${installed} · registered v${registered}` : `installed v${installed} ≠ registered v${registered}`,
  };
}

export function statusItems() {
  const status = state.data?.status;
  let application = state.error ? "OFFLINE" : status ? "OFFLINE" : "LOADING";
  if (status?.state === "running") application = status.health === "ok" ? "ONLINE" : "DEGRADED";
  const market = marketStatus();
  const marketValue = { LIVE: "CURRENT", STALE: "STALE", CACHED: "CACHED", LOADING: "LOADING" }[market] ?? "UNAVAILABLE";
  const journal = journalValue();
  const schema = schemaValue();
  return [
    { key: "application", label: "Application", value: application, detail: status ? `health ${status.health ?? "unknown"}` : state.error ?? "", page: "control" },
    { key: "market", label: "Market data", value: marketValue, detail: pretty(market), page: "markets" },
    { key: "journal", label: "Journal", value: journal.value, detail: journal.detail, page: "control" },
    { key: "schema", label: "Schema", value: schema.value, tone: schema.tone, detail: schema.detail, page: "control" },
    { key: "execution", label: "Execution", value: executionValue(), detail: "no order is ever placed by Johanna", page: "control" },
    { key: "unattended", label: "Unattended operation", value: unattendedValue(), detail: "readiness assessment", page: "control" },
    { key: "scheduler", label: "Scheduler", value: schedulerValue(), detail: pretty(readiness()?.scheduler?.state ?? "not checked"), page: "control" },
    { key: "costs", label: "CostTerms", value: costTermsValue(), detail: "commission, slippage, holding period", page: "evidence" },
  ];
}

export function freshnessLine() {
  const dashboard = state.dataReceivedAt ? stampSeconds(new Date(state.dataReceivedAt).toISOString()) : "—";
  const r = readiness();
  const checked = r?.checked_at_utc ? stampSeconds(r.checked_at_utc) : state.ops.error ? `unavailable (${esc(state.ops.error.message ?? "request failed")})` : "not checked yet";
  return `Status received ${esc(dashboard)} · readiness checked ${checked}`;
}

// The global status band (Overview and System Control Centre). Each cell links to the screen that
// explains it; the band wraps on narrow screens and never covers content.
export function statusBand(id = "status-band") {
  const cells = statusItems().map((item) => `<button type="button" class="status-cell" data-page="${esc(item.page)}" data-status-item="${esc(item.key)}" title="${esc(item.detail ?? "")}">
      <span class="status-cell-label">${esc(item.label)}</span>${opsChip(item.value, { tone: item.tone, field: item.key })}<span class="status-cell-detail">${esc(item.detail ?? "")}</span></button>`).join("");
  return `<section class="status-band" id="${esc(id)}" aria-label="Operational status">${cells}</section>
    <div class="status-band-foot"><span id="${esc(id)}-freshness">${freshnessLine()}</span><button class="text-link" type="button" data-ops-refresh>Refresh status</button></div>`;
}

// Standard panel for a capability that exists but is not activated, or has no API yet.
export function inactivePanel(title, value, text, id = "") {
  return `<section class="panel inactive-panel"${id ? ` id="${esc(id)}"` : ""}><div class="panel-head"><span class="panel-title">${esc(title)}</span>${opsChip(value)}</div><div class="inactive-copy">${text}</div></section>`;
}
