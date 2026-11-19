// B7.2 Positions & Reconciliation (read-only). Broker monitoring and reconciliation exist in the
// backend but are not activated: no broker observation is taken, so there are no positions,
// candidate matches or discrepancies to show. The monitoring state shown is the backend's own.
import { pageHeading, setInner } from "./format.js";
import { inactivePanel, opsChip } from "./components.js";
import { monitoringValue, opsViews } from "./control.js";

function positionsContent() {
  const monitoring = monitoringValue();
  return `${inactivePanel("Broker monitoring", monitoring,
    monitoring === "ACTIVE"
      ? "Monitoring is reported active by the backend. Observation and reconciliation views are planned (B7.5)."
      : "Monitoring is inactive: no broker observation is taken and open positions are not synchronised. Reconciliation results, candidate matches and discrepancies will appear here only after monitoring is separately approved and activated.",
    "positions-monitoring")}
    ${inactivePanel("User-confirmed trade links", "INACTIVE", "The link confirmation endpoint is not activated. A candidate match is never treated as a confirmed link and nothing is linked automatically.", "positions-links")}
    <section class="panel" id="positions-account"><div class="panel-head"><span class="panel-title">Account &amp; Broker</span>${opsChip("READ-ONLY", { tone: "neutral" })}</div>
      <div class="inactive-copy">Live account values and broker instrument specifications are read from MT5 on the Account &amp; Broker screen (read-only; no orders). <button class="text-link" type="button" data-page="account">Open Account &amp; Broker</button></div></section>`;
}

export function positionsPage() {
  return `${pageHeading("Positions & Reconciliation", "Broker positions and logbook reconciliation · read-only · monitoring not activated")}<div id="positions-content">${positionsContent()}</div>`;
}

opsViews.positions = () => setInner(document.querySelector("#positions-content"), positionsContent());
