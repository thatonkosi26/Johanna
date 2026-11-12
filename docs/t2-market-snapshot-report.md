# T2: coordinated MarketSnapshot

Date: 2026-11-12 · Claude Code (Opus 5.5) · Working tree only, **nothing committed** · Stopped
after T2 (T3–T10 not started)

## 1. Files changed

| File | Change |
|---|---|
| `src/johanna/analysis/__init__.py` | **New** package: pure analysis building blocks; no MT5, adapter, collector, live-market or network imports (test-enforced) |
| `src/johanna/analysis/snapshot.py` | **New:** `SnapshotLimits`, `DEFAULT_LIMITS`, `clock_from_tick()`, `build_snapshot()`, `evaluate_snapshot()`, the state constants `live` / `provisional` / `refused`, and `SNAPSHOT_VERSION` |
| `src/johanna/live_market.py` | `MT5Session.bars()` records the **snapshot tick** (bid, ask, tick time) and separate MT5 retrieval times for the tick and the bars. `LiveMarketService.bars()` marks every response with `served_from_cache`. **New** `LiveMarketService.snapshot()`; **new** `snapshot_limits` constructor argument; `_bars_payload()` gains provenance fields. |
| `src/johanna/api.py` | **New** read-only endpoint `GET /api/market/snapshot?symbol=&timeframe=&count=`, validated like `/api/market/bars` |
| `tests/test_snapshot.py` | **New:** 30 tests (§4) |

No other file changed. The dashboard, collectors, `data/`, the T0 script and the T1 checks are
untouched.

## 2. Implementation

**How a snapshot is assembled:** from one `LiveMarketService.bars()` response, produced by one
bars-worker job on one MT5 connection, which reads:
1. the snapshot tick (`get_tick`), then records its retrieval time;
2. the bars (`read_bars`, with T1's checks), then records their retrieval time.

The reads are sequential and each keeps its own time; nothing claims they're simultaneous.
The quotes worker isn't consulted, so its failures don't affect a snapshot (tested).

**Snapshot contents:**

| Field | Content |
|---|---|
| `snapshot_id` | Random 128-bit hex. Passed into `build_snapshot`, so the function itself stays deterministic. |
| `symbol`, `broker_symbol`, `timeframe` | As requested and as resolved for the broker |
| `assembled_at_utc` | Assembly time |
| `tick` | `bid`, `ask`, `time_utc` (tick time, UTC), `retrieved_at_utc` (MT5 retrieval). A placeholder tick gives `null` values. |
| `bars` | `retrieved_at_utc` (MT5 retrieval), `served_at_utc` (when the response was served), `served_from_cache`, `count`, `items` (T1 candles incl. `closure`, `valid`, `issues`, `spread_points`, unchanged), `rejected`, `quality`, `latest_tick_utc` |
| `clock` | Derived **only from the snapshot tick**: `result` (`verified` / `stale_ticks` / `mismatch` / `unavailable`), `tick_age_seconds`, `checked_at_utc`, `derived_from: "snapshot_tick"` |
| `symbol_info` | `digits`, `point`, `pip_size` |
| `upstream` | The bars response's `status`, `reason` and `message` |
| `ages` | `tick_vs_bars_seconds`, `clock_vs_bars_seconds`, `snapshot_age_seconds`, `served_age_seconds`, `tick_age_at_retrieval_seconds` |
| `limits` | The limits applied |
| `eligibility` | `state`, `reasons` (each `{code, severity, detail}`), `evaluated_at_utc`, `ages` |

**Cache provenance.** Three times are kept separate:
- **MT5 retrieval** (`tick.retrieved_at_utc`, `bars.retrieved_at_utc`): never changes when a
  response is served again from the 5 s cache.
- **Serving** (`bars.served_at_utc`, `served_from_cache`): when, and whether from the cache.
- **Evaluation** (`eligibility.evaluated_at_utc`): the assembly evaluates at its own time; a
  later analysis re-evaluates with `evaluate_snapshot(snapshot, analysed_at=...)` at its
  calculation time.

**Snapshot age** is measured from the **MT5 retrieval** of the bars, so a cached response can
never look newer than its observation (tested).

**Eligibility** (`evaluate_snapshot`: pure, deterministic; every failed condition listed;
`refused` outranks `provisional`):

| Condition | Code | Severity |
|---|---|---|
| Bars unavailable (terminal closed, timeout, …) | `bars_unavailable` (upstream reason kept) | refused |
| Server-clock mismatch | `clock_mismatch` | refused |
| Old bars served because MT5 is unavailable | `mt5_unavailable_cached` | refused |
| Source isn't live MT5 (archive) | `not_live_source` | refused |
| Time basis isn't UTC (legacy archive) | `legacy_time_basis` | refused |
| No bars returned | `no_bars` | refused |
| No usable snapshot tick | `tick_missing` | refused |
| A component has no retrieval time | `timestamps_missing` | refused |
| Tick retrieved after bars, or bars retrieved after evaluation | `inconsistent_timestamps` | refused |
| Tick vs bars retrieval > **2 s** | `tick_bars_skew` | refused |
| Clock check vs bars retrieval > **30 s** | `clock_bars_skew` | refused |
| Bars' MT5 retrieval older than **5 s** (one candle refresh interval) when evaluated | `snapshot_too_old` | refused |
| Snapshot tick older than 120 s (market closed or quiet) | `clock_stale_ticks` | provisional |
| Bars failing T1 checks present | `invalid_bars_present` | provisional |
| Duplicate times with differing values | `conflicting_duplicates` | provisional |

- **Window-level decisions:** these are deferred, as agreed. For example, rejecting a window with
  conflicting duplicates and separating price validity from ordering belong to T4. The snapshot
  only flags them.
- **Continuity:** no continuity verdict is made, because closure evidence isn't coverage. That
  belongs to T3.
- **Limits:** the defaults are the provisionally approved values, configurable through
  `SnapshotLimits` and `LiveMarketService(snapshot_limits=...)`.

## 3. API contract

**New:** `GET /api/market/snapshot?symbol=EURUSD&timeframe=H1&count=300` returns the snapshot
above. Validation is identical to `/api/market/bars` (400 with a message otherwise), and the
response is strict JSON.

**Changed: `GET /api/market/bars`**, additive only:
- **New fields:** `tick` (the snapshot tick), `retrieved_at_utc` (MT5 retrieval time of the
  bars) and `served_from_cache`.
- **Existing fields** keep their meaning: `fetched_at_utc` is still the time the bars were
  fetched, `requested_at_utc` the serving time.
- **Archive and unavailable responses** carry `tick: null` and `retrieved_at_utc: null`.

**Backward compatibility:** every existing field is unchanged. The dashboard ignores the new
fields, and all existing tests pass unchanged (§5).

## 4. New tests (`tests/test_snapshot.py`, 30)

- **Construction:** every component present from the live service; T1 candle fields carried
  through; strict JSON.
- **Retrieval timestamps:** tick < bars ≤ served < assembled, and ages match the timestamps.
- **Clock from the snapshot tick:** 5 boundary cases (verified at 0.5 s, 119 s and −30 s;
  stale_ticks at 121 s; mismatch at −61 s).
- **Independence:** a failing quotes worker doesn't affect a live snapshot.
- **Clock outcomes:** a stale snapshot tick makes the snapshot provisional; a placeholder tick
  is refused.
- **Cached vs new:** a repeat within the cache keeps the original retrieval time and tick,
  `served_from_cache` is true, and its age grows. After MT5 loss the old observation is CACHED
  and refused (`mt5_unavailable_cached`, `snapshot_too_old`).
- **Missing and unavailable:** terminal not running; clock mismatch; empty bars; archived bars
  (not live, legacy time basis); a missing tick or retrieval time.
- **Age limits:** a 5.0 s snapshot age is live and 5.1 s is refused; a 2.0 s tick-vs-bars gap is
  live and 2.3 s is refused; clock-vs-bars at 31 s is refused and 29 s isn't.
- **Inconsistent timestamps:** tick after bars, and bars after evaluation.
- **Provisional:** invalid bars; conflicting duplicates (identical duplicates aren't flagged);
  refused outranks provisional with every reason listed.
- **Determinism and configuration:** 100 identical builds and 100 identical evaluations;
  configurable limits.
- **API and contracts:** the snapshot endpoint (200 / 400); the bars response gains provenance
  fields additively.
- **Isolation and safety:** the `analysis` package import ban; only read-only MT5 calls are made.

**Results:** `tests/test_snapshot.py`: 30 passed (3.13, verbose run), and also within both full runs.

## 5. Full test results

| Command | Result |
|---|---|
| Baseline (end of T1) | 335 passed on 3.11 and 3.13 |
| `.venv311\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.11.8) | **365 passed**, 0 skipped, 0 failed |
| `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.13.3) | **365 passed**, 0 skipped, 0 failed |
| `tests/test_browser_smoke.py` (3.11.8) | **50 passed**: automatic refresh, zoom, pan, price-axis scaling, Go Live, invalid candles |
| `python -m ruff check --no-cache .` | All checks passed |
| `python -m ruff format --check --no-cache .` | 76 files already formatted |
| `node --check src/johanna/web/app.js` (v23.9.0) | OK |

The increase is exactly the 30 new tests, and no existing test was modified.

## 6. Data preservation and backend

- **`data/` unchanged:** SHA-256, size and modification time of all 11 readable files under
  `data/`, including `johanna.sqlite3`, were identical before and after T2, and the file count
  is still 12.
- **Lock file:** `data/johanna.lock` (held by your backend) has the same size and modification
  time.
- **Your backend wasn't stopped, restarted or contacted:** its three processes (PID 37288,
  started 12:00:54, and its two workers from 12:01:01) are identical before and after.
- **Isolated tests:** all tests used temporary directories and their own ports.

## 7. MT5 read-only verification

- **No trading calls:** `grep -rnE "order_send|order_check|order_calc|positions_get|
  positions_total|orders_get|history_orders|history_deals|TRADE_ACTION|\.login\(" src/
  scripts/` found **no matches** (exit status 1).
- **No new MT5 calls:** T2 adds none; the snapshot reuses `symbol_info_tick` and
  `copy_rates_from_pos`, already used before.
- **Read-only test:** `test_only_read_only_mt5_calls_are_used` checks the snapshot path.
- **No terminal or account use:** nothing in this task connected to the real terminal or account.

## 8. Unresolved issues and carried-forward items

- **Not verified live (outstanding integration item).** T1 and T2 haven't been run against the
  real Exness terminal with the modified code. Your running backend still serves the pre-T1
  code; I didn't restart it, and that needs your permission.
- **Carried forward from T1** (unchanged by T2, as instructed):
  - conflicting duplicates must make T4 reject the window;
  - the first-received duplicate policy is provisional;
  - OHLC validity, timestamp uniqueness and ordering, and feed continuity must stay separate
    in the eligibility model.

  The snapshot reports conflicting duplicates and invalid bars as provisional reasons; it
  doesn't decide them.
- **The two clock limits coincide.** The clock check and the tick are the same read, so
  `clock_vs_bars` always equals `tick_vs_bars` today, and the 2 s limit binds first. The 30 s
  check still applies if the clock source ever differs.
- **Age limit vs cache.** "One candle refresh interval" is fixed at 5 s, the dashboard's chart
  interval. A response served from the 5 s bars cache can be up to just under 5 s old, so
  snapshots built from cached bars can approach the limit. Analysis that re-evaluates later will
  correctly refuse an expired one.
- **Placeholder ticks.** These (symbol just added to Market Watch) give a refused snapshot rather
  than retrying; the next request normally succeeds.
- **No snapshot registry.** Snapshot IDs are random UUID4 values. Snapshots aren't stored or
  registered anywhere, which wasn't in scope.
