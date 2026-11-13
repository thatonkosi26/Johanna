# T6 implementation report: market conditions and trend-continuation detection

Date: 2026-11-12 · Claude Code (Opus 5.5) · Implements design revision 2.5
(`docs/t6-market-conditions-design.md`) · Working tree only, **nothing committed** · Stopped
after T6 (no API, dashboard or T7 work) · **Includes the freshness correction (§12)**:
`evaluate_market_snapshot` now requires an explicit evaluation time.

T6 classifies market conditions and evaluates one setup, trend continuation, deterministically.
Every result is `NO_TRADE`, `MARKET_CONDITION` or `TRADE_CANDIDATE`, with
`decision.actionable: false`.

- **A `TRADE_CANDIDATE` is an analytical finding only:** not an instruction, recommendation or
  order.
- **No trading values:** no prices for opening, protecting or closing a position, and no sizes
  or order parameters, are produced.
- **Synthetic data only:** T6 has been run only on synthetic histories, not on live Exness data.

## 1. Files

| File | Status | Contents |
|---|---|---|
| `src/johanna/analysis/features.py` | new | `DecisionParameters`, `required_candles`, per-candle validity, slope50, pullback window, structure-break inputs, swing sequence, expanded location zones, raw-interval clearance selection, consistency guard |
| `src/johanna/analysis/conditions.py` | new | Trend regime, directional bias (four votes), trend strength, volatility, momentum, location, swing structure. Doesn't import setups. |
| `src/johanna/analysis/setups.py` | new | Required-input check, TC1–TC6, the TC6 evidence cases, V1 and V2, cautions |
| `src/johanna/analysis/decision.py` | new | P1–P10, gate, structural checks, R1–R7, output assembly, `_evaluate`, both public entry points |
| `tests/t6_fixtures.py` | new | Deterministic trend histories on the real T3 schedule; a hand-written feature set; an independent price reflection |
| `tests/test_t6_features.py`, `test_t6_conditions.py`, `test_t6_setups.py`, `test_t6_decision.py`, `test_t6_freshness.py` | new | 22 + 42 + 38 + 62 + 27 = **191 tests** |
| `docs/t6-implementation-report.md` | new | This report |

**Nothing else changed:**
- no existing source module (`git diff --stat -- src/` is empty);
- no existing test, the API, frontend, collectors, configuration, lifecycle or `data/`.

The only other uncommitted changes are from the earlier T5.1 and design stages
(`docs/t5-analysis-report.md`, `tests/test_analysis_engine.py`,
`docs/t6-market-conditions-design.md`).

**Dependency direction:** decision → setups → conditions → features → T5, verified by imports.
Grep over the four modules finds no clock, network, filesystem, random, MT5, adapter, collector,
live-market or API import. The existing T2 import-ban test, which globs `analysis/*.py`, passes
with the new modules.

## 2. Public functions and output

```python
evaluate_market_snapshot(snapshot, *, analysed_at, required_count=None,
                         excluded_opens=(), peers=None,
                         parameters=DEFAULT_DECISION_PARAMETERS) -> dict   # production;
                                                    # analysed_at required (timezone-aware)
evaluate_market(snapshot, validation, analysis, *,
                parameters=DEFAULT_DECISION_PARAMETERS) -> dict             # supplied inputs
required_candles(parameters=..., t5=...) -> int                            # 661 by default
DecisionParameters  # frozen dataclass; experimental defaults of design §11, validated
```

**Output keys** (§8.2 of the design):
- `decision_version`, `symbol`, `broker_symbol`, `timeframe`;
- `as_of_utc`, the close of the last window candle;
- `evaluated_at_utc`, which is T5's `analysed_at_utc`, never a clock read;
- `inputs`: `snapshot_id`, versions, status, verdict, window, `price_reference`, `provenance`,
  `evaluation_time` (§12), `consistency_check`;
- `gate`, `market_condition` (`trusted`, `understood`, `summary`, `dimensions`), `opportunity`
  (`setup`, `direction`, `status`, `required_inputs`, `checklist`, `failed_conditions`,
  `vetoes`, `triggered_vetoes`, `cautions`, `unavailable_context`);
- `features` (an addition; see §9 below);
- `decision` (`outcome`, `rule`, `reasons`, `actionable: false`), `parameters`, `limitations`,
  `note`.

**Item formats:**
- **Evidence** items always carry `id, observed, threshold, comparison, source, t, detail`, with
  null where not applicable.
- **Checks** add `result` (`pass` / `fail` / `unavailable` / `not_applicable`) and
  `evidence_id`.
- **Vetoes** add `triggered` (`true` / `false` / `null`).
- **Strict JSON:** every output serialises with `allow_nan=False` (tested).

## 3. Provenance (design §1.4–§1.6)

**Both public functions call the private `_evaluate(..., provenance_mode)` with a fixed mode:**
- `evaluate_market_snapshot` → `"constructed"`, giving `content_binding: "by_construction"`;
- `evaluate_market` → `"supplied"`, giving `"not_verified"`;
- an unknown mode raises `ValueError`;
- neither public signature has a provenance parameter (tested).

**The production path:**
- `copy.deepcopy`s the snapshot, then runs `validate_window` → `analyse_window` → `_evaluate`
  on that copy only;
- the default `required_count` is `required_candles()` = 661.

The deep copy isn't cryptographic integrity protection, and the output says so in
`limitations`.

**P1–P10 always run, all ten, in order, before anything else.** Any failure gives R1, naming
each failed check.

| Check | Implemented as | Isolated failure test(s) |
|---|---|---|
| P1 | Non-empty string `snapshot_id`, equal on all three | different, empty and non-string id; twin snapshot with identical candles |
| P2 | Non-empty, equal `symbol` | swapped symbol; `None` |
| P3 | Equal timeframe, one of H1/H4/D1 | snapshot `H4` |
| P4 | `analysis.broker_symbol == snapshot.broker_symbol`, non-empty | swapped; both empty |
| P5 | `analysis.validation == validation` (deep equality) | validation copy edited |
| P6 | Verdict match, and status consistent with verdict (`invalid_input` / `unsupported_timeframe` are verdict-independent and go to the gate) | `validation_verdict` edited; status edited |
| P7 | count = len(candles), first/last match, strictly increasing, all parseable | count, first_t, reversed order and swapped pair, each synchronised so that only P7 fails |
| P8 | `analysis.window` equals the validation window (not applicable when T5 returns no window: rejected / invalid) | count edited |
| P9 | Every window candle time is in `snapshot.bars.items` | candle removed |
| P10 | `analysis_version`, `window_version`, `snapshot_version` = 1; T5 parameters equal the T5 defaults | `rsi_period` changed; snapshot version; window version |

**Also tested:**
- an analysis built from another validation of the same snapshot fails P5 and P8;
- non-mapping inputs fail all ten without raising;
- a provenance failure is reported before, and instead of, a consistency failure;
- a fault-injected analysis inside the constructed path still gives R1 with
  `mode: "constructed"`;
- a caller mutating their snapshot *during* evaluation (via an instrumented `validate_window`)
  doesn't change the result;
- the caller's snapshot is never modified, and the result holds no reference to it.

## 4. Gate, features, conditions, setup, decision

**R1, in this order:**
1. provenance;
2. evaluation time (§12): freshness re-evaluated by T2/T4 at an explicit time, and the T5
   analysis time equal to it;
3. T5 status: `rejected` / `invalid_input` / `unsupported_timeframe` / unknown;
4. structural and contract checks, giving `invalid_input`:
   - every T5 component has a boolean `trusted`;
   - for an accepted analysis, `trusted == (status == "available")`;
   - nothing is trusted in a provisional analysis;
   - EMA periods are 20/50/200;
   - window candles have finite h/l/c;
   - confirmed pivots are well formed with `confirmed_at_utc ≤ as_of`;
   - levels are well formed;
   - the T5 S/R contract holds: available never has both sides empty, and an empty side
     carries its side reason;
5. the consistency guard: recomputed EMA20/50/200 and ATR14 equal T5's values exactly.

**Features:**
- **Price and time:** `C` is the last window candle's close; `as_of` is its open plus the
  timeframe.
- **Latest values:** usable only when T5 `trusted`. In provisional diagnosis T5 marks nothing
  trusted, so component status `available` is used and nothing derived is trusted.
- **Per-candle values** (EMA20_j, EMA50_j, ATR_j) are recomputed with the T5 functions and are
  valid only if present, finite, converged at `j` (seed weight < 0.01, strict) and, for ATR,
  > 0.
- **slope50:** needs EMA50 valid at `n−1` and `n−6`, plus trusted ATR.
- **Pullback window `J`:** the last five candles, latest included. Any candle that isn't
  `tick_observed`, or any invalid EMA20_j / ATR_j, makes TC4 unavailable. Any invalid EMA50_j
  makes V1 unavailable.
- **Distances:** `d_j = |low_j − EMA20_j| / ATR_j` (bearish: `high_j`), each candle with its
  own values.
- **Swing sequence:** uses confirmed pivots only; pending pivots are counted as ignored.

**Conditions:** exactly the §3 rules, boundaries and inclusivity.
- **Bias:** four votes. Equality votes neither. Any opposing pair is `conflicting`, with no
  majority.
- **Location:** a list of states from the **expanded** zones, plus `zones_containing_price`,
  `overlaps`, `nearest_below` / `nearest_above` and `non_qualifying_levels`.

**Setup:**
- **Direction:** comes from the bias. A neutral bias gives TC3–TC6 `not_applicable`.
- **Checks and vetoes:**
  - TC1–TC6 as in §4;
  - TC6 on the **raw** T5 interval, with six evidence ids in fixed precedence;
  - V1: some close beyond its own EMA50_j;
  - V2: only the complete opposite sequence, and only with `veto_on_context_conflict`.
- **Cautions:** high volatility, overbought/oversold against the direction, extended ADX.
  They never veto and never change the checklist.

**Decision:** R1–R7 in order. R2 (provisional) shows diagnostic conditions but no setup
evaluation; the opportunity is `not_evaluable`, so a diagnostic "qualified" can't appear.

## 5. Tests and results

| Command | Result |
|---|---|
**After the freshness correction (current state):**

| Command | Result |
|---|---|
| `python -m pytest -q -p no:cacheprovider tests/test_t6_freshness.py` (3.13.3) | **27 passed** |
| `.venv311\Scripts\python.exe -m pytest -p no:cacheprovider -rfEs` (3.11.8) | **749 passed**, 0 failed, 0 errors, 0 skipped, 213 s |
| `.venv\Scripts\python.exe -m pytest -p no:cacheprovider -rfEs` (3.13.3) | **749 passed**, 0 failed, 0 errors, 0 skipped, 213 s |

- **Composition:** 749 = 558 (previous suite) + 191 (T6), including the 50 browser tests.
- **Logs:** `t6f_pytest_py311.log`, `t6f_pytest_py313.log` in the session scratchpad.

**Before the correction**, for the record: 164 T6 tests, and 722 passed on each Python. Logs:
`t6_pytest_py311.log`, `t6_pytest_py313.log`.
- **No existing test was modified.**

**Coverage highlights:**
- **Real T4 → T5 → T6 outcomes:**
  - R7 bullish and bearish, from frozen drift-plus-oscillation histories on the real T3
    schedule;
  - R6 at 661 candles; R5 at 660 (EMA200); R4 (conflicting bias); R3 (100 candles);
  - R2 (`clock_only`); R1 (rejected, tampered EMA, malformed analyses, non-finite candle);
  - precedence: R4 over an unavailable EMA200 at 300 candles; R2 over undetermined.
- **Boundaries, hand-derived:**
  - 661 = 200 + 461; slope50 needs 171 = 166 + 5 (170 fails);
  - the pullback window needs 82 = 78 + 4 for ATR_j (81 fails on ATR only);
  - convergence equality is not converged;
  - TC4 at exactly 0.25 on both sides of EMA20, passing, with 0.2500001 / 0.7499999 failing;
  - TC6 at exactly 1.0 ATR (price 1.0, ATR 0.5) in both directions;
  - ADX 20/25/40, |slope| 0.25, RSI 30/70, zone edges inclusive, strict EMA200/EMA20
    comparisons.
- **TC6:** cases 0–5 in both directions; raw versus expanded (the design §3.4 example: 0 / 0.1
  ATR / not selected while location says `at_resistance`); untrusted populated lists are case 0;
  broken support ignored.
- **Mirror symmetry:** reflecting prices end to end gives the same rule and checklist with
  bearish direction and mirrored evidence ids. It's also tested at the setup level.
- **Mutation check:** 16 deliberate defects were applied one at a time; **all 16 were caught**,
  and every module was restored byte-identical (file comparison):
  - one-sided pullback; latest ATR or latest EMA20 instead of per-candle values; invalid ATR_j
    skipped; band made exclusive; lookback off by one;
  - EMA200 requirement dropped; high volatility as a veto; unavailable swings as a conflict;
    pending pivot used;
  - live tick used as `C`; provisional treated as trusted; majority voting; TC6 on the
    expanded zone; P5 removed; R5 answered with `MARKET_CONDITION`.

  An initial run missed two. One mutation was itself a no-op and was corrected. The other
  exposed weak R3/R4/R5 assertions, which were strengthened to check the outcome as well as the
  rule.

## 6. Ruff and formatting

- `ruff check --no-cache .`: **All checks passed.**
- `ruff format --check --no-cache .`: **107 files already formatted** (after the correction).
- No JavaScript or frontend file changed, so there was no JS syntax check to run.

## 7. Determinism and no lookahead

- **Determinism:** 10 repeated evaluations give byte-identical JSON, and inputs are unchanged
  (deep-equality before and after).
- **Prefix invariance:** a 700-candle prefix gives an identical result before and after a
  longer history with altered future candles was evaluated.
- **Pending pivots:** adding a pending pivot doesn't change the decision or opportunity. A pivot
  confirmed after `as_of` is rejected as lookahead (R1).
- **No live tick:** the price reference is always the last closed candle. A tick moved to 9.0
  changes nothing. The mutation using the tick is caught.
- **No clock:** the caller's required `analysed_at` is passed to T4 and becomes
  `evaluated_at_utc`. A stale snapshot never gives a candidate or a market condition (§12).

## 8. Performance

Python 3.11.8, median of 20 runs, maximum in brackets, synthetic H1 windows:

| Window | T4 `validate_window` | T5 `analyse_window` | T6 `evaluate_market` | `evaluate_market_snapshot` (copy + T4 + T5 + T6) |
|---|---|---|---|---|
| 661 | 22.4 ms | 5.9 ms | 4.0 ms (5.2) | 39.2 ms (47.4) |
| 700 | 24.4 ms | 6.2 ms | 3.8 ms (5.2) | 42.4 ms (52.0) |
| 1,000 | 36.0 ms | 9.2 ms | 6.1 ms (7.5) | 61.3 ms (69.3) |

A test bounds the 1,000-candle constructed path at 2 s. The 1,000-candle cap is unchanged.

## 9. Deviations from revision 2.5 (all additive or clarifying; no rule changed)

1. **`DecisionParameters`** lives in `features.py` and is re-exported by `decision.py`.
   `features`, `conditions` and `setups` need it, and they can't import `decision` without
   reversing the dependency direction.
2. **A top-level `features` key** in the output carries the derived values and their
   usability: price, latest values, slope50, the pullback rows, swing pivots, level counts.
   This answers the implementation brief's requirement to show feature values and their
   availability. The design's contract didn't list it.
3. **Structural and contract checks** (§4 above) are applied under R1 `invalid_input`. The
   design required R1 for invalid input and the S/R case-1 contract; the remaining checks make
   malformed but plausible analyses fail closed instead of raising.
4. **Provisional handling:** diagnostic conditions use the T5 component status, because T5
   marks nothing trusted in a provisional window. They're all `trusted: false` and
   `understood: false`. The opportunity is `not_evaluable` with the reason "provisional window:
   diagnostic only".
5. **Pullback window closure re-check:** a window candle that isn't `tick_observed` makes TC4
   and V1 unavailable, giving R5. The design said "re-checks" without naming the consequence.
6. **P6 and P8 details:** T5's verdict-independent statuses (`invalid_input`,
   `unsupported_timeframe`) don't fail P6, and P8 is not applicable when T5 returns no window.
   Rejected inputs are therefore reported by the gate as "rejected", not as a provenance
   failure.
7. **Reporting choices:**
   - cautions are listed only when the setup has a direction;
   - TC4 reports the most recent qualifying candle, with all five distances;
   - for a `conflicting` bias, every non-neutral vote is listed as contradicting.
8. **±DI usability** follows the ADX trust flag: ADX is trusted only when both smoothing stages
   have converged.

## 10. Known limitations and open issues

- **Data:** no live data. T6 hasn't run on Exness data, and T1/T2 live verification is still
  outstanding (it needs the backend restart you control).
- **Thresholds:** experimental defaults (ADX 20/25/40, 0.25 ATR, 1.0 ATR, lookback 5, strength
  2, RSI 70/30), not validated against outcomes and not tuned.
- **Scope gaps:** single timeframe; no news, spread, cost or volume; level strength is a count.
- **TC6 meaning:** a pass because no qualifying opposing level was found means only that none
  was identified in the analysed history.
- **Swing pivots** have no age limit (U6); their timestamps are shown.
- **Pullback depth (U5):** a deep-pierce candle doesn't invalidate TC4 when another candle
  qualifies. V1 only checks closes against EMA50.
- **Freshness (resolved, §12):** an explicit evaluation time is now required. Freshness is
  only as good as the time the caller supplies: T6 can't check that `analysed_at` is the real
  current time, because it never reads a clock.
- **Short D1 history:** windows under 661 candles always give `NO_TRADE`. D1 has little margin.
- **Outdated package docstring:** `johanna/analysis/__init__.py` still describes only T2. It
  wasn't edited, because the brief limited changes to the four new modules.
- **U1–U10** stand as decided in the design.

## 11. Untouched systems

- **Existing code:** T2–T5 modules and tests (no tracked change under `src/`), the API,
  dashboard and frontend, collectors (still disabled), configuration and lifecycle.
- **No MT5 access:** no MT5 connection or terminal access, and no MT5 import in the new
  modules.
- **`data/`:** all 12 entries identical in SHA-256, size and modification time before and after
  the verification runs. The lock file can't be hashed while the backend holds it; its size and
  time were unchanged.
- **Backend not stopped, restarted or contacted:** PID 37872 and its two workers were identical
  before and after.
  - **Restart before this stage:** that process had started at 20:31:04, from your own console
    (parent `cmd.exe` 19060), after the earlier T5.1 checks and before this stage began.
- **No trades** were placed, and nothing was committed or pushed.

## 12. Freshness correction (post-review)

### The problem, verified in the code

**T2 freshness rule** (`snapshot.evaluate_snapshot`, unchanged):
- **Age:** `snapshot_age = evaluation time − bars.retrieved_at_utc`, rounded to the
  millisecond (`round(..., 3)`).
- **Refused** (`snapshot_too_old` / `inconsistent_timestamps`) when the age is above
  `max_snapshot_age_seconds` = **5.0 s**, or negative.

**How T4 applies it** (`window.validate_window`, unchanged):
- **With `analysed_at`:** T4 re-runs `evaluate_snapshot` at that time and records
  `dimensions.snapshot.evidence.re_evaluated = true`. A refused snapshot makes the window
  `rejected`, which gives T6 R1.
- **Without it:** T4 reuses the eligibility stored at assembly.

**Demonstrated before the fix:** `evaluate_market_snapshot(snapshot)` with no time still
returned **`TRADE_CANDIDATE` (R7)** for a snapshot whose bars were already stale. A time one day
later correctly gave R1.

The supplied path had the same gap whenever the caller's validation was built without
`analysed_at`.

### The correction (T6 only; T2–T5 unchanged)

1. **`evaluate_market_snapshot(..., *, analysed_at, ...)`:**
   - `analysed_at` is now a **required** keyword argument with no default, so omitting it
     raises `TypeError`.
   - An explicit `None`, a naive datetime or a non-datetime returns **R1 `NO_TRADE`**
     *without running T4/T5*. The reason is "no evaluation time given ..." or "analysed_at must
     be a timezone-aware datetime ...".
   - `inputs.provenance.checks` is then empty, and `passed` is `false`.
2. **New evaluation-time check in `_evaluate`, both paths, run right after P1–P10**
   (`inputs.evaluation_time`). It requires:
   - `validation.dimensions.snapshot.evidence.re_evaluated is True`;
   - a parseable `validation.evaluated_at_utc`;
   - `analysis.analysed_at_utc == validation.evaluated_at_utc`.

   Any failure gives R1 with the reason "evaluation time: ...".
3. **Unchanged:** T6 still never reads a clock. The caller's time goes through T4 → T5 → T6
   unchanged. No new threshold was introduced; the decision is T2's existing 5.0 s rule.
4. **No rule changed:** indicator, condition, setup, TC6 and R1–R7 rules are unchanged. This is
   an additional R1 input condition.

### Tests (`tests/test_t6_freshness.py`, 27 tests)

| Case | Result |
|---|---|
| T2 limit used unchanged | `max_snapshot_age_seconds == 5.0`; bars retrieved at BASE + 0.2 s |
| `analysed_at` omitted | `TypeError`: keyword-only, no default (signature checked) |
| `analysed_at=None` | R1 `NO_TRADE`, explicit reason; T4/T5 never called (they're replaced with functions that fail if called) |
| Naive datetime, string, integer | R1, "timezone-aware datetime" |
| Fresh, age 0.8 s | R7 `TRADE_CANDIDATE`; `evaluation_time.passed`, `re_evaluated: true` |
| Exact boundary, age 5.000 s | R7 (not > 5.0) |
| T2 millisecond resolution | 5.0004 s rounds to 5.000: fresh; **5.001 s** is the first stale value |
| Just beyond, age 5.001 s | R1 `NO_TRADE`; T4 reason `snapshot_refused` with `snapshot_too_old` |
| Before the bars were retrieved, −0.001 s | R1 |
| Stale never a candidate | ages 5.001 s, 6 s, 1 min, 1 h, 1 day, 30 days, bullish and bearish: always `NO_TRADE`, never `TRADE_CANDIDATE` or `MARKET_CONDITION`, while the same data at 0.8 s is a candidate |
| Supplied validation without `analysed_at` | R1, "snapshot freshness was not re-evaluated" (provenance passes) |
| Supplied analysis time ≠ freshness time | R1, "differs from the freshness evaluation time" |
| Supplied stale validation (6 s) | R1 |
| Determinism | 10 evaluations with identical inputs and time give identical JSON |

**Changes to existing T6 tests:**
- the `pipeline` fixture now passes an explicit fresh time (1.0 s after assembly);
- the eight existing `evaluate_market_snapshot` test calls, and one `validate_window` call, pass
  `analysed_at`.

No assertion was weakened, and no T2–T5 test was touched.

**Mutation check of the fix:** three deliberate defects, all caught, with `decision.py`
restored byte-identical:
- re-evaluation no longer required;
- `None` allowed to fall through to the stored eligibility;
- the analysis-time mismatch ignored.

**Verification:**
- Ruff check clean; 107 files formatted.
- Full suites: **749 passed** on 3.11.8 and on 3.13.3, with 0 failed, 0 errors, 0 skipped.
- `data/`: 12 entries, 0 differences before and after.
- Backend: PID 37872 and its workers unchanged.
- No tracked change under `src/`, and nothing committed.

## 13. Addendum: T4 diagnostic exposure (approved additive interface change)

**Why:** through the production path, a window T4 rejects (for example for
`insufficient_history`) gives T6 R1 with only a generic reason. T4's own reason codes were
unreachable, because `validation` was a local variable of `evaluate_market_snapshot`.

**Change to `analysis/decision.py`** (additive; no T4, T5 or T6 rule changed):
- **Private `_evaluate_constructed(...) -> (result, validation | None)`:** the former body of
  `evaluate_market_snapshot`, moved unchanged:
  - deep copy;
  - evaluation-time refusal;
  - one `validate_window` call;
  - `analyse_window(work, validation)`;
  - `_evaluate(..., "constructed")`.
- **`evaluate_market_snapshot`:** now returns `_evaluate_constructed(...)[0]`. Signature, return
  type, provenance and result are unchanged (tested).
- **New public `evaluate_market_snapshot_with_validation(...)`:** same signature; returns
  `{"result": <identical T6 result>, "validation": <the exact T4 object T5/T6 used, or None>}`.
  `None` is returned only on the refuse-before-T4 paths (missing or naive `analysed_at`,
  non-mapping snapshot), where T4 isn't called.

**Proof that it's the same object, from one T4 run** (`tests/test_t6_validation_exposure.py`,
19 tests):
- **Same object, one run:** with `decision.validate_window` and `decision.analyse_window`
  spied, T4 is called exactly once; the returned `validation` *is* the object T4 returned; and
  that same object is what `analyse_window` received.
- **Cases:** R1 (insufficient history, stale), R2, R4, R6, R7 bullish and bearish.
- **Identical results:** the companion's `result` deep-equals and JSON-equals
  `evaluate_market_snapshot` for every case, and never contains `validation`.
- **Correspondence:**
  - `validation.snapshot_id == result.inputs.snapshot_id`;
  - `validation.evaluated_at_utc == result.evaluated_at_utc == analysed_at`;
  - the window count and first/last times equal `result.inputs.window`;
  - the verdict state equals `result.inputs.validation_verdict`.
- **Refuse-before-T4 paths:** `validation is None`, and T4 isn't called (replaced with a function
  that fails if called).

The existing T6 suite (191 tests) passes unchanged.

**Use by the API:** `MarketAnalysisService` now calls the companion and adds a
`window_validation` envelope field, a verbatim projection of that T4 object. The contract is
recorded in `docs/t6-integration-plan.md` §3.4.
