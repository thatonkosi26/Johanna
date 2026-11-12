"""Coordinated market-data snapshot (T2): one symbol/timeframe, its components and their ages.

A snapshot is assembled from a single ``LiveMarketService.bars()`` response. In live
operation that response comes from one bars-worker job that reads, in order, a tick (the
*snapshot tick*) and then the bars, on one MT5 connection. The two reads are sequential, not
simultaneous: each carries its own retrieval time and the snapshot never claims otherwise.

Three times are kept apart:
- **MT5 retrieval time** (``tick.retrieved_at_utc``, ``bars.retrieved_at_utc``): when the
  backend received the data from MT5. A cached response keeps its original retrieval time.
- **Serving time** (``bars.served_at_utc``): when the bars response was served, possibly from
  the backend's short in-memory cache (``served_from_cache``).
- **Evaluation time** (``eligibility.evaluated_at_utc``): when eligibility was judged. The
  assembly evaluates at its own time; an analysis re-evaluates at its calculation time.

The server-clock check is derived from the snapshot's own tick, never from the separate
quotes worker, which is not a dependency of snapshot eligibility.

Eligibility is per snapshot, not per candle window: per-candle closure and quality from T1 are
carried through unchanged; session-aware continuity (T3) and window eligibility (T4) are not
implemented here. A tick after a candle's close is closure evidence only, not proof that the
feed covered the candle.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

SNAPSHOT_VERSION = 1
LIVE, PROVISIONAL, REFUSED = "live", "provisional", "refused"


@dataclass(frozen=True)
class SnapshotLimits:
    """Provisionally approved age limits (seconds)."""

    tick_vs_bars_seconds: float = 2.0
    clock_vs_bars_seconds: float = 30.0
    # One candle refresh interval (the dashboard requests the selected chart every 5 s).
    max_snapshot_age_seconds: float = 5.0
    # Existing server-clock rule: the snapshot tick, read as UTC, must be within this window
    # of the backend's clock at retrieval (negative = the tick is ahead of the clock).
    clock_min_tick_age_seconds: float = -60.0
    clock_max_tick_age_seconds: float = 120.0


DEFAULT_LIMITS = SnapshotLimits()


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _seconds(later: datetime | None, earlier: datetime | None) -> float | None:
    if later is None or earlier is None:
        return None
    return round((later - earlier).total_seconds(), 3)


def clock_from_tick(
    tick: Mapping[str, Any] | None, limits: SnapshotLimits = DEFAULT_LIMITS
) -> dict:
    """Server-clock check from the snapshot tick alone.

    ``tick_age_seconds`` = tick retrieval time minus the tick's own (UTC-converted) time.
    - ``verified``: within the configured window;
    - ``stale_ticks``: older than the window (market closed or quiet, or a server clock behind
      UTC): not verifiable now;
    - ``mismatch``: the tick is ahead of the backend's clock beyond tolerance;
    - ``unavailable``: no usable snapshot tick.
    """
    tick_time = _parse((tick or {}).get("time_utc"))
    retrieved = _parse((tick or {}).get("retrieved_at_utc"))
    if tick_time is None or retrieved is None:
        return {
            "result": "unavailable",
            "derived_from": "snapshot_tick",
            "tick_age_seconds": None,
            "checked_at_utc": _iso(retrieved),
        }
    age = _seconds(retrieved, tick_time)
    if age < limits.clock_min_tick_age_seconds:
        result = "mismatch"
    elif age > limits.clock_max_tick_age_seconds:
        result = "stale_ticks"
    else:
        result = "verified"
    return {
        "result": result,
        "derived_from": "snapshot_tick",
        "tick_age_seconds": age,
        "checked_at_utc": _iso(retrieved),
    }


def build_snapshot(
    bars_payload: Mapping[str, Any],
    *,
    snapshot_id: str,
    assembled_at: datetime,
    limits: SnapshotLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Assemble a snapshot from a ``LiveMarketService.bars()`` response.

    Pure: the identifier and the assembly time are passed in, so the same inputs always give
    the same snapshot. Bars, per-candle closure and quality are carried through unchanged.
    """
    tick = bars_payload.get("tick")
    snapshot = {
        "snapshot_version": SNAPSHOT_VERSION,
        "snapshot_id": snapshot_id,
        "symbol": bars_payload.get("symbol"),
        "broker_symbol": bars_payload.get("broker_symbol"),
        "timeframe": bars_payload.get("timeframe"),
        "assembled_at_utc": _iso(assembled_at),
        "source": bars_payload.get("source"),
        "time_basis": bars_payload.get("time_basis"),
        "upstream": {
            "status": bars_payload.get("status"),
            "reason": bars_payload.get("reason"),
            "message": bars_payload.get("message"),
        },
        "tick": dict(tick) if isinstance(tick, Mapping) else None,
        "bars": {
            "retrieved_at_utc": bars_payload.get("retrieved_at_utc"),
            "served_at_utc": bars_payload.get("requested_at_utc"),
            "served_from_cache": bool(bars_payload.get("served_from_cache")),
            "count": len(bars_payload.get("bars") or []),
            "items": list(bars_payload.get("bars") or []),
            "rejected": list(bars_payload.get("rejected_bars") or []),
            "quality": bars_payload.get("quality"),
            "latest_tick_utc": bars_payload.get("latest_tick_utc"),
        },
        "clock": clock_from_tick(tick if isinstance(tick, Mapping) else None, limits),
        "symbol_info": {
            "digits": bars_payload.get("digits"),
            "point": bars_payload.get("point"),
            "pip_size": bars_payload.get("pip_size"),
        },
        "limits": asdict(limits),
    }
    evaluation = evaluate_snapshot(snapshot, analysed_at=assembled_at, limits=limits)
    snapshot["ages"] = evaluation["ages"]
    snapshot["eligibility"] = evaluation
    return snapshot


def _reason(code: str, severity: str, detail: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "detail": detail}


def evaluate_snapshot(
    snapshot: Mapping[str, Any],
    *,
    analysed_at: datetime,
    limits: SnapshotLimits = DEFAULT_LIMITS,
) -> dict[str, Any]:
    """Judge a snapshot at ``analysed_at``: ``live``, ``provisional`` or ``refused``.

    Deterministic and pure. Every failed condition is listed as a reason; ``refused`` wins over
    ``provisional``. Snapshot age is measured from the MT5 *retrieval* time of the bars, so a
    response served from the cache is never younger than its underlying observation.
    """
    reasons: list[dict[str, str]] = []
    upstream = snapshot.get("upstream") or {}
    bars = snapshot.get("bars") or {}
    tick = snapshot.get("tick") or {}
    clock = snapshot.get("clock") or {}

    status = upstream.get("status")
    if status == "UNAVAILABLE" or not bars:
        detail = f"bars unavailable ({upstream.get('reason') or 'no response'})"
        code = (
            "clock_mismatch"
            if upstream.get("reason") == "server_clock_mismatch"
            else "bars_unavailable"
        )
        reasons.append(_reason(code, REFUSED, detail))
    elif status == "CACHED":
        reasons.append(
            _reason(
                "mt5_unavailable_cached",
                REFUSED,
                f"bars served from cache because MT5 is unavailable ({upstream.get('reason')})",
            )
        )
    if snapshot.get("source") not in (None, "mt5"):
        reasons.append(_reason("not_live_source", REFUSED, f"source is {snapshot.get('source')}"))
    if snapshot.get("time_basis") not in (None, "utc"):
        reasons.append(
            _reason("legacy_time_basis", REFUSED, f"time basis is {snapshot.get('time_basis')}")
        )
    if status in ("LIVE", "CACHED") and not bars.get("count"):
        reasons.append(_reason("no_bars", REFUSED, "MT5 returned no bars"))

    tick_retrieved = _parse(tick.get("retrieved_at_utc"))
    bars_retrieved = _parse(bars.get("retrieved_at_utc"))
    clock_checked = _parse(clock.get("checked_at_utc"))
    if status == "LIVE":
        if not tick or _parse(tick.get("time_utc")) is None:
            reasons.append(_reason("tick_missing", REFUSED, "no usable snapshot tick"))
        if bars_retrieved is None or tick_retrieved is None:
            reasons.append(
                _reason("timestamps_missing", REFUSED, "a component has no retrieval time")
            )

    tick_vs_bars = _seconds(bars_retrieved, tick_retrieved)
    clock_vs_bars = _seconds(bars_retrieved, clock_checked)
    snapshot_age = _seconds(analysed_at, bars_retrieved)
    served_age = _seconds(analysed_at, _parse(bars.get("served_at_utc")))
    ages = {
        "tick_vs_bars_seconds": tick_vs_bars,
        "clock_vs_bars_seconds": clock_vs_bars,
        "snapshot_age_seconds": snapshot_age,
        "served_age_seconds": served_age,
        "tick_age_at_retrieval_seconds": clock.get("tick_age_seconds"),
    }

    if tick_vs_bars is not None:
        if tick_vs_bars < 0:
            reasons.append(
                _reason(
                    "inconsistent_timestamps", REFUSED, "snapshot tick retrieved after the bars"
                )
            )
        elif tick_vs_bars > limits.tick_vs_bars_seconds:
            reasons.append(
                _reason(
                    "tick_bars_skew",
                    REFUSED,
                    f"tick and bars retrieved {tick_vs_bars} s apart "
                    f"(limit {limits.tick_vs_bars_seconds} s)",
                )
            )
    if clock_vs_bars is not None and abs(clock_vs_bars) > limits.clock_vs_bars_seconds:
        reasons.append(
            _reason(
                "clock_bars_skew",
                REFUSED,
                f"clock check {clock_vs_bars} s from bars retrieval "
                f"(limit {limits.clock_vs_bars_seconds} s)",
            )
        )
    if snapshot_age is not None:
        if snapshot_age < 0:
            reasons.append(
                _reason("inconsistent_timestamps", REFUSED, "bars retrieved after evaluation time")
            )
        elif snapshot_age > limits.max_snapshot_age_seconds:
            reasons.append(
                _reason(
                    "snapshot_too_old",
                    REFUSED,
                    f"bars retrieved from MT5 {snapshot_age} s ago "
                    f"(limit {limits.max_snapshot_age_seconds} s)",
                )
            )

    if status == "LIVE" and tick:
        result = clock.get("result")
        if result == "mismatch":
            reasons.append(_reason("clock_mismatch", REFUSED, "snapshot tick ahead of UTC"))
        elif result == "stale_ticks":
            reasons.append(
                _reason(
                    "clock_stale_ticks",
                    PROVISIONAL,
                    f"snapshot tick {clock.get('tick_age_seconds')} s old: clock not verifiable "
                    "now (market closed or quiet)",
                )
            )
        elif result != "verified" and _parse(tick.get("time_utc")) is not None:
            reasons.append(_reason("clock_unverified", PROVISIONAL, f"clock check: {result}"))

    quality = bars.get("quality") or {}
    if quality.get("invalid_bars"):
        reasons.append(
            _reason(
                "invalid_bars_present",
                PROVISIONAL,
                f"{quality['invalid_bars']} bar(s) failed the bar checks",
            )
        )
    conflicting = [r for r in bars.get("rejected") or [] if not r.get("identical_to_kept")]
    if conflicting:
        reasons.append(
            _reason(
                "conflicting_duplicates",
                PROVISIONAL,
                f"{len(conflicting)} duplicate timestamp(s) with differing values",
            )
        )

    severities = {reason["severity"] for reason in reasons}
    state = REFUSED if REFUSED in severities else PROVISIONAL if PROVISIONAL in severities else LIVE
    return {
        "state": state,
        "reasons": reasons,
        "evaluated_at_utc": _iso(analysed_at),
        "ages": ages,
    }
