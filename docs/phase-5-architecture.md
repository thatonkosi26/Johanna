# Johanna Phase 5 — Complete Trading Lifecycle: Architecture (P5.0)

Status: **consolidated design for final review**. Nothing in this document is implemented.
Implementation begins only with explicit P5.1 authorisation.

Scope: decision journal, manual trade logbook, read-only MT5 position monitoring, trade
reconciliation, in-app notifications and performance analytics, built on the accepted
T0–T6, I1–I2, V1, Phase 3 and Phase 4 work.

---

## 0. Non-negotiable constraints

1. Johanna never places, modifies, closes or deletes MT5 orders or positions. No
   `order_send`, no `order_check`, no execution path of any kind. The existing
   `DisabledExecutionService` stays the only execution implementation.
2. MT5 access stays read-only and allowlisted; no new MT5 connection is introduced.
3. No collectors are enabled; no scheduled market evaluation.
4. No external providers or channels. WhatsApp and every other external notification channel
   stay disabled and unimplemented until separately approved.
5. Existing `data/` contents are never inspected, modified, deleted or reorganised. Phase 5
   creates only the files approved in A1, A2 and A9 (§5).
6. A proposal or analysis candidate is never represented as an approved trade or an execution.
   An open position is never counted as a realised outcome.
7. Missing, stale or unverified data is never treated as zero, complete or confirmed.
8. Raw broker identifiers, the journal key and account identifiers never appear in API
   responses, frontend state, logs or error messages.

---

## 1. Current architecture (verified, relevant to Phase 5)

| Area | Existing implementation | Consequence |
|---|---|---|
| Process | One process (OS file lock, `instance_lock.py`); threaded WSGI server (`ExclusiveWSGIServer`); `ServiceManager` ordered lifecycle; `BackgroundService` stoppable threads with error states | One writer process: SQLite is appropriate. The monitor is a `BackgroundService`. |
| Composition | `JohannaApplication.__init__` wires services by constructor injection | Phase 5 services are wired the same way. |
| Persistence | `ApplicationStore`: SQLite `{data_dir}/johanna.sqlite3`, WAL, `user_version` migrations; **auto-quarantines and recreates a corrupt database** | Suitable for preferences, **not** for trading records → separate fail-closed journal store. |
| API | Fixed route table, per-route methods, 405 + `Allow`, Host/Origin checks, JSON-only POST with 16 KB limit (`/api/preferences`), `no-store` | Local write endpoints follow the preferences pattern. |
| MT5 | Read-only adapter, allowlisted fields; quotes and bars worker processes, one connection each, one call at a time (`ProcessSessionRunner`: plain lock; a job that exceeds its timeout **kills and respawns the worker**) | Monitor jobs need non-blocking admission and must stay below the kill threshold. |
| Freshness | `_request`/`_worker_read` set `_last_success` (connection status) | The monitor uses a separate path that never touches market freshness fields. |
| Analysis/risk | `MarketAnalysisService.analyse()` (single-flight, T6 envelope); `RiskService.proposal()` (Phase 4); both request-driven | The journal captures at request time; it is not continuous coverage. |
| Domain stubs | `UnimplementedSignalService` (referenced in `application.py` and `tests/test_domain_contracts.py`), others | Retained (A6). |
| Frontend | Hash routes; placeholder pages Signals, Positions, Trade History, Analytics; per-page pollers with `active()` gates | Phase 5 fills the placeholders. |

---

## 2. Architecture

```
 MT5 (read-only; existing quotes worker: one connection, one call at a time)
   │ market-data jobs (normal priority)             monitor jobs (low priority, skip-if-busy)
   ▼                                                 ▼
 LiveMarketService ─► MarketAnalysisService (T6)    TradeMonitor (BackgroundService)
 AccountIntelligence ─► RiskService (P4)              │ positions / orders / windowed deals
   │ request-time capture      │ request-time capture │ raw IDs → HMAC refs at once (memory only)
   ▼                           ▼                      ▼
 DecisionJournal: decision (immutable) ─< risk_assessment (append-only)
                  └─ decision_execution (audited projection)
 TradeLogbook (manual records) ◄─ Reconciler ─► broker_position / broker_deal (HMAC refs)
          └──────────── EventBus ─► NotificationCenter (in-app channel only)
 Analytics (read-only, four separated populations)
 JournalStore: data/trading_journal.sqlite3 + data/trading_journal.key (fail-closed)
```

### 2.1 Components

| Component | Responsibility | Depends on |
|---|---|---|
| `JournalStore` | Connection management, migrations, single write lock, transactions, mutability triggers, audit rows and hash chain, key handling, backups | stdlib `sqlite3`, `secrets`, `hmac`, `hashlib`; Windows `icacls` (A10) |
| `DecisionJournal` | Normalise T6 envelopes and Phase 4 results into decisions and risk assessments; fingerprints; observation counters; supersession links | `JournalStore`; results handed in by the application layer |
| `TradeLogbook` | Manual trades and fills; validation; void instead of delete; links to decisions | `JournalStore`; Phase 3 instrument data for validation only |
| `TradeMonitor` | Background observation of positions, pending orders and deal history; diffs → observations and lifecycle transitions; checkpointed history recovery | `LiveMarketService` (new read-only jobs), `JournalStore`, `EventBus` |
| `Reconciler` | Candidate matching, scoring, evidence, user confirmation, discrepancies, deal-based closure confirmation | `JournalStore`, `EventBus` |
| `EventBus` | Typed events with unique deduplication keys; in-process fan-out | `JournalStore` |
| `NotificationCenter` | Notification rows per (event, channel); lifecycle; in-app channel only | `EventBus`, `JournalStore` |
| `Analytics` | Pure metric functions over the four populations | `JournalStore` (read-only) |

No Phase 5 module imports execution code; a static test enforces that no Phase 5 module (or
any module under `src/`) references `order_send`, `order_check` or other execution functions.

### 2.2 Capture points and failure isolation

The application layer (not T6 or Phase 4 code) hands each completed `analyse()` envelope and
each `proposal()` result to `DecisionJournal`. A journal failure is logged, reported in
service health and never changes or delays the analysis or risk response.

---

## 3. Data model

### 3.1 Entity relationships

```
decision ──1:1── decision_observation
decision ──1:n── risk_assessment ──1:1── risk_assessment_observation
decision ──(previous_decision_id)── decision            (supersession chain)
decision ──1:1── decision_execution ──► manual_trade / reconciliation_match
decision >──< trade_decision_link >──< manual_trade ──1:n── manual_fill
manual_trade ──< reconciliation_match >── broker_position ──1:n── broker_deal
broker_position ──1:n── broker_observation          broker_order (pending, observed)
history_coverage (query-covered intervals)          meta (schema, key_check, install data)
event ──1:n── notification                           audit_log (every mutation, hash chain)
```

### 3.2 Mutability classes (enforced by SQLite triggers)

| Class | Tables | Rule |
|---|---|---|
| Immutable evidence | `decision`, `risk_assessment`, `broker_deal` | INSERT only. UPDATE and DELETE triggers abort. |
| Append-only events | `event` | INSERT only. UPDATE and DELETE triggers abort. `dedup_key` stays unique, so a duplicate insert is rejected (idempotent replay). Notification lifecycle changes live in the separate mutable, audited `notification` table, never in `event`. |
| Mutable observation metadata | `decision_observation`, `risk_assessment_observation` (`first_seen_utc`, `last_seen_utc`, `observations`) | Counter/timestamp UPDATEs only. |
| Append-only audit | `audit_log` | INSERT only; UPDATE/DELETE triggers abort; SHA-256 hash chain (§9). |
| Void-only manual evidence | `manual_fill` | INSERT, plus a single audited transition `RECORDED → VOIDED` (with reason and time); every other UPDATE and every DELETE aborts (§3.4). |
| Mutable records, every change audited | `manual_trade`, `trade_decision_link`, `reconciliation_match`, `broker_position`, `broker_order`, `broker_observation`, `notification`, `history_coverage`, `decision_execution` | Every UPDATE writes an `audit_log` row (old and new values) in the same transaction. Manual records are voided, never deleted. |

Original analysis evidence is therefore never overwritten; changes create new rows.

### 3.3 Decision journal

**`decision`** (immutable evidence)

| Field | Meaning |
|---|---|
| `id` | ULID |
| `symbol`, `broker_symbol`, `timeframe`, `as_of_utc` | `as_of_utc` = the closed candle evaluated |
| `analysis_outcome` | `TRADE_CANDIDATE` / `MARKET_CONDITION` / `NO_TRADE` |
| `t6_rule` | R1–R7 |
| `market_direction` | `BUY` / `SELL` (T6 opportunity direction) / `NONE` |
| `t6_evidence` | JSON: decision reasons, gate, validation verdict, market-condition states, opportunity checklist summary |
| `analysis_fingerprint` | Unique on (`symbol`, `timeframe`, fingerprint) |
| `snapshot_id`, `evaluated_at_utc` | Provenance of the first evaluation with this fingerprint |
| `previous_decision_id` | Predecessor for the same symbol and timeframe |
| `capture_source` | `analysis_request` / `risk_request` |

`analysis_fingerprint = SHA-256(symbol, timeframe, as_of_utc, analysis_outcome, t6_rule,
market_direction, sorted T6 reasons, validation verdict, market-condition states)`. Snapshot IDs
and timestamps are excluded, so repeated identical evaluations only update
`decision_observation` (`last_seen_utc`, `observations`).

**Derived decision state** (computed, never stored): `SUPERSEDED` if a later decision
references it; else `EXPIRED` if `as_of_utc` + timeframe has passed; else `CURRENT`.

**`risk_assessment`** (append-only, per decision)

| Field | Meaning |
|---|---|
| `id`, `decision_id` | |
| `risk_status` | `APPROVED` / `REJECTED` / `UNVERIFIED` / `NOT_EVALUATED` |
| `phase4_status_raw` | Phase 4 status as returned |
| `risk_evidence` | JSON: validations, rejection reasons, unverified reasons, cost and slippage status, sizing status (no amounts that identify the account) |
| `risk_fingerprint` | Unique per decision |
| `evaluated_at_utc`, `previous_assessment_id` | |

`NOT_EVALUATED` applies when no risk evaluation ran for the decision, or when T6 stopped
before the risk stages (Phase 4's "no T6 candidate" rejection is a precondition failure, not a
risk assessment; its raw status is kept).

**Five independent dimensions** (all exposed separately by the API and UI):

| Dimension | Values | Source |
|---|---|---|
| Analysis outcome | `TRADE_CANDIDATE` / `MARKET_CONDITION` / `NO_TRADE` | `decision` |
| Market direction | `BUY` / `SELL` / `NONE` | `decision` |
| Risk status | `APPROVED` / `REJECTED` / `UNVERIFIED` / `NOT_EVALUATED` | latest `risk_assessment` |
| Trade eligibility | `APPROVED` only if `TRADE_CANDIDATE` and latest risk `APPROVED`; else `NOT_APPROVED` | derived |
| Execution status | `NONE` / `MANUALLY_RECORDED` / `BROKER_VERIFIED` | `decision_execution` |

A T6 candidate with unverified risk is shown as "market-analysis candidate · BUY · risk
UNVERIFIED · NOT_APPROVED · execution NONE". It is never presented as an approved trade or an
execution. `MANUALLY_RECORDED` requires a user link to a logbook trade; `BROKER_VERIFIED`
requires that trade to be reconciled `MATCHED` with deal evidence.

**Coverage disclosure.** The journal records evaluations requested by the application (pages
open, API calls). It is not continuous market coverage. Every journal API response carries
`coverage: "request_time"` and the UI shows the disclosure.

### 3.4 Manual trade logbook

**`manual_trade`**: `id`, `symbol`, `direction`, `status`, `initial_sl`, `initial_tp`, `notes`,
`rationale`, `strategy_tag`, `source = "manual"`, `created_utc`, `updated_utc`, `voided_reason`.

Lifecycle: `DRAFT → OPEN → PARTIALLY_CLOSED → CLOSED`; `VOIDED` from any state (audited
correction; no delete route exists).

**`manual_fill`**: `id`, `trade_id`, `kind` (`entry` / `partial_exit` / `exit`), `time_utc`,
`price`, `volume`, `commission`, `commission_source`, `swap`, `swap_source`, `fee`, `fee_source`,
`status` (`RECORDED` / `VOIDED`), `voided_reason`, `voided_utc`, `corrects_fill_id` (the voided
fill this record replaces, if any), `created_utc`.

- **Cost provenance per component:** each of `commission_source`, `swap_source` and
  `fee_source` is `user_entered` or `unknown`, independently. A component marked `unknown` has a
  `null` amount and is never defaulted to zero, so known and unknown components stay
  distinguishable.
- **Corrections:** a fill is never edited or physically deleted. A correction voids the original
  (`RECORDED → VOIDED`; a reason is mandatory; the transition is audited) and adds a new
  `RECORDED` fill with `corrects_fill_id` pointing to the original. The original evidence stays
  available.
- **Voided fills are excluded** from volume validation, P&L, costs and analytics.
- **Volume rule:** total closed volume of `RECORDED` fills may never exceed total opened volume
  of `RECORDED` fills; a void that would break this for later fills is rejected.

Manual values are labelled "recorded"; broker values are labelled "observed"; neither is ever
written into the other. **Prefill** of a logbook entry from a broker observation is allowed
only after explicit user confirmation; the record remains manual, with an observation link.

### 3.5 Broker observations (HMAC references only)

**`broker_position`**: internal `id`, `position_ref` (HMAC), `symbol`, `side`, `volume_open`,
`price_open`, `sl`, `tp`, `first_seen_utc`, `last_seen_utc`, `state`, `source`
(`observed` / `history_recovery`).

**`broker_deal`** (immutable): `deal_ref`, `position_ref`, `order_ref` (HMAC), `time_utc`, `type`,
`entry`, `volume`, `price`, `profit`, `swap`, `commission`, `fee`, `reason` (client, SL, TP,
stop-out, …).

**`broker_order`** (pending orders observed): `order_ref`, `symbol`, `type`, `volume`,
`price_open`, `price_stoplimit`, `sl`, `tp`, `state`.

**`broker_observation`**: change log per position or order (SL, TP, volume, price observed).

**Broker-position lifecycle**

```
OBSERVED_OPEN ─► DISAPPEARED ─► CLOSED_CONFIRMED | CLOSE_UNCONFIRMED ─► HISTORY_UNAVAILABLE
recovered from history:  AWAITING_EXIT_EVIDENCE ─► CLOSED_CONFIRMED | HISTORY_UNAVAILABLE
                         ENTRY_OUTSIDE_COVERAGE (extend backwards) ─► CLOSED_CONFIRMED | HISTORY_UNAVAILABLE
```

SL, TP and volume changes create `broker_observation` rows and events; they are not states.

**`CLOSED_CONFIRMED` requires all three:**
1. entry (IN) and exit (OUT) deal evidence for the position's `position_ref`;
2. total OUT volume equals total IN volume;
3. the query intervals spanning those deals are `QUERY_COVERED` (§6).

A disappearance alone never confirms a closure. An empty successful history response never
confirms a closure.

### 3.6 Reconciliation

**`reconciliation_match`**: `manual_trade_id`, `broker_position_id`, `status`, `score`, `evidence`
(JSON: symbol, side, entry time delta, entry price delta in points, volume match, per-deal
coverage of manual fills), `decided_by` (`auto` / `user`), `decided_utc`.

Lifecycle: `UNMATCHED → CANDIDATE → MATCHED`; `AMBIGUOUS` (several comparable candidates,
never auto-picked); `MATCHED → DISCREPANCY` (later deal evidence contradicts the manual
record) `→ RESOLVED` (user action).

Matching rules: candidates must share symbol and side. The score combines entry-time delta,
entry-price delta (points) and volume agreement, with configurable tolerances. Auto-`MATCHED`
only when exactly one candidate exceeds the high threshold and no other exceeds the low
threshold; otherwise `CANDIDATE` or `AMBIGUOUS` for user decision. Partial closes map manual
exits to OUT deals; volume or price outside tolerance creates `DISCREPANCY`. Neither side's
values are ever overwritten. The scorer is a pure function (deterministic, testable).

### 3.7 Events and notifications

**`event`** (append-only, immutable after insertion; §3.2): `id`, `type`, `subject_ref`
(Johanna ID or HMAC ref), `dedup_key` (unique), `severity`, `payload`, `occurred_utc`,
`recorded_utc`, `recovered` (true for history-recovered events).

Event types: `decision.created`, `decision.changed`, `risk_assessment.changed`,
`execution.status_changed`, `position.opened`, `position.changed`, `position.disappeared`,
`position.closed_confirmed`, `order.placed_observed`, `order.removed_observed`,
`trade.reconciled`, `trade.discrepancy`, `risk.warning` (e.g. a position without stop-loss,
daily risk near limit), `history.unavailable`, `monitor.degraded`, `monitor.recovered`,
`data.quality`.

Deduplication keys include type, subject and state version, e.g.
`position.changed:{position_ref}:{sl}:{tp}:{volume}`; replays and restarts cannot duplicate.

**`notification`**: `event_id`, `channel` (`in_app` only; no external channel class exists),
`status`, `available_utc`, `presented_utc`, `acknowledged_utc`, `dismissed_utc`.

**Notification lifecycle**

```
PENDING ──(eligible, deduplicated)──► AVAILABLE ──(client reports actual presentation)──► PRESENTED ──(user)──► ACKNOWLEDGED
                 AVAILABLE or PRESENTED ──(user action)──► DISMISSED   (separate, recorded)
```

`PRESENTED` is set only by an explicit client call made when the notification is rendered
while the tab is visible and the inbox is open. Fetching notifications never changes their
status. Notified by default: changed candidates, eligibility changes, risk warnings,
discrepancies, history unavailability and monitor degradation. Routine `NO_TRADE` decisions
are journal entries only. Recovered events are marked "recovered, late".

### 3.8 Lifecycles summary

| Entity | States |
|---|---|
| Decision (derived) | `CURRENT`, `SUPERSEDED`, `EXPIRED` |
| Manual trade | `DRAFT`, `OPEN`, `PARTIALLY_CLOSED`, `CLOSED`, `VOIDED` |
| Manual fill | `RECORDED`, `VOIDED` (one-way, reason required, audited) |
| Event | Immutable after insertion (no states) |
| Broker position | `OBSERVED_OPEN`, `DISAPPEARED`, `CLOSED_CONFIRMED`, `CLOSE_UNCONFIRMED`, `AWAITING_EXIT_EVIDENCE`, `ENTRY_OUTSIDE_COVERAGE`, `HISTORY_UNAVAILABLE` |
| Reconciliation | `UNMATCHED`, `CANDIDATE`, `MATCHED`, `AMBIGUOUS`, `DISCREPANCY`, `RESOLVED` |
| Notification | `PENDING`, `AVAILABLE`, `PRESENTED`, `ACKNOWLEDGED`, `DISMISSED` |
| Monitor health | `HEALTHY`, `DEGRADED`, `UNAVAILABLE`, `STOPPED` |
| History (journal-wide) | `QUERY_COVERED`, `HISTORY_RECOVERING`, `HISTORY_UNAVAILABLE` |

---

## 4. Broker identifiers (HMAC)

- References: `ref = HMAC-SHA256(key, "<kind>:" + str(raw_id))` with kind `position`, `deal`
  or `order`. Used for uniqueness, matching, event deduplication and audit.
- **Raw identifiers are never persisted.** They exist only transiently: returned by the MT5
  worker over its pipe, converted to references immediately by the monitor, and held in an
  in-memory map (`ref → raw`) for currently known open positions only.
- **MT5 calls that need the original identifier** (e.g. `history_deals_get(position=raw)`) use
  the raw value from the in-memory map, refreshed from `positions_get`. A hash is never passed
  to MT5.
- **After a restart** (empty map), or for a position that closed while Johanna was offline,
  the monitor uses windowed history (`history_deals_get(from, to)`), computes the reference of
  each deal's position identifier and matches stored references (§6). Positions open at startup
  repopulate the map from `positions_get`.
- `position=raw` lookups are a cross-check only; closure confirmation always requires query
  coverage (§3.5).
- No key rotation: re-deriving references for closed positions would need raw identifiers that
  are not stored. A rotation would start a new reference domain, recorded in the audit log.

---

## 5. Storage, key and backup

### 5.1 Files (the only files Phase 5 may create in `data/`)

| File | Purpose |
|---|---|
| `data/trading_journal.sqlite3` (+ SQLite's own `-wal`, `-shm`) | Journal database (A1) |
| `data/trading_journal.key` | 32-byte HMAC key (A9) |
| `data/trading_journal.backup-{kind}-{utc}/` containing `trading_journal.sqlite3`, `trading_journal.key` and `pair.json` | A published backup pair directory; `{kind}` = `daily` or `premigration-v{N}` (A2) |
| `data/.trading_journal.backup-tmp-{utc}-{random}/` | Temporary build directory of one backup attempt; never a backup (§5.4) |

No other `data/` content is listed, read or modified. Existence checks glob only the
`trading_journal*` and `.trading_journal.backup-tmp-*` prefixes.

### 5.2 Store behaviour

WAL; `foreign_keys = ON`; `busy_timeout`; one process-wide write lock (HTTP threads and the
monitor); short explicit transactions; forward-only numbered migrations in one transaction each;
`PRAGMA integrity_check` at start. **Fail closed** on corruption: no quarantine, no automatic
recreation; the journal reports the problem and stays unavailable.

### 5.3 Key creation, permissions, verification and refusal

- **Content:** exactly 32 bytes from `secrets.token_bytes(32)`, raw binary.
- **Genuine first creation** only when the database, the key, every
  `trading_journal.backup-*` directory and every `.trading_journal.backup-tmp-*` directory are
  all absent. Order: create the key exclusively (`O_CREAT | O_EXCL`, never overwrite) → restrict
  and verify its ACL → create the database and store
  `meta.key_check = HMAC(key, "johanna-journal-key-check")`.
- **Failed first creation — cleanup scope:** the attempt records each path it creates itself
  (exclusive creation proves the file did not exist before). On any failure it removes **only
  those paths**: the new key, the new database and any SQLite `-wal` / `-shm` sidecars created
  during that attempt. Pre-existing files and backups are never removed. Initialisation is then
  refused for the rest of the process lifetime (the journal stays unavailable and reports why). A
  later start may attempt genuine first creation again only if every condition above still
  holds. If cleanup itself fails (for example, the new key cannot be removed), the leftover key
  without a database triggers the "orphaned key" refusal below on every later start — fail
  closed, never a replacement key.
- **Windows ACL (A10):** built-in `icacls` with explicit arguments and no shell: remove
  inheritance and grant access only to the SID of the Windows user running Johanna; read the ACL
  back and verify it. If restriction or verification fails, the journal is not created (or the
  backup is not accepted). Linux (tests/CI): `chmod 0600`.
- **Verification:** the key check runs at every start, after a restore and during each backup.
- **Refusal (monitoring and reconciliation; the decision journal and logbook keep working
  because they do not use HMAC):**

| Condition | Behaviour |
|---|---|
| Database exists, key missing | Refused; no key generated |
| Key check fails | Refused |
| Key exists, database missing | Refused (orphaned key; restore may be in progress) |
| Backups exist, database missing | Initialisation refused; restore required |
| A previous first-creation attempt failed in this process | Initialisation refused for the rest of the process lifetime |

- A replacement key is never generated automatically, and never over an existing key. The key never appears in logs, API
  responses, frontend state or error messages ("journal key unavailable or mismatched").

### 5.4 WAL-safe backup

Copying `trading_journal.sqlite3` directly while Johanna runs is **not** a valid backup
(uncheckpointed WAL content would be missing). Backups use SQLite's online backup API
(`sqlite3.Connection.backup`, stdlib). No new dependency.

**Pair publication is a single atomic directory rename**, so a database can never be published
without its key (or the reverse):

1. Create a uniquely named temporary directory
   `data/.trading_journal.backup-tmp-{utc}-{random}/` (exclusive creation). Its name never matches
   the published-pair pattern.
2. Online backup API → consistent snapshot `trading_journal.sqlite3` inside the temporary
   directory.
3. `PRAGMA integrity_check` on the snapshot must return `ok`.
4. Copy the key bytes exclusively to `trading_journal.key` inside the temporary directory.
5. Verify the pairing: `HMAC(copied key, check string)` equals `key_check` in the snapshot.
6. Apply the user-only ACL to the copied key and to the directory (A10), read it back and verify.
7. Write `pair.json` **last**: kind, timestamp, schema version, `key_check`, and the SHA-256 of
   both files, with `verified: true`. Flush all files.
8. Publish with one same-filesystem directory rename from the temporary name to
   `data/trading_journal.backup-{kind}-{utc}/`. The rename never replaces an existing directory
   (it fails if the target exists), so a previously valid backup is never overwritten.

**What counts as a valid pair:** a directory with the published-pair name **and** a `pair.json`
whose recorded hashes and `key_check` match both files. Anything else — a temporary directory, a
directory without `pair.json`, or one whose hashes do not match — is **never** treated as a
backup: it is never offered for restore and never counted or deleted by retention. (Its presence
still blocks genuine first creation, §5.3, which is the conservative direction.)

**Failed attempts:** on any failure before step 8, the attempt removes only its own temporary
directory. A temporary directory left by a crash is reported as a warning and removed by the next
backup attempt (it matches only the temporary prefix and never contains a published pair).
Published pairs are never deleted by the backup operation.

**Fallback** (only if a same-filesystem directory rename is unsupported): build and verify in
the temporary directory exactly as above, then create the final directory exclusively, copy the
database and key into it, verify the copies' hashes, and write `pair.json` **last**. Because a
directory without a valid `pair.json` is never a backup, an interrupted publication cannot be
mistaken for one. The atomic rename is the default and is expected to be available (both paths
are on the same NTFS volume under `data/`).

Pairs are taken before every migration and optionally daily. **Retention** is a separate step
run only after a new pair is published and verified, and it operates **only on valid, complete
pairs**: delete the oldest `daily` pair directories beyond seven, always as whole directories.
`premigration` pairs are kept until a separately approved cleanup. Invalid or incomplete
directories are never counted towards the seven and are never deleted by retention.

### 5.5 Restore

1. Stop the backend.
2. Select one valid pair directory (§5.4) and verify it before copying: `pair.json` hashes match
   both files; integrity check `ok`; key check matches.
3. Ensure no `trading_journal.sqlite3-wal` or `-shm` exists at the target; if one does, abort.
4. Copy the database and the key from the **same** pair directory into place; re-apply and
   verify the key ACL.
5. Start the backend: integrity check and key check run before monitoring or reconciliation
   may resume. A mismatch refuses both; no key is generated.

### 5.6 Retention

Decisions, risk assessments, trades, fills, deals, coverage and audit: indefinitely.
Acknowledged or dismissed notifications: 180 days. Observation rows: compacted to changes.
Events and voided fills: indefinitely (immutable / void-only). Daily backups: last seven valid
pairs. Pre-migration backups: until approved cleanup.

---

## 6. Query coverage and history recovery

### 6.1 Terminology

| Term | Meaning |
|---|---|
| `QUERY_COVERED` | The interval was queried successfully and every returned record was processed and committed. It proves the query succeeded; it does **not** prove the terminal held every deal for the interval. |
| `HISTORY_RECOVERING` | Required intervals remain unqueried or not fully processed. |
| `HISTORY_UNAVAILABLE` | Required history could not be retrieved or verified within the recovery policy (A11). |

There is no "complete" status. When the whole required interval is query-covered, the status
reads "`QUERY_COVERED` (query coverage only; broker completeness not verifiable)".

### 6.2 Mechanism

- `history_coverage` stores merged `QUERY_COVERED` intervals (`from_utc`, `to_utc`,
  `fetched_utc`, `deal_count`).
- Required interval: `[history_origin_utc, now − settle]`; `history_origin_utc` = journal
  creation time minus an optional initial lookback (default 0).
- Each MT5 request covers at most the maximum window, halved adaptively when a request exceeds
  its time budget and grown back after fast requests.
- Consecutive windows overlap; deals are deduplicated by HMAC reference.
- After a successful window, its interval and the deal upserts commit in one transaction (the
  checkpoint). A failed request (`None`) adds no coverage and is retried with backoff.
- One window per deal-history cycle; after an outage a catch-up cadence applies, still
  skip-if-busy and subject to the monitor safeguards (§7). Oldest gaps first.
- Progress is reported ("`HISTORY_RECOVERING`: query-covered to T").

**A11 defaults:** maximum window 24 h with adaptive halving; overlap 10 min; settlement delay
2 min; catch-up interval 15 s; unconfirmed-closure recheck 14 days; maximum lookback 365 days.

### 6.3 Empty results and closure

MT5 cannot distinguish "no deals in this window" from "history not held by the terminal". An
empty successful response may establish `QUERY_COVERED` but never confirms a closure. A
disappeared position without OUT deals in query-covered intervals stays `CLOSE_UNCONFIRMED`
with a `data.quality` event, is rechecked for 14 days, then becomes `HISTORY_UNAVAILABLE`,
never closed.

### 6.4 Example: a position opens and closes during a 10-day outage

The last coverage ends at T0. Johanna is offline from T0 to T0 + 10 d. Position P opens at
T0 + 2 d and closes at T0 + 5 d; Johanna never observed it.

1. On restart the gap is `[T0 − 10 min, now − 2 min]`: about ten 24-hour windows at one per 15 s
   (≈ 2.5 min if the worker is idle; longer when cycles are skipped). Status:
   `HISTORY_RECOVERING`.
2. Windows 1–2 return no P deals and become `QUERY_COVERED`.
3. Window 3 returns P's IN deal: a `broker_position` with `source = history_recovery`, state
   `AWAITING_EXIT_EVIDENCE` (not in `positions_get`, so not open now).
4. Window 6 returns P's OUT deals; the volumes balance and the spanning intervals are
   `QUERY_COVERED` → `CLOSED_CONFIRMED`; `position.closed_confirmed` is recorded with
   `occurred_utc` = deal time, `recorded_utc` = now, `recovered = true`.
5. Reconciliation can now match P with manual logbook entries.

If window 6 keeps failing, P stays `AWAITING_EXIT_EVIDENCE` and the journal stays
`HISTORY_RECOVERING`, then `HISTORY_UNAVAILABLE` after the policy expires. If an OUT deal's IN
lies before `history_origin_utc`, P is `ENTRY_OUTSIDE_COVERAGE`; recovery extends backwards
window by window up to 365 days, beyond which P is `HISTORY_UNAVAILABLE`.

---

## 7. Read-only MT5 integration and monitor safeguards

### 7.1 Read-only calls (A8: minimum necessary extensions)

`positions_get`, `orders_get`, `history_deals_get(from, to)`, `history_deals_get(position=raw)`
(cross-check only) and `history_orders_get` (original order SL/TP and type). Allowlists gain only
the fields required: identifiers (converted to HMAC references at once, never persisted),
`reason`, `time_update`. `order_send` and `order_check` stay excluded, enforced by test.

### 7.2 Scheduling and protection of the quotes worker (A3, A12)

| Requirement | Design |
|---|---|
| Worker | The existing quotes worker; no third MT5 connection |
| Market data priority | Monitor jobs use non-blocking lock acquisition (skip if busy) and run only after a 750 ms quiet window with no market-data job running or waiting |
| Bounded execution | Positions/orders cycle: two short calls. Deal history: one bounded window per cycle. Monitor timeout is below the runner's kill threshold. Any monitor timeout → `DEGRADED` + 5-minute backoff, so the monitor cannot repeatedly cause worker respawns |
| No queueing | Fixed-rate schedule with skip; skipped cycles are counted, never queued |
| No concurrency | Same worker lock; one MT5 call at a time |
| Market freshness untouched | A separate `monitor_read` path never touches `_last_success`, `_unavailable`, `_identity`, the server-clock `time_check`, or quote and bar caches. The monitor keeps its own `last_success_utc`; observed position prices are labelled with the monitor read time, never as quotes |
| Health | `DEGRADED` when the effective interval exceeds 2× target over 5 minutes, after 3 consecutive skips, after any timeout, or when the server clock is not verified; `UNAVAILABLE` when MT5 is down |
| Cadence targets | Positions/orders 10 s; deal history 60 s and on any disappearance; recovery catch-up 15 s. Subject to measurement |
| Times | Server epochs converted to UTC via the verified `ServerClock` |

**Live A/B acceptance (A12):** quote p95 latency increase ≤ 100 ms with the monitor on versus
off; zero monitor-attributable quote expiries; zero monitor-caused worker respawns; at least 100
quote requests per period where achievable, with sample counts and limitations reported.
Measured: quote latency p50/p95, quote age, monitor cycle duration, skip rate, runner lock wait,
worker respawn count.

---

## 8. Analytics

Four populations are always separated and labelled:

| Population | Contents | P&L source |
|---|---|---|
| 1. Decision statistics | Decisions by outcome, direction, risk status, eligibility | None (no P&L ever) |
| 2. Manually recorded trades | Closed logbook trades | User-entered `RECORDED` fills and costs (voided fills excluded; `unknown` cost components stay unknown) |
| 3. Broker-verified trades | Logbook trades `MATCHED` to `CLOSED_CONFIRMED` positions | Broker deals (profit + swap + commission + fee) |
| 4. Open / unrealised | Open positions | Monitor observations; never in realised metrics |

Definitions (computed per population, filterable by symbol, timeframe, direction, strategy tag):

- Realised P&L: sum of net results of closed trades.
- Win rate: closed trades with net result > 0 / closed trades.
- Average win / average loss: mean net result of winning / losing trades.
- Profit factor: gross profit / |gross loss|; `null` when there are no losses.
- Realised R-multiple: net result / initial risk, where initial risk uses the recorded initial
  stop-loss, entry price, volume and the instrument value per point; `null` without a recorded
  initial stop-loss.
- Costs: totals by component (commission, swap, fee) with their provenance (broker-deal
  sourced, user-entered, unknown). For manual trades a component marked `unknown` is excluded
  from the total and reported as unknown, never as zero; a trade's net result is then labelled
  "costs incomplete".
- Only `RECORDED` manual fills contribute to volumes, P&L, costs and every metric; `VOIDED`
  fills are excluded everywhere.

Proposals and candidates are never counted as trades; open trades are never realised outcomes.

---

## 9. Audit trail

- Every mutation writes an `audit_log` row (table, record, action, old and new values, actor,
  time) in the same transaction.
- `audit_log` is append-only (UPDATE and DELETE triggers abort).
- Each row stores `prev_hash` (SHA-256 of the previous row), forming a hash chain that tests
  verify.

**Security guarantee — tamper-evident, not tamper-proof:**

- The chain can detect certain accidental or unauthorised modifications or deletions of audit
  rows when it is checked.
- **No independent external audit-head anchor currently exists.**
- An attacker with sufficient access to the database file could rewrite records **and recompute
  the chain**, and the rewrite would not be detected.
- The initial implementation therefore provides **internal tamper detection only**, not
  protection against a privileged attacker rewriting the entire database. No external audit
  service or dependency is introduced.

---

## 10. API

All routes follow existing conventions (JSON, `no-store`, Host/Origin checks, opaque Johanna
IDs; no raw identifiers, HMAC references or key material in any response). Write routes are
local-only, JSON-only, ≤ 16 KB, and touch only Johanna's journal (A5).

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/journal/decisions?symbol&timeframe&outcome&eligibility&execution&cursor` | Paged decisions with the five dimensions and `coverage: "request_time"` |
| GET | `/api/journal/decisions/{id}` | Detail: `t6_evidence`, `risk_assessments[]`, supersession chain |
| GET | `/api/trades?status&symbol&cursor` | Logbook with reconciliation status |
| POST | `/api/trades` | Create a manual trade |
| GET | `/api/trades/{id}` | Detail: fills, links, matches, audit |
| PATCH | `/api/trades/{id}` | Amend notes, SL/TP, rationale (audited) |
| POST | `/api/trades/{id}/fills` | Add entry, partial exit or exit |
| POST | `/api/trades/{id}/void` | Void with reason (no DELETE route) |
| POST | `/api/trades/{id}/links` | Link decisions |
| GET | `/api/monitor/positions`, `/api/monitor/orders` | Observed state, monitor health and history status |
| GET | `/api/monitor/history?since` | Observed closures and deals (no identifiers) |
| GET | `/api/reconciliation?status` | Matches and candidates |
| POST | `/api/reconciliation/{id}/confirm`, `/reject` | User decision |
| GET | `/api/events?since&type` | Event log |
| GET | `/api/notifications?status` | In-app inbox (read-only; never changes status) |
| POST | `/api/notifications/{id}/presented` | Client reports actual presentation (idempotent) |
| POST | `/api/notifications/{id}/ack`, `/dismiss` | User actions (idempotent, audited) |
| GET | `/api/analytics/summary?population&group_by&since` | Metrics per population |

---

## 11. Frontend

| Page (route) | Content |
|---|---|
| Decisions (`#signals`, label and heading "Decisions"; A6) | Timeline per symbol/timeframe; five dimensions; supersession chain; coverage disclosure |
| Positions (`#positions`) | Observed positions and pending orders, freshness, monitor health, history status; "record this trade" prefill after explicit confirmation |
| Trade History (`#history`) | Logbook (create, edit, fills, void); reconciliation review queue (confirm/reject affects Johanna records only); deal evidence |
| Analytics (`#analytics`) | Four separated populations; cost verification status |
| Top bar | Notification inbox (presented / acknowledge / dismiss) |

No buy, sell, close or execute controls. Broker values labelled "observed"; manual values
labelled "recorded". `UnimplementedSignalService` is retained until its dependencies are
verified; the `test_browser_smoke` NAV expectation for `#signals` is updated in P5.1.

---

## 12. Testing and live verification

- Unit: fingerprints and observation counters; supersession; five-dimension derivation;
  logbook validation; reconciliation scenario table (exact, partials, scale-in, ambiguous, late
  deals, discrepancy); analytics formulas with open trades excluded.
- Store (`tmp_path`): migrations; mutability triggers (evidence, `event`, `audit_log`,
  `manual_fill` void-only); fail-closed on corruption; key creation/refusal matrix; failed first
  creation removes only its own files (including `-wal`/`-shm`) and never pre-existing ones; ACL
  application and verification (mocked `icacls` on non-Windows); WAL-safe backup with
  temporary-directory build, pair verification and atomic publication; injected failures at every
  backup step leave no valid-looking pair and never touch existing pairs; retention counts and
  deletes only valid pairs; restore with pairing checks; audit chain verification; idempotent
  replay; restart recovery; concurrent writes.
- Logbook: fill voiding (reason required, audited, no deletion), corrections via
  `corrects_fill_id`, voided fills excluded from volume validation, P&L, costs and analytics;
  per-component cost provenance.
- Events: UPDATE and DELETE on `event` abort; duplicate `dedup_key` rejected; notification
  changes never touch `event`.
- Monitor: injected worker contention (non-blocking admission, quiet window, skip counting);
  timeout → `DEGRADED`; market freshness fields untouched; raw identifiers never persisted,
  logged or exposed.
- History: checkpoint, overlap, adaptive halving, HMAC deduplication, the 10-day-outage
  scenario, empty-result handling, `HISTORY_UNAVAILABLE` after policy expiry.
- `FakeMT5`: scripted position lifecycles (open → SL change → partial close → disappear → late
  deals) with the read-only call set enforced.
- Browser: each page; no execution controls; per-page polling gates; no identifiers in the DOM;
  notification presentation only on real display.
- Live (read-only): the monitor reflects the real account; deal history read and redacted;
  A/B latency measurement (§7.2). Johanna never trades; any live trade is placed manually by the
  user, by their own choice.
- Full suites on Python 3.11.8 and 3.13.3 (SyntaxWarnings as errors), Ruff check and format,
  `node --check`.

---

## 13. File-level plan

| File | Change |
|---|---|
| `src/johanna/journal_store.py` | New: store, migrations, triggers, write lock, audit chain, key handling, backup/restore |
| `src/johanna/decision_journal.py` | New |
| `src/johanna/trade_logbook.py` | New |
| `src/johanna/trade_monitor.py` | New: `BackgroundService` task, diffing, checkpointed recovery |
| `src/johanna/reconciliation.py` | New: pure scorer and service |
| `src/johanna/events.py` | New: `EventBus`, `NotificationCenter`, `InAppChannel` |
| `src/johanna/trade_analytics.py` | New: pure metrics |
| `src/johanna/adapters/mt5.py` | Minimal read-only allowlist extensions and wrappers (A8) |
| `src/johanna/live_market.py` | Monitor jobs on the quotes worker; non-blocking admission; `monitor_read` path |
| `src/johanna/application.py` | Wiring; journal capture hooks; monitor registration |
| `src/johanna/api.py` | Routes (§10) |
| `src/johanna/web/app.js`, `styles.css` | Pages and inbox (§11) |
| `tests/fake_mt5.py` | Scripted lifecycles; added read-only calls |
| `tests/test_journal_store.py`, `test_decision_journal.py`, `test_trade_logbook.py`, `test_trade_monitor.py`, `test_history_recovery.py`, `test_reconciliation.py`, `test_events.py`, `test_trade_analytics.py`, `test_browser_trading.py` | New |

---

## 14. Milestones and acceptance criteria

| Milestone | Acceptance |
|---|---|
| **P5.1** Decision journal and store | Only the approved files are created, and only on genuine first creation; key is 32 bytes with a verified user-only ACL; missing key or failed key check refuses monitoring and reconciliation and never regenerates the key; evidence UPDATE/DELETE triggers abort; 100 identical evaluations give 1 decision with `observations = 100`; a risk change appends an assessment only; five dimensions independent; a candidate with unverified risk is `NOT_APPROVED` with execution `NONE`; coverage disclosure present; corrupt database → refusal, never data loss; a failed first creation removes only the files that attempt created (including `-wal`/`-shm`), never pre-existing files or backups, refuses initialisation and never creates a replacement key; backups use the online backup API, are built and verified (integrity, key pairing, ACL, `pair.json` hashes) in a temporary directory and published by one atomic directory rename, never overwriting; injected failures at any backup step leave no valid-looking pair; retention acts only on valid complete pairs; audit chain verifies (tamper-evident only); journal failure never affects analysis or risk responses; full suites green on 3.11 and 3.13 |
| **P5.2** Manual logbook | Over-closing rejected (counting `RECORDED` fills only); trades and fills are voided, never physically deleted; a fill void requires a reason, is audited and is one-way (`RECORDED → VOIDED`); corrections are new fills linked by `corrects_fill_id`, with the original evidence preserved; voided fills are excluded from volume validation, P&L, costs and analytics; commission, swap and fee each carry their own provenance (`user_entered` / `unknown`) and unknown components are never zero; every change audited; prefill only after explicit confirmation; linking sets `MANUALLY_RECORDED` only |
| **P5.3** Monitor | Non-blocking admission and quiet window under injected contention; skips counted, never queued; timeout → `DEGRADED` + backoff; market freshness fields untouched; raw identifiers never persisted, logged or exposed; disappearance never closes a position; live A/B thresholds met with sample counts reported |
| **P5.4** History recovery and reconciliation | Checkpointed windows with overlap and adaptive halving; HMAC deduplication; intervals `QUERY_COVERED`; status never claims broker completeness; empty responses never confirm closure; the 10-day-outage test passes (`AWAITING_EXIT_EVIDENCE` → `CLOSED_CONFIRMED`); `HISTORY_UNAVAILABLE` after policy expiry; scenario table passes; `AMBIGUOUS` never auto-matched; no overwrites; idempotent replays; `BROKER_VERIFIED` only with deal evidence |
| **P5.5** In-app notifications | `event` rows are immutable (UPDATE and DELETE triggers abort, tested) and `dedup_key` stays unique; notification lifecycle changes happen only in the audited `notification` table; `PENDING → AVAILABLE → PRESENTED → ACKNOWLEDGED`, `DISMISSED` separate; GET never marks presented; deduplicated across restart and replay; recovered events flagged late; no external channel code |
| **P5.6** Analytics and final verification | Four populations separated; proposals and candidates never counted as trades; open trades never realised; R-multiple only with a recorded initial stop-loss; cost verification status shown; live read-only verification report |

---

## 15. Approvals

| # | Decision | Status |
|---|---|---|
| A1 | Separate database `data/trading_journal.sqlite3` (+ SQLite sidecars); permission covers only this database and its backups, not other `data/` contents | Approved |
| A2 | WAL-safe backup pairs (online backup API) before each migration; optional daily pairs, last seven kept; pre-migration pairs kept until approved cleanup | Approved |
| A3 | Background monitor on the existing quotes worker, with the §7 safeguards; no third connection | Approved |
| A4 | HMAC-SHA256 references; raw identifiers transient only; `position=raw` from memory; windowed history fallback | Approved |
| A5 | Local-only write endpoints using existing API security conventions | Approved |
| A6 | `#signals` route with label and heading "Decisions"; `UnimplementedSignalService` retained until dependencies verified | Approved |
| A7 | Request-time capture only, disclosed; no scheduled evaluation; no collectors | Approved |
| A8 | Minimum necessary read-only MT5 allowlist extensions | Approved |
| A9 | Key file `data/trading_journal.key`; genuine first creation only; never regenerated automatically | Approved |
| A10 | Built-in Windows `icacls`, explicit arguments, no shell, user-only ACL with read-back verification | Approved |
| A11 | History recovery: max window 24 h (adaptive halving), overlap 10 min, settlement 2 min, catch-up 15 s, unconfirmed-closure recheck 14 days, max lookback 365 days | Approved |
| A12 | Monitor thresholds: quote p95 increase ≤ 100 ms, zero monitor-attributable quote expiries, zero monitor-caused worker respawns; A/B with ≥ 100 quote requests per period where achievable | Approved |
| A13 | Retention: decisions, trades, deals and audit indefinitely; acknowledged/dismissed notifications 180 days | Approved |

---

## 16. Limitations and implementation considerations

- **Broker history completeness is not verifiable.** `QUERY_COVERED` proves only successful
  queries; some closures may remain `CLOSE_UNCONFIRMED` or `HISTORY_UNAVAILABLE`.
- **SL/TP changes are sampled**; changes reverted between monitor cycles are missed (MT5 exposes
  no modification history).
- **Journal coverage is request-time only**, not continuous market coverage.
- **No key rotation**; a lost key permanently disables reference matching for existing records.
  Backup pairs are the mitigation.
- **Audit chain is tamper-evident only**; no external anchor (§9).
- **ACL hardening** protects against other Windows users, not against the same user or an
  administrator; it depends on `icacls` being available.
- **Pre-migration backups accumulate** until a separately approved cleanup.
- **Recovery time grows with outage length** (about one window per 15 s when the worker is idle;
  slower under quote load).
- **Trades placed from other devices** appear once the terminal syncs account history.
- **Existing behaviour outside scope:** Phase 3/4 reads update `_last_success` (connection status
  only; quote freshness is unaffected). Not changed by Phase 5.
- **Implementation considerations for P5.1:** the `test_browser_smoke` NAV title for `#signals`
  changes; ULID generation needs a stdlib implementation (no new dependency); `icacls`
  behaviour must be mocked in non-Windows tests.
