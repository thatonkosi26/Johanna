"""Deterministic analysis of a validated window (T5).

``analyse_window(snapshot, validation)`` consumes a T2 snapshot and the T4/T4.1 result of
``validate_window`` for that snapshot, and returns indicator, market-structure and volatility
results. Pure: no clock reads, no I/O, inputs are never modified.

Validation gate (the original validation result is always included unchanged):
- ``rejected`` window: nothing is calculated; status ``rejected`` with the original reasons.
  Problematic candles are never dropped to manufacture a valid window.
- ``provisional`` window: calculations are produced for diagnosis only; status
  ``provisional``; no component is trusted.
- ``accepted`` window: calculations are produced; a component is ``trusted`` only if its own
  status is ``available`` (warmed up and converged). Overall status ``available`` when every
  component is available, otherwise ``partial``.
Other statuses: ``invalid_input`` (snapshot/validation mismatch or malformed),
``unsupported_timeframe`` (T5 supports H1, H4, D1).

Component statuses: ``available``, ``insufficient_history``, ``not_converged``.
There is no overall score; a calculation result is not a trading signal or opportunity.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from johanna.analysis.continuity import TIMEFRAME_SECONDS
from johanna.analysis.indicators import (
    adx_series,
    atr_series,
    ema_series,
    rsi_series,
    seed_weight,
    updates_to_converge,
)
from johanna.analysis.structure import find_swings, support_resistance
from johanna.analysis.volatility import volatility_regime

ANALYSIS_VERSION = 1
SUPPORTED_TIMEFRAMES = ("H1", "H4", "D1")
AVAILABLE, INSUFFICIENT, NOT_CONVERGED = "available", "insufficient_history", "not_converged"


@dataclass(frozen=True)
class AnalysisParameters:
    """Approved initial parameters (Phase 2.1 decisions)."""

    ema_periods: tuple[int, ...] = (20, 50, 200)
    rsi_period: int = 14
    atr_period: int = 14
    adx_period: int = 14
    swing_k: int = 2
    sr_tolerance_atr: float = 0.25
    volatility_window: int = 100
    volatility_low: float = 25.0
    volatility_high: float = 75.0
    convergence_tolerance: float = 0.01


DEFAULT_PARAMETERS = AnalysisParameters()


def _convergence(alpha: float, updates: int, tolerance: float, first_index: int) -> dict:
    weight = seed_weight(alpha, updates)
    needed = updates_to_converge(alpha, tolerance)
    return {
        "proxy": "seed_weight",
        "alpha": alpha,
        "updates": max(0, updates),
        "seed_weight": weight,
        "tolerance": tolerance,
        "converged": weight < tolerance,
        "observations_required": first_index + 1 + needed,
    }


def _summary(
    name: str,
    period: int,
    series: list,
    times: list,
    first_index: int,
    alpha: float,
    tolerance: float,
    note: str,
) -> dict[str, Any]:
    n = len(series)
    warmed = n > first_index
    result: dict[str, Any] = {
        "name": name,
        "period": period,
        "observations": n,
        "warmup": {"observations_required": first_index + 1, "satisfied": warmed},
    }
    if not warmed:
        return {
            **result,
            "status": INSUFFICIENT,
            "value": None,
            "t": None,
            "convergence": None,
            "reason": f"{n} candles, {first_index + 1} needed for the first value",
        }
    convergence = _convergence(alpha, n - 1 - first_index, tolerance, first_index)
    convergence["note"] = note
    return {
        **result,
        "status": AVAILABLE if convergence["converged"] else NOT_CONVERGED,
        "value": series[-1],
        "t": times[-1],
        "convergence": convergence,
    }


SEED_NOTE = "remaining seed weight of the smoother; not a bound on the indicator's output error"
WILDER_NOTE = (
    "stabilisation proxy: seed weight of Wilder smoothing (alpha = 1/period); the indicator is a "
    "non-linear function of smoothed values, so this is not an output-error bound"
)


def analyse_window(
    snapshot: Mapping[str, Any],
    validation: Mapping[str, Any],
    *,
    analysed_at_utc: str | None = None,
    parameters: AnalysisParameters = DEFAULT_PARAMETERS,
) -> dict[str, Any]:
    """Analyse the validated window of ``snapshot`` (see module docstring)."""
    verdict = (validation.get("verdict") or {}) if isinstance(validation, Mapping) else {}
    state = verdict.get("state")
    output: dict[str, Any] = {
        "analysis_version": ANALYSIS_VERSION,
        "snapshot_id": snapshot.get("snapshot_id") if isinstance(snapshot, Mapping) else None,
        "symbol": snapshot.get("symbol") if isinstance(snapshot, Mapping) else None,
        "broker_symbol": snapshot.get("broker_symbol") if isinstance(snapshot, Mapping) else None,
        "timeframe": snapshot.get("timeframe") if isinstance(snapshot, Mapping) else None,
        "analysed_at_utc": analysed_at_utc
        or (validation.get("evaluated_at_utc") if isinstance(validation, Mapping) else None),
        "validation": dict(validation) if isinstance(validation, Mapping) else None,
        "validation_verdict": state,
        "validation_reasons": list(verdict.get("reasons") or []),
        "parameters": asdict(parameters),
        "indicators": None,
        "structure": None,
        "volatility": None,
        "trusted_components": [],
        "note": (
            "Deterministic calculations only. No result is a trading signal, recommendation or "
            "opportunity, and none implies live verification or trading readiness."
        ),
    }

    problems = _input_problems(snapshot, validation)
    if problems:
        return {**output, "status": "invalid_input", "reasons": problems}
    timeframe = snapshot["timeframe"]
    if timeframe not in SUPPORTED_TIMEFRAMES:
        return {
            **output,
            "status": "unsupported_timeframe",
            "reasons": [f"T5 supports {', '.join(SUPPORTED_TIMEFRAMES)}; got {timeframe}"],
        }
    if state == "rejected":
        return {
            **output,
            "status": "rejected",
            "reasons": ["the validated window was rejected; nothing was calculated"],
        }

    candles = _window_candles(snapshot, validation)
    times = [c["t"] for c in candles]
    highs = [float(c["h"]) for c in candles]
    lows = [float(c["l"]) for c in candles]
    closes = [float(c["c"]) for c in candles]
    tol = parameters.convergence_tolerance

    emas = []
    for period in parameters.ema_periods:
        emas.append(
            _summary(
                f"EMA{period}",
                period,
                ema_series(closes, period),
                times,
                period - 1,
                2.0 / (period + 1),
                tol,
                SEED_NOTE,
            )
        )
    p_rsi = parameters.rsi_period
    rsi = _summary(
        f"RSI{p_rsi}", p_rsi, rsi_series(closes, p_rsi), times, p_rsi, 1.0 / p_rsi, tol, WILDER_NOTE
    )
    p_atr = parameters.atr_period
    atr_values = atr_series(highs, lows, closes, p_atr)
    atr = _summary(f"ATR{p_atr}", p_atr, atr_values, times, p_atr, 1.0 / p_atr, tol, WILDER_NOTE)
    p_adx = parameters.adx_period
    adx_all = adx_series(highs, lows, closes, p_adx)
    adx = _summary(
        f"ADX{p_adx}",
        p_adx,
        adx_all["adx"],
        times,
        2 * p_adx - 1,
        1.0 / p_adx,
        tol,
        WILDER_NOTE + "; ADX is doubly smoothed: both stages must pass",
    )
    if adx["convergence"] is not None:
        dm_stage = _convergence(1.0 / p_adx, len(closes) - 1 - p_adx, tol, p_adx)
        adx["convergence"]["directional_movement_stage"] = dm_stage
        if not dm_stage["converged"]:
            adx["status"] = NOT_CONVERGED
            adx["convergence"]["converged"] = False
        adx["plus_di"] = adx_all["plus_di"][-1]
        adx["minus_di"] = adx_all["minus_di"][-1]
        adx["dx"] = adx_all["dx"][-1]

    step = TIMEFRAME_SECONDS[timeframe]
    k = parameters.swing_k
    if len(candles) < 2 * k + 1:
        swings: dict[str, Any] = {
            "status": INSUFFICIENT,
            "k": k,
            "confirmed": [],
            "pending": [],
            "reason": f"{len(candles)} candles, {2 * k + 1} needed",
        }
    else:
        found = find_swings(
            [{"t": t, "h": h, "l": lo} for t, h, lo in zip(times, highs, lows, strict=True)],
            step,
            k,
        )
        swings = {"status": AVAILABLE, "k": k, **found}
    levels = support_resistance(swings, atr["value"], parameters.sr_tolerance_atr)
    if levels["status"] == AVAILABLE and atr["status"] != AVAILABLE:
        levels["status"] = NOT_CONVERGED  # the tolerance rests on an unconverged ATR
        levels["reason"] = "ATR reference not converged"
    levels["atr_reference_t"] = atr["t"]

    converged_flags = [
        value is not None and seed_weight(1.0 / p_atr, i - p_atr) < tol
        for i, value in enumerate(atr_values)
    ]
    volatility = volatility_regime(
        atr_values,
        converged_flags,
        window=parameters.volatility_window,
        low=parameters.volatility_low,
        high=parameters.volatility_high,
    )
    if volatility.get("current_index") is not None:
        volatility["current_t"] = times[volatility["current_index"]]

    components = {
        **{e["name"]: e["status"] for e in emas},
        rsi["name"]: rsi["status"],
        atr["name"]: atr["status"],
        adx["name"]: adx["status"],
        "swings": swings["status"],
        "support_resistance": levels["status"],
        "volatility_regime": volatility["status"],
    }
    accepted = state == "accepted"
    for item in (*emas, rsi, atr, adx, swings, levels, volatility):
        item["trusted"] = accepted and item["status"] == AVAILABLE
    if accepted:
        status = AVAILABLE if all(s == AVAILABLE for s in components.values()) else "partial"
    else:
        status = "provisional"
    return {
        **output,
        "status": status,
        "reasons": [] if accepted else ["validated window is provisional: diagnostic only"],
        "window": {
            "count": len(candles),
            "first_t": times[0] if times else None,
            "last_t": times[-1] if times else None,
        },
        "component_statuses": components,
        "trusted_components": [
            name for name, s in components.items() if accepted and s == AVAILABLE
        ],
        "indicators": {"ema": emas, "rsi": rsi, "atr": atr, "adx": adx},
        "structure": {"swings": swings, "support_resistance": levels},
        "volatility": volatility,
    }


def _input_problems(snapshot: Any, validation: Any) -> list[str]:
    if not isinstance(snapshot, Mapping) or not isinstance(validation, Mapping):
        return ["snapshot and validation must both be mappings"]
    problems = []
    state = (validation.get("verdict") or {}).get("state")
    if state not in ("accepted", "provisional", "rejected"):
        problems.append(f"unknown validation verdict {state!r}")
    if validation.get("snapshot_id") != snapshot.get("snapshot_id"):
        problems.append("validation belongs to a different snapshot")
    if validation.get("timeframe") != snapshot.get("timeframe"):
        problems.append("validation timeframe differs from the snapshot")
    if state in ("accepted", "provisional"):
        items = {}
        for candle in (snapshot.get("bars") or {}).get("items") or []:
            items.setdefault(candle.get("t"), candle)
        for t in (validation.get("window") or {}).get("candles") or []:
            candle = items.get(t)
            if candle is None:
                problems.append(f"window candle {t} not in the snapshot")
            elif not all(
                isinstance(candle.get(f), int | float) and not isinstance(candle.get(f), bool)
                for f in ("h", "l", "c")
            ):
                problems.append(f"window candle {t} has non-numeric prices")
    return problems


def _window_candles(snapshot: Mapping[str, Any], validation: Mapping[str, Any]) -> list[dict]:
    """Copies of the validated window's candles (canonical = first snapshot item per time)."""
    items: dict[str, Mapping[str, Any]] = {}
    for candle in snapshot["bars"]["items"]:
        items.setdefault(candle["t"], candle)
    return [dict(items[t]) for t in validation["window"]["candles"]]
