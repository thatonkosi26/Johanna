# P6.2b.4 — Operational Reliability and Reconciliation Corrective Work

Scope: M-6 / L-5, M-7, M-8, M-9 and C-12, implemented in source and verified offline on
temporary SQLite journals. No production file was touched, no service activated, nothing
restarted. Schema v6 is proposed and **staged, not registered**.

## A. Verdict

**COMPLETE.** All six findings (M-6, L-5, M-7, M-8, M-9, C-12) are implemented in source and
verified offline; schema v6 is proposed and staged but **not registered**; every new capability
is inactive by default. Final verification: three consecutive clean full-suite runs on each of
Python 3.11 and 3.13 (2,190 passed, 1 skipped, 0 failed, every run). Production was not
accessed. Limitations that remain by design or need your decisions are listed per finding
(section B) and in section G.

## B. Finding-by-finding results

| Finding | Implementation | Tests | Remaining issues |
|---|---|---|---|
| **M-6** concurrent / duplicate delivery | `notifications.py`: every attempt is **claimed** in its own write transaction before the adapter is called (v6 `notification_claim`, `UNIQUE (notification_id, adapter, attempt)`); only the claim owner calls the adapter; the outcome is recorded against the claim. DB guarantees: a delivery row needs its claim (trigger) and at most one `delivered` row per notification (partial unique index). A second worker sees a fresh claim → `in_progress`; a lost race → `conflict`; neither raises. `sqlite3` errors never escape `deliver_pending` (`journal_error`). A claim whose owner never reports back becomes a visible failed attempt `claim_expired` after 5 min and is retried deterministically (next attempt number). Diagnostics per notification: `not_attempted / in_progress / claim_stale / failed_retryable (+last_error) / exhausted / delivered`. On v5 journals delivery is refused (`claims_not_installed`) — fail closed. | `test_p624_notifications.py` (11): blocking-adapter interleaving (second worker never delivers an in-flight attempt), 6 workers behind a barrier deliver 5 notifications exactly once, exact race-window claim conflict (no adapter call, rolled back), DB refuses a second success and a claimless outcome, crashed owner → `claim_expired` → retry, retryable vs exhausted, deterministic attempt numbers, DB errors contained, v5 fail-closed. Existing `test_notifications.py` moved to v6 journals. | A crash **after** the adapter succeeded but **before** the outcome was recorded leads to a retry after expiry, i.e. at-least-once across a crash. Local adapters are idempotent; any future external adapter needs its own duplicate protection. Effective only once v6 is registered. |
| **L-5** audit coverage | Audited: `notification.created`, `.delivered`, `.failed` (adapter, attempt, error code — incl. `claim_expired`), `.exhausted` (once, at the last permitted failure), `.presented` (was not audited), `.acknowledged` (actor `user`). Explicit transitions raise `NotificationError(code)` (`not_installed / not_found / invalid_transition`) instead of raw `sqlite3.IntegrityError`. | counts of each audit action; actor check; chain valid; `invalid_transition` / `not_found` | — |
| **M-7** backups | New `backups.py`: `BackupPolicy` (default **disabled**, `keep_daily ≥ 1`), `BackupManager.create_routine()` (explicit only; journal ready + verified key; free-space precheck `2 × (db + wal) + 64 MB`; `JournalStore.backup` = SQLite online backup, verified, published by one rename; re-validated), `inventory()`, pure `retention_plan()` (only valid routine pairs beyond `keep_daily`; never pre-migration, never invalid (→ manual review), never the newest valid pair), `prune(apply=False)` (dry run by default; re-validates every kept pair before deleting; refuses otherwise), `restore_rehearsal(pair, target)` (empty target outside the journal directory; opened at the pair's own schema version; key, audit and row counts). No scheduler or thread exists. | `test_p624_backups.py` (18): creation + WAL consistency (committed WAL rows included, an uncommitted transaction excluded, self-contained file), key mismatch, corrupt database, audit-chain failure, interruption at 4 points (existing pairs byte-identical, no temp left), insufficient space, unready journal, repeated backups, retention rules, recovery-point preservation, dry-run vs apply, refusal when a kept pair stops validating, restore rehearsal and its refusals, AST check that nothing calls `create_routine`/`prune_daily` and no Timer/Thread. | Schedule, retention and production use need approval. Disk-space precheck uses `shutil.disk_usage` (filesystem-level). |
| **M-8** readiness cost | `operations.ReadinessEvidence` (one per application, lock-serialised): **audit chain** — full verification cached with its anchor; later requests require the anchor row's hash unchanged and `count == max(seq)`, and verify every new row from the anchor hash; full re-verification at least every 15 min, or on any mismatch. **Pairs** — validation reused only while the three files' size + mtime are unchanged and for ≤ 6 h. Only valid results are cached (invalid is re-checked each request). `quick_check` / `foreign_key_check` still run every request. Output states `mode`, `evidence_age_seconds`, `validated_now` / `reused`. | `test_p624_readiness.py` (10): incremental = full; only new rows verified; tampered new row detected; changed anchor / deleted rows → full and invalid; documented limit demonstrated (older-row edit keeping its hash caught by the next full check within the bound); stale evidence → full; pair reuse, change, max age; missing/corrupt pairs never counted; tampered journal is `attention` on 3 consecutive requests; 8 concurrent requests consistent (exactly one full verification); no MT5 job, backup, notification or observation. | Window ≤ 15 min for an older-row edit that keeps its stored hash (needs triggers dropped first). First request still pays the full cost. See performance table (section E). |
| **M-9** linking + discrepancies | `reconciliation.py`: every result has `detail.link_status` (`USER_CONFIRMED / RECORDED_REFERENCE / CANDIDATE / AMBIGUOUS / REJECTED / NONE / UNAVAILABLE`); a candidate's position is no longer put in the result's `position_ref` (it is `candidate_position_ref`), so a stored candidate never looks linked. User-confirmed links decide a trade's position and claim it against other trades; rejected pairs are never offered again. Unavailable evidence has explicit `reason_code` (`no_observation`, `observation_not_live` — incl. missing broker identifiers —, `observation_stale`, `observation_from_future`, `observation_time_unknown`). Discrepancies, only against broker-confirmed facts of a known position: `LOGBOOK_CLOSED_BROKER_OPEN`, `BROKER_CLOSED_LOGBOOK_OPEN`, `IDENTIFIER_CONFLICT`, `SYMBOL_MISMATCH`, `DIRECTION_MISMATCH`, `VOLUME_MISMATCH` (uses RECORDED fills, so corrections count), `MULTIPLE_POSSIBLE_MATCHES`, `LINKED_POSITION_NOT_OBSERVED`. New `trade_links.py` (`confirm / reject / revoke`, user-only, must reference a stored LIVE observation containing the position, symbol mismatch refused, audited, `LinkError` codes). v6 `trade_broker_link` (exclusive active confirmation per position and per trade, revocation names its confirmation, once) and `reconciliation_discrepancy` (non-null subject key, unique per observation). `broker_monitoring` loads decisions and persists discrepancies in the same transaction; `latest()` returns them. API: `GET /api/reconciliation/links` (read-only); `POST` answers **403 `not_activated`** (no body read). | `test_p624_links.py` (28): every listed case — one candidate, several candidates, two trades / one deal, missing identifiers, duplicate observations, user confirmation, rejection, logbook-closed/broker-open, broker-closed/logbook-open, symbol, direction and volume mismatches, stale observation, repeated reconciliation, correction-linked fills — plus exclusivity/revocation, validation codes, identifier conflict, linked position not observed, persisted append-only discrepancies, v5 behaviour, inactive endpoint. | Confirmation endpoint and any UI inactive (approval). Persistence of decisions/discrepancies needs v6. |
| **C-12** evidence | New `evidence.py` (`EvidenceRegistry`): `record` (kind, source, symbol, topic, `VERIFIED/UNVERIFIED`, observed / valid-until times, JSON content; canonical fingerprint — duplicates return the existing record; keyed MAC via new `JournalStore.keyed_digest` (HMAC-SHA-256, domain-separated, refuses without a verified key)), `correct` (new record superseding one, once, with a reason; subject cannot change), `link` to decisions / risk assessments (FK-checked, idempotent), `verify` (fingerprint + MAC), `assess` (per required item `MISSING / STALE / CONFLICTING / UNVERIFIED / VERIFIED`; `sufficient` only if all `VERIFIED`; an empty requirement list is not sufficient). Sensitive content (account, login, ticket, server, password, identifier keys, ≥ 7-digit numbers, integers ≥ 10⁶, NaN/inf) refused. Every write audited. v6 `evidence_record` / `evidence_link`, immutable. | `test_p624_evidence.py` (26): creation, decision and risk-assessment association, missing, unverified, stale, conflicting, invalid references, duplicate submissions/links, corrections (original kept, chain followed, single correction, subject fixed), MAC tamper detection, HMAC required, audit chain, sensitive and invalid content, v5 `not_installed`. | Nothing records evidence yet (no wiring into capture or risk). The review's original C-12 (a recording route/CLI for **cost** evidence) is not added; the cost registry's `record_evidence` still has no route. The integer ≥ 10⁶ rule is deliberately conservative. |

## C. Architecture changes

| File | Change |
|---|---|
| `src/johanna/journal_store.py` | `SCHEMA_V6` + `STAGED_MIGRATIONS = ((6, SCHEMA_V6),)` (not in `MIGRATIONS`); `JournalStore.keyed_digest()`; `verify_audit_rows(..., start_seq, previous)` for anchored verification |
| `src/johanna/notifications.py` | claim-based delivery, `NotificationError`, audits, delivery diagnostics, `claims_installed()` |
| `src/johanna/operations.py` | `ReadinessEvidence`; readiness uses it; staged-schema text "v6 staged, not registered"; notification remediation anchor |
| `src/johanna/reconciliation.py` | `links=` input, `link_status`, `reason_code`, discrepancy pass, `discrepancies()` |
| `src/johanna/broker_monitoring.py` | loads link decisions, persists discrepancies, `latest()` returns them, `links_installed()` |
| `src/johanna/application.py` | constructs `TradeLinkService`, `EvidenceRegistry`, `BackupManager(BackupPolicy(enabled=False))`, `ReadinessEvidence` (nothing started) |
| `src/johanna/api.py` | `GET /api/reconciliation/links`; `POST` → 403 `not_activated` |
| `src/johanna/backups.py` | **new** (M-7) |
| `src/johanna/trade_links.py` | **new** (M-9) |
| `src/johanna/evidence.py` | **new** (C-12) |
| `docs/operations-runbook.md` | notification lifecycle and failures, backup policy and recovery, readiness freshness, linking and discrepancy classes, evidence lifecycle, schema v5 (installed) and v6 (staged), activation requirements — all marked inactive |
| tests | **new** `test_p624_notifications.py` (11), `test_p624_backups.py` (18), `test_p624_readiness.py` (10), `test_p624_links.py` (28), `test_p624_evidence.py` (26), `test_p624_schema_v6.py` (15) = 108. **Changed** (intended): `test_notifications.py` (delivery on v6 journals; controlled `NotificationError`), `test_cost_policy.py` and `test_p2_schema_v5.py` (v6 now staged), `test_operations.py` (staged text), `test_p6_corrective.py` (test stub accepts the new `links=` keyword) |

No dashboard JavaScript change (`app.js` displays the readiness `staged_schema` text, whose
value changed).

## D. Schema

**Schema v6 is proposed, staged, not registered** (`MIGRATIONS` ends at v5; tests assert it).
Additive only — no v1 – v5 table, index or trigger is altered:

| Object | Purpose |
|---|---|
| `notification_claim` (+ index, append-only triggers) | M-6 attempt ownership, `UNIQUE (notification_id, adapter, attempt)` |
| trigger `notification_delivery_needs_claim`, unique index `notification_delivered_once` (on the v5 table) | a delivery outcome needs its claim; at most one success per notification |
| `trade_broker_link` (+ 2 indexes, `trade_broker_link_exclusive`, `trade_broker_link_revocation`, append-only) | M-9 user decisions, actor `user` only |
| `reconciliation_discrepancy` (+ index, append-only) | M-9 findings, `subject_key NOT NULL`, `UNIQUE (observation_id, code, subject_key)`, code CHECK |
| `evidence_record` (+ index, immutable) / `evidence_link` (+ index, append-only) | C-12 |

Exact SQL: `johanna.journal_store.SCHEMA_V6` (5 tables, 7 indexes, 13 triggers).
v5 → v6 tested on temporary journals (`test_p624_schema_v6.py`, built on the Phase 2
representative journal migrated to v5 with v5 rows): every row and every v1 – v5 object
preserved, exactly the v6 objects added, integrity / foreign keys / audit chain / key OK, valid
`premigration-v5` pair; four induced failures roll back completely and a retry succeeds;
idempotent; missing / wrong key, corrupt journal, future version 7 refused without change;
**the registered v5 code refuses a v6 journal** (`schema_unsupported`). One refusal case is a
property of the design: a journal that already holds two successful deliveries of one
notification cannot get the delivered-once index, so its migration fails and rolls back
(production has no notification rows, per P6.2b.3).

## E. Testing

Environment: Windows 11; Python 3.11.8 (`.venv311`) and 3.13.3 (`.venv`); ruff 0.17.2; node
v23.9.0. Every run used `PYTHONDONTWRITEBYTECODE=1` and
`python -m pytest -q -W error::SyntaxWarning -p no:cacheprovider -rfEs tests` (focused runs: the
files listed). Logs (scratchpad): `p624_focused_py3{11,13}.log`, `p624_run{1,2,3}_py3{11,13}.log`,
`p624_dev1.log`.

**Development run (before the final runs; not clean — reported in full).** After the source
changes and before updating existing tests: `9 failed, 2073 passed, 1 skipped`. Eight were the
intended consequences already planned (v5 notification journals now refuse delivery
→ 5 in `test_notifications.py`; v6 now staged → `test_cost_policy.py::
test_schema_v5_is_registered_exactly_once`, `test_p2_schema_v5.py::
test_v5_is_registered_exactly_once_after_unchanged_v1_to_v4`; staged text →
`test_operations.py::test_healthy_manual_operation_and_unattended_never_permitted`). The ninth
was unplanned: `test_p6_corrective.py::test_m3_persistence_errors_are_reported_and_rolled_back`
— `TypeError: corrupt() got an unexpected keyword argument 'links'` (the test's stand-in for
`reconcile` lacked the new keyword); the stub now forwards keywords, its assertions unchanged.
New-test first runs exposed only test mistakes (an unordered query; a race simulation that
inserted the competing claim too early; a wrong expected row count), each fixed in the test.

**Focused runs** (`test_p624_*` ×6, `test_notifications`, `test_journal_store`,
`test_operations`, `test_broker_monitoring`, `test_p6_corrective`, `test_p2_schema_v5`,
`test_cost_policy`):

| Python | Result |
|---|---|
| 3.11.8 | 367 passed, 1 skipped, exit 0 |
| 3.13.3 | 367 passed, 1 skipped, exit 0 |

**Full suite — every final run** (2,191 collected = 2,082 + 108 new + 1 skip):

| Run | Python | UTC | Result |
|---|---|---|---|
| 1 | 3.11 | 07:02:16–07:15:39 | 2190 passed, 1 skipped, exit 0 |
| 1 | 3.13 | 07:15:39–07:29:01 | 2190 passed, 1 skipped, exit 0 |
| 2 | 3.11 | 07:29:02–07:42:42 | 2190 passed, 1 skipped, exit 0 |
| 2 | 3.13 | 07:42:42–07:56:07 | 2190 passed, 1 skipped, exit 0 |
| 3 | 3.11 | 07:56:08–08:08:55 | 2190 passed, 1 skipped, exit 0 |
| 3 | 3.13 | 08:08:55–08:22:03 | 2190 passed, 1 skipped, exit 0 |

Skip in every run: the existing POSIX `chmod` test (`tests/test_journal_store.py:107`). No
intermittent failure occurred in these runs (the Phase 2 browser timeout did not recur; it
remains unexplained). Existing safety and fail-closed tests all pass.

**Suite duration (investigated, cause not established).** Each full run took about 12 min 40 s
– 13 min 35 s, against about 7 min 35 s in Phase 2. A separate diagnostic run with
`--durations=40` (Python 3.13: 2190 passed, 1 skipped, 13 min 02 s; log `p624_durations.log`)
shows the slowest tests are existing timing-based browser and MT5-worker tests (e.g.
`test_browser_smoke.py::test_failures_back_off_without_a_tight_loop_and_polling_recovers`
15.7 s); **none of the 108 new tests is among the 40 slowest** (all below 2.3 s; together about a
minute). Phase 2 recorded no per-test durations, so the comparison cannot show which tests
became slower; machine load (for example the production backend, restarted in P6.2b.3 with
newer code and an open dashboard) is a possible but unverified explanation. No test failed or
timed out.

**Static checks.** `ruff check src tests scripts` — all checks passed. `ruff format --check src
tests scripts` — 160 files already formatted (explicit paths so that nothing walks `data/`).
`node --check src/johanna/web/app.js` — OK.

**M-8 performance** (synthetic temporary journals; measured while a test suite was running in
parallel, so absolute times are noisy; "baseline" = previous implementation: `quick_check` +
`foreign_key_check` + full audit verification + full validation of every pair):

| Journal | Audit rows | Pairs | Baseline per request | Revised first request | Revised later requests (median) | Revised after 100 new rows |
|---|---:|---:|---:|---:|---:|---:|
| small | 1,000 | 2 | 0.122 s | 0.199 s | 0.032 s | 0.020 s |
| medium | 20,000 | 5 | 3.505 s | 3.657 s | 0.063 s | 0.064 s |
| large | 100,000 | 10 | 48.207 s | 45.767 s | 0.392 s | 0.401 s |

Production's journal had 4,567 audit rows and one pair at the P6.2b.3 verification (between
the small and medium cases).

## F. Safety verification

| Item | Status |
|---|---|
| Production `data/` accessed | **No.** All tests used pytest temporary directories; searches and ruff/pytest ran on explicit `src`, `tests`, `scripts`, `docs` paths. The only project-root listing (`ls -a`) shows the top-level name `data` without entering it. |
| Production journal modified | **No** |
| Production migration run | **No** (v6 not registered) |
| Backend restarted | **No**; no production API called |
| Scheduler | OFF (unchanged; not touched) |
| Monitoring / reconciliation | inactive (no thread, schedule or call) |
| Notifications | inactive (nothing calls `notify`/`deliver_pending`; local adapters only) |
| Automatic backups | inactive (`BackupPolicy.enabled = False`, no scheduler) |
| Linking endpoint | inactive (`POST` → 403) |
| Production CostTerms | empty, unchanged (`risk_engine.py` not modified) |
| Risk policy / commission / slippage / rollover / holding | unchanged |
| MT5 | **no calls** (in-process fake only) |
| Broker actions | **none** |
| Autonomous execution | disabled |
| Git | **no commands** |
| Dependencies | none installed |

## G. Remaining blockers and decisions

| Item | Needs |
|---|---|
| Schema v6 registration + production migration | your review of section D, then a separate approval (implementation of registration, your restart, read-only verification) |
| Notification delivery activation | v6 in production + approval of what triggers delivery; external adapters need duplicate protection |
| Automatic backups | approval of schedule, retention, pruning |
| Linking confirmation endpoint / UI | review and approval |
| Evidence recording | decision on what Johanna records and when; wiring into capture/risk |
| Original C-12 (cost-evidence recording route/CLI) | decision (not implemented) |
| M-8 residual | the ≤ 15 min window and first-request cost (documented) |
| Monitoring schedule, cost evidence (C-1 – C-3), C-5 probe, C-6 rollover verbatim, C-9 registry→risk | your decisions / evidence (unchanged) |
| Intermittent browser timeout (Phase 2) | still unexplained; see section E for this phase's runs |
