"""T6 decision: provenance, gate, decision table R1-R7 and the output contract (revision 2.5).

Public entry points (§1.5, §1.6):
- ``evaluate_market_snapshot(snapshot, *, analysed_at, ...)``: production. ``analysed_at`` (the
  caller's calculation time) is required; without a valid one the result is R1 ``NO_TRADE``.
  Deep-copies the snapshot, then runs T4 ``validate_window`` (freshness judged by T2 at
  ``analysed_at``) -> T5 ``analyse_window`` -> T6 on that copy only. Provenance is established
  *by construction*; the copy is not cryptographic integrity protection.
- ``evaluate_market(snapshot, validation, analysis)``: separately supplied inputs, for controlled
  internal use and tests. Provenance rests on checks P1-P10 only (``not_verified`` content).

Both call the private ``_evaluate`` with a fixed provenance mode; callers cannot choose it.
Every result has ``decision.actionable == False``. A ``TRADE_CANDIDATE`` is an analytical
classification, not an instruction, recommendation or authorisation to trade. No prices for
opening, protecting or closing a position are produced. Pure: no clock, no I/O, no MT5.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Iterable, Mapping
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from johanna.analysis.conditions import classify_market
from johanna.analysis.continuity import TIMEFRAME_SECONDS
from johanna.analysis.engine import ANALYSIS_VERSION, SUPPORTED_TIMEFRAMES, analyse_window
from johanna.analysis.features import (
    DEFAULT_DECISION_PARAMETERS,
    EMA_PERIODS,
    DecisionParameters,
    compute_features,
    consistency_check,
    parse_utc,
    required_candles,
    t5_parameters_dict,
)
from johanna.analysis.setups import SETUP, evaluate_trend_continuation
from johanna.analysis.snapshot import SNAPSHOT_VERSION
from johanna.analysis.window import WINDOW_VERSION, validate_window

__all__ = [
    "DEFAULT_DECISION_PARAMETERS",
    "DecisionParameters",
    "evaluate_market",
    "evaluate_market_snapshot",
    "evaluate_market_snapshot_with_validation",
    "required_candles",
]

DECISION_VERSION = 1
NO_TRADE, MARKET_CONDITION, TRADE_CANDIDATE = "NO_TRADE", "MARKET_CONDITION", "TRADE_CANDIDATE"
_CONTENT_BINDING = {"constructed": "by_construction", "supplied": "not_verified"}
_STATUS_FOR_VERDICT = {
    "accepted": {"available", "partial"},
    "provisional": {"provisional"},
    "rejected": {"rejected"},
}
_VERDICT_INDEPENDENT = {"invalid_input", "unsupported_timeframe"}
NOTE = (
    "Analytical classification only. Not a recommendation, instruction or authorisation to trade."
)
STANDING_LIMITATIONS = [
    "thresholds are experimental defaults, not validated against outcomes",
    "single timeframe; no higher-timeframe confirmation",
    "news and economic events are not considered",
    "spread and trading costs are not assessed",
    "no volume information is used",
    "level strength is a count of confirmed pivots, not a reliability measure",
]
PROVENANCE_LIMITATION = {
    "constructed": (
        "provenance established by construction from one in-memory copy of the snapshot; "
        "this is not cryptographic integrity protection, and snapshot authenticity rests on T2"
    ),
    "supplied": (
        "inputs were supplied separately: provenance rests on checks P1-P10 only and candle "
        "prices are not bound to the snapshot id (content_binding not_verified)"
    ),
}


# ------------------------------------------------------------------ small helpers
def _m(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _plain(value: Any) -> Any:
    """Tuples -> lists, recursively, so parameter dictionaries compare JSON-style."""
    if isinstance(value, Mapping):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(v) for v in value]
    return value


# ------------------------------------------------------------------ provenance P1-P10 (§1.4)
def provenance_checks(snapshot: Any, validation: Any, analysis: Any) -> list[dict[str, Any]]:
    """The ten checks, always all evaluated, in order. Each is pass/fail with a detail."""
    s, v, a = _m(snapshot), _m(validation), _m(analysis)
    checks: list[dict[str, Any]] = []

    def add(id_: str, passed: bool, detail: str) -> None:
        checks.append({"id": id_, "passed": bool(passed), "detail": detail})

    if not all(isinstance(x, Mapping) for x in (snapshot, validation, analysis)):
        detail = "snapshot, validation and analysis must all be mappings"
        for i in range(1, 11):
            add(f"P{i}", False, detail)
        return checks

    ids = (s.get("snapshot_id"), v.get("snapshot_id"), a.get("snapshot_id"))
    add(
        "P1",
        all(_nonempty_str(x) for x in ids) and len(set(ids)) == 1,
        f"snapshot_id snapshot={ids[0]!r} validation={ids[1]!r} analysis={ids[2]!r}",
    )
    symbols = (s.get("symbol"), v.get("symbol"), a.get("symbol"))
    add(
        "P2",
        all(_nonempty_str(x) for x in symbols) and len(set(symbols)) == 1,
        f"symbol snapshot={symbols[0]!r} validation={symbols[1]!r} analysis={symbols[2]!r}",
    )
    tfs = (s.get("timeframe"), v.get("timeframe"), a.get("timeframe"))
    add(
        "P3",
        len(set(tfs)) == 1 and tfs[0] in SUPPORTED_TIMEFRAMES,
        f"timeframe snapshot={tfs[0]!r} validation={tfs[1]!r} analysis={tfs[2]!r}; "
        f"supported {', '.join(SUPPORTED_TIMEFRAMES)}",
    )
    brokers = (s.get("broker_symbol"), a.get("broker_symbol"))
    add(
        "P4",
        _nonempty_str(brokers[0]) and brokers[0] == brokers[1],
        f"broker_symbol snapshot={brokers[0]!r} analysis={brokers[1]!r} (validation has none)",
    )
    add(
        "P5",
        "validation" in a and a.get("validation") == dict(v),
        "analysis.validation deep-equals the supplied validation"
        if a.get("validation") == dict(v)
        else "analysis.validation differs from the supplied validation",
    )
    verdict = _m(v.get("verdict")).get("state")
    status = a.get("status")
    status_ok = status in _VERDICT_INDEPENDENT or status in _STATUS_FOR_VERDICT.get(verdict, set())
    add(
        "P6",
        a.get("validation_verdict") == verdict and verdict in _STATUS_FOR_VERDICT and status_ok,
        f"validation verdict {verdict!r}, analysis validation_verdict "
        f"{a.get('validation_verdict')!r}, analysis status {status!r}",
    )
    window = _m(v.get("window"))
    candles = window.get("candles")
    p7_detail, p7 = "window metadata consistent with its candle list", True
    if not isinstance(candles, list) or not all(isinstance(t, str) for t in candles):
        p7, p7_detail = False, "window.candles must be a list of timestamps"
    else:
        times = [parse_utc(t) for t in candles]
        if any(t is None for t in times):
            p7, p7_detail = False, "window.candles contains an unparseable timestamp"
        elif window.get("count") != len(candles) or isinstance(window.get("count"), bool):
            p7, p7_detail = False, f"window.count {window.get('count')!r} != {len(candles)}"
        elif window.get("first_t") != (candles[0] if candles else None) or window.get("last_t") != (
            candles[-1] if candles else None
        ):
            p7, p7_detail = False, "window first_t/last_t do not match the candle list"
        elif any(b <= a_ for a_, b in zip(times, times[1:], strict=False)):
            p7, p7_detail = False, "window candles are not strictly increasing"
    add("P7", p7, p7_detail)
    expected_window = {
        "count": window.get("count"),
        "first_t": window.get("first_t"),
        "last_t": window.get("last_t"),
    }
    if "window" not in a and status in ({"rejected"} | _VERDICT_INDEPENDENT):
        add("P8", True, f"not applicable: analysis status {status!r} has no window")
    else:
        add(
            "P8",
            a.get("window") == expected_window,
            f"analysis window {a.get('window')!r} vs validation window {expected_window!r}",
        )
    items = _m(s.get("bars")).get("items")
    present = (
        {c.get("t") for c in items if isinstance(c, Mapping)} if isinstance(items, list) else set()
    )
    missing = (
        [t for t in candles if t not in present] if isinstance(candles, list) else ["<no list>"]
    )
    add(
        "P9",
        isinstance(items, list) and not missing,
        "every window candle is in snapshot.bars.items"
        if not missing
        else f"{len(missing)} window candle(s) missing from the snapshot, e.g. {missing[0]!r}",
    )
    versions_ok = (
        a.get("analysis_version") == ANALYSIS_VERSION
        and v.get("window_version") == WINDOW_VERSION
        and s.get("snapshot_version") == SNAPSHOT_VERSION
    )
    params_ok = _plain(a.get("parameters")) == _plain(t5_parameters_dict())
    add(
        "P10",
        versions_ok and params_ok,
        f"versions analysis={a.get('analysis_version')!r} window={v.get('window_version')!r} "
        f"snapshot={s.get('snapshot_version')!r}; T5 parameters "
        f"{'as expected' if params_ok else 'differ from the T5 defaults T6 was written against'}",
    )
    return checks


# ------------------------------------------------------------------ evaluation time (freshness)
def evaluation_time_check(validation: Any, analysis: Any) -> dict[str, Any]:
    """Freshness must have been judged by T2 at an explicit evaluation time.

    T4 ``validate_window(..., analysed_at=t)`` re-evaluates the snapshot with T2's
    ``evaluate_snapshot`` at ``t`` (existing limits: bars at most 5 s old, millisecond
    resolution) and records ``dimensions.snapshot.evidence.re_evaluated``. Without ``analysed_at``
    T4 reuses the eligibility judged at assembly, which is not proof of current freshness.
    T5's ``analysed_at_utc`` must equal the validation's ``evaluated_at_utc``.
    """
    v, a = _m(validation), _m(analysis)
    evidence = _m(_m(_m(v.get("dimensions")).get("snapshot")).get("evidence"))
    evaluated_at = v.get("evaluated_at_utc")
    problems = []
    if evidence.get("re_evaluated") is not True:
        problems.append(
            "snapshot freshness was not re-evaluated at an explicit evaluation time "
            "(validate_window called without analysed_at); eligibility judged at assembly is "
            "not proof of current freshness"
        )
    if parse_utc(evaluated_at) is None:
        problems.append(f"validation evaluated_at_utc {evaluated_at!r} is not a UTC timestamp")
    elif a.get("analysed_at_utc") != evaluated_at:
        problems.append(
            f"analysis time {a.get('analysed_at_utc')!r} differs from the freshness evaluation "
            f"time {evaluated_at!r}"
        )
    return {
        "passed": not problems,
        "evaluated_at_utc": evaluated_at,
        "re_evaluated": evidence.get("re_evaluated"),
        "problems": problems,
    }


# ------------------------------------------------------------------ structural checks
_COMPONENT_KEYS = ("status", "trusted")


def _components(analysis: Mapping[str, Any]) -> list[tuple[str, Any]]:
    ind = _m(analysis.get("indicators"))
    st = _m(analysis.get("structure"))
    emas = ind.get("ema") if isinstance(ind.get("ema"), list) else []
    return [
        *((f"EMA{_m(e).get('period')}", e) for e in emas),
        ("RSI", ind.get("rsi")),
        ("ATR", ind.get("atr")),
        ("ADX", ind.get("adx")),
        ("swings", st.get("swings")),
        ("support_resistance", st.get("support_resistance")),
        ("volatility", analysis.get("volatility")),
    ]


def structure_problems(
    analysis: Mapping[str, Any], candles: list[Mapping[str, Any]], timeframe: str
) -> list[str]:
    """Shape and contract checks on an available/partial/provisional analysis (R1 if any)."""
    problems: list[str] = []
    if not candles:
        return ["the validated window is empty"]
    for c in candles:
        if not isinstance(c.get("t"), str) or not all(_finite(c.get(f)) for f in ("h", "l", "c")):
            problems.append(f"window candle {c.get('t')!r} has missing or non-finite prices")
    ind = _m(analysis.get("indicators"))
    emas = ind.get("ema")
    periods = [_m(e).get("period") for e in emas] if isinstance(emas, list) else None
    if periods is None or sorted(p for p in periods if isinstance(p, int)) != list(EMA_PERIODS):
        problems.append(f"indicators.ema must hold periods {EMA_PERIODS}, got {periods!r}")
    accepted = analysis.get("status") in ("available", "partial")
    for name, comp in _components(analysis):
        if not isinstance(comp, Mapping) or not all(k in comp for k in _COMPONENT_KEYS):
            problems.append(f"{name} component missing or lacks status/trusted")
            continue
        if not isinstance(comp["trusted"], bool):
            problems.append(f"{name}.trusted is not a boolean")
        elif accepted and comp["trusted"] != (comp["status"] == "available"):
            problems.append(
                f"{name}: trusted={comp['trusted']} contradicts status {comp['status']!r}"
            )
        elif not accepted and comp["trusted"]:
            problems.append(f"{name}: trusted in a non-accepted analysis")
    if problems:
        return problems
    as_of = parse_utc(candles[-1]["t"])
    if as_of is None:
        return [f"last window candle time {candles[-1]['t']!r} is not a UTC timestamp"]
    as_of += timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    swings = analysis["structure"]["swings"]
    for key in ("confirmed", "pending"):
        if not isinstance(swings.get(key), list):
            problems.append(f"swings.{key} must be a list")
    for p in swings.get("confirmed") or []:
        when = parse_utc(_m(p).get("confirmed_at_utc"))
        if (
            not isinstance(p, Mapping)
            or p.get("type") not in ("high", "low")
            or not _finite(p.get("price"))
            or parse_utc(p.get("pivot_t")) is None
            or when is None
        ):
            problems.append(f"malformed confirmed pivot {p!r}")
        elif when > as_of:
            problems.append(f"pivot {p.get('pivot_t')} confirmed at {when} after as_of (lookahead)")
    sr = analysis["structure"]["support_resistance"]
    for side in ("support", "resistance"):
        if not isinstance(sr.get(side), list):
            problems.append(f"support_resistance.{side} must be a list")
            continue
        for lvl in sr[side]:
            ok = (
                isinstance(lvl, Mapping)
                and all(_finite(lvl.get(f)) for f in ("low", "high", "level"))
                and isinstance(lvl.get("strength"), int)
                and not isinstance(lvl.get("strength"), bool)
                and lvl["strength"] >= 1
                and lvl["low"] <= lvl["high"]
                and isinstance(lvl.get("pivots"), list)
            )
            if not ok:
                problems.append(f"malformed {side} level {lvl!r}")
    if not problems and sr.get("status") == "available":
        if not sr["support"] and not sr["resistance"]:
            problems.append("support_resistance available with both sides empty (T5 contract)")
        for side in ("support", "resistance"):
            if not sr[side] and not sr.get(f"{side}_reason"):
                problems.append(
                    f"support_resistance.{side} empty without {side}_reason (T5 contract)"
                )
        if not _finite(sr.get("tolerance")) or not sr["tolerance"] >= 0:
            problems.append("support_resistance.tolerance missing or invalid")
    return problems


# ------------------------------------------------------------------ assembly
def _window_candles(snapshot: Mapping[str, Any], validation: Mapping[str, Any]) -> list[dict]:
    items: dict[Any, Mapping[str, Any]] = {}
    for c in snapshot["bars"]["items"]:
        if isinstance(c, Mapping):
            items.setdefault(c.get("t"), c)
    return [dict(items[t]) for t in validation["window"]["candles"]]


def _features_summary(features: dict) -> dict[str, Any]:
    levels = features["levels"]
    return {
        "as_of_utc": features["as_of_utc"],
        "price": features["price"],
        "latest": {
            name: {k: rec.get(k) for k in ("value", "t", "status", "usable", "reason")}
            for name, rec in features["latest"].items()
        },
        "slope50": features["slope50"],
        "pullback": features["pullback"],
        "structure_break": features["structure_break"],
        "swings": features["swings"],
        "levels": {
            "usable": levels["usable"],
            "reason": levels["reason"],
            "t5_status": levels["t5_status"],
            "t5_trusted": levels["t5_trusted"],
            "support_reason": levels["support_reason"],
            "resistance_reason": levels["resistance_reason"],
            "tolerance": levels["tolerance"],
            "support_count": len(levels["support"]),
            "resistance_count": len(levels["resistance"]),
        },
        "volatility": features["volatility"],
    }


def _not_evaluated(reason: str) -> dict[str, Any]:
    return {
        "setup": SETUP,
        "direction": None,
        "status": "not_evaluable",
        "required_inputs": {
            "all_available": False,
            "unavailable": [{"input": "validation", "reason": reason}],
        },
        "checklist": [],
        "failed_conditions": [],
        "vetoes": [],
        "triggered_vetoes": [],
        "cautions": [],
        "unavailable_context": [],
    }


def _evaluate(
    snapshot: Any,
    validation: Any,
    analysis: Any,
    *,
    provenance_mode: str,
    parameters: DecisionParameters = DEFAULT_DECISION_PARAMETERS,
    refuse_before_checks: str | None = None,
) -> dict[str, Any]:
    """The single evaluation path behind both public entry points (§1.6).

    ``refuse_before_checks``: a reason to refuse before anything is evaluated (used by the
    production entry point when no valid evaluation time was given); gives R1 ``NO_TRADE``.
    """
    if provenance_mode not in _CONTENT_BINDING:
        raise ValueError(f"unknown provenance mode {provenance_mode!r}")
    s, v, a = _m(snapshot), _m(validation), _m(analysis)
    checks = [] if refuse_before_checks else provenance_checks(snapshot, validation, analysis)
    provenance = {
        "mode": provenance_mode,
        "passed": bool(checks) and all(c["passed"] for c in checks),
        "checks": checks,
        "content_binding": _CONTENT_BINDING[provenance_mode],
    }
    freshness = (
        {"passed": False, "problems": [refuse_before_checks]}
        if refuse_before_checks
        else evaluation_time_check(validation, analysis)
    )
    window = _m(v.get("window"))
    output: dict[str, Any] = {
        "decision_version": DECISION_VERSION,
        "symbol": s.get("symbol"),
        "broker_symbol": s.get("broker_symbol"),
        "timeframe": s.get("timeframe"),
        "as_of_utc": None,
        "evaluated_at_utc": a.get("analysed_at_utc"),
        "inputs": {
            "snapshot_id": s.get("snapshot_id"),
            "analysis_version": a.get("analysis_version"),
            "analysis_status": a.get("status"),
            "validation_verdict": _m(v.get("verdict")).get("state"),
            "window": {k: window.get(k) for k in ("count", "first_t", "last_t")},
            "price_reference": None,
            "provenance": provenance,
            "evaluation_time": freshness,
            "consistency_check": None,
        },
        "gate": {"passed": False, "reasons": []},
        "market_condition": None,
        "opportunity": None,
        "features": None,
        "decision": None,
        "parameters": {"decision": _plain_params(parameters), "t5": a.get("parameters")},
        "limitations": [*STANDING_LIMITATIONS, PROVENANCE_LIMITATION[provenance_mode]],
        "note": NOTE,
    }

    def finish(outcome: str, rule: str, reasons: list[str]) -> dict[str, Any]:
        output["decision"] = {
            "outcome": outcome,
            "rule": rule,
            "reasons": reasons,
            "actionable": False,
        }
        return output

    def gate_fail(reasons: list[str]) -> dict[str, Any]:
        output["gate"] = {"passed": False, "reasons": reasons}
        return finish(NO_TRADE, "R1", reasons)

    if refuse_before_checks:
        return gate_fail([refuse_before_checks])
    # R1a: provenance first; matching indicator values are never accepted as proof.
    failed = [c for c in checks if not c["passed"]]
    if failed:
        return gate_fail([f"provenance check {c['id']} failed: {c['detail']}" for c in failed])
    # R1a': freshness must have been judged by T2/T4 at an explicit evaluation time.
    if not freshness["passed"]:
        return gate_fail([f"evaluation time: {p}" for p in freshness["problems"]])
    # R1b: T5 status gate
    status = a.get("status")
    if status in ("invalid_input", "unsupported_timeframe", "rejected"):
        return gate_fail(
            [f"T5 analysis status {status!r}", *[str(r) for r in a.get("reasons") or []]]
        )
    if status not in ("available", "partial", "provisional"):
        return gate_fail([f"unknown T5 analysis status {status!r}"])
    # R1c: structure and contract checks, then the consistency guard
    candles = _window_candles(s, v)
    timeframe = s["timeframe"]
    problems = structure_problems(a, candles, timeframe)
    if problems:
        return gate_fail([f"invalid_input: {p}" for p in problems])
    consistency = consistency_check(candles, a)
    output["inputs"]["consistency_check"] = consistency
    if not consistency["passed"]:
        return gate_fail(
            [
                f"consistency check failed: {m['input']} recomputed {m['recomputed']!r} "
                f"!= reported {m['reported']!r}"
                for m in consistency["mismatches"]
            ]
        )
    output["gate"] = {"passed": True, "reasons": []}

    diagnostic = status == "provisional"
    features = compute_features(candles, a, timeframe, diagnostic=diagnostic, parameters=parameters)
    output["as_of_utc"] = features["as_of_utc"]
    output["inputs"]["price_reference"] = features["price"]
    output["features"] = _features_summary(features)
    market = classify_market(features, trusted=not diagnostic, parameters=parameters)
    core_undetermined = [
        name
        for name in ("trend_regime", "directional_bias")
        if market["dimensions"][name]["state"] == "undetermined"
    ]
    output["market_condition"] = {
        "trusted": not diagnostic,
        "understood": not diagnostic and not core_undetermined,
        **market,
    }

    # R2: provisional validation, diagnosis only
    if diagnostic:
        output["opportunity"] = _not_evaluated("provisional window: diagnostic only")
        return finish(
            NO_TRADE,
            "R2",
            ["validated window is provisional: conditions shown for diagnosis only, none trusted"],
        )

    opportunity = evaluate_trend_continuation(market, features, parameters=parameters)
    output["opportunity"] = opportunity
    _case_limitations(output, market, opportunity)

    # R3: core conditions undetermined
    if core_undetermined:
        reasons = []
        for name in core_undetermined:
            for item in market["dimensions"][name]["unavailable"]:
                reasons.append(f"{name} undetermined: {item['input']} ({item['reason']})")
        return finish(NO_TRADE, "R3", reasons or [f"{n} undetermined" for n in core_undetermined])
    # R4: conflicting directional evidence
    bias = market["dimensions"]["directional_bias"]
    if bias["state"] == "conflicting":
        bull = [vv["id"] for vv in bias["votes"] if vv["vote"] == "bullish"]
        bear = [vv["id"] for vv in bias["votes"] if vv["vote"] == "bearish"]
        return finish(
            NO_TRADE,
            "R4",
            [
                f"directional evidence conflicts: bullish votes {bull}, bearish votes {bear}",
            ],
        )
    if "V2_swing_context_conflict" in opportunity["triggered_vetoes"]:
        return finish(
            NO_TRADE,
            "R4",
            [f"confirmed swing structure opposes the {opportunity['direction']} bias"],
        )
    # R5: required setup input unavailable
    if not opportunity["required_inputs"]["all_available"]:
        return finish(
            NO_TRADE,
            "R5",
            [
                f"required setup input unavailable: {u['input']} ({u['reason']})"
                for u in opportunity["required_inputs"]["unavailable"]
            ],
        )
    # R7 / R6
    if opportunity["status"] == "qualified":
        direction = opportunity["direction"]
        reasons = [f"{SETUP} ({direction}): 6 of 6 required checks passed, no veto triggered"]
        reasons += [f"caution: {c['id']}" for c in opportunity["cautions"]]
        return finish(TRADE_CANDIDATE, "R7", reasons)
    reasons = [f"market understood; {SETUP} not qualified"]
    reasons += [f"failed check: {cid}" for cid in opportunity["failed_conditions"]]
    reasons += [f"veto triggered: {vid}" for vid in opportunity["triggered_vetoes"]]
    return finish(MARKET_CONDITION, "R6", reasons)


def _plain_params(parameters: DecisionParameters) -> dict[str, Any]:
    return asdict(parameters)


def _case_limitations(output: dict, market: dict, opportunity: dict) -> None:
    lims = output["limitations"]
    if market["dimensions"]["swing_structure"]["state"] == "unavailable":
        lims.append("swing context unavailable: no swing-sequence evidence either way")
    for check in opportunity["checklist"]:
        if (
            check["id"] == "TC6_clearance"
            and check.get("evidence_id", "")
            and check["evidence_id"].startswith("no_")
        ):
            lims.append(
                "TC6 passed because no qualifying opposing level was identified in the analysed "
                "history; this does not establish that none exists in the market"
            )
    if market["dimensions"]["swing_structure"]["state"] != "unavailable":
        lims.append("swing pivots have no age limit; their timestamps are shown in the output")


# ------------------------------------------------------------------ public entry points
def evaluate_market(
    snapshot: Mapping[str, Any],
    validation: Mapping[str, Any],
    analysis: Mapping[str, Any],
    *,
    parameters: DecisionParameters = DEFAULT_DECISION_PARAMETERS,
) -> dict[str, Any]:
    """Evaluate separately supplied inputs (internal use and tests); ``supplied`` provenance."""
    return _evaluate(
        snapshot, validation, analysis, provenance_mode="supplied", parameters=parameters
    )


def evaluate_market_snapshot(
    snapshot: Mapping[str, Any],
    *,
    analysed_at: datetime | None,
    required_count: int | None = None,
    excluded_opens: Iterable[Any] = (),
    peers: Mapping[str, Iterable[Any]] | None = None,
    parameters: DecisionParameters = DEFAULT_DECISION_PARAMETERS,
) -> dict[str, Any]:
    """Production entry point: deep copy, then T4 -> T5 -> T6 on that copy (§1.5).

    ``analysed_at`` is required: the caller's calculation time (timezone-aware). T2/T4 judge
    the snapshot's freshness at that time with the existing limits; T6 never reads a clock.
    ``None`` or a naive/non-datetime value gives R1 ``NO_TRADE`` without evaluating anything,
    because eligibility judged at assembly is not proof of current freshness.
    """
    result, _ = _evaluate_constructed(
        snapshot,
        analysed_at=analysed_at,
        required_count=required_count,
        excluded_opens=excluded_opens,
        peers=peers,
        parameters=parameters,
    )
    return result


def evaluate_market_snapshot_with_validation(
    snapshot: Mapping[str, Any],
    *,
    analysed_at: datetime | None,
    required_count: int | None = None,
    excluded_opens: Iterable[Any] = (),
    peers: Mapping[str, Iterable[Any]] | None = None,
    parameters: DecisionParameters = DEFAULT_DECISION_PARAMETERS,
) -> dict[str, Any]:
    """``evaluate_market_snapshot`` plus the exact T4 validation object that evaluation used.

    Returns ``{"result": <T6 result, identical to evaluate_market_snapshot>, "validation":
    <the object validate_window returned and T5/T6 consumed, or None>}``. ``validation`` is
    ``None`` on the paths that refuse before T4 runs (missing/invalid ``analysed_at``,
    non-mapping snapshot). T4 is never run a second time.
    """
    result, validation = _evaluate_constructed(
        snapshot,
        analysed_at=analysed_at,
        required_count=required_count,
        excluded_opens=excluded_opens,
        peers=peers,
        parameters=parameters,
    )
    return {"result": result, "validation": validation}


def _evaluate_constructed(
    snapshot: Any,
    *,
    analysed_at: Any,
    required_count: int | None,
    excluded_opens: Iterable[Any],
    peers: Mapping[str, Iterable[Any]] | None,
    parameters: DecisionParameters,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """The single constructed pipeline: deep copy, then T4 -> T5 -> T6 (§1.5).

    Returns ``(result, validation)``; ``validation`` is the very object passed to T5 and T6,
    or ``None`` when the pipeline refuses before T4.
    """
    work = copy.deepcopy(snapshot)
    if not isinstance(analysed_at, datetime) or analysed_at.utcoffset() is None:
        reason = (
            "no evaluation time given: analysed_at is required so that snapshot freshness is "
            "judged at calculation time"
            if analysed_at is None
            else f"analysed_at must be a timezone-aware datetime, got {analysed_at!r}"
        )
        result = _evaluate(
            work,
            {},
            {},
            provenance_mode="constructed",
            parameters=parameters,
            refuse_before_checks=reason,
        )
        return result, None
    if not isinstance(work, Mapping):
        result = _evaluate(work, {}, {}, provenance_mode="constructed", parameters=parameters)
        return result, None
    validation = validate_window(
        work,
        required_count=required_candles(parameters) if required_count is None else required_count,
        analysed_at=analysed_at,
        excluded_opens=list(excluded_opens),
        peers=peers,
    )
    analysis = analyse_window(work, validation)
    result = _evaluate(
        work, validation, analysis, provenance_mode="constructed", parameters=parameters
    )
    return result, validation
