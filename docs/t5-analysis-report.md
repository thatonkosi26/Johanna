# T5: deterministic indicator and market-structure analysis

Date: 2026-11-12 · Claude Code (Opus 5.5) · Working tree only, **nothing committed** · Stopped
after T5 (T5.1 / T6 not started) · No live verification; no claim of trading readiness or
profitability; no signals

## 1. Files and responsibilities

| File | Responsibility |
|---|---|
| `src/johanna/analysis/indicators.py` | **New:** `ema_series`, `rsi_series`, `true_ranges`, `atr_series`, `adx_series`, `seed_weight`, `updates_to_converge` |
| `src/johanna/analysis/structure.py` | **New:** `find_swings` (with confirmation), `cluster_levels`, `support_resistance` |
| `src/johanna/analysis/volatility.py` | **New:** `percentile_rank`, `classify`, `volatility_regime` |
| `src/johanna/analysis/engine.py` | **New:** `analyse_window` (the validation gate and output contract), `AnalysisParameters` / `DEFAULT_PARAMETERS` |
| `tests/test_indicators.py` | **New:** 16 tests |
| `tests/test_structure_volatility.py` | **New:** 24 tests |
| `tests/test_analysis_engine.py` | **New:** 12 tests |
| `docs/t5-analysis-report.md` | This report |

**Not changed:** existing modules (`sessions`, `continuity`, `snapshot`, `window` and `__init__`
verified byte-identical with `cmp`), existing tests, API, dashboard, collectors and `data/`.

**Boundaries:** all new code is pure; there's no I/O, clock reading, MT5, adapter, collector,
live-market or network import. The T2 import-ban test globs every `analysis/*.py` and passes.
Nothing retrieves data: the engine takes an already-built snapshot and its T4/T4.1 validation.

## 2. Mathematical definitions (0-based candle index `i`, period `p`, chronological order)

| Indicator | Definition | First value | Minimum candles |
|---|---|---|---|
| EMA (20, 50, 200) | `alpha = 2/(p+1)`; seed = SMA of closes 0..p−1; `EMA_i = alpha·C_i + (1−alpha)·EMA_{i−1}` | `i = p−1` | `p` |
| RSI 14 (Wilder) | gains/losses of changes from `i = 1`; initial averages = mean of changes 1..p; then `avg = (avg·(p−1) + x)/p`; `RSI = 100 − 100/(1 + G/L)` | `i = p` | `p + 1` |
| ATR 14 (Wilder) | `TR_i = max(H−L, \|H−C_{i−1}\|, \|L−C_{i−1}\|)` from `i = 1`; seed = mean TR 1..p; then `(ATR·(p−1) + TR)/p` | `i = p` | `p + 1` |
| ADX 14 (Wilder) | `up = H_i−H_{i−1}`, `down = L_{i−1}−L_i`; `+DM = up` if `up > down` and `up > 0`, else 0; `−DM` mirrored (equal movement gives 0 to both). Wilder sums at `i = p` = plain sums over 1..p, then `S = S − S/p + x`. `±DI = 100·DM_S/TR_S`; `DX = 100·\|+DI−−DI\|/(+DI + −DI)`; ADX at `i = 2p−1` = mean DX p..2p−1, then `(ADX·(p−1) + DX)/p` | DX `i = p`; ADX `i = 2p−1` | DX `p+1`; ADX `2p` (28) |

**Edge-case conventions:**
- **RSI:** with zero average loss and some gains, RSI = 100; with zero average gain and some
  losses, RSI = 0; with both zero (flat prices), RSI = **50**.
- **ADX:** if the smoothed TR is 0 (no range at all), then +DI = −DI = 0; if +DI + −DI = 0, then
  DX = 0.
- **ATR:** the first candle has no true range, because there's no previous close.

## 3. Warm-up vs convergence

- **Warm-up** is the observation count before the first value exists (table above).
- **Convergence is a seed-weight proxy.** After `n` recursive updates the seed's remaining weight
  is `(1 − alpha)^n`, with `alpha = 2/(p+1)` for EMA and `alpha = 1/p` for Wilder smoothing.
  - **Converged** only when the proxy is below the approved **1 %** tolerance.
  - **What it measures:** seed influence on the smoother, **not** the indicator's output error.
    For RSI and ADX, which are non-linear in smoothed values, it's explicitly labelled a
    stabilisation proxy.

**Observations required for convergence:**

| Indicator | Warm-up (first value) | Updates needed (seed weight < 1 %) | Total candles |
|---|---|---|---|
| EMA20 | 20 | 47 | **67** |
| EMA50 | 50 | 116 | **166** |
| EMA200 | 200 | 461 | **661** |
| RSI14 | 15 | 63 | 78 |
| ATR14 | 15 | 63 | 78 |
| ADX14 | both stages must pass: directional-movement smoothing (78) and ADX smoothing (63 updates after index 27) | **91** |

- **Total = warm-up + updates.** The EMA's first value is the SMA seed at candle `p`; each later
  candle is one recursive update. The condition is strict (`< 1 %`), so EMA20 is not settled at
  66 candles (`(19/21)^46 ≈ 1.001 %`) and is settled at 67 (`(19/21)^47 ≈ 0.906 %`); likewise
  EMA50 at 165 / 166 and EMA200 at 660 / 661.
- **Correction (T5.1):** this table originally gave 66 and 165 for EMA20 and EMA50. The
  implementation always reported 67 and 166; only the report was wrong.

**Statuses per component:**
- `insufficient_history`: no value;
- `not_converged`: value shown, not trusted;
- `available`.

**Evidence per indicator:** period, latest value, timestamp, observations,
`warmup.observations_required` / `satisfied`, and `convergence` (alpha, updates, seed weight,
tolerance, converged, observations required, note). History is never silently reduced.

## 4. Swings and no-lookahead

- **k = 2.** A swing high is a candle whose high is **strictly greater** than the 2 preceding
  highs and **greater than or equal** to the 2 following highs. Swing lows are mirrored.
- **Tie rule:** on an equal-price plateau only the **first** candle qualifies, because later
  plateau candles aren't strictly above their predecessor. So there's one pivot per plateau and
  no duplicates (tested with 2-candle, separated-equal and long plateaus, and flat prices).
- **Confirmation:** a pivot at `i` is confirmed only once candle `i + 2` has closed.
  `confirmed_at_utc` is that candle's close time (open + timeframe), always after `pivot_t`.
- **Pending pivots:** those in the last 2 positions are reported as `pending`, with
  `confirmed_at_utc = null` and the number of candles still needed. They're never used.
- **No-lookahead (tested):**
  - appending or altering future candles never changes an already-confirmed pivot or its
    confirmation time;
  - a pending pivot can only be confirmed later, at the confirming candle's close;
  - a shorter, earlier window confirms a subset of the same pivots at the same times.
- **Reported per pivot:** type, price, pivot time, index, left and right candle times,
  confirming candle, confirmation time and status.
- **Eligibility:** the input candles are the T4/T4.1-validated window.

## 5. Support and resistance

- **Sources:** support comes from **confirmed** swing lows, resistance from **confirmed** swing
  highs. They're clustered separately and never mixed.
- **Tolerance:** `0.25 × ATR reference`, where the reference is the **latest ATR14 of the
  window** (`atr_reference_t` reported).
- **Method:** sort pivots by (price, pivot time) and walk upwards. A pivot joins the current
  cluster while `price − cluster's lowest price ≤ tolerance` (inclusive), otherwise it starts a
  new cluster, so a cluster spans at most the tolerance.
- **Per level:**
  - representative level = mean of member prices, plus low and high;
  - **strength = count of contributing confirmed pivots** (not a probability);
  - first and latest contributing confirmation times;
  - every constituent pivot.
- **No leakage:** a level is only as available as its pivots' confirmations.
- **Status:**
  - `insufficient_history` when ATR is missing or there are no confirmed pivots, stated per side;
  - `not_converged` when ATR isn't converged; the levels are still shown, but untrusted.
- **Boundary tested:** with ATR 1.0, 1.25 − 1.0 = 0.25 joins and 1.2500001 doesn't.
- **Not produced:** no entry, stop-loss or take-profit levels.

## 6. Volatility regime

- **Current:** the latest ATR14.
- **Reference:** the **100 ATR values immediately preceding** it. It excludes the current value
  and never includes anything later.
- **Percentile, mid-rank (handles ties):** `100 · (#below + 0.5 · #equal) / 100`.
- **Classification:** `< 25` low; **`25 ≤ p ≤ 75` normal (both boundaries inclusive)**; `> 75`
  high.
- **Only converged ATR values qualify** (seed proxy below 1 %), so the first usable current value
  needs `14 + 63 + 100 + 1 = 178` candles.
  - **Fewer than 101 ATR values:** `insufficient_history`.
  - **Enough values but not all converged:** `not_converged`, with a diagnostic percentile and no
    regime. No regime is invented.
- **Tested boundaries:** 24.5 low, exactly 25 normal, exactly 75 normal, 75.5 high; ties (all
  equal gives 50); preceding-only reference.

## 7. Output schema and validation gate

`analyse_window(snapshot, validation, *, analysed_at_utc=None, parameters=DEFAULT_PARAMETERS)`
returns JSON-safe data:
- `analysis_version`, `snapshot_id`, `symbol`, `broker_symbol`, `timeframe`;
- `analysed_at_utc`: given, otherwise the validation's evaluation time; never the wall clock;
- `validation`: the **original T4/T4.1 result, unchanged**, plus `validation_verdict` and
  `validation_reasons`;
- `status`, `reasons`, `parameters`, `window`, `component_statuses`, `trusted_components`;
- `indicators` {ema[3], rsi, atr, adx with +DI/−DI/DX}, `structure` {swings,
  support_resistance} and `volatility`, each with its own status and `trusted` flag;
- `note`: no signal, recommendation, opportunity, live verification or trading readiness implied.

**Overall statuses:**

| Window or input | Status | Behaviour |
|---|---|---|
| Rejected | `rejected` | Nothing calculated (`indicators`, `structure`, `volatility` = null); original reasons kept; no candles dropped |
| Provisional | `provisional` | Calculated for diagnosis; **nothing trusted**; can never become accepted |
| Accepted, all components available | `available` | Trusted per component |
| Accepted, some components short of history or convergence | `partial` | Only `available` components trusted |
| Snapshot and validation mismatch, missing window candle, non-numeric prices, non-mapping input | `invalid_input` | Reasons listed |
| Timeframe other than H1, H4 or D1 | `unsupported_timeframe` | |

**Unchanged policies:** raw candles are never modified (input immutability tested), missing
candles aren't filled, and Sunday D1 candles are neither merged nor excluded automatically
(`excluded_opens` stays caller-controlled in T4). There's no score.

## 8. Tests and reference verification

**52 new tests:** `test_indicators.py` 16, `test_structure_volatility.py` 24,
`test_analysis_engine.py` 12.

**Reference values.** No external TA library is installed (TA-Lib and pandas aren't
dependencies, and none was added), so verification uses two independent methods:
- **Hand-worked examples, arithmetic in the test comments:**
  - **EMA p=3:** `[2, 3, 4, 5]`, and `11, 10.5, 12.25`.
  - **RSI p=3:** `100 − 100/3`, then `100 − 100/6`.
  - **True ranges:** `2, 2, 4, 2`, including gap up and gap down.
  - **ATR p=3:** `8/3`, `22/9`.
  - **ADX p=2:** +DI `50, 150/7, 10`; −DI `0, 200/7, 80/3`; DX `100, 100/7, 500/11`; ADX
    `400/7, 3950/77`.
- **Independent exact-arithmetic re-derivations** (`fractions.Fraction`), using different
  formulations: EMA as a closed-form weighted sum; Wilder averages via `a + (x − a)/p`; ADX from
  Wilder averages rather than Wilder sums.
- **Convergence constants:** checked against the analytical boundary, `(199/201)^460 > 1 % >
  (199/201)^461`.
- **EMA convergence boundaries (T5.1):** for EMA20/50/200 at 66/67, 165/166 and 660/661
  candles, the seed weight is measured from `ema_series` itself (seed of 1s, then 0s), and the
  engine is run on validated windows to check `observations_required`, `converged`, `status`
  and `trusted` on each side of the boundary. Equality with the tolerance is tested as not
  converged.

**Also covered:** edge cases (flat, zero true range, equal / positive-only / negative-only
movement, zero average gain or loss), initialisation boundaries, insufficient history, all
gate states, invalid input, unsupported timeframe, strict JSON, 10–30 identical repeated runs,
input immutability, swing no-lookahead and the absence of signal vocabulary.

**Mutation checks.** I applied ten deliberate defects to the real modules, one at a time; each
was caught by the suite, and all modules were restored byte-identical (verified with `cmp`):

| Defect | Tests failing |
|---|---|
| Provisional window treated as trusted | 1 |
| Rejected window calculated anyway | 1 |
| Pivot confirmation at the pivot's own time | 2 |
| Plateau tie rule removed | 4 |
| Support/resistance boundary made exclusive | 1 |
| EMA alpha set to 1/period | 2 |
| RSI flat sequence returns 100 | 1 |
| ADX equal movement credited as +DM | 1 |
| Current ATR included in its own reference | 8 |
| Unconverged EMA reported as available | 1 |

| Command | Result |
|---|---|
| Baseline (end of T4.1) | 499 passed on 3.11 and 3.13; 50 browser |
| New T5 tests, verbose (3.13.3) | **52 passed** |
| `.venv311\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.11.8) | **551 passed**, 0 skipped, 0 failed |
| `.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider -rs` (3.13.3) | **551 passed**, 0 skipped, 0 failed |
| `tests/test_browser_smoke.py -v` (3.11.8) | **50 passed** in 186 s |
| `python -m ruff check --no-cache .` | All checks passed |
| `python -m ruff format --check --no-cache .` | 95 files already formatted |
| `node --check src/johanna/web/app.js` (v23.9.0) | OK (no JavaScript changed) |

551 = 499 + 52. Complete logs of every run were kept; none contains a failure, error, skip or
hang.

## 9. Performance

- **`analyse_window` on the 1,000-candle maximum (H1, Python 3.11):** median **37.9 ms**, worst
  82.3 ms over 20 runs. For comparison, T4 `validate_window` on the same window takes 183.5 ms.
- **No extra overhead:** every series is computed once in a single pass. There's no caching or
  persistence, and the 1,000-candle cap is unchanged.

## 10. T1–T4.1 policies unchanged

- **Modules unchanged:** `snapshot.py` (T2), `sessions.py` and `continuity.py` (T3) and
  `window.py` (T4/T4.1) are byte-identical, and T1 code in `live_market.py` wasn't touched.
- **Tests unchanged:** no existing test was modified.
- **Validation as input only:** T5 reads the validation result and can't change its verdict or
  reasons.

## 11–13. Data, backend, MT5, execution and commits

- **`data/` untouched:** SHA-256, size and modification time of all 11 readable files under
  `data/` are identical before and after T5, and the file count is still 12. No T5 code or test
  touches the filesystem; fixtures are in-memory.
- **Backend not stopped, restarted or contacted:** its processes (PID 44532, started 13:52:24, and
  its two workers from 13:52:38) are identical before and after.
- **MT5 untouched:** no connection and no MT5 import or call in any new module.
- **No execution functionality:** the search for order and position functions
  (`order_send|order_check|order_calc|positions_get|positions_total|orders_get|history_orders|
  history_deals|TRADE_ACTION|\.login\(`) over `src/` and `scripts/` finds **no matches**.
- **Nothing was committed.**

## 14. Limitations and decisions for review

**Decisions I made, for you to confirm:**
- **ATR convention:** the first true range at candle 1 (no TR for candle 0). Some platforms use
  `H − L` for candle 0, which shifts early values slightly.
- **RSI on flat prices = 50.**
- **ADX convergence = both smoothing stages below 1 %.**
- **Support/resistance:** the tolerance rests on the latest ATR; clustering is greedy from the
  lowest price (span ≤ tolerance), with the level as the mean. Single-pivot clusters are kept,
  with strength 1.
- **Volatility regime:** uses only converged ATR values (178 candles minimum), and the reference
  excludes the current value.
- **Partial results:** accepted windows with some unconverged components report `partial`.

**Limitations:**
- **History cap:** EMA200 convergence needs 661 candles. On D1, six of the seven pairs have only
  824 candles (T0), about 688 without Sunday candles, so D1 EMA200 is only just reachable.
- **No external library cross-check:** reference values are hand-worked and independently
  re-derived, not compared against TA-Lib or a trading platform.
- **No live data:** T5 hasn't been run on live Exness data. T1/T2 live verification is still
  outstanding and needs your permission to restart the backend.
- **Not wired in:** T5 isn't connected to the API or dashboard, by scope.
- **Not trading signals:** none of these results is a trading signal, opportunity, risk
  assessment or recommendation.
