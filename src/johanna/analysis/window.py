"""Analysis-window validation (T4): is a whole window of candles fit for later analysis?

Pure and deterministic. Combines, without re-implementing them:
- T1 per-candle quality (``valid``/``issues``/``closure`` on every snapshot candle, and the
  duplicates T1 set aside in ``bars.rejected``);
- T2 snapshot integrity (``eligibility``, optionally re-evaluated at the analysis time);
- T3 session-aware continuity (``analyse_continuity``).

A valid snapshot does not make its window valid, and an accepted window is not proof of
continuous feed coverage, of correct prices, or of trading readiness: it only means no rule
below found a reason to doubt the window.

Independent dimensions, each with its own status (``pass`` / ``provisional`` / ``fail``),
reasons and evidence: ``price``, ``timestamps``, ``closure``, ``continuity``,
``completeness``, ``snapshot`` and (T4.1) ``session_candles``. Verdict: any ``fail`` ->
``rejected``; otherwise any ``provisional`` -> ``provisional``; otherwise ``accepted``.
No scores.

The window is the newest ``required_count + warmup_count`` closed candles; the newest candle
is left out only when it is still forming (reported in ``excluded_candles``). A window is
never silently shortened.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from johanna.analysis.continuity import (
    MARKET_WIDE_UNVERIFIED,
    SYMBOL_MISSING,
    TIMEFRAME_SECONDS,
    UNKNOWN,
    analyse_continuity,
)
from johanna.analysis.sessions import EXNESS_T0_PROVISIONAL, SessionConfig, closures_overlapping
from johanna.analysis.snapshot import DEFAULT_LIMITS, SnapshotLimits, evaluate_snapshot

WINDOW_VERSION = 1
ACCEPTED, PROVISIONAL, REJECTED = "accepted", "provisional", "rejected"
PASS, PROVISIONAL_STATUS, FAIL = "pass", "provisional", "fail"

# T1 issue codes, by dimension. Ordering/duplicate flags are timestamp problems, not price
# problems: a well-formed candle is not price-invalid because it arrived out of order.
PRICE_ISSUES = frozenset(
    {
        "non_finite_price",
        "non_positive_price",
        "high_below_open_close_or_low",
        "low_above_open_close_or_high",
        "negative_tick_volume",
    }
)
TIMESTAMP_ISSUES = frozenset({"out_of_order", "duplicate_timestamp"})
OHLC_FIELDS = ("o", "h", "l", "c")
METADATA_FIELDS = ("v", "spread_points")

# D1 timestamp convention for Exness (evidence: docs/evidence/t0-exness-sessions/).
D1_CONVENTION = {
    "candle_open": "00:00 UTC of the calendar day (MT5 server time is UTC)",
    "sunday_candle": (
        "opens 00:00 UTC Sunday; it holds only the hours from the market reopen "
        "(17:05 New York = 21:05 UTC in US summer time, 22:05 UTC in winter) to midnight UTC"
    ),
    "evidence": (
        "T0: every D1 timestamp recorded is 00:00Z (oldest, newest, first and last Sunday "
        "candles of all 7 symbols); Sunday D1 candles contain a median of 3 (2-3) H1 candles; "
        "server tick time matched UTC within seconds."
    ),
    "status": "observed",
    "unresolved": [
        "The preserved T0 summary does not list every D1 timestamp; alignment of all of them "
        "is inferred from the sample and the H1-per-D1 structure.",
        "EURUSD has 628 Sunday D1 candles against 665 Mondays since 2014: about 37 weeks "
        "lack one. Monday holidays explain some (a Monday holiday closes the whole Sunday); "
        "the remaining weeks are not identified by the preserved evidence.",
    ],
}


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _dimension(reasons: list[dict[str, Any]], evidence: dict[str, Any]) -> dict[str, Any]:
    severities = {reason["severity"] for reason in reasons}
    status = FAIL if FAIL in severities else PROVISIONAL_STATUS if severities else PASS
    return {"status": status, "reasons": reasons, "evidence": evidence}


def _reason(code: str, severity: str, detail: str, **extra: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, "detail": detail, **extra}


def session_candle_check(
    open_times: Sequence[datetime], step_seconds: int, config: SessionConfig
) -> dict[str, Any]:
    """T4.1: candles whose whole interval lies inside configured closures.

    Each candle is the half-open interval [open, open + timeframe). It is tested against the
    union of T3's configured closure intervals (``closures_overlapping``; each closure is
    [close, reopen), the reopen tolerance is NOT closed time). A candle entirely inside the
    union is unexpected (fail). A candle partly inside, with some scheduled open time, is a
    boundary overlap: recorded as information only. Only configured closures are known
    closures; nothing else is treated as closed.
    """
    step = timedelta(seconds=step_seconds)
    unexpected, overlaps = [], []
    for start in open_times:
        end = start + step
        closures = [
            c for c in closures_overlapping(start, end, config) if c.start < end and c.end > start
        ]
        if not closures:
            continue
        # Union of the closure intervals clipped to the candle (a Monday holiday joins the
        # weekend closure, for example).
        clipped = sorted((max(c.start, start), min(c.end, end)) for c in closures)
        closed: list[list[datetime]] = []
        for low, high in clipped:
            if closed and low <= closed[-1][1]:
                closed[-1][1] = max(closed[-1][1], high)
            else:
                closed.append([low, high])
        open_parts, cursor = [], start
        for low, high in closed:
            if low > cursor:
                open_parts.append((cursor, low))
            cursor = max(cursor, high)
        if cursor < end:
            open_parts.append((cursor, end))
        record = {
            "t": _iso(start),
            "interval": [_iso(start), _iso(end)],
            "closures": [
                {"kind": c.kind, "start": _iso(c.start), "end": _iso(c.end)} for c in closures
            ],
            "closed_parts": [[_iso(low), _iso(high)] for low, high in closed],
            "open_parts": [[_iso(low), _iso(high)] for low, high in open_parts],
        }
        (overlaps if open_parts else unexpected).append(record)
    return {"unexpected": unexpected, "boundary_overlaps": overlaps}


def validate_window(
    snapshot: Mapping[str, Any],
    *,
    required_count: int,
    warmup_count: int = 0,
    analysed_at: datetime | None = None,
    config: SessionConfig = EXNESS_T0_PROVISIONAL,
    peers: Mapping[str, Iterable[Any]] | None = None,
    excluded_opens: Iterable[Any] = (),
    limits: SnapshotLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Validate the analysis window of a T2 snapshot.

    ``required_count``: candles the analysis needs; ``warmup_count``: extra older candles it
    needs before them (e.g. indicator warm-up). ``analysed_at``: re-evaluate the snapshot at
    this time (otherwise its stored eligibility is used). ``peers`` and ``excluded_opens`` are
    passed to T3 unchanged.
    """
    if (
        isinstance(required_count, bool)
        or not isinstance(required_count, int)
        or required_count < 1
    ):
        raise ValueError("required_count must be a positive integer")
    if isinstance(warmup_count, bool) or not isinstance(warmup_count, int) or warmup_count < 0:
        raise ValueError("warmup_count must be a non-negative integer")
    expected = required_count + warmup_count
    timeframe = snapshot.get("timeframe")
    bars = snapshot.get("bars") if isinstance(snapshot.get("bars"), Mapping) else {}
    items = list(bars.get("items") or [])
    rejected = list(bars.get("rejected") or [])

    # ---- snapshot integrity (T2)
    snapshot_reasons: list[dict[str, Any]] = []
    if analysed_at is not None and isinstance(snapshot.get("eligibility"), Mapping):
        eligibility = evaluate_snapshot(snapshot, analysed_at=analysed_at, limits=limits)
    else:
        eligibility = snapshot.get("eligibility")
    missing_meta = [
        key for key in ("snapshot_id", "timeframe", "eligibility", "bars") if not snapshot.get(key)
    ]
    if timeframe not in TIMEFRAME_SECONDS and "timeframe" not in missing_meta:
        missing_meta.append("timeframe (unsupported)")
    if missing_meta:
        snapshot_reasons.append(
            _reason("snapshot_metadata_missing", FAIL, f"missing: {', '.join(missing_meta)}")
        )
    state = (eligibility or {}).get("state") if isinstance(eligibility, Mapping) else None
    if state == "refused":
        snapshot_reasons.append(
            _reason(
                "snapshot_refused",
                FAIL,
                "the T2 snapshot was refused",
                snapshot_reasons=[r.get("code") for r in eligibility.get("reasons", [])],
            )
        )
    elif state == "provisional":
        snapshot_reasons.append(
            _reason(
                "snapshot_provisional",
                PROVISIONAL_STATUS,
                "the T2 snapshot is provisional",
                snapshot_reasons=[r.get("code") for r in eligibility.get("reasons", [])],
            )
        )
    elif state != "live" and not missing_meta:
        snapshot_reasons.append(
            _reason("snapshot_state_unknown", FAIL, f"snapshot state {state!r}")
        )
    snapshot_dim = _dimension(
        snapshot_reasons,
        {
            "snapshot_id": snapshot.get("snapshot_id"),
            "state": state,
            "evaluated_at_utc": (eligibility or {}).get("evaluated_at_utc")
            if isinstance(eligibility, Mapping)
            else None,
            "re_evaluated": analysed_at is not None,
            "ages": (eligibility or {}).get("ages") if isinstance(eligibility, Mapping) else None,
            "bars_retrieved_at_utc": bars.get("retrieved_at_utc"),
            "served_from_cache": bars.get("served_from_cache"),
        },
    )

    if timeframe not in TIMEFRAME_SECONDS:
        return _result(
            snapshot,
            timeframe,
            expected,
            required_count,
            warmup_count,
            [],
            [],
            {
                "snapshot": snapshot_dim,
            },
            analysed_at,
            eligibility,
        )

    step = TIMEFRAME_SECONDS[timeframe]
    # ---- timestamps: parse every candle (kept and T1-rejected duplicates alike)
    timestamp_reasons: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    groups: dict[datetime, list[tuple[str, int, Mapping[str, Any]]]] = {}
    previous_kept: datetime | None = None
    order_breaks: list[str] = []
    for source, candles in (("items", items), ("rejected", rejected)):
        for index, candle in enumerate(candles):
            raw = candle.get("t") if isinstance(candle, Mapping) else None
            moment = _parse(raw)
            if moment is None:
                kind = "missing" if raw in (None, "") else "malformed_or_timezone_naive"
                malformed.append(
                    {"source": source, "index": index, "value": repr(raw), "error": kind}
                )
                continue
            if int(moment.timestamp()) % step:
                malformed.append(
                    {
                        "source": source,
                        "index": index,
                        "value": raw,
                        "error": "not_aligned_to_timeframe",
                    }
                )
                continue
            groups.setdefault(moment, []).append((source, index, candle))
            if source == "items":
                if previous_kept is not None and moment < previous_kept:  # equal = duplicate
                    order_breaks.append(_iso(moment))
                previous_kept = moment if previous_kept is None else max(previous_kept, moment)
    if malformed:
        timestamp_reasons.append(
            _reason(
                "invalid_timestamps",
                FAIL,
                f"{len(malformed)} candle(s) without a valid, aligned UTC timestamp; their "
                "position in the window cannot be established",
                candles=malformed,
            )
        )

    # canonical candle per open time: the T1-kept candle, else the first seen
    canonical: dict[datetime, Mapping[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    for moment, entries in groups.items():
        entries.sort(key=lambda e: (e[0] != "items", e[1]))
        canonical[moment] = entries[0][2]
        if len(entries) > 1:
            first = entries[0][2]
            ohlc_conflict = any(
                tuple(e[2].get(f) for f in OHLC_FIELDS) != tuple(first.get(f) for f in OHLC_FIELDS)
                for e in entries[1:]
            )
            metadata_differs = any(
                tuple(e[2].get(f) for f in METADATA_FIELDS)
                != tuple(first.get(f) for f in METADATA_FIELDS)
                for e in entries[1:]
            )
            duplicates.append(
                {
                    "t": _iso(moment),
                    "occurrences": len(entries),
                    "ohlc_identical": not ohlc_conflict,
                    "metadata_identical": not metadata_differs,
                    "values": [
                        {f: e[2].get(f) for f in (*OHLC_FIELDS, *METADATA_FIELDS)} for e in entries
                    ],
                    "policy": "window_rejected"
                    if ohlc_conflict
                    else "canonical_retained_metadata_differs"
                    if metadata_differs
                    else "canonical_retained",
                }
            )

    # ---- window selection: closed candles, newest forming candle left out
    times = sorted(canonical)
    excluded_candles = []
    closure_reasons: list[dict[str, Any]] = []
    if times and canonical[times[-1]].get("closure") == "forming":
        excluded_candles.append({"t": _iso(times[-1]), "reason": "forming"})
        times = times[:-1]
    window_times = times[-expected:] if expected <= len(times) else times
    window = [canonical[t] for t in window_times]
    in_window = set(window_times)

    # ordering and duplicates, judged inside the window; outside it reported only
    t1_out_of_order = [
        _iso(t) for t in window_times if "out_of_order" in (canonical[t].get("issues") or [])
    ]
    window_order_breaks = [t for t in order_breaks if _parse(t) in in_window]
    if t1_out_of_order or window_order_breaks:
        timestamp_reasons.append(
            _reason(
                "out_of_order",
                FAIL,
                "candles arrived out of time order; conservative policy: the window is rejected "
                "(their prices are judged separately and are not marked invalid for this)",
                candles=sorted(set(t1_out_of_order) | set(window_order_breaks)),
            )
        )
    window_dups = [d for d in duplicates if _parse(d["t"]) in in_window]
    conflicts = [d for d in window_dups if not d["ohlc_identical"]]
    if conflicts:
        timestamp_reasons.append(
            _reason(
                "conflicting_duplicates",
                FAIL,
                f"{len(conflicts)} open time(s) with differing OHLC values; no candle is chosen",
                duplicates=conflicts,
            )
        )
    meta_only = [d for d in window_dups if d["ohlc_identical"] and not d["metadata_identical"]]
    if meta_only:
        timestamp_reasons.append(
            _reason(
                "duplicate_metadata_differs",
                PROVISIONAL_STATUS,
                "duplicates with identical OHLC but differing tick volume/spread; one canonical "
                "candle retained, the feed inconsistency is reported",
                duplicates=meta_only,
            )
        )
    identical = [d for d in window_dups if d["ohlc_identical"] and d["metadata_identical"]]
    if identical:
        timestamp_reasons.append(
            _reason(
                "identical_duplicates_collapsed",
                PROVISIONAL_STATUS,
                "exact duplicate candles collapsed to one canonical candle",
                duplicates=identical,
            )
        )
    timestamps_dim = _dimension(
        timestamp_reasons,
        {
            "duplicates": duplicates,
            "duplicates_outside_window": [d["t"] for d in duplicates if d not in window_dups],
            "candles_parsed": sum(len(v) for v in groups.values()),
        },
    )

    # ---- price/OHLC validity (T1 issue codes on window candles, duplicate copies included)
    price_reasons: list[dict[str, Any]] = []
    invalid = []
    unknown_quality = []
    for moment in window_times:
        for _, _, candle in groups[moment]:
            issues = candle.get("issues")
            if issues is None:
                if all(isinstance(candle.get(f), int | float) for f in OHLC_FIELDS) and (
                    candle is not canonical[moment]
                ):
                    continue  # a T1-rejected duplicate: values compared above
                unknown_quality.append(_iso(moment))
                continue
            price_codes = sorted(set(issues) & PRICE_ISSUES)
            if price_codes:
                invalid.append({"t": _iso(moment), "issues": price_codes})
    if invalid:
        price_reasons.append(
            _reason(
                "invalid_ohlc",
                FAIL,
                f"{len(invalid)} candle(s) with invalid prices",
                candles=invalid,
            )
        )
    if unknown_quality:
        price_reasons.append(
            _reason(
                "quality_unknown",
                FAIL,
                "candles without T1 quality information",
                candles=sorted(set(unknown_quality)),
            )
        )
    price_dim = _dimension(price_reasons, {"candles_checked": len(window)})

    # ---- closure evidence
    forming_inside = [_iso(t) for t in window_times if canonical[t].get("closure") == "forming"]
    clock_only = [_iso(t) for t in window_times if canonical[t].get("closure") == "clock_only"]
    unknown_closure = [
        _iso(t)
        for t in window_times
        if canonical[t].get("closure") not in ("tick_observed", "clock_only", "forming")
    ]
    if forming_inside:
        closure_reasons.append(
            _reason(
                "forming_candle_inside_window",
                FAIL,
                "a non-newest candle is still forming",
                candles=forming_inside,
            )
        )
    if clock_only:
        closure_reasons.append(
            _reason(
                "clock_only_closure",
                PROVISIONAL_STATUS,
                f"{len(clock_only)} candle(s) closed by clock only (no later tick seen)",
                candles=clock_only,
            )
        )
    if unknown_closure:
        closure_reasons.append(
            _reason(
                "closure_unknown",
                PROVISIONAL_STATUS,
                "closure evidence unknown",
                candles=unknown_closure,
            )
        )
    closure_dim = _dimension(
        closure_reasons,
        {
            "excluded_candles": excluded_candles,
            "tick_observed": len(window)
            - len(clock_only)
            - len(unknown_closure)
            - len(forming_inside),
        },
    )

    # ---- completeness
    completeness_reasons: list[dict[str, Any]] = []
    available = len(times)
    if available == 0:
        completeness_reasons.append(_reason("empty_window", FAIL, "no closed candles"))
    elif available < required_count:
        completeness_reasons.append(
            _reason(
                "insufficient_history",
                FAIL,
                f"{available} closed candle(s) available, {required_count} required",
            )
        )
    elif available < expected:
        completeness_reasons.append(
            _reason(
                "insufficient_warmup",
                FAIL,
                f"{available} closed candle(s) available, {required_count} + {warmup_count} "
                "warm-up required",
            )
        )
    completeness_dim = _dimension(
        completeness_reasons,
        {
            "expected": expected,
            "required": required_count,
            "warmup": warmup_count,
            "available_closed": available,
            "in_window": len(window),
        },
    )

    # ---- continuity (T3), over the window candles only
    continuity = analyse_continuity(
        [{"t": _iso(t), "v": canonical[t].get("v")} for t in window_times],
        timeframe,
        symbol=snapshot.get("symbol"),
        config=config,
        peers=peers,
        excluded_opens=excluded_opens,
    )
    by_class = continuity["summary"]["gaps_by_class"]
    continuity_reasons: list[dict[str, Any]] = []
    for name in (SYMBOL_MISSING, UNKNOWN):
        if by_class.get(name):
            continuity_reasons.append(
                _reason(
                    f"gap_{name}",
                    FAIL,
                    f"{by_class[name]} {name} gap(s) inside the window",
                    gaps=[g for g in continuity["gaps"] if g["classification"] == name],
                )
            )
    if by_class.get(MARKET_WIDE_UNVERIFIED):
        continuity_reasons.append(
            _reason(
                "gap_market_wide_unverified",
                PROVISIONAL_STATUS,
                "gap(s) missing on every compared symbol: possibly an unconfigured closure, not "
                "verified",
                gaps=[
                    g for g in continuity["gaps"] if g["classification"] == MARKET_WIDE_UNVERIFIED
                ],
            )
        )
    continuity_dim = _dimension(
        continuity_reasons,
        {
            "t3_verdict": continuity["summary"]["verdict"],
            "gaps_by_class": by_class,
            "gaps": continuity["gaps"],
            "session_config": continuity["session_config"],
            "note": continuity["summary"]["note"],
        },
    )

    # ---- session candles (T4.1): candles entirely inside configured closures
    session_check = session_candle_check(window_times, step, config)
    session_reasons = []
    if session_check["unexpected"]:
        session_reasons.append(
            _reason(
                "unexpected_candle_in_closure",
                FAIL,
                f"{len(session_check['unexpected'])} candle(s) lie entirely inside a configured "
                "market closure",
                timeframe=timeframe,
                candles=session_check["unexpected"],
            )
        )
    session_dim = _dimension(
        session_reasons,
        {
            "timeframe": timeframe,
            "session_config": config.name,
            "candles_checked": len(window_times),
            # Information only: a candle containing some scheduled open time is valid.
            "boundary_overlaps": session_check["boundary_overlaps"],
        },
    )

    dimensions = {
        "price": price_dim,
        "timestamps": timestamps_dim,
        "closure": closure_dim,
        "continuity": continuity_dim,
        "completeness": completeness_dim,
        "snapshot": snapshot_dim,
        "session_candles": session_dim,
    }
    return _result(
        snapshot,
        timeframe,
        expected,
        required_count,
        warmup_count,
        window_times,
        excluded_candles,
        dimensions,
        analysed_at,
        eligibility,
    )


def _result(
    snapshot: Mapping[str, Any],
    timeframe: Any,
    expected: int,
    required_count: int,
    warmup_count: int,
    window_times: Sequence[datetime],
    excluded_candles: list[dict[str, Any]],
    dimensions: dict[str, dict[str, Any]],
    analysed_at: datetime | None,
    eligibility: Any,
) -> dict[str, Any]:
    reasons = [
        {"dimension": name, **reason}
        for name, dim in dimensions.items()
        for reason in dim["reasons"]
    ]
    statuses = {dim["status"] for dim in dimensions.values()}
    if FAIL in statuses or len(dimensions) < 6:
        state = REJECTED
    elif PROVISIONAL_STATUS in statuses:
        state = PROVISIONAL
    else:
        state = ACCEPTED
    result = {
        "window_version": WINDOW_VERSION,
        "snapshot_id": snapshot.get("snapshot_id"),
        "symbol": snapshot.get("symbol"),
        "timeframe": timeframe,
        "evaluated_at_utc": _iso(analysed_at)
        if analysed_at is not None
        else (eligibility or {}).get("evaluated_at_utc")
        if isinstance(eligibility, Mapping)
        else None,
        "requested": {"required": required_count, "warmup": warmup_count, "expected": expected},
        "window": {
            "count": len(window_times),
            "first_t": _iso(window_times[0]) if window_times else None,
            "last_t": _iso(window_times[-1]) if window_times else None,
            "candles": [_iso(t) for t in window_times],
        },
        "excluded_candles": excluded_candles,
        "dimensions": dimensions,
        "verdict": {"state": state, "reasons": reasons},
        "note": (
            "Accepted means no validation rule found a reason to doubt this window. It is not "
            "proof of continuous feed coverage, of price correctness, or of trading readiness."
        ),
    }
    if timeframe == "D1":
        result["d1_convention"] = D1_CONVENTION
    return result
