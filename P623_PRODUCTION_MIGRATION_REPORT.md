# P6.2b.3 — Production Schema v5 Migration: Report

Stage B (read-only verification) after your manual restart. The backend process restarted at
**2026-11-19 04:40:55 UTC** (PID 15632, `python -m johanna`, default configuration, so
`data/` and port 8765). Verification ran 04:42 – 04:45 UTC.

## A. Verdict

**COMPLETE.** The production journal is at schema v5 with exactly the registered schema, a
valid verified `premigration-v4` backup exists, every v4 record present before the
migration is preserved, every integrity check passes, and every operational safeguard
remains off.

## B. Migration result

| Item | Result |
|---|---|
| Previous schema version | 4 (backup manifest and backup `user_version`) |
| Final schema version | **5** (`PRAGMA user_version`; backend journal status `schema_version: 5`) |
| Migration completed | **Yes** — journal `ready`, reason none |
| Pre-migration backup | `trading_journal.backup-premigration-v4-20261119T044056612355Z` — `validate_pair` **true** (manifest, both file hashes, key pairing, integrity, audit chain); manifest `schema_version 4`, `verified true`; exactly one premigration-v4 pair; no leftover temp directory |
| Schema compared with the registered v1 – v5 SQL (built in memory) | **identical**: no missing object, no extra object, no object whose SQL differs (24 tables, 51 indexes, 52 triggers incl. SQLite automatic indexes) |
| Backup schema compared with the registered v1 – v4 SQL | identical; no v5 table in the backup |

New v5 objects present: tables `cost_policy_record`, `cost_policy_decision`,
`broker_observation`, `reconciliation_result`, `notification`, `notification_state`,
`notification_delivery` (all **0 rows**); indexes `cost_policy_record_by_kind`,
`cost_policy_decision_by_record`, `broker_observation_by_time`,
`reconciliation_result_by_trade`; all 16 v5 triggers.

## C. Data preservation

Every v4 table, compared by primary key and column by column (backup = journal at
04:40:56 UTC, just before the migration):

| Table | Backup rows | Production rows | Backup rows missing | Changed values | Result |
|---|---:|---:|---:|---|---|
| `audit_log` | 4,558 | 4,567 | 0 | none | preserved (backup chain is an identical prefix); 9 new entries **after** the migration, see note |
| `decision` | 50 | 50 | 0 | none | preserved, identical |
| `decision_observation` | 50 | 50 | 0 | 1 row: `observations`, `last_seen_utc` | preserved; the change is post-migration operation, see note |
| `evaluation_cycle` | 6 | 6 | 0 | none | preserved, identical |
| `event` | 0 | 0 | 0 | — | identical (empty) |
| `gate_outcome` | 60 | 60 | 0 | none | preserved, identical |
| `meta` | 3 | 3 | 0 | none | preserved, identical (incl. key check) |
| `risk_assessment` | 15 | 15 | 0 | none | preserved, identical |
| `risk_assessment_observation` | 15 | 15 | 0 | none | preserved, identical |
| `shadow_intent` | 0 | 0 | 0 | — | identical (empty) |
| `shadow_scheduler_setting` | 4 | 4 | 0 | none | preserved, identical |
| `trade` | 0 | 0 | 0 | — | identical (empty) |
| `trade_decision_link` | 0 | 0 | 0 | — | identical (empty) |
| `trade_fill` | 0 | 0 | 0 | — | identical (empty) |
| `trade_note` | 0 | 0 | 0 | — | identical (empty) |
| `trade_protection` | 0 | 0 | 0 | — | identical (empty) |
| `write_request` | 0 | 0 | 0 | — | identical (empty) |

**Note — activity after the migration, not caused by it.** The open dashboard kept running
analyses after the restart; Johanna recorded each repeat of an already-captured analysis as an
observation update (the permitted, audited update of a decision observation's `observations`
count and `last_seen_utc`). At 04:42 UTC that was 9 audit entries (`observation.updated`,
actor `johanna`, first at 04:41:15 UTC, after the 04:40:56 migration) and one observation's
count 84 → 94 with `last_seen_utc` moving from 04:40:46 to 04:43:01 by a second read. The
numbers keep growing while the dashboard is open, which is expected. Every immutable value is
unchanged; no backup row is missing; no v4 row was altered by the migration.

Foreign keys: `foreign_key_check` returns no violation on either the production journal or the
backup.

## D. Integrity checks

| Check | Production journal | Backup |
|---|---|---|
| SQLite `integrity_check` | **ok** | **ok** |
| `foreign_key_check` | **0 violations** | **0 violations** |
| Audit chain | **valid** (4,567 rows) | **valid** (4,558 rows) |
| Journal key | **verified** (32 bytes; its check value equals `meta.key_check`; backend `key_status: verified`) | pair key byte-identical to the live key; its check matches the backup's `meta.key_check` |
| HMAC availability | **available** (backend `hmac_available: true`, prerequisite verified) | — |
| Append-only protections | all v4 and v5 triggers present with SQL **identical** to the registered definitions | — |

Append-only was verified **structurally**, not by attempting writes: writing to the production
journal is prohibited. The identical trigger SQL was tested functionally on temporary journals
in Phase 2 (every guard refuses its UPDATE/DELETE after migration).

## E. Operational state

| Item | State | Evidence |
|---|---|---|
| Shadow scheduler | **OFF** | last setting `enabled = 0` (user, 2026-11-18 07:11 UTC); `/api/shadow/status`: `enabled false`, `state disabled`, no next window |
| Broker monitoring | **not active** | `/api/monitoring/status`: `enabled false`, `read_only`, `installed true`, no observation, no error; `broker_observation` 0 rows |
| Reconciliation | **not active** | `/api/monitoring/reconciliation`: no observation, 0 results; `reconciliation_result` 0 rows |
| Notifications | **none, not active** | `/api/notifications`: 0 notifications, `automatic_notifications false`, external delivery none; notification tables 0 rows |
| Cost evidence | **none recorded or approved** | `cost_policy_record` and `cost_policy_decision` 0 rows |
| Production CostTerms | **empty** | `risk_engine.COST_TERMS = CostTerms()` (code unchanged; no configuration written) |
| Risk policy | **unchanged** (no policy value or setting changed) | — |
| Shadow / trade intents | **none created** | `shadow_intent` 0 before and after |
| Execution | **disabled** | `/status`: broker execution `disabled`, `autonomous_execution_active false`; shadow `execution none` |
| Broker actions / MT5 | **none by me** | no MT5 call or broker connection made in Stage B |

## F. Deviations and limitations

- **Production access (permitted minimum, read-only):** `data/trading_journal.sqlite3` opened
  with SQLite `mode=ro`; `data/trading_journal.key` read to compare check values (never
  printed or copied); the `trading_journal.backup-premigration-v4-*` pair (read via
  `validate_pair` and a `mode=ro` connection); globs limited to `trading_journal.backup-*` and
  `trading_journal.tmp-*`. No other `data/` content was listed or read. Nothing was written.
- **Backend reads:** GET `/status`, `/api/shadow/status`, `/api/monitoring/status`,
  `/api/monitoring/reconciliation` and `/api/notifications` — read-only routes with no MT5
  call and no state change. A first attempt printed nothing useful because Git Bash rewrote
  the path argument of my display script; the requests were repeated read-only.
- **Append-only** checked structurally only (see D).
- The process start time and the restart itself are as observed from the operating system;
  how the restart was performed is yours.
- No error, discrepancy or unexpected file was found.

## G. Remaining blockers (not addressed here)

| Item | Status |
|---|---|
| Code rollback | Older code now refuses this v5 journal by design; the verified `premigration-v4` pair is the recovery point. Committing the current tree (uncommitted since `dea1208`) would give a code rollback point |
| Intermittent browser-test timeout (Phase 2) | cause unknown; test-harness only |
| M-6/L-5 notification atomicity/audit; M-7 automatic backups (C-13); M-8 readiness cost; M-9 user-confirmed linking and discrepancy check; C-12 evidence recording | implementation needed before the related activation |
| C-1 commission, C-2 slippage, C-3 holding periods, C-5 probe, C-6 rollover, C-7 monitoring, C-8 notifications, C-9 registry→risk, C-11 | your decisions / evidence |
| Activation of P6.3 monitoring, P6.4 notifications, P6.R operations, cost approval, execution | not started; each needs your separate approval |
