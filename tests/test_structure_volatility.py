"""T5 market structure (swings, support/resistance) and volatility regime: hand-built cases."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from johanna.analysis.structure import cluster_levels, find_swings, support_resistance
from johanna.analysis.volatility import classify, percentile_rank, volatility_regime

T0 = datetime(2026, 11, 10, 0, 0, tzinfo=UTC)


def candles(highs, lows=None):
    lows = lows if lows is not None else [h - 0.5 for h in highs]
    return [
        {"t": (T0 + timedelta(hours=i)).isoformat().replace("+00:00", "Z"), "h": h, "l": lo}
        for i, (h, lo) in enumerate(zip(highs, lows, strict=True))
    ]


def t(i: int) -> str:
    return (T0 + timedelta(hours=i)).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------- swings
def test_known_swing_high_and_confirmation_time() -> None:
    result = find_swings(candles([1, 2, 5, 3, 2, 1, 4]), 3600, 2)
    highs = [p for p in result["confirmed"] if p["type"] == "high"]
    assert [(p["index"], p["price"]) for p in highs] == [(2, 5)]
    pivot = highs[0]
    assert pivot["pivot_t"] == t(2)
    assert pivot["confirmed_by_t"] == t(4)
    assert pivot["confirmed_at_utc"] == t(5)  # close of the 2nd following candle, not t(2)
    assert pivot["left_t"] == [t(0), t(1)] and pivot["right_t"] == [t(3), t(4)]


def test_known_swing_low_and_pending_pivot() -> None:
    lows = [5, 4, 1, 3, 4, 2, 6]
    result = find_swings(candles([lo + 1 for lo in lows], lows), 3600, 2)
    confirmed_lows = [p for p in result["confirmed"] if p["type"] == "low"]
    assert [(p["index"], p["price"]) for p in confirmed_lows] == [(2, 1)]
    # Index 5 (low 2) has only one following candle: pending, never treated as known.
    (pending,) = [p for p in result["pending"] if p["type"] == "low"]
    assert pending["index"] == 5 and pending["confirmed_at_utc"] is None
    assert pending["confirmation_status"] == "pending" and pending["candles_still_needed"] == 1


@pytest.mark.parametrize(
    ("highs", "pivots"),
    [
        ([1, 2, 5, 5, 3, 2], [2]),  # plateau: only the first candle qualifies
        ([1, 2, 5, 3, 5, 2, 1], [2]),  # equal later high: left side not strictly lower
        ([1, 2, 5, 5, 5, 2, 1], [2]),  # long plateau
        ([3, 3, 3, 3, 3, 3], []),  # flat: no pivots
    ],
)
def test_equal_price_ties_do_not_create_duplicate_pivots(highs, pivots) -> None:
    result = find_swings(candles(highs, [0.0] * len(highs)), 3600, 2)
    assert [
        p["index"] for p in result["confirmed"] + result["pending"] if p["type"] == "high"
    ] == pivots


def test_insufficient_future_candles_never_confirm() -> None:
    result = find_swings(candles([1, 2, 3, 4, 9, 2]), 3600, 2)
    assert [p for p in result["confirmed"] if p["type"] == "high"] == []
    assert [p["index"] for p in result["pending"] if p["type"] == "high"] == [4]


def test_future_candles_cannot_make_a_pivot_known_earlier() -> None:
    base = [1, 2, 3, 2, 4, 6, 5]  # index 5 pending (one candle after it)
    prefix = find_swings(candles(base), 3600, 2)
    assert [p["index"] for p in prefix["pending"] if p["type"] == "high"] == [5]
    for future in ([4], [5.5], [7], [6]):
        full = find_swings(candles(base + future), 3600, 2)
        for pivot in full["confirmed"]:
            # Confirmed only by a candle after the prefix: never at or before the prefix end.
            if pivot["index"] == 5:
                assert pivot["confirmed_at_utc"] == t(8)
            assert pivot["confirmed_at_utc"] > pivot["pivot_t"]
        # Pivots already confirmed in the prefix are unchanged by any future candles.
        prefix_confirmed = {
            (p["type"], p["index"], p["confirmed_at_utc"]) for p in prefix["confirmed"]
        }
        full_confirmed = {(p["type"], p["index"], p["confirmed_at_utc"]) for p in full["confirmed"]}
        assert prefix_confirmed <= full_confirmed


def test_swings_are_deterministic_and_validate_k() -> None:
    data = candles([1, 3, 2, 5, 1, 4, 2, 6, 3, 1])
    assert len({json.dumps(find_swings(data, 3600, 2), sort_keys=True) for _ in range(30)}) == 1
    with pytest.raises(ValueError):
        find_swings(data, 3600, 0)


# ---------------------------------------------------------------- support / resistance
def pivot(kind, price, i):
    return {"type": kind, "price": price, "pivot_t": t(i), "confirmed_at_utc": t(i + 3)}


def test_clustering_boundary_is_inclusive_at_exactly_025_atr() -> None:
    # ATR 1.0 -> tolerance 0.25 (exact in binary); 1.25 - 1.0 == 0.25 joins, 1.2500001 does not.
    lows = [
        pivot("low", 1.0, 1),
        pivot("low", 1.25, 5),
        pivot("low", 1.2500001, 9),
        pivot("low", 2.0, 13),
    ]
    levels = cluster_levels(lows, 0.25)
    assert [lvl["strength"] for lvl in levels] == [2, 1, 1]
    assert levels[0]["level"] == 1.125 and (levels[0]["low"], levels[0]["high"]) == (1.0, 1.25)
    assert [p["price"] for p in levels[0]["pivots"]] == [1.0, 1.25]


def test_support_and_resistance_are_kept_separate() -> None:
    swings = {
        "confirmed": [
            pivot("low", 1.10, 1),
            pivot("high", 1.12, 2),
            pivot("low", 1.11, 4),
            pivot("high", 1.13, 6),
            pivot("high", 1.30, 8),
        ]
    }
    result = support_resistance(swings, atr_reference=0.08)  # tolerance 0.02
    assert result["status"] == "available" and result["tolerance"] == pytest.approx(0.02)
    assert [lvl["strength"] for lvl in result["support"]] == [2]
    assert [lvl["strength"] for lvl in result["resistance"]] == [2, 1]
    assert all(p["price"] in (1.10, 1.11) for lvl in result["support"] for p in lvl["pivots"])


def test_level_availability_comes_from_its_pivots_confirmations() -> None:
    levels = cluster_levels([pivot("high", 1.5, 2), pivot("high", 1.51, 20)], 0.05)
    (level,) = levels
    assert level["first_confirmed_at_utc"] == t(5) and level["latest_confirmed_at_utc"] == t(23)


def test_insufficient_pivots_or_atr_are_explicit() -> None:
    assert support_resistance({"confirmed": []}, 0.01)["status"] == "insufficient_history"
    no_atr = support_resistance({"confirmed": [pivot("low", 1.0, 1)]}, None)
    assert no_atr["status"] == "insufficient_history" and no_atr["reason"] == "no ATR reference"
    only_lows = support_resistance({"confirmed": [pivot("low", 1.0, 1)]}, 0.1)
    assert only_lows["status"] == "available" and only_lows["resistance"] == []
    assert only_lows["resistance_reason"] == "no confirmed swing highs"


def test_clustering_is_deterministic() -> None:
    pivots = [pivot("low", p, i) for i, p in enumerate([1.3, 1.1, 1.2, 1.1, 1.25, 1.05])]
    outputs = {json.dumps(cluster_levels(pivots, 0.1), sort_keys=True) for _ in range(30)}
    assert len(outputs) == 1


# ---------------------------------------------------------------- volatility regime
REFERENCE = [float(v) for v in range(1, 101)]  # 1..100


@pytest.mark.parametrize(
    ("current", "percentile", "regime"),
    [
        (10.0, 9.5, "low"),  # 9 below, 1 equal
        (25.0, 24.5, "low"),  # just below 25
        (25.5, 25.0, "normal"),  # exactly 25th: inclusive
        (50.5, 50.0, "normal"),
        (75.5, 75.0, "normal"),  # exactly 75th: inclusive
        (76.0, 75.5, "high"),  # just above 75
        (200.0, 100.0, "high"),
    ],
)
def test_percentile_boundaries(current, percentile, regime) -> None:
    result = volatility_regime([None] * 14 + REFERENCE + [current], [True] * 115)
    assert result["percentile"] == pytest.approx(percentile)
    assert result["regime"] == regime and result["status"] == "available"


def test_ties_use_mid_rank() -> None:
    assert percentile_rank([5.0] * 100, 5.0) == 50.0
    assert percentile_rank([1.0, 2.0, 2.0, 3.0], 2.0) == 50.0
    assert classify(25.0) == "normal" and classify(75.0) == "normal"
    assert classify(24.999) == "low" and classify(75.001) == "high"


def test_insufficient_and_unconverged_history() -> None:
    short = volatility_regime(REFERENCE, [True] * 100)  # 100 values: no current + 100 reference
    assert short["status"] == "insufficient_history" and short["regime"] is None
    flags = [False] * 50 + [True] * 51
    unconverged = volatility_regime(REFERENCE + [60.0], flags)
    assert unconverged["status"] == "not_converged" and unconverged["regime"] is None
    assert unconverged["percentile"] is not None  # diagnostic only


def test_reference_window_uses_only_preceding_observations() -> None:
    values = [None] * 3 + [float(v) for v in range(1, 151)]
    result = volatility_regime(values, [True] * len(values))
    assert result["current_index"] == len(values) - 1
    assert (result["reference_first_index"], result["reference_last_index"]) == (
        len(values) - 101,
        len(values) - 2,
    )
    # The current value (150) is above all 100 preceding values (50..149) -> 100th percentile.
    assert result["percentile"] == 100.0
