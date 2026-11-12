# Johanna — Phase 1 acceptance report

Date: 2026-11-11 · Prepared by Claude Code (Opus 5.5) · Working tree only, **nothing committed**
Builds on `docs/phase-1-remediation-handoff.md` (first remediation round). Codex is no longer
part of this project; that handoff is kept for history.

## 1. Decision

**Recommendation: PARTIAL. Do not sign Phase 1 off yet. There is one blocking item.**

Every requirement in Phase 1 Prompts 1–4 is now implemented and backed by automated tests,
live runs or both (§3). The blocker was found during the live MT5 check:

> **MT5 timestamps stored as "UTC" are broker server time, +2 hours.** All 793 stored bars,
> and any newly collected bars, are shifted. Charts and "latest bar" show times 2 hours (or 3
> during the broker's DST) in the future. Freshness and health are unaffected. Details and
> options in §5.

The fix needs two decisions from you: the broker's server timezone, and consent to rewrite
stored bar timestamps. Once those are made and verified, I see nothing else in Phase 1 that
should block acceptance.

## 2. What changed in this round

| Area | Change | Files |
|---|---|---|
| Collector coordination | `SourceLock`: one OS-level lock per source (`<state_dir>/mt5.lock`, `calendar.lock`) taken inside `run_configured_source`, the single entry point used by both the CLI and the backend worker. The CLI exits with code 3 when busy; the backend shows `CollectorBusyError`. | `collectors.py`, `instance_lock.py` (now a generic `FileLock`), `web/app.js` |
| Windows write race (**bug found**) | Atomic `replace` failed with `Access is denied` whenever any reader had the file open, and that crashed the monitor worker. Dashboard status reads could trigger it. Now retried for up to ~1 s at all 7 atomic-write sites. | `fsutil.py` (new), `collectors.py`, `market_data.py`, `ticks.py`, `adapters/mt5.py`, `adapters/economic_calendar.py` |
| Calendar 403/429 (**pre-existing bug**) | The collector meant not to retry a denial or rate limit, but its loop never exited, so it re-requested immediately (3 requests by default). It now stops after one. | `collectors.py` |
| Calendar health | The backend shows `degraded` when the calendar collector's last poll failed (it previously showed `running`). It also exposes `collector_status` and `collector_last_attempt_utc`. | `data_services.py` |
| Domain contracts | Typed Protocols, data types and refusing stubs for news, intelligence, signals, rules, risk and execution (§6). | `domain/*.py` (new), `application.py`, `services.py` (`Service` is now `@runtime_checkable`) |
| Browser tests | Dependency-free CDP driver plus 14 smoke tests (§7). | `tests/browser_harness.py`, `tests/test_browser_smoke.py`, `pyproject.toml` (marker) |
| Line endings | My earlier edit scripts had converted 16 files to CRLF; restored to the repo's LF convention. Content unchanged. | 16 files |
| Docs | Collector lock, browser tests, MT5 timestamp warning. | `README.md`, this report |

New tests this round: `test_collector_locking.py` (8), `test_fsutil.py` (2),
`test_calendar_refresh.py` (7), `test_domain_contracts.py` (9), `test_browser_smoke.py` (14).

A pre-round snapshot of every source/test/doc file was saved to
`%TEMP%\claude\...\scratchpad\baseline-pre-acceptance\` before any change, so this round is
diffable even though nothing is committed.

## 3. Requirements status (Prompts 1–4)

| Requirement | Status | Evidence |
|---|---|---|
| P1 Separated layers (frontend, core, market data, calendar, news, intelligence, signals, rules, risk, execution, persistence) | PASS | Real services: `MarketDataService`, `CalendarService`, `ApplicationStore`. Contracts: `johanna.domain.*`. The frontend uses only the HTTP API. `test_domain_contracts.py` checks that the domain package imports no broker or provider code. |
| P1 Integrations have sensible boundaries (Twelve Data, Forex Factory, Gemini, MT5) | PASS | Adapters under `adapters/`; MT5 and Forex Factory run only through existing collectors; Gemini is a contract only. |
| P2 Entry point, startup/shutdown order, config, logging, error handling | PASS | `__main__.py`, `application.py`; tests in `test_application.py` and `test_phase1_remediation.py` |
| P2 Health/status reflect reality | PASS | 503 when stopped or a service fails; live-verified |
| P2 No duplicate services or processes | PASS | Instance lock + exclusive port + **source lock (new)**; live CLI refusal (exit 3) |
| P2 Worker failures visible | PASS | Logged traceback, API state, UI "Failed"; browser test |
| P2 Security (secrets, Host/Origin, CSRF, framing) | PASS | Tests; earlier live run |
| P3 Persistence + restart recovery | PASS | Kill/restart live run; atomic writes; monitoring preference survives restart |
| P3 Cached market and calendar data persisted and readable | PASS, **with the §5 timestamp defect** | 793 real bars read; snapshots persisted |
| P4 Navigation, top bar, overview, honest states, responsive | PASS | 14 browser tests incl. 400px no-overflow on 5 pages |
| Execution safety | PASS | No order/position/account API anywhere (grep below); `DisabledExecutionService` refuses; config rejects `execution_enabled=true` |

## 4. Collector conflict: design and verification

- **One mechanism, one place.** `run_configured_source()` wraps every run (`--once` and
  continuous, real and dry-run) in `with SourceLock(config.state_dir, source)`. The CLI's
  `main()` and the backend's monitor/refresh tasks both call it. No other code path starts a
  collector.
- **Scope.** The lock lives in the same state directory as the source's progress file, so
  everything that would write the same state and store contends for it. `mt5` and `calendar`
  never block each other.
- **Release and stale locks.** `msvcrt.locking` (Windows) or `flock` (Linux/macOS) on an open
  handle. The OS drops the lock when the process exits for any reason, so a leftover file is
  never a lock. Released on normal exit, on exceptions and on monitoring toggle-off. Locks are
  per handle, so threads in one process also exclude each other.
- **Tests.** Backend running → CLI exit 3, and vice versa. Independent sources run
  concurrently. Release after an exception. A leftover file doesn't block. A **lock held by a
  killed subprocess is recovered**. Thread exclusion.
- **Live (real MT5).** Backend monitoring was running → `python -m johanna.collectors --source
  mt5 --once` exited 3 with a clear message. The backend kept collecting. Toggling monitoring
  off released the lock.
- **Not verified:** Linux execution (the `flock` path). No Linux machine was available; the CI
  workflow (`ubuntu-latest`) will exercise it on the next push.

## 5. Live MT5 verification (read-only), and the blocking timestamp defect

**Method.** Your terminal was already running (started 22:59 SAST); it was not started or
logged in by me. The adapter's complete API surface was reviewed first. It calls only
`initialize`, `terminal_info`, `account_info`, `symbols_get`, `symbol_info`,
`symbol_info_tick`, `copy_rates_from_pos`, `version`, `shutdown`, and `symbol_select` (Market
Watch visibility only). No order, position or account-setting call exists. All outputs went to
a temporary directory; `data/` was not written (file times unchanged: `johanna.sqlite3`
22:15:25, `collector_state/mt5.json` 2026-10-31). Script: `scratchpad\live_mt5_verify.py`.

**Results (2026-11-11 ~21:13 UTC):**
- Terminal connected, account connected (booleans only; no identifiers read or logged).
- 12,164 visible symbols; EURUSD available; live tick with bid/ask; 5 M5 bars.
- Real backend (`python -m johanna`, `JOHANNA_COLLECTORS_CONFIG`): monitoring `running`,
  freshness `fresh`, 20 bars stored, collector targets EURUSD H1/M5/ticks all `ok`,
  `/health` 200, no errors in the log.
- CLI conflict refused (exit 3); monitoring off → `paused`, worker stopped.

**Defect.** The same probe compared MT5's "UTC" with the system clock:

```
system UTC now      : 2026-11-11T21:13:52+00:00
MT5 tick 'time_utc' : 2026-11-11T23:13:53+00:00   -> offset +2.0 h
```

Your stored history agrees: its last H1 bar is stamped **Friday 2026-10-30 22:00 UTC**, which
is after the FX market close that day in real UTC but consistent with server time. The adapter
(Week 1–2 code) treats MT5 epoch values as UTC; for this broker they are server time. The
machine's own zone is also UTC+2, so this is either the broker's server zone (EET-style, often
UTC+3 in summer) or local-time conversion. Either way, the stored values are wrong by the
offset.

**Options (your decision).**
1. *Configured server timezone (recommended).* Add `mt5.server_timezone` (IANA, e.g. the
   broker's documented zone) and convert in the adapter. Migrate stored bars once with a
   backup, using an explicit, reversible command. Handles DST correctly.
2. *Measured offset.* Derive the offset live (tick time − UTC, rounded to 30 min). Simple, but
   wrong for history across a DST change.
3. *Accept and label.* Keep server time and label it as such everywhere. Not suitable for
   analysis against UTC calendar events.

Not done here because it requires the broker zone and a migration of your existing data, which
this task forbids changing.

## 6. Domain interfaces: scope decision

**Decision: required, implemented as contracts only.** The Phase 1 brief (Prompt 2B) asks to
"inspect the implementation of" the Gemini intelligence, trading signals, trading rules, risk
management and broker/execution *interfaces*, and to verify they are "meaningful service
boundaries rather than placeholder classes", while allowing "interfaces or stubs … clearly
identified as not yet implemented". Anonymous `ManagedService("trading_signals")` placeholders
did not meet that. Leaving it out would have kept Prompt 2B at PARTIAL.

**What exists** (`src/johanna/domain/`): per boundary, frozen dataclasses (`Headline`,
`AnalysisRequest/Result`, `Signal`, `RuleEvaluation`, `RiskAssessment`, `OrderIntent`,
`ExecutionResult`), a `@runtime_checkable` Protocol, and a stub. Stubs report
`implemented: false`, `contract: "johanna.domain.<module>.<Protocol>"` and the same
`disabled`/`not_implemented` state as before (the UI is unchanged). Every domain method raises
`ServiceNotImplementedError`; execution raises `ExecutionUnavailableError` ("no order was
sent").

**What does not exist:** news ingestion, model calls, signal logic, rule evaluation, risk
maths, broker execution. The domain package imports no adapter, collector or network module
(a test enforces this). MT5 remains read-only.

## 7. Calendar refresh verification

**Live retrieval: not performed, by design.** The existing configuration says *not
authorized*: `forex_factory_authorized = false` in the app config and
`authorized_for_local_cache = false` in `collectors.toml`. Making a request would bypass that.
The README records that you have written permission; enabling it is your call.

**Offline end-to-end (fake HTTP transport behind the real provider, collector, backend worker,
snapshot and API; `test_calendar_refresh.py`):**

| Behaviour | Result |
|---|---|
| Successful retrieval | 1 request, `Accept: application/json`, snapshot written with ETag |
| Normalisation and UTC | `08:30-05:00` → `13:30:00Z`; `13:15+01:00` → `12:15:00Z`; missing `actual` stays null |
| Duplicates | 3 rows → 2 events |
| Freshness | `fresh` after refresh; `stale` when serving an old cache; `no_snapshot` when none |
| Rate limit 429 | **exactly 1 request** (mutation test: the pre-fix code made 3); `degraded`; error visible in API |
| Denial 403 with cache | 1 request; cached events served as `stale` |
| Network error | bounded retries (1 + 2), then `degraded` |
| No unnecessary requests | fresh cache → 0 requests |
| 304 Not Modified | `If-None-Match` sent; snapshot refreshed |
| Collector permission flag off | worker `error` (`CollectorConfigError`), 0 requests |

**Live steps for you (one request, then cached for 6 h):**
1. Confirm your written permission covers automated retrieval and local caching.
2. In `collectors.toml` set `[calendar] enabled = true` and `authorized_for_local_cache = true`.
3. In your backend TOML set `[integrations] calendar_enabled = true`,
   `forex_factory_authorized = true` and `collectors_config = "collectors.toml"`.
   **Note:** with `collectors_config` set, *Market monitoring* (default on) also starts the
   MT5 collector. Turn monitoring off in Settings if you only want the calendar.
4. Run `python -m johanna --config <your toml>` and open `/#calendar`. Expect *Freshness:
   Fresh*, a snapshot time, and events. The snapshot is saved under
   `data/collectors/calendar/snapshots/week-<monday>.json`.
5. If you see *Degraded*, the Calendar page and `GET /status` → `services.economic_calendar.
   collector_last_error` show why (403/429/network). It does not retry a 403 or 429.

## 8. Browser verification

**Approach.** `tests/browser_harness.py` talks to headless Edge or Chrome over the DevTools
protocol using an ~80-line standard-library WebSocket client. No new dependency. The backend
runs in-process over synthetic data with fake workers. Tests skip when no browser is found
(verified: 14 skipped with `JOHANNA_BROWSER` pointing nowhere).

**Coverage (14 tests):** dashboard loads real API data (40 bars, watchlist price equals the
backend value, canvas actually drawn, calendar event shown); all 12 pages via clicks, with hash
and `aria-current`; settings (theme, timezone, timeframe) saved to the backend and restored
after reload; the monitoring toggle starts and stops the worker; failed worker → *Monitoring
Failed* / *Application Degraded*; Markets and Calendar status strips; empty states; stale
states; **true 400px viewport with no horizontal overflow** on 5 pages; mobile menu; desktop
sidebar.

**Sensitivity checks.** Injecting `.chart-tools{min-width:520px}` makes the overflow test
report 147px on Overview and Charts.

**Correction to my original audit.** Screenshot-based "400px" checks were wrong: headless
Edge's minimum window is ~492px, so those images were crops. Re-measured at a true 400px
viewport, **Codex's original stylesheet did not overflow either**. Audit finding M10 was a
measurement artifact, not a defect.

**Remaining limits:** Chromium only (no Firefox/Safari); no pixel/visual-regression
comparison; fonts and rendering differ from a real desktop session.

## 9. Final verification (2026-11-11, Windows 11)

| Command | Exit | Result |
|---|---|---|
| `.venv311\Scripts\python.exe -m pytest -q -p no:cacheprovider` (3.11.8) | 0 | **189 passed** (incl. 14 browser) |
| `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider` (3.13.3) | 0 | **189 passed** |
| `… -m pytest -m "not browser"` (3.11) | 0 | 175 passed, 14 deselected |
| `JOHANNA_BROWSER=C:/nonexistent … pytest tests/test_browser_smoke.py` | 0 | 14 skipped |
| `python -m ruff check --no-cache .` | 0 | All checks passed |
| `python -m ruff format --check --no-cache .` | 0 | 53 files already formatted |
| `node --check src/johanna/web/app.js` | 0 | OK |
| `grep -rnE "order_send\|order_check\|order_calc\|positions_get\|orders_get\|TRADE_ACTION\|ORDER_TYPE\|place_order\|close_position\|modify_order" src/` | 1 | no matches |
| Live MT5 script (§5) | 0 | connectivity, data, monitoring, lock: verified |

Previous results remain reproducible: the 149 tests from the first round are all still
included and passing. Earlier test count 149 + 40 new = 189.

## 10. Outstanding

| Severity | Item | Next step |
|---|---|---|
| **High (blocking)** | MT5 server-time timestamps stored as UTC (§5) | You choose the broker timezone and approve a backed-up migration; then implement option 1 |
| Medium | Calendar live retrieval not performed (authorization flags are off) | Follow §7 steps if and when you choose |
| Medium | Linux `flock` path not executed locally | Watch the next CI run |
| Low | Backend monitor does not auto-retry when the CLI holds the lock | Toggle monitoring after stopping the CLI (documented); auto-retry is optional |
| Low | Graceful shutdown via signal on Windows only through Ctrl+C | Unchanged; documented in the handoff |
| Low | wsgiref is single-threaded; a hung MT5 call during monitoring toggle-off would stall HTTP (≤30 s join) | Acceptable for a local single-user backend |
