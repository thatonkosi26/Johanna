# T6 design: market conditions and trend-continuation detection (revision 2.5)

Date: 2026-11-12 · Claude Code (Opus 5.5) · **Design only.** Only this document was changed. No
source, test, data or other documentation was touched, nothing was committed, and T6
implementation is not authorised. Revision 2 applies review corrections A–E. Revision 2.1 adds
two clarifications: overlapping support/resistance zones (§3.4) and TC6 clearance cases
(§4.1). Revisions 2.2–2.5 add the verified T5 support/resistance contract, input
provenance checks, the production entry point (§1.5), how provenance metadata is produced
(§1.6), the raw-interval versus expanded-zone distinction (§2.3, §3.4, §4.1) and limitation
clarifications. Every change
is listed in §12.

**In short:**
- **Scope:** T6 does three things and nothing else:
  1. classifies market conditions;
  2. evaluates **one** setup, trend continuation, as a bullish or bearish mirror-image
     checklist;
  3. makes an evidence-based decision: `NO_TRADE`, `MARKET_CONDITION` or `TRADE_CANDIDATE`.

  Support/resistance reactions, range strategies and breakouts are deferred.
- **Default is `NO_TRADE`.** Invalid, provisional, undetermined, conflicting or unavailable
  inputs always produce `NO_TRADE`. `MARKET_CONDITION` is used only when every required input
  was available and the market was understood, but the setup didn't qualify.
- **No score, no probability, `actionable: false` always.** A `TRADE_CANDIDATE` means "the
  predefined analytical conditions hold". It isn't a recommendation and doesn't authorise any
  trade. The thresholds are experimental defaults, not validated parameters.

---

## 1. Architecture and module responsibilities

### 1.1 Foundation (as read from the source)

| Fact in the code | Consequence for T6 |
|---|---|
| T5 `analyse_window` reports only the **latest** value of each indicator, each with a `trusted` flag (trusted = window `accepted` **and** component `available`) | Per-candle values (EMA20_j, EMA50_j, ATR_j) must be recomputed (§1.3). T6 never re-derives trust for T5's latest values. |
| An accepted T4 window contains only closed candles. A forming newest candle is excluded; a forming candle inside the window, or any `clock_only` closure, makes the window at best `provisional` | Every candle in an accepted window has closed. The last candle's close is a safe as-of point. |
| Pivots: `confirmed` (with `confirmed_at_utc` = close of candle `i + k`) and `pending` | T6 uses `confirmed` only. |
| S/R levels: `low`, `high`, `level`, `strength` (pivot count), latest confirmation; tolerance = 0.25 × latest ATR | Used for location and clearance |
| Volatility: `regime` low / normal / high, from converged ATR only | Reused unchanged; no new thresholds |

### 1.2 Pipeline and modules

```
public  evaluate_market_snapshot(snapshot, …)      production entry point (§1.5)
   │  deep copy of snapshot, then, in sequence and from that copy only:
   ├─► T4/T4.1 validate_window(copy) ─► T5 analyse_window(copy, validation) ─┐
   └──────────────► private _evaluate(copy, validation, analysis, provenance_mode="constructed")

public  evaluate_market(snapshot, validation, analysis)   supplied inputs (internal use, tests)
   └──────────────► private _evaluate(snapshot, validation, analysis, provenance_mode="supplied")

_evaluate     provenance P1–P10 (§1.4) → gate → decision table (§8) → output (§8.2)
        decision.py
          └ setups.py      trend continuation checklist + vetoes (§4–§7)
              └ conditions.py   market-condition dimensions (§3); no knowledge of setups
                  └ features.py      derived per-candle measurements, validity (§2)
                      └ T5 pure functions: ema_series, atr_series, seed_weight; T5 outputs
```

**All modules are pure:** no network, broker, MT5, filesystem or clock access, and no imports
from the live-market or collector code.

**One-way dependencies:** decision → setups → conditions → features → T5. Conditions can't
import setups.

| Module | Responsibility |
|---|---|
| `features.py` | Per-candle series, validity flags, pullback distances, slope, swing sequence, nearest levels, consistency guard |
| `conditions.py` | Trend regime, directional bias, trend strength, volatility, location, momentum, swing structure. Each has supporting, contradicting and unavailable evidence. |
| `setups.py` | Required-input availability check; bullish and bearish checklist; vetoes; cautions |
| `decision.py` | Public: `evaluate_market_snapshot(...)`, the production entry point (§1.5), and `evaluate_market(...)` for separately supplied inputs (internal use and tests). Private: `_evaluate(..., provenance_mode)`, which holds the provenance checks, gate, decision table and output assembly (§1.6). Also `DecisionParameters`. |

### 1.3 Recomputation and consistency guard

- **Recomputed series:** T6 recomputes EMA20, EMA50, EMA200 and ATR14 over the validated
  window's closes, highs and lows, using the **same T5 functions**.
- **Guard:** the recomputed latest values must equal T5's reported values exactly. Same code
  and same inputs give bit-identical floats; any mismatch gives `NO_TRADE` through rule R1.
- **Taken from T5 directly, not recomputed:** ADX, +DI and −DI; RSI; the S/R levels and
  pivots; the volatility regime.
- **The guard isn't provenance evidence.** Matching indicator values never count as proof
  that the inputs belong together. Provenance is established separately (§1.4) and is checked
  first.

### 1.4 Input provenance checks (rule R1)

**Verified against source and tests (revision 2.3).**

**Snapshot (T2):**
- `build_snapshot` copies a **caller-supplied** `snapshot_id`, plus `symbol`, `broker_symbol`,
  `timeframe` and `bars.items`.
- In production, `LiveMarketService.snapshot` passes `uuid.uuid4().hex`.
  `test_snapshot.py` asserts the id is 32 characters, and that a cached re-read gets a **new**
  id.

**Validation (T4):** `validate_window` returns:
- `snapshot_id`, `symbol` and `timeframe`, all copied from the snapshot;
- `window.{count, first_t, last_t, candles}` and `verdict`;
- **no** `broker_symbol`.

A missing `snapshot_id` is `rejected` (`test_missing_snapshot_metadata_is_rejected`).

**Analysis (T5):** `analyse_window` returns:
- `snapshot_id`, `symbol`, `broker_symbol` and `timeframe`, copied from the **snapshot**;
- `validation`: an unchanged copy of the validation result;
- `validation_verdict`, `status` and `window.{count, first_t, last_t}`, derived from the
  validation's candle list;
- `analysis_version` and `parameters`.

**T5's own input check:**
- it compares `snapshot_id` and `timeframe` between snapshot and validation, and requires every
  window candle to be in the snapshot;
- it does **not** compare `symbol`;
- tested: `test_invalid_input_and_unsupported_timeframe` (id mismatch, missing candle) and
  `test_accepted_long_window_is_fully_available_and_trusted` (`result["validation"] ==
  validation`).

**Checks T6 performs before anything else.** Every check is an exact equality or presence
test. Any failure gives `invalid_input` and **R1 `NO_TRADE`**, with each failed check named.

| # | Check |
|---|---|
| P1 | `snapshot.snapshot_id`, `validation.snapshot_id` and `analysis.snapshot_id` are all **non-empty strings** and all equal |
| P2 | `symbol` is a non-empty string and equal across all three inputs |
| P3 | `timeframe` is equal across all three inputs and is one of H1, H4, D1 |
| P4 | `analysis.broker_symbol == snapshot.broker_symbol`, a non-empty string. Validation doesn't carry it, so it's bound through P1 (see the gaps below). |
| P5 | `analysis.validation` **deep-equals** `validation`. This binds the verdict, reasons, dimensions and window that T5 consumed to the validation T6 was given. |
| P6 | `analysis.validation_verdict == validation.verdict.state`. Also, `analysis.status` is consistent with that verdict: `accepted` with `available` / `partial`; `provisional` with `provisional`; `rejected` with `rejected`. |
| P7 | Window integrity within the validation: `window.count == len(window.candles)`; `first_t == candles[0]`; `last_t == candles[-1]`; candles strictly increasing |
| P8 | `analysis.window == {count, first_t, last_t}` of `validation.window` |
| P9 | Every `validation.window.candles` time is present in `snapshot.bars.items`. The canonical (first) item per time is used, as in T4 and T5. |
| P10 | Version and parameter compatibility: `analysis.analysis_version == 1`; `validation.window_version` and `snapshot.snapshot_version` are the versions T6 was written against; `analysis.parameters` equals the T5 parameters T6 expects (EMA 20/50/200, RSI, ATR and ADX 14, k = 2, S/R 0.25, volatility 100 / 25 / 75, tolerance 0.01) |

**Order of checks:** P1–P10 run before the gate's T5-status checks and before the consistency
guard. A provenance failure is reported as such, even if indicator values happen to match.

**Residual gaps (not resolvable with existing fields):**
1. **The id is unique only by convention.** `build_snapshot` accepts any string. Production
   uses uuid4, but the test helpers reuse `"snap-1"` for many different snapshots. T6 can check
   that ids are **equal**, but not that they're **unique**.
2. **Candle prices aren't bound.** No field binds the OHLC values to the id or the window.
   Suppose a snapshot were altered in place after T4/T5 (same id, same times, different prices).
   P1–P10 would pass, and only the consistency guard might notice, which isn't proof.
3. **Validation lacks `broker_symbol`.** It's verified only through P1 and P4.

**These gaps apply only to the separately supplied path,** `evaluate_market(snapshot,
validation, analysis)`. The production entry point (§1.5) builds the validation and the
analysis itself, from one copy of the snapshot, so the three inputs can't come from different
snapshots or different versions of one snapshot. That path reports
`content_binding: "by_construction"`; the separately supplied path reports `"not_verified"`.
Neither path adds hashes, schema changes or persistence, and T2–T5 are unchanged (U10,
resolved).

### 1.5 Production entry point: provenance by construction (U10, option a)

```
evaluate_market_snapshot(snapshot, *, required_count=None, analysed_at=None,
                         excluded_opens=(), peers=None, parameters=DEFAULT_DECISION_PARAMETERS)
```

**Sequence, all pure; the caller supplies `analysed_at`, and T6 never reads a clock:**
1. **Copy:** `work = copy.deepcopy(snapshot)`. The caller's object isn't read again.
2. **Validate:** `validation = validate_window(work, required_count=…, analysed_at=analysed_at,
   excluded_opens=excluded_opens, peers=peers)`. These are T4's existing parameters and
   defaults, unchanged.
   - **Default `required_count`:** the T6 minimum derived from the parameters, 661 with the
     defaults, since below that T6 always gives `NO_TRADE` (§6.2).
   - **No warm-up added:** `warmup_count = 0`. The 661 already includes the EMA200 warm-up.
3. **Analyse:** `analysis = analyse_window(work, validation)`, the existing T5 call with
   default parameters.
4. **Evaluate:** `_evaluate(work, validation, analysis, provenance_mode="constructed")`
   (§1.6). This is the same private function `evaluate_market` uses, with the same P1–P10,
   gate and decision table.
   - **P1–P10 still run here** and must pass by construction. A failure would indicate a
     defect and gives R1 `NO_TRADE`, like any other provenance failure.
5. **Output:** the same contract (§8.2). `_evaluate` writes `inputs.provenance.mode:
   "constructed"` and `content_binding: "by_construction"` from the mode it was given; the
   wrapper adds nothing afterwards.

**What the deep copy does, and doesn't do:**
- **Does:** all three downstream inputs are derived from one in-memory object that nothing
  else holds a reference to. Changes the caller makes to their own snapshot, during or after
  the call, can't make T4, T5 and T6 see different content. That's the provenance guarantee:
  the validation and the analysis were *constructed from* that snapshot.
- **Doesn't:** it isn't cryptographic integrity protection. It doesn't prove the snapshot is
  genuine, that it came from MT5, or that it wasn't altered *before* the call. It doesn't make
  the output tamper-evident afterwards either. Snapshot authenticity still rests on T2.

**The separately supplied path stays:** `evaluate_market(snapshot, validation, analysis)` is
kept for controlled internal use and testing:
- it calls `_evaluate(..., provenance_mode="supplied")`, so it reports `mode: "supplied"` and
  `content_binding: "not_verified"`;
- the P1–P10 checks are its only provenance evidence;
- production callers should use `evaluate_market_snapshot`.

**Not connected to anything:** neither function is wired to the API, dashboard, application
lifecycle or MT5 in T6.

### 1.6 How provenance metadata is produced

**One private function produces every result:** `_evaluate(snapshot, validation, analysis, *,
provenance_mode)`. It runs P1–P10 (§1.4), the gate and the decision table (§8.1), and
assembles the output (§8.2), including `inputs.provenance`. The two public entry points
differ only in how they obtain the three inputs and which mode they pass:

| Public entry point | How it gets the inputs | `provenance_mode` it passes | `inputs.provenance` written by `_evaluate` |
|---|---|---|---|
| `evaluate_market_snapshot(snapshot, …)` | Builds them itself from one deep copy (§1.5) | `"constructed"` (fixed in code) | `mode: "constructed"`, `content_binding: "by_construction"` |
| `evaluate_market(snapshot, validation, analysis)` | Supplied by the caller | `"supplied"` (fixed in code) | `mode: "supplied"`, `content_binding: "not_verified"` |

**Rules:**
1. **Content binding follows the mode, by fixed mapping:** `constructed` gives
   `by_construction`; `supplied` gives `not_verified`. No other combination is possible, and
   no other mode value is accepted. An unknown mode is a programming error: it raises, and
   never produces a result.
2. **Callers can't choose the mode:** neither public function has a provenance parameter.
   A caller of `evaluate_market` can't label supplied inputs as constructed, and the
   production path never labels its inputs as unverified.
3. **The metadata is written in one place:** inside `_evaluate`, together with the rest of the
   output. No wrapper edits the output afterwards.
4. **P1–P10 are identical for both modes:** the same checks, run before anything else, with
   R1 `NO_TRADE` on any failure. The mode never relaxes or skips a check. `by_construction`
   adds to P1–P10; it doesn't replace them.
5. **Privacy is a convention:** `_evaluate` isn't exported from `johanna.analysis`, and tests
   go through the public functions. The leading underscore prevents accidental mislabelling
   through the public API; it isn't a security boundary (consistent with §1.5: the copy isn't
   cryptographic protection either).

## 2. Feature definitions and input validity

### 2.1 Notation

- **Candles:** the validated window holds candles `0 … n−1`, oldest first.
- **Price reference:** `C = close_{n−1}`, the last closed candle.
- **As-of time:** `as_of_utc` = open time of candle `n−1` + timeframe, i.e. its close.
- **Defaults:** `L = slope_lookback = 5`; `P = pullback_lookback = 5`.
- **Pullback lookback:** `J = {n−P, …, n−1}`, the five most recent closed candles, **including
  the latest**.

### 2.2 Validity of a per-candle value

A recomputed value `X_j` is **valid** only if all four hold:
1. it isn't `None` (warm-up satisfied at `j`);
2. it's finite;
3. it's **converged at `j`**: `seed_weight(alpha, j − first_index) < 0.01`, the same strict
   proxy and tolerance as T5;
4. for ATR only, `ATR_j > 0`.

Validity is evaluated **per candle**. A candle near the end of the window isn't valid just
because the latest value is.

### 2.3 Features

| Feature | Definition | Valid when |
|---|---|---|
| `C`, `EMA20`, `EMA50`, `EMA200`, `ATR` (latest) | Index `n−1`. EMA200, ATR, ADX and RSI must also be `trusted` in T5. | T5 `trusted` and the consistency guard passes |
| `slope50` | `(EMA50_{n−1} − EMA50_{n−1−L}) / ATR_{n−1}` | Both EMA50 values valid; latest ATR trusted and > 0 |
| `d_j` (bullish) | `abs(low_j − EMA20_j) / ATR_j`, for `j ∈ J` | `EMA20_j` and `ATR_j` valid for **every** `j ∈ J` (§5.2) |
| `d_j` (bearish) | `abs(high_j − EMA20_j) / ATR_j`, for `j ∈ J` | Same |
| `structure_break_j` (bullish) | `close_j < EMA50_j` (strict) | `EMA50_j` valid for every `j ∈ J` |
| `structure_break_j` (bearish) | `close_j > EMA50_j` (strict) | Same |
| `swing_sequence` | §7.1 | — |
| `nearest_resistance` (bullish clearance) | Uses the **raw T5 level interval** `[low, high]`. Among T5 resistance levels with `strength ≥ 2` and `high ≥ C`: the one with the lowest `low`. Distance `max(0, low − C) / ATR`, which is 0 only when `C` is inside the raw interval. The evidence cases are in §4.1. | T5 S/R `trusted`; latest ATR trusted |
| `nearest_support` (bearish clearance) | Raw T5 level interval. Among support levels with `strength ≥ 2` and `low ≤ C`: the one with the highest `high`. Distance `max(0, C − high) / ATR`, which is 0 only when `C` is inside the raw interval. The evidence cases are in §4.1. | Same |
| `zones` (location only, §3.4) | Every qualifying level (`strength ≥ 2`) as an **expanded location zone** `[low − tol, high + tol]`, `tol` = T5 S/R tolerance. Not used by TC6. | T5 S/R `trusted` |

**Terminology, used consistently below:**
- **Raw T5 level interval:** `[low, high]` exactly as T5 reports it. It's what TC6 uses.
- **Expanded location zone:** `[low − tol, high + tol]`. It's used only by the location
  dimension.

**History needed, with the defaults:**

| Requirement | Candles |
|---|---|
| EMA200 converged at `n−1` | **661** |
| EMA50 converged at `n−1−L` | 166 + 5 = 171 |
| EMA50 valid for all of `J` | 166 + 4 = 170 |
| ATR valid for all of `J` | 78 + 4 = 82 |
| EMA20 valid for all of `J` | 67 + 4 = 71 |

**Any window with at least 661 candles satisfies all of these.** With fewer than 661, EMA200
is unavailable, which gives `NO_TRADE` (§6).

## 3. Market-condition classification

**Dimension shape:**
- every dimension returns `state`, `role` (`core` or `context`), `trusted`, `supporting`,
  `contradicting` and `unavailable` (each entry an input with a reason);
- every comparison has a stated inclusive or exclusive side.

**Roles:**
- **Core** dimensions (trend regime, directional bias) decide whether the market is
  understood.
- **Context** dimensions inform, caution or (swing structure only, §7) conflict. Their
  unavailability is reported but never fabricated into a conflict.

### 3.1 Trend regime (core): `trending` | `ranging` | `transitional` | `undetermined`

| State | Conditions |
|---|---|
| `trending` | `ADX ≥ 25` **and** `abs(slope50) > 0.25` |
| `ranging` | `ADX ≤ 20` **and** `abs(slope50) ≤ 0.25` |
| `transitional` | All inputs available, neither row matched. The mismatch is listed as contradicting evidence. |
| `undetermined` | ADX untrusted, or `slope50` invalid |

### 3.2 Directional bias (core): `bullish` | `bearish` | `neutral` | `conflicting` | `undetermined`

**Four core votes:**

| ID | Bullish vote | Bearish vote |
|---|---|---|
| `price_vs_ema50` | `C > EMA50` | `C < EMA50` |
| `ema20_vs_ema50` | `EMA20 > EMA50` | `EMA20 < EMA50` |
| `di_balance` | `+DI > −DI` | `−DI > +DI` |
| `ema50_slope` | `slope50 > 0.25` | `slope50 < −0.25` |

Equality votes neither.

**Resolution (no weighting, no majority):**
- **`bullish`** or **`bearish`:** all four votes agree.
- **`conflicting`:** at least one bullish and at least one bearish vote.
- **`neutral`:** otherwise.
- **`undetermined`:** any vote's input unavailable.

### 3.3 Context dimensions

| Dimension | States | Rule |
|---|---|---|
| Trend strength | weak / moderate / strong / extended | ADX `< 20` / `20 ≤ x < 25` / `25 ≤ x ≤ 40` / `> 40` |
| Volatility | low / normal / high / undetermined | T5 `regime`, unchanged |
| Momentum | overbought / oversold / neutral / undetermined | RSI `≥ 70` / `≤ 30` / otherwise |
| Location | A **list** of applicable states, plus zone relationships | §3.4 |
| Swing structure | higher_highs_higher_lows / lower_highs_lower_lows / mixed / unavailable | §7.1 |

### 3.4 Location: multiple states and overlapping zones

Location is **context only**:
- no TC check, veto or decision rule reads it;
- TC6 computes its own clearance from the **raw T5 level intervals** (§4.1), never from the
  expanded location zones, so location can neither qualify nor reject a setup;
- location never suppresses a zone to produce a single label.

In this section "zone" always means an **expanded location zone**.

**Zones:**
- **Which levels:** every qualifying T5 level (`strength ≥ 2`), support and resistance alike.
- **Band (expanded location zone):** `[low − tol, high + tol]`, where `tol` is the T5 S/R
  tolerance (0.25 × latest ATR). This is the existing "at" band; no new threshold.
- **Ids:** `S1, S2, …` for support and `R1, R2, …` for resistance, numbered by ascending `low`,
  then `high`, then earliest pivot time. The numbering is deterministic.
- **Each zone reports:**
  - side, level, low, high and strength;
  - latest confirmation time;
  - `contains_price` (`C` inside the band, both edges inclusive);
  - `distance_atr`, from `C` to the nearest edge of the **expanded** location zone (`0` when
    the expanded zone contains `C`). It's a location value, not the TC6 clearance distance;
  - `position`: `contains` / `above` / `below`.

**States** (`states` is a list; every applicable state is included, in this fixed order):

| State | Applies when |
|---|---|
| `at_support` | At least one qualifying support zone contains `C` |
| `at_resistance` | At least one qualifying resistance zone contains `C` |
| `between_levels` | At least one qualifying support zone lies wholly below `C` **and** at least one qualifying resistance zone lies wholly above `C` |
| `above_all_resistance` | Qualifying resistance zones exist, and all of them lie wholly below `C` |
| `below_all_support` | Qualifying support zones exist, and all of them lie wholly above `C` |
| `no_levels` | No qualifying zones at all. It's the only state in this case. |
| `undetermined` | T5 S/R or the latest ATR is untrusted. It's the only state in this case. |

`at_support` and `at_resistance` can both apply, and either can appear together with
`between_levels`.

**Relationships, reported explicitly instead of being resolved:**
- **`zones_containing_price`:** every zone id containing `C`, in id order. Several zones of
  either side can contain `C` at once.
- **`overlaps`:** every pair of zones whose bands intersect, as `[id_a, id_b]` in id order,
  same-side or opposite-side. Edges touching count as intersecting (inclusive). Example:
  `[["R2", "S3"]]`.
- **`nearest_below` / `nearest_above`:** among zones *not* containing `C`, the closest zone
  wholly below and the closest wholly above, with `distance_atr`. They're reported even when
  `C` is inside another zone. That covers "inside one zone and close to another" without a
  separate "nearby" threshold: the distance is given, not judged.

**Examples:**

| Situation | `states` | Relationships |
|---|---|---|
| `C` inside support S2 and resistance R1, whose bands overlap | `["at_support", "at_resistance"]` | `zones_containing_price: ["R1", "S2"]`, `overlaps: [["R1", "S2"]]` |
| `C` inside S2; S1 is wholly below; R1 is 0.4 ATR above | `["at_support", "between_levels"]` | `nearest_above: {"id": "R1", "distance_atr": 0.4}` |
| `C` inside R3 only; no qualifying support | `["at_resistance"]` | `nearest_below: null` |
| No qualifying levels (only strength-1 levels exist) | `["no_levels"]` | `zones: []`; the strength-1 levels are listed under `non_qualifying_levels` (informational) |

**Location and TC6 can legitimately differ.** This is intended, because location-zone
expansion must not influence TC6.

Example: a qualifying resistance with raw interval `[1.08500, 1.08520]`, ATR 0.00100, so
`tol` = 0.00025 and the expanded location zone is `[1.08475, 1.08545]`. No other qualifying
resistance exists above.

| `C` | Location | TC6 (bullish) |
|---|---|---|
| 1.08510 (inside the raw interval) | `at_resistance` | Level selected (`high ≥ C`); distance **0**; `price_inside_zone: true`; `resistance_clearance_insufficient` → `fail` |
| 1.08490 (below the raw interval, inside the expanded zone) | `at_resistance` | Level selected; distance `(1.08500 − 1.08490) / 0.00100` = **0.1**, not 0; `price_inside_zone: false`; `resistance_clearance_insufficient` → `fail` |
| 1.08530 (above the raw interval, inside the expanded zone) | `at_resistance` | Level **not** selected (`high` 1.08520 < `C`); `no_qualifying_resistance_above` → `pass` |

**What this shows:**
- Being inside an expanded location zone doesn't make TC6 clearance zero, and doesn't decide
  TC6 at all.
- With the default parameters, any selected level whose expanded zone contains `C` is at most
  `tol` = 0.25 ATR away, which is below the 1.0 ATR threshold, so TC6 fails on its own raw
  distance.

## 4. Trend continuation: bullish and bearish checklists

**Which checklist runs:**
- **Direction:** the setup direction is the directional bias. Bias `bullish` runs the bullish
  checklist; `bearish` runs the bearish one.
- **Neutral bias:** direction `null`. TC2 fails, and TC3–TC6 are `not_applicable`.
- **Each check returns** `pass`, `fail`, `unavailable` or `not_applicable`, never a bare
  boolean.

| ID | Bullish | Bearish (exact mirror) |
|---|---|---|
| `TC1_regime` | regime = `trending` | regime = `trending` |
| `TC2_bias` | bias = `bullish` | bias = `bearish` |
| `TC3_long_term` | `C > EMA200` **and** `EMA50 > EMA200` | `C < EMA200` **and** `EMA50 < EMA200` |
| `TC4_pullback` | some `j ∈ J` has `abs(low_j − EMA20_j) / ATR_j ≤ 0.25` | some `j ∈ J` has `abs(high_j − EMA20_j) / ATR_j ≤ 0.25` |
| `TC5_resumption` | `C > EMA20` | `C < EMA20` |
| `TC6_clearance` | nearest qualifying resistance distance `≥ 1.0` ATR, or no qualifying resistance at or above `C` (§4.1) | nearest qualifying support distance `≥ 1.0` ATR, or no qualifying support at or below `C` (§4.1) |

**Vetoes (each rejects the setup even if TC1–TC6 pass):**

| ID | Bullish | Bearish |
|---|---|---|
| `V1_ema50_structure_break` | some `j ∈ J` has `close_j < EMA50_j` | some `j ∈ J` has `close_j > EMA50_j` |
| `V2_swing_context_conflict` | swing structure = `lower_highs_lower_lows` (§7) | swing structure = `higher_highs_higher_lows` |

**Cautions** are reported, never veto and never add support:
- volatility `high` (§7.2);
- momentum overbought (bullish) or oversold (bearish);
- trend strength `extended`.

**Setup status:**
- **`not_evaluable`:** any required input (§6.1) is unavailable.
- **`qualified`:** all six checks pass and no veto triggered.
- **`not_qualified`:** otherwise.

### 4.1 TC6 clearance: evidence cases

**Verified T5 contract** (revision 2.2, checked against `structure.support_resistance`,
`engine.analyse_window`, their tests and a read-only probe of the real code). T6 relies only on
these facts:

| T5 situation | `status` | `trusted` | `support` / `resistance` | Reason fields |
|---|---|---|---|---|
| ATR missing or ≤ 0 (also when the window is too short for swings, since ATR then can't exist either) | `insufficient_history` | false | both `[]` | `reason: "no ATR reference"`; no side reasons |
| ATR present, no confirmed pivots on either side | `insufficient_history` | false | both `[]` | `support_reason: "no confirmed swing lows"`, `resistance_reason: "no confirmed swing highs"`; no `reason` |
| Levels exist but ATR isn't converged (engine downgrade) | `not_converged` | false | **populated** | `reason: "ATR reference not converged"` |
| Window `provisional` | `available` (or as above) | false | **populated** | engine-level `reasons` |
| Confirmed lows only (or highs only), accepted, ATR converged | `available` | **true** | one side populated, the other `[]` | `resistance_reason: "no confirmed swing highs"` (or `support_reason: "no confirmed swing lows"`) |
| Levels all of strength 1, accepted, ATR converged | `available` | **true** | populated, every `strength` 1 | none. T5 has no strength filter: `min_level_strength` is applied by T6 only. |

**Consequences for T6:**
1. **Trust comes from `trusted` only:** never from `status`, and never from the lists being
   empty. Untrusted lists can be empty or fully populated, and neither is evidence either way.
2. **A trusted result always has at least one non-empty side.** T5 returns
   `insufficient_history` when both sides are empty, so "no levels on either side" is never
   trusted. It goes to case 0 and R5 `NO_TRADE`, never to a pass.
3. **Case 1 needs all three of:**
   - `trusted: true`;
   - an empty list on the relevant side;
   - T5's side reason present (`resistance_reason` / `support_reason`).

   If a trusted result has an empty side **without** that reason, the T5 contract is violated,
   giving `invalid_input` and R1 `NO_TRADE`.

   **What case 1 means:** the analysed window contains no confirmed swing high (or low). It
   doesn't mean no resistance exists outside the window.

TC6 records exactly one `evidence_id`, evaluated in this order (first match wins).

**TC6 uses the raw T5 level interval `[low, high]` only:**
- **Bullish resistance distance:** `max(0, low − C) / ATR`.
- **Bearish support distance (exact mirror):** support levels at or below `C`, distance
  `max(0, C − high) / ATR`.
- **Zero distance:** only when `C` lies inside the raw interval.
- **No expansion:** the location expansion by `tol` (§3.4) is never applied here.

Each TC6 record also carries the T5 `status`, `trusted` and reason fields it relied on.

| # | Bullish `evidence_id` | Bearish `evidence_id` | Condition | TC6 result |
|---|---|---|---|---|
| 0 | `support_resistance_untrusted` | `support_resistance_untrusted` | T5 S/R `trusted` is false (any of the first four contract rows above, whatever the lists contain), or latest ATR untrusted | `unavailable`, giving R5 `NO_TRADE` |
| 1 | `no_resistance_levels` | `no_support_levels` | T5 S/R trusted, the resistance (bearish: support) list is empty, and T5's `resistance_reason` (bearish: `support_reason`) is present | `pass` |
| 2 | `no_qualifying_resistance` | `no_qualifying_support` | Resistance (bearish: support) levels exist on **that side**, but **none** has `strength ≥ 2` | `pass` |
| 3 | `no_qualifying_resistance_above` | `no_qualifying_support_below` | Qualifying levels exist, but none is at or above `C` (bullish: all have `high < C`) | `pass` |
| 4 | `resistance_clearance_insufficient` | `support_clearance_insufficient` | Nearest qualifying level distance `< 1.0` ATR, including `0` when `C` is inside its raw T5 level interval | `fail` |
| 5 | `resistance_clearance_sufficient` | `support_clearance_sufficient` | Nearest qualifying level distance `≥ 1.0` ATR | `pass` |

**Rules:**
- **Pass when no qualifying opposing level exists (cases 1–3):** the current experimental
  behaviour, kept unchanged. The evidence id shows *why* nothing was in the way.
  - **What a pass in cases 1–3 means:** no qualifying opposing level was **identified in the
    analysed history**, i.e. the validated window's confirmed pivots.
  - **What it doesn't mean:** that no resistance (or support) exists in the market. Levels
    older than the window, levels not formed as confirmed k = 2 pivots, and levels with fewer
    than two pivots aren't seen.
  - **Where this is stated:** the TC6 record carries this as `detail`, and §11 lists it as a
    limitation.
- **Strength-1 levels are only reported:** in case 2 or 3, the nearest non-qualifying
  (strength-1) level on the relevant side is reported as informational evidence
  (`nearest_non_qualifying_level`, with its distance). It never changes the result, so the
  minimum strength of 2 is unchanged.
- **What each record contains:**
  - the selected level (when there is one), with its raw `low` and `high`;
  - the distance in ATR, the threshold 1.0 and the comparison `>=`;
  - `price_inside_zone`: `true` exactly when `C` is inside that level's **raw T5 interval**
    (`low ≤ C ≤ high`), which is when the distance is 0. It says nothing about the expanded
    location zone.
- **Only opposing levels count:** bullish clearance looks only at resistance levels; bearish
  only at support. A support zone above `C`, a "broken" support, isn't treated as resistance.
  There's no role reversal (see U9).
- **Independent of location:** location's expanded zones (§3.4) don't feed TC6. Both read the
  same T5 levels independently: TC6 uses the raw intervals, location the expanded zones. Their
  results can differ (§3.4 example).

## 5. Corrected pullback rule (Correction A)

### 5.1 Formula

| Direction | Distance | Qualifies when |
|---|---|---|
| Bullish | `d_j = abs(Low_j − EMA20_j) / ATR_j` | `d_j ≤ 0.25` (inclusive) |
| Bearish | `d_j = abs(High_j − EMA20_j) / ATR_j` | `d_j ≤ 0.25` (inclusive) |

`TC4_pullback` passes when **at least one** candle in `J` qualifies.

### 5.2 Rules

1. **Closed candles only:** `J` contains only closed candles of the accepted window. T6
   re-checks that none is `forming` or `clock_only`.
2. **Same-candle values:** `EMA20_j` and `ATR_j` are the values **at candle `j`**, never the
   latest EMA20 or ATR.
3. **Validity:** `EMA20_j` and `ATR_j` must be valid (§2.2) for **every** candle in `J`. If any
   is missing, non-finite, unconverged, or (for ATR) zero or negative:
   - TC4 is `unavailable`;
   - the setup is `not_evaluable`;
   - the outcome is `NO_TRADE` (rule R5).

   An invalid ATR can never produce a qualifying pullback: no division happens and no result
   is fabricated.
4. **Lookback:** five closed candles, including the latest.
5. **EMA50 veto stays separate:** the structural-break veto (`V1`) is unchanged and evaluated
   independently over the same `J`. TC4 doesn't consider closes.
6. **Mirror symmetry:** reflecting prices (`p → 2M − p`) swaps lows and highs and turns every
   bullish evaluation into the identical bearish one. This is property-tested.

### 5.3 Why absolute distance

The previous one-sided rule, `low_j − EMA20_j ≤ 0.25 ATR`, held for **any** low below EMA20,
however deep. A candle that plunged 3 ATR through EMA20 counted as a "touch", which describes a
sharp reversal, not a pullback.

The absolute rule gives a **band** of ±0.25 ATR around that candle's EMA20:
- **inside the band:** a low that comes near the EMA20 or pierces it slightly qualifies;
- **outside the band:** a low that stays well above it (no pullback), or goes well through it,
  doesn't qualify.

Dividing by `ATR_j` uses that candle's own volatility, so the band means the same thing
throughout the lookback.

### 5.4 Pullback examples (synthetic values)

| Case | Candle values | `d_j` | Result |
|---|---|---|---|
| **Bullish valid** (EURUSD H1) | low 1.08470, EMA20_j 1.08460, ATR_j 0.00108 | 0.00010 / 0.00108 = **0.093** | qualifies |
| **Bullish too deep** | low 1.08390, EMA20_j 1.08460, ATR_j 0.00110 | 0.00070 / 0.00110 = **0.636** | **fails.** The old one-sided rule would have passed it (−0.00070 ≤ 0.000275). |
| **Bearish valid** (GBPUSD H1) | high 1.26820, EMA20_j 1.26840, ATR_j 0.00120 | 0.00020 / 0.00120 = **0.167** | qualifies |
| **Bearish too far above** | high 1.26920, EMA20_j 1.26840, ATR_j 0.00120 | 0.00080 / 0.00120 = **0.667** | **fails.** The old mirrored rule would have passed it. |
| **Boundary** | diff 0.25, ATR_j 1.0 (exact in binary) | 0.25 | qualifies (inclusive); 0.2500001 fails |
| **Invalid ATR** | ATR_j = 0 on one candle in `J` | — | TC4 `unavailable` → `NO_TRADE` (R5) |

**"Too deep" in full:**
- **TC4 fails:** that candle's close (1.08450) stayed above EMA50_j (1.08300), so V1 isn't
  triggered, and no other candle in `J` came within 0.25 ATR of its EMA20.
- **Outcome:** with every input available and the market understood, the setup is
  `not_qualified`, which gives `MARKET_CONDITION` with `failed_conditions: ["TC4_pullback"]`.

## 6. EMA200 requirement and unavailable inputs (Corrections B and E)

### 6.1 Required setup inputs

These are direction-independent, so they're checked once, before the checklist runs:
- `EMA20_j`, `EMA50_j` and `ATR_j` valid for every `j ∈ J`;
- `EMA50_{n−1−L}` valid;
- **EMA200 latest trusted** (converged, accepted window, consistency guard passed);
- latest ATR trusted and > 0;
- T5 support/resistance `trusted` (the flag, not `status` or the lists).
  - **One empty side is allowed:** TC6 case 1 (§4.1).
  - **Both sides empty is never trusted** in T5 (`insufficient_history`), so it gives R5
    `NO_TRADE`.

If any is unavailable:
- the setup is `not_evaluable`;
- the outcome is `NO_TRADE` (R5), with every missing input listed under
  `opportunity.required_inputs.unavailable`;
- the determinable checks are still shown for transparency, but they can't turn the outcome
  into `MARKET_CONDITION`.

Context inputs (swing structure, volatility, momentum, trend strength) are **not** required
setup inputs.

### 6.2 EMA200 is mandatory (`require_ema200_alignment = true`)

- **Requirement:** bullish needs `C > EMA200` and `EMA50 > EMA200`; bearish needs the exact
  inverse. Both comparisons are strict, so equality fails.
- **Unavailable EMA200 means no qualification:** if EMA200 isn't converged or isn't trusted,
  the setup can't qualify and the outcome is `NO_TRADE`. There's no fallback to a shorter
  EMA and no skipping the check.
- **The parameter stays configurable:** setting it to `false` would be an explicit,
  documented choice, not a default. The design doesn't propose it.
- **Effect on short histories:** any validated window under **661 candles** always produces
  `NO_TRADE` for T6.
  - **H1 and H4:** the cap is 1,000 candles, so the caller must request at least 661.
  - **D1:** T0 found 824 candles for six of the seven pairs, about 688 if Sunday candles are
    excluded (caller-controlled in T4). D1 is just reachable, with little margin.
  - **D1 risk:** a shorter request, or a window that isn't accepted, gives `NO_TRADE`.

## 7. Context conflicts versus missing context (Correction D)

### 7.1 Swing sequence (from T5 confirmed pivots with `confirmed_at_utc ≤ as_of_utc`; pending ignored)

`H_prev`, `H_last` are the two most recent confirmed swing highs; `L_prev`, `L_last` are the two
most recent confirmed swing lows.

**Visible in the output:** the swing-structure dimension, and V2's evidence, always list the
pivots used. For each of `H_prev`, `H_last`, `L_prev` and `L_last`: type, price, `pivot_t`
and `confirmed_at_utc`. This applies whatever the state, including `mixed`. When the state is
`unavailable`, the list holds the pivots that were found, with the count still needed.

**No age limit (U6, retained):** the pivots may be anywhere in the window. For this initial
experiment the timestamps are shown so their age can be judged, but age doesn't affect the
classification.

| State | Rule |
|---|---|
| `higher_highs_higher_lows` | `H_last > H_prev` **and** `L_last > L_prev` |
| `lower_highs_lower_lows` | `H_last < H_prev` **and** `L_last < L_prev` |
| `mixed` | Both pairs exist, neither row matches. This includes equal prices, expanding (HH + LL) and contracting (LH + HL) structure. |
| `unavailable` | Fewer than two confirmed highs **or** fewer than two confirmed lows |

### 7.2 What counts as a genuine directional conflict

**Exactly one context condition is a genuine conflict:** a **complete opposite swing
sequence**:
- `lower_highs_lower_lows` against a bullish setup;
- `higher_highs_higher_lows` against a bearish setup.

**When it fires** (with `veto_on_context_conflict = true`):
- `V2_swing_context_conflict` triggers;
- the decision is **`NO_TRADE` through rule R4**, conflicting directional evidence, not
  `MARKET_CONDITION`, because confirmed structure contradicts the core bias.

**Never counted as a conflict:**

| Situation | Treatment |
|---|---|
| Swing structure `unavailable` (too few confirmed pivots) | Listed under `unavailable_context` and in `limitations`. Doesn't veto and doesn't block a candidate. |
| Swing structure `mixed` | Context that is neither supportive nor opposing; reported as such |
| Bias `neutral` with any swing structure | No setup direction, so nothing to conflict with |
| Volatility high, RSI overbought or oversold, ADX extended | Cautions only (§4) |
| Price relative to EMA200 | Not context. It's the required check TC3; a wrong side **fails** TC3. |

## 8. Decision table and output contract (Correction E)

### 8.1 Decision table (evaluated in order; first match wins)

| Rule | Situation | Outcome |
|---|---|---|
| R1 | Invalid input, failed provenance check (P1–P10, §1.4), T5 `rejected`, unsupported timeframe, or failed consistency check | `NO_TRADE` |
| R2 | Validation `provisional` (T5 status `provisional`) | `NO_TRADE`. Conditions shown for diagnosis, `trusted: false`. |
| R3 | Core market conditions undetermined (trend regime or directional bias `undetermined`) | `NO_TRADE` |
| R4 | Conflicting directional evidence: bias `conflicting`, **or** `V2_swing_context_conflict` triggered | `NO_TRADE` |
| R5 | A required setup input (§6.1) unavailable or untrusted | `NO_TRADE` |
| R6 | Market understood (R1–R5 didn't apply) and the setup is `not_qualified` (a check failed or `V1` triggered) | `MARKET_CONDITION` |
| R7 | All six checks pass, no veto | `TRADE_CANDIDATE` |

**Notes:**
- **R6 and R7 are mutually exclusive** once R1–R5 haven't matched.
- **`MARKET_CONDITION` can't hide missing data:** reaching it requires every required input to
  have been available. Unavailable *context* is still listed in the output.
- **Neutral bias and transitional or ranging regimes** count as understood conditions, so they
  lead to `MARKET_CONDITION` (R6), not `NO_TRADE`.

### 8.2 Output contract

```
{
  "decision_version": 1,
  "symbol", "broker_symbol", "timeframe",
  "as_of_utc",                 // close of the last window candle
  "evaluated_at_utc",          // = T5 analysed_at_utc; never read from a clock
  "inputs": {"snapshot_id", "analysis_version", "analysis_status", "validation_verdict",
             "window": {"count", "first_t", "last_t"},
             "price_reference": {"source": "last_closed_candle_close", "t", "value"},
             "provenance": {"mode": "constructed" | "supplied",          // written by _evaluate, §1.6
                            "passed", "checks": [{"id": "P1".."P10", "passed", "detail"}],
                            "content_binding": "by_construction" | "not_verified"},
                            // only the pairs constructed/by_construction and supplied/not_verified occur
             "consistency_check": {"passed", "compared": [...]}},
  "gate": {"passed", "reasons": [...]},
  "market_condition": {
    "trusted", "understood",                       // understood = R1–R3 did not apply
    "summary": {"<dimension>": "<state>", ..., "location": ["<state>", ...]},   // location is a list (§3.4)
    "dimensions": {"<dimension>": {"state", "role", "trusted",
                                   "supporting": [Evidence], "contradicting": [Evidence],
                                   "unavailable": [{"input", "reason"}]}}
  },
  "opportunity": {
    "setup": "trend_continuation", "direction": "bullish" | "bearish" | null,
    "status": "qualified" | "not_qualified" | "not_evaluable",
    "required_inputs": {"all_available", "unavailable": [{"input", "reason"}]},
    "checklist": [Check],               // TC1..TC6
    "failed_conditions": ["<id>", ...],
    "vetoes": [Veto],                   // V1, V2 with triggered true/false
    "triggered_vetoes": ["<id>", ...],
    "cautions": [Evidence],
    "unavailable_context": [{"input", "reason"}]
  },
  "decision": {"outcome": "NO_TRADE" | "MARKET_CONDITION" | "TRADE_CANDIDATE",
               "rule": "R1".."R7", "reasons": [...], "actionable": false},
  "parameters": {...}, "limitations": [...],
  "note": "Analytical classification only. Not a recommendation, instruction or authorisation to trade."
}
Evidence = {"id", "observed", "threshold", "comparison", "source", "t", "detail"}
Check    = Evidence + {"result": "pass" | "fail" | "unavailable" | "not_applicable",
                       "evidence_id"}   // e.g. TC6: one of the §4.1 identifiers
Veto     = Evidence + {"triggered": true | false | null}   // null when its inputs are unavailable
```

**What the contract excludes:** no score or probability field, and no prices for opening,
protecting or closing a position. Directional words are `bullish` / `bearish` only.

### 8.3 Example: `TRADE_CANDIDATE` with high-volatility caution (EURUSD H1, synthetic)

**Why it qualifies:**
- **Trend and bias:** ADX 29.4 and slope50 +0.73 ATR give `trending`; all four votes are
  bullish.
- **Long-term:** `C` 1.08540 > EMA200 1.07720, and EMA50 1.08310 > EMA200.
- **Pullback and resumption:** candle `n−3` touched with `d = 0.093`, and `C` closed above
  EMA20 1.08495.
- **Clearance:** the nearest qualifying resistance is 2.0 ATR away.
- **Context:** swing structure `higher_highs_higher_lows`.

**Volatility is `high`** (T5 percentile 88.0). That's recorded as a caution; it neither vetoes
nor supports the setup.

```json
{
  "decision_version": 1, "symbol": "EURUSD", "timeframe": "H1",
  "as_of_utc": "2026-11-11T13:00:00Z", "evaluated_at_utc": "2026-11-11T13:00:04Z",
  "inputs": {"analysis_status": "available", "validation_verdict": "accepted",
             "window": {"count": 700, "first_t": "2026-10-01T08:00:00Z", "last_t": "2026-11-11T12:00:00Z"},
             "provenance": {"mode": "constructed", "passed": true, "content_binding": "by_construction"},
             "price_reference": {"source": "last_closed_candle_close", "t": "2026-11-11T12:00:00Z", "value": 1.0854},
             "consistency_check": {"passed": true, "compared": ["EMA20", "EMA50", "EMA200", "ATR14"]}},
  "gate": {"passed": true, "reasons": []},
  "market_condition": {
    "trusted": true, "understood": true,
    "summary": {"trend_regime": "trending", "directional_bias": "bullish", "trend_strength": "strong",
                "volatility": "high", "momentum": "neutral", "location": ["between_levels"],
                "swing_structure": "higher_highs_higher_lows"},
    "dimensions": {
      "trend_regime": {"state": "trending", "role": "core", "trusted": true,
        "supporting": [{"id": "adx_at_or_above_trend_min", "observed": 29.4, "threshold": 25.0, "comparison": ">="},
                       {"id": "ema50_slope_not_flat", "observed": 0.73, "threshold": 0.25, "comparison": "abs >"}],
        "contradicting": [], "unavailable": []},
      "directional_bias": {"state": "bullish", "role": "core", "trusted": true,
        "supporting": [{"id": "price_vs_ema50", "observed": 1.0854, "threshold": 1.0831, "comparison": ">"},
                       {"id": "ema20_vs_ema50", "observed": 1.08495, "threshold": 1.0831, "comparison": ">"},
                       {"id": "di_balance", "observed": 27.1, "threshold": 14.8, "comparison": "+DI > -DI"},
                       {"id": "ema50_slope", "observed": 0.73, "threshold": 0.25, "comparison": ">"}],
        "contradicting": [], "unavailable": []},
      "volatility": {"state": "high", "role": "context", "trusted": true,
        "supporting": [], "contradicting": [], "unavailable": [],
        "detail": "T5 regime; ATR percentile 88.0 (T5 thresholds 25/75, unchanged)"}
    }
  },
  "opportunity": {
    "setup": "trend_continuation", "direction": "bullish", "status": "qualified",
    "required_inputs": {"all_available": true, "unavailable": []},
    "checklist": [
      {"id": "TC1_regime", "result": "pass", "observed": "trending"},
      {"id": "TC2_bias", "result": "pass", "observed": "bullish"},
      {"id": "TC3_long_term", "result": "pass", "observed": {"close": 1.0854, "ema50": 1.0831, "ema200": 1.0772}, "comparison": "close > ema200 and ema50 > ema200"},
      {"id": "TC4_pullback", "result": "pass", "observed": 0.093, "threshold": 0.25, "comparison": "abs(low_j - ema20_j) / atr_j <=", "t": "2026-11-11T10:00:00Z",
       "detail": "low 1.08470, ema20_j 1.08460, atr_j 0.00108"},
      {"id": "TC5_resumption", "result": "pass", "observed": 1.0854, "threshold": 1.08495, "comparison": ">"},
      {"id": "TC6_clearance", "result": "pass", "observed": 2.0, "threshold": 1.0, "comparison": ">=", "evidence_id": "resistance_clearance_sufficient", "price_inside_zone": false, "detail": "resistance R4, raw T5 interval 1.08760-1.08790, strength 2; distance (1.08760 - 1.08540) / 0.00110"}],
    "failed_conditions": [],
    "vetoes": [{"id": "V1_ema50_structure_break", "triggered": false, "detail": "no close below its EMA50_j in the last 5 candles"},
               {"id": "V2_swing_context_conflict", "triggered": false, "observed": "higher_highs_higher_lows",
                "pivots": [
                  {"role": "H_prev", "type": "high", "price": 1.0842, "pivot_t": "2026-11-09T15:00:00Z", "confirmed_at_utc": "2026-11-09T18:00:00Z"},
                  {"role": "H_last", "type": "high", "price": 1.0861, "pivot_t": "2026-11-10T14:00:00Z", "confirmed_at_utc": "2026-11-10T17:00:00Z"},
                  {"role": "L_prev", "type": "low", "price": 1.0815, "pivot_t": "2026-11-10T02:00:00Z", "confirmed_at_utc": "2026-11-10T05:00:00Z"},
                  {"role": "L_last", "type": "low", "price": 1.083, "pivot_t": "2026-11-11T06:00:00Z", "confirmed_at_utc": "2026-11-11T09:00:00Z"}]}],
    "triggered_vetoes": [],
    "cautions": [{"id": "volatility_high", "observed": "high", "detail": "caution only: not a veto, not supporting evidence"}],
    "unavailable_context": []
  },
  "decision": {"outcome": "TRADE_CANDIDATE", "rule": "R7", "actionable": false,
               "reasons": ["trend_continuation (bullish): 6 of 6 required checks passed, no veto triggered",
                           "caution: volatility regime high"]},
  "limitations": ["thresholds are experimental defaults, not validated against outcomes",
                  "single timeframe", "news and economic events not considered",
                  "spread and trading costs not assessed"],
  "note": "Analytical classification only. Not a recommendation, instruction or authorisation to trade."
}
```

**Paired check:** the same inputs with volatility `normal` give the identical outcome, rule and
checklist. Only `cautions` changes. This is tested (§10).

### 8.4 Further examples (abbreviated)

| Example | Key facts | Result |
|---|---|---|
| **Bullish low too deep** (§5.4) | Everything as in §8.3, except the only pullback candidate has `d = 0.636` | `opportunity.status: not_qualified`, `failed_conditions: ["TC4_pullback"]` → **`MARKET_CONDITION`** (R6) |
| **Bearish valid** (GBPUSD H1) | Mirror of §8.3: trending, all four votes bearish, `C` and EMA50 below EMA200, `d = 0.167` on a high, `C < EMA20`, nearest qualifying support 1.4 ATR below, swing structure `lower_highs_lower_lows` | **`TRADE_CANDIDATE`** (R7), direction `bearish` |
| **Bearish high too far above** | As above, but the only candidate has `d = 0.667` | **`MARKET_CONDITION`** (R6), `failed_conditions: ["TC4_pullback"]` |
| **EMA200 unavailable** (D1, 640 candles) | Regime `trending`, bias `bullish`, but EMA200 `not_converged` (seed weight 0.0123 ≥ 0.01) | `required_inputs.unavailable: [{"input": "EMA200", "reason": "not_converged: 640 candles, 661 needed"}]`, status `not_evaluable` → **`NO_TRADE`** (R5). TC3 shown as `unavailable`. |
| **Genuine opposing swing context** | Bullish core votes and all TC checks pass, but `H_last` 1.0861 < `H_prev` 1.0874 and `L_last` 1.0822 < `L_prev` 1.0829 (`lower_highs_lower_lows`) | `V2_swing_context_conflict` triggered → **`NO_TRADE`** (R4), reason "confirmed swing structure opposes the bullish bias" |
| **Missing swing context** | Same as §8.3, but only one confirmed swing low exists in the window | `swing_structure: unavailable`; `unavailable_context: [{"input": "swing_sequence", "reason": "1 confirmed swing low, 2 needed"}]`; V2 `triggered: null`. Still **`TRADE_CANDIDATE`** (R7), with a limitation "swing context unavailable". Not treated as a conflict. |
| **Understood, no setup** (USDJPY H4) | ADX 16.8, slope50 0.05 → `ranging`; bias `neutral`; every required input available | TC1 `fail`, TC2 `fail`, TC3–TC6 `not_applicable` → **`MARKET_CONDITION`** (R6) |
| **Conflicting core** (GBPUSD H1) | EMA20 > EMA50 and `C` > EMA50 (bullish votes); −DI 21.5 > +DI 18.2 (bearish vote) | Bias `conflicting` → **`NO_TRADE`** (R4) |
| **Provisional** | Last candle closed `clock_only` | **`NO_TRADE`** (R2); diagnostics `trusted: false` |

## 9. No lookahead and determinism

- **As-of point:** `as_of_utc` is the close of the last window candle, and every input dates
  from at or before it.
- **Closed candles only:** forming and `clock_only` candles are excluded by T4, and re-checked
  by T6 for `J`.
- **Causal per-candle values:** `EMA20_j`, `EMA50_j` and `ATR_j` depend only on candles
  `≤ j`, because the recursive T5 functions are causal.
- **Confirmed pivots only,** with `confirmed_at_utc ≤ as_of_utc` asserted. Pending pivots never
  enter the swing sequence, levels or location.
- **No live tick:** the snapshot tick comes from after `as_of_utc`, so it's never used as `C`.
- **Prefix invariance:** the T6 result for the first `m` candles depends only on those
  candles. Appending or altering later candles can't change it.
- **Determinism:** pure functions, no clock, no randomness, and stable sort orders (pivots by
  time, levels by T5 order). Repeated runs give identical JSON, and the inputs are never
  modified.

## 10. Test coverage

**Boundary tests for the corrected rules:**
- **Pullback distance:**
  - `d_j` exactly 0.25 qualifies, and 0.2500001 fails;
  - checked on both sides of EMA20, low above and low below, and mirrored with highs;
  - uses exactly representable values (ATR 1.0, difference 0.25).
- **Lookback edges:** a touch on candle `n−5` counts; a touch on `n−6` doesn't; a touch on the
  latest candle counts.
- **Invalid or unconverged inputs in `J`:**
  - `ATR_j = 0`, `ATR_j` unconverged and `EMA20_j` unconverged each make TC4 `unavailable`,
    which gives `NO_TRADE` (R5), never a qualifying touch;
  - tested at the feature level, because accepted windows of 661 or more candles can't produce
    an unconverged `J`.
- **EMA200:** `C == EMA200` fails TC3; `EMA50 == EMA200` fails; 660 vs 661 candles gives R5
  `NO_TRADE` vs evaluable.
- **Swing sequence:**
  - equal highs give `mixed`; one confirmed low gives `unavailable`;
  - a pending pivot that would complete a conflicting sequence is ignored;
  - `lower_highs_lower_lows` against bullish gives R4 `NO_TRADE`; `mixed` doesn't veto.
- **Volatility:** paired tests, identical except `regime` high vs normal (and vs low), give the
  same outcome and rule; only `cautions` differs.
- **Existing boundaries:**
  - ADX 20 and 25 (both sides, and equality), and 40;
  - |slope50| 0.25; clearance 1.0;
  - level strength 1 vs 2; zone edges; RSI 30 and 70.
- **TC6 clearance cases (§4.1):**
  - each of cases 0–5 reached in isolation, bullish and bearish, with the exact `evidence_id`
    and result;
  - distance exactly 1.0 gives `sufficient`; 0.9999999 gives `insufficient`;
  - `C` inside the raw T5 interval (including exactly at `low` or `high`) gives distance 0,
    `insufficient` and `price_inside_zone: true`;
  - **raw versus expanded:** `C` 0.1 ATR below a resistance's raw `low`, inside its expanded
    location zone, gives distance 0.1 (not 0) and `price_inside_zone: false`, while location
    reports `at_resistance`. `C` just above the raw `high` but inside the expanded zone means
    the level isn't selected (case 3, `pass`), while location reports `at_resistance`;
  - **mutation:** making TC6 use the expanded zone instead of the raw interval must be caught
    by these tests;
  - a strength-1 level 0.1 ATR above `C` with no qualifying level still passes (case 2),
    reported as informational;
  - a broken support above `C` is ignored by bullish clearance.
- **Location (§3.4):**
  - `C` inside overlapping support and resistance zones reports both states, both ids and the
    overlap pair;
  - three mutually overlapping zones report all three pairs;
  - inside one zone with another 0.4 ATR away reports `nearest_above` with its distance;
  - zone edge equality is inclusive (containment and overlap), measured on the expanded
    location zone;
  - zone ids and orders are stable under input permutation;
  - **location can't change the outcome:** a paired test alters only the location inputs that
    TC6 doesn't use, and the outcome, rule and checklist are identical.

**Provenance tests (§1.4):**
- **Each check fails alone:** P1–P10 each fail in isolation, giving R1 `NO_TRADE` with that
  check named. Examples:
  - a different, empty or non-string `snapshot_id` on any of the three inputs;
  - a symbol or broker symbol swapped;
  - an analysis from a different validation of the same snapshot (different `required_count`);
  - a validation verdict edited after analysis;
  - `window.count` altered;
  - a window candle removed from the snapshot;
  - T5 parameters changed.
- **Matching values aren't enough:** an analysis from snapshot A paired with snapshot B, with
  identical candles but a different id, gives R1 even though every indicator value matches.
- **Provenance comes first:** a provenance failure is reported as provenance, not as a
  consistency-guard failure.
- **Production entry point (§1.5):**
  - **Same result:** `evaluate_market_snapshot(s)` gives the same output as
    `evaluate_market` on separately computed `validate_window` / `analyse_window` inputs for
    the same snapshot, except `provenance.mode` and `content_binding`.
  - **Caller's object untouched:** the caller's snapshot is never modified.
  - **Caller changes have no effect:** the caller altering their snapshot object after the
    call can't change the result, and the result holds no reference to that object.
  - **Defaults:** `required_count` defaults to 661 with the default parameters.
  - **Provenance still checked:** P1–P10 are evaluated and reported as passed.
  - **Purity:** no clock read. `analysed_at` is passed through to T4 unchanged.
- **Provenance metadata (§1.6):**
  - `evaluate_market_snapshot` always gives `constructed` / `by_construction`, including when
    the outcome is `NO_TRADE`;
  - `evaluate_market` always gives `supplied` / `not_verified`, even for inputs that were in
    fact built correctly from one snapshot;
  - neither public function accepts a provenance argument (signature test);
  - an unknown mode passed to `_evaluate` raises (the one direct test of the private
    function);
  - a P1 failure introduced inside the constructed path, by a fault-injected stub, still gives
    R1 `NO_TRADE` with `mode: "constructed"`.
- **Swing pivots shown (§7.1):** the four pivots and their timestamps appear for every swing
  state, including `mixed`, and as the partial list for `unavailable`.

**Rule tests:**
- each of R1–R7 reached in isolation, and rule order respected (for example, an R4 conflict
  with an unavailable input gives R4);
- `actionable` false in every outcome.

**Property tests:**
- mirror symmetry (`p → 2M − p` swaps bullish and bearish with an identical outcome);
- 30 identical runs give identical JSON;
- input immutability; strict JSON; prefix invariance.

**Vocabulary test:** the T5 ban list is extended to T6 output (buy, sell, entry, stop_loss,
take_profit, target, lot, position size).

**End-to-end:** synthetic histories through real T4 → T5 → T6, both trending (constructed drift)
and ranging.

**Mutation checks (each must be caught):**
- **Pullback rule:**
  - one-sided pullback rule restored;
  - latest ATR used instead of `ATR_j`;
  - latest EMA20 used instead of `EMA20_j`;
  - an invalid `ATR_j` silently skipped;
  - the pullback band made exclusive;
  - lookback off by one.
- **EMA200:** check skipped when unavailable; R5 replaced by `MARKET_CONDITION`.
- **Context and volatility:** high volatility made a veto; `unavailable` swing context treated
  as a conflict; `mixed` treated as a conflict.
- **Trust and lookahead:** a pending pivot used; the live tick used as `C`; provisional
  analysis treated as trusted.
- **Conflict handling:** majority voting instead of `conflicting`.

**Performance:** four O(n) recomputed series at n ≤ 1,000, well within T5's own 37.9 ms. A
generous 2 s bound is tested on the 1,000-candle maximum.

## 11. Assumptions, limitations and deferred functionality

**Experimental defaults** (configurable; not tuned, not validated):

| Parameter | Default | Basis |
|---|---|---|
| `adx_range_max` / `adx_trend_min` / `adx_extended` | 20 / 25 / 40 | Wilder's published conventions |
| `flat_slope_atr` / `touch_tolerance_atr` | 0.25 / 0.25 | The approved S/R "indistinguishable" unit |
| `clearance_atr` | 1.0 | Judgement: about one average candle range |
| `slope_lookback` / `pullback_lookback` | 5 / 5 | Judgement |
| `min_level_strength` | 2 | Judgement: a level confirmed at least twice |
| RSI overbought / oversold | 70 / 30 | Wilder's convention; caution only |
| `require_ema200_alignment` / `veto_on_context_conflict` | true / true | Review decisions B and D |

No threshold will be tuned against evaluation data.

**Limitations:**
- single timeframe;
- no news or calendar;
- no spread or cost assessment, and no spread filter;
- no volume;
- level strength is a count, not a reliability measure;
- late-trend bias is inherent to continuation setups;
- ADX near 25 can flip the regime between candles (no hysteresis, to keep purity and prefix
  invariance);
- D1 has little history margin (§6.2);
- a TC6 pass because no qualifying opposing level was found means only that none was
  identified in the analysed history, not that the market has no such level (§4.1);
- swing pivots have no age limit (U6). Their timestamps are shown, but an old pivot counts the
  same as a recent one;
- the production entry point's provenance comes from construction (one deep copy), not from
  cryptographic integrity. Snapshot authenticity rests on T2 (§1.5);
- T6 has never run on live data, and T1/T2 live verification is still outstanding.

**Deferred:**
- support/resistance reactions, range strategies and breakouts;
- multi-timeframe alignment;
- spread filters;
- outcome evaluation, which is the only way to validate thresholds;
- API, dashboard and lifecycle wiring;
- anything involving risk sizing, execution, ML, AI or news.

**Unresolved ambiguities.** I chose a conservative default for each; please confirm or
override:

| # | Ambiguity | Default I chose |
|---|---|---|
| U1 | R5 versus a definitive failure: when a required input is unavailable **and** another check already fails on available data, the outcome could be `NO_TRADE` or `MARKET_CONDITION` | **`NO_TRADE`** (rules in order: R5 before R6). Consequence: below 661 candles, T6 never says `MARKET_CONDITION`. |
| U2 | Genuine swing conflict could be a setup veto leading to `MARKET_CONDITION`, or conflicting directional evidence leading to `NO_TRADE` | **`NO_TRADE`** via R4, following the original "default to `NO_TRADE` when evidence is contradictory" |
| U3 | Lookback membership: whether `J` includes the latest candle | **Includes it**, candles `n−5 … n−1` |
| U4 | Invalid value on one candle in `J`: TC4 `unavailable`, or skip that candle | **TC4 unavailable**, giving `NO_TRADE` |
| U5 | A deep-pierce candle in `J` alongside a valid touch | **No effect.** TC4 needs one qualifying candle, and depth is handled only by the unchanged EMA50 veto V1. |
| U6 | Swing sequence recency: the last two confirmed highs and lows anywhere in the window, with no age limit | **No age limit** |
| U7 | `TRADE_CANDIDATE` while swing context is `unavailable` | **Allowed,** with a stated limitation |
| U8 | Clearance ignores strength-1 levels for the result (they're reported only), and is measured from `C` to the nearest qualifying level's raw T5 interval edge (0 only if `C` is inside the raw interval; the expanded location zone isn't used) | **As stated** in §4.1 |
| U9 | No role reversal: a broken support above `C` isn't resistance for bullish clearance, and vice versa | **Not treated as opposing.** Changing this would alter TC6 behaviour, so it isn't proposed. |
| U10 | Closing the provenance gaps in §1.4 | **Resolved (2.4): option (a) adopted.** `evaluate_market_snapshot` (§1.5) builds the validation and the analysis from one deep copy of the snapshot. `evaluate_market` with P1–P10 stays for internal use and tests. How each path's provenance metadata is produced: §1.6 (2.5). Option (b), a T4 digest, isn't adopted: no hashes, schema changes or persistence, and T2–T5 are unchanged. |

## 12. Changes in this revision

| # | Change | Reason |
|---|---|---|
| 1 | Scope narrowed to trend continuation only; other setups deferred | Review scope |
| 2 | **Pullback rule (A):** `abs(Low_j − EMA20_j) / ATR_j ≤ 0.25` (bearish: `High_j`), with per-candle EMA20 **and ATR** (previously the latest ATR) | Correction A |
| 3 | Per-candle validity rules added (§2.2); any invalid `EMA20_j` or `ATR_j` in `J` makes TC4 `unavailable`, never a touch; lookback stated to include the latest candle | Correction A |
| 4 | EMA50 structure-break veto clarified to compare each candle with its own `EMA50_j` (strict `<`); its logic is otherwise unchanged; renamed `V1_ema50_structure_break` | Correction A (kept separate) |
| 5 | **EMA200 (B):** stays mandatory; unavailable gives `not_evaluable` and `NO_TRADE` (previously `MARKET_CONDITION`); short-history effect documented | Corrections B and E |
| 6 | **Volatility (C):** `high` is a caution only, never a veto, never support; T5 thresholds reused | Correction C |
| 7 | **Swing context (D):** exact sequence rules; only a complete opposite sequence is a conflict; `unavailable` and `mixed` are not; EMA200 removed from "context", since it's required check TC3 | Correction D |
| 8 | Former `V1_untrusted` veto removed and replaced by the required-input check and rule R5 | Correction E |
| 9 | **Decision table (E)** restated as R1–R7, including R5 "required input unavailable → `NO_TRADE`"; the "opposite qualified setups" rule dropped (one setup only) | Correction E |
| 10 | Output restructured into `market_condition`, `opportunity` and `decision`; checks report `pass` / `fail` / `unavailable` / `not_applicable`; `failed_conditions`, `triggered_vetoes`, `cautions`, `unavailable_context` added; `actionable` moved into `decision` | Output contract |
| 11 | Clearance distance defined with a clamp at 0 and an explicit level-selection rule | Remove ambiguity |
| 12 | Examples, tests and mutations updated for all of the above; earlier open decisions on scope, EMA200, volatility, veto, thresholds, spread and breakouts are now settled by the review | Review |
| 13 | **(2.1)** Location reports a list of applicable states plus explicit zone relationships: zone ids, `zones_containing_price`, `overlaps`, `nearest_below` / `nearest_above` with distances. No new threshold; stated to be context only and unable to qualify or reject a setup. | Clarification 1 |
| 14 | **(2.1)** TC6 records one of six ordered evidence ids per direction (§4.1), distinguishing no levels, no qualifying levels, no qualifying level on the relevant side, insufficient clearance and sufficient clearance; strength-1 levels reported only; no-role-reversal stated as U9. Behaviour, thresholds and minimum strength unchanged. | Clarification 2 |
| 15 | **(2.1)** Cross-reference fix: the volatility caution now points to §7.2 (it wrongly pointed to §6.2) | Editorial |
| 16 | **(2.2)** Verified T5 support/resistance contract added to §4.1. Trust taken from `trusted` only (untrusted lists can be empty or populated). Case 0 covers every untrusted path. Case 1 requires trusted, an empty side and T5's side reason (otherwise R1). Case 2 made side-specific. §6.1 states that both-sides-empty is never trusted. All six cases and their results unchanged. | Pre-implementation verification |
| 17 | **(2.3)** Input provenance checks P1–P10 added (§1.4), run first, any failure giving R1 `NO_TRADE`; the consistency guard explicitly excluded as provenance evidence; R1 row, `inputs.provenance` in the output contract and provenance tests added; residual gaps documented and closing options left to you (U10). No other decision changed. | Provenance verification |
| 18 | **(2.4)** §8.3 example window corrected: 700 H1 candles ending 2026-11-11T12:00Z start at **2026-10-01T08:00:00Z**, not 2026-10-13T09:00Z (the old value counted 700 continuous hours, ignoring weekend closures). Verified with the real T3 schedule and T4 `validate_window` (accepted). Indicator values, checklist, caution and outcome unchanged. | Review |
| 19 | **(2.4)** U10 resolved with option (a): production entry point `evaluate_market_snapshot` (§1.5), deep copy then T4 → T5 → T6, provenance by construction; explicitly not cryptographic; `evaluate_market` with P1–P10 kept for internal use and tests; `inputs.provenance.mode` added | Review |
| 20 | **(2.4)** TC6 "no qualifying opposing level" pass clarified to mean "none identified in the analysed history" (§4.1, §11); swing-sequence pivots and their timestamps always shown in the output (§7.1, §8.3 example); no-age-limit policy (U6) retained | Review |
| 21 | **(2.5)** Provenance metadata made unambiguous (§1.6): both public entry points call one private `_evaluate(..., provenance_mode)`. `evaluate_market_snapshot` fixes `"constructed"` (giving `by_construction`), `evaluate_market` fixes `"supplied"` (giving `not_verified`). The fixed mode-to-binding mapping is written only inside `_evaluate`, no public provenance parameter exists, and P1–P10 are identical for both modes. Pipeline (§1.2), module table, §1.5, output contract and tests updated. U10 decision unchanged. | Review of 2.4 |
| 22 | **(2.5)** TC6 stated to use the **raw T5 level interval** `[low, high]` (distance 0 and `price_inside_zone: true` only inside it); location uses the **expanded location zone** `[low − tol, high + tol]`, which never influences TC6. Terminology added (§2.3); §3.4, §4.1, U8, the §8.3 example and tests updated; a worked example shows location and TC6 legitimately differing. The TC6 calculation, threshold, minimum strength, level selection and all outcomes are unchanged. | Review of 2.4 |
