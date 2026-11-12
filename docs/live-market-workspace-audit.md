# Live market workspace: verification audit

Date: 2026-11-12 · Auditor: Claude Code (Opus 5.5) · Working tree only, nothing committed,
no source file changed by this audit.

> **Independence caveat.** The same assistant that implemented the workspace and wrote
> `docs/live-market-workspace-report.md` carried out this audit. Every finding below comes from
> re-reading the source, running the tests, and running new probes against the in-memory fake
> MT5. Treat claims that rest only on the earlier live runs as *reported, not independently
> verified* (§5). A second reviewer is advisable for the High and Medium items.

## 1. Verdict: **PASS WITH CONDITIONS**

The core is sound.
- **Read-only MT5:** no trading API calls exist anywhere.
- **Timestamp conversion:** correct, machine-timezone independent, and never applied twice.
- **Process isolation:** works, and leaves no orphaned workers.
- **No fabricated prices:** they never appear in the API.
- **Archived bars:** labelled CACHED and flagged as server time.
- **Collectors:** the workspace runs with all collectors disabled.
- **Tests:** all 237 pass on Python 3.11 and 3.13.

**No Critical findings.** There is **one High finding**: the dashboard can keep showing LIVE
after its quote request fails. There are **seven Medium** findings, four confirmed by probe
(listed in §4). Fix the High finding and M3 before Phase 2. The rest are small, contained
changes (§7).

## 2. Findings by severity

### Critical

None.

### High

**H1. The dashboard keeps showing LIVE badges after the quotes or bars request fails.**
- **Where:** `src/johanna/web/app.js`, `loadQuotes()` (585–599) and `loadBars()` (601–623).
- **What happens:** on any HTTP or network error, `state.quotes` and `state.bars` keep the last
  successful payload. Only `quotesError` and `barsError` are set. `quotesError` is never
  rendered anywhere (grep: set on 592/594, never read). `barsError` is shown only when no bars
  are held (line 292).
- **Effect:** after a 500 from `/api/market/quotes`, or while the backend is down (up to the
  30 s dashboard poll), the watchlist, quote strip, top bar ("Market data LIVE") and chart
  badge keep saying **LIVE** over frozen prices. Ages keep growing, because they're recomputed
  client-side, but the badge doesn't change.
- **Why it matters:** this breaks the workspace's central guarantee that a cached value is
  never labelled LIVE, in the place a trader actually looks. The backend status logic is
  correct; the defect is in the presentation layer.
- **Coverage:** no browser test covers it.

### Medium

**M1. A timed-out worker is respawned on every request, with no backoff (confirmed by probe).**
- **Where:** `ProcessSessionRunner.run()` / `_kill()` / `_ensure_worker()`
  (`live_market.py` 380–443); `MT5Session._connected_adapter()` (206–227).
- **What happens:** the reconnect backoff (`reconnect_interval`, 5 s) lives inside the worker's
  `MT5Session`, and that state dies with the killed process.
- **Probe:** three requests against a fake MT5 hung for 5 s, with a 1 s timeout, spawned
  **three separate worker processes** (PIDs 13228, 34308, 24764).
- **Effect:** with a hung terminal (the IPC-timeout state seen during this project), the
  dashboard spawns a new Python + MetaTrader5 process every 2 s for quotes, plus one every 30 s
  for bars. Each process calls `mt5.initialize()` and is killed after 4 s, mid-call, for as
  long as the page is open.
- **Fix needed:** parent-side backoff after `timeout` / `worker_failed`.

**M2. The "never launches MT5" guarantee depends on the process image name only.**
- **Where:** `terminal_process_running()` (`live_market.py` 62–77) and
  `MetaTrader5Adapter.initialize()` (`mt5.py` 208–229).
- **Image name only:** the check looks for any process called `terminal64.exe`. Brokers ship
  separate MT5 installs that all use that name. If terminal A is running while
  `MT5_TERMINAL_PATH` points at install B, the check passes, and `mt5.initialize(B)`, which per
  MetaQuotes starts the terminal if it isn't running, can **launch B**.
- **Race window:** there is also a small window between `tasklist` and `initialize`.
- **Verify script:** `scripts/verify_live_market.py` line 130 calls `mt5.initialize(TERMINAL)`
  with **no process check at all**. Its fallback path is hard-coded to
  `C:/Program Files/MetaTrader 5/terminal64.exe`.
- **Not exploited so far:** the earlier runs didn't hit this, but the guarantee as documented
  is stronger than the code.

**M3. A "verified" server-clock result persists after MT5 becomes unavailable (confirmed by
probe).**
- **Where:** `LiveMarketService._update_time_check()` (`live_market.py` 649–670). It returns
  early when no row is LIVE or STALE, so the previous result is kept.
- **Probe:** after terminal loss, rows were `CACHED` and the connection `unavailable`, yet
  `time_check` was still **`verified`**. The Market information panel would show
  "Verified against live ticks" while MT5 is gone. After an account or server switch, the old
  verification keeps standing until a new quote proves otherwise.
- **Misleading label:** a server clock *behind* UTC during market hours is reported as
  `unverifiable_market_quiet` ("market closed or quiet"). That describes the wrong cause.
- **Bars not tied to the check:** bars are labelled `LIVE` whatever the time-check result. The
  chart shows an info notice when it isn't verified, which is acceptable, but the bars payload
  itself carries no verification field.

**M4. A watchlist of more than 50 pairs makes every quote fail (confirmed by probe).**
- **Where:** `persistence._validated_watchlist()` allows up to **200** symbols, but
  `BackendAPI._market_quotes()` rejects more than **50** (`api.py`).
- **Probe:** a saved 51-pair watchlist turned the dashboard's default quotes request into
  `400 invalid_symbols`. Every pair showed UNAVAILABLE with no reason, because of H1.

**M5. The marker meaning "legacy server time" is ambiguous, so collector data could mix time
bases.**
- **Two meanings, one value:** `get_bars()` writes `source_timezone = server_clock.zone`
  (`mt5.py` 403–405), which is `"UTC"` for Exness. Legacy rows from MetaQuotes-Demo, really
  UTC+3, *also* carry `"UTC"`.
- **Guard skipped under UTC:** `_refuse_legacy_mt5_data()` (`collectors.py` 894–) returns
  early when the clock is UTC. If the optional collector were enabled under the default clock,
  it would append correct Exness-UTC EURUSD bars into the **same store** as the 793 UTC+3 bars,
  and no field could tell them apart.
- **Archive fallback mislabels:** `LiveMarketService._archived_bars()` (`live_market.py`
  700–726) labels any `MT5`/`"UTC"` row as legacy, so correct bars would be mislabelled too.
- **Status:** latent. Collectors are disabled and out of scope, but this must be resolved
  before any collector is re-enabled.

**M6. The optional background collector runs MT5 inside the backend process.**
- **Where:** `application._monitor_task()` hands `run_configured_source(...)` to
  `BackgroundService`, a **thread** (`services.py`).
- **Effect if enabled:** if `integrations.collectors_config` is set, the default preference
  `market_monitoring_enabled = True` (`persistence.py` line 26) starts it automatically. The
  MetaTrader5 package then runs inside the web server's process. That brings back the GIL stalls
  the worker architecture exists to prevent. The collector also uses `initialize(path)` with no
  process check, so it can launch the terminal.
- **Status:** latent; collectors are not configured by default.

**M7. The forming candle isn't identified in the bars data contract.**
- **Where:** `MT5Session.bars()` requests `include_current=True` (`live_market.py` 171), but
  neither the row nor the payload marks which bar is still forming.
- **Display side:** the UI assumes the last bar is forming and always prints "last candle still
  forming" (`app.js` 302). That's also printed for archived Parquet bars and on weekends, when
  it's false.
- **Phase 2 risk:** indicators and market structure must exclude the forming bar explicitly, or
  they will repaint. This is a prerequisite for Phase 2.

### Low

- **L1. Cross-site GETs without an `Origin` header are accepted.** An `<img>` or `<script>` tag
  on any website can call
  `http://127.0.0.1:<port>/api/market/quotes?symbols=XAUUSD,...` (`api.py` `_host_allowed`).
  The response can't be read cross-origin, but the request makes MT5 select arbitrary symbols
  into Market Watch and loads the workers. The in-memory bars cache is keyed by `count`
  (10–1000), so repeated cross-site requests can grow memory without bound.
- **L2. Pip size assumes FX conventions.** `pip_size()` treats any 3/5-digit symbol as 10
  points. That's correct for FX majors, including 3-digit JPY, but the API and watchlist accept
  any symbol (metals, indices), where "pips" would be wrong.
- **L3. Archive presentation details.**
  - **Different broker:** the archive fallback shows a different broker's prices
    (MetaQuotes-Demo), labelled only "Parquet archive".
  - **Digits:** `digits` is `None`, so a JPY archive would render 5 decimals.
  - **No max age:** in-memory CACHED bars and quotes have no maximum age. Quote age is shown;
    bar age is not.
  - **Weekend bars:** bars stay `LIVE` on weekends.
- **L4. `ProcessSessionRunner` timing and lifecycle.**
  - **Waits:** the worst-case wait is about twice the timeout (lock wait plus `poll`). The
    first request after start can wait up to `start_timeout` (20 s).
  - **Shutdown:** `close()` takes the lock without a timeout, so it can block for the duration
    of an in-flight bars call (≤ 20 s) plus 5 s.
  - **Failed start:** the parent pipe end isn't closed on a failed start.
  - **Probe:** a hard-killed backend's idle worker exits in < 1 s. A busy worker exits when its
    current MT5 call returns (≈ 6 s in the probe) and prints a `BrokenPipeError` traceback.
- **L5. One DST-boundary bar fails the whole request.** For a configured DST-observing zone, a
  single bar inside a DST fold or gap fails the entire request (`ServerClock.to_utc` raises).
  This never applies to UTC or Exness, nor to NY+7, where the fold falls on the weekend.
- **L6. Symbol variant choice is arbitrary.** `resolve_symbol()` picks the shortest, then
  alphabetical, when several variants are visible (`EURUSDc` vs `EURUSDm`), with no user
  override.
- **L7. `_overall()` overstates.** It reports `LIVE` if *any* single row is LIVE.
- **L8. Verify script docstring.** It contains `scripts\verify…` in a non-raw string, which
  renders as a vertical-tab character.
- **L9. Startup rewrites SQLite settings.** See §3F. Also, the config snapshot stores
  `mt5_terminal_path`, a local path containing the Windows user name.
- **L10. Not-implemented features stay visible.** The navigation still lists Trading Signals,
  Risk, Positions and History, and Settings offers an "Autonomous execution" preference. All are
  labelled not implemented or inactive, and the Overview states "No signals are shown or
  implied". That's acceptable, but worth keeping honest as Phase 2 lands.

## 3. Audit areas

### A. MT5 read-only safety: PASS (with M2)

- **No trading calls:** `grep -rnE "order_send|order_check|order_calc|positions_get|
  positions_total|orders_get|history_orders|history_deals|TRADE_ACTION|login\(" src/ scripts/`
  found **no matches**. `test_only_read_only_mt5_calls_are_made` asserts that every fake-MT5
  call is in `READ_ONLY_CALLS`.
- **Workers:** they run only `MT5Session.quotes` and `.bars` (`_worker_main` dispatches only
  ops the parent sends; the parent sends only `"quotes"`, `"bars"` and `"close"`).
- **Account data:** `server_identity()` returns company, server, account type and
  terminal-connected. Login, name and balance are read by `account_info()` inside the worker
  but never returned, logged or persisted. The identity is held in memory and shown in the
  UI; it isn't stored in SQLite.
- **Market Watch:** `symbol_select(symbol, True)` is called only when `visible` is false
  (`MT5Session._format`). It's a Market Watch view change, not an account setting. L1 means a
  third-party page can trigger it.
- **Terminal launch:** guarded by `tasklist`, with the image-name and race caveats of M2.

### B. Timestamp correctness: PASS (with M3)

**Interpretation used.** The implementation treats MT5 `time` / `time_msc` values as the
**broker's wall-clock time written as if it were seconds since 1970**, not as true UTC
(`ServerClock` docstring, `mt5.py` 91–104). `ServerClock(zone, shift_hours)` describes that
wall clock: server wall time = wall time in `zone` + `shift_hours`.

**Conversion steps** (`ServerClock.to_utc`, `mt5.py` 123–135):
- **Step 1:** `wall = datetime(1970,1,1) + timedelta(seconds=epoch)`. This is naive arithmetic.
  It never consults the machine's timezone (`time.localtime` / `fromtimestamp` aren't used).
- **Step 2:** `reference = wall − shift_hours`, which gives wall time in `zone`.
- **Step 3:** localize `reference` in `zone` with `johanna.time._localize`. That tries both
  `fold` values and accepts only times that round-trip. A nonexistent time (DST gap) or an
  ambiguous one (DST fold) raises `ValueError`, re-raised as `MT5AdapterError`, so the time is
  refused rather than guessed.
- **Step 4:** convert to UTC.

The maths is correct. `is_utc` short-circuits to `wall.replace(tzinfo=UTC)`.

**Why UTC is the default.** With `ServerClock()` (zone `"UTC"`, shift 0), "broker wall clock"
and "UTC" are the same thing. So for a GMT+0 server such as Exness (documented), the epoch is
used as UTC unchanged. That is consistent with the documentation, and no offset was invented.
The UTC default rests on Exness's documentation plus the live check described in §5. This
audit didn't repeat the live check.

**Other checks:**
- **No double conversion:** each epoch is converted once in the adapter and carried as an aware
  UTC `datetime`. `_iso()` only formats it. Tested by `test_tick_and_bar_conversion_is_single_and_utc`.
- **Machine-timezone independence:** tested by `test_conversion_does_not_depend_on_machine_timezone`.
  `tzdata` is a declared dependency, so Windows doesn't depend on a system zone database.
- **Future refusal:** `_reject_future` refuses any tick, or newest bar, more than 60 s ahead of
  the system's UTC clock, raising `MT5TimeBaseError`, shown as `server_clock_mismatch`. Bars
  fetch a tick first, closing the H4/D1 gap where a shifted bar-open time can still be in the
  past.
- **Detection limits:** a server clock *behind* UTC can't be detected as a mismatch, only as
  "not verified". No offset is detectable while the market is closed. Both are documented.
- **Unverified isn't shown as verified:** this holds for chart notices. M3 is the exception.

### C. Market-data integrity: PASS (with M7, L2, L3)

- **Spread:** `(ask − bid) / pip`, rounded to 0.01. Tested: EURUSD 7 points = 0.7 pips;
  USDJPY 0.015 = 1.5 pips.
- **Pip size:** `pip_size` gives 0.0001 for 5- and 4-digit pairs and 0.01 for 3- and 2-digit
  JPY pairs. That's correct for FX (L2 covers other instruments).
- **Age and status:** age = now − tick time (UTC). LIVE if age ≤ 60 s, otherwise STALE.
  Missing tick time gives STALE.
- **No fabricated prices:**
  - **Placeholder ticks:** MT5's all-zero placeholder tick is refused as `symbol_loading`
    (`_is_real_tick`).
  - **Unavailable rows:** they carry no price fields at all.
  - **UI:** prints "—" for UNAVAILABLE.
  - **Exception:** UI staleness, H1.
- **Bars:** sorted, and `strictly_increasing_utc` refuses duplicates and regressions. The
  forming bar is included (`include_current=True`) but not flagged (M7).
- **Cache:** quotes have a 1 s TTL and bars 5 s. Within the TTL, cached MT5 data is served as
  LIVE (by design: fetched within the last second or five). Once MT5 fails, cached values
  become `CACHED`, never LIVE.
- **Concurrency:** cache writes happen under `self._lock`.
- **Parquet fallback:** labelled `CACHED`, `source: "parquet_archive"`, and
  `time_basis: "legacy_server_time"` for legacy rows. The chart shows a warning notice
  (`chartNoticeMarkup`). The archive is read-only (`MarketDataService.read_bars`, pyarrow read).

### D. Worker processes and responsiveness: PASS (with M1, L4)

- **Isolation:** spawned processes connected by a pipe. The backend blocks only in
  `Connection.poll`, which releases the GIL. `test_slow_mt5_call_in_worker_does_not_stall_the_backend`
  checks that a backend thread stalls for less than 0.3 s while the worker holds its GIL for
  1.5 s.
- **Timeout:** terminate, then `join(5)`, then respawn on the next request. Tested by
  `test_timed_out_worker_is_terminated_and_replaced`. No backoff, per M1.
- **Orphans:** none.
  - **Daemon flag:** workers are `daemon=True`.
  - **Parent killed:** a worker exits on pipe EOF when its parent dies.
  - **Probe:** with the parent hard-killed via `TerminateProcess`, the idle worker was gone
    within 1 s, and the busy one when its call returned (about 6 s).
- **Shutdown:** `__main__` `finally` calls `application.stop()`, which calls
  `ServiceManager.stop()`, then `LiveMarketService.stop()`, then `runner.close()` (sends
  `"close"`, then joins). Tested at runner level (`test_close_stops_the_worker`); not tested end
  to end with real processes.
- **Shared state:** the runner has one lock per worker, and the service lock guards all shared
  dicts. No unsynchronised mutation was found.

### E. Dashboard behaviour: PASS WITH CONDITIONS (H1, M3, M7)

- **Consistent selection:** pair and timeframe are stored in SQLite preferences and used by the
  Overview, Charts and Markets pages.
- **Freshness:** last quote time, age and status per row; "Last quote" in the top bar.
- **Display timezone:** all chart labels and timestamps use `Intl.DateTimeFormat` with
  `timeZone: user_timezone`. Data stays in UTC.
- **Distinct states:** separate colours and text for LIVE (green), STALE (amber), CACHED
  (blue), UNAVAILABLE (red) and unverified (info notice). The badge font is very small (9 px).
- **No implied signals:** the Overview says "Not available yet … No signals are shown or
  implied"; signal, risk and position pages say "not implemented".
- **Collectors disabled:** fully usable; the browser tests run without collectors.

### F. Data preservation: PASS

- **This audit changed nothing:** SHA-256, size and modification time of all 14 files under
  `data/` were identical before and after. The SQLite `-wal` / `-shm` sidecars were left
  untouched. Parquet was only read (pyarrow).
- **Startup writes SQLite configuration on every start:**
  - **Every start:** `JohannaApplication.start()` calls `save_application_config()` (replaces
    the config snapshot) and `_sync_monitoring()` (rewrites `service_config.market_data`).
  - **Store startup:** `ApplicationStore.start()` sets WAL mode, runs schema migrations and
    `INSERT OR IGNORE`s defaults.
  - **Lock file:** `InstanceLock` writes `data/johanna.lock`.
  - **Scope:** this is configuration, not market history. WAL mode also explains why
    `-wal` / `-shm` files exist.
- **Historical data:** the live workspace has **no write path** to Parquet or ticks
  (`live_market.py` only reads via the `cached_bars` callback). The only code that writes
  history is the collector, which is disabled; M5 and M6 cover its latent risks.
- **Separation:** live retrieval (MT5 workers) and the archive (Parquet) are separate. The
  archive is consulted only as the CACHED fallback, and labelled as such.
- **Archive contents (read-only check):** 793 EURUSD bars (83 H1, 710 M5) and 3,053 ticks,
  2026-10-23 to 2026-10-30. Every bar carries `source="MT5"`, `source_timezone="UTC"` (the
  legacy marker).

## 4. Commands and probes executed

| Command | Result |
|---|---|
| `git status --short`, `git diff --stat` | 8 modified, 33 untracked (pre-existing work); preserved |
| `sha256sum` + `find -printf` over `data/`, before and after | identical |
| `.venv311\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.11.8) | **237 passed**, 0 skipped |
| `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.13.3) | **237 passed**, 0 skipped |
| `python -m ruff check --no-cache .` | All checks passed |
| `python -m ruff format --check --no-cache .` | 64 files already formatted |
| `node --check src/johanna/web/app.js` (v23.9.0) | OK |
| grep for trading / position APIs in `src/`, `scripts/` | no matches |
| Orphan probe (scratchpad `orphan_probe.py`, fake MT5) | no orphans (§3D) |
| Defect probe (scratchpad `defect_probe.py`, fake MT5, temp dirs) | confirmed M1, M3, M4 |
| pyarrow read of `data/**/clean/*.parquet` | 793 bars + 3,053 ticks, legacy markers |

**Not run, by design:** `scripts/verify_live_market.py`, any connection to the real terminal or
account, and the collectors. The 18 browser tests ran headless Edge against the fake MT5 and
passed.

## 5. Completion-report claims not independently verified

- **Live Exness verification:** run 2026-11-12 06:15 UTC; `Exness-MT5Real21` reported
  `time_check: verified`, all seven pairs LIVE, and the cross-check matched. Evidence: only the
  implementer's captured output. Not repeated, under this audit's constraints.
- **Live latencies:** `/health` ≤ 78 ms (MetaQuotes-Demo) and ≤ 31 ms (Exness). Not reproduced;
  only the fake-MT5 GIL test supports the design.
- **Real-package GIL hold:** "0.26–2.9 s" per call. Not reproduced; the test simulates it.
- **MetaQuotes-Demo clock:** +7,190 s offset and the New York + 7 inference. Not reproduced. The
  793-bar archive's markers are consistent with server-time storage.
- **Closed-terminal simulation:** the live run. Covered here only by fake-MT5 tests.
- **The 23:22 backend start:** attributed to a manual start. Can't be verified from the file
  system.

## 6. Remaining Exness-specific verification

1. **Re-run the live acceptance yourself:** run `scripts/verify_live_market.py` during market
   hours, and once over a weekend. Expect `STALE`, `unverifiable_market_quiet`, and no
   `verified` claim. (Fix M2's unguarded `initialize` in the script first, or make sure the
   terminal is open.)
2. **Pin the terminal path:** set `MT5_TERMINAL_PATH` to the Exness install's exact
   `terminal64.exe`, so M2 can't pick another install.
3. **Account type:** the connected account is **real** (cent, `c` suffix). Prefer an Exness demo
   account for development.
4. **Symbol variants:** if both `c` and `m` variants are visible, confirm which one is chosen
   (L6).
5. **Phase 2 conventions to decide, not defects:**
   - **D1 boundary:** Exness D1 candles open at 00:00 UTC, unlike New York-close charts. That
     affects daily levels and market structure.
   - **Sessions:** Exness stays on UTC all year, so session times shift by an hour in local
     DST terms.

## 7. Ready for Phase 2 (indicators and market structure)?

**Yes, with conditions.** Retrieval is kept separate from analysis, which is what Phase 2 needs:
indicators can consume `LiveMarketService.bars()` output without touching MT5.

**Before Phase 2:**
- **H1:** the UI must downgrade, or clear, LIVE state when a request fails, and show the error.
- **M3:** reset or expire the time check when MT5 becomes unavailable or the server identity
  changes.
- **M7:** mark the forming bar in the bars payload, so indicators can exclude it and don't
  repaint.

**Soon after, small:**
- **M1:** parent-side backoff after timeouts.
- **M4:** align the 50/200 limits.
- **M2:** match the terminal process by full path, and guard the verify script.

**Before any collector is re-enabled:**
- **M5:** an unambiguous time-basis marker, or a separate store for the legacy bars.
- **M6:** run the collector out of process, with the same no-launch guard.

Johanna's purpose is market analysis and decision support. This workspace is the right
foundation for that, and none of the findings call for redesign.
