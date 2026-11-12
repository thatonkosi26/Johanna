"""T5 indicators: hand-calculated references and independent re-derivations.

No external TA library is installed (TA-Lib/pandas are not dependencies), so references are
(a) small hand-worked examples whose arithmetic is written out below, and (b) independent
re-derivations in exact rational arithmetic (``fractions.Fraction``) using a different
formulation from the implementation (e.g. EMA as a closed-form weighted sum).
"""

from __future__ import annotations

import json
import random
from fractions import Fraction

import pytest

from johanna.analysis.indicators import (
    adx_series,
    atr_series,
    ema_series,
    rsi_series,
    seed_weight,
    true_ranges,
    updates_to_converge,
)


def approx(values):
    return [None if v is None else pytest.approx(float(v), rel=1e-12, abs=1e-12) for v in values]


# ---------------------------------------------------------------- EMA
def test_ema_hand_calculated() -> None:
    # p = 3, alpha = 2/4 = 0.5. Seed = SMA(1, 2, 3) = 2 at index 2.
    # i3: 0.5*4 + 0.5*2 = 3; i4: 0.5*5 + 0.5*3 = 4; i5: 0.5*6 + 0.5*4 = 5.
    assert ema_series([1, 2, 3, 4, 5, 6], 3) == [None, None, 2, 3, 4, 5]
    # Seed = SMA(10, 11, 12) = 11; i3: 0.5*10 + 0.5*11 = 10.5; i4: 0.5*14 + 0.5*10.5 = 12.25.
    assert ema_series([10, 11, 12, 10, 14], 3) == [None, None, 11, 10.5, 12.25]


def test_ema_matches_closed_form_weighted_sum() -> None:
    rng = random.Random(7)
    closes = [Fraction(rng.randint(10000, 12000), 10000) for _ in range(60)]
    period = 20
    alpha = Fraction(2, period + 1)
    seed = sum(closes[:period]) / period
    n = len(closes) - period  # recursive updates
    closed_form = (
        sum(alpha * (1 - alpha) ** j * closes[-1 - j] for j in range(n)) + (1 - alpha) ** n * seed
    )
    assert ema_series([float(c) for c in closes], period)[-1] == pytest.approx(
        float(closed_form), rel=1e-12
    )


def test_ema_period_boundaries_and_insufficient_history() -> None:
    assert ema_series([1.0, 2.0], 3) == [None, None]
    assert ema_series([1.0, 2.0, 3.0], 3) == [None, None, 2.0]  # exactly p: seed only
    assert ema_series([], 3) == []
    with pytest.raises(ValueError):
        ema_series([1.0], 0)


def test_seed_weight_convergence_proxy() -> None:
    assert seed_weight(0.5, 2) == 0.25
    assert seed_weight(0.5, 0) == 1.0
    # EMA200: alpha = 2/201; (199/201)^460 > 1 % > (199/201)^461.
    alpha = 2 / 201
    assert seed_weight(alpha, 460) > 0.01 > seed_weight(alpha, 461)
    assert updates_to_converge(alpha, 0.01) == 461
    assert updates_to_converge(2 / 21, 0.01) == 47  # EMA20: ln(0.01)/ln(19/21) = 46.05
    assert updates_to_converge(2 / 51, 0.01) == 116  # EMA50: 115.1
    assert updates_to_converge(1 / 14, 0.01) == 63  # Wilder 14: 62.1


# ---------------------------------------------------------------- RSI
def test_rsi_hand_calculated_mixed() -> None:
    # p = 3, closes 10, 11, 12, 11, 13: changes +1, +1, -1, +2.
    # Initial averages (changes 1..3): gain 2/3, loss 1/3 -> RS 2 -> RSI 100 - 100/3.
    # Next: gain (2/3*2 + 2)/3 = 10/9, loss (1/3*2 + 0)/3 = 2/9 -> RS 5 -> RSI 100 - 100/6.
    assert rsi_series([10, 11, 12, 11, 13], 3) == approx(
        [None, None, None, 100 - Fraction(100, 3), 100 - Fraction(100, 6)]
    )


def test_rsi_directional_and_flat_conventions() -> None:
    assert rsi_series([1, 2, 3, 4, 5], 3)[-2:] == [100.0, 100.0]  # zero average loss
    assert rsi_series([5, 4, 3, 2, 1], 3)[-2:] == [0.0, 0.0]  # zero average gain
    assert rsi_series([2, 2, 2, 2, 2], 3)[-2:] == [50.0, 50.0]  # flat: documented 50


def test_rsi_initialisation_boundary_and_history() -> None:
    assert rsi_series([1, 2, 3], 3) == [None, None, None]  # needs p + 1 closes
    assert rsi_series([1, 2, 3, 2], 3)[3] == pytest.approx(100 - 100 / (1 + (2 / 3) / (1 / 3)))


def test_rsi_matches_independent_exact_recurrence() -> None:
    rng = random.Random(11)
    closes = [Fraction(rng.randint(10000, 10100), 10000) for _ in range(80)]
    p = 14
    changes = [b - a for a, b in zip(closes, closes[1:], strict=False)]
    g = sum(max(c, 0) for c in changes[:p]) / p
    lo = sum(max(-c, 0) for c in changes[:p]) / p
    for c in changes[p:]:
        g = g + (max(c, 0) - g) / p  # algebraically equal to (g*(p-1) + x)/p
        lo = lo + (max(-c, 0) - lo) / p
    expected = 100 - 100 / (1 + g / lo)
    assert rsi_series([float(c) for c in closes], p)[-1] == pytest.approx(
        float(expected), rel=1e-10
    )


# ---------------------------------------------------------------- ATR
H = [10, 11, 12, 15, 14]
L = [8, 9, 10, 13, 12]
C = [9, 10, 11, 14, 12]


def test_true_range_hand_calculated_including_gaps() -> None:
    # i1: max(2, |11-9|, |9-9|) = 2; i2: max(2, 2, 1) = 2;
    # i3 gap up: max(2, |15-11|=4, |13-11|=2) = 4; i4: max(2, |14-14|=0, |12-14|=2) = 2.
    assert true_ranges(H, L, C) == [None, 2, 2, 4, 2]
    # gap down: previous close 14, candle 10/9 -> max(1, |10-14|=4, |9-14|=5) = 5
    assert true_ranges([15, 10], [13, 9], [14, 9.5])[1] == 5


def test_atr_wilder_initialisation_and_smoothing() -> None:
    # p = 3: ATR(i3) = (2 + 2 + 4)/3 = 8/3; ATR(i4) = (8/3*2 + 2)/3 = 22/9.
    assert atr_series(H, L, C, 3) == approx([None, None, None, Fraction(8, 3), Fraction(22, 9)])


def test_atr_flat_and_insufficient_history() -> None:
    assert atr_series([1] * 6, [1] * 6, [1] * 6, 3)[-1] == 0.0
    assert atr_series(H[:3], L[:3], C[:3], 3) == [None, None, None]


# ---------------------------------------------------------------- ADX
AH = [10, 12, 13, 12, 11]
AL = [8, 9, 10, 8, 7]
AC = [9, 11, 12, 9, 8]


def test_adx_hand_calculated() -> None:
    # p = 2. DM/TR from i1: +DM 2,1,0,0; -DM 0,0,2,1; TR 3,3,4,4.
    # i2: sums TR 6, +DM 3, -DM 0 -> +DI 50, -DI 0, DX 100.
    # i3: TR 6-3+4 = 7, +DM 3-1.5+0 = 1.5, -DM 0-0+2 = 2 -> +DI 150/7, -DI 200/7,
    #     DX = 100*(50/7)/(350/7) = 100/7. ADX(i3) = mean(100, 100/7) = 400/7.
    # i4: TR 7-3.5+4 = 7.5, +DM 0.75, -DM 2-1+1 = 2 -> +DI 10, -DI 80/3,
    #     DX = 100*(50/3)/(110/3) = 500/11; ADX = (400/7 + 500/11)/2 = 3950/77.
    result = adx_series(AH, AL, AC, 2)
    assert result["plus_di"] == approx([None, None, 50, Fraction(150, 7), 10])
    assert result["minus_di"] == approx([None, None, 0, Fraction(200, 7), Fraction(80, 3)])
    assert result["dx"] == approx([None, None, 100, Fraction(100, 7), Fraction(500, 11)])
    assert result["adx"] == approx([None, None, None, Fraction(400, 7), Fraction(3950, 77)])


def test_adx_edge_cases() -> None:
    flat = adx_series([5] * 8, [5] * 8, [5] * 8, 2)
    assert flat["adx"][-1] == 0.0 and flat["plus_di"][-1] == 0.0  # zero true range
    equal = adx_series([10, 11, 12, 13, 14], [10, 9, 8, 7, 6], [10, 10, 10, 10, 10], 2)
    assert equal["plus_di"][-1] == 0.0 and equal["minus_di"][-1] == 0.0  # equal movement
    assert equal["dx"][-1] == 0.0
    up = adx_series([1, 2, 3, 4, 5, 6], [0.5, 1.5, 2.5, 3.5, 4.5, 5.5], [1, 2, 3, 4, 5, 6], 2)
    assert up["minus_di"][-1] == 0.0 and up["adx"][-1] == 100.0  # positive-only movement
    down = adx_series([6, 5, 4, 3, 2, 1], [5.5, 4.5, 3.5, 2.5, 1.5, 0.5], [6, 5, 4, 3, 2, 1], 2)
    assert down["plus_di"][-1] == 0.0 and down["adx"][-1] == 100.0  # negative-only movement


def test_adx_initialisation_boundaries() -> None:
    assert adx_series(AH[:3], AL[:3], AC[:3], 2)["adx"] == [None, None, None]  # needs 2p
    assert adx_series(AH[:4], AL[:4], AC[:4], 2)["adx"][3] == pytest.approx(400 / 7)
    assert adx_series(AH[:2], AL[:2], AC[:2], 2)["dx"] == [None, None]  # needs p + 1


def test_adx_matches_independent_wilder_averages() -> None:
    # Wilder sums S = S - S/p + x are p * (Wilder average); DI ratios are scale-free.
    rng = random.Random(3)
    closes, highs, lows = [], [], []
    price = Fraction(11000, 10000)
    for _ in range(60):
        price += Fraction(rng.randint(-30, 30), 10000)
        highs.append(price + Fraction(rng.randint(1, 20), 10000))
        lows.append(price - Fraction(rng.randint(1, 20), 10000))
        closes.append(price)
    p = 14
    tr, pdm, mdm = [], [], []
    for i in range(1, 60):
        tr.append(
            max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        )
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        pdm.append(up if up > down and up > 0 else 0)
        mdm.append(down if down > up and down > 0 else 0)
    a_tr, a_p, a_m = sum(tr[:p]) / p, sum(pdm[:p]) / p, sum(mdm[:p]) / p
    dxs = []
    for i in range(p - 1, len(tr)):
        if i > p - 1:
            a_tr += (tr[i] - a_tr) / p
            a_p += (pdm[i] - a_p) / p
            a_m += (mdm[i] - a_m) / p
        pdi, mdi = 100 * a_p / a_tr, 100 * a_m / a_tr
        dxs.append(100 * abs(pdi - mdi) / (pdi + mdi))
    adx = sum(dxs[:p]) / p
    for x in dxs[p:]:
        adx += (x - adx) / p
    result = adx_series(
        [float(v) for v in highs], [float(v) for v in lows], [float(v) for v in closes], p
    )
    assert result["adx"][-1] == pytest.approx(float(adx), rel=1e-10)


# ---------------------------------------------------------------- determinism
def test_indicators_are_deterministic_and_do_not_modify_input() -> None:
    rng = random.Random(5)
    closes = [1.1 + rng.random() / 100 for _ in range(300)]
    highs = [c + 0.001 for c in closes]
    lows = [c - 0.001 for c in closes]
    before = (list(closes), list(highs), list(lows))
    outputs = {
        json.dumps(
            [
                ema_series(closes, 20),
                rsi_series(closes, 14),
                atr_series(highs, lows, closes, 14),
                adx_series(highs, lows, closes, 14),
            ]
        )
        for _ in range(20)
    }
    assert len(outputs) == 1
    assert (closes, highs, lows) == before
