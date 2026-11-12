# Automatic live market refresh: report

Date: 2026-11-12 · Claude Code (Opus 5.5) · Working tree only, **nothing committed**

## 1. Previous behaviour and the problem

The dashboard did poll, but the mechanism wasn't built for continuous live monitoring:

- **Chart updates were slow.** Candles were requested only every **30 s**, so the forming candle
  visibly lagged the market. Quotes refreshed every 2 s.
- **Timers didn't wait for answers.** Fixed `setInterval` timers fired regardless of whether the
  previous request had finished. An in-flight flag skipped overlapping requests, but there was:
  - no backoff after failures;
  - no request timeout, so one hung request blocked polling until the expiry logic intervened;
  - no protection against a late answer overwriting newer state.
- **The chart reset regularly.** Every 30 s the background dashboard refresh rebuilt the whole
  page, recreating the chart canvas, even when nothing had changed.
- **Every refresh repainted the chart**, changed or not.
- **Clock-only candles looked confirmed.** Candles closed by time alone were drawn exactly like
  candles confirmed by a later tick.

## 2. Implementation and files changed

The existing pipeline is reused as is. There's no new endpoint, provider or backend service, and
no backend file changed. The browser calls the same `GET /api/market/quotes` (the watchlist) and
`GET /api/market/bars` (the selected pair and timeframe). Those go through the same
`LiveMarketService`, caches, worker processes, backoff and terminal verification.

| File | Change |
|---|---|
| `src/johanna/web/app.js` | **New:** `POLL` (intervals, backoff and timeouts), `createPoller()`, `fetchJson()`, `refreshQuotes()`, `refreshBars()`, `quotesInterval()`, `barsInterval()`, `checkExpiry()`, `livePolling`, `startLivePolling()`, `stopLivePolling()`, `livePollingState()`, `onVisibilityChange()`, `candleEvidence()`, `mergeBars()`, `sameBar()`, `setInner()`, `replaceOuter()`, `dashboardSignature()`; a `LOADING` status before the first answer. **Changed:** `loadDashboard()` (no full re-render when nothing changed), `drawChart()` (skips unchanged repaints; clock-only candles faded), `renderLive()` (targeted, change-only DOM updates), `chartStatusMarkup()` / `chartFootMarkup()` (forming and unconfirmed counts), `savePreferences()` (restarts the affected poller). **Removed:** the four `setInterval` timers and the global `visibilitychange` listener. `loadQuotes()` / `loadBars()` remain as one-line wrappers. |
| `tests/fake_mt5.py` | The fake's forming candle now follows the current bid, like real MT5, and completed candles keep stable values as the window slides. Test-only. |
| `tests/test_browser_smoke.py` | 10 new tests; `_serve()` accepts a service clock; 2 existing tests adapted to the new lifecycle (§8) |

## 3. Quote polling

- **Interval:** about every **3 s** while the market is live. It slows to **6 s** when no quote is
  LIVE (market closed, MT5 unavailable), which is still well inside the 10 s display expiry.
- **Scope:** requests carry no symbol list, so the backend answers for the **saved watchlist**.
- **What updates:** bid, ask, spread, quote age and status (LIVE / STALE / CACHED / UNAVAILABLE),
  through the existing `quoteView()` rules.
  - **After a failed refresh:** the last good quote stays visible as **CACHED**, with its age and
    the failure reason.
  - **No earlier quote:** **UNAVAILABLE**, with no price shown.
  - **Before the first answer:** the status reads **LOADING** instead of claiming UNAVAILABLE.
- **Watchlist changes:** the running request is abandoned and a new one starts, so an answer for
  the old watchlist can't land.
- **Recovery:** when quotes come back LIVE after an outage, the chart is refreshed immediately.

## 4. Candle polling and chart updates

- **Interval:** about every **5 s** for the selected pair and timeframe only. It slows to
  **20 s** when the chart isn't LIVE or no tick has arrived for 5 minutes (market closed).
- **Merging (`mergeBars`):**
  - **Matching:** each refresh is merged into the current candles **by open time**, with MT5's
    answer authoritative.
  - **Forming and new candles:** the forming candle takes its new OHLC; when the timeframe
    advances, the new candle is appended and the previous one becomes complete.
  - **No duplicates:** an open time can never appear twice.
  - **Unchanged candles:** they keep their existing objects.
  - **Nothing invented:** no values are interpolated or extended.
- **Repaints:** the canvas is repainted only when something visible changed: candles and their
  evidence class, the live bid line, size, theme, timezone or digits.
- **Snapshot refresh:** the periodic dashboard refresh no longer rebuilds a live page, so the
  canvas is never recreated and the chart doesn't flicker or reset.
- **Window position:** the chart has **no zoom or pan**. It always shows the newest candles,
  sized to the canvas width, and refreshes never move that window (§12).
- **Distinct candle styles:**
  - **Forming:** hollow, with a dashed wick.
  - **Closed by clock only:** faded, counted as "N unconfirmed" in the chart status, and
    explained in the footer.
  - **Tick-confirmed:** solid.
- **Live bid line:** follows every fresh quote.
- **Changing the selection:** switching pair or timeframe abandons the running request and
  fetches the new selection at once. Three guards make sure a late answer for the old selection
  is discarded:
  - a request sequence number;
  - the selection key;
  - the payload's own `symbol` and `timeframe`.

**Clock-only candles.** `candleEvidence()` classifies each candle using only the backend's
`complete`, `close_t` and `latest_tick_utc`:
- **forming:** `complete` is false;
- **tick:** a tick at or after the close was seen, so the prices are confirmed;
- **clock:** closed by time only, so a feed stall can't be ruled out;
- **unproven:** archived bars, which carry no close or tick data.

Clock-only candles are never presented as confirmed price data.

## 5. Visibility and lifecycle

- **Start:** `startLivePolling()` runs once the dashboard has loaded. It's idempotent: calling
  it again adds no timers and no listeners. Listeners are registered through an
  `AbortController`.
- **Stop:** `stopLivePolling()`, also run on `pagehide`:
  - clears every timer;
  - aborts running requests;
  - removes the listeners.

  A page restored from the back-forward cache restarts polling.
- **Hidden tab:** no new requests are scheduled; a request already running may finish.
- **Visible again:** quotes, chart and snapshot refresh immediately, and the expiry display is
  re-checked.
- **Backend side:** nothing changed. It still has no background polling service; data is
  requested only while a visible dashboard asks for it.

## 6. Failure handling and recovery

- **No overlaps:** each stream (quotes, chart, snapshot) has its own poller, and a poll is
  scheduled only *after* the previous one finishes. Requests therefore can't overlap or pile
  up.
- **Failure types:** HTTP errors (the backend's own message is shown), network errors, malformed
  or unexpected responses, and requests unanswered within 45 s all count as failures.
- **Backoff:** each failure doubles the delay (interval × 2^failures), capped at **30 s**, so
  there's no tight loop. A success resets it.
- **Recovery:** automatic, and covers both a broken backend and a returning MT5 connection. It
  takes no longer than the current backoff delay.
- **Coexists with the backend:** the backend's 1 s and 5 s caches, its worker restart backoff
  (2 s doubling to 60 s), its timeouts and its full-path terminal verification are unchanged.
  The browser's slower pace adds to them rather than replacing them.
- **Display expiry still works:** shown data becomes CACHED after 10 s (quotes) or 65 s
  (chart) without a fresh answer, even while a request hangs.

## 7. Performance

| Situation | Quote requests | Chart requests |
|---|---|---|
| Market live | 1 every ~3 s | 1 every ~5 s |
| Market closed or MT5 unavailable | 1 every ~6 s | 1 every ~20 s |
| Failing | 6 s, 12 s, 24 s, 30 s … | 10 s, 20 s, 30 s … |
| Tab hidden or page closed | none | none |

- **Unselected pairs:** never requested for candle data.
- **No full-page rebuilds:** only changed elements are rewritten, and the canvas is repainted
  only on visible change.
- **Snapshot:** refreshed every 30 s, without a re-render unless something changed.

## 8. Tests executed

| Command | Result |
|---|---|
| `.venv311\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.11.8) | **296 passed**, 0 skipped (286 before; 10 new) |
| `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.13.3) | **296 passed**, 0 skipped |
| The 10 new refresh tests, run again repeatedly (3.11 ×2, 3.13 ×2, plus 5 runs of the two fake-clock tests) | all passed |
| `python -m ruff check --no-cache .` | All checks passed |
| `python -m ruff format --check --no-cache .` | 68 files already formatted |
| `node --check src/johanna/web/app.js` (v23.9.0) | OK |
| Browser smoke tests (`tests/test_browser_smoke.py`, included above) | 36 passed |

**Flakiness found and fixed.** An earlier full run had one failure, on 3.13, in
`test_forming_candle_updates_and_a_new_candle_is_appended`, and one repeat run failed the same
way. The test's fake market clock moved with real time, so an M1 candle sometimes rolled over
naturally mid-test. That was a test defect, not a code defect: the fake clock now starts 5 s
into a minute. The two fake-clock tests then passed five times in a row, and both complete
suites passed (results above).

**New tests in `tests/test_browser_smoke.py`** (headless Edge, fake MT5, temporary data
directories):

| Test | Covers |
|---|---|
| `test_quotes_refresh_automatically_about_every_three_seconds` | Updated bid and spread appear unprompted; requests about 3 s apart, never faster |
| `test_quote_requests_never_overlap_or_pile_up` | A backend slower than the interval: at most one request at a time, no backlog |
| `test_polling_pauses_while_hidden_and_resumes_when_visible` | No requests while hidden; quotes and chart refresh within 2 s of becoming visible |
| `test_forming_candle_updates_and_a_new_candle_is_appended` | Forming candle's close and high follow the price; completed candle unchanged (same object); next minute adds one candle; previous becomes complete and tick-confirmed; 300 candles, no duplicates; canvas and timeframe preserved |
| `test_chart_is_not_rebuilt_or_repainted_when_nothing_changed` | Two refreshes plus a snapshot refresh: canvas not recreated, zero repaints |
| `test_late_answer_for_an_old_selection_cannot_overwrite_the_chart` | A slow EURUSD answer arriving after switching to USDJPY is discarded |
| `test_clock_only_closed_candles_are_not_presented_as_confirmed` | Quiet feed: clock-only candles classified `clock`, shown "unconfirmed"; older candles `tick` |
| `test_merge_keeps_unchanged_candles_and_never_duplicates` | `mergeBars`: ordering, de-duplication, MT5 values, unchanged objects kept |
| `test_failures_back_off_without_a_tight_loop_and_polling_recovers` | Backend outage: at most 3 requests in 10 s; recovers to LIVE automatically |
| `test_starting_twice_adds_no_timers_or_listeners_and_stop_ends_polling` | One `visibilitychange` and one `pagehide` listener after 3 starts; stop gives zero timers, zero listeners, zero requests; restart resumes |

**Earlier tests that still cover their behaviour:**
- **Failed refreshes:** quotes and chart go from LIVE to CACHED with the reason, and recover.
- **Hung requests:** they expire.
- **Before any success:** a failure shows UNAVAILABLE with no prices.
- **Clock check:** it follows disconnection.
- **Worker backoff:** `test_mt5_worker_process.py`.

**Two existing tests adapted:**
- **Hung-quote test:** it now inspects `livePolling.quotes.inFlight`.
- **Chart-expiry test:** it stops polling before jumping the clock, removing a race.
- **Recovery waits:** recovery checks now lower the backoff cap before the simulated outage,
  because recovery deliberately waits out the backoff.

## 9. MT5 remains read-only

- **No trading calls:** `grep -rnE "order_send|order_check|order_calc|positions_get|
  positions_total|orders_get|history_orders|history_deals|TRADE_ACTION|\.login\(" src/
  scripts/` found **no matches** (exit status 1).
- **Backend untouched:** no backend, adapter or worker code changed. The browser still reaches
  MT5 only through the two read-only endpoints.
- **Safety checks preserved:** full-path terminal verification, worker isolation and backoff are
  untouched.
- **No real account used:** no automated test or step of this task used the real Exness account.

## 10. Collectors remain disabled

No collector code or configuration was changed, and no collector was run. The tests use the
default configuration (`collectors_config` unset).

## 11. `data/` unchanged

- **All 11 readable files unchanged:** SHA-256, size and modification time were identical
  before and after this task, including `johanna.sqlite3`. `data/` held 12 files at the start
  of this task and still does.
- **The SQLite sidecars were already gone at the start of this task.** The `-wal` / `-shm`
  files noted in earlier reports weren't there when this task's baseline was taken. Your own
  Johanna backend, started 09:54 on `data/`, uses SQLite's WAL mode, which removes those files
  when connections close cleanly; that is the most likely cause. This task didn't open, remove
  or modify any SQLite file.
- **`data/johanna.lock` couldn't be hashed.** Your own Johanna backend (`python -m johanna`,
  started 09:54 with its two MT5 workers) held it open throughout. Its size and modification time
  were unchanged.
- **Your backend was left alone:** it wasn't stopped, restarted or contacted. All tests used
  temporary directories and their own ports.

## 12. Known limitations

- **No zoom or pan.** The chart has none, so "preserving zoom and position" means the window of
  newest candles is never moved or reset by refreshes. Adding zoom or pan would be new
  functionality and is out of scope.
- **Intervals are targets.** A slow backend, backoff or the quiet-market intervals make polling
  slower, never faster.
- **Full window per refresh.** Each chart refresh still requests the full 300-candle window,
  which the backend serves from MT5 or its 5 s cache. Requesting only recent candles would need
  an API change.
- **One request can finish after hiding:** hiding the tab stops new requests, but one already
  running (quotes, chart or snapshot) is allowed to finish.
- **Repeat open dashboards multiply load.** Several open dashboard tabs each poll independently;
  the backend's caches and worker serialization absorb this.
- **Clock-only candles are shown, not hidden.** They're marked unconfirmed; deciding whether
  analysis may use them is for Phase 2 (not started).
- **Not verified live.** The live refresh hasn't been checked against the real Exness terminal
  in this task.
