# Phase 2 — Register Schema v5 and Verify Migration Offline

Scope: register the corrected schema v5 in source code and verify v4 → v5 offline on temporary
SQLite journals only. Production `data/` files were not opened, copied, queried or migrated.
The backend was not restarted; it keeps running its already-loaded code.

> **Important consequence of this phase:** v5 is now in `MIGRATIONS`. The **next start of the
> backend from this source tree will migrate the production journal from v4 to v5** (after a
> verified pre-migration backup). Until you decide on the controlled production migration
> (section G), do not restart the backend from this tree.

## A. Verdict

**COMPLETE — with one open item.** Schema v5 is registered once, after unchanged v1 – v4; the
offline v4 → v5 verification (success, preservation, rollback, refusals, idempotency) passes
on Python 3.11 and 3.13, including inside every full-suite run; production was not touched.
**Open item:** one full-suite run (6 total) had a single intermittent **browser** failure
(`test_browser_risk.py::test_approved_proposal_renders_as_proposed_parameters`, page heading
not rendered within 10 s). It was investigated, did not reproduce in 15 further runs of the
browser tests, and its cause is **not identified** (section D). It is reported, not
dismissed; I found no link to the v5 change, but cannot prove there is none.

## B. Registration details

**Preconditions (checked before any change).** `PHASE1_CORRECTIVE_REMEDIATION_REPORT.md` was read:
verdict COMPLETE; H-1, H-2, M-1 – M-5, M-10 and T-1 each reported corrected with named tests and
six final clean full runs; the corrected v4 → v5 migration passed temporary-journal tests
(preservation, rollback/retry, missing/wrong key, corrupt journal); `MIGRATIONS[-1]` was 4 and v5
was only in `STAGED_MIGRATIONS`. The Phase 1 report asked for your review of its section C
(design, constraints, indexes, triggers, rollback) before registration; this Phase 2 instruction
("register the corrected, reviewed schema v5") is taken as that review and approval. Production
being on v4 is known from the P6.2a post-migration verification and nothing since has migrated
it; it was **not re-checked** here because reading `data/` is prohibited.

**Files changed**

| File | Change |
|---|---|
| `src/johanna/journal_store.py` | `MIGRATIONS` now `(1..4 unchanged, (5, SCHEMA_V5))`, defined after `SCHEMA_V5`; `STAGED_MIGRATIONS` removed; new `migration_sequence_valid()`; `JournalStore._open()` refuses an invalid sequence (`migration_order`) **before** any backup or migration |
| `src/johanna/operations.py` | readiness no longer claims "v5 staged, not registered (a restart does not migrate)"; blocking code `schema_v5_not_registered` → `schema_v5_not_installed` (raised only when the journal is not at v5, e.g. refused) |
| `docs/operations-runbook.md` | section "Staged schema v5" → "Schema v5" (registered; next start migrates after a verified backup; installing the tables enables nothing) |
| `tests/test_p2_schema_v5.py` | **new**, 32 tests (section C) |
| `tests/test_journal_v4.py`, `test_cost_policy.py`, `test_notifications.py`, `test_broker_monitoring.py`, `test_p6_corrective.py`, `test_operations.py`, `test_shadow_api.py` | assertions that encoded "v5 unregistered / fresh journal is v4" updated; tests of the **v4 behaviour** (v3 → v4 migration, `not_installed` on a v4 journal) now build a v4 journal explicitly with `MIGRATIONS[:4]`, so that coverage is kept rather than deleted |

**Registration location:** `src/johanna/journal_store.py`, the `MIGRATIONS` tuple immediately
after `SCHEMA_V5`, entry `(5, SCHEMA_V5)`. It is consumed only by `JournalStore` (default
`migrations=MIGRATIONS`), and applied only inside `JournalStore.start()` → `_open_existing()`:
verified key required → `premigration-v4` backup pair created and validated → v5 applied in one
`BEGIN IMMEDIATE` transaction with `foreign_key_check` before `COMMIT`. Importing the module,
constructing `JournalStore`/`JohannaApplication` or calling `status()` never opens or migrates a
journal (tested).

**v1 – v4 not altered.** The registration was an exact-text replacement of the `MIGRATIONS`
tuple and its comment (script in the scratchpad, `ph2_register.py`); `SCHEMA_V1` – `SCHEMA_V4`
and the `SCHEMA_V5` DDL were not touched. Tests assert `MIGRATIONS[i] == (i+1, SCHEMA_Vi+1)` for
1 – 4 and now **pin SHA-256 prefixes** of all five texts (v1 `30368f37…`, v2 `7c0469c1…`, v3
`e23ae860…`, v4 `847e1af8…`, v5 `1e590842…`) so any later edit of a released migration fails the
suite. Limitation: there is no independent pre-Phase-2 copy of the texts to compare against
(Git use is prohibited and the package is an editable install), so "unchanged before this
phase" rests on the replacement being confined to the tuple.

**Registered exactly once:** `[v for v, _ in MIGRATIONS] == [1, 2, 3, 4, 5]`; a second v5, a gap,
a missing v4 or a sequence not starting at 1 is refused with `migration_order` before any
backup or change (new hardening: previously a duplicate v5 would have committed v5 and only then
refused).

**Activation check:** with the v5 tables installed, nothing starts: monitoring has no thread or
schedule (`observe()` is explicit), nothing calls `notify()`, the cost registry is diagnostic
only (risk keeps the empty production `COST_TERMS`), and the v5 API routes are GET-only and
read-only (tests: no MT5 job, no state change, audit row count unchanged).

## C. Migration verification (temporary journals only)

**Setup.** Each test uses a fresh pytest `tmp_path`; the journal is created by Johanna's own code
with `migrations=MIGRATIONS[:4]` and populated through its services: the shadow scheduler is
enabled, runs one H1 candidate pass (evaluation cycles, gate outcomes, a shadow intent,
decisions, risk assessments) and is disabled again; a decision with a risk assessment; a manual
trade with entry fill, decision link, protection and note (with their idempotency
`write_request` rows); one `event` row; audit entries for all of these; and an HMAC broker
reference computed from the journal key. Every one of the 17 v4 tables is non-empty (asserted).
The migration is then performed by an ordinary `JournalStore.start()` with the **registered
default sequence**, exactly as a backend start would.

**Successful migration.** Schema 4 → 5; journal `ready`; key `verified`; HMAC available; one
valid `premigration-v4` pair whose database is the untouched v4 journal (rows identical, version
4) with the same key. Schema objects 88 → 127.

**Row-count preservation** (rows also compared value-for-value and in order):

| Table | Before | After | Identical |
|---|---:|---:|---|
| `audit_log` | 23 | 23 | yes |
| `decision` | 4 | 4 | yes |
| `decision_observation` | 4 | 4 | yes |
| `evaluation_cycle` | 3 | 3 | yes |
| `event` | 1 | 1 | yes |
| `gate_outcome` | 78 | 78 | yes |
| `meta` | 3 | 3 | yes |
| `risk_assessment` | 4 | 4 | yes |
| `risk_assessment_observation` | 4 | 4 | yes |
| `shadow_intent` | 3 | 3 | yes |
| `shadow_scheduler_setting` | 2 | 2 | yes |
| `trade` | 1 | 1 | yes |
| `trade_decision_link` | 1 | 1 | yes |
| `trade_fill` | 1 | 1 | yes |
| `trade_note` | 1 | 1 | yes |
| `trade_protection` | 2 | 2 | yes |
| `write_request` | 3 | 3 | yes |
| `broker_observation` (new) | — | 0 | n/a |
| `cost_policy_decision` (new) | — | 0 | n/a |
| `cost_policy_record` (new) | — | 0 | n/a |
| `notification` (new) | — | 0 | n/a |
| `notification_delivery` (new) | — | 0 | n/a |
| `notification_state` (new) | — | 0 | n/a |
| `reconciliation_result` (new) | — | 0 | n/a |

**Schema and constraint checks.**
- Exactly the expected objects were added: 7 tables (`cost_policy_record`,
  `cost_policy_decision`, `broker_observation`, `reconciliation_result`, `notification`,
  `notification_state`, `notification_delivery`), 4 indexes, 16 triggers (append-only guards,
  `cost_policy_decision_terminal`, `notification_state_order`), plus SQLite's automatic
  unique/PK indexes.
- Exact column lists of `cost_policy_record` (incl. `broker_terms`, `lot_units`; no
  `broker_terms_fingerprint`) and `reconciliation_result` (incl. `subject_key`).
- Every v4 table, index and trigger SQL is byte-identical after the migration.
- `integrity_check` = ok, `foreign_key_check` empty, `foreign_keys` = 1 on the journal's own
  connections; audit chain valid; HMAC reference identical before and after.
- Append-only still effective: every unconditional `*_no_update` / `*_no_delete` trigger on v4
  tables refuses its statement after migration, and no row changed. On the new tables:
  observation/result updates and deletes refused, the M-3 unique logical key refuses a
  duplicate, and a result referencing a non-existent v4 trade is refused (foreign key).

**Rollback** (four induced failures, each: `migration_failed`, still version 4, the complete
`sqlite_master` identical to before — no partial v5 object — every row identical, key
unchanged, the verified backup kept; then a retry with the registered sequence succeeds with
every v4 row preserved and the audit chain valid):

| Induced failure | Result |
|---|---|
| syntax error after all v5 objects were created | rolled back, retry OK |
| NOT NULL/CHECK failure after all v5 objects | rolled back, retry OK |
| foreign-key violation detected by `foreign_key_check` before `COMMIT` | rolled back, retry OK |
| failure in the middle (after `cost_policy_*`, at `broker_observation`) | rolled back, retry OK |
| crash *after* the v5 `COMMIT` (fault hook) | journal complete at v5 (never half-migrated); next start runs nothing, no new backup |

**Refusal and failure cases** (none recreates, replaces or changes journal data; no backup is
made where the refusal precedes it):

| Case | Reason | Verified |
|---|---|---|
| missing key | `migration_requires_verified_key` | key not recreated; all objects/rows/version identical; no pair |
| invalid key (wrong 32 bytes, 31 bytes, empty) | `migration_requires_verified_key` | key file untouched; journal identical; no pair |
| corrupt journal (not a database) | `corrupt_database` | file byte-identical; key unchanged; no pair |
| failed integrity check (damaged index page, readable header) | `corrupt_database` | file byte-identical; no pair |
| pre-existing foreign-key violation | `migration_failed` | still v4; journal identical (the violation is not hidden) |
| tampered audit chain | `backup_audit_invalid` | still v4; journal identical; no pair |
| unexpected schema version 0 / 6 / 99 | `schema_unsupported` | journal identical; no pair |
| journal file missing, key present | `orphaned_key` | no new journal created; key unchanged |
| invalid sequence (v5 twice, gap, v4 missing, not from 1) | `migration_order` | refused before backup; journal identical |

**Idempotency and registration.** Three further starts after the migration: ready at v5, the
whole database identical, still exactly one pre-migration pair. A fresh journal is created
directly at v5 (`journal.created` audit entry records `schema_version: 5`; scheduler setting
empty = OFF). The runner reports `latest_version == 5` and v5 as the only pending migration for
a v4 journal. Readiness on a migrated app reports installed v5 with monitoring, notifications
and scheduler off; on a refused journal it reports `schema_v5_not_installed`.

## D. Test evidence

Environment: Python 3.11.8 (`.venv311`) and 3.13.3 (`.venv`); `PYTHONDONTWRITEBYTECODE=1`;
command `python -m pytest -q -W error::SyntaxWarning -p no:cacheprovider -rfEs`. Complete logs
(scratchpad): `ph2_focused_.venv311.log`, `ph2_focused_.venv.log`, `ph2_run{1,2,3}_py3{11,13}.log`,
`ph2_repro_*.log`.

**Development runs (before the final runs, not clean — for completeness):** after registering
v5, the journal-related files showed the expected failures from tests that encoded "v5
unregistered / fresh journal is v4" (first `test_journal_v4.py::
test_v3_journal_migrates_to_v4_after_a_verified_backup`: `assert 5 == 4`; then 14 in
`test_cost_policy`, `test_notifications`, `test_broker_monitoring`, `test_p6_corrective`,
`test_operations`, `test_shadow_api`). Each was updated as described in section B. The new
module's first run failed 28/32 because of a fixture error (`add_protection` needs
`effective_utc`), then 1/32 (the integrity-corruption bytes missed the single index cell);
both fixed in the test fixture only.

**Focused migration/journal suite** (14 files: `test_p2_schema_v5`, `test_journal_store`,
`test_journal_v4`, `test_p51_review`, `test_shadow_journal`, `test_shadow_review`,
`test_trade_logbook`, `test_journal_api`, `test_cost_policy`, `test_notifications`,
`test_broker_monitoring`, `test_p6_corrective`, `test_operations`, `test_shadow_api`):

| Python | Result |
|---|---|
| 3.11.8 | 456 passed, 1 skipped, exit 0 |
| 3.13.3 | 456 passed, 1 skipped, exit 0 |

`tests/test_p2_schema_v5.py`: 32 passed on both versions (also inside every full run).

**Full suite — every run (2,083 collected = 2,050 + 32 new + 1 skipped)**

| Run | Python | UTC | Result |
|---|---|---|---|
| 1 | 3.11 | 03:18:35–03:26:37 | 2082 passed, 1 skipped, exit 0 |
| 1 | 3.13 | 03:26:38–03:34:26 | 2082 passed, 1 skipped, exit 0 |
| 2 | 3.11 | 03:34:26–03:42:19 | 2082 passed, 1 skipped, exit 0 |
| 2 | 3.13 | 03:42:19–03:50:20 | **1 failed**, 2081 passed, 1 skipped, exit 1 |
| 3 | 3.11 | 03:50:20–03:58:13 | 2082 passed, 1 skipped, exit 0 |
| 3 | 3.13 | 03:58:13–04:06:06 | 2082 passed, 1 skipped, exit 0 |

The skip in every run is the existing POSIX `chmod` test (`tests/test_journal_store.py:107`).
Every migration and journal test passed in all six runs.

**The failure (run 2, Python 3.13) — complete traceback:**

```
____________ test_approved_proposal_renders_as_proposed_parameters ____________
    def test_approved_proposal_renders_as_proposed_parameters(page, tmp_path) -> None:
        fake = make_fake()
        served, url, app = _serve_risk(tmp_path, fake, envelope(level=BUY_LEVEL), VERIFIED_COSTS)
        try:
>           _open(page, url)
tests	est_browser_risk.py:68:
tests	est_browser_risk.py:50: in _open
    _load(page, url, "risk")
tests	est_browser_smoke.py:185: in _load
    page.wait_for("document.querySelector('.page-heading h1')")
    def wait_for(self, expression: str, timeout: float = 10.0) -> bool:
        ...
>       raise AssertionError(f"Timed out waiting for: {expression} (last={last!r})")
E       AssertionError: Timed out waiting for: document.querySelector('.page-heading h1') (last=False)
testsrowser_harness.py:223: AssertionError
```

**Investigation.**
- What the failure means: the dashboard renders nothing (not even the error page, which also
  has a heading) until `GET /api/dashboard` completes, so for 10 s either that request did not
  complete or rendering threw. The test normally takes ≈0.9 s.
- Link to v5: `/api/dashboard` → `application.dashboard_data()` → `status()`; that path
  reads no v5 table (journal `status()` does no SQL). The new module runs *after*
  `test_browser_risk.py` (alphabetical order), so it cannot have left state behind for it.
  No link found — not proven absent.
- Reproduction attempts (Python 3.13), all passed: `test_browser_risk.py` alone ×8 (64
  tests); all 11 browser files in suite order ×4 (664 tests); `test_browser_risk.py` ×3 with
  timings.
- Hypothesis tested and **not supported**: the test is the module's first navigation in a
  freshly launched browser, and `open()` can return on the initial `about:blank` (already
  `complete`). Measured: the first test's call takes 0.91–0.93 s, no slower than the others,
  so cold start does not explain a >10 s stall.
- Not in any Phase 1 log (12 full runs) or this phase's other five runs.
- Conclusion: **unexplained intermittent browser-harness failure, 1 in 6 full runs here.**
  No test or code was changed for it, and the suite was not rerun until green to replace
  it. Possible next steps (not done, outside scope): capture browser console output and the
  server request log on timeout in `browser_harness.wait_for`, so a recurrence identifies
  itself.

**Other checks.** `ruff check .` — all checks passed. `ruff format --check .` — 177 files
already formatted. `node --check src/johanna/web/app.js` (node v23.9.0) — OK (`app.js`
unchanged; it shows the readiness field `staged_schema`, whose text changed).

## E. Safety confirmation

| Item | Final state | Basis |
|---|---|---|
| Source migration registration | **v5 registered** (`MIGRATIONS[4] == (5, SCHEMA_V5)`) | code + tests |
| Production journal schema | **v4, unchanged by this phase** — not re-read | no production file opened; no backend start; every migration ran on `tmp_path` journals |
| Production journal data | Unchanged by this phase — not re-read | as above |
| Production key and backups | Unchanged by this phase — not re-read | as above |
| Backend | **Not restarted** (still running its previously loaded code, so it is unaffected by the source change) | no stop/start/restart command issued |
| Scheduler | OFF as last verified; not changed | no scheduler API call or production write; test apps only |
| Monitoring | Not activated | no thread/schedule exists; no call made |
| Reconciliation | Not activated | as above |
| Notifications | Not activated | no notification created outside temporary test journals |
| CostTerms | Empty and unchanged (`COST_TERMS.components == ()`) | asserted by `test_cost_policy.py` and `test_p62a_risk.py`; `risk_engine.py` (where it is defined) not edited in this phase |
| Risk policy | Unchanged | no risk-policy file edited |
| MT5 | **No calls** (only the in-process fake used by tests) | — |
| Broker actions | **None** | — |
| Git | **No commands** | — |
| Dependencies | None installed | — |

**Disclosure.** While listing the Python files changed in this phase I ran two `find` commands
from the project root. They excluded `data/` from their *output*, but `find` still walked the
`data/` directory tree (directory entries only). No file in `data/` was opened, read, hashed,
copied or modified, and nothing from it was printed. I should have scoped the search to `src/`
and `tests/`; later searches were.

## F. Remaining blockers and limitations

| Item | Status |
|---|---|
| **Production migration not performed** | By design. Production stays on v4 until you approve a controlled migration; **any restart from this source tree performs it**, so restarts should wait for that decision |
| Production state not re-verified | Requires authorised read access to `data/` (post-migration verification step) |
| v1 – v4 "unchanged before this phase" | Established by the confined edit and now pinned by hashes; no independent pre-phase copy to diff against (Git prohibited) |
| Rehearsal on a copy of the production journal | Not done (prohibited here); optional before the production migration if you want evidence on the real data shape |
| Intermittent browser failure | `test_browser_risk.py::test_approved_proposal_renders_as_proposed_parameters` timed out once (1/6 full runs); cause unidentified (section D). Suggest adding diagnostics capture to the browser harness before relying on browser tests as a gate |
| Still open from the review (unchanged) | M-6/L-5 notification atomicity/audit; M-7 automatic backups (C-13); M-8 readiness cost; M-9 user-confirmed link path and discrepancy check; C-12 evidence recording route; C-1 – C-11 decisions/evidence; L items |

## G. Next approval request

The only approval requested:

> **Approve (or decline) a separate, controlled production migration of the journal from v4 to
> v5**: you stop and start the backend yourself from this source tree (the start performs the
> migration after a verified `premigration-v4` backup), and I then perform a read-only
> post-migration verification (schema version, row counts vs. the backup, integrity, foreign
> keys, audit chain, key status, scheduler OFF, nothing activated), as for P6.2a.

Nothing further will be done until you decide.
