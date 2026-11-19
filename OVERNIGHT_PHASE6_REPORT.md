# Overnight Phase 6 report (P6.2b.1 → P6.R)

Date: 2026-11-18 (UTC). Scope: implementation and **offline** verification only. Nothing in
this report is live production verification unless it says so explicitly.

## A. Executive summary

**Overall verdict: COMPLETE (offline), with every financial and activation decision left to
the user.** All five phases are implemented and tested on temporary journals and the fake MT5.
No production migration, no activation, no cost approval, no broker action.

| Phase | Status |
|---|---|
| P6.2b.1 Cost infrastructure corrections | COMPLETE |
| P6.2b.2 Cost evidence and policy-management infrastructure | COMPLETE (registry staged; approvals BLOCKED on user) |
| P6.3 Read-only monitoring and reconciliation | COMPLETE (offline; not live-tested; activation BLOCKED) |
| P6.4 Notification infrastructure (local only) | COMPLETE (offline; external delivery not implemented) |
| P6.R Unattended-operation hardening | COMPLETE (diagnostics + runbook; enables nothing) |

**One design rule protects production throughout.** The journal migrates automatically when
the backend starts. New tables for P6.2b.2/P6.3/P6.4 are therefore in a **staged** schema v5
(`journal_store.SCHEMA_V5`, `STAGED_MIGRATIONS`) that is **not registered** in `MIGRATIONS`.
Restarting the backend migrates nothing; the new features report `not_installed` on the
production v4 journal. Registering v5 is an explicit user decision (C-4).

**Critical outstanding risks:** none found in the implemented code. Open items are evidence
and approvals (section C) and the caveats in section F. Production still cannot approve any
trade: commission, slippage and holding periods are unverified.

## B. Phase-by-phase results

Final test results for the whole programme are in section B.6.

### B.1 P6.2b.1 — Cost infrastructure corrections — COMPLETE

Files: `src/johanna/cost_evidence.py` (new, pure), `src/johanna/risk_engine.py`,
`src/johanna/risk_service.py`; tests `tests/test_cost_evidence.py` (new, 63) plus updated
`risk_fixtures.py`, `test_risk_corrections.py`, `test_risk_safeguards.py`, `test_p62a_risk.py`,
`test_risk_engine.py`, `test_shadow_intents.py`; design note `docs/p6.2b.1-cost-evidence.md`.

Decisions:
- **Rollover = 17:00 America/New_York**, Monday–Friday. Exness states 21:00 GMT (summer) /
  22:00 GMT (winter), which is exactly New York 17:00 under US daylight saving. Exness does not
  say whose "summer"; in the weeks when US and UK clocks differ the alternative reading (22:00
  Europe/London) is also computed and the **larger** count used, flagged
  `seasonal_ambiguity`. Rollovers at or after the evaluation time up to the end of the holding
  window, each once (an event exactly at the evaluation instant counts — conservative). Triple
  day from live MT5 (`swap_rollover3days`, currently Wednesday). Uses the bundled IANA
  database (`tzdata`, already a project dependency).
- The P6.2b defect is fixed: e.g. Tuesday 22:30 UTC now counts Wednesday's triple, not
  Tuesday's already-passed single charge.
- **Bank holidays are not modelled** (no reliable source); every swap result says
  `holiday_adjustments: not_modelled`.
- **Holding period per timeframe** (`HoldingAssumption`: hours, rationale, status
  PROPOSED/APPROVED/REJECTED, effective date, policy version, approval time). Only APPROVED,
  effective and ≤ 30 days old is used; otherwise a negative swap keeps the proposal
  UNVERIFIED. No default is ever substituted. `HOLDING_PROPOSALS` (H1 24 h, H4 120 h, D1 480 h)
  are drafts, status PROPOSED, never in `COST_TERMS`. Not a time stop.
- **Structured cost components** (`CostComponent`): value, currency (USC/USD for commission;
  none for slippage), unit (`amount_per_lot`; `points`/`pips`), basis (`per_side`/
  `round_trip`), instrument scope, account type, source reference and description, evidence
  and effective dates, status (VERIFIED/UNVERIFIED/PROVISIONAL/REJECTED; EXPIRED derived),
  approval time. Each assessed independently; missing = `MISSING`, never zero. Conversions are
  recorded step by step (USD×100, per side×2, pips×10 for 5/3-digit quotes) and shown per lot
  and per 0.01 lot. Two records for one cost are refused.
- Live swap rates remain broker data (never configured). Spread, sizing, M1 final budget,
  structural TP and policy values unchanged. `RISK_ENGINE_VERSION` 2 → 3.

Limitations: holidays not modelled; the rollover clock evidence is a search summary, not
verbatim; schema unchanged (no migration needed: the cost evidence is carried in the existing
JSON evidence and cost-terms version).

Approvals required: C-1, C-2, C-3, C-6.

### B.2 P6.2b.2 — Cost evidence and policy management — COMPLETE (approvals BLOCKED)

Files: `src/johanna/cost_policy.py` (new), `cost_evidence.py` (broker-terms fingerprint),
`journal_store.py` (staged v5 tables), `application.py`, `api.py`; tests
`tests/test_cost_policy.py` (new, 17).

- **Registry** (staged v5 `cost_policy_record` / `cost_policy_decision`, append-only, audited):
  evidence is stored UNVERIFIED/PROVISIONAL (holding: PROPOSED) — never verified on entry;
  deduplicated by fingerprint; supersession keeps history.
- **Decisions are separate rows.** Only the user approves or rejects (database CHECK); approval
  requires typing back the converted value (e.g. `7 USC/lot RT`, `120 hours`) so a USD figure
  cannot pass as USC unnoticed. Johanna may only INVALIDATE. Nothing follows a rejection or
  invalidation (trigger). Approval lapses after 30 days unless explicitly renewed.
- **Broker-terms invalidation:** each record can carry a fingerprint of the account currency/
  unit and the symbol's contract, precision, currencies and execution mode. If the live terms
  differ, the record reads `BROKER_TERMS_CHANGED` and the engine refuses it.
- **Diagnostics:** `GET /api/costs/readiness` (read-only; Phase 3 instrument read through the
  shared cache) explains per instrument and timeframe exactly why costs are unverified, with
  live swap values and which directions need a holding period.
- The registry is **not wired into the risk service**: production keeps the empty
  `COST_TERMS`. `proposed_terms()` shows what *would* apply, for review only.

Approvals required: C-1, C-2, C-3, C-4, C-9.

### B.3 P6.3 — Read-only monitoring and reconciliation — COMPLETE (offline)

Files: `src/johanna/reconciliation.py` (new, pure), `src/johanna/broker_monitoring.py` (new),
`adapters/mt5.py` (`reconciliation_snapshot`), `live_market.py` (`reconciliation`,
`reconciliation_read`), `application.py`, `api.py`; tests `tests/test_broker_monitoring.py`
(new, 24); `tests/fake_mt5.py` (test data may override fixed tickets).

- Same single MT5 worker (no second connection); reads are **background** reads, deferred
  (never queued) when the dashboard uses the worker. Read-only calls only
  (`positions_get`, `orders_get`, `history_deals_get`).
- Broker identifiers leave the worker only to be replaced by HMAC references before anything
  is kept; without a verified key the observation is UNAVAILABLE and **no MT5 read happens**.
- States: CONFIRMED_OPEN / CONFIRMED_CLOSED (broker-confirmed only), PENDING, CANDIDATE_MATCH
  (one fitting deal, **never linked automatically**), AMBIGUOUS, UNMATCHED, UNAVAILABLE, plus
  UNRECORDED_POSITION. **Closure is never inferred from absence**: it needs closing deals for
  the full volume inside history that covers the trade. Stale/failed/future observations →
  UNAVAILABLE.
- Staged v5 `broker_observation` / `reconciliation_result`, append-only; confirmed states
  require `broker_confirmed` (CHECK). Reconciliation never changes the logbook.
- **No thread, no schedule; `enabled` is always False.** `GET /api/monitoring/status` and
  `/api/monitoring/reconciliation` report the last explicit observation and trigger no MT5 read.

Limitations: not tested against a live terminal; P5.2 manual trades carry no broker
reference, so the best outcome for them is CANDIDATE_MATCH until a user-confirmation path
exists (future work).

Approvals required: C-4, C-7.

### B.4 P6.4 — Notification infrastructure — COMPLETE (local only)

Files: `src/johanna/notifications.py` (new), `application.py`, `api.py`; tests
`tests/test_notifications.py` (new, 22).

- Lifecycle `pending → available → presented → acknowledged`, each once and in order
  (trigger); acknowledged only by the user. GET never changes state.
- Idempotent per `dedup_key` (unique) across retries and restarts; delivery attempts numbered
  per adapter, bounded (5), recorded with sanitised error codes.
- Content only from fixed templates: categories information / trade_candidate /
  risk_rejection / broker_event; a candidate is always "not an approved trade and not an
  order"; broker events only for broker-confirmed facts; identifier-like values, secrets and
  instruction wording are rejected.
- Failures never raise into callers. **Local adapters only** (in-memory, dashboard inbox);
  constructing any other adapter is refused; no network modules imported (tested).
- Nothing calls `notify()` automatically. `GET /api/notifications` lists (read-only).

Limitations: no POST endpoints for presented/acknowledged yet (service-level only); staged v5.

Approvals required: C-4, C-8.

### B.5 P6.R — Unattended-operation hardening — COMPLETE

Files: `src/johanna/operations.py` (new), `api.py`, `web/app.js` (read-only Operations
readiness panel on Analytics), `docs/operations-runbook.md` (new); tests
`tests/test_operations.py` (new, 9) and an added assertion in `tests/test_browser_intents.py`.

- `GET /api/operations/readiness`: process and single-instance lock; journal state, read-only
  `quick_check`, foreign keys, audit chain, key status, installed vs registered schema, valid
  backup pairs; MT5 state (no identity); scheduler state/health; execution, monitoring,
  notifications and cost-terms state. **`unattended_operation_permitted` is always false** in
  this phase, with each blocking reason and a runbook link. No MT5 read, no journal write, no
  paths, identities or keys (tested statically and by row counts).
- Runbook: startup/shutdown, duplicate process, journal refused (per reason), restore
  procedure that never regenerates keys or deletes evidence, integrity/audit failures, MT5,
  scheduler error, and the prerequisites for every future activation.
- Verified by tests: a restart never enables a disabled service; a second backend on the same
  data is refused; a refused journal and a tampered audit chain are reported with remediation.
- Existing safeguards relied on (already tested): no catch-up evaluation, deduplicated
  windows, bounded retries, stale data never LIVE, execution disabled.

### B.6 Test results (final run, code frozen)

| Check | Command | Result |
|---|---|---|
| Python 3.11.8 | `.venv311\Scripts\python -m pytest -q -W error::SyntaxWarning -p no:cacheprovider -rs` | exit 0 — **1953 passed, 1 skipped** (451 s) |
| Python 3.13.3 | `.venv\Scripts\python -m pytest -q -W error::SyntaxWarning -p no:cacheprovider -rs` | exit 0 — **1953 passed, 1 skipped** (447 s) |
| Ruff lint | `ruff check .` | All checks passed |
| Ruff format | `ruff format --check .` | 174 files already formatted |
| Node | `node --check src/johanna/web/app.js` | OK |
| Browser tests | included in both suites (Analytics, intents, readiness panel, shadow, trades, smoke, …) | passed |
| Migration tests | v3→v4 (existing) and v4→staged v5 on temporary journals | passed |

The only skip on both is the existing POSIX `chmod` test. No failures, no warnings. 1953 =
1831 (end of P6.2a) + 135 new tests (cost evidence 63, cost policy 17, monitoring 24,
notifications 22, operations 9) − 13 superseded parametrised cases (the old weekday-based
`_swap_units` table and the old flat `CostTerms` validation cases, replaced by event-based and
structured-record tests). No assertion was weakened to pass: the changed expectations are the
new reason wording, engine version 3, and the structured evidence fields.

## C. Outstanding user approvals (none was decided on your behalf)

| # | Question | Why it matters | Options | Recommendation | If not approved | Blocks production use? |
|---|---|---|---|---|---|---|
| C-1 | Supply verbatim, dated Exness evidence of the Standard Cent commission, and approve it? | Commission is a required cost; without it nothing can be approved | (a) provide verbatim text from your Exness Personal Area / Help Centre; (b) keep UNVERIFIED | (a), recorded via the registry after v5, approved by typing back the USC value | Every proposal stays UNVERIFIED | Yes (approvals) |
| C-2 | How should slippage evidence be collected? | No measured evidence exists; guessing is not allowed | (a) add a requested-price field to the manual logbook (code change); (b) approved read-only reading of MT5 order/deal history; (c) keep UNVERIFIED | (a) and (b), then a measured allowance for your approval | Slippage stays unmodelled; no approval possible | Yes (approvals) |
| C-3 | Approve, change or reject the holding-period proposals (H1 24 h, H4 120 h, D1 480 h)? | Negative swap needs a holding window per timeframe | approve as is / other values / keep PROPOSED | Decide with shadow data on actual holding times; approve explicitly per timeframe | Negative-swap directions stay UNVERIFIED | Yes (approvals) |
| C-4 | Register staged schema v5 (production migration at the next restart)? | Needed to persist cost evidence, broker observations and notifications | register after review / keep staged | Review first; then register and run a post-migration check like P6.2a | Features stay `not_installed` (in-memory only) | No (manual use unaffected) |
| C-5 | Run the read-only `order_calc_profit` probe (M-1)? | Confirms MT5 calculator excludes costs on this account | approve probe / skip | Approve (calculation only, no order) | M-1 stays partially resolved; current conservative handling continues | No |
| C-6 | Confirm the Exness rollover clock and holiday practice verbatim? | Rollover counting is based on a search summary | supply verbatim / accept current conservative model | Supply verbatim | Seasonal ambiguity handled conservatively; holidays not modelled | Partly (swap evidence quality) |
| C-7 | Activate broker monitoring (cadence, after C-4)? | Read-only reconciliation needs observations | activate on a schedule / keep explicit-only | After C-4 and a live read-only test | No observations; reconciliation idle | No |
| C-8 | Wire notifications (which events) and any external channel? | Only local adapters exist; nothing is sent | local inbox only / external channel later | Local inbox only, after C-4 | No notifications | No |
| C-9 | Allow the risk service to use registry-approved terms? | Separate from approving evidence | wire after evidence approval / keep empty terms | Only after C-1–C-3 are approved | Production terms stay empty | Yes (approvals) |
| C-10 | Restart the backend to load the new code? | The running backend still has the P6.2a code | restart / wait | Restart when convenient: **no migration happens** (v5 is not registered); then run the checklist in section E | New endpoints unavailable | No |

## D. Safety and production-state verification

| Item | Occurred? |
|---|---|
| Production database migration | **NO** (still schema v4; no `premigration-v4` pair; no v5 tables) |
| Scheduler activation | **NO** (latest setting `enabled 0`, 2026-11-18 07:11:00Z; 0 active windows) |
| CostTerms configuration | **NO** (`COST_TERMS` empty; no registry rows anywhere in production) |
| MT5 trading action | **NO** (only read-only calls exist; tests assert the read-only call set) |
| External notification | **NO** (no external adapter exists) |
| Backend restart | **NO** (by me) |
| Git operation | **NO** |
| Unrelated data modification | **NO** |

Read-only production evidence (journal opened `mode=ro`, GET requests only): schema v4,
integrity ok, 0 foreign-key violations, audit chain valid (3,211 rows, earlier entries
unchanged), key verified and user-only, all v3/v4 immutable rows unchanged, `shadow_intent` 0
rows, trading tables 0 rows. New journal rows since the migration are your dashboard's
requests only (2 H4 analyses and 2 risk assessments at 16:55–16:56 UTC, plus observation
updates).

## E. Morning verification checklist

Do these in order. Steps 1–3 need no restart.

1. Read this report and `docs/p6.2b.1-cost-evidence.md`, `docs/operations-runbook.md`.
2. Optionally re-run the offline checks: `.venv311\Scripts\python -m pytest -q` and
   `.venv\Scripts\python -m pytest -q`; `ruff check .`; `ruff format --check .`;
   `node --check src\johanna\web\app.js`.
3. Read-only production check before any restart: journal schema 4, scheduler disabled,
   0 pending/running windows (as in section D).
4. If you choose to restart (C-10 — no migration will run): then
   - `GET /api/dashboard` health ok; `GET /api/shadow/status` → `enabled: false`, journal
     schema 4, key verified;
   - `GET /api/operations/readiness` → `manual_operation: "ok"`,
     `unattended_operation_permitted: false`, `journal.installed_schema_version: 4`;
   - `GET /api/costs/readiness` → `approval_possible_anywhere: false`, commission and
     slippage `MISSING`, registry `not_installed`;
   - `GET /api/monitoring/status` → `enabled: false`, `last_observation: null`;
   - `GET /api/notifications` → `status: "not_installed"`;
   - Analytics page → Shadow trade intents empty state, Opportunity funnel, Operations
     readiness panel "Not permitted" with its reasons, no new buttons;
   - integrity / foreign keys / audit chain / key check unchanged; no `premigration-v4` pair.
5. Decide the approvals in section C (none is needed for continued manual use).

## F. Known caveats

- **Commission evidence:** secondary (search summaries); verbatim text not retrieved; stays
  UNVERIFIED.
- **Slippage evidence:** none; stays UNVERIFIED.
- **Holding periods:** proposals only (H1 24 h, H4 120 h, D1 480 h); not approved.
- **Exness rollover:** based on a search summary of the Help Centre; seasonal reading
  ambiguous in US/UK clock-gap weeks (handled conservatively); bank holidays not modelled.
- **M-1:** MetaQuotes documents `order_calc_profit` as a price-difference calculation; swap
  cannot be included (no time input); commission is not mentioned; not live-probed.
- **Not tested against a live broker:** the P6.3 reconciliation read (field names `identifier`,
  `ticket`, `position_id` follow MetaQuotes' structures), monitoring, notifications, the
  readiness panel and the new endpoints (the running backend has the P6.2a code).
- **Staged schema v5** is tested on temporary journals only.
- Reconciliation of P5.2 manual trades can reach CANDIDATE_MATCH at best until a user-confirmed
  link path is built.
- Notifications have no acknowledge/presented API yet (service-level only).
- Interim full-suite runs during the night executed while files were being edited; only the
  final run in B.6 is authoritative.
