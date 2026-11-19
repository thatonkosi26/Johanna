# B7.1 — UI Implementation Roadmap

Proposal only. Each stage is separately approved, keeps the existing browser tests green, adds
its own browser tests, and never activates a backend capability.

## Recommended sequence (adjusted)

The provisional order is kept with three changes justified by the code:

1. **Safety corrections first** (inside B7.2): the autonomous-execution toggle, one-click
   scheduler enable and the stale "not implemented" label exist today.
2. **Module split before new screens** (inside B7.2): `app.js` is 3,725 lines; new screens
   should not be added to one file. A dependency-free split into ES modules (served as
   `/assets/*.js`) requires a small backend change to the asset allowlist in `api.py`.
3. **Evidence & Costs moves earlier** (B7.3 with Decisions & Risk): cost readiness is the
   blocker that makes every proposal non-actionable, and its API already exists. Reconciliation
   and notifications stay in B7.5 as read-only status views.

---

### B7.2 — Shell, navigation, safety corrections, Overview

- **Scope**: new navigation (7 + Settings) with hash redirects; Overview status band and
  summaries; System Control Centre skeleton (readiness + services table); safety corrections;
  ES-module split with no behaviour change.
- **Reuse**: `renderTopbar`, `statusBadge`, `fieldMarkup`, `chartMarkup`, `quoteStripMarkup`,
  `analysisPanelCompact`, `readinessMarkup`, `healthMarkup`, pollers in `startLivePolling`.
- **New**: `StatusChip` vocabulary, `InactiveCapabilityPanel`, `ConfirmDialog`, services table.
- **APIs**: `/api/dashboard`, `/api/operations/readiness`, `/api/shadow/status`,
  `/api/monitoring/status`, `/api/notifications` (status block), `/api/account/summary`.
- **Backend changes (small, separately reviewed)**: asset allowlist for module files; correct
  `JournalStore.status()["monitoring_and_reconciliation"]` wording (implemented, inactive);
  remove or neutralise the `autonomous_execution_enabled` preference write path.
- **Acceptance**: every existing browser test passes or is migrated with the same assertion;
  Overview shows Execution DISABLED, Unattended NOT PERMITTED, Scheduler state, CostTerms state;
  no control enables anything without `ConfirmDialog`; no new MT5 request on Overview beyond
  existing pollers.
- **Safety**: scheduler enable requires confirmation; autonomous execution shown read-only;
  inactive services never rendered as toggles.

### B7.3 — Market Analysis, Decisions & Risk, Cost readiness

- **Scope**: Market Analysis tabs (reuse Charts/Markets/Calendar); Decisions & Risk with
  lifecycle header, filters and decision detail drawer; shadow research tab (moved);
  Evidence & Costs screen with cost readiness.
- **Reuse**: chart/overlay code, `riskContent`, `decisionsContent`, shadow renderers,
  `decisionLinkMarkup`.
- **New**: `LifecycleHeader`, `DetailDrawer`, cost-readiness table.
- **APIs**: `/api/market/*`, `/api/risk/proposal`, `/api/journal/decisions` (+ filters),
  `/api/journal/decisions/{id}`, `/api/shadow/*`, `/api/costs/readiness`.
- **Dependencies**: B7.2 shell.
- **Acceptance**: a candidate is never labelled approved; a proposal without volume shows the
  blocking reason in the lifecycle header; cost readiness shows each missing component per
  instrument and "approves nothing"; `/api/costs/readiness` is polled only while its screen is
  open (it reads MT5 instruments).
- **Safety**: no approval control for costs; unknown costs never 0.

### B7.4 — Trade Journal

- **Scope**: move the existing logbook into the new shell; add summary strip and filters
  (open/closed/voided, symbol); improve mobile forms.
- **Reuse**: all trade renderers and `tradeRequest`/`tradeWrite`.
- **New**: summary strip, stacked mobile layout.
- **APIs**: existing `/api/trades*`.
- **Acceptance**: all current trade browser tests pass; totals only from calculated values with
  their status; estimates labelled; voided records visible but distinguished.
- **Safety**: no broker linkage implied; banner retained.

### B7.5 — Positions & Reconciliation, Notifications (read-only)

- **Scope**: read-only screens/sections for monitoring status, latest reconciliation
  (`link_status`, discrepancies), user links, and notification list with `delivery` states.
- **New**: reconciliation result table, discrepancy list, notification list.
- **APIs**: `/api/monitoring/status`, `/api/monitoring/reconciliation`,
  `/api/reconciliation/links` (GET), `/api/notifications`.
- **Acceptance**: every screen shows INACTIVE with the reason; empty states explain that no
  observation is taken; no confirm, observe, deliver or acknowledge control.
- **Safety**: the POST links endpoint stays 403 and is not called.

### B7.6 — Backups & recovery, Evidence, System Control Centre completion

- **Scope**: complete System Control Centre (journal & schema, backups, restrictions,
  notification service); evidence registry view.
- **Backend prerequisite (separate approval)**: read-only APIs for backup inventory/status
  (`BackupManager.inventory()/status()`), evidence list/assess (`EvidenceRegistry`), cost
  registry history (`CostPolicyRegistry.history()`), and optionally an audit-log page.
- **Acceptance**: automatic backups shown DISABLED; recovery points listed with validity; no
  backup/prune/restore control; evidence statuses MISSING/STALE/CONFLICTING/UNVERIFIED/VERIFIED
  shown verbatim.
- **Safety**: read-only APIs must not validate every pair on each request without the M-8
  evidence cache.

### B7.7 — Integration, accessibility, responsive, regression

- **Scope**: keyboard navigation and focus order, ARIA for chips/tables/dialogs, contrast check
  in both themes, 400 × 900 and 1600 × 1000 browser tests for every screen, performance check of
  pollers per screen, fix the known browser-test snapshot race
  (`test_repeated_toggling_adds_no_requests_timers_or_listeners`).
- **Acceptance**: full suite green three consecutive times on Python 3.11 and 3.13; no
  regression in poller counts; every inactive capability visibly inactive.

## Dependencies summary

B7.2 → B7.3 → B7.4 (independent of B7.3 after B7.2) → B7.5 → B7.6 (needs new read-only
backend APIs) → B7.7.
