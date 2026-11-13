"""T6 market-condition classification (revision 2.5 §3): boundaries, votes, location zones.

Inputs are hand-written feature sets (t6_fixtures.base_features), so every expected value is
stated explicitly rather than recomputed with the implementation's formulas.
"""

from __future__ import annotations

import pytest
from t6_fixtures import base_features, level

from johanna.analysis.conditions import classify_market
from johanna.analysis.features import DecisionParameters


def dims(features, trusted=True):
    return classify_market(features, trusted=trusted)["dimensions"]


def with_adx(adx=None, slope=None, plus=None, minus=None):
    f = base_features()
    if adx is not None:
        f["latest"]["ADX14"]["value"] = adx
    if slope is not None:
        f["slope50"]["value"] = slope
    if plus is not None:
        f["latest"]["ADX14"]["plus_di"] = plus
    if minus is not None:
        f["latest"]["ADX14"]["minus_di"] = minus
    return f


# ---------------------------------------------------------------- trend regime
@pytest.mark.parametrize(
    ("adx", "slope", "state"),
    [
        (25.0, 0.2500001, "trending"),  # ADX >= 25 inclusive, |slope| > 0.25 exclusive
        (25.0, -0.73, "trending"),  # direction-free: abs(slope)
        (24.999, 0.73, "transitional"),
        (25.0, 0.25, "transitional"),  # |slope| exactly 0.25 is flat
        (20.0, 0.25, "ranging"),  # ADX <= 20 inclusive and |slope| <= 0.25 inclusive
        (20.0, -0.25, "ranging"),
        (20.001, 0.1, "transitional"),
        (19.0, 0.2500001, "transitional"),
        (40.0, 0.0, "transitional"),  # strong ADX with a flat EMA50
    ],
)
def test_trend_regime_boundaries(adx, slope, state) -> None:
    d = dims(with_adx(adx, slope))["trend_regime"]
    assert d["state"] == state and d["role"] == "core"
    if state == "transitional":
        assert d["supporting"] == [] and len(d["contradicting"]) == 2


@pytest.mark.parametrize("missing", ["ADX14", "slope50"])
def test_trend_regime_undetermined_when_an_input_is_unusable(missing) -> None:
    f = base_features()
    target = f["slope50"] if missing == "slope50" else f["latest"]["ADX14"]
    target["usable"], target["reason"] = False, "not_converged: test"
    d = dims(f)["trend_regime"]
    assert d["state"] == "undetermined" and d["trusted"] is False
    assert d["unavailable"] == [{"input": missing, "reason": "not_converged: test"}]


# ---------------------------------------------------------------- directional bias
def bias_case(price, e20, e50, plus, minus, slope):
    f = base_features()
    f["price"]["value"] = price
    f["latest"]["EMA20"]["value"] = e20
    f["latest"]["EMA50"]["value"] = e50
    f["latest"]["ADX14"]["plus_di"], f["latest"]["ADX14"]["minus_di"] = plus, minus
    f["slope50"]["value"] = slope
    return dims(f)["directional_bias"]


@pytest.mark.parametrize(
    ("args", "state", "votes"),
    [
        ((1.2, 1.15, 1.1, 30, 10, 0.5), "bullish", ["bullish"] * 4),
        ((1.0, 1.05, 1.1, 10, 30, -0.5), "bearish", ["bearish"] * 4),
        # three bullish votes and one bearish vote: conflicting, never a majority
        (
            (1.2, 1.15, 1.1, 10, 30, 0.5),
            "conflicting",
            ["bullish", "bullish", "bearish", "bullish"],
        ),
        # some neither, none opposing: neutral
        ((1.2, 1.15, 1.1, 30, 10, 0.25), "neutral", ["bullish", "bullish", "bullish", "neither"]),
        ((1.1, 1.1, 1.1, 20, 20, 0.0), "neutral", ["neither"] * 4),  # equality votes neither
        (
            (1.1, 1.15, 1.1, 30, 30, -0.2500001),
            "conflicting",
            ["neither", "bullish", "neither", "bearish"],
        ),
    ],
)
def test_directional_bias_votes(args, state, votes) -> None:
    d = bias_case(*args)
    assert d["state"] == state
    assert [v["vote"] for v in d["votes"]] == votes
    assert [v["id"] for v in d["votes"]] == [
        "price_vs_ema50",
        "ema20_vs_ema50",
        "di_balance",
        "ema50_slope",
    ]
    if state == "conflicting":
        assert d["supporting"] == [] and all(v["vote"] != "neither" for v in d["contradicting"])


def test_bias_undetermined_when_any_vote_input_is_unusable() -> None:
    for name in ("EMA20", "EMA50", "ADX14"):
        f = base_features()
        f["latest"][name]["usable"] = False
        assert dims(f)["directional_bias"]["state"] == "undetermined"
    f = base_features()
    f["slope50"]["usable"] = False
    assert dims(f)["directional_bias"]["state"] == "undetermined"


# ---------------------------------------------------------------- context dimensions
@pytest.mark.parametrize(
    ("adx", "state"),
    [
        (19.999, "weak"),
        (20.0, "moderate"),
        (24.999, "moderate"),
        (25.0, "strong"),
        (40.0, "strong"),
        (40.001, "extended"),
    ],
)
def test_trend_strength_bands(adx, state) -> None:
    assert dims(with_adx(adx))["trend_strength"]["state"] == state


@pytest.mark.parametrize(
    ("rsi", "state"),
    [(70.0, "overbought"), (69.999, "neutral"), (30.0, "oversold"), (30.001, "neutral")],
)
def test_momentum_bands(rsi, state) -> None:
    f = base_features()
    f["latest"]["RSI14"]["value"] = rsi
    assert dims(f)["momentum"]["state"] == state


@pytest.mark.parametrize("regime", ["low", "normal", "high"])
def test_volatility_reuses_the_t5_regime(regime) -> None:
    f = base_features()
    f["volatility"]["regime"] = regime
    d = dims(f)["volatility"]
    assert d["state"] == regime and d["role"] == "context"


def test_unusable_context_is_undetermined_not_fabricated() -> None:
    f = base_features()
    f["volatility"]["usable"], f["volatility"]["reason"] = False, "not_converged: x"
    f["latest"]["RSI14"]["usable"] = False
    d = dims(f)
    assert d["volatility"]["state"] == "undetermined"
    assert d["volatility"]["unavailable"] == [
        {"input": "volatility_regime", "reason": "not_converged: x"}
    ]
    assert d["momentum"]["state"] == "undetermined"


def test_provisional_classification_is_never_trusted() -> None:
    d = dims(base_features(), trusted=False)
    assert all(not x["trusted"] for x in d.values())
    assert d["trend_regime"]["state"] == "trending"  # still computed for diagnosis


# ---------------------------------------------------------------- location (expanded zones)
def loc(price, support, resistance, atr=0.001, tol=0.00025, **level_extra):
    f = base_features()
    f["price"]["value"] = price
    f["latest"]["ATR14"]["value"] = atr
    f["levels"].update(tolerance=tol, support=support, resistance=resistance, **level_extra)
    return dims(f)["location"]


def test_price_inside_overlapping_support_and_resistance_reports_both() -> None:
    # S zone [1.0995, 1.1015], R zone [1.1005, 1.1025]; price 1.1010 is in both.
    d = loc(1.1010, [level(1.0998, 1.1012, 2)], [level(1.1008, 1.1022, 2)], tol=0.0003)
    assert d["state"] == ["at_support", "at_resistance"]
    assert d["zones_containing_price"] == ["R1", "S1"]
    assert d["overlaps"] == [["R1", "S1"]]


def test_three_mutually_overlapping_zones_report_every_pair() -> None:
    d = loc(
        1.2000,
        [level(1.1000, 1.1002, 2), level(1.1003, 1.1005, 2)],
        [level(1.1004, 1.1006, 3)],
        tol=0.0003,
    )
    assert sorted(map(tuple, d["overlaps"])) == [("R1", "S1"), ("R1", "S2"), ("S1", "S2")]
    assert d["state"] == ["above_all_resistance"]


def test_inside_one_zone_and_near_another_reports_the_distance() -> None:
    # price 1.1000 inside S2 [1.0995, 1.1005]; R1 zone starts at 1.1004 + ... -> 0.4 ATR above
    d = loc(
        1.1000,
        [level(1.0900, 1.0905, 2), level(1.09975, 1.10025, 2)],
        [level(1.10065, 1.1010, 2)],
        atr=0.001,
        tol=0.00025,
    )
    assert d["state"] == ["at_support", "between_levels"]
    assert d["zones_containing_price"] == ["S2"]
    assert d["nearest_above"]["id"] == "R1"
    assert d["nearest_above"]["distance_atr"] == pytest.approx(0.4)
    assert d["nearest_below"]["id"] == "S1"


def test_zone_edges_are_inclusive() -> None:
    # zone [1.0, 1.5] expanded by 0.25 -> [0.75, 1.75]; price exactly on both edges
    for price in (0.75, 1.75):
        d = loc(price, [level(1.0, 1.5, 2)], [], atr=1.0, tol=0.25, resistance_reason="r")
        assert d["state"][0] == "at_support"
    assert loc(1.75 + 1e-9, [level(1.0, 1.5, 2)], [], atr=1.0, tol=0.25)["state"] == []


def test_strength_one_levels_never_form_zones() -> None:
    d = loc(1.1, [level(1.09, 1.091, 1)], [level(1.11, 1.111, 1)])
    assert d["state"] == ["no_levels"] and d["zones"] == []
    assert len(d["non_qualifying_levels"]) == 2


def test_location_undetermined_when_levels_untrusted() -> None:
    f = base_features()
    f["levels"]["usable"], f["levels"]["reason"] = False, "insufficient_history: no ATR reference"
    d = dims(f)["location"]
    assert d["state"] == ["undetermined"] and d["trusted"] is False


def test_zone_ids_are_stable_under_input_permutation() -> None:
    a = [level(1.09, 1.091, 2, "2026-11-01T00:00:00Z"), level(1.08, 1.081, 2)]
    d1 = loc(1.1, a, [])
    d2 = loc(1.1, list(reversed(a)), [])
    assert d1["zones"] == d2["zones"] and d1["zones"][0]["low"] == 1.08


def test_swing_structure_unavailable_is_reported_not_conflicting() -> None:
    f = base_features()
    f["swings"].update(state="unavailable", reason="1 confirmed swing low(s), 2 needed")
    d = dims(f)["swing_structure"]
    assert d["state"] == "unavailable"
    assert d["unavailable"] == [
        {"input": "swing_sequence", "reason": "1 confirmed swing low(s), 2 needed"}
    ]


def test_custom_parameters_move_the_boundaries() -> None:
    p = DecisionParameters(adx_trend_min=30.0)
    f = with_adx(29.4)
    assert classify_market(f, trusted=True, parameters=p)["dimensions"]["trend_regime"][
        "state"
    ] == ("transitional")
    with pytest.raises(ValueError):
        DecisionParameters(adx_range_max=26.0)
