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
from johanna.cost_evidence import ACCOUNT_TYPE, CostComponent, HoldingAssumption
from johanna.live_market import LiveMarketService
from johanna.risk_engine import COST_TERMS, CostTerms
from johanna.risk_service import RiskService

# SYNTHETIC, test-only cost evidence that lets tests reach the approval branch. Production has
# no verified commission, slippage or holding assumption (COST_TERMS), so live proposals stay
# UNVERIFIED. P6.2b.1: structured components with explicit currency, unit and basis.
SYNTHETIC_SYMBOLS = ("EURUSDc", "GBPUSDc", "USDJPYc")
INTRADAY_HOURS = 1e-6  # a ~4 ms holding window: no rollover can fall inside it in practice


def synthetic_terms(
    commission: float | None = 0.0,
    slippage: float | None = 0.0,
    holding_hours: float | None = INTRADAY_HOURS,
    *,
    symbols=SYNTHETIC_SYMBOLS,
    when: datetime | None = None,
    commission_currency: str = "USC",
    commission_basis: str = "round_trip",
    slippage_source: str = "test fixture (synthetic, not a real bound)",
    lot_units: float = 1000.0,
    broker_terms: dict | None = None,
) -> CostTerms:
    """Synthetic VERIFIED evidence approved at ``when`` (default now); ``None`` omits a part."""
    when = when or datetime.now(UTC)
    terms = fixture_broker_terms() if broker_terms is None else broker_terms
    common = {
        "instruments": tuple(symbols),
        "broker_terms": {s: terms[s] for s in symbols},
        "account_type": ACCOUNT_TYPE,
        "evidence_date": when,
        "effective_utc": when,
        "status": "VERIFIED",
        "approved_at_utc": when,
    }
    components = []
    if commission is not None:
        components.append(
            CostComponent(
                component="commission",
                value=commission,
                currency=commission_currency,
                unit="amount_per_lot",
                basis=commission_basis,
                lot_units=lot_units,
                source_reference="test fixture (synthetic, not a broker term)",
                source_description="synthetic test rate",
                **common,
            )
        )
    if slippage is not None:
        components.append(
            CostComponent(
                component="slippage",
                value=slippage,
                currency=None,
                unit="points",
                basis="per_side",
                source_reference=slippage_source,
                source_description="synthetic test bound",
                **common,
            )
        )
    holding = {}
    if holding_hours is not None:
        holding = {
            tf: HoldingAssumption(
                timeframe=tf,
                max_hours=holding_hours,
                rationale="test fixture (synthetic)",
                status="APPROVED",
                effective_utc=when,
                policy_version="test-holding",
                approved_at_utc=when,
            )
            for tf in ("H1", "H4", "D1")
        }
    return CostTerms(components=tuple(components), holding=holding)


_BROKER_TERMS: dict = {}


def fixture_broker_terms() -> dict:
    """Per-instrument broker-terms fingerprints of the FAKE broker (computed once from a fake
    instrument payload; no real MT5)."""
    if not _BROKER_TERMS:
        from johanna.cost_policy import live_broker_terms  # noqa: PLC0415

        _, account, _ = stack(make_fake(), StubAnalysis())
        _BROKER_TERMS.update(live_broker_terms(account.instruments()))
    return dict(_BROKER_TERMS)


def negative_swap(fake: FakeMT5, symbol: str = "EURUSDc", long: float = -5.7, **extra) -> FakeMT5:
    """Give a fake symbol a negative long swap (the fake's default is zero, Phase 1 M-10)."""
    fake.symbols[symbol]["info"] = {
        **fake.symbols[symbol].get("info", {}),
        "swap_long": long,
        **extra,
    }
    return fake


PRODUCTION_COSTS = COST_TERMS


def _real(sign: int) -> dict:
    live = FakeLive(lambda s, t, c, n: snapshot(trend_bars(sign=sign)))
    return service(live, FixedClock(RETRIEVED + timedelta(seconds=0.8))).analyse("EURUSD", "H1")


# Genuine pipeline R7 candidates with a genuine structural target (P6.2b.8: TC6 fails without
# one). BUY: L_last 1.23455, ATR ~0.000887, TC6 resistance 1.2622 (strength 6, ~27 ATR).
# SELL: H_last 0.96545, TC6 support 0.9378 (strength 6, ~27 ATR).
REAL_BUY = _real(1)
REAL_SELL = _real(-1)
assert REAL_BUY["result"]["decision"]["rule"] == REAL_SELL["result"]["decision"]["rule"] == "R7"
NO_TARGET = object()  # sentinel: ``selected_level`` removed (None) while TC6 still claims "pass"
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
    selected_level: object = None,
) -> dict:
    """A real R7 envelope, re-timed to ``evaluated_at`` (default now) and reshaped.

    ``selected_level`` replaces the TC6 level evidence verbatim -- ``NO_TARGET`` for ``None``,
    or any malformed value -- while the checklist keeps claiming ``pass``: an envelope that T6
    can no longer produce (P6.2b.8), used to prove Phase 4 revalidates independently."""
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
    if selected_level is not None:
        tc6 = next(c for c in r["opportunity"]["checklist"] if c["id"] == "TC6_clearance")
        tc6["selected_level"] = None if selected_level is NO_TARGET else selected_level
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


def propose(fake=None, env=None, symbol="EURUSD", timeframe="H1", costs=None, **kwargs) -> dict:
    """One proposal through the real service. ``costs`` defaults to the synthetic verified
    terms so sizing/daily/broker tests reach the approval branch; pass PRODUCTION_COSTS to
    see the production behaviour (net reward:risk unverified)."""
    fake = fake if fake is not None else make_fake()
    env = env if env is not None else envelope(level=BUY_LEVEL)
    costs = VERIFIED_COSTS if costs is None else costs
    _, _, risk = stack(fake, StubAnalysis(env), costs=costs, **kwargs)
    return risk.proposal(symbol, timeframe)


def check_ids(result: dict, kind: str = "rejection_reasons") -> list[str]:
    return [item["id"] for item in result[kind]]


# Built after the helpers it needs. Phase 1 (M-10): approved one hour before import, so no test
# whose clock trails the import time can see the synthetic evidence as "not yet effective".
VERIFIED_COSTS = synthetic_terms(when=datetime.now(UTC) - timedelta(hours=1))
