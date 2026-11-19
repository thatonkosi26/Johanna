# P6.2b.5 — Schema v6 Registration and Offline Migration Verification

Scope: register the staged schema v6 after v1 – v5 and verify v5 → v6 offline on temporary
SQLite journals. Production `data/` was not accessed; the backend was not restarted.

> **Consequence of this phase:** v6 is now in `MIGRATIONS`. The **next start of the backend
> from this source tree will migrate the production journal from v5 to v6** (after a verified
> `premigration-v5` backup). That start would also put the P6.2b.4 code live for the first time
> (the running backend was started at 04:40 UTC, before P6.2b.4). Do not restart until you have
> decided on the controlled production migration.

## A. Verdict

**COMPLETE WITH CAVEATS.** Schema v6 is registered exactly once after unchanged v1 – v5 and
is verified offline (schema, preservation, v6 constraints, rollback at six stages, eight refusal
cases, idempotency); production remains on v5 and every new capability stays inactive.
**Caveat:** the required "three consecutive clean runs" was not met by the first Python 3.11
sequence — run 3 had one failure in a pre-existing browser test
(`test_browser_overlay.py::test_repeated_toggling_adds_no_requests_timers_or_listeners`). Its
cause was identified with direct evidence as a snapshot race in that test (section E), unrelated
to the migration; the test was **not** changed (outside this phase's scope). Three further
consecutive Python 3.11 runs were clean; Python 3.13 had three consecutive clean runs.

## B. Registration results

**Review before changing anything.** Read `P624_OPERATIONAL_RELIABILITY_REPORT.md`; inspected
`SCHEMA_V6`, `STAGED_MIGRATIONS`, `MIGRATIONS`, `migration_sequence_valid`, the pre-migration
backup path (`backup` → `validate_pair`), `JournalStore._open` / `_open_existing` /
`_apply_migration`, readiness diagnostics, the runbook and `test_p624_schema_v6.py`. The staged
tests applied exactly `js.SCHEMA_V6` (via `STAGED_MIGRATIONS`), so the staged schema and its
tests were the same migration. Checks made on the DDL itself:
- `evidence_record` compares `valid_until_utc >= observed_utc` as text; correct because every
  write goes through `utc_iso` (fixed width: microseconds and `Z`).
- The additive objects on the v5 table `notification_delivery` (trigger
  `notification_delivery_needs_claim`, unique index `notification_delivered_once`) leave that
  table's own definition unchanged; the index makes a journal with two successful deliveries of
  one notification unmigratable (refusal, tested; production had no notification rows at the
  P6.2b.3 verification).
- **No DDL correction was needed; `SCHEMA_V6` is unchanged** (SHA-256 prefix `3242050074b27636`,
  now pinned).

**One correction to the migration runner (not the schema).** `_apply_migration` rolled back
explicitly only on `sqlite3.Error`; any other exception between `BEGIN` and `COMMIT` relied on
SQLite discarding the transaction when the connection closed (safe, but implicit). It now
issues `ROLLBACK` for every exception (non-SQLite exceptions are re-raised, so the journal is
refused as `unexpected_error`), and a no-op test hook `before_commit_<version>` sits
immediately before `COMMIT` so that a failure at that exact point can be induced. Tested (D).

**Exact registered sequence** (`src/johanna/journal_store.py`, `MIGRATIONS`, now defined after
`SCHEMA_V6`):

```
MIGRATIONS = ((1, SCHEMA_V1), (2, SCHEMA_V2), (3, SCHEMA_V3), (4, SCHEMA_V4),
              (5, SCHEMA_V5), (6, SCHEMA_V6))
```

- **v1 – v5 unchanged:** SHA-256 prefixes still equal the values pinned in Phase 2 (v1
  `30368f37…`, v2 `7c0469c1…`, v3 `e23ae860…`, v4 `847e1af8…`, v5 `1e590842…`; asserted by
  `test_p2_schema_v5.py::test_released_migration_texts_are_pinned`, now also pinning v6).
- **v6 exactly once;** `migration_sequence_valid(MIGRATIONS)` true; duplicate, gap or missing
  versions refused (`migration_order`) before any backup.
- **`STAGED_MIGRATIONS` removed**, so v6 cannot be represented both as staged and registered
  (tests assert its absence).
- Unchanged and re-verified: future/unsupported versions refused, key verification before
  migration, verified pre-migration backup first, one transaction, `foreign_key_check` before
  commit, rollback on failure, nothing re-applied on a v6 journal.

**Diagnostics and documentation.** Readiness: `staged_schema` now reads "none staged (v6
registered; a v5 journal migrates at the next start after a verified backup)"; the blocking
reason became `schema_not_current` (installed ≠ registered — the previous rule "installed ≠ 5"
would have flagged a correctly migrated v6 journal). Runbook: new section **Schema v6
migration** (contents, trigger, backup, transactional rollback, refusals, failure handling,
rollback to older code, post-migration verification, activation boundary); the notification,
evidence and activation sections no longer call v6 "staged". Module comments updated likewise.
Nothing is described as active.

## C. Offline migration results

**Fixture.** `tests/test_p625_schema_v6.py::build_v5`: the Phase 2 representative journal built
with `MIGRATIONS[:4]` through Johanna's services (shadow scheduler run with cycles, gate outcomes
and shadow intents; decisions and observations; risk assessments; a manual trade with entry
fill, protection, note and decision link; idempotency requests; an event; audit entries), then
migrated with the exact registered `MIGRATIONS[:5]` and given v5-era rows: a notification with
pending/available states and a failed + a delivered attempt, a LIVE broker observation, a trade
and a broker-position reconciliation result, a holding-period cost record and its user
rejection, plus an audit entry and an HMAC broker reference. **All 24 v1 – v5 tables are
non-empty** (asserted). The migration is an ordinary `JournalStore.start()` with the
registered default sequence, as at a backend start.

**Result:** schema 5 → **6**; journal ready; key verified; HMAC available; one valid
`premigration-v5` pair holding the untouched v5 journal.

**Schema objects** (including SQLite's automatic indexes): tables 24 → 29, indexes 51 → 69
(7 named + 11 automatic), triggers 52 → 65. Every v1 – v5 object's SQL is identical; exactly
the expected 5 tables, 7 named indexes and 13 triggers were added; the complete migrated
`sqlite_master` equals a schema built in memory from `MIGRATIONS` (no unexpected object).

**Row counts** (also compared value for value, in order):

| Table | Before (v5) | After (v6) | Identical |
|---|---:|---:|---|
| `audit_log` | 25 | 25 | yes |
| `broker_observation` | 1 | 1 | yes |
| `cost_policy_decision` | 1 | 1 | yes |
| `cost_policy_record` | 1 | 1 | yes |
| `decision` | 4 | 4 | yes |
| `decision_observation` | 4 | 4 | yes |
| `evaluation_cycle` | 3 | 3 | yes |
| `event` | 1 | 1 | yes |
| `gate_outcome` | 78 | 78 | yes |
| `meta` | 3 | 3 | yes |
| `notification` | 1 | 1 | yes |
| `notification_delivery` | 2 | 2 | yes |
| `notification_state` | 2 | 2 | yes |
| `reconciliation_result` | 2 | 2 | yes |
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
| `evidence_link` (new) | — | 0 | n/a |
| `evidence_record` (new) | — | 0 | n/a |
| `notification_claim` (new) | — | 0 | n/a |
| `reconciliation_discrepancy` (new) | — | 0 | n/a |
| `trade_broker_link` (new) | — | 0 | n/a |

**Integrity:** `integrity_check` ok, no foreign-key violation, `foreign_keys` on for the
journal's connections; audit chain valid and the backup's chain an identical prefix; key file
byte-identical; the same HMAC broker reference before and after; `keyed_digest` available.

**New v6 constraints, tested on the migrated journal** (raw SQL with foreign keys on):

| Object | Verified |
|---|---|
| `notification_claim` | unique (notification, adapter, attempt); FK to `notification`; attempt ≥ 1; append-only |
| delivery guarantees | an outcome without its claim refused; a second success refused — including for the notification already delivered in the v5 era; a claimed failure accepted; the service delivers a new notification exactly once on the migrated journal |
| `trade_broker_link` | FK trade; 64-character reference; actor user only; REVOKED needs its target; one active confirmation per trade and per position; revocation must match trade and position; revoke once; re-confirm after revocation; append-only; the service confirms a link on the migrated v5-era observation |
| `reconciliation_discrepancy` | unique (observation, code, subject key); subject key NOT NULL and equal to trade / position; code CHECK; JSON object detail; FK observation; append-only |
| `evidence_record` | fingerprint unique; validity order; JSON object; correction needs a reason; FK and single correction (`supersedes_id` unique); immutable; the service records and verifies a keyed MAC on the migrated journal |
| `evidence_link` | unique; FK evidence, decision and risk assessment; subject consistency and subject key; append-only |

## D. Rollback and refusal results

Every case: temporary journal; for rollbacks the complete `sqlite_master`, every row and the
version (5) are identical afterwards, and a retry with the registered sequence reaches v6.

| Induced failure | Outcome |
|---|---|
| invalid SQL (after all v6 objects) | `migration_failed`, v5, no v6 object, rows unchanged; retry OK |
| NOT NULL failure (after all objects) | same |
| CHECK failure (after all objects) | same |
| foreign-key violation (detected by `foreign_key_check` before commit) | same |
| failure after partial schema creation (claims, links, then error at the discrepancy table) | same |
| failure immediately before `COMMIT` — SQLite error | same |
| failure immediately before `COMMIT` — non-SQLite exception | refused `unexpected_error`, explicitly rolled back, v5 unchanged; retry OK |

| Refusal case | Reason | Verified |
|---|---|---|
| missing journal key | `migration_requires_verified_key` | key not recreated; journal identical; no new pair |
| incorrect key | `migration_requires_verified_key` | key file kept as is; journal identical |
| corrupt journal | `corrupt_database` | file byte-identical; key unchanged |
| invalid audit chain | `backup_audit_invalid` | journal identical; no new pair |
| existing foreign-key violation | `migration_failed` | journal identical (violation not hidden) |
| unsupported future version (7) | `schema_unsupported` | journal identical |
| invalid sequence (v6 twice; gap to 7; v5 missing) | `migration_order` | before any backup; journal identical |
| incompatible notification history (two successful deliveries) | `migration_failed` | journal identical, still v5 |

**Idempotency:** one migration creates v6 and exactly one `premigration-v5` pair; three further
starts leave the whole database identical, create no backup and no object; a fresh journal is
created directly at v6 with empty v6 tables; code registering only v1 – v5 refuses a v6 journal
(`schema_unsupported`).

## E. Regression tests

Environment: Windows 11; Python 3.11.8 (`.venv311`) and 3.13.3 (`.venv`); ruff 0.17.2; node
v23.9.0. Every run: `PYTHONDONTWRITEBYTECODE=1 python -m pytest -q -W error::SyntaxWarning
-p no:cacheprovider -rfEs tests` (focused runs: the files listed). Logs (scratchpad):
`p625_focused_py3{11,13}.log`, `p625_run{1..6}_py311.log`, `p625_run{1..3}_py313.log`.

**Development runs (before the final runs).** After registration, focused runs exposed the
expected test updates (registration now 1 – 6, fresh journals at v6, readiness text/code, v5
fixtures pinned to `MIGRATIONS[:5]`) and one more:
`test_p6_corrective.py::test_v5_corrected_migration_preserves_every_v4_row` — `assert 2 == 1`
pairs: its "second start" used the full registered sequence and therefore continued to v6 with
a second backup; it now uses `MIGRATIONS[:5]` like the rest of that v4 → v5 test. The new module
`test_p625_schema_v6.py` passed on first run (one trivial test was removed as meaningless).

**Focused runs** (`test_p625_schema_v6`, `test_p624_schema_v6`, `test_journal_store`,
`test_journal_v4`, `test_p2_schema_v5`, `test_p6_corrective`, `test_p624_readiness`,
`test_operations`, `test_notifications`, `test_p624_notifications`, `test_p624_links`,
`test_p624_evidence`, `test_p624_backups`, `test_cost_policy`, `test_broker_monitoring`):

| Python | Result |
|---|---|
| 3.11.8 | 404 passed, 1 skipped, exit 0 |
| 3.13.3 | 404 passed, 1 skipped, exit 0 |

**Full suite — every run** (2,218 collected = 2,190 + 27 new + 1 skip):

| Run | Python | UTC | Result |
|---|---|---|---|
| 1 | 3.11 | 08:52:58–09:03:15 | 2217 passed, 1 skipped, exit 0 |
| 1 | 3.13 | 09:03:15–09:12:22 | 2217 passed, 1 skipped, exit 0 |
| 2 | 3.11 | 09:12:22–09:21:05 | 2217 passed, 1 skipped, exit 0 |
| 2 | 3.13 | 09:21:05–09:30:12 | 2217 passed, 1 skipped, exit 0 |
| 3 | 3.11 | 09:30:12–09:38:45 | **1 failed**, 2216 passed, 1 skipped, exit 1 |
| 3 | 3.13 | 09:38:45–09:47:06 | 2217 passed, 1 skipped, exit 0 |
| 4 | 3.11 | 09:52:01–10:01:29 | 2217 passed, 1 skipped, exit 0 |
| 5 | 3.11 | 10:01:29–10:11:23 | 2217 passed, 1 skipped, exit 0 |
| 6 | 3.11 | 10:11:23–10:19:51 | 2217 passed, 1 skipped, exit 0 |

Python 3.13: runs 1 – 3 consecutive and clean. Python 3.11: runs 1 – 2 clean, run 3 failed,
runs 4 – 6 consecutive and clean. Skip in every run: the existing POSIX `chmod` test
(`tests/test_journal_store.py:107`). Every migration, journal and v6 test passed in all nine runs.

**The failure — complete traceback (run 3, Python 3.11):**

```
_________ test_repeated_toggling_adds_no_requests_timers_or_listeners _________
    def test_repeated_toggling_adds_no_requests_timers_or_listeners(page, served) -> None:
        url, _, stub = served
        _ready(page, url)
        ...
        timers = page.evaluate("livePollingState().timers")
        ...
        for _ in range(10):
            page.evaluate("document.querySelector('[data-overlay-mode=\"analysis\"]').click()")
            page.evaluate("document.querySelector('[data-overlay-mode=\"clean\"]').click()")
        assert {k: _listener_count(page, "document", k) for k in kinds} == before
        assert _listener_count(page, "window", "resize") == window_resize
>       assert page.evaluate("livePollingState().timers") == timers
E       AssertionError: assert 5 == 4
tests	est_browser_overlay.py:308: AssertionError
```

**Investigation.** `livePollingState().timers` counts pollers that currently have a scheduled
timer (plus the quote-expiry timer); a poller **in flight has no timer** at that instant, so the
value varies with timing even without any leak (and a duplicate timer could not be counted,
since each poller has one timer slot). Evidence:
- Not reproduced: the single test ×15 and `test_browser_overlay.py` ×3 (90 tests) on 3.11 all
  passed.
- Diagnostic probe (scratchpad `probe_timers_test.py`, same fixtures, **no toggling**): steady
  state is 5 timers with nothing in flight (400 samples over 8 s). Sampling right after the page
  is ready over 12 fresh loads:

```
LOAD 0: first=(5, ()) counts={(5, ()): 60}
LOAD 1: first=(5, ()) counts={(5, ()): 60}
LOAD 2: first=(5, ()) counts={(5, ()): 60}
LOAD 3: first=(5, ()) counts={(5, ()): 60}
LOAD 4: first=(5, ()) counts={(5, ()): 60}
LOAD 5: first=(4, ('bars',)) counts={(4, ('bars',)): 1, (5, ()): 59}
LOAD 6: first=(5, ()) counts={(5, ()): 60}
LOAD 7: first=(4, ('analysis',)) counts={(4, ('analysis',)): 4, (5, ()): 56}
LOAD 8: first=(5, ()) counts={(5, ()): 60}
LOAD 9: first=(5, ()) counts={(5, ()): 60}
LOAD 10: first=(5, ()) counts={(5, ()): 60}
LOAD 11: first=(3, ('bars', 'analysis')) counts={(3, ('bars', 'analysis')): 1, (4, ('analysis',)): 7, (5, ()): 52}
```

  The count is 4 (or 3) exactly while a poller (`bars`, `analysis`) is in flight. The failing
  run's "before" value 4 was taken in such a window; the "after" value 5 is the steady state.
- The dashboard code was not changed in this phase, and the test does not touch the journal
  schema. **Conclusion:** a snapshot race in the test, not a timer leak and not caused by v6.
  Suggested fix (not made; needs your approval as it is outside scope): wait until no poller is
  in flight before each timer snapshot.

**Static checks.** `ruff check src tests scripts` — all checks passed. `ruff format --check src
tests scripts` — 161 files already formatted. `node --check src/johanna/web/app.js` — OK
(`app.js` unchanged).

## F. Safety verification

| Item | Status |
|---|---|
| Production `data/` accessed | **No** — temporary directories only; every command used explicit `src`, `tests`, `scripts`, `docs` paths (pytest `tests`; ruff `src tests scripts`) |
| Production journal modified | **No** |
| Production migration | **None** (production remains on v5 until your restart) |
| Backend restarted | **No**; no production API called |
| Scheduler | OFF (not touched) |
| Monitoring / reconciliation | inactive |
| Notifications | inactive (no trigger calls `notify` / `deliver_pending`) |
| Automatic backups | inactive (`BackupPolicy.enabled = False`, no scheduler) |
| Linking endpoint | inactive (`POST /api/reconciliation/links` → 403) |
| Production CostTerms | empty, unchanged (`risk_engine.py` not modified) |
| MT5 | **no calls** (in-process fake only) |
| Broker actions | **none** |
| Autonomous execution | disabled |
| Git | **no commands** |
| Dependencies | none installed |

## G. Remaining risks and decisions

| Item | Note |
|---|---|
| **Next restart = migration + deployment** | migrates production v5 → v6 and deploys all P6.2b.4 / P6.2b.5 code (not yet running). Requires your approval of a controlled production migration (preflight, your restart, read-only verification). |
| Rollback to older code | refused by design after v6; recovery = manual restore of the `premigration-v5` pair |
| Delivered-once index | would refuse migration of a journal holding duplicate successful deliveries; production had none at P6.2b.3 (re-checked only in the production preflight/verification) |
| Free disk space for the `premigration-v5` pair | not checked (needs `data/`); a shortage fails safely before migration |
| M-6 at-least-once across a crash; M-8 15-minute window | unchanged limitations from P6.2b.4 |
| Activation of notifications, linking endpoint/UI, evidence recording, automatic backups, monitoring | each needs its own approval; UI work not started |
| Browser test snapshot race | `test_browser_overlay.py::test_repeated_toggling_adds_no_requests_timers_or_listeners` can fail when a poller is in flight at its first snapshot (root cause shown in E); fix proposed, not made |
| Uncommitted working tree | all changes since `dea1208` remain uncommitted; a commit before the restart would give a code rollback point (I may not run Git) |
