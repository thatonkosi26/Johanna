"""T6 features: derived per-candle measurements over a validated, analysed window.

Pure and deterministic: no clock, no I/O, inputs never modified. Built only from the validated
window's candles (via the T5 indicator functions) and the T5 ``analyse_window`` result.

Usability ("may this value be relied on?"):
- latest values come from T5. For an accepted analysis a value is usable only when T5 marks it
  ``trusted``. For a provisional analysis (diagnosis only, decision R2) the component status
  ``available`` is used and nothing produced from it is ever trusted;
- per-candle historical values (EMA20_j, EMA50_j, ATR_j) are recomputed with the T5 functions
  and are valid only if present, finite, converged at ``j`` (seed weight < tolerance, the T5
  proxy) and, for ATR, strictly positive.

Clearance (TC6) uses the raw T5 level interval ``[low, high]``; location uses the expanded
location zone ``[low - tol, high + tol]``. The two never feed each other.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from johanna.analysis.continuity import TIMEFRAME_SECONDS
from johanna.analysis.engine import DEFAULT_PARAMETERS as T5_PARAMETERS
from johanna.analysis.engine import AnalysisParameters
from johanna.analysis.indicators import atr_series, ema_series, seed_weight, updates_to_converge

FEATURES_VERSION = 1


@dataclass(frozen=True)
class DecisionParameters:
    """Experimental T6 defaults (revision 2.5 §11). Configurable; not tuned or validated."""

    adx_range_max: float = 20.0
    adx_trend_min: float = 25.0
    adx_extended: float = 40.0
    flat_slope_atr: float = 0.25
    touch_tolerance_atr: float = 0.25
    clearance_atr: float = 1.0
    slope_lookback: int = 5
    pullback_lookback: int = 5
    min_level_strength: int = 2
    rsi_overbought: float = 70.0
    rsi_oversold: float = 30.0
    require_ema200_alignment: bool = True
    veto_on_context_conflict: bool = True

    def __post_init__(self) -> None:
        for name in ("slope_lookback", "pullback_lookback", "min_level_strength"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
        for name in ("flat_slope_atr", "touch_tolerance_atr", "clearance_atr"):
            value = getattr(self, name)
            if not isinstance(value, int | float) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite non-negative number")
        if not 0 <= self.adx_range_max < self.adx_trend_min <= self.adx_extended:
            raise ValueError("ADX thresholds must satisfy 0 <= range_max < trend_min <= extended")
        if not 0 <= self.rsi_oversold < self.rsi_overbought <= 100:
            raise ValueError("RSI thresholds must satisfy 0 <= oversold < overbought <= 100")


DEFAULT_DECISION_PARAMETERS = DecisionParameters()
EMA_PERIODS = (20, 50, 200)


def evidence(
    id_: str,
    observed: Any = None,
    *,
    threshold: Any = None,
    comparison: str | None = None,
    source: str | None = None,
    t: str | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """One evidence item; every key is always present (null when not applicable)."""
    return {
        "id": id_,
        "observed": observed,
        "threshold": threshold,
        "comparison": comparison,
        "source": source,
        "t": t,
        "detail": detail,
    }


def parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo else None


def iso_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def converged_total(first_index: int, alpha: float, tolerance: float) -> int:
    """Observations needed for a converged value: first index + 1 + updates to converge."""
    return first_index + 1 + updates_to_converge(alpha, tolerance)


def required_candles(
    parameters: DecisionParameters = DEFAULT_DECISION_PARAMETERS,
    t5: AnalysisParameters = T5_PARAMETERS,
) -> int:
    """Smallest window for which every required setup input can be valid (661 by default)."""
    tol = t5.convergence_tolerance
    ema = {p: converged_total(p - 1, 2.0 / (p + 1), tol) for p in t5.ema_periods}
    atr = converged_total(t5.atr_period, 1.0 / t5.atr_period, tol)
    back = parameters.pullback_lookback - 1
    return max(
        ema[200],
        ema[50] + parameters.slope_lookback,
        ema[50] + back,
        ema[20] + back,
        atr + back,
    )


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def value_validity(
    series: Sequence[float | None],
    index: int,
    alpha: float,
    first_index: int,
    tolerance: float,
    *,
    positive: bool = False,
) -> tuple[bool, str | None]:
    """(valid, reason) for one recomputed per-candle value (module docstring)."""
    if index < 0 or index >= len(series):
        return False, "index outside the window"
    value = series[index]
    if value is None:
        return False, "insufficient_history: no value yet at this candle"
    if not _finite(value):
        return False, "non-finite value"
    weight = seed_weight(alpha, index - first_index)
    if not weight < tolerance:
        return False, f"not_converged: seed weight {weight:.6g} >= {tolerance}"
    if positive and not value > 0:
        return False, f"non-positive value {value}"
    return True, None


def _component(component: Mapping[str, Any], diagnostic: bool) -> tuple[bool, str | None]:
    """Usability of a T5 latest-value component (see module docstring)."""
    status = component.get("status")
    if diagnostic:
        ok = status == "available"
    else:
        ok = component.get("trusted") is True
    if ok:
        return True, None
    reason = component.get("reason") or status
    return False, f"{status}: {reason}" if reason and reason != status else str(status)


def _latest(
    name: str, component: Mapping[str, Any], diagnostic: bool, *, positive: bool = False
) -> dict[str, Any]:
    usable, reason = _component(component, diagnostic)
    value = component.get("value")
    if usable and not _finite(value):
        usable, reason = False, "non-finite value"
    if usable and positive and not value > 0:
        usable, reason = False, f"non-positive value {value}"
    return {
        "name": name,
        "value": value,
        "t": component.get("t"),
        "status": component.get("status"),
        "usable": usable,
        "reason": reason,
    }


def consistency_check(candles: Sequence[Mapping[str, Any]], analysis: Mapping[str, Any]) -> dict:
    """Recomputed latest EMA20/50/200 and ATR14 must equal the T5 values exactly."""
    closes = [float(c["c"]) for c in candles]
    highs = [float(c["h"]) for c in candles]
    lows = [float(c["l"]) for c in candles]
    emas = {e["period"]: e for e in analysis["indicators"]["ema"]}
    compared, mismatches = [], []
    for period in EMA_PERIODS:
        series = ema_series(closes, period)
        mine = series[-1] if series else None
        theirs = emas[period].get("value")
        compared.append(f"EMA{period}")
        if mine != theirs:
            mismatches.append({"input": f"EMA{period}", "recomputed": mine, "reported": theirs})
    atr = atr_series(highs, lows, closes, T5_PARAMETERS.atr_period)
    mine = atr[-1] if atr else None
    theirs = analysis["indicators"]["atr"].get("value")
    compared.append(f"ATR{T5_PARAMETERS.atr_period}")
    if mine != theirs:
        mismatches.append({"input": "ATR14", "recomputed": mine, "reported": theirs})
    return {"passed": not mismatches, "compared": compared, "mismatches": mismatches}


def swing_sequence(swings: Mapping[str, Any], usable: bool, reason: str | None, as_of: datetime):
    """Swing-structure state from confirmed pivots (confirmed_at <= as_of); pending ignored."""
    pending = len(swings.get("pending") or [])
    if not usable:
        return {
            "state": "unavailable",
            "usable": False,
            "reason": reason,
            "pivots": [],
            "pending_ignored": pending,
        }
    confirmed = [
        p for p in swings.get("confirmed") or [] if parse_utc(p.get("confirmed_at_utc")) <= as_of
    ]
    highs = sorted((p for p in confirmed if p.get("type") == "high"), key=lambda p: p["pivot_t"])
    lows = sorted((p for p in confirmed if p.get("type") == "low"), key=lambda p: p["pivot_t"])
    pivots = []
    for role, group, idx in (
        ("H_prev", highs, -2),
        ("H_last", highs, -1),
        ("L_prev", lows, -2),
        ("L_last", lows, -1),
    ):
        if len(group) >= -idx:
            p = group[idx]
            pivots.append(
                {
                    "role": role,
                    "type": p["type"],
                    "price": p["price"],
                    "pivot_t": p["pivot_t"],
                    "confirmed_at_utc": p["confirmed_at_utc"],
                }
            )
    result = {"usable": True, "reason": None, "pivots": pivots, "pending_ignored": pending}
    if len(highs) < 2 or len(lows) < 2:
        missing = []
        if len(highs) < 2:
            missing.append(f"{len(highs)} confirmed swing high(s), 2 needed")
        if len(lows) < 2:
            missing.append(f"{len(lows)} confirmed swing low(s), 2 needed")
        return {**result, "state": "unavailable", "reason": "; ".join(missing)}
    h_prev, h_last = highs[-2]["price"], highs[-1]["price"]
    l_prev, l_last = lows[-2]["price"], lows[-1]["price"]
    if h_last > h_prev and l_last > l_prev:
        state = "higher_highs_higher_lows"
    elif h_last < h_prev and l_last < l_prev:
        state = "lower_highs_lower_lows"
    else:
        state = "mixed"
    return {**result, "state": state}


def _level_key(level: Mapping[str, Any]) -> tuple:
    first = min((p["pivot_t"] for p in level.get("pivots") or []), default="")
    return (level["low"], level["high"], first)


def location_zones(levels: Mapping[str, Any], price: float, atr: float, min_strength: int):
    """Expanded location zones ``[low - tol, high + tol]`` for every qualifying level (§3.4)."""
    tol = levels["tolerance"]
    zones, non_qualifying = [], []
    for side, prefix in (("resistance", "R"), ("support", "S")):
        qualifying = sorted(
            (lvl for lvl in levels[side] if lvl["strength"] >= min_strength), key=_level_key
        )
        for number, lvl in enumerate(qualifying, start=1):
            z_low, z_high = lvl["low"] - tol, lvl["high"] + tol
            contains = z_low <= price <= z_high
            if contains:
                distance, position = 0.0, "contains"
            elif z_high < price:
                distance, position = (price - z_high) / atr, "below"
            else:
                distance, position = (z_low - price) / atr, "above"
            zones.append(
                {
                    "id": f"{prefix}{number}",
                    "side": side,
                    "level": lvl["level"],
                    "low": lvl["low"],
                    "high": lvl["high"],
                    "zone_low": z_low,
                    "zone_high": z_high,
                    "strength": lvl["strength"],
                    "latest_confirmed_at_utc": lvl.get("latest_confirmed_at_utc"),
                    "contains_price": contains,
                    "distance_atr": distance,
                    "position": position,
                }
            )
        for lvl in sorted(
            (lvl for lvl in levels[side] if lvl["strength"] < min_strength), key=_level_key
        ):
            non_qualifying.append(
                {"side": side, "low": lvl["low"], "high": lvl["high"], "strength": lvl["strength"]}
            )
    return zones, non_qualifying


def select_clearance_level(
    levels: Mapping[str, Any], price: float, direction: str, min_strength: int
) -> dict[str, Any]:
    """TC6 level selection on the raw T5 interval (§4.1 cases 1-3 and the selected level)."""
    side = "resistance" if direction == "bullish" else "support"
    items = list(levels[side])
    if direction == "bullish":
        relevant = [lvl for lvl in items if lvl["high"] >= price]
        order = sorted(relevant, key=_level_key)
    else:
        relevant = [lvl for lvl in items if lvl["low"] <= price]
        order = sorted(relevant, key=lambda lvl: (-lvl["high"], -lvl["low"], _level_key(lvl)[2]))
    qualifying_any = [lvl for lvl in items if lvl["strength"] >= min_strength]
    qualifying_side = [lvl for lvl in order if lvl["strength"] >= min_strength]
    non_qualifying = [lvl for lvl in order if lvl["strength"] < min_strength]
    nearest_nq = non_qualifying[0] if non_qualifying else None
    return {
        "side": side,
        "empty": not items,
        "any_qualifying": bool(qualifying_any),
        "selected": qualifying_side[0] if qualifying_side else None,
        "nearest_non_qualifying": nearest_nq,
    }


def raw_distance(level: Mapping[str, Any], price: float, atr: float, direction: str) -> float:
    if direction == "bullish":
        return max(0.0, level["low"] - price) / atr
    return max(0.0, price - level["high"]) / atr


def compute_features(
    candles: Sequence[Mapping[str, Any]],
    analysis: Mapping[str, Any],
    timeframe: str,
    *,
    diagnostic: bool,
    parameters: DecisionParameters = DEFAULT_DECISION_PARAMETERS,
) -> dict[str, Any]:
    """All T6 features for the validated window (structure checked by the caller)."""
    n = len(candles)
    tol = T5_PARAMETERS.convergence_tolerance
    closes = [float(c["c"]) for c in candles]
    highs = [float(c["h"]) for c in candles]
    lows = [float(c["l"]) for c in candles]
    last_t = candles[-1]["t"]
    as_of = parse_utc(last_t) + timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    price = closes[-1]

    ind = analysis["indicators"]
    emas = {e["period"]: e for e in ind["ema"]}
    latest = {f"EMA{p}": _latest(f"EMA{p}", emas[p], diagnostic) for p in EMA_PERIODS}
    latest["ATR14"] = _latest("ATR14", ind["atr"], diagnostic, positive=True)
    latest["RSI14"] = _latest("RSI14", ind["rsi"], diagnostic)
    adx = _latest("ADX14", ind["adx"], diagnostic)
    adx["plus_di"] = ind["adx"].get("plus_di")
    adx["minus_di"] = ind["adx"].get("minus_di")
    if adx["usable"] and not (_finite(adx["plus_di"]) and _finite(adx["minus_di"])):
        adx["usable"], adx["reason"] = False, "non-finite directional indicators"
    latest["ADX14"] = adx

    series = {
        20: ema_series(closes, 20),
        50: ema_series(closes, 50),
    }
    atr = atr_series(highs, lows, closes, T5_PARAMETERS.atr_period)
    p_atr = T5_PARAMETERS.atr_period

    def ema_valid(period: int, j: int) -> tuple[bool, str | None]:
        return value_validity(series[period], j, 2.0 / (period + 1), period - 1, tol)

    def atr_valid(j: int) -> tuple[bool, str | None]:
        return value_validity(atr, j, 1.0 / p_atr, p_atr, tol, positive=True)

    # ---- slope50 = (EMA50_{n-1} - EMA50_{n-1-L}) / ATR_{n-1}
    lag = parameters.slope_lookback
    slope: dict[str, Any] = {"value": None, "lookback": lag, "usable": False, "reason": None}
    reasons = []
    for j in (n - 1, n - 1 - lag):
        ok, why = ema_valid(50, j)
        if not ok:
            reasons.append(f"EMA50 at candle {j}: {why}")
    if not latest["ATR14"]["usable"]:
        reasons.append(f"ATR14: {latest['ATR14']['reason']}")
    if reasons:
        slope["reason"] = "; ".join(reasons)
    else:
        slope.update(
            value=(series[50][n - 1] - series[50][n - 1 - lag]) / latest["ATR14"]["value"],
            usable=True,
            from_t=candles[n - 1 - lag]["t"],
            to_t=last_t,
        )

    # ---- pullback window J: the last P closed candles (including the latest)
    p = parameters.pullback_lookback
    window_rows, pull_problems, break_problems = [], [], []
    for j in range(max(0, n - p), n):
        c = candles[j]
        e20_ok, e20_why = ema_valid(20, j)
        e50_ok, e50_why = ema_valid(50, j)
        a_ok, a_why = atr_valid(j)
        closure = c.get("closure")
        if closure != "tick_observed":
            pull_problems.append(f"candle {c['t']}: closure {closure!r}, not tick_observed")
            break_problems.append(f"candle {c['t']}: closure {closure!r}, not tick_observed")
        if not e20_ok:
            pull_problems.append(f"EMA20 at {c['t']}: {e20_why}")
        if not a_ok:
            pull_problems.append(f"ATR14 at {c['t']}: {a_why}")
        if not e50_ok:
            break_problems.append(f"EMA50 at {c['t']}: {e50_why}")
        row = {
            "t": c["t"],
            "low": lows[j],
            "high": highs[j],
            "close": closes[j],
            "ema20": series[20][j],
            "ema50": series[50][j],
            "atr": atr[j],
            "d_bullish": None,
            "d_bearish": None,
        }
        if e20_ok and a_ok:
            row["d_bullish"] = abs(lows[j] - series[20][j]) / atr[j]
            row["d_bearish"] = abs(highs[j] - series[20][j]) / atr[j]
        window_rows.append(row)
    if n < p:
        pull_problems.append(f"{n} candles, {p} needed for the pullback window")
        break_problems.append(f"{n} candles, {p} needed for the pullback window")
    pullback = {
        "lookback": p,
        "usable": not pull_problems,
        "reason": "; ".join(pull_problems) or None,
        "candles": window_rows,
    }
    structure_break = {
        "usable": not break_problems,
        "reason": "; ".join(break_problems) or None,
    }

    # ---- structure (T5)
    structure = analysis["structure"]
    swings_ok, swings_why = _component(structure["swings"], diagnostic)
    swings = swing_sequence(structure["swings"], swings_ok, swings_why, as_of)
    sr = structure["support_resistance"]
    sr_ok, sr_why = _component(sr, diagnostic)
    levels = {
        "usable": sr_ok,
        "reason": sr_why,
        "t5_status": sr.get("status"),
        "t5_trusted": sr.get("trusted"),
        "t5_reason": sr.get("reason"),
        "support_reason": sr.get("support_reason"),
        "resistance_reason": sr.get("resistance_reason"),
        "tolerance": sr.get("tolerance"),
        "support": [dict(lvl) for lvl in sr.get("support") or []],
        "resistance": [dict(lvl) for lvl in sr.get("resistance") or []],
    }

    vol = analysis["volatility"]
    vol_ok, vol_why = _component(vol, diagnostic)
    volatility = {
        "usable": vol_ok,
        "reason": vol_why,
        "regime": vol.get("regime"),
        "percentile": vol.get("percentile"),
        "current_t": vol.get("current_t"),
    }

    return {
        "features_version": FEATURES_VERSION,
        "diagnostic": diagnostic,
        "count": n,
        "last_t": last_t,
        "as_of_utc": iso_utc(as_of),
        "price": {"source": "last_closed_candle_close", "t": last_t, "value": price},
        "latest": latest,
        "slope50": slope,
        "pullback": pullback,
        "structure_break": structure_break,
        "swings": swings,
        "levels": levels,
        "volatility": volatility,
    }


def t5_parameters_dict() -> dict[str, Any]:
    return asdict(T5_PARAMETERS)
