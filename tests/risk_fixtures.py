"""Phase 4 test fixtures: real T6 envelopes reshaped per test, FakeMT5 service stacks.

The envelopes come from the real T2 -> T4 -> T5 -> T6 pipeline (``trend_bars``). Tests then set
the evaluation time to "now" and, where a test needs specific levels, the TC6 level, the swing
pivot or ATR -- clearly synthetic data, never presented as market observations.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

from fake_mt5 import FakeMT5
from t6_fixtures import trend_bars
from test_market_analysis import RETRIEVED, FakeLive, FixedClock, service
from test_window_validation import snapshot

from johanna.account_intelligence import AccountIntelligenceService
from johanna.adapters.mt5 import MetaTrader5Adapter
from johanna.live_market import LiveMarketService
from johanna.risk_engine import COST_TERMS, CostTerms
from johanna.risk_service import RiskService

# SYNTHETIC, test-only cost terms that let tests reach the approval branch. Production has no
# verified commission rate or holding period (COST_TERMS), so live proposals stay UNVERIFIED.
VERIFIED_COSTS = CostTerms(
    commission_round_trip_per_lot={"EURUSDc": 0.0, "GBPUSDc": 0.0, "USDJPYc": 0.0},
    commission_source="test fixture (synthetic, not a broker term)",
    holding_rollovers=0,  # intraday: swap 0, so sizing numbers do not depend on the weekday
    holding_source="test fixture (synthetic)",
    slippage_bound_points_per_side={"EURUSDc": 0.0, "GBPUSDc": 0.0, "USDJPYc": 0.0},
    slippage_source="test fixture (synthetic, not a real bound)",
)
PRODUCTION_COSTS = COST_TERMS


def _real(sign: int) -> dict:
    live = FakeLive(lambda s, t, c, n: snapshot(trend_bars(sign=sign)))
    return service(live, FixedClock(RETRIEVED + timedelta(seconds=0.8))).analyse("EURUSD", "H1")


REAL_BUY = _real(1)  # R7 bullish; L_last 1.23455, ATR ~0.000887, no qualifying resistance
REAL_SELL = _real(-1)  # R7 bearish; H_last 0.96545
REAL_R6 = None  # built lazily (a not-qualified market)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def envelope(
    direction: str = "bullish",
    *,
    level: tuple[float, float] | None = None,
    pivot: float | None = None,
    atr: float | None = None,
    evaluated_at: datetime | None = None,
    symbol: str = "EURUSD",
) -> dict:
    """A real R7 envelope, re-timed to ``evaluated_at`` (default now) and reshaped."""
    env = copy.deepcopy(REAL_BUY if direction == "bullish" else REAL_SELL)
    env["symbol"] = symbol
    r = env["result"]
    r["symbol"] = symbol
    now = evaluated_at or datetime.now(UTC)
    r["evaluated_at_utc"] = iso(now)
    r["as_of_utc"] = iso(now.replace(minute=0, second=0, microsecond=0))
    if level is not None:
        tc6 = next(c for c in r["opportunity"]["checklist"] if c["id"] == "TC6_clearance")
        tc6["selected_level"] = {
            "low": level[0],
            "high": level[1],
            "level": (level[0] + level[1]) / 2,
            "strength": 3,
            "latest_confirmed_at_utc": None,
        }
    if pivot is not None:
        role = "L_last" if direction == "bullish" else "H_last"
        for p in r["features"]["swings"]["pivots"]:
            if p["role"] == role:
                p["price"] = pivot
    if atr is not None:
        r["features"]["latest"]["ATR14"]["value"] = atr
    return env


class StubAnalysis:
    """Stands in for MarketAnalysisService: returns an envelope (or raises)."""

    def __init__(self, env=None, error: Exception | None = None) -> None:
        self.env, self.error, self.calls = env, error, []

    def analyse(self, symbol: str, timeframe: str) -> dict:
        self.calls.append((symbol, timeframe))
        if self.error is not None:
            raise self.error
        env = copy.deepcopy(self.env)
        if env is not None and env["result"].get("evaluated_at_utc") == "NOW":
            env["result"]["evaluated_at_utc"] = iso(datetime.now(UTC))
        return env


# Quotes matching the real envelopes' price scale.
BUY_SYMBOLS = {
    "EURUSDc": {"bid": 1.23790, "ask": 1.23798, "digits": 5, "point": 0.00001},
    "GBPUSDc": {"bid": 1.31943, "ask": 1.31953, "digits": 5, "point": 0.00001},
    "USDJPYc": {"bid": 159.119, "ask": 159.129, "digits": 3, "point": 0.001},
}
SELL_SYMBOLS = {
    "EURUSDc": {"bid": 0.96200, "ask": 0.96208, "digits": 5, "point": 0.00001},
    "GBPUSDc": {"bid": 0.96200, "ask": 0.96210, "digits": 5, "point": 0.00001},
    "USDJPYc": {"bid": 159.119, "ask": 159.129, "digits": 3, "point": 0.001},
}
BUY_LEVEL = (1.24600, 1.24700)  # reward 0.00802 from ask 1.23798; SL 1.23432 -> 1:2.19
SELL_LEVEL = (0.95300, 0.95400)  # reward 0.00800 from bid 0.96200; SL 0.96568 -> 1:2.17


def make_fake(symbols: dict | None = None, **kwargs) -> FakeMT5:
    return FakeMT5(symbols=copy.deepcopy(symbols or BUY_SYMBOLS), **kwargs)


def stack(fake: FakeMT5, analysis: StubAnalysis, *, running=None, clock=None, **risk_kwargs):
    live = LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake),
        terminal_running=running or (lambda: fake.running),
        reconnect_interval=0,
        clock=clock,
    )
    account = AccountIntelligenceService(live, clock=clock)
    return live, account, RiskService(analysis, account, live, clock=clock, **risk_kwargs)


def propose(
    fake=None, env=None, symbol="EURUSD", timeframe="H1", costs=VERIFIED_COSTS, **kwargs
) -> dict:
    """One proposal through the real service. ``costs`` defaults to the synthetic verified
    terms so sizing/daily/broker tests reach the approval branch; pass PRODUCTION_COSTS to
    see the production behaviour (net reward:risk unverified)."""
    fake = fake if fake is not None else make_fake()
    env = env if env is not None else envelope(level=BUY_LEVEL)
    _, _, risk = stack(fake, StubAnalysis(env), costs=costs, **kwargs)
    return risk.proposal(symbol, timeframe)


def check_ids(result: dict, kind: str = "rejection_reasons") -> list[str]:
    return [item["id"] for item in result[kind]]
