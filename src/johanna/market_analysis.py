"""T6 market analysis for the backend API (integration stage I1).

``MarketAnalysisService.analyse(symbol, timeframe)`` runs one coherent, read-only operation:

1. a **new** coordinated T2 snapshot from ``LiveMarketService.snapshot`` with
   ``required_candles() + 1`` bars (one extra for a possibly forming newest candle);
2. ``analysed_at`` = the injected backend clock, read **after** the snapshot returned (so T2's
   age ``analysed_at - bars.retrieved_at_utc`` covers every delay since the MT5 read);
3. the accepted constructed pipeline via ``evaluate_market_snapshot_with_validation`` (T4 -> T5
   -> T6 on one deep copy; T2's existing freshness rule decides), which also returns the exact
   T4 validation object that evaluation used;
4. the T6 result returned **unchanged** inside a transport envelope, plus ``window_validation``:
   a verbatim projection of that same T4 object (T4 is never run a second time).

Concurrency: single-flight per ``(symbol, timeframe)``. Requests arriving while that key is being
computed join the running computation and receive its result (same snapshot, same
``evaluated_at_utc``). There is **no completed-result cache**: once a computation finishes its
record is removed and the next request computes again. Failures are delivered to every waiter
and never cached.

Read-only: this module only calls ``LiveMarketService.snapshot`` (bar and tick reads through the
existing workers, timeouts and backoff) and pure analysis functions. It never touches MT5,
workers, accounts or orders directly, and never retries.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from johanna.analysis.decision import (
    DEFAULT_DECISION_PARAMETERS,
    DecisionParameters,
    evaluate_market_snapshot_with_validation,
    required_candles,
)
from johanna.analysis.engine import SUPPORTED_TIMEFRAMES
from johanna.analysis.snapshot import evaluate_snapshot

LOGGER = logging.getLogger("johanna.market_analysis")
ANALYSIS_TIMEFRAMES = SUPPORTED_TIMEFRAMES  # H1, H4, D1 (the accepted T6 scope)


class MarketAnalysisError(RuntimeError):
    """A computation failed; raised to the leader and every joined waiter alike."""


@dataclass
class _InFlight:
    done: threading.Event = field(default_factory=threading.Event)
    computation: dict[str, Any] | None = None
    error: Exception | None = None
    joiners: int = 0


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class MarketAnalysisService:
    def __init__(
        self,
        live_market: Any,
        *,
        clock: Callable[[], datetime],
        parameters: DecisionParameters = DEFAULT_DECISION_PARAMETERS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._live = live_market
        self._clock = clock
        self._parameters = parameters
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._in_flight: dict[tuple[str, str], _InFlight] = {}

    @property
    def requested_count(self) -> int:
        return required_candles(self._parameters) + 1

    def waiting(self, symbol: str, timeframe: str) -> int | None:
        """Joined waiters of the running computation for a key, or None if none is running."""
        with self._lock:
            record = self._in_flight.get((symbol, timeframe))
            return None if record is None else record.joiners

    def analyse(self, symbol: str, timeframe: str) -> dict[str, Any]:
        if timeframe not in ANALYSIS_TIMEFRAMES:
            raise ValueError(f"analysis supports {', '.join(ANALYSIS_TIMEFRAMES)}")
        key = (symbol, timeframe)
        with self._lock:
            record = self._in_flight.get(key)
            leader = record is None
            if leader:
                record = _InFlight()
                self._in_flight[key] = record
            else:
                record.joiners += 1
        if leader:
            try:
                record.computation = self._compute(symbol, timeframe)
            except Exception as exc:  # delivered to every waiter, never cached
                record.error = exc
            finally:
                with self._lock:
                    if self._in_flight.get(key) is record:
                        del self._in_flight[key]
                record.done.set()
        else:
            record.done.wait()
        if record.error is not None:
            raise MarketAnalysisError(
                f"market analysis for {symbol} {timeframe} failed"
            ) from record.error
        if record.computation is None:  # leader interrupted by a non-Exception
            raise MarketAnalysisError(f"market analysis for {symbol} {timeframe} did not complete")
        return self._envelope(record.computation, joined=not leader)

    # ------------------------------------------------------------------ internals
    def _compute(self, symbol: str, timeframe: str) -> dict[str, Any]:
        count = self.requested_count
        started = self._monotonic()
        snapshot = self._live.snapshot(symbol, timeframe, count)
        fetched = self._monotonic()
        analysed_at = self._clock()  # read after the snapshot returned
        evaluated = evaluate_market_snapshot_with_validation(
            snapshot, analysed_at=analysed_at, parameters=self._parameters
        )
        finished = self._monotonic()
        result = evaluated["result"]
        snapshot_ms = round((fetched - started) * 1000, 3)
        evaluation_ms = round((finished - fetched) * 1000, 3)
        timing = {
            "snapshot": snapshot_ms,
            "evaluation": evaluation_ms,
            "total": round(snapshot_ms + evaluation_ms, 3),
        }
        decision = (result or {}).get("decision") or {}
        LOGGER.info(
            "market analysis %s %s: %s %s (snapshot %.0f ms, evaluation %.0f ms, total %.0f ms)",
            symbol,
            timeframe,
            decision.get("outcome"),
            decision.get("rule"),
            timing["snapshot"],
            timing["evaluation"],
            timing["total"],
        )
        return {
            "symbol": symbol,
            "timeframe": timeframe,
            "requested_count": count,
            "timing_ms": timing,
            "data_age_at_evaluation_ms": _data_age_ms(snapshot, analysed_at),
            "snapshot": _snapshot_summary(snapshot),
            "window_validation": _window_validation(evaluated["validation"], result),
            "result": result,
        }

    def _envelope(self, computation: dict[str, Any], *, joined: bool) -> dict[str, Any]:
        result = computation["result"]
        return {
            "symbol": computation["symbol"],
            "timeframe": computation["timeframe"],
            "served_at_utc": _iso(self._clock()),  # per response; never an evaluation time
            "service": {
                "joined_in_flight": joined,
                "requested_count": computation["requested_count"],
                "timing_ms": dict(computation["timing_ms"]),
                "data_age_at_evaluation_ms": computation["data_age_at_evaluation_ms"],
            },
            "snapshot": dict(computation["snapshot"]),
            # Joiners get independent deep copies, so no response shares mutable state.
            "window_validation": copy.deepcopy(computation["window_validation"])
            if joined
            else computation["window_validation"],
            "result": copy.deepcopy(result) if joined else result,
        }


def _data_age_ms(snapshot: Any, analysed_at: datetime) -> float | None:
    """T2's own snapshot age at ``analysed_at`` (same ms rounding), in ms. Diagnostic only."""
    if not isinstance(snapshot, dict):
        return None
    try:
        seconds = evaluate_snapshot(snapshot, analysed_at=analysed_at)["ages"][
            "snapshot_age_seconds"
        ]
    except Exception:  # malformed snapshot: T6 itself reports why; the age is just unknown
        return None
    return None if seconds is None else round(seconds * 1000, 3)


_COPIED_FIELDS = ("window_version", "snapshot_id", "evaluated_at_utc", "requested")


def _window_validation(validation: Any, result: Any) -> dict[str, Any]:
    """Verbatim projection of the T4 validation object the evaluation used.

    Field names and values are copied (deep copies) from ``validation``; only ``window.candles``
    and each dimension's ``evidence`` are omitted. ``reason_codes`` is
    ``[r["code"] for r in validation["verdict"]["reasons"]]`` in T4's order. When T4 did not run
    (``validation is None``), the reason is the T6 R1 reason itself.
    """
    if validation is None:
        reasons = ((result or {}).get("decision") or {}).get("reasons") or []
        return {
            "available": False,
            "reason": f"T4 did not run: {reasons[0]}" if reasons else "T4 did not run",
        }
    out: dict[str, Any] = {"available": True}
    for name in _COPIED_FIELDS:
        out[name] = copy.deepcopy(validation.get(name))
    window = validation.get("window") or {}
    out["window"] = {key: window.get(key) for key in ("count", "first_t", "last_t")}
    out["excluded_candles"] = copy.deepcopy(validation.get("excluded_candles"))
    verdict = validation.get("verdict") or {}
    out["verdict"] = copy.deepcopy(verdict)
    out["reason_codes"] = [reason.get("code") for reason in verdict.get("reasons") or []]
    out["dimensions"] = {
        name: {
            "status": dimension.get("status"),
            "reasons": copy.deepcopy(dimension.get("reasons")),
        }
        for name, dimension in (validation.get("dimensions") or {}).items()
    }
    if "recency" in validation:  # E1: additive recency summary (pass states included)
        out["recency"] = copy.deepcopy(validation["recency"])
    if "d1_convention" in validation:
        out["d1_convention"] = copy.deepcopy(validation["d1_convention"])
    return out


def _snapshot_summary(snapshot: Any) -> dict[str, Any]:
    s = snapshot if isinstance(snapshot, dict) else {}
    upstream = s.get("upstream") if isinstance(s.get("upstream"), dict) else {}
    bars = s.get("bars") if isinstance(s.get("bars"), dict) else {}
    eligibility = s.get("eligibility") if isinstance(s.get("eligibility"), dict) else {}
    return {
        "snapshot_id": s.get("snapshot_id"),
        "upstream_status": upstream.get("status"),
        "upstream_reason": upstream.get("reason"),
        "source": s.get("source"),
        "bars_retrieved_at_utc": bars.get("retrieved_at_utc"),
        "bars_served_from_cache": bars.get("served_from_cache"),
        "eligibility_at_assembly": eligibility.get("state"),
    }
