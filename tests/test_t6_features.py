"""T6 features (revision 2.5 §2): history requirements, per-candle validity, swing sequence.

Real T4 -> T5 inputs come from the existing synthetic histories; expected counts are derived by
hand from the approved definitions (p + n recursive updates), not from the implementation.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

import pytest
from t6_fixtures import pipeline, trend_bars
from test_analysis_engine import history

from johanna.analysis.decision import evaluate_market
from johanna.analysis.features import (
    DecisionParameters,
    required_candles,
    swing_sequence,
    value_validity,
)
from johanna.analysis.indicators import atr_series, ema_series

AS_OF = datetime(2026, 11, 11, 13, 0, tzinfo=UTC)


def features_of(bars, **kwargs):
    return evaluate_market(*pipeline(bars, **kwargs))["features"]


# ---------------------------------------------------------------- history requirements
def test_required_candles_is_the_ema200_total_by_default() -> None:
    # EMA200: 200 seed observations + 461 updates (199/201)^461 < 1 % (T5.1) = 661.
    # Others are smaller: EMA50 lag 166 + 5 = 171; EMA20 window 67 + 4; ATR window 78 + 4.
    assert required_candles() == 661
    assert required_candles(DecisionParameters(slope_lookback=600)) == 166 + 600


def test_lagged_ema50_needs_166_plus_the_slope_lookback() -> None:
    assert features_of(history(170))["slope50"]["usable"] is False
    usable = features_of(history(171))["slope50"]
    assert usable["usable"] is True and usable["lookback"] == 5


def test_pullback_window_needs_a_valid_atr_on_every_candle() -> None:
    # ATR14 first exists at index 14 and converges after 63 updates (index 77, 78 candles);
    # the oldest of the five window candles is index n-5, so all five are valid from n = 82.
    # EMA20 alone would allow n = 71 (67 + 4), so 81 fails only because of ATR_j.
    short = features_of(history(81))["pullback"]
    assert short["usable"] is False
    assert short["reason"].startswith("ATR14 at") and "EMA20" not in short["reason"]
    assert all(r["d_bullish"] is None for r in short["candles"][:1])
    assert features_of(history(82))["pullback"]["usable"] is True


def test_ema200_latest_is_usable_only_from_661_candles() -> None:
    assert features_of(history(660))["latest"]["EMA200"]["usable"] is False
    assert features_of(history(661))["latest"]["EMA200"]["usable"] is True


# ---------------------------------------------------------------- per-candle validity
def test_validity_uses_the_strict_one_percent_style_threshold() -> None:
    series = [1.0] * 10
    # alpha 0.5: seed weight after 2 updates is exactly 0.25 -> not below a 0.25 tolerance.
    assert value_validity(series, 2, 0.5, 0, 0.25) == (
        False,
        "not_converged: seed weight 0.25 >= 0.25",
    )
    assert value_validity(series, 3, 0.5, 0, 0.25) == (True, None)


@pytest.mark.parametrize(
    ("value", "reason_start"),
    [
        (None, "insufficient_history"),
        (math.inf, "non-finite"),
        (math.nan, "non-finite"),
        (0.0, "non-positive"),
        (-0.0001, "non-positive"),
    ],
)
def test_invalid_atr_values_are_never_valid(value, reason_start) -> None:
    series = [1.0] * 9 + [value]
    ok, reason = value_validity(series, 9, 0.5, 0, 0.25, positive=True)
    assert ok is False and reason.startswith(reason_start)


def test_index_outside_the_window_is_invalid() -> None:
    assert value_validity([1.0], 5, 0.5, 0, 0.25)[0] is False
    assert value_validity([1.0], -1, 0.5, 0, 0.25)[0] is False


def test_pullback_rows_use_each_candles_own_ema20_and_atr() -> None:
    bars = trend_bars()
    f = features_of(bars)
    closes = [b["c"] for b in bars]
    highs = [b["h"] for b in bars]
    lows = [b["l"] for b in bars]
    e20 = ema_series(closes, 20)
    atr = atr_series(highs, lows, closes, 14)
    rows = f["pullback"]["candles"]
    assert [r["t"] for r in rows] == [b["t"] for b in bars[-5:]]  # last five, latest included
    for offset, r in enumerate(rows, start=len(bars) - 5):
        assert r["ema20"] == e20[offset] and r["atr"] == atr[offset]
        assert r["d_bullish"] == abs(lows[offset] - e20[offset]) / atr[offset]
        assert r["d_bearish"] == abs(highs[offset] - e20[offset]) / atr[offset]
    assert rows[0]["ema20"] != f["latest"]["EMA20"]["value"]  # not the latest value


def test_price_reference_is_the_last_closed_candle_not_the_live_tick() -> None:
    bars = trend_bars()
    base = evaluate_market(*pipeline(bars))
    odd_tick = {
        "bid": 9.0,
        "ask": 9.1,
        "time_utc": "2026-11-11T13:59:59.500000Z",  # E1: the fixture tick time (T1-consistent)
        "retrieved_at_utc": "2026-11-11T14:00:30Z",
    }
    moved = evaluate_market(*pipeline(bars, tick=odd_tick))
    assert base["inputs"]["price_reference"] == {
        "source": "last_closed_candle_close",
        "t": bars[-1]["t"],
        "value": bars[-1]["c"],
    }
    assert moved["inputs"]["price_reference"] == base["inputs"]["price_reference"]
    assert moved["decision"] == base["decision"]


def test_as_of_is_the_close_of_the_last_window_candle() -> None:
    f = features_of(trend_bars())
    assert (f["price"]["t"], f["as_of_utc"]) == ("2026-11-11T12:00:00Z", "2026-11-11T13:00:00Z")


# ---------------------------------------------------------------- swing sequence
def piv(kind, price, hour, confirmed_hour=None):
    t = f"2026-11-11T{hour:02d}:00:00Z"
    c = f"2026-11-11T{(confirmed_hour if confirmed_hour is not None else hour + 3):02d}:00:00Z"
    return {"type": kind, "price": price, "pivot_t": t, "confirmed_at_utc": c}


@pytest.mark.parametrize(
    ("highs", "lows", "state"),
    [
        ((1.0, 1.1), (0.5, 0.6), "higher_highs_higher_lows"),
        ((1.1, 1.0), (0.6, 0.5), "lower_highs_lower_lows"),
        ((1.0, 1.0), (0.5, 0.6), "mixed"),  # equal highs
        ((1.0, 1.1), (0.6, 0.5), "mixed"),  # expanding
        ((1.1, 1.0), (0.5, 0.6), "mixed"),  # contracting
    ],
)
def test_swing_sequence_states(highs, lows, state) -> None:
    confirmed = [
        piv("high", highs[0], 1),
        piv("low", lows[0], 2),
        piv("high", highs[1], 4),
        piv("low", lows[1], 5),
    ]
    result = swing_sequence({"confirmed": confirmed, "pending": []}, True, None, AS_OF)
    assert result["state"] == state
    assert [p["role"] for p in result["pivots"]] == ["H_prev", "H_last", "L_prev", "L_last"]


def test_swing_sequence_unavailable_lists_found_pivots_and_count_needed() -> None:
    confirmed = [piv("high", 1.0, 1), piv("high", 1.1, 4), piv("low", 0.5, 2)]
    result = swing_sequence({"confirmed": confirmed, "pending": []}, True, None, AS_OF)
    assert result["state"] == "unavailable"
    assert result["reason"] == "1 confirmed swing low(s), 2 needed"
    assert [p["role"] for p in result["pivots"]] == ["H_prev", "H_last", "L_last"]


def test_pending_pivots_never_complete_a_sequence() -> None:
    confirmed = [piv("high", 1.1, 1), piv("high", 1.0, 4), piv("low", 0.6, 2)]
    pending = [
        {"type": "low", "price": 0.5, "pivot_t": "2026-11-11T11:00:00Z", "confirmed_at_utc": None}
    ]
    result = swing_sequence({"confirmed": confirmed, "pending": pending}, True, None, AS_OF)
    assert result["state"] == "unavailable" and result["pending_ignored"] == 1


def test_untrusted_swings_are_unavailable() -> None:
    result = swing_sequence(
        {"confirmed": [], "pending": []}, False, "insufficient_history: x", AS_OF
    )
    assert (result["state"], result["reason"]) == ("unavailable", "insufficient_history: x")
