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
    capture_analysis,
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

    def analyse_background(self, symbol: str, timeframe: str) -> tuple[dict[str, Any], dict]:
        """P6.1 shadow evaluation: the same computation as ``analyse`` on a new snapshot, but
        outside the single-flight registry -- it neither joins a dashboard computation nor lets
        one join it, so a deferred background read can never fail a dashboard request. Returns
        the envelope and the T2 snapshot it analysed (for closure evidence). Errors propagate
        unchanged (``MarketUnavailable`` included); nothing is cached."""
        if timeframe not in ANALYSIS_TIMEFRAMES:
            raise ValueError(f"analysis supports {', '.join(ANALYSIS_TIMEFRAMES)}")
        observed: dict[str, Any] = {}
        computation = self._compute(symbol, timeframe, observed)
        return self._envelope(computation, joined=False), observed["snapshot"]

    # ------------------------------------------------------------------ internals
    def _compute(
        self, symbol: str, timeframe: str, observed: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        count = self.requested_count
        started = self._monotonic()
        snapshot = self._live.snapshot(symbol, timeframe, count)
        if observed is not None:
            observed["snapshot"] = snapshot
        fetched = self._monotonic()
        analysed_at = self._clock()  # read after the snapshot returned
        # The capture hands over the T5 object of this same (single) evaluation for the overlay.
        with capture_analysis() as captured:
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
            "overlay": overlay_payload(result, evaluated["validation"], captured),
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
            # Additive (overlay V1 phase 2): visualisation data from the same evaluation.
            "overlay": copy.deepcopy(computation["overlay"]) if joined else computation["overlay"],
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


OVERLAY_VERSION = 1
OVERLAY_LIMIT = 300  # analysed candles, matching the dashboard chart's 300-bar request


def overlay_payload(
    result: Any, validation: Any, captured: dict[str, Any] | None
) -> dict[str, Any]:
    """Visualisation data (EMA series, confirmed swings) from the one evaluation's own T5 object.

    Nothing is calculated here: values, timestamps and swing records are copied from T5
    (``analyse_window(include_series=True)``) and T4. Coverage is deterministic: the most recent
    ``OVERLAY_LIMIT`` candles of the T4 analysis window (all of it when shorter), ending at the
    last analysed candle; ``coverage.truncated`` says whether older analysed candles exist.
    ``status``: ``available``; ``partial`` (some component missing or not warmed up, listed in
    ``reasons``); ``unavailable`` (no usable analysis, e.g. a rejected window).
    """
    result = result if isinstance(result, dict) else {}
    inputs = result.get("inputs") or {}
    window = inputs.get("window") or {}
    provenance = inputs.get("provenance") or {}
    base: dict[str, Any] = {
        "overlay_version": OVERLAY_VERSION,
        "snapshot_id": inputs.get("snapshot_id"),
        "symbol": result.get("symbol"),
        "broker_symbol": result.get("broker_symbol"),
        "timeframe": result.get("timeframe"),
        "window": {key: window.get(key) for key in ("count", "first_t", "last_t")},
        "last_analysed_t": window.get("last_t"),
        "as_of_utc": result.get("as_of_utc"),
        "validation_verdict": inputs.get("validation_verdict"),
        "analysis_status": inputs.get("analysis_status"),
        "provenance": {"mode": provenance.get("mode"), "passed": provenance.get("passed")},
    }

    def unavailable(reason: str) -> dict[str, Any]:
        return {
            **base,
            "status": "unavailable",
            "reasons": [reason],
            "coverage": None,
            "ema": {},
            "swings": None,
        }

    captured = captured or {}
    analysis = captured.get("analysis")
    if captured.get("runs") != 1 or not isinstance(analysis, dict):
        return unavailable("the analysis object of this evaluation was not available")
    if captured.get("validation") is not validation or not isinstance(validation, dict):
        return unavailable("the captured analysis does not belong to this validation")
    if analysis.get("status") not in ("available", "partial", "provisional"):
        return unavailable(f"T5 analysis status {analysis.get('status')!r}: nothing calculated")
    candles = list((validation.get("window") or {}).get("candles") or [])
    if not candles or candles[0] != window.get("first_t") or candles[-1] != window.get("last_t"):
        return unavailable("the analysis window does not match its validation")
    covered = candles[-OVERLAY_LIMIT:]
    in_coverage = set(covered)
    reasons: list[str] = []
    ema: dict[str, Any] = {}
    for summary in (analysis.get("indicators") or {}).get("ema") or []:
        name = summary.get("name")
        series = summary.get("series")
        if not isinstance(series, list):
            reasons.append(f"{name}: series not available")
            continue
        times = [point[0] for point in series]
        if times != candles:  # T5 must have analysed exactly the T4 window, in order
            return unavailable(f"{name}: T5 series timestamps differ from the analysis window")
        convergence = summary.get("convergence") or {}
        required = convergence.get("observations_required")
        converged_from_t = (
            times[required - 1]
            if isinstance(required, int) and 0 < required <= len(times)
            else None
        )
        status = summary.get("status")
        if status != "available":
            reasons.append(f"{name}: {status}")
        ema[name] = {
            "period": summary.get("period"),
            "status": status,
            "trusted": summary.get("trusted"),
            "converged_from_t": converged_from_t,
            "points": [[t, value] for t, value in series if t in in_coverage],
        }
    swings_t5 = (analysis.get("structure") or {}).get("swings") or {}
    if swings_t5.get("status") != "available":
        reasons.append(f"swings: {swings_t5.get('status')}")
    confirmed = [
        {
            # Derived identifier: swing type + pivot time (unique per T5's one-pivot rule).
            "id": f"{record.get('type')}@{record.get('pivot_t')}",
            "type": record.get("type"),
            "price": record.get("price"),
            "pivot_t": record.get("pivot_t"),
            "confirmed_by_t": record.get("confirmed_by_t"),
            "confirmed_at_utc": record.get("confirmed_at_utc"),
            "confirmation_status": record.get("confirmation_status"),
        }
        for record in swings_t5.get("confirmed") or []
        if record.get("pivot_t") in in_coverage
    ]
    return {
        **base,
        "status": "partial" if reasons else "available",
        "reasons": reasons,
        "coverage": {
            "first_t": covered[0],
            "last_t": covered[-1],
            "count": len(covered),
            "limit": OVERLAY_LIMIT,
            "truncated": len(candles) > len(covered),
            "selection": "the most recent candles of the analysis window, ending at the last "
            "analysed candle",
        },
        "ema": ema,
        "swings": {
            "status": swings_t5.get("status"),
            "trusted": swings_t5.get("trusted"),
            "k": swings_t5.get("k"),
            "confirmed": confirmed,
            "pending_excluded": len(swings_t5.get("pending") or []),
        },
    }


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
