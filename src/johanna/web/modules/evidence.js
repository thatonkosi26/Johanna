// B7.2 Evidence & Costs. The CostTerms state comes from the readiness snapshot; the per-instrument
// cost-readiness view (GET /api/costs/readiness, which reads broker instruments from MT5) is
// planned for B7.3 and is deliberately not requested here. There is no evidence API yet.
import { pageHeading, setInner } from "./format.js";
import { costTermsValue, inactivePanel } from "./components.js";
import { opsViews } from "./control.js";

function evidenceContent() {
  const costs = costTermsValue();
  const costText = costs === "NOT CONFIGURED"
    ? "Production cost terms are empty: commission, slippage and holding-period assumptions are not verified, so every risk proposal is UNVERIFIED on costs and produces no actionable volume. Unknown costs are never treated as zero. The per-instrument cost-readiness view is planned for the next stage (B7.3)."
    : costs === "CONFIGURED"
      ? "Cost terms are reported configured. Per-instrument verification, approval and expiry are shown in the cost-readiness view planned for B7.3."
      : "The CostTerms state has not been established yet.";
  return `${inactivePanel("Cost terms", costs, costText, "evidence-costs")}
    ${inactivePanel("Evidence registry", "NOT AVAILABLE", "Evidence recording is inactive and there is no evidence API in this version. Nothing is recorded automatically and no evidence is shown as verified.", "evidence-registry")}`;
}

export function evidencePage() {
  return `${pageHeading("Evidence & Costs", "Cost verification and evidence records · read-only · approves nothing")}<div id="evidence-content">${evidenceContent()}</div>`;
}

opsViews.evidence = () => setInner(document.querySelector("#evidence-content"), evidenceContent());
