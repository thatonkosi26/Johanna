"""Phase 4 orchestration: gather the inputs, run the pure risk engine (read-only).

Per request: one T6 evaluation (``MarketAnalysisService``, single-flight per key), the Phase 3
account and instrument snapshot (shared 5 s cache), one exposure read (positions, pending
orders, today's deals) and one MT5 calculator call for the sized proposal. All MT5 work runs on
the existing quotes worker; nothing here can place, modify or close an order. Requests for the
same key share one evaluation.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from johanna.live_market import MarketUnavailable
from johanna.market_analysis import ANALYSIS_TIMEFRAMES
from johanna.risk_engine import (
    COST_TERMS,
    RISK_POLICY,
    CostTerms,
    RiskInputs,
    RiskPolicy,
    finalise,
    plan_trade,
    trading_day_start,
)

LOGGER = logging.getLogger("johanna.risk_service")


class RiskService:
    name = "risk_proposals"

    def __init__(
        self,
        analysis: Any,
        account: Any,
        live_market: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        policy: RiskPolicy = RISK_POLICY,
        costs: CostTerms = COST_TERMS,
    ) -> None:
        self.analysis = analysis
        self.account = account
        self.live_market = live_market
        self._clock = clock or (lambda: datetime.now(UTC))
        self.policy = policy
        # Verified transaction-cost terms (F1). None exist yet: net reward:risk stays unverified.
        self.costs = costs
        self._locks: dict[tuple[str, str], threading.Lock] = {}
        self._guard = threading.Lock()
        self.evaluations = 0  # diagnostics and tests

    def supported(self, symbol: str, timeframe: str) -> str | None:
        """Why a request cannot be evaluated (``None`` when it can). Decided without MT5."""
        if timeframe not in ANALYSIS_TIMEFRAMES:
            return f"proposals use the T6 analysis timeframes {', '.join(ANALYSIS_TIMEFRAMES)}"
        if self.account.supported_pair(symbol) is None:
            return (
                f"supported instruments: {', '.join(self.account.pairs)} (or their broker symbols)"
            )
        return None

    def proposal(self, symbol: str, timeframe: str) -> dict[str, Any]:
        """``symbol`` may be the pair (``EURUSD``) or the broker symbol (``EURUSDc``); it is
        mapped to the pair with Phase 3's ``supported_pair`` and the engine checks that a
        broker-form request names this broker's actual symbol (no substitution)."""
        pair = self.account.supported_pair(symbol)
        if pair is None:
            raise ValueError(f"unsupported symbol {symbol!r}")
        with self._guard:
            lock = self._locks.setdefault((pair, timeframe), threading.Lock())
        with lock:  # one evaluation per key at a time
            self.evaluations += 1
            return self._evaluate(pair, timeframe, requested=symbol.strip())

    def assess(self, symbol: str, timeframe: str, analysis: dict[str, Any]) -> dict[str, Any]:
        """P6.1 shadow evaluation: the unchanged Phase 4 assessment of an analysis envelope the
        caller already obtained (no second analysis, no request-time decision capture)."""
        pair = self.account.supported_pair(symbol)
        if pair is None:
            raise ValueError(f"unsupported symbol {symbol!r}")
        with self._guard:
            lock = self._locks.setdefault((pair, timeframe), threading.Lock())
        with lock:
            self.evaluations += 1
            return self._evaluate(pair, timeframe, requested=symbol.strip(), analysis=analysis)

    def _evaluate(
        self,
        symbol: str,
        timeframe: str,
        *,
        requested: str,
        analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        analysis_error = None
        if analysis is None:
            try:
                analysis = self.analysis.analyse(symbol, timeframe)
            except Exception as exc:  # MarketAnalysisError or unexpected: fail closed
                analysis_error = f"{type(exc).__name__}: {exc}"
        account = self.account.summary()
        instrument = self.account.instrument(symbol)
        day_start = trading_day_start(self._clock(), self.live_market.server_clock)
        try:
            payload = self.live_market.exposure_read(day_start)
            exposure = {"status": "available", **payload}
        except MarketUnavailable as exc:
            exposure = {"status": "unavailable", "reason": exc.code}
        inputs = RiskInputs(
            symbol=symbol,
            timeframe=timeframe,
            analysis=analysis,
            analysis_error=analysis_error,
            account=account,
            instrument=instrument,
            exposure=exposure,
            evaluated_at=self._clock(),
            day_start_utc=day_start,
            policy=self.policy,
            requested_symbol=requested,
            costs=self.costs,
        )
        plan = plan_trade(inputs)
        calculations, calc_error = None, None
        if plan["calc_requests"]:
            try:
                calculations = self.live_market.risk_calculations(plan["calc_requests"])["value"]
            except MarketUnavailable as exc:
                calc_error = exc.code
        result = finalise(plan, calculations, calc_error)
        LOGGER.info(
            "risk proposal %s %s: %s (%d rejection(s), %d unverified)",
            symbol,
            timeframe,
            result["status"],
            len(result["rejection_reasons"]),
            len(result["unverified"]),
        )
        return result
