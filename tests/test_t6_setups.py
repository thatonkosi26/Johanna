"""T6 trend-continuation setup (revision 2.5 §4-§7): TC1-TC6, TC6 evidence cases, vetoes.

Hand-written feature sets (t6_fixtures); the bearish cases use an independent reflection.
"""

from __future__ import annotations

import pytest
from t6_fixtures import base_features, level, pivot, reflect_features, row

from johanna.analysis.conditions import classify_market
from johanna.analysis.features import DecisionParameters
from johanna.analysis.setups import clearance, evaluate_trend_continuation


def evaluate(features, parameters=None):
    p = parameters or DecisionParameters()
    market = classify_market(features, trusted=True, parameters=p)
    return evaluate_trend_continuation(market, features, parameters=p)


def check(result, id_):
    return next(c for c in result["checklist"] if c["id"] == id_)


def veto(result, id_):
    return next(v for v in result["vetoes"] if v["id"] == id_)


# ---------------------------------------------------------------- baseline
@pytest.mark.parametrize("bearish", [False, True])
def test_base_case_qualifies_in_both_directions(bearish) -> None:
    f = reflect_features(base_features()) if bearish else base_features()
    r = evaluate(f)
    assert r["direction"] == ("bearish" if bearish else "bullish")
    assert r["status"] == "qualified", r
    assert [c["result"] for c in r["checklist"]] == ["pass"] * 6
    assert r["triggered_vetoes"] == [] and r["failed_conditions"] == []
    tc6 = check(r, "TC6_clearance")
    side = "support" if bearish else "resistance"
    assert tc6["evidence_id"] == f"{side}_clearance_sufficient"
    assert tc6["observed"] == pytest.approx(2.0)  # (1.0876 - 1.0854) / 0.0011
    tc4 = check(r, "TC4_pullback")
    assert tc4["t"] == "2026-11-11T10:00:00Z" and tc4["observed"] == pytest.approx(0.0001 / 0.00108)


# ---------------------------------------------------------------- individual checks fail alone
def test_tc1_fails_alone_when_not_trending() -> None:
    f = base_features()
    f["latest"]["ADX14"]["value"] = 24.0  # transitional; bias unaffected
    r = evaluate(f)
    assert r["failed_conditions"] == ["TC1_regime"] and r["status"] == "not_qualified"


def test_tc2_neutral_bias_makes_directional_checks_not_applicable() -> None:
    f = base_features()
    f["latest"]["ADX14"]["plus_di"] = f["latest"]["ADX14"]["minus_di"]  # di vote: neither
    r = evaluate(f)
    assert r["direction"] is None and check(r, "TC2_bias")["result"] == "fail"
    assert {
        check(r, i)["result"]
        for i in ("TC3_long_term", "TC4_pullback", "TC5_resumption", "TC6_clearance")
    } == {"not_applicable"}
    assert all(v["triggered"] is None for v in r["vetoes"])


@pytest.mark.parametrize(
    ("ema200", "ema50", "result"),
    [(1.0772, 1.0831, "pass"), (1.0854, 1.0831, "fail"), (1.0831, 1.0831, "fail")],
)
def test_tc3_strict_ema200_alignment(ema200, ema50, result) -> None:
    f = base_features()
    f["latest"]["EMA200"]["value"] = ema200  # 1.0854 == close: strict comparison fails
    f["latest"]["EMA50"]["value"] = ema50
    assert check(evaluate(f), "TC3_long_term")["result"] == result


def test_tc3_unavailable_ema200_makes_the_setup_not_evaluable() -> None:
    f = base_features()
    f["latest"]["EMA200"].update(usable=False, reason="not_converged: 640 candles, 661 needed")
    r = evaluate(f)
    assert check(r, "TC3_long_term")["result"] == "unavailable"
    assert r["status"] == "not_evaluable"
    assert {"input": "EMA200", "reason": "not_converged: 640 candles, 661 needed"} in r[
        "required_inputs"
    ]["unavailable"]


def test_tc4_band_is_inclusive_on_both_sides_and_uses_each_candles_own_values() -> None:
    def single(low, ema20, atr):
        f = base_features()
        rows = [
            row(f"2026-11-11T{h:02d}:00:00Z", 1.09, 1.091, 1.0905, 1.08, 1.07, 0.001)
            for h in range(8, 13)
        ]
        rows[2] = row("2026-11-11T10:00:00Z", low, low + 0.5, low + 0.1, ema20, 0.0, atr)
        f["pullback"]["candles"] = rows
        return check(evaluate(f), "TC4_pullback")

    assert single(1.25, 1.0, 1.0)["result"] == "pass"  # |1.25 - 1.0| / 1.0 == 0.25 exactly
    assert single(0.75, 1.0, 1.0)["result"] == "pass"  # pierces below by exactly 0.25
    assert single(1.2500001, 1.0, 1.0)["result"] == "fail"
    assert single(0.7499999, 1.0, 1.0)["result"] == "fail"  # too deep: the old rule passed it
    # the same 0.25 distance fails against a smaller ATR on that candle
    assert single(1.25, 1.0, 0.9)["result"] == "fail"


def test_tc4_too_deep_low_gives_not_qualified_without_v1() -> None:
    f = base_features()
    f["pullback"]["candles"][2] = row(
        "2026-11-11T10:00:00Z", 1.08390, 1.08540, 1.08450, 1.08460, 1.08300, 0.00110
    )
    r = evaluate(f)
    assert check(r, "TC4_pullback")["result"] == "fail"  # d = 0.0007 / 0.0011 = 0.636
    assert veto(r, "V1_ema50_structure_break")["triggered"] is False
    assert r["failed_conditions"] == ["TC4_pullback"]


def test_tc4_deep_pierce_does_not_invalidate_another_qualifying_candle() -> None:
    f = base_features()
    f["pullback"]["candles"][0] = row(
        "2026-11-11T08:00:00Z", 1.0790, 1.0860, 1.0850, 1.0844, 1.0828, 0.0011
    )
    r = evaluate(f)  # candle 08:00 pierces 4.9 ATR below its EMA20 but closes above EMA50 (U5)
    assert check(r, "TC4_pullback")["result"] == "pass" and r["status"] == "qualified"


def test_tc4_unavailable_window_is_never_a_touch() -> None:
    f = base_features()
    f["pullback"].update(
        usable=False, reason="ATR14 at 2026-11-11T10:00:00Z: non-positive value 0.0"
    )
    r = evaluate(f)
    assert check(r, "TC4_pullback")["result"] == "unavailable" and r["status"] == "not_evaluable"


def test_tc5_requires_a_close_beyond_ema20() -> None:
    f = base_features()
    f["latest"]["EMA20"]["value"] = 1.0854  # equal: strict comparison fails
    assert check(evaluate(f), "TC5_resumption")["result"] == "fail"


# ---------------------------------------------------------------- TC6 evidence cases (§4.1)
def tc6(f, bearish=False):
    g = reflect_features(f) if bearish else f
    return check(evaluate(g), "TC6_clearance")


@pytest.mark.parametrize("bearish", [False, True])
def test_tc6_case0_untrusted_is_unavailable_even_with_populated_lists(bearish) -> None:
    f = base_features()
    f["levels"].update(
        usable=False, reason="not_converged: ATR reference not converged", t5_trusted=False
    )
    c = tc6(f, bearish)
    assert (c["result"], c["evidence_id"]) == ("unavailable", "support_resistance_untrusted")


@pytest.mark.parametrize("bearish", [False, True])
def test_tc6_case1_trusted_empty_side(bearish) -> None:
    f = base_features()
    f["levels"].update(resistance=[], resistance_reason="no confirmed swing highs")
    c = tc6(f, bearish)
    side = "support" if bearish else "resistance"
    assert (c["result"], c["evidence_id"]) == ("pass", f"no_{side}_levels")
    assert "does not establish" in c["detail"]
    assert c["t5"]["side_reason"] == "no confirmed swing highs"


@pytest.mark.parametrize("bearish", [False, True])
def test_tc6_case2_only_strength_one_levels_on_that_side(bearish) -> None:
    f = base_features()
    f["levels"]["resistance"] = [level(1.0856, 1.0857, 1), level(1.0870, 1.0871, 1)]
    c = tc6(f, bearish)
    side = "support" if bearish else "resistance"
    assert (c["result"], c["evidence_id"]) == ("pass", f"no_qualifying_{side}")
    nq = c["nearest_non_qualifying_level"]
    assert nq["strength"] == 1 and nq["distance_atr"] == pytest.approx(0.0002 / 0.0011)


@pytest.mark.parametrize("bearish", [False, True])
def test_tc6_case3_qualifying_levels_only_on_the_wrong_side_of_price(bearish) -> None:
    f = base_features()
    f["levels"]["resistance"] = [level(1.0840, 1.0850, 2)]  # entirely below the close 1.0854
    c = tc6(f, bearish)
    expected = "no_qualifying_support_below" if bearish else "no_qualifying_resistance_above"
    assert (c["result"], c["evidence_id"]) == ("pass", expected)


@pytest.mark.parametrize(
    ("low", "high", "result", "evidence", "inside", "distance"),
    [
        (1.0866, 1.0867, "pass", "sufficient", False, 1.0909091),  # exact 1.0: separate test
        (1.0864999, 1.0866, "fail", "insufficient", False, 0.9999091),
        (1.0850, 1.0860, "fail", "insufficient", True, 0.0),  # close inside the raw interval
        (1.0854, 1.0860, "fail", "insufficient", True, 0.0),  # exactly at low
    ],
)
def test_tc6_cases4_and_5_on_the_raw_interval(
    low, high, result, evidence, inside, distance
) -> None:
    f = base_features()
    f["latest"]["ATR14"]["value"] = 0.0011
    f["levels"]["resistance"] = [level(low, high, 2)]
    c = tc6(f)
    assert (c["result"], c["evidence_id"]) == (result, f"resistance_clearance_{evidence}")
    assert c["price_inside_zone"] is inside
    assert c["observed"] == pytest.approx(distance, abs=1e-6)


def test_tc6_exact_one_atr_boundary_with_representable_numbers() -> None:
    # Called directly: changing the close here would also change the bias votes.
    f = base_features()
    f["price"]["value"] = 1.0
    f["latest"]["ATR14"]["value"] = 0.5
    f["levels"]["resistance"] = [level(1.5, 1.75, 2)]  # (1.5 - 1.0) / 0.5 == 1.0 exactly
    c = clearance(f, "bullish", DecisionParameters())
    assert c["observed"] == 1.0 and c["evidence_id"] == "resistance_clearance_sufficient"
    f["levels"]["resistance"] = [level(1.4999999, 1.75, 2)]
    assert clearance(f, "bullish", DecisionParameters())["evidence_id"] == (
        "resistance_clearance_insufficient"
    )
    # bearish mirror: support high 0.5 below a close of 1.0 with ATR 0.5
    f["levels"]["support"] = [level(0.25, 0.5, 2)]
    assert clearance(f, "bearish", DecisionParameters())["observed"] == 1.0


def test_tc6_raw_interval_versus_expanded_location_zone() -> None:
    # design §3.4 example: raw [1.08500, 1.08520], ATR 0.001, tol 0.00025
    def case(price):
        f = base_features()
        f["price"]["value"] = price
        f["latest"]["ATR14"]["value"] = 0.001
        f["levels"].update(tolerance=0.00025, resistance=[level(1.08500, 1.08520, 2)])
        market = classify_market(f, trusted=True)
        loc = market["dimensions"]["location"]["state"]
        return loc, check(evaluate(f), "TC6_clearance")

    loc, c = case(1.08510)
    assert "at_resistance" in loc and c["observed"] == 0.0 and c["price_inside_zone"] is True
    loc, c = case(1.08490)  # inside the expanded zone, below the raw low
    assert "at_resistance" in loc
    assert c["observed"] == pytest.approx(0.1) and c["price_inside_zone"] is False
    assert c["evidence_id"] == "resistance_clearance_insufficient"
    loc, c = case(1.08530)  # inside the expanded zone, above the raw high: level not selected
    assert "at_resistance" in loc
    assert (c["result"], c["evidence_id"]) == ("pass", "no_qualifying_resistance_above")


def test_tc6_ignores_broken_support_above_price() -> None:
    f = base_features()
    f["levels"]["support"] = [level(1.0828, 1.0832, 3), level(1.0856, 1.0858, 4)]
    assert check(evaluate(f), "TC6_clearance")["evidence_id"] == "resistance_clearance_sufficient"


# ---------------------------------------------------------------- vetoes and cautions
def test_v1_close_below_its_own_ema50_vetoes() -> None:
    f = base_features()
    f["pullback"]["candles"][1] = row(
        "2026-11-11T09:00:00Z", 1.0826, 1.0858, 1.0828, 1.0845, 1.0829, 0.00109
    )
    r = evaluate(f)
    assert veto(r, "V1_ema50_structure_break")["triggered"] is True
    assert veto(r, "V1_ema50_structure_break")["observed"] == ["2026-11-11T09:00:00Z"]
    assert r["status"] == "not_qualified" and r["triggered_vetoes"] == ["V1_ema50_structure_break"]


def test_v2_only_the_complete_opposite_sequence_triggers() -> None:
    for state, triggered in (
        ("lower_highs_lower_lows", True),
        ("mixed", False),
        ("higher_highs_higher_lows", False),
    ):
        f = base_features()
        f["swings"]["state"] = state
        assert veto(evaluate(f), "V2_swing_context_conflict")["triggered"] is triggered
    f = base_features()
    f["swings"].update(state="unavailable", reason="1 confirmed swing low(s), 2 needed")
    r = evaluate(f)
    assert veto(r, "V2_swing_context_conflict")["triggered"] is None
    assert r["status"] == "qualified"  # missing context is not a conflict (U7)
    assert {"input": "swing_sequence", "reason": "1 confirmed swing low(s), 2 needed"} in r[
        "unavailable_context"
    ]


def test_v2_respects_the_context_conflict_switch() -> None:
    f = base_features()
    f["swings"]["state"] = "lower_highs_lower_lows"
    r = evaluate(f, DecisionParameters(veto_on_context_conflict=False))
    assert veto(r, "V2_swing_context_conflict")["triggered"] is False


def test_v2_shows_the_pivots_and_their_times() -> None:
    pivots = veto(evaluate(base_features()), "V2_swing_context_conflict")["pivots"]
    assert [p["role"] for p in pivots] == ["H_prev", "H_last", "L_prev", "L_last"]
    assert all(p["pivot_t"] and p["confirmed_at_utc"] for p in pivots)


@pytest.mark.parametrize(
    ("change", "caution"),
    [
        (lambda f: f["volatility"].update(regime="high"), "volatility_high"),
        (lambda f: f["latest"]["RSI14"].update(value=75.0), "momentum_overbought"),
        (lambda f: f["latest"]["ADX14"].update(value=45.0), "trend_strength_extended"),
    ],
)
def test_cautions_never_veto_or_change_the_checklist(change, caution) -> None:
    base = evaluate(base_features())
    f = base_features()
    change(f)
    r = evaluate(f)
    assert [c["id"] for c in r["cautions"]] == [caution]
    assert r["status"] == "qualified" and r["checklist"] == base["checklist"]


def test_bearish_cautions_mirror() -> None:
    f = reflect_features(base_features())
    f["latest"]["RSI14"]["value"] = 25.0
    assert [c["id"] for c in evaluate(f)["cautions"]] == ["momentum_oversold"]


def test_required_inputs_are_direction_independent() -> None:
    f = base_features()
    f["latest"]["ADX14"]["plus_di"] = f["latest"]["ADX14"]["minus_di"]  # neutral bias
    f["levels"].update(usable=False, reason="insufficient_history: no ATR reference")
    r = evaluate(f)
    assert r["direction"] is None and r["status"] == "not_evaluable"


def test_pivot_helper_shape() -> None:
    assert pivot("H_last", "high", 1.0, "a", "b")["role"] == "H_last"
