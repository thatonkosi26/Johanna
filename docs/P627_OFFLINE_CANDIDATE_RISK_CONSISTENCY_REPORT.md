# P6.2b.7 — Offline Candidate-to-Risk Consistency Investigation

**Date:** 2026-11-19
**Scope:** offline source analysis plus temporary synthetic tests. I didn't access `data/`, the production journal, MT5, live market data, backend APIs or Git. I didn't change source code, tests or configuration.

**Verdict:** **DESIGN INCONSISTENCY CONFIRMED — SAFE RESOLUTION IDENTIFIED**

---

## 1. Executive summary

- **The inconsistency is real.** T6's TC6 check *passes* in three cases where it hasn't selected an opposing level (`selected_level = None`). Phase 4 uses exactly that `selected_level` as its take-profit, so it always rejects these candidates with `construction/target_structure`.
- **What this means in practice.** An R7 `TRADE_CANDIDATE` whose TC6 pass came from "no qualifying opposing level" can never become a trade. It is a candidate that is structurally dead on arrival.
- **It is fail-closed, not unsafe.** No trade, approval or intent can result from it. The defect is consistency and signal quality: the candidates are misleading, not dangerous.
- **When a level *is* selected, the stages agree.** Both use the same envelope, the same strength threshold (≥ 2) and the same side rules.
- **Phase 4 applies further filters after that**, which are designed behaviour, not inconsistencies:
  - stop direction;
  - target direction;
  - gross R:R ≥ 2;
  - net R:R ≥ 2 after costs.
- **TC6's 1.0 ATR clearance doesn't imply 1:2.** It measures something different. In one synthetic case, exactly 1.0 ATR clearance gave a gross R:R of 0.41.
- **The two historical EURUSD H1 SELL candidates fit this pattern.** Both are R7 with TC6 pass, and both were rejected with `target_structure`. From source, TC6 must have taken one of the three no-target branches. Persisted evidence can't say which, and holds no prices, levels or R:R.
- **Recommendation: Option C, a shared structural-target validation contract, introduced in stages.**
  - Stage 1 is Option A's semantics: T6 doesn't qualify a setup without a valid structural target.
  - Phase 4 stays the authority, using live quotes and costs.
  - Nothing is relaxed. The 1:2 net rule and every fail-closed check stay unchanged.
- **Secondary findings:**
  - Phase 4 raises an unhandled `AttributeError` on a non-mapping `selected_level`. The real T6 can't produce one. The result is still fail-closed: HTTP 500, and no approval.
  - Phase 4 doesn't re-check the target's strength.
  - The journal doesn't persist TC6's `evidence_id` or `selected_level`.

## 2. Source trace (file:line)

### 2.1 T6 candidate generation

| Step | Location |
|---|---|
| Decision table R1–R7 (`_evaluate`) | `src/johanna/analysis/decision.py:415` |
| R5: required setup input unavailable | `decision.py:576-585` |
| R7: `opportunity.status == "qualified"`, so `TRADE_CANDIDATE` ("6 of 6 required checks passed, no veto") | `decision.py:586-591` |
| R6: otherwise, `MARKET_CONDITION` with the failed checks and vetoes | `decision.py:592-595` |
| Limitation text when TC6's `evidence_id` starts with `no_` ("does not establish that none exists in the market") | `decision.py:602-616` |
| Required inputs (EMA20/50/200, ATR14, slope50, pullback, structure break, usable levels, otherwise R5) | `src/johanna/analysis/setups.py:39` |
| Experimental parameters: `adx_trend_min` 25, `flat_slope_atr` 0.25, `touch_tolerance_atr` 0.25, `clearance_atr` 1.0, `pullback_lookback` 5, `min_level_strength` 2, `require_ema200_alignment` True | `src/johanna/analysis/features.py:35-64` |

### 2.2 TC1–TC6 (trend continuation)

- **TC1** trend strength: ADX ≥ 25 and |slope50| > 0.25 ATR.
- **TC2** all four bias votes agree.
- **TC3** EMA200 alignment.
- **TC4** pullback within 0.25 ATR of EMA20 in the last 5 candles, all `tick_observed`.
- **TC5** close beyond EMA20.
- **TC6** clearance.
- Vetoes **V1** (EMA50 break) and **V2** (swing conflict).
- Location: `setups.py` (the checklist is assembled around `setups.py:264`; TC3–TC5 logic is around `setups.py:280-310`).

### 2.3 TC6 level selection and clearance

- **`clearance()`**, `setups.py:63-156`, with one `evidence_id` per path (first match wins).

  | Condition | Result | `evidence_id` | `selected_level` |
  |---|---|---|---|
  | levels or ATR not usable | **unavailable** (leads to R5) | `support_resistance_untrusted` | — |
  | no levels on the opposing side | **pass** | `no_resistance_levels` / `no_support_levels` | **None** |
  | levels exist, none with strength ≥ 2 | **pass** | `no_qualifying_resistance` / `no_qualifying_support` | **None** |
  | qualifying levels exist, none on the far side of price | **pass** | `no_qualifying_resistance_above` / `no_qualifying_support_below` | **None** |
  | nearest qualifying level, distance ≥ 1.0 ATR | pass | clearance | set |
  | nearest qualifying level, distance < 1.0 ATR | fail | clearance | set |

- **`select_clearance_level()`**, `features.py:314`:
  - Side: resistance if bullish, support if bearish.
  - Relevant levels: `high ≥ price` if bullish; `low ≤ price` if bearish.
  - Order: nearest first.
  - `selected` = first level with strength ≥ `min_level_strength`.
  - It also reports `nearest_non_qualifying`.
- **`raw_distance()`**, `features.py:339`:
  - bullish: `max(0, low − price) / ATR`;
  - bearish: `max(0, price − high) / ATR`.
  - `price` = last closed candle's close, not a live quote.

### 2.4 Evidence persistence

- **`analysis_record()`**, `src/johanna/decision_journal.py:105`:
  - It persists the decision (outcome, rule, reasons), the gate, validation, market states and the opportunity.
  - For the checklist it keeps **only `{id, result}`** (`decision_journal.py:144-148`).
  - It does **not** keep the TC6 `evidence_id`, `selected_level`, distance, ATR, levels, prices or limitations.
- **`risk_record()`**, `decision_journal.py:181`:
  - It persists Phase 4 status, validations (id and outcome), rejections and unverified `[area, id]`, and `gross_reward_risk`.
  - Free-text detail isn't persisted.
- **Shadow cycles** do persist each checklist `evidence_id` as a gate `reason_code` (`src/johanna/shadow_evaluation.py:232-241`). No shadow cycle has produced an R7, so this evidence doesn't exist for the two historical candidates.

### 2.5 Phase 4 setup intake and target selection

- **`plan_trade()`**, `src/johanna/risk_engine.py:363`. Order: `_setup`, then account and instrument, then `_construct`, then money, then `_net_gate`, then sizing.
- **`_setup()`**, `risk_engine.py:589-649`:
  - T6 missing, so `data/analysis` unverified.
  - Evaluation older than `max_analysis_age_seconds` (60 s, `risk_engine.py:153`), so `analysis_fresh` unverified.
  - Not `TRADE_CANDIDATE`, so `t6_candidate` failed.
  - Not qualified, direction not bullish/bearish, or market not trusted, so `t6_candidate` failed.
- **Target selection**, `risk_engine.py:846-858`:
  - It looks up the `TC6_clearance` checklist item and reads its `selected_level`.
  - If the level is missing (or `low`/`high` is non-finite), the result is **`construction/target_structure` failed**.
  - The module docstring states this design: "Take-profit: the near edge of the level T6's TC6 check selected … No such level: rejected."

### 2.6 SL/TP construction

- **Prerequisites:**
  - ATR unusable, so `construction/atr` unverified (`risk_engine.py:833-838`).
  - No `L_last`/`H_last` swing, so `stop_structure` failed (`risk_engine.py:839-845`).
- **Construction**, `risk_engine.py:859-868`:
  - buffer = `stop_buffer_atr` (0.25, `risk_engine.py:150`) × ATR;
  - BUY: SL = `L_last − buffer`, rounded down; TP = `level.low`, rounded down; entry = ask;
  - SELL: SL = `H_last + buffer`, rounded up; TP = `level.high`, rounded up; entry = bid.

### 2.7 Target validation

`risk_engine.py:883-905`:
- `stop_direction`: SL must be below the bid (BUY) or above the ask (SELL).
- `target_direction`: TP must be beyond the entry, with reward > 0.

### 2.8 R:R and cost checks

- **Gross check**, `risk_engine.py:906-917`: if `reward / risk < min_reward_risk` (2.0, `risk_engine.py:149`), then `construction/reward_risk` failed. Spread is included; costs can only lower the ratio. Otherwise `construction/levels` passed (`risk_engine.py:918-925`).
- **Net gate**, `_net_gate`, `risk_engine.py:1352-1404`:
  - If costs aren't established (production `COST_TERMS` are empty), `costs/net_reward_risk` is **unverified** and the proposal is UNVERIFIED.
  - Otherwise net = (reward − cost) / (risk + cost). If net < 2, `net_reward_risk` failed; else passed.

## 3. Historical candidate reconstruction (2 × EURUSD H1 bearish)

This is based only on facts already in `docs/P6_PRODUCTION_OPPORTUNITY_AGGREGATE_REPORT.md` (P6.1), combined with source logic. **No production data was read in this phase.**

**Established from persisted evidence (P6.1):**

| Fact | Value |
|---|---|
| Count | 2 R7 decisions, 2 distinct candles, 87 observations in total |
| Symbol / timeframe / direction | EURUSD / H1 / bearish (SELL) |
| Source | `analysis_request` (risk requests deduplicated onto the same decision) |
| Checklist | TC1–TC6 all pass; no vetoes; one carried the caution `volatility_high` |
| Phase 4 | linked assessment for each, both **REJECTED** with `construction/target_structure` |
| Gross R:R / costs | none recorded; construction stopped before them |

**Inferred from source:** for the bearish case, TC6 passed through one of three no-target branches:
- `no_support_levels`
- `no_qualifying_support`
- `no_qualifying_support_below`

The reasoning:
- `target_structure` fires only when `selected_level` is absent or has a non-finite edge.
- The R7 decision requires TC6 = pass.
- TC6 passes with a selected level only when distance ≥ 1 ATR, and then the level is present.
- The non-finite-edge alternative would need T5 to emit a non-finite level price. No code path for that was found, but it can't be excluded from persisted evidence alone.
- Because the risk request's analysis was deduplicated onto the same decision, the envelope Phase 4 saw had the same TC6 result.

**Cannot be established:**
- which of the three branches applied;
- the support levels and their strengths;
- the close price, ATR or `H_last` pivot;
- the would-be SL;
- whether any structural target would have reached 1:2 gross or net;
- each candidate's exact candle time beyond what P6.1 reported.

I haven't estimated or invented any of these.

## 4. Offline synthetic matrix (executed)

- **What ran:** a temporary file, `scratchpad/p627/test_p627_matrix.py`, outside the repository (full path in §11).
- **How:** it imports the repository's existing fixtures (`tests/risk_fixtures.py`, `tests/t6_fixtures.py`) and calls the **real** `setups.clearance`, `features.select_clearance_level` and `RiskService → plan_trade` against a `FakeMT5`.
- **Data:** all synthetic. Envelopes are real-pipeline R7 envelopes reshaped by the fixtures.
- **Results:**
  - Python 3.13: **45 passed, 1 failed**.
  - Python 3.11: **45 passed, 1 failed**.
  - The one failure is a genuine finding (case 10d).

### 4.1 Layer 1 — TC6 (T6)

| # | Case | Result |
|---|---|---|
| 1 | valid target: resistance strength 2 at 2.0 ATR | pass, level selected |
| 2 | no resistance levels | **pass, `selected_level` None** (`no_resistance_levels`) |
| 3 | only a strength-1 level (0.55 ATR away) | **pass, None** (`no_qualifying_resistance`) |
| 5 | strength-3 zone entirely below price (bullish) | **pass, None** (`no_qualifying_resistance_above`) |
| 4 | qualifying level 0.55 ATR away | fail (clearance) |
| 7 | levels s5 @ far, s1 @ nearest, s2 @ middle | s2 selected; s1 skipped (reported as `nearest_non_qualifying`); pass at 1.45 ATR |
| 10 | levels not usable | unavailable (`support_resistance_untrusted`), so R5 |
| — | bearish, no support levels | **pass, None** (`no_support_levels`) |
| 8 | level at exactly 1.0 ATR, structural stop below `L_last` | TC6 **pass**, but gross R:R **0.41**: clearance ≠ 1:2 |

### 4.2 Layer 2 — Phase 4 (real `plan_trade`)

| # | Case | Status | Rejection / unverified |
|---|---|---|---|
| 1 | valid BUY target (1:2.19), synthetic verified costs | APPROVED_FOR_MANUAL_REVIEW | — |
| 1 | valid SELL target (1:2.17), synthetic verified costs | APPROVED_FOR_MANUAL_REVIEW | — |
| 1 | valid BUY, **production** CostTerms | UNVERIFIED | `net_reward_risk`, `limit` ×2, `margin_volume` |
| 2 | no target BUY (TC6 pass, None) | REJECTED | `target_structure` |
| 2 | no target SELL (historical shape) | REJECTED | `target_structure` |
| 4 | target too close (gross < 2) | REJECTED | `reward_risk` |
| 5 | wrong-side target | REJECTED | `target_direction` |
| 6 | stale T6 evidence (300 s) | UNVERIFIED | `analysis_fresh` |
| 8 | structural stop too wide (gross < 2) | REJECTED | `reward_risk` |
| 9 | gross 2.19, slippage 10 pts/side | APPROVED_FOR_MANUAL_REVIEW | — (net still ≥ 2) |
| 9 | gross 2.19, slippage 20 / 40 pts/side | REJECTED | **`net_reward_risk`** (gross passes, net fails) |
| 10a | `selected_level.low` = NaN | REJECTED | `target_structure` |
| 10b | `high` missing | REJECTED | `target_structure` |
| 10c | `low`/`high` None | REJECTED | `target_structure` |
| 10d | `selected_level` is a string | **unhandled `AttributeError`** at `risk_engine.py:851` | — (see §9) |
| 10e | TC6 checklist entry absent | REJECTED | `target_structure` |
| 3' | `selected_level.strength` = 1 (TC6 can't produce this) | APPROVED_FOR_MANUAL_REVIEW | — (Phase 4 doesn't re-check strength) |
| — | no target × {EURUSD, GBPUSD, USDJPY} × {H1, H4, D1} | REJECTED (9 of 9) | `target_structure` |

### 4.3 Option C prototype contract (test-only function, 9 cases)

| Case | Verdict |
|---|---|
| valid BUY | pass |
| valid SELL | pass |
| no target BUY / SELL | fail, `no_structural_target` |
| strength 1 | fail, `target_strength_below_minimum` |
| too close | fail, `structural_reward_risk_below_minimum` |
| wrong side | fail, `target_wrong_side` |
| wide stop | fail, `structural_reward_risk_below_minimum` |
| NaN | fail, `invalid_target_evidence` |

All 9 matched expectations.

## 5. Direction, symbol and timeframe coverage

- **BUY vs SELL:**
  - The logic is mirror-symmetric in `select_clearance_level`, `raw_distance` and `_construct`.
  - The no-target case fails identically for both: BUY rejected `target_structure`, SELL rejected `target_structure`.
  - Valid targets pass for both directions.
- **Symbols and timeframes:**
  - There is a single `DecisionParameters` default and a single `RiskPolicy`.
  - Neither TC6 nor `_construct` has a symbol- or timeframe-specific branch.
  - The 3 × 3 run confirms that the `target_structure` rejection is uniform.
  - **Caveat:** the fixture envelope is EURUSD-scaled. For USDJPY the run is only meaningful up to the `target_structure` check, which comes before any price comparison. Price-scale behaviour for JPY (digits 3) was not tested here.
- **Frequency:** D1 has produced 0 production decisions (P6.1), so it has no historical evidence.

## 6. Resolution options

### Option A — align TC6 with Phase 4: no qualifying target means TC6 fails

- **What changes:** the three no-target branches return `fail`, with the existing `evidence_id`s kept, so the setup becomes R6 instead of R7.
- **Safety:** strictly tighter. It removes candidates that can never trade, and adds none.
- **Consistency:** removes the contradiction completely for the no-target case.
- **Frequency:**
  - R7 goes down by the share of no-target candidates. Historically that is 2 of 2 (100 %, n = 2, too small to generalise).
  - Approvable proposals are unchanged (still 0 from these cases).
  - The R6 count rises.
- **SL compatibility:** none needed; SL logic is untouched.
- **1:2 net:** unchanged.
- **Failure behaviour:** still fail-closed. The existing T6 limitation text becomes unnecessary.
- **Tests and regressions:**
  - T6 tests asserting a pass on `no_*` evidence must be updated.
  - `REAL_BUY` and `REAL_SELL` in `tests/risk_fixtures.py` are themselves no-target R7s and would become R6. Many Phase 4, shadow and browser tests depend on them, so a sizeable fixture migration is needed: synthetic levels in the base bars.
  - The T6 spec (§4.1, revision 2.5) needs a revision.
- **Limitation:** it doesn't address "has a target but R:R < 2". Those still reach Phase 4 as R7 and get rejected there (consistent, but R7 still over-promises).

### Option B — alternative target method (for example an ATR multiple, a fixed R multiple or a measured move)

- **What changes:** when no structural level exists, Phase 4 synthesises a take-profit.
- **Safety:** **weakest of the three.** It manufactures a target that market structure doesn't support. It moves the strategy from "structural target" to "assumed target", and it can turn today's rejections into approvals with no structural evidence. The 1:2 rule would still apply, but would be satisfied by construction (for example TP = entry + 2R + costs), so the gate would become meaningless.
- **Consistency:** resolves the contradiction by redefining Phase 4, not by aligning the two stages.
- **Frequency:** increases candidate-to-proposal conversion. This is the reason to be cautious about it.
- **Change scope:** a methodology and strategy change that needs owner design, back-testing and validation. It is outside the "do not modify strategy" boundary.
- **Tests:** new construction paths and new approval paths; the highest regression risk.

### Option C — shared structural-target validation contract (recommended)

- **What changes:** one pure function, for example `structural_target(direction, levels/selected, price, stop_reference, atr, params)`, that both stages call:
  - **T6 (TC6, or a separate checklist item)** asks whether a valid structural target exists:
    - present;
    - finite;
    - strength ≥ `min_level_strength`;
    - on the correct side;
    - clearance ≥ `clearance_atr`.
    - Optionally (a design decision, see §10) a *preliminary* structural R:R from the close and the `L_last`/`H_last` pivot ± buffer, against `min_reward_risk`.
  - **Phase 4** calls the same contract again on the envelope's level, with live bid/ask, tick rounding, then gross and net costs. It stays the final authority, and it re-verifies strength and type, closing the §4.2 3' and 10d gaps.
- **Stage 1 = Option A semantics** (target must exist and be valid), with shared reason codes: `no_structural_target`, `target_strength_below_minimum`, `target_wrong_side`, `invalid_target_evidence`, `structural_reward_risk_below_minimum`.
- **Safety:** tighter than today at both stages. Nothing is relaxed, and Phase 4 keeps every check including 1:2 net.
- **Consistency:** a single definition of "target", so T6 and Phase 4 can't disagree on existence, strength, side or validity again. The remaining differences are explicit and justified: live quote vs close, and costs.
- **Frequency:** same as A for stage 1. If the optional preliminary R:R is adopted, R7 drops further, by candidates Phase 4 would reject with `reward_risk` anyway.
- **SL compatibility:** the contract takes the stop reference as an input and doesn't choose the stop, so Phase 4's structural stop (`L_last`/`H_last` ± 0.25 ATR) is unchanged.
- **1:2 net:** unchanged; still enforced only in Phase 4 with verified costs. T6 can never approve anything.
- **Failure behaviour:** every missing, non-finite or ill-typed input fails closed with a coded reason, and there are no exceptions (the prototype covers NaN and None). Phase 4 should treat a contract failure as `construction/target_structure` failed, not unverified, to keep current semantics.
- **Tests and regressions:**
  - the same fixture migration as A;
  - new contract unit tests (the §4 matrix);
  - a cross-stage property test: any envelope T6 qualifies has a target Phase 4 accepts structurally;
  - persistence tests if evidence fields are added.

## 7. Ranking and recommendation

1. **Option C (staged; stage 1 = A's rule through the shared contract).** It is the only option that removes this inconsistency *and* prevents the next one, and it hardens Phase 4.
2. **Option A.** Safe, minimal and correct for the observed failure, but it leaves the two definitions separate, so they can drift again.
3. **Option B.** Not recommended. It weakens the evidential basis of the take-profit, and it is a strategy change outside this mandate.

Under every option, the following must not change:
- the 1:2 minimum **net** reward:risk;
- the cost-evidence requirement;
- the fail-closed handling.

## 8. Observability recommendations (no change made)

- Persist TC6's `evidence_id` and a non-identifying summary of `selected_level` (low, high, strength, distance in ATR) in `analysis_record`. Without this, the cause of a `target_structure` rejection can't be audited after the fact (§3). Whether a schema change is needed has not been assessed.
- Show TC6's `evidence_id` next to `target_structure` rejections in the Risk and Decision views.

## 9. Secondary findings

| # | Finding | Severity | Notes |
|---|---|---|---|
| S1 | A non-mapping `selected_level` raises `AttributeError` in `_construct` (`risk_engine.py:851`) | Low | The real T6 never emits this (it emits a dict or None). It propagates to the API's generic handler (`api.py:263`, HTTP 500), so no approval results, but no assessment is journaled either. Fix inside Option C with a `Mapping` type check. |
| S2 | Phase 4 doesn't re-check the strength or validity of TC6's level | Low | Trusts T6; not exploitable from real T6 output. Closed by Option C. |
| S3 | TC6 clearance (≥ 1 ATR from close) and Phase 4 gross R:R (≥ 2 from live entry vs structural stop) measure different things | Info | Designed. R7 doesn't mean "1:2 achievable"; candidates with a target can still fail `reward_risk`. Option C's optional preliminary R:R would make this explicit. |
| S4 | Decision evidence lacks TC6 `evidence_id` / `selected_level` | Medium (audit) | See §8. |

## 10. Unresolved issues and decisions for the owner

1. **TC6 semantics change.** Making the no-target branches fail (A, or C stage 1) changes T6 strategy behaviour and the T6 spec §4.1. It needs explicit owner approval; this phase didn't change it.
2. **Preliminary R:R in T6 (C, optional).** T6 doesn't know the live entry, tick size or costs. A close-based pre-check must stay advisory or conservative, and Phase 4 must stay authoritative.
3. **Where the contract lives.** For example `johanna.analysis` (T6 side) imported by `risk_engine`. The direction of dependency needs checking against the current layering.
4. **Fixture migration.** The fixtures `REAL_BUY` and `REAL_SELL` are no-target R7s. They need synthetic qualifying levels, and every dependent test needs review.
5. **Historical branch.** The TC6 branch for the 2 candidates can't be recovered from the journal (§3). Recovering it would need the original market data, which this phase isn't authorised to read.
6. **Price-scale coverage.** JPY (digits 3) behaviour of the contract and construction was not exercised in this phase.
7. **Frequency impact.** It can't be quantified from n = 2 R7. A representative offline sample (an approved archived fixture) would be needed.

## 11. Tests executed and evidence

| Item | Detail |
|---|---|
| Temporary test file | `C:\Users\thato\AppData\Local\Temp\claude\691c6184479ac4f8\2f3a5f1f-2db0-45a6-a173-a889b237ef2d\scratchpad\p627\test_p627_matrix.py` (not in the repository) |
| Captured output | `...\scratchpad\p627\results_py313.txt` |
| Command | `python -m pytest -q -p no:cacheprovider --rootdir . test_p627_matrix.py`, run from the scratchpad with `PYTHONPATH=<repo>\src;<repo>\tests` and `PYTHONDONTWRITEBYTECODE=1` |
| Python 3.13 (`.venv`) | 45 passed, 1 failed (`test_l2_invalid_target_evidence_rejected[not-a-dict]`, finding S1) |
| Python 3.11 (`.venv311`) | 45 passed, 1 failed (same) |
| Data | synthetic only: FakeMT5, fixture envelopes and synthetic cost terms |
| Not done | no access to `data/`, the journal, MT5, the live market, backend APIs or Git; no repository source, test or config change; the existing suite was not modified |
