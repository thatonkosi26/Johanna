# Phase 1 — Correctness and Schema v5 Remediation

Baseline: the independent review of the overnight P6.2b.1 → P6.R work (findings H-1, H-2,
M-1 – M-5, M-10, T-1). Scope: offline corrections only. Production `data/` was not accessed.

## A. Executive verdict

**COMPLETE.** Every finding in scope (H-1, H-2, M-1 – M-5, M-10, T-1) is corrected and
tested offline; schema v5 is corrected and still unregistered. The final verification is three
consecutive clean full-suite runs per Python version (2,050 passed, 1 skipped, 0 failed each).
The first verification set was **not** clean and is reported in full (section D): it exposed
one regression introduced in this phase (fixed) and identified the intermittent failure
(fixed). Nothing is activated; production was not touched.

## B. Finding-by-finding results

### H-1 — Ambiguous reconciliation candidates

- **Original defect:** two unreferenced trades fitting one broker deal each received
  `CANDIDATE_MATCH` for the same deal.
- **Root cause:** deals were excluded only when claimed by a *recorded* reference; candidate
  relationships between unreferenced trades were never compared.
- **Changed:** `src/johanna/reconciliation.py` — `reconcile()` now first computes every
  unreferenced trade's fitting deals (`_fits`) and, per deal, the set of trades it fits; a trade
  is `CANDIDATE_MATCH` only if exactly one deal fits it **and that deal fits no other trade**,
  otherwise `AMBIGUOUS` (detail lists `candidates` and `competing_trades`). New `_complete()`
  guard: an entry fill without a timezone-aware time or a positive volume gives `PENDING`
  ("incomplete logbook evidence") and claims nothing. Nothing is ever linked automatically.
- **Tests:** `tests/test_p6_corrective.py` — two trades / one deal; one trade / one deal; two
  trades / different deals; deal claimed by a recorded reference; several deals / one trade;
  five incomplete-evidence variants; a randomised-shape check that no deal is a unique
  candidate for two trades.
- **Result:** all pass (section D). **Limitation:** an ambiguous candidate's position is still
  reported `UNRECORDED_POSITION` (it is genuinely unlinked); unchanged behaviour.

### H-2 — Swap holding window

- **Original defect:** swap was counted from `evaluated_at` to `evaluated_at + max_hours`,
  but a proposal may be entered until it expires (next candle close) — up to 24 h later for D1.
- **Root cause:** the window ignored the entry-validity period.
- **Changed:** `src/johanna/cost_evidence.py` — new `holding_window(evaluated_at,
  entry_valid_until, max_hours)` → `[evaluated_at, max(entry_valid_until, evaluated_at) +
  max_hours]` (naive times / invalid durations raise). `src/johanna/risk_engine.py`
  `_swap_cost` derives `entry_valid_until = as_of + timeframe` (the same rule as the
  proposal's `valid_until_utc`); if it cannot be established the swap is `unverified` (fail
  closed). The swap view reports `entry_valid_until_utc`.
- **Tests:** H1/H4/D1 windows; D1 validity across the Wednesday triple (old 1 charge → new 4);
  conservativeness for **every permitted entry** sampled every 15 minutes across a rollover, a
  triple, the US DST start, a US/UK gap week and the US DST end; exact start/end boundaries;
  naive/invalid inputs; expired validity never shortens the window; an engine-level D1 test on
  a **pinned clock** (fake MT5 and every service share it) → 4 charges, cost 22.8 USC/lot;
  missing as-of → swap unverified.
- **Limitation:** holidays still not modelled (unchanged, reported per result).

### M-1 — Multi-instrument broker fingerprints

- **Original defect:** one fingerprint per record, but the fingerprint includes the symbol, so
  multi-instrument evidence was rejected for every instrument but one.
- **Design chosen:** a per-instrument mapping on the single record (keeps one evidence
  document = one record, one approval, one audit entry; validation is a set comparison).
- **Changed:** `cost_evidence.CostComponent.broker_terms: {broker symbol: fingerprint}` —
  keys must equal the covered instruments exactly, values must be well-formed fingerprints,
  and it is **required for VERIFIED** evidence. `_component_status` checks each instrument only
  against its own fingerprint (`BROKER_TERMS_UNKNOWN` / `BROKER_TERMS_CHANGED`, fail closed).
  Schema v5 `cost_policy_record.broker_terms TEXT` (JSON object, `json_valid` CHECK) replaces
  `broker_terms_fingerprint`. `cost_policy`: records store the mapping; approval is refused
  unless every covered instrument has a fingerprint; history reports `instrument_terms`
  (MATCH / CHANGED / UNKNOWN / NOT_RECORDED) per instrument, so one changed instrument leaves
  the others usable. Test fixture computes the fake broker's per-instrument fingerprints
  (`fixture_broker_terms`).
- **Tests:** each instrument matches only its own fingerprint (EUR's never validates GBP); one
  of three changing; a three-instrument record verified in the engine for EURUSD, GBPUSD and
  USDJPY; missing/malformed/extra fingerprints refused; expired and out-of-scope evidence;
  registry per-instrument states.

### M-2 — Missing broker identifiers

- **Original defect:** `ref(kind, None)` produced one shared reference for every
  identifier-less object.
- **Changed:** `broker_monitoring._sanitise` validates every identifier (positive `int`, not
  `bool`) **before** any reference is made: position (`identifier`, else `ticket`), order
  `ticket`, deal `ticket`, and the `position_id` of buy/sell deals (non-trade deals such as
  deposits may lack one). Any failure → `MissingIdentifier` → the whole observation is
  `UNAVAILABLE` (`broker_identifier_missing`) and only an empty UNAVAILABLE row is stored.
  Defence in depth: `JournalStore.broker_ref` now refuses `None`, non-int, `bool` and
  non-positive identifiers.
- **Tests:** position without identifier/ticket; deal without ticket; buy deal without
  position; several missing; wrong types (`"555001"`, `1.5`, `True`, `-5`); valid ids and a
  deposit deal; missing HMAC key (no MT5 call); `broker_ref` guard for 7 bad values.

### M-3 — Reconciliation persistence constraints

- **Original defect:** `UNIQUE(observation_id, subject, trade_id, position_ref)` allowed
  duplicates (NULLs are distinct) and `INSERT OR IGNORE` silently dropped CHECK/NOT NULL
  violations.
- **Changed:** schema v5 `reconciliation_result.subject_key TEXT NOT NULL` with
  `CHECK ((subject='trade' AND subject_key=trade_id) OR (subject='broker_position' AND
  subject_key=position_ref))` and `UNIQUE (observation_id, subject, subject_key)`.
  `broker_monitoring._persist_results`: one reconciliation per observation — a repeat returns
  `already_reconciled` and writes nothing (explicit idempotency); plain `INSERT` in one
  transaction; `sqlite3.IntegrityError` rolls everything back and is returned
  (`persistence.status = "failed"`, `last_error = reconciliation_persist_failed`), never
  ignored. A changed logbook is reconciled against a new observation.
- **Tests:** reconcile once / again (idempotent, row count unchanged); database refuses a
  duplicate logical result, `CONFIRMED_OPEN` marked inferred, NULL or mismatched
  `subject_key`, an invalid state, NULL basis, a broker-position row without a reference; a
  corrupted result set is reported `failed` and the valid rows are rolled back; no
  `OR IGNORE` remains in the module.

### M-4 — Valid reconciliation JSON

- **Original defect:** `json.dumps(...)[:2000]` could cut JSON in half.
- **Changed:** `broker_monitoring.bounded_detail()` limits *data* before serialisation: lists
  to `MAX_DETAIL_ITEMS` (20) with `<key>_omitted` counts, strings to 300 characters,
  non-JSON values stringified, and an explicit `"truncated": true`; a final guard replaces an
  over-long result with a small valid summary. Output is always valid JSON ≤ 2000 characters.
- **Tests:** small; exactly at the limit (no truncation); one over; 45 references; 5,000-char
  field; 30 long list items; non-mapping input; datetime/set/nested values; 40 long keys;
  explicit truncation markers; 45 fitting deals persisted and parsed back through `latest()`.

### M-5 — Meaning of a lot

- **Original defect:** `amount_per_lot` did not say how many units a lot is (cent lot 1,000 vs
  standard lot 100,000) — a possible 100× error.
- **Changed:** `CostComponent.lot_units` — required, positive and finite for
  `amount_per_lot`, forbidden otherwise. Assessment compares it with the instrument's verified
  contract size: mismatch → `LOT_SIZE_MISMATCH`, unknown → `LOT_SIZE_UNKNOWN` (never usable).
  No conversion between lot sizes is performed. Conversion steps state the lot basis; the
  approval confirmation now reads e.g. `7 USC/lot RT per 1000 units`. The engine and the
  readiness diagnostic pass the live contract size. Schema v5 `cost_policy_record.lot_units`
  (`CHECK ((kind='commission') = (lot_units IS NOT NULL))`, `lot_units > 0`).
- **Tests:** missing/zero/negative/NaN/inf/bool lot sizes; lot size on non-per-lot evidence;
  1,000 vs 100,000 against both contract sizes; unknown contract; a standard-lot figure never
  becomes actionable in the engine; USC and USD conversions; volume basis and step rounding;
  database refuses commission without `lot_units`.

### M-10 — Intermittent full-suite failures

- **What was observed:** the review's first independent run: `2 failed, 1,951 passed`
  (identities not captured — that capture gap was the reviewer's error and is not repeated
  here). This phase captured every run in full.
- **Identified cause (new evidence):** `tests/test_shadow_api.py::
  test_reads_never_change_state_and_expose_records` enables then disables the real scheduler
  on the **wall clock**. Within 10 minutes after an hourly close, enabling owes that hour's H1
  windows, which disabling closes as `SKIPPED scheduler_disabled`; the test then sees extra
  SKIPPED rows. It failed in this phase's run 2 (Python 3.13, executed at ≈19:00 UTC) and
  the review's failing run executed at ≈18:00 UTC. Reproduced deterministically: clock pinned
  at 19:05 → extra `scheduler_disabled` H1 windows; pinned at 12:30 → none.
- **Second wall-clock hazard found and removed:** the fake MT5's default long swap (−5.7)
  made every fixture-based result depend on the rollover schedule at the time of the run
  (e.g. `test_total_expected_loss_never_exceeds_the_budget` compared cent-rounded amounts that
  varied with the swap count, and H-2 would have made intraday fixtures count a rollover when
  run in the hour before 21:00/22:00 UTC). The second review failure cannot be identified
  after the fact; this is the most likely candidate, stated as an assumption.
- **Changed (tests only):** the shadow-API test pins the scheduler clock (Wednesday 12:30 UTC,
  no window owed); the fake's default swap is 0 (`tests/fake_mt5.py`) and swap tests set it
  explicitly (`risk_fixtures.negative_swap`); the budget test asserts the exact rounding bound
  (≤ ½ cent + per-lot rounding) instead of equality at a half-cent boundary; `VERIFIED_COSTS`
  is approved one hour before import (no clock-trailing test can see it "not yet effective");
  `propose()` resolves its default at call time. No production code was changed for M-10.
- **Verification:** section D (every run listed, including failing ones).

### T-1 — Weak tests

- `test_monitoring_api_is_read_only_and_triggers_no_mt5_read` now wraps the real worker
  runner and asserts that **no job** reached it during the GETs (the vacuous
  `getattr(..., "jobs", None)` comparison is gone).
- `test_stale_mt5_data_is_reported_not_live` → `test_mt5_states_are_reported_exactly`: idle
  is `ok`; an MT5 failure is `unavailable` with its reason, `attention` and the runbook link.
- `test_dashboard_priority_defers_the_background_read` exercises the **real admission**: a
  dashboard read makes the worker recently used, the background observation is deferred
  before any MT5 call (nothing queued), and is admitted after the quiet window.

## C. Schema v5 (staged, still unregistered)

DDL changes (in `johanna.journal_store.SCHEMA_V5` only):

| Table | Change |
|---|---|
| `cost_policy_record` | `broker_terms_fingerprint TEXT` → `broker_terms TEXT CHECK (NULL OR (json_valid AND json_type='object'))`; new `lot_units REAL`; `CHECK ((kind='commission') = (lot_units IS NOT NULL))`; `CHECK (lot_units IS NULL OR lot_units > 0)`; holding records must have `broker_terms IS NULL` |
| `reconciliation_result` | new `subject_key TEXT NOT NULL`; `CHECK` tying it to `trade_id` / `position_ref`; `UNIQUE (observation_id, subject, subject_key)` replaces the NULL-permissive unique key |
| all other v5 tables / triggers / indexes | unchanged (append-only triggers, user-only approval, terminal decisions, notification order) |

Migration tests (temporary journals only; no production copy): v4 journal with decisions,
risk assessment, a trade with its fill and decision link, audit chain → corrected v5: every
v4 table row-for-row identical, key byte-identical, valid `premigration-v4` pair, integrity
`ok`, no foreign-key violations, audit chain valid, new columns present and the old column
absent; a second start runs nothing and makes no new backup. Induced failure
(`SCHEMA_V5 + "CREATE TABLE broken ("`) → `migration_failed`, still v4, no v5 table, every row
identical; the retry succeeds. Missing key / wrong key → `migration_requires_verified_key`
(key never regenerated); corrupt journal → `corrupt_database`, file byte-identical.
`MIGRATIONS[-1]` is still 4; `STAGED_MIGRATIONS == ((5, SCHEMA_V5),)`.

**Not yet ready to register merely because the temporary migration succeeds** — see F.

## D. Test evidence

Environment: Python 3.11.8 (`.venv311`) and 3.13.3 (`.venv`); command for every run
`python -m pytest -q -W error::SyntaxWarning -p no:cacheprovider -rfEs` with
`PYTHONDONTWRITEBYTECODE=1`. Complete logs: scratchpad `ph1_run*_py*.log` (set 1) and
`ph1b_run*_py*.log` (set 2).

**Set 1 (after the corrections, before two test fixes) — NOT clean**

| Run | Python | UTC | Result |
|---|---|---|---|
| 1 | 3.11 | 18:32:55–18:40:32 | 1 failed, 2049 passed, 1 skipped |
| 1 | 3.13 | 18:40:32–18:48:08 | 1 failed, 2049 passed, 1 skipped |
| 2 | 3.11 | 18:48:08–18:55:54 | 1 failed, 2049 passed, 1 skipped |
| 2 | 3.13 | 18:55:54–19:03:33 | **2 failed**, 2048 passed, 1 skipped |
| 3 | 3.11 | 19:03:33–19:11:18 | 1 failed, 2049 passed, 1 skipped |
| 3 | 3.13 | 19:11:18–19:18:59 | 1 failed, 2049 passed, 1 skipped |

Failure 1 — every run (regression from the M-10 fixture change):
`tests/test_browser_risk.py::test_unverified_net_costs_are_prominent_and_never_approved`

```
>   assert _row(page, "costs_per_lot") == "unverified / unverified / unverified"
E   AssertionError: - unverified / unverified / unverified
E                   + unverified / 0.0000 / unverified
tests	est_browser_risk.py:191
```
Cause: with the fake's default swap now zero, the swap is established at 0 instead of
unverified. Fix: the test's fake sets the negative long swap explicitly (its intent unchanged).

Failure 2 — run 2 / Python 3.13 only (the intermittent wall-clock failure, M-10):
`tests/test_shadow_api.py::test_reads_never_change_state_and_expose_records`

```
>   assert [(i["status"], i["reason_code"]) for i in items] == [("SKIPPED", "market_closed")]
E   AssertionError: assert [('SKIPPED', ...er_disabled')] == [('SKIPPED', 'market_closed')]
E     Left contains 3 more items, first extra item: ('SKIPPED', 'scheduler_disabled')
tests	est_shadow_api.py:169
```
Cause and fix: section B, M-10 (reproduced deterministically; pinned clock).

**Set 2 (final code) — clean, three consecutive runs per version**

| Run | Python | UTC | Result |
|---|---|---|---|
| 1 | 3.11 | 19:21:22–19:29:06 | 2050 passed, 1 skipped, exit 0 |
| 1 | 3.13 | 19:29:06–19:36:44 | 2050 passed, 1 skipped, exit 0 |
| 2 | 3.11 | 19:36:44–19:44:24 | 2050 passed, 1 skipped, exit 0 |
| 2 | 3.13 | 19:44:24–19:51:57 | 2050 passed, 1 skipped, exit 0 |
| 3 | 3.11 | 19:51:57–19:59:38 | 2050 passed, 1 skipped, exit 0 |
| 3 | 3.13 | 19:59:38–20:07:14 | 2050 passed, 1 skipped, exit 0 (spans the 20:00 close) |

The single skip in every run is the existing POSIX `chmod` test
(`tests/test_journal_store.py:107`). No unidentified failure remains.

Other checks: `ruff check .` — all checks passed; `ruff format --check .` — 175 files already
formatted; `node --check src/johanna/web/app.js` — OK (frontend unchanged in this phase).
Focused: `tests/test_p6_corrective.py` 97 passed; risk/cost/monitoring/registry/operations
suites passed after each change (e.g. 195 and 198 passed in the intermediate focused runs).
Migration tests (temporary journals): all v4 → corrected v5 tests in section C passed.

Test count: 2,050 = 1,953 (before this phase) + 97 (`test_p6_corrective.py`); no test was
removed. Changed existing tests (all documented above): `fake_mt5.py` (default swap 0),
`risk_fixtures.py` (lot units, per-instrument fingerprints, `negative_swap`, VERIFIED_COSTS
timing, `propose` default), `test_risk_engine.py` (`re` allowed in the pure module),
`test_risk_corrections.py`, `test_risk_safeguards.py`, `test_cost_evidence.py`,
`test_cost_policy.py` (explicit negative swap, new lot/terms fields, exact rounding bound),
`test_broker_monitoring.py`, `test_operations.py` (T-1), `test_shadow_api.py` (pinned clock),
`test_browser_risk.py` (explicit negative swap).

## E. Production safety confirmation

| Item | Status |
|---|---|
| Production `data/` accessed | **No** (no read, no query, no copy in this phase) |
| Production journal schema | Unchanged by this phase — not re-verified, because reading `data/` was prohibited |
| Schema v5 registered | **No** (`MIGRATIONS[-1] == 4`, asserted by tests) |
| Scheduler state changed | **No** (temporary apps only) |
| CostTerms | Unchanged and empty (`COST_TERMS.components == ()`, `holding == {}`) |
| MT5 calls | **None** (only the in-process fake) |
| Broker writes / trading | **None** |
| Notifications | **None** |
| Backend restarted | **No** |
| Git commands | **None** |
| Unrelated data | Not accessed or changed |

## F. Remaining blockers

Resolved in this phase: H-1, H-2, M-1, M-2, M-3, M-4, M-5, M-10, T-1.

Still open from the review (deferred by scope, unchanged):

| Item | Needed next |
|---|---|
| Registering schema v5 (C-4) | Your review of section C, then a separately approved registration with a post-migration check like P6.2a. Optional first: a rehearsal on a temporary copy of the production journal (needs approval to read `data/`). |
| M-6 notification attempt atomicity; L-5 audit of notification states | Implementation, before notifications |
| M-7 automatic journal backups (C-13); M-8 readiness cost | Implementation, before unattended operation |
| M-9 user-confirmed link path; logbook-vs-broker discrepancy check | Implementation, before monitoring activation |
| C-12 evidence recording route / CLI | Your decision and implementation |
| C-1 commission, C-2 slippage, C-3 holding periods, C-5 M-1 probe, C-6 rollover verbatim, C-7 monitoring, C-8 notifications, C-9 registry→risk, C-10 restart, C-11 | Your decisions / evidence |
| L-1 – L-4, L-6 – L-8 | Low-priority clean-ups |

New observations (not defects requiring this phase): the review's second unidentified failure
cannot be named after the fact (most likely the swap-schedule dependence removed here — an
assumption); production state was not re-verified because `data/` access was prohibited.

## G. Approval request

Requested approval for the **next single step only**:

> Review sections B and C, and approve (or reject) **registering the corrected schema v5** in
> `MIGRATIONS` as a separate, bounded step — to be implemented and tested offline, followed by
> your manual restart and a read-only post-migration verification (as for P6.2a).

Nothing further will be done until you decide.
