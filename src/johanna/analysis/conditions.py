"""T6 market-condition classification (revision 2.5 §3). Knows nothing about setups.

Each dimension returns ``state``, ``role`` (``core`` or ``context``), ``trusted``,
``supporting``, ``contradicting`` and ``unavailable``. Core dimensions (trend regime,
directional bias) decide whether the market is understood; context dimensions inform. No score,
weight or majority vote is used anywhere. Pure and deterministic.
"""

from __future__ import annotations

from typing import Any

from johanna.analysis.features import (
    DEFAULT_DECISION_PARAMETERS,
    DecisionParameters,
    evidence,
    location_zones,
)

CORE, CONTEXT = "core", "context"
LOCATION_ORDER = (
    "at_support",
    "at_resistance",
    "between_levels",
    "above_all_resistance",
    "below_all_support",
)


def _dimension(
    state: Any,
    role: str,
    trusted: bool,
    *,
    supporting: list | None = None,
    contradicting: list | None = None,
    unavailable: list | None = None,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "state": state,
        "role": role,
        "trusted": trusted,
        "supporting": supporting or [],
        "contradicting": contradicting or [],
        "unavailable": unavailable or [],
        **extra,
    }


def _missing(name: str, record: dict[str, Any]) -> dict[str, Any]:
    return {"input": name, "reason": record.get("reason")}


def trend_regime(features: dict, trusted: bool, p: DecisionParameters) -> dict[str, Any]:
    adx = features["latest"]["ADX14"]
    slope = features["slope50"]
    missing = [_missing(n, r) for n, r in (("ADX14", adx), ("slope50", slope)) if not r["usable"]]
    if missing:
        return _dimension("undetermined", CORE, False, unavailable=missing)
    a, s = adx["value"], slope["value"]
    if a >= p.adx_trend_min:
        adx_ev = evidence(
            "adx_at_or_above_trend_min",
            a,
            threshold=p.adx_trend_min,
            comparison=">=",
            source="indicators.adx.value",
            t=adx["t"],
        )
    elif a <= p.adx_range_max:
        adx_ev = evidence(
            "adx_at_or_below_range_max",
            a,
            threshold=p.adx_range_max,
            comparison="<=",
            source="indicators.adx.value",
            t=adx["t"],
        )
    else:
        adx_ev = evidence(
            "adx_between_range_and_trend",
            a,
            threshold=[p.adx_range_max, p.adx_trend_min],
            comparison="range_max < adx < trend_min",
            source="indicators.adx.value",
            t=adx["t"],
        )
    flat = abs(s) <= p.flat_slope_atr
    slope_ev = evidence(
        "ema50_slope_flat" if flat else "ema50_slope_not_flat",
        s,
        threshold=p.flat_slope_atr,
        comparison="abs <=" if flat else "abs >",
        source="features.slope50",
        t=features["last_t"],
    )
    if a >= p.adx_trend_min and not flat:
        return _dimension("trending", CORE, trusted, supporting=[adx_ev, slope_ev])
    if a <= p.adx_range_max and flat:
        return _dimension("ranging", CORE, trusted, supporting=[adx_ev, slope_ev])
    return _dimension("transitional", CORE, trusted, contradicting=[adx_ev, slope_ev])


def directional_bias(features: dict, trusted: bool, p: DecisionParameters) -> dict[str, Any]:
    latest = features["latest"]
    price = features["price"]["value"]
    slope = features["slope50"]
    needed = (
        ("EMA20", latest["EMA20"]),
        ("EMA50", latest["EMA50"]),
        ("ADX14 (+DI/-DI)", latest["ADX14"]),
        ("slope50", slope),
    )
    missing = [_missing(n, r) for n, r in needed if not r["usable"]]
    if missing:
        return _dimension("undetermined", CORE, False, unavailable=missing, votes=[])
    e20, e50 = latest["EMA20"]["value"], latest["EMA50"]["value"]
    pdi, mdi = latest["ADX14"]["plus_di"], latest["ADX14"]["minus_di"]
    s = slope["value"]

    def vote(bull: bool, bear: bool) -> str:
        return "bullish" if bull else "bearish" if bear else "neither"

    t = features["last_t"]
    votes = [
        {
            **evidence("price_vs_ema50", price, threshold=e50, comparison="close vs EMA50", t=t),
            "vote": vote(price > e50, price < e50),
        },
        {
            **evidence("ema20_vs_ema50", e20, threshold=e50, comparison="EMA20 vs EMA50", t=t),
            "vote": vote(e20 > e50, e20 < e50),
        },
        {
            **evidence(
                "di_balance",
                pdi,
                threshold=mdi,
                comparison="+DI vs -DI",
                source="indicators.adx.plus_di/minus_di",
                t=t,
            ),
            "vote": vote(pdi > mdi, mdi > pdi),
        },
        {
            **evidence(
                "ema50_slope",
                s,
                threshold=p.flat_slope_atr,
                comparison="slope50 vs +/-flat_slope_atr",
                source="features.slope50",
                t=t,
            ),
            "vote": vote(s > p.flat_slope_atr, s < -p.flat_slope_atr),
        },
    ]
    cast = [v["vote"] for v in votes]
    if all(v == "bullish" for v in cast):
        state = "bullish"
    elif all(v == "bearish" for v in cast):
        state = "bearish"
    elif "bullish" in cast and "bearish" in cast:
        state = "conflicting"
    else:
        state = "neutral"
    if state in ("bullish", "bearish"):
        supporting, contradicting = votes, []
    elif state == "conflicting":
        supporting, contradicting = [], [v for v in votes if v["vote"] != "neither"]
    else:
        supporting, contradicting = [], []
    return _dimension(
        state, CORE, trusted, supporting=supporting, contradicting=contradicting, votes=votes
    )


def trend_strength(features: dict, trusted: bool, p: DecisionParameters) -> dict[str, Any]:
    adx = features["latest"]["ADX14"]
    if not adx["usable"]:
        return _dimension("undetermined", CONTEXT, False, unavailable=[_missing("ADX14", adx)])
    a = adx["value"]
    if a < p.adx_range_max:
        state = "weak"
    elif a < p.adx_trend_min:
        state = "moderate"
    elif a <= p.adx_extended:
        state = "strong"
    else:
        state = "extended"
    ev = evidence(
        "adx",
        a,
        threshold=[p.adx_range_max, p.adx_trend_min, p.adx_extended],
        comparison="weak < range_max <= moderate < trend_min <= strong <= extended_max < extended",
        source="indicators.adx.value",
        t=adx["t"],
    )
    return _dimension(state, CONTEXT, trusted, supporting=[ev])


def volatility(features: dict, trusted: bool) -> dict[str, Any]:
    vol = features["volatility"]
    if not vol["usable"]:
        return _dimension(
            "undetermined", CONTEXT, False, unavailable=[_missing("volatility_regime", vol)]
        )
    detail = "T5 regime (T5 percentile thresholds, unchanged)"
    ev = evidence(
        "atr_percentile",
        vol["percentile"],
        source="volatility.percentile",
        t=vol["current_t"],
        detail=detail,
    )
    return _dimension(vol["regime"], CONTEXT, trusted, supporting=[ev])


def momentum(features: dict, trusted: bool, p: DecisionParameters) -> dict[str, Any]:
    rsi = features["latest"]["RSI14"]
    if not rsi["usable"]:
        return _dimension("undetermined", CONTEXT, False, unavailable=[_missing("RSI14", rsi)])
    r = rsi["value"]
    state = (
        "overbought" if r >= p.rsi_overbought else "oversold" if r <= p.rsi_oversold else "neutral"
    )
    ev = evidence(
        "rsi",
        r,
        threshold=[p.rsi_oversold, p.rsi_overbought],
        comparison="oversold <= 30 < neutral < 70 <= overbought",
        source="indicators.rsi.value",
        t=rsi["t"],
    )
    return _dimension(state, CONTEXT, trusted, supporting=[ev])


def location(features: dict, trusted: bool, p: DecisionParameters) -> dict[str, Any]:
    """Expanded location zones only (§3.4). Context: no setup check reads this."""
    levels = features["levels"]
    atr = features["latest"]["ATR14"]
    missing = [
        _missing(n, r)
        for n, r in (("support_resistance", levels), ("ATR14", atr))
        if not r["usable"]
    ]
    empty = {
        "zones": [],
        "zones_containing_price": [],
        "overlaps": [],
        "nearest_below": None,
        "nearest_above": None,
        "non_qualifying_levels": [],
    }
    if missing:
        return _dimension(["undetermined"], CONTEXT, False, unavailable=missing, **empty)
    price = features["price"]["value"]
    zones, non_qualifying = location_zones(levels, price, atr["value"], p.min_level_strength)
    if not zones:
        return _dimension(
            ["no_levels"], CONTEXT, trusted, **{**empty, "non_qualifying_levels": non_qualifying}
        )
    support = [z for z in zones if z["side"] == "support"]
    resistance = [z for z in zones if z["side"] == "resistance"]
    applicable = {
        "at_support": any(z["contains_price"] for z in support),
        "at_resistance": any(z["contains_price"] for z in resistance),
        "between_levels": any(z["zone_high"] < price for z in support)
        and any(z["zone_low"] > price for z in resistance),
        "above_all_resistance": bool(resistance)
        and all(z["zone_high"] < price for z in resistance),
        "below_all_support": bool(support) and all(z["zone_low"] > price for z in support),
    }
    states = [s for s in LOCATION_ORDER if applicable[s]]
    overlaps = [
        [a["id"], b["id"]]
        for i, a in enumerate(zones)
        for b in zones[i + 1 :]
        if a["zone_low"] <= b["zone_high"] and b["zone_low"] <= a["zone_high"]
    ]
    below = [z for z in zones if z["position"] == "below"]
    above = [z for z in zones if z["position"] == "above"]

    def brief(z: dict | None) -> dict | None:
        return None if z is None else {"id": z["id"], "distance_atr": z["distance_atr"]}

    nearest_below = min(below, key=lambda z: z["distance_atr"]) if below else None
    nearest_above = min(above, key=lambda z: z["distance_atr"]) if above else None
    return _dimension(
        states,
        CONTEXT,
        trusted,
        zones=zones,
        zones_containing_price=[z["id"] for z in zones if z["contains_price"]],
        overlaps=overlaps,
        nearest_below=brief(nearest_below),
        nearest_above=brief(nearest_above),
        non_qualifying_levels=non_qualifying,
        tolerance=levels["tolerance"],
    )


def swing_structure(features: dict, trusted: bool) -> dict[str, Any]:
    swings = features["swings"]
    unavailable = []
    if swings["state"] == "unavailable":
        unavailable = [{"input": "swing_sequence", "reason": swings["reason"]}]
    return _dimension(
        swings["state"],
        CONTEXT,
        trusted and swings["usable"],
        unavailable=unavailable,
        pivots=swings["pivots"],
        pending_ignored=swings["pending_ignored"],
    )


def classify_market(
    features: dict, *, trusted: bool, parameters: DecisionParameters = DEFAULT_DECISION_PARAMETERS
) -> dict[str, Any]:
    """Every dimension. ``trusted`` is false for diagnostic (provisional) evaluations."""
    p = parameters
    dims = {
        "trend_regime": trend_regime(features, trusted, p),
        "directional_bias": directional_bias(features, trusted, p),
        "trend_strength": trend_strength(features, trusted, p),
        "volatility": volatility(features, trusted),
        "momentum": momentum(features, trusted, p),
        "location": location(features, trusted, p),
        "swing_structure": swing_structure(features, trusted),
    }
    return {
        "summary": {name: d["state"] for name, d in dims.items()},
        "dimensions": dims,
    }
