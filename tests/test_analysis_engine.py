"""T5 engine: validation gate, statuses, output contract, determinism, immutability.

Candle histories follow the real T3 schedule (slots entirely inside configured closures are
skipped), so windows pass the real T4/T4.1 ``validate_window`` before analysis.
"""

from __future__ import annotations

import copy
import json
import random
import time
from datetime import UTC, datetime, timedelta

import pytest
from test_window_validation import candle, snapshot

from johanna.analysis.engine import DEFAULT_PARAMETERS, analyse_window
from johanna.analysis.indicators import ema_series, updates_to_converge
from johanna.analysis.sessions import EXNESS_T0_PROVISIONAL
from johanna.analysis.window import session_candle_check, validate_window


def history(
    count: int, *, end: datetime = datetime(2026, 11, 11, 13, 0, tzinfo=UTC), seed: int = 1
) -> list[dict]:
    """``count`` H1 candles ending at ``end`` (exclusive of closed slots), deterministic walk."""
    times = []
    moment = end
    while len(times) < count:
        moment -= timedelta(hours=1)
        if not session_candle_check([moment], 3600, EXNESS_T0_PROVISIONAL)["unexpected"]:
            times.append(moment)
    times.reverse()
    rng = random.Random(seed)
    price, out = 1.1, []
    for moment in times:
        open_ = price
        close = round(open_ + rng.uniform(-0.002, 0.002), 5)
        high = round(max(open_, close) + rng.uniform(0, 0.001), 5)
        low = round(min(open_, close) - rng.uniform(0, 0.001), 5)
        out.append(candle(moment, o=open_, h=high, low=low, c=close))
        price = close
    return out


def analysed(bars, required=None, **kwargs):
    snap = snapshot(bars)
    validation = validate_window(snap, required_count=required or len(bars))
    return snap, validation, analyse_window(snap, validation, **kwargs)


# ---------------------------------------------------------------- gate and statuses
def test_accepted_long_window_is_fully_available_and_trusted() -> None:
    snap, validation, result = analysed(history(700))
    assert validation["verdict"]["state"] == "accepted", validation["verdict"]
    assert result["status"] == "available"
    assert set(result["component_statuses"].values()) == {"available"}
    assert result["trusted_components"] == list(result["component_statuses"])
    assert result["validation"] == validation  # original T4/T4.1 result kept intact
    ema200 = result["indicators"]["ema"][2]
    assert ema200["convergence"]["observations_required"] == 661
    assert ema200["convergence"]["seed_weight"] < 0.01 and ema200["trusted"] is True
    assert result["volatility"]["regime"] in ("low", "normal", "high")
    assert result["analysed_at_utc"] == validation["evaluated_at_utc"]


def test_warm_up_and_convergence_are_separate() -> None:
    _, _, partial = analysed(history(300))
    assert partial["status"] == "partial"
    ema200 = partial["indicators"]["ema"][2]
    assert ema200["warmup"]["satisfied"] is True and ema200["status"] == "not_converged"
    assert ema200["value"] is not None and ema200["trusted"] is False
    assert ema200["convergence"]["updates"] == 100
    assert partial["indicators"]["ema"][0]["status"] == "available"
    _, _, short = analysed(history(150))
    assert short["indicators"]["ema"][2]["status"] == "insufficient_history"
    assert short["indicators"]["ema"][2]["value"] is None
    assert short["volatility"]["status"] == "not_converged"  # 136 ATRs, only 73 converged
    _, _, tiny = analysed(history(20))
    assert tiny["indicators"]["adx"]["status"] == "insufficient_history"
    assert tiny["volatility"]["status"] == "insufficient_history"


def test_provisional_window_is_diagnostic_only() -> None:
    bars = history(700)
    bars[-1] = candle(
        bars[-1]["t"],
        o=bars[-1]["o"],
        h=bars[-1]["h"],
        low=bars[-1]["l"],
        c=bars[-1]["c"],
        closure="clock_only",
    )
    snap, validation, result = analysed(bars)
    assert validation["verdict"]["state"] == "provisional"
    assert result["status"] == "provisional"
    assert result["trusted_components"] == []
    assert all(not e["trusted"] for e in result["indicators"]["ema"])
    assert result["indicators"]["rsi"]["value"] is not None  # calculated for diagnosis
    assert {r["code"] for r in result["validation_reasons"]} == {"clock_only_closure"}


def test_rejected_window_produces_no_results() -> None:
    bars = history(50)
    bars[10] = candle(bars[10]["t"], issues=["non_positive_price"])
    _, validation, result = analysed(bars)
    assert result["status"] == "rejected"
    assert result["indicators"] is None and result["structure"] is None
    assert result["volatility"] is None and result["trusted_components"] == []
    assert result["validation_reasons"] == validation["verdict"]["reasons"]


def test_invalid_input_and_unsupported_timeframe() -> None:
    snap, validation, _ = analysed(history(30))
    other = dict(snap, snapshot_id="different")
    assert analyse_window(other, validation)["status"] == "invalid_input"
    broken = copy.deepcopy(validation)
    broken["window"]["candles"].append("2030-01-01T00:00:00Z")
    result = analyse_window(snap, broken)
    assert result["status"] == "invalid_input"
    assert any("not in the snapshot" in r for r in result["reasons"])
    assert analyse_window("x", validation)["status"] == "invalid_input"
    m1 = [candle(datetime(2026, 11, 10, 10, i, tzinfo=UTC)) for i in range(30)]
    snap_m1 = snapshot(m1, timeframe="M1")
    validation_m1 = validate_window(snap_m1, required_count=30)
    assert analyse_window(snap_m1, validation_m1)["status"] == "unsupported_timeframe"


# ---------------------------------------------------------------- contract and properties
def test_output_is_strict_json_and_deterministic() -> None:
    snap, validation, _ = analysed(history(400))
    outputs = {
        json.dumps(
            analyse_window(snap, validation, analysed_at_utc="2026-11-11T13:00:05Z"),
            allow_nan=False,
            sort_keys=True,
        )
        for _ in range(10)
    }
    assert len(outputs) == 1


def test_inputs_are_not_modified() -> None:
    snap, validation, _ = analysed(history(250))
    before = (copy.deepcopy(snap), copy.deepcopy(validation))
    analyse_window(snap, validation)
    assert (snap, validation) == before


def test_engine_values_match_the_window_candles() -> None:
    bars = history(260)
    snap, validation, result = analysed(bars)
    closes = [b["c"] for b in bars]
    assert result["indicators"]["ema"][0]["value"] == ema_series(closes, 20)[-1]
    assert result["indicators"]["ema"][0]["t"] == bars[-1]["t"]
    assert result["window"] == {"count": 260, "first_t": bars[0]["t"], "last_t": bars[-1]["t"]}


def test_structure_has_no_lookahead_and_levels_from_confirmed_pivots() -> None:
    bars = history(400)
    _, _, result = analysed(bars)
    swings = result["structure"]["swings"]
    assert swings["confirmed"], "the walk should contain pivots"
    for p in swings["confirmed"]:
        assert p["confirmed_at_utc"] > p["pivot_t"]
    levels = result["structure"]["support_resistance"]
    confirmed_times = {p["pivot_t"] for p in swings["confirmed"]}
    for side in ("support", "resistance"):
        for level in levels[side]:
            assert level["strength"] == len(level["pivots"])
            assert all(p["pivot_t"] in confirmed_times for p in level["pivots"])
    assert levels["tolerance"] == pytest.approx(0.25 * result["indicators"]["atr"]["value"])
    # A shorter, earlier window confirms a subset of the same pivots at the same times.
    _, _, earlier = analysed(bars[:300])
    earlier_set = {
        (p["type"], p["pivot_t"], p["confirmed_at_utc"])
        for p in earlier["structure"]["swings"]["confirmed"]
    }
    later_set = {(p["type"], p["pivot_t"], p["confirmed_at_utc"]) for p in swings["confirmed"]}
    assert earlier_set <= later_set


def test_no_signal_vocabulary_in_output() -> None:
    _, _, result = analysed(history(700))
    text = json.dumps(result).lower()
    for word in ("buy", "sell", "entry", "stop_loss", "take_profit", 'signal":'):
        assert word not in text


def test_default_parameters_are_the_approved_ones() -> None:
    p = DEFAULT_PARAMETERS
    assert p.ema_periods == (20, 50, 200) and (p.rsi_period, p.atr_period, p.adx_period) == (
        14,
        14,
        14,
    )
    assert (p.swing_k, p.sr_tolerance_atr, p.volatility_window) == (2, 0.25, 100)
    assert (p.volatility_low, p.volatility_high, p.convergence_tolerance) == (25.0, 75.0, 0.01)


# ---------------------------------------------------------------- T5.1 EMA convergence boundary
# Totals are p (SMA seed, first EMA at index p - 1) + n recursive updates, n being the smallest
# integer with (1 - alpha)^n < 1 %: EMA20 20 + 47, EMA50 50 + 116, EMA200 200 + 461.
EMA_BOUNDARIES = [(0, 20, 67), (1, 50, 166), (2, 200, 661)]


@pytest.mark.parametrize(("position", "period", "total"), EMA_BOUNDARIES)
def test_ema_seed_weight_crosses_one_percent_exactly_at_total(position, period, total) -> None:
    # Independent of updates_to_converge: by linearity, closes of 1 for the seed and 0 after it
    # make ema_series output exactly the remaining seed weight (1 - alpha)^n.
    def seed_weight_after(observations: int) -> float:
        return ema_series([1.0] * period + [0.0] * (observations - period), period)[-1]

    assert seed_weight_after(total - 1) >= 0.01  # not settled one observation earlier
    assert seed_weight_after(total) < 0.01  # strictly below 1 % at the total


@pytest.mark.parametrize(("position", "period", "total"), EMA_BOUNDARIES)
def test_engine_ema_is_not_settled_before_total_and_settled_at_total(
    position, period, total
) -> None:
    for count, settled in ((total - 1, False), (total, True)):
        _, validation, result = analysed(history(count))
        assert validation["verdict"]["state"] == "accepted", validation["verdict"]
        ema = result["indicators"]["ema"][position]
        assert ema["period"] == period
        convergence = ema["convergence"]
        assert convergence["observations_required"] == total
        assert convergence["updates"] == count - period
        assert convergence["converged"] is settled
        assert (convergence["seed_weight"] < 0.01) is settled
        assert ema["status"] == ("available" if settled else "not_converged")
        assert ema["trusted"] is settled
        assert ema["value"] is not None  # warmed up in both cases


def test_convergence_condition_is_strict_at_equality() -> None:
    # (1 - 0.5)^2 == 0.25 exactly: equal to the tolerance is NOT converged, so 3 updates.
    assert updates_to_converge(0.5, 0.25) == 3


def test_performance_on_the_1000_candle_maximum() -> None:
    snap, validation, _ = analysed(history(1000))
    started = time.perf_counter()
    result = analyse_window(snap, validation)
    elapsed = time.perf_counter() - started
    assert result["status"] == "available"
    assert elapsed < 2.0, elapsed  # generous bound; typical runtime is far lower
