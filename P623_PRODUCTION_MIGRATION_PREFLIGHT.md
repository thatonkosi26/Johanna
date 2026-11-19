# P6.2b.3 — Production Schema v5 Migration: Stage A Preflight

Stage A only: source code, tests and reports were reviewed. **No production journal file,
backup or key was opened, queried or inspected, and no backend API was called.** The only
look at the live system was the operating system's process list (start time of the running
backend). The backend was not stopped, started or restarted.

## 1. Verdict

**READY — on one condition you must accept first (section 2).** The migration path is
verified: the migration needs a verified key, a verified `premigration-v4` backup is made
first, the migration runs in one transaction that rolls back completely on failure, it never
runs twice, and it activates nothing. **The next backend start from this source tree will
migrate the production journal from v4 to v5.**

## 2. Condition: the restart deploys more than the migration

The running backend (PID 23684, `python -m johanna`) started **2026-11-18 16:51 UTC**. These
source files changed after that, so they are not running yet and **all go live on the same
restart**:

| Changed after the backend started | What it brings |
|---|---|
| `application.py`, `api.py`, `notifications.py`, `web/app.js` (≈17:07–17:29 UTC) | wiring of the overnight P6.2b.2 / P6.3 / P6.4 infrastructure: cost registry, broker monitoring and notification services; GET-only `/api/monitoring/*`, `/api/notifications` and readiness routes; dashboard panels |
| `risk_service.py`, `adapters/mt5.py`, `live_market.py` | P6.R background-read admission for the MT5 worker (quiet window, defer not queue); still read-only |
| `risk_engine.py`, `cost_evidence.py`, `reconciliation.py`, `broker_monitoring.py`, `cost_policy.py` (≈18:21–18:24 UTC) | Phase 1 corrections H-1, H-2, M-1 – M-5 |
| `journal_store.py`, `operations.py` (2026-11-19 ≈03:06 UTC) | Phase 2: v5 registration, sequence check, corrected readiness text |

All of it is covered by the full suite (2,082 passed in 5 of 6 Phase 2 runs; one unexplained
browser-test timeout, see risks) and none of it starts a job, an MT5 read schedule, a
notification or an order. But the restart is a **code deployment plus migration**, not a
migration alone; the earlier restart decision C-10 is effectively decided by this restart. If
you do not want those changes live yet, the verdict becomes NOT READY.

Consequence for rollback: once the journal is v5, **older code refuses it**
(`schema_unsupported`, fail closed). Going back to older code would require restoring the
`premigration-v4` pair (manual procedure, section 5), losing anything recorded after it.

## 3. Exact startup trigger

`python -m johanna` → `johanna/__main__.py: main()`:

1. `AppConfig.load()`; `JohannaApplication(config)` — constructs `JournalStore(data_dir)` with
   the default `migrations=MIGRATIONS` (now v1 – v5). **Construction opens nothing.**
2. `make_server(...)` binds the port first (if the port is in use: exit, nothing started).
3. `application.start()` → instance lock (a second backend refuses) →
   `ServiceManager.start()` → `JournalStore.start()` → `_open()`:
   - `migration_sequence_valid(MIGRATIONS)` (1..5, each once) else `migration_order`;
   - existing database → `_open_existing()`: `integrity_check`, `user_version` (must be
     1 – 5), `meta.key_check` read; key verified; **pending = [v5]** → backup → validate →
     `_apply_migration(conn, 5, SCHEMA_V5)`.
4. Then preferences, market-monitoring preference sync (the existing market-data collector,
   unrelated to broker monitoring), and `shadow.start()` (resumes only if the stored state
   is enabled).

`JournalStore.start()` never raises: a refused journal leaves the rest of the backend
running, with journal features refusing.

## 4. Key verification

- Migration requires `key_status == "verified"`: key file present, exactly 32 bytes, its
  check value equals `meta.key_check` (constant-time compare), user-only ACL. Otherwise
  `migration_requires_verified_key` — **no backup, no migration, the key is never
  regenerated**.
- Tested offline: missing key; wrong 32 bytes; 31 bytes; empty file.

## 5. Backup creation and verification (before any schema change)

`backup("premigration-v4")`:

1. Private temp directory `trading_journal.tmp-*` in `data/`.
2. SQLite online backup API → consistent snapshot (WAL-safe), converted to a single file.
3. Snapshot `integrity_check` = ok; audit chain verified (`backup_audit_invalid` otherwise).
4. Key copied; its check value must match the snapshot's `meta.key_check`
   (`backup_key_mismatch`); user-only ACLs.
5. `pair.json` manifest: kind, time, `schema_version: 4`, key check, SHA-256 of both files.
6. Published by one rename to `trading_journal.backup-premigration-v4-<UTC stamp>`.
7. `validate_pair()` re-verifies name, manifest, hashes, key pairing, integrity and audit
   chain — failure → `backup_unverified`, **no migration**.

Any failure removes only that attempt's temp directory; the live journal is untouched.
Premigration pairs are never pruned. Space needed: roughly one more copy of the journal file
in `data/` (size not checked: reading `data/` is not permitted in Stage A).

Manual restore (runbook, only on your instruction): stop the backend, move the live journal
and its `-wal`/`-shm` aside, copy the pair's journal and key into `data/`, start.

## 6. Rollback behaviour

`_apply_migration`: `foreign_keys` OFF outside the transaction → `BEGIN IMMEDIATE` → every
v5 statement (a statement ending the transaction is refused) → `foreign_key_check` must be
empty → `user_version = 5` → `COMMIT`. Any SQLite error → `ROLLBACK` → `migration_failed`;
the journal stays exactly v4 (schema and rows), the verified backup remains. Tested offline
with four induced failures (start, middle, after all objects, foreign-key violation) and a
crash after `COMMIT` (journal complete at v5). Note: after `migration_failed`, **a later
start retries automatically** — per your failure rules, do not restart again; report instead.

Other refusals that never recreate or replace anything: `corrupt_database` (incl. failed
integrity check), `schema_unsupported`, `backup_*`, `orphaned_key` (journal missing, key
present), `restore_required` (journal missing, backups present), `stale_sidecars`. A new
journal and key are created **only** when no journal, key, backup or sidecar exists at all.

## 7. No second migration

At v5, `pending` is empty: no backup, no SQL. Tested: three further starts leave the
database identical and one premigration pair.

## 8. Expected post-migration schema

- `PRAGMA user_version = 5`.
- All 17 v4 tables unchanged (rows and SQL): `audit_log`, `decision`,
  `decision_observation`, `evaluation_cycle`, `event`, `gate_outcome`, `meta`,
  `risk_assessment`, `risk_assessment_observation`, `shadow_intent`,
  `shadow_scheduler_setting`, `trade`, `trade_decision_link`, `trade_fill`, `trade_note`,
  `trade_protection`, `write_request`.
- 7 new, **empty** tables: `cost_policy_record`, `cost_policy_decision`,
  `broker_observation`, `reconciliation_result`, `notification`, `notification_state`,
  `notification_delivery`.
- 4 new indexes: `cost_policy_record_by_kind`, `cost_policy_decision_by_record`,
  `broker_observation_by_time`, `reconciliation_result_by_trade` (+ SQLite automatic
  unique/PK indexes).
- 16 new triggers: append-only guards on all 7 tables, `cost_policy_decision_terminal`,
  `notification_state_order`.
- The migration writes no row and **no audit entry** (as for v4): the audit chain is
  unchanged by it.

## 9. Expected operational state after the restart

| Item | Expected | Why |
|---|---|---|
| Shadow scheduler | OFF | last stored state OFF at the last verification (P6.2a/P6.1.1); the migration does not write `shadow_scheduler_setting`; `shadow.start()` resumes only if enabled |
| Broker monitoring / reconciliation | inactive | no thread or schedule exists; `observe()`/`reconcile()` are only called explicitly; GET routes read-only |
| Notifications | none | nothing calls `notify()`; local inbox adapter only; no external delivery |
| Cost evidence | none recorded or approved | v5 cost tables empty; registry diagnostic only |
| Production CostTerms | empty | `risk_engine.COST_TERMS = CostTerms()`; proposals stay UNVERIFIED on costs |
| Execution | disabled | `DisabledExecutionService` refuses every request; shadow intents never become orders |
| MT5 | read-only | worker reads on dashboard requests only (as before), with background admission |

## 10. Unresolved risks and blockers

| Risk | Assessment |
|---|---|
| Bundled code deployment (section 2) | **Needs your acceptance** — the only condition on READY |
| Rollback to older code after v5 | Refused by design; recovery = manual restore of the premigration pair |
| Production state not re-checked in Stage A | Schema v4, scheduler OFF, key verified are from the last verification; Stage B checks them |
| Free disk space for the backup | Not checked; a lack of space fails safely (`backup_failed`, no migration) |
| Intermittent browser-test timeout (Phase 2, 1 in 6 runs, cause unknown) | Test-harness symptom; no link to the migration found; not a migration blocker |
| Uncommitted working tree | Every change since `dea1208` is uncommitted; committing before the restart would give a code rollback point (I may not run Git) |
| `migration_failed` auto-retry on the next start | Do not restart again after a failure; report it |

## 11. What happens next

When you are ready: stop the running backend and start it again from this tree
(`python -m johanna`, with your usual configuration). That start performs the v4 → v5
migration. Then tell me to continue with Stage B (read-only verification). I will not
restart anything or open the production journal before that.
