// B7.2 System Control Centre (read-only) and the shared operations snapshot.
// One request feeds the Overview status band, this screen, Positions & Reconciliation and
// Evidence & Costs: GET /api/operations/readiness (diagnostic, no MT5 read, no journal write).
// It is fetched when one of those screens opens or on "Refresh status" -- never polled -- and
// nothing on these screens enables, approves, confirms or restores anything.
import { state } from "./state.js";
import { emptyState, esc, pageHeading, pretty, setInner, stampSeconds } from "./format.js";
import { malformed, tradeGet } from "./api.js";
import {
  checkVocabulary, costTermsValue, executionValue, inactivePanel, journalValue, opsChip,
  schedulerValue, schemaValue, statusBand, unattendedValue,
} from "./components.js";

// Screens that show operations data register a partial re-render here (no full page render, so
// charts and form inputs are not rebuilt when the snapshot arrives).
export const opsViews = {};

export async function loadOps() {
  const seq = ++state.ops.seq;
  const result = await tradeGet("/api/operations/readiness");
  if (seq !== state.ops.seq) return;
  if (result.error || !Array.isArray(result.payload?.checks)) {
    state.ops.error = result.error ?? malformed();
  } else {
    state.ops.readiness = result.payload;
    state.ops.error = null;
    state.ops.receivedAt = Date.now();
  }
  opsViews[state.page]?.();
}

export const OPS_PAGES = new Set(["overview", "control", "positions", "evidence"]);

// Monitoring state as reported by the backend (installed tables + enabled flag).
export function monitoringValue() {
  const monitoring = state.ops.readiness?.services?.monitoring;
  if (!monitoring) return state.ops.error || state.ops.readiness ? "UNAVAILABLE" : "NOT CHECKED";
  if (monitoring.enabled === true) return "ACTIVE";
  if (monitoring.installed === false) return "NOT AVAILABLE";
  return "INACTIVE";
}

export function notificationsValue() {
  const notifications = state.ops.readiness?.services?.notifications;
  if (!notifications) return state.ops.error || state.ops.readiness ? "UNAVAILABLE" : "NOT CHECKED";
  return notifications.automatic === true ? "ACTIVE" : "INACTIVE";
}

const rows = (items) => `<div class="info-list">${items.map(([label, value, id]) => `<div class="info-row"${id ? ` id="${esc(id)}"` : ""}><span>${esc(label)}</span><strong>${value}</strong></div>`).join("")}</div>`;
const panel = (title, meta, body, id = "") => `<section class="panel"${id ? ` id="${esc(id)}"` : ""}><div class="panel-head"><span class="panel-title">${esc(title)}</span><span class="panel-meta">${esc(meta)}</span></div>${body}</section>`;
const unavailable = (what) => state.ops.error
  ? emptyState(`${what} unavailable`, state.ops.error.message || "The readiness request failed.", "error-state")
  : emptyState("Checking", "Waiting for the readiness assessment.");

function readinessSection(r) {
  if (!r) return panel("Readiness", "DIAGNOSTIC · ENABLES NOTHING", unavailable("Readiness"), "readiness-panel");
  const checks = r.checks.map((c) => {
    const detail = c.detail && typeof c.detail === "object"
      ? Object.entries(c.detail).filter(([, v]) => v !== null && typeof v !== "object").map(([k, v]) => `${pretty(k)} ${v}`).join(" · ")
      : String(c.detail ?? "");
    return `<tr data-readiness-check="${esc(c.check)}"><td>${esc(pretty(c.check))}</td><td>${opsChip(checkVocabulary(c.status), { field: "check" })}</td><td>${esc(detail)}</td><td>${esc(c.remediation ?? "—")}</td></tr>`;
  }).join("");
  const blockers = r.unattended_blocking_reasons.map((b) => `<li data-readiness-blocker="${esc(b.code)}">${esc(b.reason)} <span class="muted">· ${esc(b.remediation)}</span></li>`).join("");
  return panel("Readiness", "DIAGNOSTIC · ENABLES NOTHING", `${rows([
    ["Manual operation", opsChip(r.manual_operation === "ok" ? "OK" : "ATTENTION"), "readiness-manual"],
    ["Unattended operation", opsChip(unattendedValue()), "readiness-unattended"],
    ["Assessed", esc(stampSeconds(r.checked_at_utc))],
  ])}
    <div class="panel-note account-note" id="readiness-blockers">Why unattended operation is not permitted:<ul>${blockers}</ul></div>
    <div class="table-scroll"><table class="data-table" id="readiness-checks"><thead><tr><th>Check</th><th>Status</th><th>Detail</th><th>Remediation</th></tr></thead><tbody>${checks}</tbody></table></div>`, "readiness-panel");
}

// Service states. "live" values come from the readiness response; "by design" rows have no
// status API in this version and are stated as fixed facts of the current release.
export function serviceRows() {
  const r = state.ops.readiness;
  const notifications = r?.services?.notifications;
  return [
    ["scheduler", "Shadow scheduler", schedulerValue(), "live · readiness", "Background shadow evaluation (read-only, never orders). Turning it on or off requires separate approval; there is no control on this screen."],
    ["monitoring", "Broker monitoring", monitoringValue(), "live · readiness", "No broker observation is taken while inactive."],
    ["reconciliation", "Reconciliation", monitoringValue() === "ACTIVE" ? "ACTIVE" : monitoringValue() === "INACTIVE" ? "INACTIVE" : monitoringValue(), "live · follows monitoring", "Runs only on an explicit broker observation; nothing is linked automatically."],
    ["notifications", "Notifications", notificationsValue(), "live · readiness", notifications?.external_delivery ? `External delivery: ${notifications.external_delivery}.` : "Local inbox only; nothing is sent automatically."],
    ["backups", "Automatic backups", "DISABLED", "by design · no status API yet", "No backup schedule exists in this version. Pre-migration backups are made by the journal before a migration."],
    ["evidence", "Evidence recording", "INACTIVE", "by design · no status API yet", "Nothing is recorded automatically."],
    ["linking", "User-confirmed trade linking", "INACTIVE", "by design · endpoint answers 403", "The confirmation endpoint is not activated."],
    ["execution", "Autonomous execution", executionValue(), "live · status and readiness", "Johanna has no authorisation to execute trades; no order is ever placed."],
    ["costs", "CostTerms", costTermsValue(), "live · readiness", "Without verified costs a risk proposal has no actionable volume."],
  ];
}

function servicesSection() {
  const body = `<div class="table-scroll"><table class="data-table" id="service-status"><thead><tr><th>Capability</th><th>State</th><th>Source</th><th>Note</th></tr></thead><tbody>${serviceRows().map(([key, label, value, source, note]) => `<tr data-service="${esc(key)}"><td>${esc(label)}</td><td>${opsChip(value, { field: key })}</td><td class="muted">${esc(source)}</td><td>${esc(note)}</td></tr>`).join("")}</tbody></table></div>
    <div class="panel-note account-note">Any activation requires separate approval and backend safeguards. This screen contains no activation control.</div>`;
  return panel("Service status", "READ-ONLY", body, "service-panel");
}

function journalSection(r) {
  if (!r) return panel("Journal and schema", "READ-ONLY", unavailable("Journal status"), "journal-panel");
  const j = r.journal ?? {};
  const audit = r.checks.find((c) => c.check === "audit_chain")?.detail ?? {};
  const journal = journalValue();
  const schema = schemaValue();
  const pairs = j.backup_pair_evidence ? ` (validated now ${j.backup_pair_evidence.validated_now}, reused ${j.backup_pair_evidence.reused})` : "";
  return panel("Journal and schema", "READ-ONLY", rows([
    ["Journal", `${opsChip(journal.value)} <span class="muted">${esc(journal.detail)}</span>`, "control-journal"],
    ["Schema", `${opsChip(schema.value, { tone: schema.tone })} <span class="muted">${esc(schema.detail)}</span>`, "control-schema"],
    ["Pending schema change", esc(j.staged_schema ?? "—")],
    ["Audit chain", audit.valid === true ? `${opsChip("OK")} ${esc(audit.rows ?? "")} rows · ${esc(audit.mode ?? "")} check` : opsChip(audit.valid === false ? "ATTENTION" : "NOT CHECKED"), "control-audit"],
    ["Key", esc(pretty(j.key_status ?? "—"))],
    ["Valid backup pairs", `${esc(j.valid_backup_pairs ?? "—")}${esc(pairs)}`, "control-backup-pairs"],
  ]), "journal-panel");
}

function schedulerSection(r) {
  const s = r?.scheduler;
  if (!s) return panel("Scheduler", "READ-ONLY", unavailable("Scheduler status"), "scheduler-panel");
  return panel("Scheduler", "READ-ONLY", `${rows([
    ["State", `${opsChip(schedulerValue())} <span class="muted">${esc(pretty(s.state))}</span>`, "control-scheduler"],
    ["Health", esc(pretty(s.health ?? "—"))],
    ["Pending / running windows", `${esc(s.pending ?? 0)} / ${esc(s.running ?? 0)}`],
    ["Consecutive errors", esc(s.consecutive_errors ?? 0)],
  ])}<div class="panel-note account-note" id="scheduler-readonly">The shadow scheduler evaluates closed candles in the background for research only; it never places orders. Activation requires separate approval — this application has no control for it.</div>`, "scheduler-panel");
}

function notificationsSection() {
  return inactivePanel("Notifications", notificationsValue(), "Local inbox infrastructure only: nothing is created or delivered automatically and there is no external delivery. A read-only history view is planned (B7.5).", "notifications-panel");
}

function backupsSection(r) {
  const pairs = r?.journal?.valid_backup_pairs;
  return panel("Backups and recovery", "READ-ONLY", `${rows([
    ["Automatic backups", `${opsChip("DISABLED")} <span class="muted">no schedule exists in this version</span>`, "control-auto-backups"],
    ["Verified recovery points", pairs == null ? opsChip("NOT CHECKED") : `${esc(pairs)} valid backup pair${pairs === 1 ? "" : "s"}`],
    ["Backup history, verification and restore rehearsal", opsChip("NOT AVAILABLE"), "control-backup-api"],
  ])}<div class="panel-note account-note">There is no backup status API yet; restore remains a documented manual procedure. No backup, prune or restore action exists here.</div>`, "backups-panel");
}

function restrictionsSection() {
  const items = [
    "Johanna never places, modifies or closes orders; broker execution is disabled.",
    "MT5 is read-only.",
    "A market-analysis candidate is not an approved trade; a risk proposal is not an order.",
    "Unknown trading costs are never treated as zero.",
    "Estimated P&L is never shown as realised P&L.",
    "Unattended operation is not permitted while readiness blocks remain.",
    "Trading rules and news are not implemented and are not shown as features.",
  ];
  return panel("Operational restrictions", "FIXED", `<ul class="restriction-list" id="restrictions">${items.map((t) => `<li>${esc(t)}</li>`).join("")}</ul>`, "restrictions-panel");
}

export function controlContent() {
  const r = state.ops.readiness;
  return `${statusBand("control-status-band")}
    <div class="control-grid">${readinessSection(r)}${servicesSection()}${journalSection(r)}${schedulerSection(r)}${notificationsSection()}${backupsSection(r)}${restrictionsSection()}</div>`;
}

export function controlPage() {
  return `${pageHeading("System Control Centre", "Operational state, readiness and restrictions · read-only · enables nothing")}<div id="control-content">${controlContent()}</div>`;
}

opsViews.control = () => setInner(document.querySelector("#control-content"), controlContent());
