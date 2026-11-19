# P6.2b.8 — Shared Structural-Target Contract Implementation

**Date:** 2026-11-19
**Scope:** source, tests and documentation only. Nothing deployed or restarted, no migrations, no production data, no MT5, no Git.

## 1. Executive verdict

**__VERDICT__**

- **The P6.2b.7 inconsistency is fixed.** T6 and Phase 4 now share one definition of a valid structural target: `johanna.analysis.targets.validate_structural_target`.
- **TC6 no longer passes without a target.** Its three no-target branches now fail, so no R7 can be produced without valid target evidence.
- **Phase 4 independently revalidates the target evidence.** It no longer trusts the checklist. Malformed evidence (including the non-mapping case that used to raise `AttributeError` and return HTTP 500) is now a structured `REJECTED` result.
- **Every risk safeguard is unchanged** (§10).

## 2. Files changed and reasons

| File | Change | Reason |
|---|---|---|
| `src/johanna/analysis/targets.py` | **new** | The shared, pure contract with stable reason codes |
| `src/johanna/analysis/setups.py` | TC6 uses the contract; the no-target branches fail; new `opposing_level_check`; new evidence id `*_level_invalid`; `LEVEL_CHECK_CODES` | TC6 changes (§4) |
| `src/johanna/analysis/decision.py` | limitation text now says "TC6 failed …"; `DECISION_VERSION` 1 → 2 | The decision semantics changed; provenance must show it |
| `src/johanna/risk_engine.py` | independent revalidation (`_target_check`); `target_validation` in the result; methodology docstring; `RISK_ENGINE_VERSION` 3 → 4 | Phase 4 changes (§5) |
| `src/johanna/web/modules/overview.js` | Overview wording for the `execution_disabled` notice | Fixes an intermittent B7.2 browser failure (§8). Unrelated to targets, but it blocked clean full runs |
| `tests/t6_fixtures.py` | `trend_bars(target=True)` adds an early plateau that gives a genuine opposing level; `target=False` is the old plain trend | Fixture migration (§6) |
| `tests/risk_fixtures.py` | the real envelopes are genuine R7s with targets; `selected_level=` override with a `NO_TARGET` sentinel; an R7 assertion | Fixture migration (§6) |
| `tests/test_structural_target_contract.py` | **new** (25 test functions, 78 cases) | Contract, TC6 and cross-stage property tests |
| `tests/test_p628_phase4_targets.py` | **new** (22 test functions, 82 cases) | Phase 4, API, symbols, timeframes and the JPY price scale |
| `tests/test_t6_setups.py`, `tests/test_t6_decision.py` | no-target cases now assert `fail` / R6; new real-pipeline R6 test | The behaviour change itself |
| `tests/test_risk_engine.py` | no-target case uses `NO_TARGET`; the purity test allows the two pure analysis imports and checks that the contract module is stdlib-only | Phase 4's new dependency |
| `tests/test_p62a_risk.py` | `NO_TARGET`; version 4 | Version bump |
| `tests/test_shadow_evaluation.py` | `CANDIDATE_NO_TARGET` uses `NO_TARGET`; dedup key `d{DECISION_VERSION}` | Version bump |
| `tests/test_browser_risk.py` | `NO_TARGET` | Fixture migration |
| `tests/test_browser_smoke.py` | waits for the readiness notices before reading the Overview text | Makes the placeholder guard deterministic (§8) |
| `docs/t6-market-conditions-design.md` | revision 2.6: TC6 row, §4.1 case table and rules, new §4.2 contract, §11, change log #23 | Documentation (§7 of the brief) |

## 3. Shared contract design

**Placement and imports:**
- It lives in `src/johanna/analysis/targets.py`, which imports only `math`, `collections.abc`, `numbers` and `typing`. No clock and no I/O, and a test enforces this.
- `johanna.analysis` never imports Phase 4 (checked), so `setups → targets` and `risk_engine → targets` have no cycle.

**Signature and result:**
- Signature: `validate_structural_target(target, *, direction, reference_price, min_strength, atr=None, clearance_atr=None)`.
- It returns `{valid, reason, detail, target, distance_atr}`.

**Checks, in a fixed order (the first failure wins):**
1. **Direction** must be `bullish` or `bearish`. Otherwise `invalid_direction`.
2. **Existence:** `None` gives `no_structural_target`.
3. **Structure:**
   - the target must be a mapping;
   - `low` and `high` must be finite reals (booleans and strings rejected) with `low ≤ high`;
   - `strength` must be a finite real;
   - the reference price must be a finite positive real;
   - the minimum must be a finite real.

   Otherwise `invalid_target_evidence`. Nothing is coerced.
4. **Strength** `≥ min_strength`, otherwise `target_strength_below_minimum`.
5. **Side:** the interval must lie entirely beyond the reference price (bullish `low >` reference, bearish `high <` reference). Otherwise `target_wrong_side`.
6. **Clearance** (only when `clearance_atr` is given): requires a finite positive ATR, and distance `≥ clearance_atr`. Otherwise `target_clearance_insufficient`. A missing or invalid ATR or threshold gives `invalid_target_evidence`.
7. Otherwise `structural_target_valid`.

**Stable codes:** `structural_target_valid`, `no_structural_target`, `target_strength_below_minimum`, `target_wrong_side`, `invalid_target_evidence`, `invalid_direction`, `target_clearance_insufficient`. A test asserts this tuple exactly.

**T6 vocabulary:**
- T6 output must contain no trading vocabulary. The existing `BANNED` words include "target" and "take_profit", and a test enforces this.
- So TC6 records the verdict as `opposing_level_check {valid, code, distance_atr}` using the one-to-one `LEVEL_CHECK_CODES` mapping (`no_opposing_level`, `level_wrong_side` and so on).
- Phase 4 reports the contract codes verbatim.

## 4. TC6 changes

- **The no-target branches now fail:**
  - `no_resistance_levels` / `no_support_levels`;
  - `no_qualifying_resistance` / `no_qualifying_support`;
  - `no_qualifying_resistance_above` / `no_qualifying_support_below`.

  They keep their evidence ids, carry `opposing_level_check.code = no_opposing_level`, and have an updated `detail` ("TC6 fails"). The setup becomes `not_qualified`, and the decision becomes R6 with "failed check: TC6_clearance".
- **A selected level is judged by the contract,** with the clearance requirement and the close as the reference:
  - pass: `*_clearance_sufficient`;
  - too close, inside or touching the zone: `*_clearance_insufficient` (as before);
  - any other contract failure (for example a non-finite T5 boundary): the new `*_level_invalid`.
- **Unchanged:**
  - the clearance threshold (1.0 ATR);
  - `min_level_strength` (2);
  - level selection (`select_clearance_level`, untouched);
  - case 0 (untrusted, unavailable, R5);
  - TC1–TC5, V1, V2, cautions, and every ADX, EMA, ATR, pullback, bias and swing threshold.

  `DecisionParameters` was not touched.
- **No reward:risk filtering in T6.**
- **Edge case (stricter, consistent):** if `clearance_atr` were 0, a level containing the close used to pass with distance 0. It now fails as `level_wrong_side`. With the default of 1.0 the result is identical (a fail).
- `DECISION_VERSION` 2 is recorded in every T6 output and in shadow dedup keys, so evaluations under the old and new rules are distinguishable.

## 5. Phase 4 changes

- **`_target_check`:**
  - It finds the TC6 checklist item defensively: a non-list checklist and non-mapping items are tolerated.
  - It reads `selected_level` directly and calls the contract with the **executable entry** as the reference (ask for BUY, bid for SELL), with no clearance requirement.
  - Minimum strength is T6's default 2, or a stricter (higher, finite) value declared in the envelope's `parameters.decision`. It is never lower.
- **Failures:**
  - `target_wrong_side` → `construction/target_direction` failed (the existing id);
  - every other code → `construction/target_structure` failed (the existing id).
  - The detail names the contract code, and the result carries `target_validation {contract, reference, valid, reason, detail}`.
  - Construction stops: no proposal, no gross R:R, and costs `not_evaluated`. The status is always `REJECTED`, never `UNVERIFIED` and never approved.
- **Afterwards, all unchanged:**
  - tick rounding of SL and TP;
  - stop and target direction checks after rounding;
  - gross R:R ≥ 2;
  - net R:R ≥ 2 with verified costs;
  - sizing, margin, daily risk and expiry.
- **The HTTP 500 path is closed.** A non-mapping `selected_level` now produces `200 OK` with a structured rejection (tested through the WSGI API).
- `RISK_ENGINE_VERSION` 4 is recorded in every risk result. The journal's `risk_engine_version >= 2` constraint is satisfied; no migration.

## 6. Fixture migrations

- **`trend_bars` (`tests/t6_fixtures.py`):**
  - The old fixture was an R7 only because it had no opposing level, which is exactly the shape this phase forbids. With the new TC6 it is R6.
  - The default history (`target=True`) now has a 100-bar flat oscillating plateau at bars `count-640 … count-540`, inside the 661-candle window, placed `drift·count + 0.02` beyond the start.
  - Its repeated swing extremes form a genuine T5 level of **strength 6**, about 27 ATR beyond the close: resistance 1.2622 for BUY, support 0.9378 for SELL.
  - The recent bars are unchanged: close, ATR, `L_last`/`H_last`, pullback and EMAs. So every price-based Phase 4 expectation (for example 1:2.19 with `BUY_LEVEL`) is unchanged.
  - Through the **real** T2→T6 pipeline the result is R7 with `resistance_clearance_sufficient` / `support_clearance_sufficient` for 700 and 720 bars in both directions.
  - `target=False` reproduces the old history and is used to show that the real pipeline now gives R6.
- **`REAL_BUY` / `REAL_SELL`:**
  - These are now genuine R7s with genuine targets. An import-time assertion guards this.
  - A bare `envelope()` therefore now carries a valid target. Its Phase 4 path is approved with the T6-selected take-profit (1.2622 / 0.9378).
- **The old "no target" envelope** is replaced by `envelope(selected_level=NO_TARGET)`. This is a checklist that still claims TC6 `pass` without a level, a shape T6 can no longer produce. It is kept deliberately, to prove Phase 4 rejects independently.
- **What changed in each affected test:** six tests switched from `envelope()` to `NO_TARGET`, and their assertions are unchanged (`target_structure` rejection). No assertion was weakened.
- **Version assertions:** test_shadow_evaluation and test_p62a now use the version constants plus an explicit value, instead of a hard-coded 1 or 3.

## 7. Test coverage and results by Python version

**New tests:**

| Area | Cases |
|---|---|
| **Contract** | valid BUY and SELL; missing target; strength 0, 1 and 1.999 (and the minimum itself); wrong side both directions, including a zone containing or touching the price; missing boundaries (4 cases); `None` boundaries (3); NaN, +inf and −inf in each of low, high and strength (9); unexpected types (9, including bool, list, tuple, set, bytes); numeric strings, booleans and an inverted interval (7); missing strength; invalid direction (7); invalid reference price (7); invalid minimum (3); clearance including the exact 1.0 boundary and invalid ATR (5); fixed check order; purity and no mutation; multiple levels with correct selection |
| **TC6** | no levels, no qualifying level, and none beyond price all **fail**, both directions (6); valid target passes, both directions; below clearance fails, both directions; malformed T5 level gives `resistance_level_invalid`. Existing TC1–TC5, V1, V2 and caution tests pass unmodified |
| **Cross-stage property** | 3,000 deterministic random level sets (seed 628; both directions; ATR 0.0004, 0.0011 and 0.003): every TC6 pass has a selected level that passes the contract with and without clearance, and every failure carries a non-valid code (both branches exercised, more than 300 each). Real pipeline: every R7 (700 and 720 bars, both directions) carries a contract-valid level, and `target=False` is never R7 |
| **Phase 4** | valid BUY and SELL with exact entry, SL and TP; genuine pipeline targets in both directions; SL unchanged for both directions (recomputed from the pivot, 0.25 ATR and tick rounding); missing target; TC6 entry absent; malformed checklist (4); weak target (3); stricter declared strength honoured and a looser one ignored; wrong side (3); malformed mappings (13); NaN and inf (9); checklist claiming `pass` with invalid evidence; API with no 500 (3 shapes); gross failure still `reward_risk`; net failure still `net_reward_risk`; missing costs still UNVERIFIED |
| **Symbols, timeframes, JPY** | EURUSDc, GBPUSDc, USDJPYc × H1, H4, D1 × BUY, SELL: valid targets approved with prices on each instrument's tick (18), and missing or invalid targets rejected (18 × 2 shapes). USDJPY 3-digit rounding: BUY SL 158.2206 → 158.220, TP 161.0004 → 161.000; SELL SL 159.875, TP 157.5004 → 157.501. JPY wrong side and too close (gross < 2) |

**Focused and regression run:** 17 files (the new tests, T6, risk, risk API, corrections, safeguards, shadow, intents, decision journal, journal API, market analysis, browser risk, analysis and smoke).

| Run | Python 3.11 | Python 3.13 |
|---|---|---|
| Focused and regression | **712 passed** | **712 passed** |

**Full suite, three consecutive runs each:**

__FULLRUNS__

## 8. Regression results

- **The first full non-browser run after the source change had 18 failures, all expected:**
  - T6 no-target assertions;
  - the old no-target envelopes;
  - version constants;
  - the risk-module import allowlist;
  - T6's banned-word check, which caught my first draft writing the contract's "target" codes into T6 output. That was fixed with the signal-free mapping, not by changing the test.

  Every one was resolved as described in §6.
- **The B7.2 intermittent (`test_browser_smoke::test_overview_shows_live_mt5_quotes_and_candles`)** failed 2 of 4 in isolation before the fix.
  - **Cause:** once readiness loads, the Overview's operational notices showed the backend reason "broker execution is disabled by design". The test's placeholder guard forbids the phrase "broker execution" on the Overview, so the outcome depended on load timing.
  - **Fix:** the Overview words that notice itself ("Order placement is disabled by design: Johanna is manual-only."), keyed by its stable code. The test now **waits** for the notices, so the guard always covers them. The assertion is unchanged.
  - **Result:** 6 of 6 passes afterwards.
- __REGRESSION_NOTES__

## 9. Ruff, formatting and syntax

- `ruff check src tests scripts`: **All checks passed**.
- `ruff format --check src tests scripts`: **166 files already formatted**.
- `node --check` on `app.js` and every module in `web/modules/`: **no syntax errors**. The only frontend change is `overview.js`.

## 10. Risk safeguards unchanged

| Safeguard | Status | Evidence |
|---|---|---|
| Structural stop-loss (`L_last`/`H_last` ± buffer, rounded away from the entry) | unchanged | code untouched; `test_stop_loss_construction_is_unchanged` |
| Stop buffer 0.25 ATR | unchanged | `RISK_POLICY.stop_buffer_atr == 0.25` asserted |
| Target construction (near edge, rounded towards the entry) | unchanged | exact TP values asserted, BUY, SELL and JPY |
| Gross R:R minimum 2.0 | unchanged | `test_gross_reward_risk_failure_remains_rejected` |
| Net R:R minimum 1:2 | unchanged | `test_net_reward_risk_failure_remains_rejected`; policy untouched |
| Verified cost evidence | unchanged | `test_missing_cost_evidence_remains_unverified` (UNVERIFIED) |
| Margin, volume, daily risk, freshness, expiry, manual execution only | unchanged | code untouched; existing suites pass |
| `RiskPolicy`, `COST_TERMS`, `DecisionParameters` | **not modified** | — |
| Alternative target methods | **none introduced** | the take-profit still comes only from TC6's structural level |

Production `CostTerms` are not needed by the new tests. `PRODUCTION_COSTS` is the existing in-code empty constant, used only to show UNVERIFIED behaviour.

## 11. No production data or operational change

- I did not access, list or read anything under `data/`, the journal, its key, backups, or WAL/SHM files.
- No MT5 or broker connection, no live market data, no production API calls.
- The backend was not started, stopped or restarted. No migration was registered or executed.
- The scheduler, monitoring, reconciliation, notifications and execution were not touched.
- No Git operations.
- All tests use synthetic data, FakeMT5 and temporary directories.

**Deployment note (not done):**
- The running backend still has the old code.
- After a user-controlled restart:
  - new T6 decisions will be `decision_version` 2;
  - new risk results will be `risk_engine_version` 4;
  - shadow dedup keys will change to `…|d2|r4`, so windows are re-keyed rather than re-evaluated twice under the same rules.
- No schema change is involved.

## 12. Remaining limitations and unresolved issues

1. **Candidate frequency:** R7 frequency will fall by the share of no-target candidates. In production that was 2 of 2 historical candidates (n = 2). It can't be quantified further offline.
2. **A setup with a valid target can still fail Phase 4.** It can fail on reward:risk (gross or net), stop direction, or costs. That is by design: TC6 clearance is not reward:risk, and T6 doesn't promise 1:2.
3. **Phase 4's minimum strength comes from T6's default.** A stricter value declared in the envelope is honoured, but RiskPolicy has no separate setting (none was requested).
4. **Decision evidence in the journal** (`analysis_record`) still persists only `{id, result}` per check. TC6's `evidence_id` and `opposing_level_check` aren't journaled (the P6.2b.7 observability item, not in scope). Phase 4's `target_validation` reason is in the risk result. `risk_record` persists only the rejection `[area, id]`.
5. **Fixture realism:** the plateau target in `trend_bars` is synthetic. It is deliberately far (about 27 ATR), so it says nothing about typical production level distances.
6. The **B7.2 report placeholders** (`docs/B72_IMPLEMENTATION_REPORT.md`) are still unfilled from the earlier phase. These full runs also cover the B7.2 tests.
