# Live market workspace: remediation report

Date: 2026-11-12 · Claude Code (Opus 5.5) · Working tree only, **nothing committed**

**What this covers:** the six findings from `docs/live-market-workspace-audit.md` that were
selected for fixing: H1, M1, M2, M3, M4 and M7. M5 and M6 are deferred (§10).

**Result:** all six are fixed and covered by regression tests.
- **Tests:** 286 pass on Python 3.11 and 3.13 (up from 237: 49 new tests).
- **Read-only:** MT5 stays read-only.
- **Collectors:** they stay disabled.
- **Data:** `data/` is unchanged; SHA-256, size and modification time were identical before and
  after.

## 1. Findings addressed

| # | Finding | Fix | Status |
|---|---|---|---|
| H1 | LIVE badges survived failed refreshes | The frontend derives what it shows. A failed or overdue refresh turns stored LIVE data into CACHED, keeps its age, shows the reason, and drops clock verification. | **Fixed** |
| M1 | Worker respawned on every request | Bounded exponential backoff in `ProcessSessionRunner` (2 s doubling to 60 s), reset by any worker reply | **Fixed** |
| M2 | Terminal checked by process name only | The intended terminal is confirmed **by full executable path** and attached to by that path. The verify script uses the same guard. | **Fixed** |
| M3 | "Verified" outlived the connection | Clock check states `verified` / `stale_ticks` / `unavailable` / `mismatch`. It is set only from fresh ticks, dropped on connection loss, and expires after 30 s without a re-check. | **Fixed** |
| M4 | 200 saveable, 50 quotable | One limit, `MAX_WATCHLIST = 200`, used everywhere. Quotes are fetched in batches of 25, and the API explains every refusal. | **Fixed** |
| M7 | Forming candle not identified | Every bar carries `close_t` and `complete`, derived from the timeframe, the server clock and the latest server tick. The forming candle is drawn hollow. | **Fixed** |

While writing the H1 browser tests I found and fixed two related frontend defects:
- **Chart stayed LIVE after quotes failed.** It kept its LIVE badge when the quotes refresh
  failed. A failed or overdue quote refresh now downgrades it too.
- **Retry loop after a failed bars request.** `loadBars()` retried in a tight loop, because it
  compared against the stored key instead of the requested one.

## 2. Files and functions changed

| File | Changes |
|---|---|
| `src/johanna/live_market.py` | **New:** `_process_image_paths()` (stdlib `ctypes`), `find_running_terminal()`, `bar_is_complete()`, constants `TIMEFRAME_SECONDS`, `QUOTE_BATCH`, `COMPLETION_GRACE`, `DEFAULT_TERMINAL_IMAGE`, `_CONNECTION_LOSS`. **Changed:** `_real_session()` (attach by verified path), `MT5Session.bars()` (completion flags, `latest_tick_utc`), `ProcessSessionRunner` (`run`, `_refuse_during_backoff`, `_worker_failed`, `_ensure_worker`, `backoff_seconds`, `spawn_count`), `LiveMarketService.__init__` (`verification_ttl`), `.status()`, `._set_time_check()`, `._request()`, `.quotes()` (batches), `._update_time_check()`, `._archived_bars()`, `_bar()`. Cache TTL comparisons made strict (`<`). **Removed:** `terminal_process_running()` (the `tasklist` name check). |
| `src/johanna/adapters/mt5.py` | `ServerClock.from_utc()` and `ServerClock.bar_close_utc()` |
| `src/johanna/persistence.py` | `MAX_WATCHLIST = 200`, used by `_validated_watchlist()` (message now states the received count) |
| `src/johanna/api.py` | `_market_quotes()`: limit is `MAX_WATCHLIST`, with explanatory `message`s; `_market_bars()`: explanatory `message` |
| `src/johanna/web/app.js` | **New:** `quoteView()`, `overallStatus()`, `quotesExpired()`, `barsExpired()`, `marketStatus()`, `quotesProblemText()`, `clockCheckText()`, `chartKey()`, `currentBarsError()`, `requestFailure()`, `networkFailure()`, `showSaveMessage()`, a 1 s expiry timer. **Changed:** `chartData()`, `chartStatusMarkup()`, `chartNoticeMarkup()`, `chartEmptyMarkup()`, `chartFootMarkup()`, `drawChart()` (hollow forming candle), `loadQuotes()`, `loadBars()`, `renderTopbar()`, `quoteStripMarkup()`, `watchlistRows()`, `watchlistMarkup()`, `marketInfoMarkup()`, `renderLive()`, add-pair handler. |
| `src/johanna/web/styles.css` | `.forming-badge`, `.save-message` |
| `scripts/verify_live_market.py` | `direct_read()` guarded by `find_running_terminal`; no hard-coded terminal path; body moved into `main()`; candle checks use `complete` |
| `tests/conftest.py` | Test isolation now patches `find_running_terminal` |
| `tests/fake_mt5.py`, `tests/fake_mt5_session.py` | `initialize_paths`; `make_flag_session`, `make_offline_session` |
| `docs/live-market-workspace-report.md` | Pointer to this report; two superseded statements corrected |

## 3. Design decisions

**H1: the display is derived, never stored.**
- **Last good response, timed:** `state.quotes` and `state.bars` hold only the last
  *successful* responses, with the time they arrived.
- **Derived display state:** what the page shows comes from `quoteView()` and `chartData()`.
  After a failed refresh (HTTP error, backend error, network failure), or when no fresh
  response has arrived for 10 s (quotes) or 65 s (bars), they:
  - keep earlier values visible as **CACHED**, with their age;
  - show the reason, using the backend's own error message where it sent one;
  - mark the server-clock check as not verified.
- **Hung requests:** expiry also covers requests that hang, and a 1 s timer re-renders the
  moment it applies.
- **No prior data:** UNAVAILABLE with no price. Only a new successful response restores LIVE,
  and the backend grants LIVE only to ticks that passed the age and future-time checks.

**M1: backoff lives in the parent.**
- **Why the parent:** the old reconnect pacing lived inside the worker and died with it, so the
  backoff now sits in `ProcessSessionRunner`.
- **What counts as a worker failure:** `worker_failed` (start failure or crash) and `timeout`
  (terminated). Each is followed by 2, 4, 8 … 60 s of immediate, honest refusals that carry the
  last failure's reason plus "Next attempt in N s", without spawning anything.
- **What doesn't:** normal MT5 unavailability, answered by a healthy worker, is not a worker
  failure.
- **Reset and recovery:** any worker reply resets the backoff. A failed worker recovers on the
  first request after its backoff, with no application restart.

**M2: identity by full path.**
- **Process paths:** `find_running_terminal()` lists process image paths using
  `EnumProcesses` and `QueryFullProcessImageNameW` (stdlib `ctypes`; no new dependency, and no
  reliance on the deprecated `wmic`).
- **Configured terminal:** a process at *exactly* `MT5_TERMINAL_PATH` must be running.
  Comparison ignores case and slash direction. Another broker's `terminal64.exe` gives
  `terminal_path_mismatch`.
- **Unconfigured:** exactly one terminal must be running; several give `multiple_terminals`.
- **Cannot list processes:** gives `terminal_unverified`.
- **Attach:** `initialize()` is called only with the verified, running path, never without a
  path.
- **Residual window:** a terminal closed in the milliseconds between check and connect remains a
  theoretical risk (§10).

**M3: verification means "now".**
- **Source:** only ticks fetched from MT5 in that request can verify; CACHED values and the 1 s
  in-memory cache never do.
- **Connection loss:** loss-type failures (`terminal_not_running`, `connection_lost`,
  `timeout`, `worker_failed`, …) set `unavailable` with the reason. Stale ticks set
  `stale_ticks`; refused future ticks set `mismatch`.
- **Expiry and history:** `verified` expires to `unavailable` (`not_rechecked`) after 30 s
  without a re-check. `last_verified_utc` keeps the history visibly separate ("last verified
  …").
- **UTC not assumed:** UTC is never assumed to be verified. The Exness UTC configuration and
  mismatch handling are unchanged.

**M4: one limit of 200, kept.**
- **Why 200:** it was already the persisted limit, and keeping it means no saved watchlist can
  be invalidated.
- **Batching:** the backend fetches quotes in batches of 25 symbols, so each MT5 round trip
  stays inside its 4 s timeout, including first-time symbol resolution. A failed batch affects
  only its own pairs.
- **Clear refusals:** above 200 symbols, the API and preferences both refuse with explanatory
  messages. The Markets page shows "N / 200" and explains refused additions. Nothing is
  truncated.

**M7: completion is evidence-based.**
- **Close time:** `close_t` is computed on the server clock (`ServerClock.bar_close_utc`, which
  handles DST-observing server zones; identical to open + duration under UTC).
- **Complete when:** a bar is `complete` when either:
  - a server tick at or after its close exists, or
  - the system clock is more than 60 s past the close (the tolerated clock skew).

  Array position is never used. Rows are evaluated individually.
- **Market closures:** a candle can stay "forming" a little longer than strictly necessary, for
  example Friday's D1 candle until 24 h after it opened, but an incomplete candle is never
  reported complete.
- **Cached and archived bars:**
  - **Cached:** a forming candle stays forming, because its values are partial.
  - **Archived Parquet:** bars are complete, because the collector stores closed bars only.
  - **Unaffected:** OHLC values and timestamps are unchanged.
- **Wire format per bar:** `{t, o, h, l, c, v, close_t, complete}`. Each bars payload also
  carries `latest_tick_utc`. Phase 2 indicators must use `complete` bars only.

## 4. New regression tests

| File | Tests |
|---|---|
| `tests/test_live_market_remediation.py` (new) | 37 tests, listed in detail below |
| `tests/test_mt5_worker_process.py` | 1 replaced and 4 added. Timeout, then backoff, then replacement; exponential and bounded progression; recovery without restart; healthy-worker unavailability isn't backed off; no worker survives failures and `close()`. |
| `tests/test_browser_smoke.py` | 8 added. Failed quote refresh: LIVE becomes CACHED with reason, then recovery. Hung quote refresh expires. Failure before any success: UNAVAILABLE with no price. Failed chart refresh: CACHED, then a new pair goes UNAVAILABLE, then recovery. Hung chart refresh expires. Forming candle flagged and drawn hollow. Clock check after disconnect. Watchlist limit and invalid addition explained. 2 updated. |
| `tests/test_live_market.py` | Stale state renamed `stale_ticks`; archive bars are `complete` |

**`test_live_market_remediation.py` in detail:**
- **M2:** full-path matching; another terminal refused; unconfigured exactly-one / several /
  none; unlistable processes refused; real Windows process listing; the worker session never
  initializes an unverified terminal and attaches by the verified path; the verify script cannot
  start MT5.
- **M3:** verified, then disconnection, timeout, stale ticks, mismatch, or reconnection; expiry
  without a re-check; cached quotes don't re-verify.
- **M4:** 200 pairs quoted in 8 batches; 201 refused clearly with nothing lost; a failed batch
  affects only its pairs.
- **M7:** forming and complete marked for M1, M5, M15, H1, H4 and D1; close plus grace with no
  new tick (6 timeframes); a tick after close proves completion; the completion rule itself; a
  cached forming candle is never promoted; server-clock close for UTC and New York + 7 across
  DST.

## 5. Test results (2026-11-12, Windows 11, Python with no network or terminal access)

| Command | Exit | Result |
|---|---|---|
| Baseline before changes: `.venv311\Scripts\python.exe -m pytest -q -p no:cacheprovider` | 0 | 237 passed |
| `.venv311\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.11.8) | 0 | **286 passed**, 0 skipped |
| `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.13.3) | 0 | **286 passed**, 0 skipped |
| `python -m ruff check --no-cache .` | 0 | All checks passed |
| `python -m ruff format --check --no-cache .` | 0 | 67 files already formatted |
| `node --check src/johanna/web/app.js` (v23.9.0) | 0 | OK |
| `tests/test_browser_smoke.py` (included above) | 0 | 26 passed |
| `tests/test_mt5_worker_process.py` (included above) | 0 | 9 passed |

Browser tests run in headless Edge against the fake MT5. The worker-process tests spawn real
child processes running fake sessions. Nothing contacted a real MT5 terminal or account.

## 6. MT5 remains read-only

- **No trading calls:** `grep -rnE "order_send|order_check|order_calc|positions_get|
  positions_total|orders_get|history_orders|history_deals|TRADE_ACTION|\.login\(" src/
  scripts/` found **no matches**.
- **Fake enforces it:** `FakeMT5` implements only the read-only calls, and
  `test_only_read_only_mt5_calls_are_made` passes.
- **New code is read-only:** process listing opens other processes with
  `PROCESS_QUERY_LIMITED_INFORMATION` only. It never terminates, starts, logs in to or changes
  any terminal.
- **No real account used:** the Exness account wasn't used by any automated test or
  verification in this task.

## 7. Collectors remain disabled

- **Untouched:** no collector code was changed. `integrations.collectors_config` still
  defaults to `None`, and the market-monitoring state is `not_configured` (asserted by
  `test_api_serves_watchlist_quotes_and_bars_without_collectors`).
- **Not run:** no collector was run.

## 8. `data/` not modified

- **Hashes:** SHA-256, size and modification time of all 14 files under `data/` were
  identical before and after this task, including the SQLite `-wal` / `-shm` sidecars, which
  were left in place.
- **Temporary directories:** all tests and probes used temporary directories.

## 9. Behaviour changes to be aware of

- **New reasons:** `terminal_path_mismatch`, `multiple_terminals`, `terminal_unverified` and
  `worker_failed`. A worker crash used to report `mt5_error`; it now reports `worker_failed`.
- **Several terminals:** if more than one MT5 terminal is running and `MT5_TERMINAL_PATH` isn't
  set, live data is now refused (`multiple_terminals`) instead of guessing.
- **Clock-check names:** `unverifiable_market_quiet` is now `stale_ticks`, and a new
  `unavailable` state appears in `/status` and `/api/market/quotes`.
- **Bar fields:** bars gain `close_t` and `complete`, and bars payloads gain `latest_tick_utc`.
  Existing fields are unchanged.
- **Simulated closed-terminal run:** when the real terminal is open,
  `scripts/verify_live_market.py` now reports `terminal_path_mismatch` for its simulated
  "closed terminal" run, because a different terminal is running. When no terminal is open, it
  reports `terminal_not_running`. Both refuse, and neither launches anything.

## 10. Remaining limitations and deferred work

**Deferred: both must be resolved before any collector is ever re-enabled.**
- **M5:** the time-basis marker is ambiguous. Legacy server-time archive rows and correct UTC
  rows both carry `source_timezone = "UTC"`, and `_refuse_legacy_mt5_data()` skips its check
  under a UTC clock. A collector could therefore mix the two in one store. Not addressed, per
  instructions; the archive and its labels are unchanged.
- **M6:** the optional collector runs MT5 in a thread inside the backend process. That brings
  back GIL stalls, and its `initialize(path)` has no running-terminal check. Not addressed;
  collector execution is unchanged.

**Other limitations:**
- **Check-to-connect window:** a few milliseconds remain between confirming the terminal is
  running and connecting to it. A terminal closed in exactly that window could be restarted by
  `MetaTrader5.initialize()`. The MetaTrader5 package offers no attach-only mode.
- **Feed stalls:** if the terminal's feed stalls while it still reports "connected", a candle
  whose close has passed is marked complete even though the terminal may have missed ticks.
  MT5 backfills the history when it catches up, and the stall is visible as STALE quotes and
  `stale_ticks`.
- **Server clocks behind UTC:** these still read as `stale_ticks`, not as a mismatch. That's
  inherent to tick-based checking.
- **Not re-run live:** the live acceptance script wasn't re-run against the real terminal in
  this task, per instructions. Run `.venv311\Scripts\python.exe scripts\verify_live_market.py`
  with MT5 open to confirm the new guard on your machine. Set `MT5_TERMINAL_PATH` if more than
  one terminal is installed and running.
- **Audit Low findings:** L1–L10 from the audit are unchanged, apart from L8 (the script
  docstring), which is fixed.

## 11. Ready for Phase 2?

**Yes.** The three conditions the audit set for Phase 2 are met:
- **H1:** the display can no longer show a stored value as LIVE.
- **M3:** clock verification describes the current connection only.
- **M7:** every bar states whether it's complete, so indicators can use closed candles only.

M1, M2 and M4 are also fixed. Phase 2 (indicators and market structure) should consume
`LiveMarketService.bars()` and filter on `complete`. M5 and M6 remain blockers for collectors
only, not for Phase 2. Phase 2 has not been started.
