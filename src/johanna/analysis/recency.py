"""Market-data recency (E1): is the snapshot's closed-candle history current and complete?

Pure and deterministic: everything comes from the T2 snapshot and T4's own window selection;
no clock reads, no MT5. T4 calls :func:`assess_recency` and reports the result as its
``recency`` dimension plus a top-level ``recency`` summary.

Request freshness (T2) says when data was fetched; recency says whether the candles are the
ones the market state implies. Four parts (design revisions 1-8, final form):

- **R0 reference**: the snapshot tick time (``T_tick``), the tick retrieval time (``T_read``)
  and the bars retrieval time (``T_bars``) must exist and agree with the bars response.
- **R1 future candle**: no candle may open after the slot containing the later of ``T_tick``
  and ``T_bars``.
- **R-CV closure labels**: every canonical candle's closure label must be the one T1 would have
  produced (``analysis.closure``) for its close time ``t + step``; ``complete`` and ``close_t``
  must agree. Results: ``V`` verified closed, ``Fok`` valid forming, ``U`` unknown (not
  recomputable), ``X`` contradictory.
- **Coverage (R2 tick, R4 clock)**: every required slot from the T4 window's first candle up to
  the expected latest closed slot ``E`` is checked individually. R2 uses ``E(T_tick)``; R4
  (only when the tick is stale during scheduled open time) uses ``E(T_read - grace)``.
  Exclusion takes precedence; then a slot is verified (``V``), unverified (``U``: may only
  rescue a slot, giving PROVISIONAL) or uncovered (forming, contradictory or absent: FAIL).
  Forming and contradictory candles never count as closed history.
- **R5 session agreement**: a tick or newest candle inside a configured closure contradicts
  the schedule (PROVISIONAL).

:func:`aggregate` combines R2 and R4: FAIL dominates; the only route to PASS is R2 PASS with R4
not triggered; a triggered R4 (stale tick) always adds ``tick_stale_in_open_session``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from johanna.analysis.closure import (
    CLOCK_ONLY,
    COMPLETION_GRACE,
    FORMING,
    TICK_OBSERVED,
    classify_closure,
)
from johanna.analysis.continuity import (
    MARKET_WIDE_UNVERIFIED,
    TIMEFRAME_SECONDS,
    rollover_spans,
    scheduled_open_seconds,
    slot_required,
)
from johanna.analysis.sessions import (
    EXNESS_T0_PROVISIONAL,
    SessionConfig,
    closures_overlapping,
    is_scheduled_closed,
)
from johanna.analysis.snapshot import DEFAULT_LIMITS, SnapshotLimits

RECENCY_VERSION = 1
FAIL, PROVISIONAL = "fail", "provisional"  # reason severities (as T4's dimensions)
PASS_STATUS, NOT_APPLICABLE, NOT_TRIGGERED, NOT_EVALUATED = (
    "pass",
    "not_applicable",
    "not_triggered",
    "not_evaluated",
)
LOOKBACK = timedelta(days=16)  # longer than any configured closure (Christmas + weekend ~3 d)
MAX_LISTED = 50
EXCUSED_T3_CLASSES = frozenset({"expected_no_tick"})  # T3: an expected break, not a gap

# Classification of the dimension, most severe first (design revision 6-8 precedence).
_CLASS_OF_CODE = {
    "recency_reference_missing": "unverifiable",
    "candle_after_reference": "future_candle",
    "forming_candle_behind_tick": "closure_contradiction",
    "forming_candle_past_clock_close": "closure_contradiction",
    "tick_closure_unsupported": "closure_contradiction",
    "clock_closure_premature": "closure_contradiction",
    "closure_precedence_mismatch": "closure_contradiction",
    "closure_flag_mismatch": "closure_contradiction",
    "close_time_mismatch": "closure_contradiction",
    "closed_history_gap": "incomplete_closed_history",
    "closed_history_behind_tick": "stale_closed_history",
    "history_behind_clock": "stale_feed",
    "session_schedule_contradiction": "session_contradiction",
    "closed_history_gap_market_wide_unverified": "unverified_gap",
    "expected_slot_excluded": "excluded_reference",
    "recency_relies_on_unverified_closure": "unverified_coverage",
    "tick_stale_in_open_session": "tick_stale_open_session",
}
CLASS_PRECEDENCE = (
    "unverifiable",
    "future_candle",
    "closure_contradiction",
    "incomplete_closed_history",
    "stale_closed_history",
    "stale_feed",
    "session_contradiction",
    "unverified_gap",
    "excluded_reference",
    "unverified_coverage",
    "tick_stale_open_session",
    "current_rollover",
    "current_market_closed",
    "current",
)


# ---------------------------------------------------------------- small helpers
def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.astimezone(UTC) if value.tzinfo is not None else None
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _floor(moment: datetime, step: timedelta) -> datetime:
    seconds = int(step.total_seconds())
    epoch = moment.timestamp()
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=UTC)


def _reason(code: str, severity: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, "detail": detail, **extra}


def _slots(slots: Sequence[datetime], timeframe: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "count": len(slots),
        "first": _iso(slots[0]) if slots else None,
        "last": _iso(slots[-1]) if slots else None,
        "slots": [_iso(s) for s in slots[:MAX_LISTED]],
    }
    if timeframe == "D1":
        kinds = sorted({"sunday_partial_d1" for s in slots if s.weekday() == 6})
        out["kinds"] = kinds
    return out


def _severity(reasons: Iterable[Mapping[str, Any]]) -> str:
    severities = {r["severity"] for r in reasons}
    return FAIL if FAIL in severities else PROVISIONAL if severities else PASS_STATUS


def expected_latest_closed(
    moment: datetime, step: timedelta, config: SessionConfig = EXNESS_T0_PROVISIONAL
) -> datetime | None:
    """``E(t)``: the latest slot ``s`` with ``s + step <= t`` that the schedule requires
    (T3's rule), looking back at most 16 days; ``None`` if there is none."""
    candidate = _floor(moment - step, step)
    earliest = moment - LOOKBACK
    closures = closures_overlapping(earliest - step, moment, config)
    while candidate >= earliest:
        if slot_required(candidate, step, closures):
            return candidate
        candidate -= step
    return None


def _required_slots(
    low: datetime, high: datetime, step: timedelta, config: SessionConfig
) -> list[datetime]:
    if low > high:
        return []
    closures = closures_overlapping(low, high + step, config)
    out, slot = [], low
    while slot <= high:
        if slot_required(slot, step, closures):
            out.append(slot)
        slot += step
    return out


def _inside_closure(start: datetime, step: timedelta, config: SessionConfig) -> bool:
    """Whether ``[start, start + step)`` lies entirely inside configured closures (T4.1's
    meaning: closures without their reopen tolerance)."""
    end = start + step
    spans = sorted(
        (max(c.start, start), min(c.end, end))
        for c in closures_overlapping(start, end, config)
        if c.start < end and c.end > start
    )
    cursor = start
    for low, high in spans:
        if low > cursor:
            return False
        cursor = max(cursor, high)
    return cursor >= end


# ---------------------------------------------------------------- R-CV
_MISMATCH = {
    (FORMING, TICK_OBSERVED): "forming_candle_behind_tick",
    (FORMING, CLOCK_ONLY): "forming_candle_past_clock_close",
    (TICK_OBSERVED, FORMING): "tick_closure_unsupported",
    (TICK_OBSERVED, CLOCK_ONLY): "tick_closure_unsupported",
    (CLOCK_ONLY, TICK_OBSERVED): "closure_precedence_mismatch",
    (CLOCK_ONLY, FORMING): "clock_closure_premature",
}
_MISMATCH_DETAIL = {
    "forming_candle_behind_tick": "candle(s) labelled forming although the snapshot tick is at "
    "or after their close",
    "forming_candle_past_clock_close": "candle(s) labelled forming although the bars were "
    "retrieved at or after their close plus the completion grace",
    "tick_closure_unsupported": "candle(s) labelled tick_observed although the snapshot tick is "
    "before their close",
    "clock_closure_premature": "candle(s) labelled clock_only although neither the tick nor the "
    "clock (close plus the completion grace) establishes closure",
    "closure_precedence_mismatch": "candle(s) labelled clock_only although the snapshot tick "
    "already establishes closure (T1 would label them tick_observed)",
    "closure_flag_mismatch": "candle(s) whose complete flag disagrees with their closure label",
    "close_time_mismatch": "candle(s) whose close_t differs from open + timeframe",
}


@dataclass
class _Candles:
    verified: set[datetime] = field(default_factory=set)
    unknown: set[datetime] = field(default_factory=set)
    forming: set[datetime] = field(default_factory=set)
    contradictory: dict[datetime, dict[str, Any]] = field(default_factory=dict)


def _validate_closures(
    canonical: Mapping[datetime, Mapping[str, Any]],
    step: timedelta,
    t_tick: datetime,
    t_bars: datetime,
) -> _Candles:
    out = _Candles()
    for moment, candle in canonical.items():
        label = candle.get("closure")
        close = moment + step
        if label not in (FORMING, TICK_OBSERVED, CLOCK_ONLY):
            out.unknown.add(moment)  # unknown/unrecognised: never recomputed, never trusted
            continue
        record = {"t": _iso(moment), "close_t": _iso(close), "label": label}
        close_t = candle.get("close_t")
        if close_t is not None and _parse(close_t) != close:
            out.contradictory[moment] = {
                **record,
                "code": "close_time_mismatch",
                "expected": None,
                "reported_close_t": str(close_t),
            }
            continue
        expected = classify_closure(close, t_tick, t_bars)
        if label != expected:
            out.contradictory[moment] = {
                **record,
                "code": _MISMATCH[(label, expected)],
                "expected": expected,
            }
            continue
        complete = candle.get("complete")
        if complete is not None and bool(complete) != (expected != FORMING):
            out.contradictory[moment] = {
                **record,
                "code": "closure_flag_mismatch",
                "expected": expected,
                "complete": bool(complete),
            }
            continue
        (out.forming if label == FORMING else out.verified).add(moment)
    return out


# ---------------------------------------------------------------- coverage (R2 / R4)
@dataclass
class RuleResult:
    """One coverage check. ``status``: pass / provisional / fail / not_applicable /
    not_triggered."""

    rule: str
    status: str
    reasons: list[dict[str, Any]] = field(default_factory=list)
    expected: datetime | None = None
    na_reason: str | None = None
    summary: dict[str, Any] = field(default_factory=dict)


def not_triggered(rule: str = "R4", **summary: Any) -> RuleResult:
    return RuleResult(rule, NOT_TRIGGERED, summary=summary)


@dataclass
class _Context:
    timeframe: str
    step: timedelta
    config: SessionConfig
    window_first: datetime
    window_last: datetime
    candles: _Candles
    excluded: frozenset[datetime]
    t3_gaps: list[tuple[datetime, datetime, str]]


def _t3_class(slot: datetime, ctx: _Context) -> str | None:
    for start, end, classification in ctx.t3_gaps:
        if start <= slot < end:
            return classification
    return None


def check_coverage(rule: str, expected: datetime | None, ctx: _Context) -> RuleResult:
    """Revision 7/8 ``check()``: every required slot in ``[W_first, E]``, exclusion first."""
    if expected is None:
        return RuleResult(rule, NOT_APPLICABLE, na_reason="expected_slot")
    if ctx.window_first > expected:
        return RuleResult(
            rule, NOT_APPLICABLE, expected=expected, na_reason="reference_before_window"
        )
    required = _required_slots(ctx.window_first, expected, ctx.step, ctx.config)
    excluded = [s for s in required if s in ctx.excluded]
    checked = [s for s in required if s not in ctx.excluded]
    verified, unknown = ctx.candles.verified, ctx.candles.unknown
    reasons: list[dict[str, Any]] = []
    strict_gap = [s for s in checked if s not in verified]
    permissive_gap = [s for s in strict_gap if s not in unknown]
    if not required:  # defensive: E is required by construction
        reasons.append(
            _reason(
                "recency_reference_missing",
                FAIL,
                "no required slot in the coverage interval",
                missing=["no_required_slots"],
                rule=[rule],
            )
        )
    elif not checked:
        reasons.append(
            _reason(
                "recency_reference_missing",
                FAIL,
                "every required slot in the coverage interval is analytically excluded: no "
                "positive coverage",
                missing=["reference_interval"],
                rule=[rule],
            )
        )
    else:
        inner = [s for s in permissive_gap if s <= ctx.window_last]
        trailing = [s for s in permissive_gap if s > ctx.window_last]
        if inner:
            classes = {s: _t3_class(s, ctx) for s in inner}
            excused = [s for s in inner if classes[s] in EXCUSED_T3_CLASSES]
            market_wide = [s for s in inner if classes[s] == MARKET_WIDE_UNVERIFIED]
            failing = [s for s in inner if s not in excused and s not in market_wide]
            if failing:
                reasons.append(
                    _reason(
                        "closed_history_gap",
                        FAIL,
                        f"{len(failing)} required slot(s) inside the analysis window have no "
                        "verified or unknown candle",
                        missing_required_slots=_slots(failing, ctx.timeframe),
                        rule=[rule],
                    )
                )
            if market_wide:
                reasons.append(
                    _reason(
                        "closed_history_gap_market_wide_unverified",
                        PROVISIONAL,
                        "required slot(s) missing on every compared symbol (T3): possibly an "
                        "unconfigured closure, not verified",
                        missing_required_slots=_slots(market_wide, ctx.timeframe),
                        rule=[rule],
                    )
                )
        if trailing:
            code = "closed_history_behind_tick" if rule == "R2" else "history_behind_clock"
            reference = "snapshot tick" if rule == "R2" else "backend clock at retrieval"
            reasons.append(
                _reason(
                    code,
                    FAIL,
                    f"{len(trailing)} required slot(s) expected closed by the {reference} are "
                    "missing after the window's latest candle",
                    missing_required_slots=_slots(trailing, ctx.timeframe),
                    latest_window_t=_iso(ctx.window_last),
                    expected_latest_closed_t=_iso(expected),
                    rule=[rule],
                )
            )
        relied = [s for s in strict_gap if s in unknown]
        if relied:
            reasons.append(
                _reason(
                    "recency_relies_on_unverified_closure",
                    PROVISIONAL,
                    f"{len(relied)} required slot(s) are covered only by candles whose closure "
                    "is unknown",
                    unverified_candles=_slots(relied),
                    rule=[rule],
                )
            )
        if expected in ctx.excluded:
            reasons.append(
                _reason(
                    "expected_slot_excluded",
                    PROVISIONAL,
                    "the expected latest closed slot is analytically excluded: currency through "
                    "it is not established by data",
                    expected_latest_closed_t=_iso(expected),
                    rule=[rule],
                )
            )
    status = _severity(reasons)
    return RuleResult(
        rule,
        status,
        reasons,
        expected=expected,
        summary={
            "interval": [_iso(ctx.window_first), _iso(expected)],
            "required": len(required),
            "excluded": [_iso(s) for s in excluded[:MAX_LISTED]],
            "strict_gap": _slots(strict_gap),
            "permissive_gap": _slots(permissive_gap),
            "basis": {
                "E_verified": expected in verified and expected not in ctx.excluded,
                "slots_verified": len(checked) - len(strict_gap),
            },
        },
    )


# ---------------------------------------------------------------- aggregation
_TICK_STALE = "tick_stale_in_open_session"


def _row(r2: str, r4: str) -> str:
    return f"R2={r2},R4={r4}"


def aggregate(
    r2: RuleResult, r4: RuleResult, tick_stale_reason: dict[str, Any] | None = None
) -> tuple[str, list[dict[str, Any]], str]:
    """Combine R2 and R4 (revision 8, exhaustive truth table). Returns
    ``(status, reasons, row)``; ``status`` is pass / provisional / fail."""
    reasons = [*r2.reasons, *r4.reasons]
    if r4.status != NOT_TRIGGERED:
        reasons.append(
            tick_stale_reason
            or _reason(_TICK_STALE, PROVISIONAL, "the snapshot tick is stale during open time")
        )
    row = _row(r2.status, r4.status)

    def missing(result: RuleResult) -> dict[str, Any]:
        return _reason(
            "recency_reference_missing",
            FAIL,
            f"{result.rule}: no applicable reference establishes that the window is current",
            missing=[result.na_reason or "reference_before_window"],
            rule=[result.rule],
        )

    no_reference = False
    if r4.status == NOT_APPLICABLE:  # stale tick and no clock coverage (rows h-k)
        reasons.append(missing(r4))
        no_reference = True
    elif r2.status == NOT_APPLICABLE and r4.status == NOT_TRIGGERED:  # Q1: conservative
        reasons.append(missing(r2))
        no_reference = True
    if no_reference or FAIL in (r2.status, r4.status):  # FAIL dominates
        return FAIL, reasons, row
    if r2.status == PASS_STATUS and r4.status == NOT_TRIGGERED:
        return PASS_STATUS, reasons, row  # the only route to PASS
    return PROVISIONAL, reasons, row


def _merge(reasons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Identical reasons reported by R2 and R4 (e.g. the same inner gap) appear once, with
    both rules listed."""
    out: list[dict[str, Any]] = []
    for reason in reasons:
        key = {k: v for k, v in reason.items() if k not in ("rule", "detail")}
        for existing in out:
            if {k: v for k, v in existing.items() if k not in ("rule", "detail")} == key:
                rules = set(existing.get("rule", [])) | set(reason.get("rule", []))
                if rules:
                    existing["rule"] = sorted(rules)
                break
        else:
            out.append(dict(reason))
    return out


def _classify(reasons: Sequence[Mapping[str, Any]], context: str) -> str:
    classes = {_CLASS_OF_CODE.get(r["code"], "unverifiable") for r in reasons}
    classes.add(context)
    return next(name for name in CLASS_PRECEDENCE if name in classes)


# ---------------------------------------------------------------- entry point
def assess_recency(
    snapshot: Mapping[str, Any],
    *,
    canonical: Mapping[datetime, Mapping[str, Any]],
    window_times: Sequence[datetime],
    timeframe: str,
    config: SessionConfig = EXNESS_T0_PROVISIONAL,
    limits: SnapshotLimits = DEFAULT_LIMITS,
    excluded_opens: Iterable[Any] = (),
    t3_gaps: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Recency of one snapshot's window. Returns ``(reasons, summary)``.

    ``canonical``: T4's canonical candle per valid aligned open time (forming included);
    ``window_times``: T4's selected window (unchanged selection); ``t3_gaps``: T3's gap
    records for the window (severity alignment); ``excluded_opens``: the caller's analytical
    exclusions (the same ones T3 honours).
    """
    step = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    tick = snapshot.get("tick") if isinstance(snapshot.get("tick"), Mapping) else {}
    bars = snapshot.get("bars") if isinstance(snapshot.get("bars"), Mapping) else {}
    t_tick = _parse(tick.get("time_utc"))
    t_read = _parse(tick.get("retrieved_at_utc"))
    t_bars = _parse(bars.get("retrieved_at_utc"))
    summary: dict[str, Any] = {
        "recency_version": RECENCY_VERSION,
        "timeframe": timeframe,
        "session_config": config.name,
        "close_grace_seconds": COMPLETION_GRACE.total_seconds(),
        "reference": {
            "tick_time_utc": _iso(t_tick),
            "tick_retrieved_at_utc": _iso(t_read),
            "bars_retrieved_at_utc": _iso(t_bars),
            "tick_age_seconds": round((t_read - t_tick).total_seconds(), 3)
            if t_tick and t_read
            else None,
        },
    }

    def finish(reasons, context="current", **extra):
        reasons = _merge(reasons)
        summary.update(extra)
        summary["status"] = _severity(reasons)
        summary["classification"] = _classify(reasons, context)
        summary["context"] = context
        return reasons, summary

    # R0: references present and consistent
    missing = [
        name
        for name, value in (
            ("tick_time_utc", t_tick),
            ("tick_retrieved_at_utc", t_read),
            ("bars_retrieved_at_utc", t_bars),
        )
        if value is None
    ]
    latest_tick = bars.get("latest_tick_utc")
    if t_tick is not None and latest_tick is not None and _parse(latest_tick) != t_tick:
        missing.append("consistent_tick_reference")
    if not canonical:
        missing.append("closed_or_unverified_candle")
    if missing:
        return finish(
            [
                _reason(
                    "recency_reference_missing",
                    FAIL,
                    "recency cannot be judged: " + ", ".join(missing),
                    missing=missing,
                )
            ],
            rules={"R2": {"status": NOT_EVALUATED}, "R4": {"status": NOT_EVALUATED}},
        )

    reasons: list[dict[str, Any]] = []
    candles = _validate_closures(canonical, step, t_tick, t_bars)
    observed = sorted(canonical)
    newest = observed[-1]
    forming_sorted = sorted(t for t, c in canonical.items() if c.get("closure") == FORMING)
    summary.update(
        {
            "latest_observed_t": _iso(newest),
            "latest_forming_t": _iso(forming_sorted[-1]) if forming_sorted else None,
            "forming_candles": [_iso(t) for t in forming_sorted[:MAX_LISTED]],
            "latest_verified_closed_t": _iso(max(candles.verified)) if candles.verified else None,
            "latest_covered_t": _iso(max(candles.verified | candles.unknown))
            if candles.verified | candles.unknown
            else None,
            "unverified_closure": [_iso(t) for t in sorted(candles.unknown)[:MAX_LISTED]],
            "contradictory_candles": len(candles.contradictory),
            "forming_within_close_grace": any(t + step <= t_read for t in candles.forming),
            "window": {
                "first_t": _iso(window_times[0]) if window_times else None,
                "last_t": _iso(window_times[-1]) if window_times else None,
            },
        }
    )

    # R1: no candle from the future
    reference_slot = _floor(max(t_tick, t_bars), step)
    if newest > reference_slot:
        reasons.append(
            _reason(
                "candle_after_reference",
                FAIL,
                "a candle opens after the slot containing the snapshot tick and bars retrieval",
                latest_observed_t=_iso(newest),
                reference_slot_t=_iso(reference_slot),
            )
        )

    # R-CV: one reason per contradiction kind, every affected candle listed
    by_code: dict[str, list[dict[str, Any]]] = {}
    for moment in sorted(candles.contradictory):
        record = candles.contradictory[moment]
        by_code.setdefault(record["code"], []).append(
            {k: v for k, v in record.items() if k != "code"}
        )
    for code, records in by_code.items():
        reasons.append(
            _reason(
                code,
                FAIL,
                f"{len(records)} {_MISMATCH_DETAIL[code]}",
                candles=records[:MAX_LISTED],
                count=len(records),
                tick_time_utc=_iso(t_tick),
                bars_retrieved_at_utc=_iso(t_bars),
            )
        )

    # R5: session agreement
    if is_scheduled_closed(t_tick, config):
        reasons.append(
            _reason(
                "session_schedule_contradiction",
                PROVISIONAL,
                "the snapshot tick falls inside a configured market closure",
                kind="tick_inside_closure",
                tick_time_utc=_iso(t_tick),
            )
        )
    if _inside_closure(newest, step, config):
        reasons.append(
            _reason(
                "session_schedule_contradiction",
                PROVISIONAL,
                "the newest candle lies entirely inside a configured market closure",
                kind="observed_candle_inside_closure",
                latest_observed_t=_iso(newest),
            )
        )

    excluded = frozenset(t for t in (_parse(v) for v in excluded_opens) if t is not None)
    summary["excluded_slots_with_candles"] = [
        _iso(t) for t in sorted(excluded & set(canonical))[:MAX_LISTED]
    ]

    # R0 (closed evidence) and the window
    if not (candles.verified | candles.unknown) or not window_times:
        missing = (
            ["closed_or_unverified_candle"]
            if not (candles.verified | candles.unknown)
            else ["analysis_window"]
        )
        reasons.append(
            _reason(
                "recency_reference_missing",
                FAIL,
                "recency cannot be judged: " + ", ".join(missing),
                missing=missing,
            )
        )
        return finish(
            reasons,
            rules={"R2": {"status": NOT_EVALUATED}, "R4": {"status": NOT_EVALUATED}},
        )

    gaps = []
    for gap in t3_gaps:
        start, end = _parse(gap.get("start_utc")), _parse(gap.get("end_utc"))
        if start is not None and end is not None:
            gaps.append((start, end, gap.get("classification")))
    ctx = _Context(
        timeframe=timeframe,
        step=step,
        config=config,
        window_first=window_times[0],
        window_last=window_times[-1],
        candles=candles,
        excluded=excluded,
        t3_gaps=gaps,
    )

    # R2 (tick reference) and R4 (backend clock, only when the tick is stale in open time)
    r2 = check_coverage("R2", expected_latest_closed(t_tick, step, config), ctx)
    tick_age = (t_read - t_tick).total_seconds()
    open_seconds = scheduled_open_seconds(t_tick, t_read, config) if tick_age > 0 else 0.0
    trigger = {
        "tick_age_seconds": round(tick_age, 3),
        "scheduled_open_seconds": round(open_seconds, 3),
        "limit_seconds": limits.clock_max_tick_age_seconds,
    }
    context = "current"
    if (
        tick_age > limits.clock_max_tick_age_seconds
        and open_seconds > limits.clock_max_tick_age_seconds
    ):
        r4 = check_coverage(
            "R4", expected_latest_closed(t_read - COMPLETION_GRACE, step, config), ctx
        )
    else:
        r4 = not_triggered()
        if tick_age > limits.clock_max_tick_age_seconds:
            # A configured closure explains the silence before the daily rollover does (the
            # Friday close coincides with that day's rollover window).
            context = (
                "current_market_closed"
                if closures_overlapping(t_tick, t_read, config)
                or not rollover_spans(t_tick, t_read, config)
                else "current_rollover"
            )
    stale = _reason(
        _TICK_STALE,
        PROVISIONAL,
        f"the snapshot tick is {round(tick_age, 3)} s old while the market was scheduled open "
        f"for {round(open_seconds, 3)} s of that time",
        **trigger,
    )
    status, aggregated, row = aggregate(r2, r4, stale)
    reasons.extend(aggregated)

    def rule_summary(result: RuleResult) -> dict[str, Any]:
        return {
            "status": result.status,
            "expected_latest_closed_t": _iso(result.expected),
            **({"not_applicable": result.na_reason} if result.na_reason else {}),
            **result.summary,
        }

    return finish(
        reasons,
        context,
        rules={
            "R2": rule_summary(r2),
            "R4": {**rule_summary(r4), "trigger": trigger},
        },
        aggregate={"status": status, "row": row},
    )
