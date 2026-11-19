"""P6.2b.8 shared structural-target contract (``johanna.analysis.targets``), TC6's use of it, and
the cross-stage property: whatever TC6 qualifies passes the contract Phase 4 applies.

All data synthetic (hand-written feature sets, deterministic seeded generators).
"""

from __future__ import annotations

import copy
import math
import random

import pytest
from t6_fixtures import base_features, level, reflect_features

from johanna.analysis.conditions import classify_market
from johanna.analysis.features import DEFAULT_DECISION_PARAMETERS, select_clearance_level
from johanna.analysis.setups import LEVEL_CHECK_CODES, clearance, evaluate_trend_continuation
from johanna.analysis.targets import (
    DIRECTIONS,
    INVALID_DIRECTION,
    INVALID_TARGET_EVIDENCE,
    NO_STRUCTURAL_TARGET,
    REASON_CODES,
    STRUCTURAL_TARGET_VALID,
    TARGET_CLEARANCE_INSUFFICIENT,
    TARGET_STRENGTH_BELOW_MINIMUM,
    TARGET_WRONG_SIDE,
    validate_structural_target,
)

MIN = DEFAULT_DECISION_PARAMETERS.min_level_strength
BUY_TARGET = {"low": 1.24600, "high": 1.24700, "strength": 3}
SELL_TARGET = {"low": 0.95300, "high": 0.95400, "strength": 3}


def buy(target, price=1.23798, **kwargs):
    return validate_structural_target(
        target, direction="bullish", reference_price=price, min_strength=MIN, **kwargs
    )


def sell(target, price=0.96200, **kwargs):
    return validate_structural_target(
        target, direction="bearish", reference_price=price, min_strength=MIN, **kwargs
    )


# ---------------------------------------------------------------- the contract
def test_reason_codes_are_stable() -> None:
    assert REASON_CODES == (
        "structural_target_valid",
        "no_structural_target",
        "target_strength_below_minimum",
        "target_wrong_side",
        "invalid_target_evidence",
        "invalid_direction",
        "target_clearance_insufficient",
    )
    assert DIRECTIONS == ("bullish", "bearish")
    assert set(LEVEL_CHECK_CODES) == set(REASON_CODES)  # T6 records every outcome, one-to-one
    assert len(set(LEVEL_CHECK_CODES.values())) == len(REASON_CODES)
    assert not any("target" in code for code in LEVEL_CHECK_CODES.values())  # signal-free T6


def test_valid_buy_target() -> None:
    r = buy(BUY_TARGET)
    assert (r["valid"], r["reason"]) == (True, STRUCTURAL_TARGET_VALID)
    assert r["target"] == BUY_TARGET and r["distance_atr"] is None


def test_valid_sell_target() -> None:
    r = sell(SELL_TARGET)
    assert (r["valid"], r["reason"]) == (True, STRUCTURAL_TARGET_VALID)
    assert r["target"] == SELL_TARGET


def test_missing_target() -> None:
    for r in (buy(None), sell(None)):
        assert (r["valid"], r["reason"], r["target"]) == (False, NO_STRUCTURAL_TARGET, None)


@pytest.mark.parametrize("strength", [0, 1, 1.999])
def test_strength_below_minimum(strength) -> None:
    r = buy({**BUY_TARGET, "strength": strength})
    assert (r["valid"], r["reason"]) == (False, TARGET_STRENGTH_BELOW_MINIMUM)
    assert buy({**BUY_TARGET, "strength": MIN})["valid"]  # the minimum itself qualifies


def test_wrong_side_target_both_directions() -> None:
    assert buy({"low": 1.2300, "high": 1.2310, "strength": 3})["reason"] == TARGET_WRONG_SIDE
    assert sell({"low": 0.9700, "high": 0.9710, "strength": 3})["reason"] == TARGET_WRONG_SIDE
    # a zone containing or touching the reference price is not beyond it
    assert buy({"low": 1.2370, "high": 1.2390, "strength": 3})["reason"] == TARGET_WRONG_SIDE
    assert buy({"low": 1.23798, "high": 1.2390, "strength": 3})["reason"] == TARGET_WRONG_SIDE
    assert sell({"low": 0.9610, "high": 0.9620, "strength": 3})["reason"] == TARGET_WRONG_SIDE


@pytest.mark.parametrize(
    "target",
    [
        {"high": 1.247, "strength": 3},  # low missing
        {"low": 1.246, "strength": 3},  # high missing
        {"strength": 3},  # both missing
        {},
    ],
)
def test_missing_price_boundaries(target) -> None:
    assert buy(target)["reason"] == INVALID_TARGET_EVIDENCE


@pytest.mark.parametrize(
    "target",
    [
        {"low": None, "high": 1.247, "strength": 3},
        {"low": 1.246, "high": None, "strength": 3},
        {"low": None, "high": None, "strength": 3},
    ],
)
def test_none_boundaries(target) -> None:
    assert buy(target)["reason"] == INVALID_TARGET_EVIDENCE


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("key", ["low", "high", "strength"])
def test_nan_and_infinity(bad, key) -> None:
    r = buy({**BUY_TARGET, key: bad})
    assert (r["valid"], r["reason"], r["target"]) == (False, INVALID_TARGET_EVIDENCE, None)


@pytest.mark.parametrize(
    "target",
    ["1.246", 1.246, 3, ["low", 1.246], (1.246, 1.247), {1.246, 1.247}, True, object(), b"x"],
)
def test_unexpected_target_types(target) -> None:
    r = buy(target)
    assert (r["valid"], r["reason"]) == (False, INVALID_TARGET_EVIDENCE)


@pytest.mark.parametrize(
    "target",
    [
        {"low": "1.246", "high": 1.247, "strength": 3},  # numeric strings are not coerced
        {"low": 1.246, "high": "1.247", "strength": 3},
        {"low": 1.246, "high": 1.247, "strength": "3"},
        {"low": True, "high": 1.247, "strength": 3},  # booleans are not numbers here
        {"low": 1.246, "high": 1.247, "strength": True},
        {"low": [1.246], "high": 1.247, "strength": 3},
        {"low": 1.247, "high": 1.246, "strength": 3},  # inverted interval
    ],
)
def test_malformed_values_are_never_coerced(target) -> None:
    assert buy(target)["reason"] == INVALID_TARGET_EVIDENCE


def test_missing_strength() -> None:
    assert buy({"low": 1.246, "high": 1.247})["reason"] == INVALID_TARGET_EVIDENCE
    assert buy({"low": 1.246, "high": 1.247, "strength": None})["reason"] == (
        INVALID_TARGET_EVIDENCE
    )


@pytest.mark.parametrize("direction", [None, "", "BUY", "long", "Bullish", 1, ["bullish"]])
def test_invalid_direction(direction) -> None:
    r = validate_structural_target(
        BUY_TARGET, direction=direction, reference_price=1.23798, min_strength=MIN
    )
    assert (r["valid"], r["reason"]) == (False, INVALID_DIRECTION)


@pytest.mark.parametrize("price", [None, math.nan, math.inf, 0, -1.0, "1.23798", True])
def test_invalid_reference_price(price) -> None:
    assert buy(BUY_TARGET, price=price)["reason"] == INVALID_TARGET_EVIDENCE


@pytest.mark.parametrize("minimum", [None, math.nan, "2"])
def test_invalid_minimum_strength(minimum) -> None:
    r = validate_structural_target(
        BUY_TARGET, direction="bullish", reference_price=1.23798, min_strength=minimum
    )
    assert r["reason"] == INVALID_TARGET_EVIDENCE


def test_clearance_requirement() -> None:
    # (1.246 - 1.23798) / 0.001 = 8.02 ATR
    r = buy(BUY_TARGET, atr=0.001, clearance_atr=1.0)
    assert r["valid"] and r["distance_atr"] == pytest.approx(8.02)
    near = buy(BUY_TARGET, atr=0.01, clearance_atr=1.0)  # 0.802 ATR
    assert (near["valid"], near["reason"]) == (False, TARGET_CLEARANCE_INSUFFICIENT)
    assert near["distance_atr"] == pytest.approx(0.802)
    exact = validate_structural_target(
        {"low": 1.5, "high": 1.75, "strength": 2},
        direction="bullish",
        reference_price=1.0,
        min_strength=MIN,
        atr=0.5,
        clearance_atr=1.0,
    )
    assert exact["valid"] and exact["distance_atr"] == 1.0  # the threshold itself passes
    assert sell(SELL_TARGET, atr=0.001, clearance_atr=1.0)["distance_atr"] == pytest.approx(8.0)
    for atr in (None, 0, -0.001, math.nan, "0.001"):
        assert buy(BUY_TARGET, atr=atr, clearance_atr=1.0)["reason"] == INVALID_TARGET_EVIDENCE


def test_check_order_is_fixed() -> None:
    # direction before existence, structure before strength, strength before side
    assert validate_structural_target(None, direction="up", reference_price=1.0, min_strength=MIN)[
        "reason"
    ] == (INVALID_DIRECTION)
    weak_wrong = {"low": 1.2300, "high": 1.2310, "strength": 1}
    assert buy(weak_wrong)["reason"] == TARGET_STRENGTH_BELOW_MINIMUM
    assert buy({**weak_wrong, "high": math.nan})["reason"] == INVALID_TARGET_EVIDENCE


def test_contract_is_pure_and_does_not_mutate_its_input() -> None:
    target = {"low": 1.246, "high": 1.247, "strength": 3, "extra": [1]}
    before = copy.deepcopy(target)
    first, second = buy(target), buy(target)
    assert first == second and target == before
    assert "extra" not in first["target"]


def test_multiple_candidate_levels_select_the_nearest_qualifying_one() -> None:
    levels = {
        "support": [],
        "resistance": [
            level(1.0900, 1.0905, 5),  # strongest, farthest
            level(1.0858, 1.0859, 1),  # nearest, too weak: skipped
            level(1.0870, 1.0872, 2),  # nearest qualifying -> selected
            level(1.0840, 1.0845, 4),  # below price: not a resistance target
        ],
    }
    pick = select_clearance_level(levels, 1.0854, "bullish", MIN)
    assert (pick["selected"]["low"], pick["selected"]["strength"]) == (1.0870, 2)
    assert pick["nearest_non_qualifying"]["low"] == 1.0858
    check = validate_structural_target(
        pick["selected"],
        direction="bullish",
        reference_price=1.0854,
        min_strength=MIN,
        atr=0.0011,
        clearance_atr=1.0,
    )
    assert check["valid"] and check["distance_atr"] == pytest.approx(0.0016 / 0.0011)
    f = base_features()
    f["levels"]["resistance"] = levels["resistance"]
    c = clearance(f, "bullish", DEFAULT_DECISION_PARAMETERS)
    assert c["result"] == "pass" and c["selected_level"]["low"] == 1.0870


# ---------------------------------------------------------------- TC6 uses the contract
def tc6(features, direction):
    return clearance(features, direction, DEFAULT_DECISION_PARAMETERS)


@pytest.mark.parametrize("bearish", [False, True])
def test_tc6_valid_target_with_sufficient_clearance_passes(bearish) -> None:
    f = reflect_features(base_features()) if bearish else base_features()
    c = tc6(f, "bearish" if bearish else "bullish")
    assert c["result"] == "pass"
    assert c["opposing_level_check"]["valid"] is True
    assert c["opposing_level_check"]["code"] == "opposing_level_valid"
    assert c["opposing_level_check"]["distance_atr"] == pytest.approx(c["observed"])


@pytest.mark.parametrize("bearish", [False, True])
def test_tc6_target_below_clearance_fails(bearish) -> None:
    f = base_features()
    f["levels"]["resistance"] = [level(1.0860, 1.0863, 2)]  # 0.545 ATR
    g = reflect_features(f) if bearish else f
    c = tc6(g, "bearish" if bearish else "bullish")
    side = "support" if bearish else "resistance"
    assert (c["result"], c["evidence_id"]) == ("fail", f"{side}_clearance_insufficient")
    assert c["opposing_level_check"]["code"] == "level_clearance_insufficient"
    assert c["selected_level"] is not None  # the level exists; it is too close


@pytest.mark.parametrize(
    ("levels", "evidence"),
    [
        ([], "no_resistance_levels"),
        ([level(1.0860, 1.0862, 1)], "no_qualifying_resistance"),
        ([level(1.0840, 1.0845, 3)], "no_qualifying_resistance_above"),
    ],
)
@pytest.mark.parametrize("bearish", [False, True])
def test_tc6_no_target_fails_symmetrically(levels, evidence, bearish) -> None:
    f = base_features()
    f["levels"]["resistance"] = levels
    g = reflect_features(f) if bearish else f
    c = tc6(g, "bearish" if bearish else "bullish")
    expected = (
        evidence.replace("resistance", "support").replace("above", "below") if bearish else evidence
    )
    assert (c["result"], c["evidence_id"]) == ("fail", expected)
    assert c["opposing_level_check"]["code"] == "no_opposing_level"
    market = classify_market(g, trusted=True)
    setup = evaluate_trend_continuation(market, g)
    assert setup["status"] == "not_qualified" and setup["failed_conditions"] == ["TC6_clearance"]


def test_tc6_malformed_level_evidence_fails_without_exception() -> None:
    f = base_features()
    bad = level(1.0876, 1.0879, 2)
    bad["low"] = math.nan  # a corrupt T5 interval
    f["levels"]["resistance"] = [bad]
    c = tc6(f, "bullish")
    assert (c["result"], c["evidence_id"]) == ("fail", "resistance_level_invalid")
    assert c["opposing_level_check"]["code"] == "invalid_level_evidence"


# ---------------------------------------------------------------- cross-stage property
def _random_levels(rng: random.Random, price: float, atr: float) -> list[dict]:
    out = []
    for _ in range(rng.randint(0, 5)):
        low = price + rng.uniform(-6.0, 6.0) * atr
        out.append(
            level(round(low, 5), round(low + rng.uniform(0, 1.5) * atr, 5), rng.randint(1, 5))
        )
    return out


def test_property_whatever_tc6_qualifies_passes_the_phase4_contract() -> None:
    """Over 3000 deterministic random level sets (both directions, varied ATR): every TC6 pass
    has a selected level, and that level passes the shared contract with and without the
    clearance requirement -- the same contract Phase 4 applies. Every TC6 failure on a trusted
    input is explained by a non-valid contract code."""
    rng = random.Random(628)
    passes = fails = 0
    for i in range(3000):
        bearish = i % 2 == 1
        f = base_features()
        atr = rng.choice([0.0004, 0.0011, 0.003])
        f["latest"]["ATR14"]["value"] = atr
        f["levels"]["resistance"] = _random_levels(rng, 1.0854, atr)
        f["levels"]["support"] = _random_levels(rng, 1.0854, atr)
        g = reflect_features(f) if bearish else f
        direction = "bearish" if bearish else "bullish"
        c = tc6(g, direction)
        price = g["price"]["value"]
        if c["result"] == "pass":
            passes += 1
            assert c["selected_level"] is not None
            for kwargs in ({}, {"atr": atr, "clearance_atr": 1.0}):
                check = validate_structural_target(
                    c["selected_level"],
                    direction=direction,
                    reference_price=price,
                    min_strength=MIN,
                    **kwargs,
                )
                assert check["valid"], (c, check)
        else:
            fails += 1
            assert c["result"] == "fail"
            assert c["opposing_level_check"]["valid"] is False
            assert c["opposing_level_check"]["code"] != "opposing_level_valid"
    assert passes > 300 and fails > 300  # both branches genuinely exercised


@pytest.mark.parametrize("count", [700, 720])  # 1000 ends where TC4 fails: not an R7
@pytest.mark.parametrize("sign", [1, -1])
def test_property_real_pipeline_candidates_carry_contract_valid_targets(sign, count) -> None:
    """Through the real T2 -> T6 pipeline: an R7 always carries a selected level that passes
    the shared contract; without an opposing level (``target=False``) there is no R7."""
    from t6_fixtures import pipeline, trend_bars  # noqa: PLC0415

    from johanna.analysis.decision import evaluate_market  # noqa: PLC0415

    direction = "bullish" if sign == 1 else "bearish"
    r = evaluate_market(*pipeline(trend_bars(count, sign=sign)))
    assert r["decision"]["rule"] == "R7"
    tc6 = next(c for c in r["opportunity"]["checklist"] if c["id"] == "TC6_clearance")
    check = validate_structural_target(
        tc6["selected_level"],
        direction=direction,
        reference_price=r["features"]["price"]["value"],
        min_strength=MIN,
    )
    assert check["valid"], check
    plain = evaluate_market(*pipeline(trend_bars(count, sign=sign, target=False)))
    assert plain["decision"]["rule"] != "R7"
