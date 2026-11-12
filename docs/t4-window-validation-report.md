# T4: analysis-window validation and rejection

Date: 2026-11-12 · Claude Code (Opus 5.5) · Working tree only, **nothing committed** · Stopped
after T4 (T5 and later not started) · No live verification, no claim of feed continuity or
trading readiness

## 1. Files changed

| File | Change |
|---|---|
| `src/johanna/analysis/window.py` | **New:** `validate_window()`, `D1_CONVENTION`, verdict and status constants, T1 issue-code groupings (`PRICE_ISSUES`, `TIMESTAMP_ISSUES`) |
| `tests/test_window_validation.py` | **New:** 48 tests (§7) |

**Not changed:** existing source files, the T1–T3 modules, tests, endpoints, dashboard, collectors
and `data/`. T4 isn't wired into the API or the dashboard.

## 2. Architecture and decisions

- **Location:** `johanna.analysis.window`, pure and deterministic.
- **Inputs:** a T2 snapshot (which carries T1's per-candle quality and closure and T1's
  `rejected` duplicates), T3's session configuration, and optional `peers` / `excluded_opens`,
  passed through to T3 unchanged.
- **Reuse, not re-implementation:**
  - T1 issue codes are read, not re-derived;
  - T2 eligibility is read, or re-evaluated with `evaluate_snapshot` when `analysed_at` is given;
  - continuity is T3's `analyse_continuity`, with all its classifications preserved.
- **No forbidden imports:** no MT5, adapters, collectors, live market or network code. The T2
  import-ban test covers `window.py` automatically.
- **Responsibilities:** T1 judges individual candles, T2 snapshot integrity and freshness, T3
  session continuity, and T4 the suitability of the complete window.
- **Window definition:** the newest `required_count + warmup_count` **closed** candles. The newest
  candle is left out only if it's still forming, and is reported in `excluded_candles`. If fewer
  are available, the window isn't shortened: completeness fails and every available candle is
  still checked.
- **Output:** each of six dimensions gets `status` (`pass` / `provisional` / `fail`), `reasons`
  (code, severity, detail, evidence) and `evidence`. The verdict is `accepted` / `provisional` /
  `rejected`, with every reason tagged by dimension. There are no scores.

## 3. Duplicate policy

Candles are grouped by open time across both the snapshot's candles and the duplicates T1 set
aside. The canonical candle is the one T1 kept, otherwise the first seen.

| Case | Definition | Policy | Effect |
|---|---|---|---|
| **Conflicting** | Any OHLC (`o`, `h`, `l`, `c`) differs | No candle is chosen; all values are reported (`policy: window_rejected`) | **rejected** |
| **Metadata differs** | OHLC identical, but tick volume (`v`) or `spread_points` differ | One canonical candle kept; the feed inconsistency is reported (`canonical_retained_metadata_differs`) | **provisional** |
| **Identical** | OHLC and metadata identical | One canonical candle kept (`canonical_retained`) | **provisional** (conservative: exact repeats are still a feed anomaly) |

- **What counts as a conflict:** differing tick volume or spread alone doesn't make candles price
  conflicts, but they're never treated as clean. OHLC disagreement is always a conflict.
- **Recorded per duplicate:** open time, occurrences, `ohlc_identical`, `metadata_identical`,
  every copy's values, and the policy applied.
- **Duplicates can't hide problems:**
  - every copy's price issues are checked;
  - a copy that differs in OHLC triggers the conflict rule anyway;
  - T1's `duplicate_timestamp` flag on the kept candle isn't counted as a price failure.
- **Outside the window:** duplicates there are reported (`duplicates_outside_window`) but don't
  affect the verdict.

## 4. Independent quality dimensions

| Dimension | Fails (reject) when | Provisional when | Evidence |
|---|---|---|---|
| `price` | Any window candle (including duplicate copies) has a T1 price code: `non_finite_price`, `non_positive_price`, `high_below_open_close_or_low`, `low_above_open_close_or_high`, `negative_tick_volume`; or lacks T1 quality data | — | Candles and codes |
| `timestamps` | Missing, malformed, timezone-naive or misaligned timestamp anywhere (its position can't be established); out-of-order arrival inside the window; conflicting duplicates | Identical duplicates; metadata-only duplicates | All duplicate records |
| `closure` | A non-newest candle is still forming | `clock_only` candles; unknown closure evidence | Excluded candles, tick-observed count |
| `continuity` | T3 `symbol_missing` or `unknown` gap inside the window | T3 `market_wide_unverified` gap | T3 verdict, gaps by class, every gap record, the T3 "not proof" note |
| `completeness` | No closed candles; fewer than `required_count` (`insufficient_history`); fewer than `required + warmup` (`insufficient_warmup`) | — | Expected, required, warm-up, available, in-window |
| `snapshot` | Snapshot `refused` (stored, or on re-evaluation at `analysed_at`, e.g. stale); missing `snapshot_id` / `timeframe` / `eligibility` / `bars`; unsupported timeframe; unknown state | Snapshot `provisional` (its reasons listed) | State, evaluation time, ages, bars' MT5 retrieval time, cache flag |

**Separations kept:**
- **Price, timestamps and continuity:** a well-formed candle that arrived out of order fails
  `timestamps` while `price` passes (tested).
- **Insufficient history vs invalid data:** a complete window with an invalid candle passes
  `completeness` and fails `price` (tested).

## 5. Window verdict rules

`rejected` if **any** dimension fails, otherwise `provisional` if **any** dimension is
provisional, otherwise `accepted`.

| Condition | Verdict |
|---|---|
| Conflicting duplicate timestamps | rejected |
| Invalid OHLC | rejected |
| Missing, malformed, timezone-naive or misaligned timestamp | rejected |
| Out-of-order arrival inside the window (conservative policy, documented) | rejected |
| Refused T2 snapshot, including stale when re-evaluated | rejected |
| Missing snapshot metadata | rejected |
| Insufficient history or warm-up; empty window | rejected |
| T3 `unknown` or `symbol_missing` gap | rejected |
| A forming candle that isn't the newest | rejected |
| T3 `market_wide_unverified` gap | provisional (never accepted as clean) |
| Provisional T2 snapshot | provisional (valid-looking candles can't lift it) |
| `clock_only` or unknown closure | provisional (the approved clock-only policy) |
| Identical or metadata-only duplicates | provisional |
| T3 `configured_closure`, `expected_no_tick` or `analytical_exclusion` gaps only | no effect (accepted if nothing else applies) |

Every result carries the note: *"Accepted means no validation rule found a reason to doubt this
window. It is not proof of continuous feed coverage, of price correctness, or of trading
readiness."*

## 6. Sunday D1 timestamp convention

**Evidence** (`docs/evidence/t0-exness-sessions/report.json`, preserved in T3):
- **All recorded D1 timestamps are 00:00Z:** EURUSD D1 2014-01-14T00:00:00Z … 2026-11-12T00:00:00Z;
  Sunday candles first and last 2014-01-19T00:00:00Z and 2026-11-08T00:00:00Z. GBPUSD and the
  other pairs run 2024-03-26 … 2026-11-12, with Sunday candles 2024-04-07 … 2026-11-08, all at
  00:00Z.
- **Sunday candles are partial days:** they hold a median of 3 (2–3) H1 candles, which matches
  21:05/22:05 UTC → midnight. A New York-close D1 convention couldn't produce a separate small
  Sunday candle.
- **Server time is UTC:** tick times matched UTC within seconds.

**Conclusion (`D1_CONVENTION.status = "observed"`):** an Exness D1 candle's timestamp is its
**open at 00:00 UTC of the calendar day**. That isn't the market reopening time: a Sunday D1
candle is timestamped Sunday 00:00 UTC, but trading starts at 17:05 New York (21:05 UTC in US
summer time, 22:05 UTC in winter).

**How T4 applies it:**
- **Grid check:** D1 candles must sit on the 00:00 UTC grid; others are rejected as
  `not_aligned_to_timeframe`. A D1 candle stamped at the reopen time is rejected (tested).
- **Expected Sunday candle:** T3 treats each D1 slot (00:00–24:00 UTC) as required if the schedule
  has any open time in it. That makes the Sunday slot required, while a Sunday made fully closed
  by a Monday holiday (2023-12-24) isn't.
- **Untouched:** raw Sunday candles are never changed or merged; exclusions stay caller-controlled
  (`excluded_opens`); no new exclusion policy was added.

**Unresolved** (kept in every D1 result's `d1_convention.unresolved`):
1. The preserved summary doesn't list every D1 timestamp. Alignment of all of them is inferred
   from the sample and the H1-per-D1 structure, not checked one by one.
2. EURUSD has 628 Sunday D1 candles against 665 Mondays since 2014, so about 37 weeks lack one.
   Monday holidays account for some; the rest aren't identified by the preserved evidence.
   **D1 continuity therefore isn't conclusively verified.**

## 7. Tests and results

**`tests/test_window_validation.py` (48 tests):**
- **Baseline:** a clean live window is accepted, all six dimensions are reported, and the result
  is strict JSON.
- **Duplicates:**
  - identical (canonical kept, recorded, provisional);
  - conflicting (rejected, both values reported, none chosen);
  - differing tick volume, and differing spread (not a price conflict; provisional);
  - three conflicts all reported;
  - duplicates inside the candle list itself (not misreported as out-of-order);
  - 50 identical runs.
- **OHLC:** each of the 5 T1 price codes rejects the window, with the timestamp dimension
  unaffected. Also: several invalid candles; valid mixed with invalid; an invalid candle behind an
  identical duplicate still rejects; out-of-order is a timestamp failure, not a price failure.
- **Timestamps:** missing, empty, malformed, timezone-naive and misaligned (5 cases); an
  out-of-order list.
- **Completeness:** exact count with the forming candle excluded; one short (`insufficient_history`);
  warm-up short (`insufficient_warmup`, window not shortened); empty; newest-closed selection;
  complete window with invalid data; invalid requested counts (4 cases).
- **Snapshot:** provisional and refused snapshots; stale on re-evaluation at analysis time; missing
  `eligibility`, missing `snapshot_id` and unsupported timeframe; deterministic verdicts; a window
  from the real `LiveMarketService` snapshot over the fake MT5.
- **Closure:** clock-only is provisional; a forming candle inside the window rejects.
- **Continuity:**
  - configured weekend closure accepted;
  - expected rollover break accepted;
  - unknown gap rejected;
  - symbol-specific gap rejected;
  - market-wide gap provisional;
  - the "not proof" note preserved.
- **Sunday D1:**
  - the convention and its unresolved points are reported;
  - the six sequences (Friday → Sunday, Sunday → Monday, Friday → Monday with Sunday missing, US
    fall-back, US spring-forward, Monday holiday);
  - explicit caller exclusion;
  - D1 candles stamped 21:00Z or 22:05Z rejected.

**Mutation check.** I temporarily weakened three rules in the real module, one at a time:
conflicts made provisional, unknown gaps tolerated, out-of-order ignored. The suite failed for
each (1, 2 and 2 failures). The module was then restored byte-identical (verified with `cmp`).

| Command | Result |
|---|---|
| Baseline (end of T3) | 411 passed on 3.11 and 3.13 |
| `tests/test_window_validation.py -v` (3.13.3) | **48 passed** |
| `.venv311\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.11.8) | **459 passed**, 0 skipped, 0 failed (3 min 58 s) |
| `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.13.3) | **459 passed**, 0 skipped, 0 failed (3 min 57 s) |

459 = 411 + 48: exactly the new tests, and no existing test was modified. Complete logs of every
run were kept this time, as you asked after T3. None contained a failure, error or hang.

## 8–9. Browser, Ruff, formatting and JavaScript

| Command | Result |
|---|---|
| `tests/test_browser_smoke.py -v` (3.11.8) | **50 passed** in 178 s (normal duration) |
| `python -m ruff check --no-cache .` | All checks passed |
| `python -m ruff format --check --no-cache .` | 85 files already formatted |
| `node --check src/johanna/web/app.js` (v23.9.0) | OK (no JavaScript was changed) |

## 10. Existing functionality preserved

- **Additive only:** T4 adds one module and its tests. No existing source or test file was
  modified.
- **Unchanged:** market-data endpoints, quotes, bars, snapshots, the chart, refresh, zoom, pan,
  price-axis scaling and Go Live.
- **Tests:** all pre-existing tests pass (§7).

## 11–13. Data, backend and MT5

- **`data/` untouched:** SHA-256, size **and** modification time of all 11 readable files under
  `data/` are identical before and after T4, and the file count is still 12. `data/johanna.lock`
  has the same size and modification time. T4 code and tests never open `data/`; fixtures are
  in-memory, and the one service test uses the in-memory fake.
- **Backend not stopped, restarted or contacted:** your backend's three processes (PID 44532,
  started 13:52:24, and its two workers from 13:52:38) are identical before and after.
- **MT5 untouched:** nothing connected to MT5. A search for order and position functions
  (`order_send|order_check|order_calc|positions_get|positions_total|orders_get|history_orders|
  history_deals|TRADE_ACTION|\.login\(`) over `src/` and `scripts/` finds **no matches**, and
  T4 adds no MT5 call of any kind.

## 14. Limitations and outstanding decisions

**Decisions for you to confirm** (I chose the conservative option in each case):
- **(a) Out-of-order:** an out-of-order arrival inside the window rejects it.
- **(b) Duplicates:** identical duplicates make the window provisional rather than accepted.
- **(c) Metadata-only duplicates:** these make the window provisional.
- **(d) `unknown` gaps:** these reject rather than make the window provisional.
- **(e) `market_wide_unverified` gaps:** these are provisional rather than rejecting.

**Limitations:**
- **No gap filling.** A window containing an unknown gap is rejected even if a longer window would
  satisfy the count; the window is never moved past a gap. Choosing a different window is the
  caller's decision.
- **Candles inside closures aren't flagged.** T3 and T4 detect *missing* candles, not candles that
  appear during a scheduled closure; the fake MT5 produces such candles and they pass. Whether an
  "unexpected candle during closure" check is wanted is undecided.
- **Archived bars can't be accepted.** Their snapshots are always refused, so their legacy time
  basis can never enter an analysis window.
- **D1 continuity not conclusively verified** (§6). The Sunday exclusion policy is still for the
  caller to supply.
- **Still outstanding:** live verification of T1/T2 against the real Exness terminal, which needs
  your permission to restart the backend.
- **Not claimed:** feed continuity, price correctness or trading readiness.
