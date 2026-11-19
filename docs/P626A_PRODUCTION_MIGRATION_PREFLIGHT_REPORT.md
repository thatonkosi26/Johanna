# P6.2b.6a — Production Schema v6 Migration Preflight (Read-Only)

## 1. Overall verdict

**READY FOR EXPLICIT MIGRATION APPROVAL — with two disclosures you should weigh first.**

Every required check passed (section 4). This is a recommendation only, not permission to
migrate or restart. **The production journal is on schema v5** (read-only evidence).

1. **Deviation (my action):** opening the journal with SQLite's read-only mode (`mode=ro`)
   created the two standard WAL sidecar files next to it — `trading_journal.sqlite3-wal`
   (0 bytes) and `trading_journal.sqlite3-shm` (32 KB index), both timestamped 10:24:47.08 UTC,
   the instant my connection opened. The journal file itself was not modified (size 2,383,872
   bytes and last write 09:56:32 UTC both unchanged; no data written). Section 9.
2. **Backend restarted outside this session:** the running backend is **PID 26716, started
   06:30:21 UTC** — not the process verified after the v5 migration (PID 15632, 04:40:55 UTC).
   I did not start or stop any process. 06:30 UTC was during P6.2b.4 development and before v6
   was registered (≈08:40 UTC), so that start could not migrate to v6 (confirmed: journal v5),
   but the running code is whatever the source tree held at 06:30 UTC. My P6.2b.5 report's
   statement that the running backend predates P6.2b.4 is therefore wrong.

## 2. Timestamp and environment

- Checks run 2026-11-19 10:24:31 – 10:25:12 UTC; Windows 11; Python 3.13.3 (`.venv`, isolated
  mode `-I`), SQLite via the standard library.
- Production paths used — only those defined by the application configuration (`AppConfig`
  default `storage.data_dir = "data"`; the backend runs as `python -m johanna` with no
  `--config`): `data/trading_journal.sqlite3`, its `-wal`/`-shm` sidecar paths (metadata),
  `data/trading_journal.key`, and the `data/` volume statistics. The journal's last write
  (09:56:32 UTC) while this backend was running and the audit growth since P6.2b.3 (4,567 →
  4,604 rows) are consistent with the backend using this directory. `JOHANNA_DATA_DIR` in the
  backend's environment could not be read (limitation, section 9).
- No directory was listed or traversed; no backup pair was opened.

## 3. Checks performed and methods

| Check | Method |
|---|---|
| Migration sequence, pins, staging | import of `johanna.journal_store` (no `JournalStore` created); SHA-256 of each migration text; `migration_sequence_valid`; `hasattr(STAGED_MIGRATIONS)` |
| Startup / backup / transaction behaviour | source review of `__main__.main`, `JournalStore._open`, `_open_existing`, `backup`, `validate_pair`, `_apply_migration` (not executed) |
| Journal state | `sqlite3.connect("file:…?mode=ro", uri=True)` + `PRAGMA query_only = ON`: `user_version`, full `integrity_check`, `foreign_key_check`, full audit-chain verification (`verify_audit_rows`), schema compared with a v1 – v5 schema built in memory from the registered SQL, presence of any v6 object name, aggregate row counts, notification delivery aggregates, last scheduler setting |
| Key | the application's own read-only procedure: length 32, `key_check_value(key) == meta.key_check`, `verify_user_only` (an `icacls` query); key bytes kept in memory only, never printed |
| Disk | `shutil.disk_usage("data")` (volume statistics), `is_dir`, `os.access(W_OK)` (metadata), file sizes via `stat` on the exact paths |
| Backend state | Windows process list (PID and start time) before and after |

## 4. Results

| Check | Result |
|---|---|
| Registered migration sequence | **PASS** |
| Production schema version | **v5** (`user_version = 5`; schema identical to registered v1 – v5) |
| Journal integrity | **PASS** (`integrity_check` = ok) |
| Foreign-key integrity | **PASS** (0 violations) |
| Key verification | **PASS** (length, key check against `meta`, user-only ACL) |
| Audit-chain verification | **PASS** (4,604 rows, valid) |
| Notification delivery uniqueness | **PASS** (0 notifications, 0 deliveries) |
| Other v6 constraints/refusal conditions | **PASS** |
| Backup destination | **VERIFIED** |
| Free disk space | **SUFFICIENT** |
| Backend state | **UNCHANGED** during this phase (PID 26716 before and after; see disclosure 2 for the earlier restart) |

## 5. Schema and migration-sequence verification

- `MIGRATIONS` = versions 1, 2, 3, 4, 5, 6, each once, in order; `migration_sequence_valid` true;
  `STAGED_MIGRATIONS` absent.
- SHA-256 prefixes: v1 `30368f372a6e52a9`, v2 `7c0469c15de91678`, v3 `e23ae860ed51fc9c`,
  v4 `847e1af8380a40de`, v5 `1e59084258cbea09` — equal to the pinned values; v6
  `3242050074b27636` — equal to the P6.2b.5 registered and pinned definition.
- Startup path: `python -m johanna` → bind port → `application.start()` → instance lock →
  `ServiceManager.start()` → `JournalStore.start()` → `_open()`: sequence check →
  `integrity_check` → `user_version` (5, supported) → key verified → pending `[6]` →
  `backup("premigration-v5")` → `validate_pair` → `_apply_migration(6)`.
- The migration refuses on: invalid sequence (`migration_order`), integrity failure
  (`corrupt_database`), unsupported version (`schema_unsupported`), unverified key
  (`migration_requires_verified_key`), backup failure/verification (`backup_failed`,
  `backup_unverified`, `backup_audit_invalid`, `backup_key_mismatch`). The migration is one
  `BEGIN IMMEDIATE` transaction with `foreign_key_check` before `COMMIT` and explicit rollback
  on any exception (`migration_failed` / `unexpected_error`).

## 6. Journal integrity, key and audit

- Journal exists and is readable; `user_version` 5; application schema exactly the registered
  v1 – v5 schema (no missing or extra object); no v6 object name already present.
- `integrity_check`: ok. `foreign_key_check`: 0 violations.
- Key: present, 32 bytes, check value matches the journal's stored key check, ACL user-only.
- Audit chain: valid, 4,604 rows, no problem reported.
- Aggregate row counts: `audit_log` 4,604; `decision` 53; `decision_observation` 53;
  `risk_assessment` 19; `risk_assessment_observation` 19; `evaluation_cycle` 6; `gate_outcome`
  60; `meta` 3; `shadow_scheduler_setting` 4 (latest: disabled); every other table 0 (incl.
  `trade*`, `shadow_intent`, `event`, `write_request`, all v5 cost / broker / reconciliation /
  notification tables).

## 7. Notification uniqueness

`notification` 0 rows; `notification_delivery` 0 rows; successful deliveries 0; notifications
with more than one success **0**. The v6 unique index `notification_delivered_once` can be
created. The new trigger `notification_delivery_needs_claim` applies only to future inserts.
Other v6 data dependencies: none — v6 is otherwise additive (new tables, indexes and triggers);
its foreign keys reference existing tables only through new, empty tables; no existing object
uses a v6 name.

## 8. Backup destination and disk space

- Destination: the journal's own directory (`data/`); the pair is built in a temporary
  `.trading_journal.backup-tmp-*` directory there and published by a same-volume rename as
  `trading_journal.backup-premigration-v5-<UTC stamp>`. The directory exists and is writable by
  metadata (no write attempted).
- Required space: journal 2,383,872 bytes, no WAL content at sizing time, key 32 bytes →
  minimum ≈ 2.4 MB; with the M-7 policy margin (2 × size + 64 MB) ≈ 72 MB.
  Free: 12,294,017,024 bytes (≈ 12.3 GB) of 510,770,802,688. **Sufficient.**
- Validation: `JournalStore.backup` checks the snapshot's integrity, audit chain and key
  pairing before publishing; `validate_pair` re-verifies manifest, both file hashes, key check,
  integrity and audit chain before any migration statement runs.
- WAL/SHM: the backup uses SQLite's online backup API, which takes a consistent snapshot
  including committed WAL content, and converts the copy to a single file. The sidecars now
  present (section 9) are SQLite's normal WAL files; they do not affect the backup or the
  migration, and the journal's `_open` checks sidecars only when the database file is missing.

## 9. Limitations and deviations

- **Sidecar creation (deviation).** At sizing time neither `-wal` nor `-shm` existed (the
  backend had no connection open). SQLite's read-only mode on a WAL-mode database created them
  when my connection opened (0-byte WAL, 32 KB index). No journal data was written; the journal
  file is unchanged. I did **not** delete them: removing a WAL index that the running backend
  may open at any moment would be riskier than leaving files SQLite creates and manages itself.
  They will be reused or removed by SQLite in normal operation. In hindsight a read-only check
  should either be run while the backend holds the journal open, or note this effect in
  advance; `immutable=1` would avoid file creation but can read inconsistent data while the
  backend writes, so it was not appropriate.
- `JOHANNA_DATA_DIR` in the backend's environment could not be read; the default path is
  supported by the evidence in section 2 but not proven.
- The code currently running (started 06:30 UTC) could not be identified exactly: no API was
  called and the tree at that time is not recoverable without Git.
- No backup pair was opened (not required); free-space is a point-in-time value.

## 10. Migration risk assessment

- **Low data risk:** the journal is healthy, at v5, with valid key and audit chain, no
  notification history and no pre-existing v6 objects; every refusal path is fail-closed and
  the verified `premigration-v5` pair is made before any change.
- **Deployment scope:** the next restart migrates to v6 **and** deploys the final P6.2b.4 and
  P6.2b.5 code (replacing the 06:30 UTC build). Everything new stays inactive.
- **Rollback:** older code refuses a v6 journal; recovery is the manual restore of the
  `premigration-v5` pair. The working tree is uncommitted; a commit before the restart would
  give a code rollback point.
- **Open item:** the pre-existing browser-test snapshot race (P6.2b.5) is unrelated to the
  migration.

## 11. Production state confirmation

- Journal content and schema: **unchanged** (v5; file size and last write unchanged).
- Journal key, backups, configuration, preferences, CostTerms, risk policy, scheduler:
  **unchanged** (key read only; no backup touched).
- Files created: the two SQLite WAL sidecars described in section 9 (my read-only connection).
- Backend: not started, stopped or restarted by me (PID 26716 unchanged during this phase).
- No migration, no backup creation, no service activation, no MT5 call, no broker action, no
  Git command, no source or test change.
