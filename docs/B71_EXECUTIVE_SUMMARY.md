# B7.1 — Executive Summary

## Overall maturity

**Solid core, incomplete control centre.** Market analysis (chart, overlay, conditions,
opportunity checks), the live risk proposal, the account/broker view and the manual trade journal
are implemented carefully: freshness is explicit, unknown values are labelled instead of zeroed,
candidates are never called approved, estimates never called realised, and writes are idempotent.
What is missing is the layer that tells you at a glance whether the system is healthy, what is
active and what blocks actionable trading. Several backend capabilities built in P6 have no screen.

## Most important gaps

1. **No system home**: readiness, service states and restrictions are buried in "Analytics" and
   Settings; monitoring, reconciliation, notifications and backups are not shown at all.
2. **Overview is market-only**: no account, decision, risk, journal or system summary.
3. **Cost readiness invisible**: `/api/costs/readiness` explains why no proposal is actionable,
   but no screen calls it.
4. **Decision history is a flat list**: the decision-detail API and list filters are unused.
5. **Unused or missing APIs**: monitoring, reconciliation, links, notifications have APIs without
   UI; backups, evidence and cost-registry history have no API.
6. **Safety presentation issues**: a working "Autonomous execution" toggle in Settings, a
   one-click scheduler enable without confirmation, readiness chips reading COMPLETED/FAILED, and
   a stale "monitoring and reconciliation not_implemented" label.
7. **Maintainability**: a single 3,725-line `app.js`.

## Highest-priority improvements

1. Remove the autonomous-execution toggle (read-only status), add confirmation to scheduler
   enable, fix readiness vocabulary and the stale label.
2. Add a status band (Execution DISABLED, Unattended NOT PERMITTED, Scheduler, CostTerms,
   Journal) to Overview and a System Control Centre.
3. Show cost readiness and a Candidate → Proposal → Actionable lifecycle on Decisions & Risk.
4. Decision detail drawer and filters.

## Recommended information architecture

Overview · Market Analysis (Chart/Watchlist/Calendar) · Decisions & Risk (Current proposal/
Decision history/Shadow research) · Trade Journal · Positions & Reconciliation (read-only,
INACTIVE) · Evidence & Costs · System Control Centre (Readiness/Services/Journal/Scheduler/
Notifications/Backups/Restrictions) · Settings. Trading Rules and News leave the navigation until
implemented.

## Implementation sequence

- **B7.2** shell, navigation, safety corrections, Overview status band, Control Centre skeleton,
  ES-module split.
- **B7.3** Market Analysis, Decisions & Risk, Evidence & Costs (cost readiness).
- **B7.4** Trade Journal in the new shell, summary strip.
- **B7.5** read-only Positions & Reconciliation and Notifications.
- **B7.6** System Control Centre completion, backups and evidence views (needs new read-only APIs).
- **B7.7** accessibility, responsive tests, regression, known browser-test race.

## Architectural and safety concerns

- Several B7 stages need small backend changes (asset allowlist for modules, read-only APIs for
  backups/evidence/cost history, the stale journal label, the execution preference). Each should
  be reviewed separately; none may activate a capability.
- `/api/costs/readiness` reads MT5 instruments; poll it only while its screen is open.
- Every activation control (scheduler today; others later) must use an explicit confirmation and
  backend safeguards; inactive capabilities must never appear as toggles.
- ML assessment and context checks named in the brief do not exist in the backend and are not
  proposed.
