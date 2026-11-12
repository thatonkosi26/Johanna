"""Continuity classification (T3): what the missing candles between observed candles mean.

Pure and deterministic. Input: candles of ONE symbol and ONE timeframe (the ``t`` open times
of a ``LiveMarketService`` bars response), a ``SessionConfig``, and optionally the open times of
other symbols (``peers``) for cross-symbol comparison.

For every pair of consecutive candles, the candle slots between them are *missing*. A slot is
*required* when the schedule says the market was open during it (outside closures and the
reopen tolerance). Each gap gets exactly one classification:

- ``configured_closure``: no required slot is missing; the gap is a configured weekly/holiday
  closure, or a reopen within tolerance (``closure_kinds`` says which).
- ``expected_no_tick``: M1/M5 only; a short gap starting inside the configured daily rollover
  window and no longer than its maximum: the expected break.
- ``analytical_exclusion``: the only missing required slots are open times the caller
  deliberately excluded (``excluded_opens``), e.g. a future analysis policy; not a data gap.
- ``market_wide_unverified``: required slots are missing here and in every informative peer.
  Possibly a holiday or broker pause, possibly a shared feed problem: NOT treated as a
  legitimate closure.
- ``symbol_missing``: required slots are missing here while at least one peer has candles in
  them.
- ``unknown``: required slots are missing and no peer can inform the judgement.

Unexplained gaps carry ``possible_causes``. On M1/M5 a quiet market can produce no candle
("no_tick_interval") and that cannot be told apart from a feed interruption, so both are listed
and ``certainty`` is "uncertain".

What this does NOT do: prove continuity. Adjacent candles and positive tick volume do not show
that the feed covered every candle, so the summary never says "continuous"; the best verdict
is ``no_unexplained_gaps_detected``.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from johanna.analysis.sessions import (
    EXNESS_T0_PROVISIONAL,
    Closure,
    SessionConfig,
    closures_overlapping,
    in_rollover_window,
)

TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}
NO_TICK_TIMEFRAMES = frozenset({"M1", "M5"})  # quiet markets can leave such slots empty

CONFIGURED_CLOSURE = "configured_closure"
EXPECTED_NO_TICK = "expected_no_tick"
ANALYTICAL_EXCLUSION = "analytical_exclusion"
MARKET_WIDE_UNVERIFIED = "market_wide_unverified"
SYMBOL_MISSING = "symbol_missing"
UNKNOWN = "unknown"
UNEXPLAINED = frozenset({MARKET_WIDE_UNVERIFIED, SYMBOL_MISSING, UNKNOWN})


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


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _covered(start: datetime, end: datetime, spans: Sequence[tuple[datetime, datetime]]) -> bool:
    """Whether [start, end) is entirely inside the union of ``spans``."""
    cursor = start
    for span_start, span_end in sorted(spans):
        if span_start > cursor:
            break
        cursor = max(cursor, span_end)
        if cursor >= end:
            return True
    return cursor >= end


def _required(slot: datetime, step: timedelta, closures: Sequence[Closure]) -> bool:
    """A slot is required if the market was scheduled open during it, outside the closures
    and the reopen tolerance that follows each closure."""
    spans = [(c.start, c.reopen_deadline) for c in closures]
    return not _covered(slot, slot + step, spans)


def _peer_times(
    peers: Mapping[str, Iterable[Any]] | None,
) -> dict[str, tuple[set, datetime, datetime]]:
    informative = {}
    for name, times in (peers or {}).items():
        parsed = {t for t in (_parse(v) for v in times) if t is not None}
        if parsed:
            informative[name] = (parsed, min(parsed), max(parsed))
    return informative


def analyse_continuity(
    bars: Sequence[Mapping[str, Any]],
    timeframe: str,
    *,
    symbol: str | None = None,
    config: SessionConfig = EXNESS_T0_PROVISIONAL,
    peers: Mapping[str, Iterable[Any]] | None = None,
    excluded_opens: Iterable[Any] = (),
) -> dict[str, Any]:
    """Classify the gaps between consecutive candles of one symbol and timeframe.

    ``bars``: candles with an ISO-8601 UTC ``t`` (open time) and optionally ``v``.
    ``peers``: other symbols' candle open times on the same timeframe (cross-symbol evidence).
    ``excluded_opens``: open times the caller deliberately removed from ``bars``.
    """
    if timeframe not in TIMEFRAME_SECONDS:
        raise ValueError(f"unsupported timeframe {timeframe!r}")
    step = timedelta(seconds=TIMEFRAME_SECONDS[timeframe])
    input_errors: list[dict[str, Any]] = []
    observed: dict[datetime, Mapping[str, Any]] = {}
    previous: datetime | None = None
    for index, bar in enumerate(bars):
        moment = _parse(bar.get("t") if isinstance(bar, Mapping) else None)
        if moment is None:
            raw = bar.get("t") if isinstance(bar, Mapping) else bar
            input_errors.append(
                {"index": index, "error": "missing_or_malformed_timestamp", "value": repr(raw)}
            )
            continue
        if int(moment.timestamp()) % TIMEFRAME_SECONDS[timeframe]:
            input_errors.append(
                {"index": index, "error": "not_aligned_to_timeframe", "value": _iso(moment)}
            )
            continue
        if moment in observed:
            input_errors.append(
                {"index": index, "error": "duplicate_timestamp", "value": _iso(moment)}
            )
            continue
        if previous is not None and moment < previous:
            input_errors.append({"index": index, "error": "out_of_order", "value": _iso(moment)})
        observed[moment] = bar
        previous = moment if previous is None else max(previous, moment)

    times = sorted(observed)
    excluded = {t for t in (_parse(v) for v in excluded_opens) if t is not None}
    informative_peers = _peer_times(peers)
    gaps: list[dict[str, Any]] = []
    gap_before: dict[datetime, dict[str, Any]] = {}
    for earlier, later in zip(times, times[1:], strict=False):
        gap_start = earlier + step
        if later <= gap_start:
            continue
        gap = _classify_gap(gap_start, later, step, timeframe, config, excluded, informative_peers)
        gaps.append(gap)
        gap_before[later] = gap

    candles = []
    for position, moment in enumerate(times):
        bar = observed[moment]
        notes = []
        if position == 0:
            notes.append("first_in_window")  # nothing is known about what preceded it
        if timeframe == "D1" and moment.weekday() == 6:
            notes.append("sunday_partial_day")  # Exness Sunday D1: reopen to 00:00 UTC only
        if bar.get("v") == 0:
            notes.append("zero_tick_volume")
        gap = gap_before.get(moment)
        candles.append(
            {
                "t": _iso(moment),
                "gap_before": gap["classification"] if gap else None,
                "notes": notes,
            }
        )

    by_class: dict[str, int] = {}
    for gap in gaps:
        by_class[gap["classification"]] = by_class.get(gap["classification"], 0) + 1
    confirmed_missing = by_class.get(SYMBOL_MISSING, 0) + by_class.get(MARKET_WIDE_UNVERIFIED, 0)
    if confirmed_missing:
        verdict = "unexplained_gaps"
    elif by_class.get(UNKNOWN) or input_errors:
        verdict = "unknown"
    else:
        verdict = "no_unexplained_gaps_detected"
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "session_config": config.name,
        "bars_considered": len(times),
        "first_t": _iso(times[0]) if times else None,
        "last_t": _iso(times[-1]) if times else None,
        "input_errors": input_errors,
        "gaps": gaps,
        "candles": candles,
        "summary": {
            "verdict": verdict,
            "gaps_by_class": by_class,
            "unexplained_gaps": sum(by_class.get(name, 0) for name in UNEXPLAINED),
            "peers_considered": sorted(informative_peers),
            "note": "Absence of detected gaps is not proof of continuous feed coverage.",
        },
    }


def _classify_gap(
    gap_start: datetime,
    gap_end: datetime,
    step: timedelta,
    timeframe: str,
    config: SessionConfig,
    excluded: set[datetime],
    peers: dict[str, tuple[set, datetime, datetime]],
) -> dict[str, Any]:
    closures = closures_overlapping(gap_start, gap_end, config)
    slots = []
    slot = gap_start
    while slot < gap_end:
        slots.append(slot)
        slot += step
    required = [s for s in slots if _required(s, step, closures)]
    required_missing = [s for s in required if s not in excluded]
    excluded_slots = [s for s in slots if s in excluded]
    kinds = {c.kind for c in closures if c.start < gap_end and c.end > gap_start}
    spanned = [c for c in closures if c.start < gap_end and gap_start < c.reopen_deadline]
    if spanned:
        # Judged against the last scheduled reopen the gap reaches (a Monday holiday extends
        # the weekend closure, so the weekend's own reopen is not the relevant one).
        last = max(spanned, key=lambda c: c.end)
        if gap_end > last.end:
            kinds.add("reopen_delay")  # the first candle came after the scheduled reopen
    closure_kinds = sorted(kinds)
    duration = gap_end - gap_start
    record: dict[str, Any] = {
        "start_utc": _iso(gap_start),
        "end_utc": _iso(gap_end),
        "duration_minutes": round(duration.total_seconds() / 60, 3),
        "missing_slots": len(slots),
        "required_missing_slots": len(required_missing),
        "excluded_slots": len(excluded_slots),
        "closure_kinds": closure_kinds,
        "possible_causes": [],
        "certainty": "configured",
        "peers_with_data": [],
        "peers_without_data": [],
    }
    if not required_missing:
        if excluded_slots and any(s in excluded for s in required):
            record["classification"] = ANALYTICAL_EXCLUSION
            record["certainty"] = "intentional"
        else:
            record["classification"] = CONFIGURED_CLOSURE
        return record
    if (
        timeframe in NO_TICK_TIMEFRAMES
        and in_rollover_window(gap_start, config)
        and duration <= config.rollover_max
        and not closures
    ):
        record["classification"] = EXPECTED_NO_TICK
        record["closure_kinds"] = ["daily_rollover"]
        record["possible_causes"] = ["daily_rollover_break"]
        return record

    causes = (
        ["no_tick_interval", "feed_interruption"]
        if timeframe in NO_TICK_TIMEFRAMES
        else ["feed_interruption", "missing_history"]
    )
    record["possible_causes"] = causes
    record["certainty"] = "uncertain"
    informative = {
        name: data
        for name, data in peers.items()
        if data[1] <= required_missing[0] and data[2] >= required_missing[-1]
    }
    with_data = sorted(
        name
        for name, (times, _, _) in informative.items()
        if any(s in times for s in required_missing)
    )
    record["peers_with_data"] = with_data
    record["peers_without_data"] = sorted(set(informative) - set(with_data))
    if not informative:
        record["classification"] = UNKNOWN
    elif with_data:
        record["classification"] = SYMBOL_MISSING
    else:
        record["classification"] = MARKET_WIDE_UNVERIFIED
    return record
