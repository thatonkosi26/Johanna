"""Deterministic technical indicators (T5): EMA, Wilder RSI, Wilder ATR, Wilder ADX.

Pure functions over chronological sequences. Each ``*_series`` function returns a list
aligned with its input: ``None`` until the indicator is mathematically initialised.

Warm-up vs convergence:
- **Warm-up** is the minimum number of observations for the first value to exist.
- **Convergence (stabilisation)** is judged with a *seed-weight proxy*: the remaining weight
  of the initial seed in an exponential smoother after ``n`` recursive updates,
  ``(1 - alpha) ** n``. EMA: ``alpha = 2 / (period + 1)``. Wilder smoothing (RSI, ATR,
  ADX): ``alpha = 1 / period``. A value is reported as converged only when the proxy is below
  the tolerance (default 1 %). The proxy measures seed influence on the smoother, not the
  error of the final indicator value: RSI and ADX are non-linear functions of smoothed
  quantities, so for them it is a stabilisation proxy, not an output-error bound.

Index conventions (0-based candle index ``i``; ``p`` = period):
- EMA: first value at ``i = p - 1`` (SMA of closes 0..p-1); needs ``p`` candles.
- RSI: changes start at ``i = 1``; first value at ``i = p`` (averages of changes 1..p); needs
  ``p + 1`` candles.
- ATR: true range needs the previous close, so TR starts at ``i = 1``; first ATR at ``i = p``
  (SMA of TR 1..p); needs ``p + 1`` candles.
- ADX: DM/TR start at ``i = 1``; Wilder sums and the first DX at ``i = p``; the first ADX at
  ``i = 2p - 1`` (mean of DX ``p``..``2p - 1``); needs ``2p`` candles.
"""

from __future__ import annotations

import math
from collections.abc import Sequence


def _check_period(period: int) -> None:
    if isinstance(period, bool) or not isinstance(period, int) or period < 1:
        raise ValueError("period must be a positive integer")


def seed_weight(alpha: float, updates: int) -> float:
    """Remaining weight of the seed after ``updates`` recursive steps: (1 - alpha) ** n."""
    return (1.0 - alpha) ** max(0, updates)


def updates_to_converge(alpha: float, tolerance: float) -> int:
    """Smallest n with (1 - alpha) ** n < tolerance."""
    if not 0 < tolerance < 1 or not 0 < alpha < 1:
        return 0
    n = math.floor(math.log(tolerance) / math.log(1.0 - alpha)) + 1
    while seed_weight(alpha, n) >= tolerance:
        n += 1
    while n > 0 and seed_weight(alpha, n - 1) < tolerance:
        n -= 1
    return n


def ema_series(closes: Sequence[float], period: int) -> list[float | None]:
    """EMA seeded with the SMA of the first ``period`` closes, then
    ``EMA_i = alpha * close_i + (1 - alpha) * EMA_{i-1}``."""
    _check_period(period)
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period:
        return out
    alpha = 2.0 / (period + 1)
    value = sum(closes[:period]) / period
    out[period - 1] = value
    for i in range(period, len(closes)):
        value = alpha * closes[i] + (1.0 - alpha) * value
        out[i] = value
    return out


def _rsi_from(avg_gain: float, avg_loss: float) -> float:
    """Edge cases: no losses and some gains -> 100; no gains and some losses -> 0; neither
    (flat prices) -> 50 (no directional movement)."""
    if avg_loss == 0.0:
        return 50.0 if avg_gain == 0.0 else 100.0
    if avg_gain == 0.0:
        return 0.0
    return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)


def rsi_series(closes: Sequence[float], period: int) -> list[float | None]:
    """Wilder RSI: initial averages = simple means of the first ``period`` gains/losses
    (changes 1..p); then ``avg = (avg * (p - 1) + current) / p``."""
    _check_period(period)
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, len(closes))]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, len(closes))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    out[period] = _rsi_from(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        avg_gain = (avg_gain * (period - 1) + gains[i - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i - 1]) / period
        out[i] = _rsi_from(avg_gain, avg_loss)
    return out


def true_ranges(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]
) -> list[float | None]:
    """``TR_i = max(H_i - L_i, |H_i - C_{i-1}|, |L_i - C_{i-1}|)`` for ``i >= 1``; ``None`` at
    ``i = 0`` (no previous close)."""
    out: list[float | None] = [None] * len(closes)
    for i in range(1, len(closes)):
        previous = closes[i - 1]
        out[i] = max(highs[i] - lows[i], abs(highs[i] - previous), abs(lows[i] - previous))
    return out


def atr_series(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int
) -> list[float | None]:
    """Wilder ATR: SMA of TR 1..p at ``i = p``, then ``ATR = (ATR * (p - 1) + TR) / p``."""
    _check_period(period)
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    tr = true_ranges(highs, lows, closes)
    value = sum(tr[1 : period + 1]) / period  # type: ignore[arg-type]
    out[period] = value
    for i in range(period + 1, len(closes)):
        value = (value * (period - 1) + tr[i]) / period  # type: ignore[operator]
        out[i] = value
    return out


def adx_series(
    highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], period: int
) -> dict[str, list[float | None]]:
    """Wilder ADX with +DI, -DI and DX.

    For ``i >= 1``: ``up = H_i - H_{i-1}``, ``down = L_{i-1} - L_i``;
    ``+DM = up if up > down and up > 0 else 0``; ``-DM = down if down > up and down > 0
    else 0`` (equal movement gives 0 to both). Wilder sums at ``i = p``: the plain sums of
    TR, +DM, -DM over 1..p; afterwards ``S = S - S / p + x``. ``+DI = 100 * +DM_S / TR_S``,
    ``-DI = 100 * -DM_S / TR_S``, ``DX = 100 * |+DI - -DI| / (+DI + -DI)``.
    Conventions: ``TR_S == 0`` (no range at all) gives ``+DI = -DI = 0``; ``+DI + -DI == 0``
    gives ``DX = 0``. ``ADX`` at ``i = 2p - 1`` is the mean of DX ``p``..``2p - 1``, then
    ``ADX = (ADX * (p - 1) + DX) / p``.
    """
    _check_period(period)
    n = len(closes)
    plus_di: list[float | None] = [None] * n
    minus_di: list[float | None] = [None] * n
    dx: list[float | None] = [None] * n
    adx: list[float | None] = [None] * n
    result = {"plus_di": plus_di, "minus_di": minus_di, "dx": dx, "adx": adx}
    if n < period + 1:
        return result
    tr = true_ranges(highs, lows, closes)
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if up > down and up > 0 else 0.0
        minus_dm[i] = down if down > up and down > 0 else 0.0
    tr_s = sum(tr[1 : period + 1])  # type: ignore[arg-type]
    plus_s = sum(plus_dm[1 : period + 1])
    minus_s = sum(minus_dm[1 : period + 1])
    for i in range(period, n):
        if i > period:
            tr_s = tr_s - tr_s / period + tr[i]  # type: ignore[operator]
            plus_s = plus_s - plus_s / period + plus_dm[i]
            minus_s = minus_s - minus_s / period + minus_dm[i]
        pdi = 100.0 * plus_s / tr_s if tr_s > 0 else 0.0
        mdi = 100.0 * minus_s / tr_s if tr_s > 0 else 0.0
        plus_di[i], minus_di[i] = pdi, mdi
        dx[i] = 100.0 * abs(pdi - mdi) / (pdi + mdi) if pdi + mdi > 0 else 0.0
    first_adx = 2 * period - 1
    if n > first_adx:
        value = sum(dx[period : first_adx + 1]) / period  # type: ignore[arg-type]
        adx[first_adx] = value
        for i in range(first_adx + 1, n):
            value = (value * (period - 1) + dx[i]) / period  # type: ignore[operator]
            adx[i] = value
    return result
