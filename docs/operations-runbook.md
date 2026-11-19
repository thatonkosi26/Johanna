# Johanna operations runbook (P6.R)

Manual procedures for the conditions reported by `GET /api/operations/readiness`. Nothing in
this runbook is automated, and nothing here enables an unattended service. **Never regenerate
the journal key, never delete or recreate `trading_journal.sqlite3`, and never overwrite a
backup pair.** When in doubt, stop the backend (Ctrl+C in its console) and ask.

## Startup and shutdown

- Start: `python -m johanna` from the project directory. Stop: Ctrl+C (orderly: the shadow
  scheduler stops first, then services; the journal checkpoints its WAL).
- One backend per data directory: a second start fails with *"Another Johanna backend is
  already using …"* (`InstanceLockError`). Do not delete `data/johanna.lock` while a backend
  is running; after a crash the operating system releases the lock with the process.
- A restart never enables anything that was disabled: the scheduler's state comes from its
  append-only journal history (no row = disabled); monitoring and notifications have no
  activation in this phase.
- Check after every start: `GET /api/operations/readiness` → `manual_operation: "ok"`.

## Journal refused

`journal.state = "refused"` with a reason. Johanna keeps running; journal features return 503.

| Reason | Meaning | Action |
|---|---|---|
| `corrupt_database` | integrity check failed | Stop. Do **not** delete the file. Restore from the newest valid backup pair (see *Restore*). |
| `orphaned_key` | key present, database missing | Stop. Find the database (backup pair); never delete the key. |
| `restore_required` | database missing, backups present | Restore from the newest valid pair. |
| `migration_requires_verified_key` | key missing or wrong while a migration is pending | Put the original key back (from the pre-migration pair, byte-identical). Never generate a new key. |
| `backup_failed` / `backup_unverified` / `backup_audit_invalid` / `backup_key_mismatch` | the mandatory pre-migration backup could not be made or verified | Investigate disk space and the audit chain; the original journal is untouched. |
| `migration_failed` | the migration rolled back | The journal is still at its previous version and the verified backup exists; report the failure. A later start retries. |
| `schema_unsupported` | the journal is newer than this code | Run the matching code version. |

### Restore (manual)

1. Stop the backend.
2. Choose the newest pair `data/trading_journal.backup-*` that validates
   (`johanna.journal_store.validate_pair(path)` returns `True`).
3. Move (do not delete) the damaged `trading_journal.sqlite3` and its `-wal`/`-shm` files to a
   dated folder outside `data/`.
4. Copy the pair's `trading_journal.sqlite3` and `trading_journal.key` into `data/`.
5. Start the backend and check readiness. Records after the backup are not in the restored
   journal; they remain in the moved file for investigation.

## Journal integrity failure

`journal_integrity` or `foreign_keys` attention: stop the backend, keep every file, restore as
above if the live file is damaged.

## Audit chain failure

`audit_chain.valid = false` (`first_bad_seq` given): the journal was altered outside Johanna.
Stop; do not "repair" rows. Compare with the newest valid backup pair and report.

## MT5 unavailable

`mt5.state = "unavailable"` with `unavailable_reason`: start or log in to the MT5 terminal; the
worker reconnects with bounded backoff. Stale data is never shown as LIVE. Worker timeouts
terminate the worker process and back off; the dashboard shows the reason.

## Scheduler error

`scheduler.state = "error"` after repeated failures: the scheduler stopped itself (no endless
restarts). Read `last_error`, fix the cause (usually MT5 or the journal), then disable and
re-enable it from the Analytics page. Missed windows are recorded MISSED, never evaluated late.

## Scheduler activation

The scheduler is OFF until the user enables it on the Analytics page. Enabling it is a user
decision; nothing enables it automatically.

## Cost evidence

Production cost terms are empty, so every proposal is UNVERIFIED with no actionable volume.
`GET /api/costs/readiness` lists each missing piece (commission, slippage, holding period per
timeframe). Approval requires dated, verbatim evidence and the user's explicit decision.

## Monitoring activation

Broker monitoring (P6.3) is read-only infrastructure with no thread or schedule (`observe()`
and `reconcile()` run only when called explicitly). Activation needs a verified journal key
and a separately approved schedule. **Not active.**

## Notifications: lifecycle and failure handling

Local adapters only (dashboard inbox); nothing creates a notification automatically and there
is no external delivery (WhatsApp, email, push are neither implemented nor authorised).
**Not active.**

- Lifecycle: `pending → available → presented → acknowledged`, each once and in order
  (database trigger); `acknowledged` only by the user. Every step is audited
  (`notification.created / delivered / failed / exhausted / presented / acknowledged`).
- Delivery (P6.2b.4, needs schema v6 installed): each attempt is first *claimed* (unique per
  notification, adapter and attempt), and only the claim's owner calls the adapter, so two
  workers can never deliver the same attempt; the database also allows at most one successful
  delivery per notification. On a v5 journal delivery is refused (`claims_not_installed`).
- A claim whose owner never reports back (crash) becomes a visible failed attempt
  (`claim_expired`) after 5 minutes and is retried. Local adapters are idempotent; an
  external adapter would need its own duplicate protection before it could be approved.
- Diagnostics (`GET /api/notifications`, read-only): `delivery` is `not_attempted`,
  `in_progress`, `claim_stale`, `failed_retryable` (with `last_error`), `exhausted` (5 failed
  attempts per adapter; never retried) or `delivered`.

## Backups: policy and recovery

- **Pre-migration pairs** (`trading_journal.backup-premigration-vN-*`) are made by the journal
  before every migration and are never pruned.
- **Routine pairs** (`…backup-daily-*`) exist as infrastructure only (`johanna.backups`):
  `BackupPolicy.enabled = False`, no schedule, and nothing calls `create_routine()`.
  **Automatic backups are not active**; a schedule needs a separate approval.
- Every pair uses the SQLite online backup API (consistent and WAL-safe), is checked
  (integrity, audit chain, key pairing, file hashes) and is published by one directory rename;
  a failed or interrupted backup leaves no partial pair and never touches existing pairs.
- Disk space: a backup needs about one more copy of the journal (database + WAL). A routine
  backup refuses with `insufficient_space` below 2 × that size + 64 MB free.
- Retention (when approved): keeps the newest 7 valid routine pairs and every pre-migration
  pair, never deletes the newest valid pair of any kind, and never deletes invalid pairs
  (reported for manual review). Pruning is a dry run unless explicitly applied, and refuses if
  any kept pair no longer validates.
- Restore rehearsal: `BackupManager.restore_rehearsal(pair, target)` copies a valid pair into
  an empty directory **outside** `data/` and opens it at the pair's own schema version
  (nothing migrated), reporting key status, audit validity and row counts. The real restore
  stays manual (see *Restore (manual)*).

## Readiness verification and freshness

`GET /api/operations/readiness` runs `quick_check` and `foreign_key_check` on every request.
The audit chain and backup pairs reuse **verified evidence**, never a previous verdict alone:

- Audit chain: after a full verification, later requests check that the last verified row is
  unchanged, that no row was deleted or inserted before it, and verify every new row. A full
  verification runs again at least every 15 minutes (`detail.mode` = `full` or
  `incremental`, with `evidence_age_seconds`). An edit of an older row that keeps its stored
  hash (which requires dropping the append-only triggers) is caught by the next full check.
- Backup pairs: a pair's validation is reused while its files are unchanged, for at most
  6 hours; new, changed, missing, corrupt or invalid pairs are validated every time.
- Invalid evidence is never cached, so an invalid journal is never reported `ok`.

## User-confirmed trade linking and discrepancies

Reconciliation is read-only towards the broker and never links a trade automatically.
Every trade result states its `link_status`:

| `link_status` | Meaning |
|---|---|
| `USER_CONFIRMED` | the user confirmed this trade ↔ position link (append-only decision) |
| `RECORDED_REFERENCE` | a broker deal reference was recorded on the trade itself |
| `CANDIDATE` | exactly one fitting broker deal; **not a link** until the user confirms |
| `AMBIGUOUS` | several fitting deals, or one deal fitting several trades |
| `REJECTED` | the user rejected the only fitting position |
| `NONE` / `UNAVAILABLE` | no candidate / no usable broker evidence (`reason_code`: `no_observation`, `observation_not_live` incl. missing broker identifiers, `observation_stale`, …) |

Link decisions (`CONFIRMED`, `REJECTED`, `REVOKED`) are made only by the user, must name a
stored live observation that contains the position, are exclusive (one active confirmed link
per position and per trade) and are audited. **The confirmation endpoint
(`POST /api/reconciliation/links`) answers `403 not_activated`** until separately approved;
`GET /api/reconciliation/links` lists decisions read-only.

Discrepancies (evaluated only against broker-confirmed facts of a known position):
`LOGBOOK_CLOSED_BROKER_OPEN`, `BROKER_CLOSED_LOGBOOK_OPEN`, `IDENTIFIER_CONFLICT`,
`SYMBOL_MISMATCH`, `DIRECTION_MISMATCH`, `VOLUME_MISMATCH` (open volume, or entry volume of a
closed position, after logbook corrections), `MULTIPLE_POSSIBLE_MATCHES` and
`LINKED_POSITION_NOT_OBSERVED` (absence is never treated as a closure). Unavailable or stale
evidence produces no discrepancy; it is reported as unavailable.

## Evidence lifecycle

`johanna.evidence` (schema v6; nothing is recorded automatically): an evidence record
holds kind, source, symbol, topic, observation time, optional validity end, content and the
source's verification status (`VERIFIED` / `UNVERIFIED`), with a content fingerprint and a
keyed MAC (journal key). Records are immutable; a mistake is fixed by a correction record that
names the record it supersedes (once) with a reason. Links associate evidence with decisions
and risk assessments. When assessed, an item is `MISSING`, `STALE` (validity passed),
`CONFLICTING` (different content for the same topic), `UNVERIFIED` or `VERIFIED`; only all
`VERIFIED` is sufficient — **missing evidence is never approval**. Content containing account,
login, ticket, server, password or identifier-like values is refused.

## Schema v5

Installed in production on 2026-11-19 (P6.2b.3) after a verified `premigration-v4` pair.
Its tables (cost-policy registry, broker observations/reconciliation, notifications) enable
nothing by themselves.

## Schema v6 migration

**Registered in source (P6.2b.5); not yet installed in production** — production stays on v5
until the user restarts the backend from this source tree. The running backend keeps the code
it loaded, so it is unaffected by the registration.

- **Contents** (`johanna.journal_store.SCHEMA_V6`, additive; no v1 – v5 table changes):
  tables `notification_claim`, `trade_broker_link`, `reconciliation_discrepancy`,
  `evidence_record`, `evidence_link`; 7 indexes (incl. the unique `notification_delivered_once`
  on the v5 `notification_delivery` table); 13 triggers (append-only guards,
  `notification_delivery_needs_claim`, `trade_broker_link_exclusive`,
  `trade_broker_link_revocation`).
- **Trigger:** the next `python -m johanna` start. `JournalStore.start()` checks the migration
  sequence (1 – 6, each once), the journal's integrity and version, and verifies the key; only
  then does it migrate.
- **Backup first:** `trading_journal.backup-premigration-v5-<UTC>` (SQLite online backup,
  integrity, audit chain, key pairing, file hashes, published by one rename, re-validated).
  If it cannot be made or verified (`backup_failed`, `backup_unverified`,
  `backup_audit_invalid`, `backup_key_mismatch`) nothing is migrated. Space: about one more
  copy of the journal in `data/`.
- **Transactional:** one `BEGIN IMMEDIATE` transaction, `foreign_key_check` before `COMMIT`;
  any error — including a non-SQLite exception — rolls back explicitly. The journal stays at
  v5 with no v6 object, and is refused (`migration_failed`).
- **Refusals (nothing recreated, nothing changed):** missing or wrong key
  (`migration_requires_verified_key`), corrupt journal (`corrupt_database`), tampered audit
  chain (`backup_audit_invalid`), an existing foreign-key violation (`migration_failed`), a
  future version (`schema_unsupported`), an invalid sequence (`migration_order`), and a journal
  holding two successful deliveries of one notification (the delivered-once index refuses →
  `migration_failed`; production had no notification rows at the P6.2b.3 check).
- **On failure:** do **not** restart again (a later start retries automatically), do not run
  SQL by hand, do not restore or regenerate anything; report the refusal reason and wait. The
  verified `premigration-v5` pair is the recovery point (see *Restore (manual)*).
- **Rollback to older code:** code that registers only v1 – v5 refuses a v6 journal
  (`schema_unsupported`); going back requires restoring the `premigration-v5` pair.
- **Post-migration verification (read-only):** `PRAGMA user_version = 6`; every v1 – v5 object
  unchanged and exactly the v6 objects added; every v5 row present and identical compared with
  the `premigration-v5` pair; `integrity_check` ok; no foreign-key violation; audit chain
  valid (the pair's chain an identical prefix); key verified and HMAC available; the five v6
  tables empty; scheduler OFF; monitoring, reconciliation and notifications inactive; CostTerms
  empty.
- **Activation boundary:** installing v6 activates nothing. Notification delivery, the linking
  confirmation endpoint (`POST /api/reconciliation/links` → 403), evidence recording, automatic
  backups and monitoring each need their own approval.

## Activation requirements (all currently inactive)

| Capability | Needs before activation |
|---|---|
| Notification delivery | schema v6 installed (production migration); approval of a delivery trigger |
| Automatic backups | approval of a schedule (`BackupPolicy.enabled`), retention and pruning |
| Broker monitoring / reconciliation | approval of a schedule; verified journal key |
| User-confirmed linking endpoint | schema v6; review and approval of the confirmation UI/API |
| Evidence recording | schema v6; approval of what is recorded and by whom |
| Shadow scheduler | the user's explicit enable (currently OFF) |

## Execution boundary

Broker execution is disabled by design (`DisabledExecutionService` refuses every request).
Shadow intents are hypothetical records and can never become orders.
