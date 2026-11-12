"""Volatility regime (T5): ATR percentile against a rolling reference window.

Pure and deterministic.

Construction: the *current* observation is the latest ATR value. The *reference window* is the
``window`` (default 100) ATR values immediately **preceding** it; the current value is not
part of its own reference, and nothing after it is used.

Percentile (mid-rank convention, handles ties):
``percentile = 100 * (count(reference < current) + 0.5 * count(reference == current)) / n``.

Classification: ``percentile < low`` (25) -> ``low``; ``low <= percentile <= high`` (75) ->
``normal`` (both boundaries inclusive); ``percentile > high`` -> ``high``.

Only *converged* ATR values may be used (seed-weight proxy below tolerance); with too few ATR
values the result is ``insufficient_history``, with enough values but too few converged ones it
is ``not_converged`` (a diagnostic percentile is still shown). No classification is invented.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any


def percentile_rank(reference: Sequence[float], current: float) -> float:
    less = sum(1 for value in reference if value < current)
    equal = sum(1 for value in reference if value == current)
    return 100.0 * (less + 0.5 * equal) / len(reference)


def classify(percentile: float, low: float = 25.0, high: float = 75.0) -> str:
    if percentile < low:
        return "low"
    if percentile > high:
        return "high"
    return "normal"


def volatility_regime(
    atr_values: Sequence[float | None],
    converged: Sequence[bool],
    *,
    window: int = 100,
    low: float = 25.0,
    high: float = 75.0,
) -> dict[str, Any]:
    """``atr_values`` aligned with candles (``None`` before ATR exists); ``converged[i]`` says
    whether ATR at ``i`` passed the convergence proxy."""
    indices = [i for i, value in enumerate(atr_values) if value is not None]
    base: dict[str, Any] = {
        "window": window,
        "thresholds": {"low_below": low, "high_above": high},
        "method": "mid-rank percentile of the latest ATR vs the preceding window ATR values",
    }
    if len(indices) < window + 1:
        return {
            **base,
            "status": "insufficient_history",
            "regime": None,
            "percentile": None,
            "reason": f"{len(indices)} ATR values, {window + 1} needed",
        }
    current_i = indices[-1]
    reference_i = indices[-(window + 1) : -1]
    reference = [atr_values[i] for i in reference_i]
    current = atr_values[current_i]
    percentile = percentile_rank(reference, current)  # type: ignore[arg-type]
    all_converged = all(converged[i] for i in (*reference_i, current_i))
    return {
        **base,
        "status": "available" if all_converged else "not_converged",
        "regime": classify(percentile, low, high) if all_converged else None,
        "percentile": percentile,
        "current_atr": current,
        "current_index": current_i,
        "reference_first_index": reference_i[0],
        "reference_last_index": reference_i[-1],
        **({} if all_converged else {"reason": "reference or current ATR not converged"}),
    }
