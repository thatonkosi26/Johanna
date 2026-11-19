# P6.2b.6b — Controlled Production Schema v6 Migration

**Result: COMPLETE.** The production journal migrated from schema v5 to **v6** through the
normal startup path when you restarted the backend. A verified `premigration-v5` backup was
created first, every pre-existing record is preserved, all integrity checks pass, and every
operational capability remains inactive. Verification was read-only and created no files.

## 1. Pre-start checks (before your restart; no production file opened)

| Check | Result |
|---|---|
| Project directory | `C:\Users\thato\Downloads\Johanna` (the backend's working directory, P6.2b.6a.1) |
| Production journal path | `C:\Users\thato\Downloads\Johanna\data\trading_journal.sqlite3` (`data_dir = "data"`, `JOHANNA_DATA_DIR` unset) |
| Registered migrations | v1 – v6, each once, in order; `STAGED_MIGRATIONS` absent; SHA-256 prefixes equal the pins (v1 `30368f37…`, v2 `7c0469c1…`, v3 `e23ae860…`, v4 `847e1af8…`, v5 `1e590842…`, v6 `32420500…`) |
| Reviewed P6.2b.4 / P6.2b.5 code present | yes (v6 registration; explicit rollback and before-commit hook in `_apply_migration`) |
| Backup before migration | `_open_existing` makes `premigration-v{version}` and runs `validate_pair` before `_apply_migration` |
| No source change since review | 0 files in `src/` modified after 08:42 UTC; SHA-256 manifest of all 68 source files recorded |

## 2. User-confirmed restart

- Instructions given: orderly stop (Ctrl+C) of PID 26716; in the project directory clear
  `JOHANNA_DATA_DIR`; start with
  `C:\Users\thato\AppData\Local\Programs\Python\Python313\python.exe -m johanna`.
- Your startup output: `{"timestamp": "2026-11-19T10:34:48.665446+00:00", "level": "INFO",
  "logger": "johanna", "message": "Johanna backend started", "host": "127.0.0.1",
  "port": 8765}` — no refusal line reported.
- New process (OS process list): **PID 27836**, started 10:34:47 UTC, executable the global
  Python 3.13.3, command line `python.exe -m johanna`. The source tree is byte-identical to the
  pre-start manifest (68 files), so the deployed code is the reviewed P6.2b.4 / P6.2b.5 code.

## 3. Migration result

| Item | Result |
|---|---|
| Previous schema | 5 (backup manifest and backup `user_version`) |
| Final schema | **6** (`PRAGMA user_version`; backend journal status `schema_version: 6`) |
| Migration completed | **yes** — journal `ready`, no refusal reason |
| Backup | exactly **one** pair: `trading_journal.backup-premigration-v5-20261119T103448249332Z` (created 10:34:48.25 UTC, during startup) |

## 4. Backup verification

- `validate_pair`: **true** (published name, manifest `verified`, both file hashes, key pairing,
  integrity, audit chain).
- Manifest: kind `premigration-v5`, `schema_version` 5.
- Backup database: `user_version` 5; `integrity_check` ok; 0 foreign-key violations; audit chain
  valid (4,604 rows); schema identical to the registered v1 – v5 definitions.
- Pair key byte-identical to the live key; its check value matches the backup's stored key
  check.

## 5. Integrity, foreign keys, key, audit

| Check | Production journal |
|---|---|
| `integrity_check` | **ok** |
| `foreign_key_check` | **0 violations** |
| Key | **verified** — 32 bytes, check value equals the journal's stored key check, user-only ACL; backend `key_status: verified`, `hmac_available: true` |
| Audit chain | **valid**, 4,608 rows; the backup's 4,604-row chain is an identical prefix |
| Backend readiness (GET `/api/operations/readiness`) | `manual_operation: ok`; installed 6 = registered 6; `journal_ready`, `journal_integrity`, `foreign_keys`, `audit_chain`, `journal_key` all ok |

## 6. Existing data preservation

Every v1 – v5 table compared with the `premigration-v5` backup by primary key, column by column:

| Table | Backup | Journal | Missing | Changed |
|---|---:|---:|---:|---|
| `audit_log` | 4,604 | 4,608 | 0 | none (4 new entries after the migration) |
| `decision` | 53 | 53 | 0 | none |
| `decision_observation` | 53 | 53 | 0 | 1 row: `observations`, `last_seen_utc` |
| `risk_assessment` | 19 | 19 | 0 | none |
| `risk_assessment_observation` | 19 | 19 | 0 | 1 row: `observations`, `last_seen_utc` |
| `evaluation_cycle` | 6 | 6 | 0 | none |
| `gate_outcome` | 60 | 60 | 0 | none |
| `meta` | 3 | 3 | 0 | none |
| `shadow_scheduler_setting` | 4 | 4 | 0 | none |
| `event`, `shadow_intent`, `trade`, `trade_fill`, `trade_protection`, `trade_note`, `trade_decision_link`, `write_request` | 0 | 0 | 0 | — |
| `notification`, `notification_state`, `notification_delivery`, `broker_observation`, `reconciliation_result`, `cost_policy_record`, `cost_policy_decision` | 0 | 0 | 0 | — |

The only differences are normal operation **after** the migration: the open dashboard repeated
already-captured analyses, recorded as the permitted, audited observation updates (4 audit
entries `observation.updated`, actor `johanna`, the first at 10:35:14 UTC, after the 10:34:48
backup). No backup row is missing and no immutable value changed.

## 7. New v6 objects

- Schema identical to a schema built in memory from the registered v1 – v6 SQL: **no missing and
  no unexpected object**. Totals (incl. SQLite automatic indexes): 29 tables, 69 indexes,
  65 triggers.
- The five v6 tables exist and are **empty**: `notification_claim`, `trade_broker_link`,
  `reconciliation_discrepancy`, `evidence_record`, `evidence_link`; with the 7 v6 indexes
  (incl. `notification_delivered_once`) and 13 v6 triggers (incl.
  `notification_delivery_needs_claim`, `trade_broker_link_exclusive`,
  `trade_broker_link_revocation`).

## 8. Operational service states

| Service | State | Evidence |
|---|---|---|
| Shadow scheduler | **OFF** | last setting disabled; `/api/shadow/status` `enabled false`, no next window |
| Monitoring / reconciliation | **inactive** | `/api/monitoring/status` `enabled false`, installed, no observation; 0 observation / result rows; `/api/reconciliation/links` 0 links, confirmation endpoint inactive, `automatic_linking false` |
| Notifications | **inactive** | 0 notifications; `automatic_notifications false`; external delivery none (claims now installed, unused) |
| Automatic backups | **disabled** | `BackupPolicy(enabled=False)` in the application; no scheduler exists |
| Evidence recording | **inactive** | 0 evidence rows; nothing records automatically |
| Production CostTerms | **empty** | `COST_TERMS = CostTerms()`; readiness `cost_terms_configured false`; 0 cost records |
| Risk policy | **unchanged** | no policy file or setting changed |
| Autonomous execution | **disabled** | `/status` `autonomous_execution_active false`; broker execution `disabled` |
| Readiness | unattended operation **not permitted** (blocking: scheduler disabled, costs unverified, monitoring and notifications not activated, execution disabled) | `/api/operations/readiness` |

## 9. Deviations and limitations

- Method: the journal had no WAL sidecars after the orderly restart, so it was read with SQLite
  `immutable=1` (creates no files) under a guard — WAL absent and journal size and modification
  time identical before and after the read (both held). **No sidecar was created** (confirmed
  absent afterwards); the backup pair was read with `mode=ro` (a rollback-journal file, no
  sidecars).
- The pair was located with a glob restricted to `trading_journal.backup-premigration-v5-*`
  (the minimum needed to confirm exactly one); no other `data/` entry was listed or read.
- Backend reads used only GET routes that are read-only by design and test (`/status`,
  `/api/shadow/status`, `/api/monitoring/status`, `/api/notifications`,
  `/api/reconciliation/links`, `/api/operations/readiness`).
- Cosmetic: the readiness `staged_schema` text still says "a v5 journal migrates at the next
  start", which is a general statement and now redundant with installed = registered = 6.
- The journal key was read in memory to compare check values; it was not printed or copied.

## 10. Authorisation statement

Only the controlled v5 → v6 migration (through your restart), the deployment of the reviewed
P6.2b.4 / P6.2b.5 code, and read-only verification were performed. **No autonomous execution and
no service activation was authorised or performed.** I did not start, stop or restart the
backend, run SQL migrations, modify the journal, key, backups or sidecars, change CostTerms,
risk policy, preferences or scheduler, call MT5, act on the broker, run Git, or change source or
tests.

**Recovery note:** older code refuses a v6 journal; if recovery were ever needed it is the
documented manual restore of the verified `premigration-v5` pair, with your separate approval.
