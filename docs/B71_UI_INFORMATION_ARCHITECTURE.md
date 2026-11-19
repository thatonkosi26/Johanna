# B7.1 — Recommended UI Information Architecture

Proposal only (nothing implemented). It reuses what works, regroups what is scattered, and
adds read-only status views for inactive backend capabilities before any workflow controls.

## 1. Assessment of the proposed nine-area structure

| Proposed area | Verdict | Reason |
|---|---|---|
| Overview | **Keep, redefine** | today a market screen; should summarise market, decisions, risk, journal and system state |
| Market Analysis | **Keep; merge** Charts + Markets + Calendar | the three share instrument context; News is an unimplemented service |
| Decisions & Risk | **Keep; merge** Decisions + Risk | the API already links decisions to risk assessments |
| Trade Journal | **Keep** (rename Trade History) | complete per-trade workflow exists |
| Reconciliation | **Merge with Positions**, read-only | backend inactive; a full screen of empty workflows is premature |
| Notifications | **Fold into System Control Centre** for now | backend inactive and local-only; an inbox page later |
| Evidence & Costs | **Keep**, start with cost readiness | cost readiness is the main blocker to actionable proposals and already has an API |
| Backups & Recovery | **Fold into System Control Centre** | needs a new read-only API first; small content |
| System Control Centre | **Keep; promote** | collects readiness, services, journal, scheduler, restrictions |

## 2. Recommended navigation (7 primary + Settings)

1. **Overview**
2. **Market Analysis** (tabs: Chart · Watchlist · Calendar)
3. **Decisions & Risk** (tabs: Current proposal · Decision history · Shadow research)
4. **Trade Journal**
5. **Positions & Reconciliation** — read-only, marked INACTIVE
6. **Evidence & Costs**
7. **System Control Centre** (sections: Readiness · Services · Journal & schema · Scheduler ·
   Notifications · Backups & recovery · Restrictions)
8. Settings (secondary, bottom of the sidebar)

Trading Rules and News leave the primary navigation until implemented (a footnote in System
Control Centre lists not-implemented services). Route ids should match labels (no `signals` for
"Decisions"); old hashes redirect.

## 3. Screen-by-screen structure

### 3.1 Overview
- **Status band** (always visible on Overview): Application, Market data, Journal (schema v6,
  key verified), Execution `DISABLED`, Unattended `NOT PERMITTED`, Scheduler `OFF`, CostTerms
  `NOT CONFIGURED` — each a chip linking to its screen. Sources: `/api/dashboard`,
  `/api/operations/readiness`, `/api/shadow/status`.
- **Market summary**: selected pair chart (existing `chartMarkup()`), quote strip, compact
  analysis (existing).
- **Decisions & risk summary**: latest decision per instrument/timeframe with its separate
  dimensions; current proposal status chip with "no actionable volume" when applicable.
- **Journal summary**: open manual trades and today's realised result (only where calculable;
  otherwise "not calculated").
- **Account snapshot**: balance, equity, free margin with validation chips (existing
  `fieldMarkup()`).
- **Alerts**: readiness blockers and failed checks (not notifications, which are inactive).

### 3.2 Market Analysis
- Tabs reuse current Charts, Markets and Calendar renderers unchanged.
- Instrument context header (pair, timeframe, market status, last quote, analysis age).
- Analysis panel and overlay as today.

### 3.3 Decisions & Risk
- **Current proposal** (existing Risk page) with a lifecycle header:
  *Candidate → Risk proposal → Actionable?* — the third step reads "Not actionable: transaction
  costs not established" while CostTerms are empty.
- **Decision history**: existing table plus filters (outcome, eligibility, execution — supported
  by `/api/journal/decisions`) and a **detail drawer** using `/api/journal/decisions/{id}`
  (analysis evidence, risk assessments, observations, linked trades).
- **Shadow research**: existing shadow diagnostics, intents, funnel and cycles (moved from
  Analytics), with the shadow banner. Scheduler control moves to System Control Centre.

### 3.4 Trade Journal
- Existing logbook (list, detail, fills, protections, notes, links, voids) unchanged.
- Add a **summary strip**: open/closed counts, realised net where calculable, count of trades
  with unknown costs (computed from existing list/detail payloads; no estimates shown as
  realised).

### 3.5 Positions & Reconciliation (read-only)
- Header chip `MONITORING INACTIVE — NO BROKER OBSERVATION IS TAKEN` from
  `/api/monitoring/status`.
- Latest reconciliation (`/api/monitoring/reconciliation`): observation time/status, results
  with `link_status` labels (USER_CONFIRMED, RECORDED_REFERENCE, CANDIDATE, AMBIGUOUS, REJECTED,
  UNAVAILABLE) and discrepancies — all empty today; empty state explains why.
- User links (`/api/reconciliation/links`) list; **no confirm button** until the endpoint is
  approved (show "Confirmation not activated").

### 3.6 Evidence & Costs
- **Cost readiness** (`/api/costs/readiness`): per instrument — commission, slippage, swap and
  holding-period status, blocking reasons, live broker-terms fingerprint, registry status;
  banner "DIAGNOSTIC ONLY — APPROVES NOTHING".
- **Evidence registry**: placeholder "No evidence API yet" until a read-only endpoint exists.

### 3.7 System Control Centre
- **Readiness**: manual operation, unattended permitted (always prominent), checks with
  `OK` / `ATTENTION` chips (not COMPLETED/FAILED), blockers with runbook anchors.
- **Services**: table of Scheduler, Monitoring, Reconciliation, Notifications, Automatic backups,
  Evidence recording, Execution — state (`ACTIVE` / `INACTIVE` / `DISABLED` / `NOT AVAILABLE`),
  source, and "requires approval".
- **Journal & schema**: state, schema installed/registered, key status, audit chain rows/valid,
  valid backup pairs.
- **Scheduler**: current state and the existing enable/disable control behind an explicit
  confirmation dialog that names the consequence ("runs in the background until disabled; no
  orders").
- **Notifications**: status from `/api/notifications` `service` block; list read-only.
- **Backups & recovery**: needs a new read-only API (inventory, last result, policy, "automatic
  backups disabled").
- **Restrictions**: fixed statements (no order placement, execution disabled, MT5 read-only).

### 3.8 Settings
- Keep preferences, timezone, collector, services health.
- Replace the **Autonomous execution toggle** with a read-only row "Autonomous execution: not
  available (disabled by design)".

## 4. Cross-cutting components

| Component | Purpose | Based on |
|---|---|---|
| `StatusChip` with fixed vocabulary | LIVE / CACHED / STALE / UNAVAILABLE; ACTIVE / INACTIVE / DISABLED / NOT AVAILABLE; OK / ATTENTION; VERIFIED / UNVERIFIED / MISSING | `statusBadge`, `field-chip` |
| `FreshnessLine` | "Retrieved hh:mm:ss · n s ago · source" on every data panel | account freshness rows |
| `InactiveCapabilityPanel` | standard empty state for inactive features with the reason and approval requirement | `emptyState` |
| `LifecycleHeader` | Candidate → Proposal → Actionable with the blocking step | risk status chip |
| `ConfirmDialog` | required for every state-changing control (scheduler, future activations) | new |
| `DetailDrawer` | decision and intent details | shadow intent detail |

## 5. Empty, error and inactive states

- **Empty** (healthy, no data): "No decisions recorded yet" with when data appears.
- **Error** (request failed): message, last successful time, data marked CACHED/STALE.
- **Inactive** (capability exists, not activated): grey chip `INACTIVE`, one sentence of what it
  would do, "requires separate approval"; never an enabled control.
- **Not available** (no backend support): `NOT AVAILABLE`; no fake values.
- Never show 0 for unknown; never call an estimate realised; never call a candidate approved.

## 6. Responsive layout

- ≥ 1180 px: sidebar + two-column grids (as today).
- 900–1180 px: single-column panels, status band wraps.
- < 760 px: drawer navigation (existing), status band as a horizontally scrollable chip row,
  tables become stacked key/value cards for Decisions, Journal and Services (instrument table
  keeps horizontal scroll), chart keeps full width with controls in a second row.
- Test each primary screen at 400 × 900 and 1600 × 1000 in the browser harness.
