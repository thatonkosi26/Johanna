# Phase 1 remediation — handoff from Claude Code to Codex

> **Superseded for acceptance purposes by `docs/phase-1-acceptance-report.md`** (later on
> 2026-11-11). Codex is no longer part of the project. This file is kept as the record of the
> first remediation round. Items in §10 below that were later resolved: (1) domain contracts,
> (2) live MT5 monitoring, (5) frontend tests, (6) collector CLI/backend guard.

Date: 2026-11-11. Author: Claude Code (Opus 5.5). Status: **uncommitted working-tree changes**.
Phase 1 is **not** declared complete by this document. Final acceptance needs your independent
verification.

## 0. Read this first

- **Baseline.** I was told you had done several rounds of remediation after my Phase 1 audit.
  None of that work is in this repository. When I started, every file was byte-identical to what
  I audited earlier the same evening (`api.py` still returned 404 for `POST /status`;
  `tests/test_application.py` was unchanged). There were no other branches, stashes or
  worktrees, and `origin/main` is still `88fa1e7`. The user chose to remediate **this tree**.
  If you have remediation work somewhere else (a cloud task, another machine), expect
  conflicts in the files listed in §5. Merge by behaviour, using the tests in §6 as the
  contract.
- **No byte-level baseline exists.** Your Phase 1 files were never committed (they are `??` in
  `git status`), so `git diff` cannot show my changes against your versions. §3 and §5 describe
  every change. Recommendation: commit a baseline before the next round so handoffs can be
  diffed.
- Nothing was committed or pushed. No dependencies were added. No user data under `data/` was
  modified. All verification used temporary copies.

## 1. Original audit findings and status

Status key: **Resolved+verified** (automated test and/or live run), **Implemented, not
verified**, **Partial**, **Unresolved**, **N/A**.

| # | Original finding | Status | Evidence |
|---|---|---|---|
| C1 | Test suite red (`POST /status` → 404, expected 405) | Resolved+verified | `api.py` route table; 149/149 pass on Py 3.11.8 and 3.13.3 |
| C2 | Domain boundaries are names only | **Partial** | Market data and calendar are now real services (`data_services.py`). News, Gemini, signals, rules, risk and execution remain `ManagedService` placeholders by design (Phase 1 forbids implementing them). No typed Protocols were added for them; see §8 D5 |
| H3 | Two backends could share a port/DB | Resolved+verified | `ExclusiveWSGIServer` (no SO_REUSEADDR) and `InstanceLock`. Live: second process on same port exits 1 (`WinError 10048`); second process on another port with same data dir exits 1 (lock) |
| H4 | Non-atomic preference writes | Resolved+verified | `save_preferences` validates everything, then writes in one transaction. Live: rejected request leaves DB watchlist unchanged |
| H5 | `/health` always 200 | Resolved+verified | 503 `unavailable` when stopped; 503 `degraded` when a service is in error |
| H6 | Monitoring state stored twice and inconsistent | Resolved+verified | The preference is the single source; it drives the worker and is mirrored into `service_config`, and reconciled on start |
| M7 | CSRF / DNS rebinding | Resolved+verified | JSON-only POST (415 otherwise); Host allow-list (421); Origin must match Host; static pages send `X-Frame-Options: DENY` and a CSP |
| M8 | Persistence started last | Resolved+verified | `test_persistence_starts_first_and_stops_last` |
| M9a | `BackgroundService` swallowed errors | Resolved+verified | Logs traceback; `error`/`exited` states with timestamps; visible in API and UI |
| M9b | Provider update timestamps never written | Resolved+verified (by design change) | Derived from Parquet manifests and collector state files and exposed as `last_provider_updates.market_data` / `.economic_calendar`. Not copied into SQLite; see §8 D3 |
| M10 | Dashboard overflow at 400px | Resolved+verified | **Correction to my audit:** headless Edge has a ~492px minimum viewport, so the original "400px" screenshot was a crop. Verified now with true 400px iframes. Grid tracks also fixed to `minmax(0,1fr)` |
| M11 | Misleading "Monitoring enabled" badge | Resolved+verified | UI shows the actual worker state (`Running` / `Paused` / `Not configured` / `Failed` / `Stopped unexpectedly`) |
| L12 | JSON logs dropped `extra`; access logs not JSON | Resolved+verified | `JsonFormatter` includes extras; access lines go through `johanna.access` |
| L13 | SQLite connections never closed | Resolved (tests pass; not stress-tested) | `_transaction()` closes connections |
| L14 | Timezone not editable; `system` theme ignored | Resolved, browser-rendered | Settings has timezone and theme selects; `system` follows `prefers-color-scheme` |
| L15 | Page not in URL; poll wiped input | Resolved, browser-rendered | Hash routing (`#markets` renders Markets); background refresh skips the page re-render while an input or select has focus |
| L16 | Chart labelled "OHLC" but draws closes | Resolved | Label is now `CLOSE` |
| — | Watchlist price/change mixed timeframes (found this round) | Resolved+verified | `_watch_row` uses the selected timeframe, otherwise the most recent one |
| — | Duplicate bars across import and collector roots (found this round) | Resolved+verified | De-duplicated on (symbol, timeframe, timestamp); the collector root wins |
| — | Dashboard re-read all Parquet on every poll (found this round) | Resolved+verified | Cache keyed on partition path/mtime/size |
| — | Collector output dirs could diverge from what the app read | Resolved+verified | When `collectors_config` is set, the app reads that file's `output_dir`/`state_dir` |

## 2. What Codex had implemented correctly (preserved)

- Config loading and validation (`config.py`): unknown keys, ranges, env overrides,
  `execution_enabled` refusal. Only extended.
- SQLite store design (`persistence.py`): schema, `PRAGMA user_version` migration, corrupt-DB
  quarantine, secret rejection, upserts. Only the write path and connection handling changed.
- `ServiceManager` semantics: start once, rollback on failure, reverse stop, name de-duplication.
  Unchanged.
- The API's static asset allow-list and generic 500 handling. Kept.
- Dashboard structure, visual design, the 12 navigation pages and the honest placeholder copy.
  Kept; only state handling and wording changed.
- MT5 adapter and collectors: read-only, untouched. Reused as workers through the existing
  `run_configured_source(..., stop_event=...)`.

## 3. What I changed, and why

### Data continuity (`src/johanna/data_services.py`, new)
- `MarketDataService` (name `market_data`) replaces the placeholder. `read_bars()` merges the
  `ParquetBarStore` roots `[data_dir, <collector output>/mt5]`; on a key collision the later
  root wins. `status()` reports `freshness`, which is `fresh` only if the last successful update
  is ≤15 min old. The last successful update is the max of the manifest
  `ingestion_time_utc` (complete manifests only) and the collector-state
  `targets.*.last_successful_retrieval_utc`. Status also reports `latest_bar_utc`, `bar_count`,
  `monitoring_state` and collector `target_errors`.
  *Why:* the audit found the app read Parquet directly, with no freshness and no provider
  timestamps.
- `CalendarService` (name `economic_calendar`) reads the current UTC week's
  `WeeklySnapshotCache` snapshot. Freshness uses the adapter's own rule
  (`0 <= age <= max_age`; a snapshot dated in the future is stale). States: `no_snapshot` and
  `error` (corrupt file). Snapshots are served even when refresh is disabled.
- Optional workers: `monitor_task` / `refresh_task` wrapped in `BackgroundService`.

### Monitoring as a real control (`application.py`)
- `JohannaApplication._sync_monitoring()`: the preference `market_monitoring_enabled` starts or
  stops the monitor worker and overwrites `service_config.market_data.monitoring_enabled`. It
  runs on start and on every preference save that touches the key.
- The worker is the existing MT5 collector, created only when
  `integrations.collectors_config` is set **and** `market_data_provider == "mt5"`. Otherwise
  monitoring reports `not_configured`. There is no Twelve Data collector, so none is invented.
- The calendar worker exists only if `calendar_enabled`. It also refuses to run (raising
  `PermissionError`, which shows as a visible error) unless `forex_factory_authorized` is true.
  The collector separately enforces its own `authorized_for_local_cache`.

### Lifecycle and reliability
- `instance_lock.py` (new): an OS-level exclusive lock on `<data_dir>/johanna.lock`
  (msvcrt / fcntl). The OS releases it if the process dies.
- `application.py`: order is lock → persistence → market data → calendar → placeholders.
  `health()` / `_health()` added. `status()` adds `health`, `market_monitoring_state`,
  `market_data_freshness`, `calendar_freshness`, and derived `last_provider_updates`.
- `services.py`: `BackgroundService` logs failures with a traceback, has `exited` and `error`
  states, records `started_at_utc` / `failed_at_utc`, and `stop()` takes the lock.
- `__main__.py`: binds the port **before** `application.start()` (a busy port never acquires
  the lock or starts workers). Uses `ExclusiveWSGIServer` and `LoggingRequestHandler`. Exits 1
  with a clear log line on bind or lock failure.

### Persistence (`persistence.py`)
- `save_preferences`: full validation first, then a single `_transaction()` (watchlist +
  preferences).
- `SYMBOL_PATTERN` = `[A-Za-z0-9][A-Za-z0-9._/#-]{1,31}`, applied to the watchlist and the
  selected pair. It accepts broker suffixes (`EURUSD.a`, `XAU/USD`, `US30-cash`). The watchlist
  is capped at 200.
- `_transaction()` commits or rolls back and always closes. `stop()` closes its checkpoint
  connection. `ZoneInfo` `ValueError` (e.g. an empty string) maps to a validation error.

### API (`api.py`)
- Route table → 404 for unknown paths, 405 with an `Allow` header for known paths.
- `/health` uses `application.health()` (200 or 503).
- `_host_allowed`: `Host` must be loopback or the configured host (any host is allowed when
  bound to `0.0.0.0`/`::`). A present `Origin` must equal `Host`; `Origin: null` is rejected.
  Violations get 421.
- `POST /api/preferences` requires `Content-Type: application/json` (otherwise 415).
- Static responses send `X-Frame-Options: DENY`, a CSP (`'self'` only, `frame-ancestors
  'none'`) and `Referrer-Policy`. JSON responses send `nosniff`.

### Config, logging, docs
- `config.py`: `integrations.collectors_config` / `JOHANNA_COLLECTORS_CONFIG` (must exist).
- `logging_config.py`: `extra=` fields are included in the JSON output.
- `README.md`, `johanna.example.toml`: document monitoring, health, the lock and the request
  rules.

### Dashboard (`web/app.js`, `web/styles.css`)
- Top bar: Application `Online` / `Degraded` / `Offline` (from `health`), Monitoring (actual
  worker state), Market data (freshness), and Last update parsed as dates.
- Overview badge, Settings monitoring row and the health list use the backend state labels.
  Only `running` is shown green.
- Markets and Calendar pages show status strips (freshness, last success, latest bar, snapshot
  time, source, collector target errors).
- Settings: theme select (dark/light/system), timezone select (`Intl.supportedValuesOf`),
  "SAVED BY BACKEND".
- Hash routing; navigation works during a refresh; background refresh doesn't re-render while
  the user is editing.
- CSS: `minmax(0,1fr)` tracks at ≤650px, `.text-link` style, error/degraded colours, wrapping
  panel heads.

## 4. Why each change was necessary

See the "Why" notes in §3 and the finding numbers in §1. In short, every change maps to an audit
finding or to a defect found while fixing one (the last four rows of §1). Nothing in §3 adds
Phase 2+ functionality: no Gemini, news, signals, rules, risk calculation or order execution.

## 5. Files

| File | Change |
|---|---|
| `src/johanna/data_services.py` | **new** |
| `src/johanna/instance_lock.py` | **new** |
| `src/johanna/application.py` | rewritten (public methods kept: `start`, `stop`, `save_preferences`, `cache_provider_data`, `mark_provider_update`, `save_service_configuration`, `status`, `dashboard_data`; added `health`) |
| `src/johanna/api.py` | routing/security rewrite of `__call__`; `_save_preferences`, `_static`, `_response` extended |
| `src/johanna/__main__.py` | rewritten |
| `src/johanna/services.py` | `BackgroundService` rewritten; logger added |
| `src/johanna/persistence.py` | `save_preferences`, `set_watchlist`, `_transaction`, `stop`, helpers |
| `src/johanna/config.py` | `collectors_config` |
| `src/johanna/logging_config.py` | extras in JSON |
| `src/johanna/web/app.js`, `src/johanna/web/styles.css` | targeted edits (§3) |
| `README.md`, `johanna.example.toml` | docs |
| `tests/test_application.py` | one assertion changed (below) |
| `tests/test_phase1_remediation.py` | **new**, 34 tests |
| `docs/phase-1-remediation-handoff.md` | this file |

Untouched: `adapters/*`, `collectors.py`, `ingestion.py`, `market_data.py`, `quality.py`,
`ticks.py`, `mt5_smoke.py`, `index.html`, `collectors*.toml`, everything under `data/`.

## 6. Tests

**Changed:** `test_application_initializes_domain_services_and_disables_execution` previously
asserted `market_data.state == "not_implemented"`. It now asserts `"idle"` with
`monitoring_state == "not_configured"`. The placeholder was replaced by a real service; `idle`
means "serving stored data, no worker".

**Added (`tests/test_phase1_remediation.py`, all offline and synthetic):**
- Continuity: collector bars reach status/API/dashboard; the watchlist never mixes timeframes;
  the collector root overrides the import root; market freshness `no_data → fresh → stale →
  fresh` via collector state; unreadable Parquet gives an `error` state and `/health` degraded.
- Calendar: freshness matches the adapter rule (including a future snapshot); a corrupt
  snapshot gives `error`; collectors-config paths are used; a missing `collectors_config` is
  rejected.
- Monitoring/workers: the preference starts and stops the worker, survives restart and doesn't
  restart a paused worker; stale `service_config` is reconciled; a worker failure is logged,
  visible and returns 503; a worker that returns unexpectedly shows `exited`; collector target
  errors give `degraded`; calendar refresh requires authorization; no monitor without config or
  for Twelve Data.
- Lifecycle: persistence first; a second app on the same data dir is refused, then allowed after
  stop; health is unavailable until started; the exclusive server refuses a shared port.
- Persistence: a rejected request changes nothing (including after restart); invalid symbols are
  rejected and broker suffixes accepted.
- API: 405 + `Allow`; 415 for text/plain; Host/Origin matrix (6 cases); frame and CSP headers;
  execution stays unavailable and `JOHANNA_EXECUTION_ENABLED=true` is rejected.
- Logging: extras kept.

## 7. Verification commands and results (2026-11-11, Windows 11)

| Command | Exit | Result |
|---|---|---|
| `.venv311\Scripts\python.exe -m pytest -q -p no:cacheprovider` (3.11.8) | 0 | 149 passed |
| `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` (3.13.3) | 0 | 149 passed |
| `python -m pytest tests/test_phase1_remediation.py` | 0 | 34 passed |
| `python -m ruff check --no-cache .` | 0 | All checks passed |
| `python -m ruff format --check --no-cache .` | 0 | 38 files already formatted |
| `node --check src/johanna/web/app.js` | 0 | syntax OK |
| `grep -rnE "order_send\|order_check\|positions_get\|TRADE_ACTION\|ORDER_TYPE\|place_order\|close_position\|modify_order" src/` | 1 | no matches |

Live runs (scripts in the session scratchpad, temp data dirs, real `python -m johanna`):
- **Original audit script re-run:** `POST /status` → 405; garbage symbol → 400; partial write
  → 400 with the watchlist unchanged; text/plain POST → 415; second instance on the same port
  → exit 1 (`WinError 10048`); force-kill and restart → all 9 preferences restored;
  `service_config.monitoring_enabled` matches the preference; dummy secret absent from logs and
  DB; every log line is JSON.
- **Copy of the real `data/collectors` + `data/collector_state`:** 793 bars (EURUSD M5 + H1),
  latest bar `2026-10-30T22:55:00Z`, last success `2026-10-31T05:15:31Z` → `freshness: stale`
  (correct). A second process with the same data dir on another port exits 1 (lock).
- **In-process app with a failing fake monitor:** `/health` → 503
  `{"status":"degraded","failed_services":["market_data"]}`; traceback logged; UI shows
  *Application Degraded*, *Monitoring Failed*, `market data: Failed`.
- **Headless Edge screenshots:** Overview/Markets/Calendar/Settings at 1600px; true 400px
  viewport via iframes behind a scratch proxy that strips `X-Frame-Options`. No page-level
  horizontal overflow.

## 8. Decisions and trade-offs

- **D1 Monitoring worker = existing MT5 collector.** No new provider code. It is opt-in through
  `collectors_config` because enabling it makes real (read-only) MT5 calls. The preference
  defaults to `true`, so setting `collectors_config` starts MT5 polling on the next launch.
- **D2 Collector root wins on duplicate bars.** It is the newer source. Revisit if imported
  history should be authoritative.
- **D3 No provider timestamps duplicated into SQLite.** The manifest and collector state files
  are already durable and authoritative; copying them would create a second, possibly stale,
  truth. `runtime_state` / `provider_cache` remain for providers without their own state.
- **D4 Market staleness = 15 min since the last success.** This is wall-clock, not market-hours
  aware (it reports stale over weekends, which is accurate for "is data current?"). The value is
  hard-coded in the `MarketDataService(stale_after=...)` default and not yet configurable.
- **D5 No typed Protocols for news/Gemini/signals/rules/risk/execution.** The brief said to avoid
  unnecessary abstractions and forbids implementing them. My original audit's C2 asked for
  contracts. That is the main open architectural question (§10).
- **D6 Host check is skipped when the bind host is `0.0.0.0`/`::`.** This is an explicit
  operator choice; the Origin check still applies.
- **D7 Degraded collector targets (`degraded`) keep `/health` at 200.** Only `error` gives 503,
  so one bad symbol doesn't mark the whole app down.

## 9. Potential regressions to investigate

1. **Running the collector CLI and the backend monitor for the same source at once.** Both write
   the same Parquet store and state file. Nothing prevents it yet (documented in the README).
2. **`set_monitoring(False)` blocks the request** until the collector finishes its current poll
   (up to 30 s join). The MT5 collector waits on `stop_event`, so this is normally fast, but a
   hung MT5 call would stall the HTTP thread (wsgiref is single-threaded).
3. **`InstanceLock` on network drives** is untested.
4. **CSP:** any future inline `<script>` or `style=""` attribute will be blocked. The current UI
   uses neither (checked).
5. **Host allow-list:** reaching the UI as `http://<machine-name>:8765` while bound to
   127.0.0.1 now gets 421 (intended).
6. **`ManagedService` placeholders are gone for `market_data` and `economic_calendar`.** Anything
   expecting `state == "not_implemented"` for them will break (only the one test did).
7. **Status calls read the manifest and collector state files on each `/status`, `/health` and
   `/api/dashboard`.** Cheap now; watch it if the state files grow (the MT5 state file holds
   pending bars).

## 10. Outstanding (not resolved by me)

1. **C2 contracts:** decide whether Phase 1 needs typed Protocols for news, Gemini, signals,
   rules, risk and execution (D5). This is a scoping decision for the user and you.
2. **Live MT5 monitoring through the backend was not run.** It needs a signed-in MT5 terminal;
   only fake workers were used. Run it once with `collectors_config = "collectors.toml"`.
3. **Calendar refresh through the backend was not run live** (needs the Forex Factory
   permission and a network request).
4. **Graceful shutdown via signal on Windows:** only Ctrl+C in a console triggers it (SIGTERM is
   not delivered on Windows). Verified in-process and via force-kill recovery only.
5. **No frontend unit tests.** The repo has no JS tooling; UI verification was manual
   (screenshots), not automated clicking. A Playwright-style smoke test would close this.
6. **No cross-process guard** between the collector CLI and the backend monitor (§9.1).
7. **Stale-after threshold is not configurable** (D4).
