"""T6 trend-continuation setup (revision 2.5 §4-§7): checklist, vetoes, cautions.

The only setup in T6. Consumes the market conditions and the features; never reads the location
dimension (TC6 uses the raw T5 level interval, §4.1). Every check returns ``pass``, ``fail``,
``unavailable`` or ``not_applicable``. Pure and deterministic.
"""

from __future__ import annotations

from typing import Any

from johanna.analysis.features import (
    DEFAULT_DECISION_PARAMETERS,
    DecisionParameters,
    evidence,
    raw_distance,
    select_clearance_level,
)

SETUP = "trend_continuation"
PASS, FAIL, UNAVAILABLE, NOT_APPLICABLE = "pass", "fail", "unavailable", "not_applicable"
NO_LEVEL_DETAIL = (
    "no qualifying opposing level was identified in the analysed history; this does not "
    "establish that no such level exists in the market"
)


def _check(id_: str, result: str, observed: Any = None, **kwargs: Any) -> dict[str, Any]:
    evidence_id = kwargs.pop("evidence_id", None)
    extra = kwargs.pop("extra", {})
    return {
        **evidence(id_, observed, **kwargs),
        "result": result,
        "evidence_id": evidence_id,
        **extra,
    }


def required_inputs(features: dict) -> dict[str, Any]:
    """Direction-independent required setup inputs (§6.1)."""
    latest = features["latest"]
    unavailable = []
    for name in ("EMA20", "EMA50", "EMA200", "ATR14"):
        if not latest[name]["usable"]:
            unavailable.append({"input": name, "reason": latest[name]["reason"]})
    if not features["slope50"]["usable"]:
        unavailable.append(
            {"input": "EMA50 lagged (slope50)", "reason": features["slope50"]["reason"]}
        )
    if not features["pullback"]["usable"]:
        unavailable.append(
            {"input": "EMA20_j/ATR14_j (pullback window)", "reason": features["pullback"]["reason"]}
        )
    if not features["structure_break"]["usable"]:
        unavailable.append(
            {"input": "EMA50_j (pullback window)", "reason": features["structure_break"]["reason"]}
        )
    if not features["levels"]["usable"]:
        unavailable.append({"input": "support_resistance", "reason": features["levels"]["reason"]})
    return {"all_available": not unavailable, "unavailable": unavailable}


def clearance(features: dict, direction: str, p: DecisionParameters) -> dict[str, Any]:
    """TC6 on the raw T5 level interval; one evidence id per §4.1 (first match wins)."""
    levels = features["levels"]
    atr = features["latest"]["ATR14"]
    price = features["price"]["value"]
    bull = direction == "bullish"
    name = "resistance" if bull else "support"
    t5 = {
        "t5_status": levels["t5_status"],
        "t5_trusted": levels["t5_trusted"],
        "t5_reason": levels["t5_reason"],
        "side_reason": levels[f"{name}_reason"],
    }
    base = {
        "threshold": p.clearance_atr,
        "comparison": ">=",
        "source": "structure.support_resistance",
    }
    if not levels["usable"] or not atr["usable"]:
        why = levels["reason"] if not levels["usable"] else atr["reason"]
        return _check(
            "TC6_clearance",
            UNAVAILABLE,
            evidence_id="support_resistance_untrusted",
            detail=f"untrusted input: {why}",
            extra={"price_inside_zone": None, "selected_level": None, "t5": t5},
            **base,
        )
    pick = select_clearance_level(levels, price, direction, p.min_level_strength)
    nq = pick["nearest_non_qualifying"]
    nearest_nq = None
    if nq is not None:
        nearest_nq = {
            "low": nq["low"],
            "high": nq["high"],
            "strength": nq["strength"],
            "distance_atr": raw_distance(nq, price, atr["value"], direction),
        }
    common = {"selected_level": None, "price_inside_zone": None, "t5": t5}
    if pick["empty"]:
        return _check(
            "TC6_clearance",
            PASS,
            evidence_id=f"no_{name}_levels",
            detail=NO_LEVEL_DETAIL,
            extra={**common, "nearest_non_qualifying_level": None},
            **base,
        )
    if not pick["any_qualifying"]:
        return _check(
            "TC6_clearance",
            PASS,
            evidence_id=f"no_qualifying_{name}",
            detail=NO_LEVEL_DETAIL,
            extra={**common, "nearest_non_qualifying_level": nearest_nq},
            **base,
        )
    selected = pick["selected"]
    if selected is None:
        return _check(
            "TC6_clearance",
            PASS,
            evidence_id=f"no_qualifying_{name}_{'above' if bull else 'below'}",
            detail=NO_LEVEL_DETAIL,
            extra={**common, "nearest_non_qualifying_level": nearest_nq},
            **base,
        )
    distance = raw_distance(selected, price, atr["value"], direction)
    inside = selected["low"] <= price <= selected["high"]
    sufficient = distance >= p.clearance_atr
    return _check(
        "TC6_clearance",
        PASS if sufficient else FAIL,
        distance,
        evidence_id=f"{name}_clearance_{'sufficient' if sufficient else 'insufficient'}",
        detail=(
            f"{name} raw T5 interval {selected['low']}-{selected['high']}, strength "
            f"{selected['strength']}; distance in ATR from the close"
        ),
        extra={
            "price_inside_zone": inside,
            "selected_level": {
                "low": selected["low"],
                "high": selected["high"],
                "level": selected["level"],
                "strength": selected["strength"],
                "latest_confirmed_at_utc": selected.get("latest_confirmed_at_utc"),
            },
            "t5": t5,
        },
        **base,
    )


def _pullback(features: dict, direction: str, p: DecisionParameters) -> dict[str, Any]:
    pb = features["pullback"]
    if not pb["usable"]:
        return _check("TC4_pullback", UNAVAILABLE, detail=pb["reason"])
    key = "d_bullish" if direction == "bullish" else "d_bearish"
    field = "low" if direction == "bullish" else "high"
    rows = pb["candles"]
    distances = [
        {"t": r["t"], "d": r[key], field: r[field], "ema20_j": r["ema20"], "atr_j": r["atr"]}
        for r in rows
    ]
    qualifying = [r for r in rows if r[key] <= p.touch_tolerance_atr]
    best = min(rows, key=lambda r: (r[key], r["t"]))
    chosen = qualifying[-1] if qualifying else best
    return _check(
        "TC4_pullback",
        PASS if qualifying else FAIL,
        chosen[key],
        threshold=p.touch_tolerance_atr,
        comparison=f"abs({field}_j - ema20_j) / atr_j <=",
        source="features.pullback",
        t=chosen["t"],
        detail=(
            f"{len(qualifying)} of {len(rows)} candles within the band; "
            f"{field} {chosen[field]}, ema20_j {chosen['ema20']}, atr_j {chosen['atr']}"
        ),
        extra={"candles": distances},
    )


def _structure_break(features: dict, direction: str) -> dict[str, Any]:
    sb = features["structure_break"]
    if not sb["usable"]:
        return {**evidence("V1_ema50_structure_break", detail=sb["reason"]), "triggered": None}
    rows = features["pullback"]["candles"]
    if direction == "bullish":
        breaks = [r["t"] for r in rows if r["close"] < r["ema50"]]
        rule = "close_j < EMA50_j"
    else:
        breaks = [r["t"] for r in rows if r["close"] > r["ema50"]]
        rule = "close_j > EMA50_j"
    return {
        **evidence(
            "V1_ema50_structure_break",
            breaks,
            comparison=rule,
            source="features.pullback",
            detail=f"{len(breaks)} of {len(rows)} candles in the pullback window break structure",
        ),
        "triggered": bool(breaks),
    }


def _swing_conflict(conditions: dict, direction: str, p: DecisionParameters) -> dict[str, Any]:
    swing = conditions["dimensions"]["swing_structure"]
    state = swing["state"]
    opposite = "lower_highs_lower_lows" if direction == "bullish" else "higher_highs_higher_lows"
    record = evidence(
        "V2_swing_context_conflict",
        state,
        threshold=opposite,
        comparison="== complete opposite sequence",
        source="conditions.swing_structure",
    )
    record["pivots"] = swing["pivots"]
    if state == "unavailable":
        return {**record, "detail": "swing context unavailable; not a conflict", "triggered": None}
    is_opposite = state == opposite
    record["detail"] = (
        "confirmed swing structure opposes the setup direction"
        if is_opposite
        else "no complete opposite swing sequence"
    )
    return {**record, "triggered": is_opposite and p.veto_on_context_conflict}


def evaluate_trend_continuation(
    conditions: dict,
    features: dict,
    *,
    parameters: DecisionParameters = DEFAULT_DECISION_PARAMETERS,
) -> dict[str, Any]:
    p = parameters
    dims = conditions["dimensions"]
    regime, bias = dims["trend_regime"]["state"], dims["directional_bias"]["state"]
    direction = bias if bias in ("bullish", "bearish") else None
    required = required_inputs(features)
    latest = features["latest"]
    price = features["price"]["value"]

    checks = []
    if regime == "undetermined":
        checks.append(_check("TC1_regime", UNAVAILABLE, regime, threshold="trending"))
    else:
        checks.append(
            _check(
                "TC1_regime", PASS if regime == "trending" else FAIL, regime, threshold="trending"
            )
        )
    if bias == "undetermined":
        checks.append(_check("TC2_bias", UNAVAILABLE, bias, threshold="bullish or bearish"))
    else:
        checks.append(
            _check("TC2_bias", PASS if direction else FAIL, bias, threshold="bullish or bearish")
        )

    if direction is None:
        for id_ in ("TC3_long_term", "TC4_pullback", "TC5_resumption", "TC6_clearance"):
            checks.append(_check(id_, NOT_APPLICABLE, detail="no setup direction"))
        vetoes = [
            {
                **evidence("V1_ema50_structure_break", detail="no setup direction"),
                "triggered": None,
            },
            {
                **evidence("V2_swing_context_conflict", detail="no setup direction"),
                "triggered": None,
                "pivots": dims["swing_structure"]["pivots"],
            },
        ]
        cautions: list = []
    else:
        bull = direction == "bullish"
        e200, e50, e20 = latest["EMA200"], latest["EMA50"], latest["EMA20"]
        if not (e200["usable"] and e50["usable"]):
            why = e200["reason"] if not e200["usable"] else e50["reason"]
            checks.append(_check("TC3_long_term", UNAVAILABLE, detail=why))
        else:
            ok = (
                price > e200["value"] and e50["value"] > e200["value"]
                if bull
                else price < e200["value"] and e50["value"] < e200["value"]
            )
            checks.append(
                _check(
                    "TC3_long_term",
                    PASS if ok else FAIL,
                    {"close": price, "ema50": e50["value"], "ema200": e200["value"]},
                    comparison="close > ema200 and ema50 > ema200"
                    if bull
                    else "close < ema200 and ema50 < ema200",
                    source="indicators.ema",
                    t=features["last_t"],
                )
            )
        checks.append(_pullback(features, direction, p))
        if not e20["usable"]:
            checks.append(_check("TC5_resumption", UNAVAILABLE, detail=e20["reason"]))
        else:
            ok = price > e20["value"] if bull else price < e20["value"]
            checks.append(
                _check(
                    "TC5_resumption",
                    PASS if ok else FAIL,
                    price,
                    threshold=e20["value"],
                    comparison=">" if bull else "<",
                    source="features.price vs indicators.ema[EMA20]",
                    t=features["last_t"],
                )
            )
        checks.append(clearance(features, direction, p))
        vetoes = [_structure_break(features, direction), _swing_conflict(conditions, direction, p)]
        cautions = []
        if dims["volatility"]["state"] == "high":
            cautions.append(
                evidence(
                    "volatility_high",
                    "high",
                    source="conditions.volatility",
                    detail="caution only: not a veto, not supporting evidence",
                )
            )
        momentum = dims["momentum"]["state"]
        if (bull and momentum == "overbought") or (not bull and momentum == "oversold"):
            cautions.append(
                evidence(
                    f"momentum_{momentum}",
                    momentum,
                    source="conditions.momentum",
                    detail="caution only: not a veto",
                )
            )
        if dims["trend_strength"]["state"] == "extended":
            cautions.append(
                evidence(
                    "trend_strength_extended",
                    "extended",
                    source="conditions.trend_strength",
                    detail="caution only: very high ADX often accompanies a late trend",
                )
            )

    unavailable_context = []
    for name in ("swing_structure", "volatility", "momentum", "trend_strength"):
        unavailable_context.extend(dims[name]["unavailable"])

    failed = [c["id"] for c in checks if c["result"] == FAIL]
    triggered = [v["id"] for v in vetoes if v["triggered"] is True]
    if not required["all_available"]:
        status = "not_evaluable"
    elif all(c["result"] == PASS for c in checks) and not triggered:
        status = "qualified"
    else:
        status = "not_qualified"
    return {
        "setup": SETUP,
        "direction": direction,
        "status": status,
        "required_inputs": required,
        "checklist": checks,
        "failed_conditions": failed,
        "vetoes": vetoes,
        "triggered_vetoes": triggered,
        "cautions": cautions,
        "unavailable_context": unavailable_context,
    }
