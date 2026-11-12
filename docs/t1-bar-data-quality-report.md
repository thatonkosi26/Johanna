# T1: bar metadata, live candle checks and closure evidence

Date: 2026-11-12 · Claude Code (Opus 5.5) · Working tree only, **nothing committed** · Stopped
after T1 (T2–T10 not started)

## 1. Files changed

| File | Change |
|---|---|
| `src/johanna/adapters/mt5.py` | **New** `read_bars()`: bars exactly as MT5 returned them (its order, duplicates kept) plus `spread_points`; no all-or-nothing ordering check. **`get_bars()`** now delegates to it and then sorts, enforces strictly increasing times and removes `spread_points`, so its output and behaviour are unchanged for the collector, whose Parquet store rejects unknown fields. **`price_format()`** returns `None` for missing, zero or invalid digits/point instead of crashing (`_optional_int`, `_optional_positive_float`). |
| `src/johanna/live_market.py` | **New:** `ohlc_issues()`, `check_bars()`, `_finite()`, `_bar_values()`, closure constants (`forming`, `tick_observed`, `clock_only`, `unknown`). **Changed:** `pip_size()` returns `None` without metadata; `MT5Session.quotes()` (no spread in pips without a pip size); `MT5Session.bars()` (uses `read_bars`, checks bars, sets closure, adds `point`, `pip_size`, `rejected_bars`, `quality`); `_archived_bars()` (same checks; closure `unknown`); `_bar()` (new fields); quote rows (add `point`, `pip_size`). |
| `src/johanna/web/app.js` | `candleEvidence()` prefers the backend's `closure` (old derivation kept as fallback); candles with a missing price aren't drawn (`drawable`); `sameBar()` also compares `closure` and `valid`; an "N invalid" badge in the chart status |
| `src/johanna/web/styles.css` | `.invalid-badge` |
| `tests/fake_mt5.py` | `rates_hook` (inject anomalous rates); `metadata_missing` symbol option. Test-only. |
| `tests/test_bar_checks.py` | **New:** 25 tests (§4) |
| `tests/test_browser_smoke.py` | 1 new browser test (§4) |

No other file changed. The new script from T0 is untouched.

## 2. Implementation summary

- **Metadata.** The symbol's `point` and `pip_size` (10 points on 3/5-digit quotes, otherwise 1)
  are now in every bars response and quote row. Missing metadata is sent as `null` and never
  guessed; the spread in pips becomes `null` then too.
- **Per-bar spread.** MT5's own per-bar spread, in points, is kept as `spread_points`. It's
  `null` when the record has none, and always `null` for archived bars.
- **Live OHLC checks** (`ohlc_issues`), the same rules the archive store already applies:
  - **Issue codes:** `non_finite_price`, `non_positive_price`, `high_below_open_close_or_low`,
    `low_above_open_close_or_high`, `negative_tick_volume`.
  - **Non-finite prices** are sent as `null`: JSON can't carry NaN or Infinity, and browsers
    reject them.
  - **Nothing corrected:** no value is altered or rounded.
- **Duplicates and ordering** (`check_bars`):
  - **Order:** bars are processed in the order received. Any bar dated earlier than one already
    received is flagged `out_of_order`, and the output is sorted by time.
  - **One bar per open time:** for a duplicated time, the first is kept and flagged
    `duplicate_timestamp`. Each further copy goes to `rejected_bars`, with its values as received
    and `identical_to_kept`.
- **No silent dropping:** every received bar is either returned or listed in `rejected_bars`.
  An anomaly no longer fails the whole request.
- **Each bar's verdict:** `valid` (no issues) and `issues` (the codes).
- **Strict on purpose:** an out-of-order or kept duplicate bar is marked invalid, so later
  stages can refuse it. T1 only reports; how to use invalid bars is decided at eligibility (T4).
- **Request summary:** `quality` = `{bars_received, bars_returned, invalid_bars,
  duplicates_rejected, out_of_order}`.
- **Closure evidence per bar:**
  - `forming`: the interval hasn't ended.
  - `tick_observed`: ended, and a server tick at or after the close was seen.
  - `clock_only`: ended only because the clock is past the close plus the 60 s grace.
  - `unknown`: no close time could be established (DST gap or fold, or archived bars).

  As agreed, closure says the interval has ended; it doesn't prove the feed covered the whole
  candle.
- **Left alone:** the server-clock refusal of future timestamps still fails the whole read, since
  it's a clock problem, not a property of one bar.

## 3. API contract changes and backward compatibility

**Bars response:** new fields `point`, `pip_size`, `rejected_bars`, `quality`.
**Each bar:** new fields `spread_points`, `closure`, `valid`, `issues`.
**Each quote row:** new fields `point`, `pip_size`.

**Backward compatibility:**
- **Additive only:** no existing field was removed, renamed or retyped.
- **Bar order:** bars stay sorted by open time, one per open time, as before.
- **Normal prices unchanged:** finite OHLC values are passed through as received.
- **Collector path unchanged:** `get_bars()` gives identical output and still raises on duplicate
  or out-of-order times. Tested.

**Behaviour changes:**
- **Anomalies no longer fail the request.** Previously a duplicate or out-of-order timestamp
  failed the whole bars request (`mt5_error`). Now the request succeeds and reports the bars
  concerned.
- **Non-finite prices arrive as `null`.** Previously a NaN or Infinity price would have produced a
  response browsers couldn't parse; it now arrives as `null` and is flagged.
- **Missing metadata is `null`.** Previously missing digits/point raised an error and made the
  symbol unavailable; now it's reported as `null`.

**Frontend:** it keeps working with both old and new responses. Candles with a missing price
aren't drawn, but are counted in the chart's "N invalid" badge. Zoom, pan, price scale, Go Live
and automatic refresh are unchanged (§5).

## 4. New tests

**`tests/test_bar_checks.py` (25 tests):**
- **Valid and invalid OHLC:** consistent bars pass unchanged, and 9 kinds of inconsistent values
  are flagged without being altered or dropped.
- **Non-finite prices:** they become `null`, so the response is valid strict JSON.
- **Duplicates:** one bar kept, the others reported (identical and differing copies).
- **Out-of-order:** sorted and flagged.
- **Empty input:** an empty bar list gives an empty result.
- **Adapter:** `read_bars` keeps MT5's order, duplicates and spread; the collector's `get_bars`
  keeps its schema and strictness.
- **Live service:**
  - **Combined anomalies** (inverted candle, NaN, swapped pair, duplicate) are reported without
    failing the request, with exact `quality` counts.
  - **Closure evidence:** forming, tick_observed and clock_only, checked bar by bar against the
    rule.
  - **Metadata:** point, pip size and spread for EURUSD and USDJPY, in bars and quotes; missing
    metadata gives `null` with data still LIVE.
  - **MT5 answers:** an empty answer is an empty LIVE result; a `None` answer is UNAVAILABLE.
- **Archived bars:** closure `unknown`, checks applied.
- **API compatibility:** every pre-T1 field and type is still present; no `NaN` or `Infinity`
  in the body.
- **Read-only:** only read-only MT5 calls are made.

**`tests/test_browser_smoke.py`:** `test_invalid_and_unplottable_candles_are_reported_not_drawn_or_fatal`.
A NaN candle, an inconsistent candle and a duplicate time arrive:
- the chart stays LIVE and shows "4 invalid";
- the NaN candle isn't drawn;
- candles are still drawn;
- zoom, pan and Go Live still work.

**Results:** `tests/test_bar_checks.py`: 25 passed (3.13, verbose run). The browser test passed within the 50-test browser run (3.11).

## 5. Full test results

| Command | Result |
|---|---|
| Baseline before changes: `.venv311\Scripts\python.exe -m pytest -q -p no:cacheprovider` | 309 passed |
| `.venv311\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.11.8) | **335 passed**, 0 skipped, 0 failed |
| `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.13.3) | **335 passed**, 0 skipped, 0 failed |
| `tests/test_browser_smoke.py -v` (3.11.8) | **50 passed**: automatic refresh, zoom, pan, price-axis scaling, Go Live and the new invalid-candle test |

The difference from the baseline is exactly the 26 new tests (309 + 25 + 1). No existing test
was changed: the 85 existing live-market, remediation, worker and server-clock tests passed
unchanged after the backend changes, which is the backward-compatibility check for current
consumers.

## 6. Ruff, formatting and JavaScript

| Command | Result |
|---|---|
| `python -m ruff check --no-cache .` | All checks passed |
| `python -m ruff format --check --no-cache .` | 72 files already formatted |
| `node --check src/johanna/web/app.js` (v23.9.0) | OK |

## 7. Data preservation

- **`data/` unchanged:** SHA-256, size and modification time of all 11 readable files under
  `data/`, including `johanna.sqlite3`, were identical before and after T1, and the file count
  is still 12.
- **Lock file:** `data/johanna.lock` (held by your backend) has the same size and modification
  time.
- **Your backend wasn't stopped, restarted or contacted:** its three processes (PID 29232,
  started 11:34:26, and its two workers from 11:34:32) are identical before and after.
- **Isolated tests:** every test used temporary directories and its own ports.

## 8. MT5 read-only verification

- **No trading calls:** `grep -rnE "order_send|order_check|order_calc|positions_get|
  positions_total|orders_get|history_orders|history_deals|TRADE_ACTION|\.login\(" src/
  scripts/` found **no matches** (exit status 1).
- **No new MT5 calls:** the new `read_bars()` uses only `copy_rates_from_pos`, already used
  before.
- **Read-only test:** `test_only_read_only_mt5_calls_are_used` checks every call against the
  read-only set.
- **No terminal or account use:** no step of this task connected to the real terminal or account.
  All tests use the fake MT5.

## 9. Limitations and open points

- **No feed-continuity evidence.** `tick_observed` proves only that the server moved past the
  candle's close. Continuity and gap classification belong to T3.
- **Strict invalid marking.** Out-of-order and kept duplicate bars are marked invalid even when
  their prices are fine. Whether eligibility (T4) should treat ordering anomalies differently from
  value errors is open.
- **Duplicate choice.** For differing duplicates the first-received bar is kept; MT5 gives no way
  to know which is right. Both are visible (kept, flagged, plus the rejected entry).
- **No per-bar pip spread.** `spread_points` is in points; pips need `pip_size`. No per-bar
  spread in pips was added.
- **Archived bars** have no point, pip size or spread (not stored), and their closure is always
  `unknown`.
- **Unaffected by design:** the quotes path wasn't changed beyond `point`/`pip_size` and the
  null-safe spread.
- **Not verified live.** I didn't check this against the real terminal (not part of T1). Your
  running backend serves the previous code until it's restarted.
