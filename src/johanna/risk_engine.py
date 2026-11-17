"""Phase 4: risk management and trade construction (deterministic, pure, read-only).

Turns a qualified T6 trend-continuation candidate into *proposed* trade parameters and checks
them against the account's risk policy and the broker's real constraints. Nothing here places,
modifies or closes an order; there is no clock, no I/O and no MT5 access. The service
(``johanna.risk_service``) gathers the inputs and calls the two stages:

1. ``plan_trade(inputs)``: data validation, daily exposure, trade construction and sizing.
   Returns the draft result and the MT5 calculator requests that verify it.
2. ``finalise(plan, calculations)``: reconciles the MT5 calculator answers (profit at
   stop-loss / take-profit, margin) and produces the final decision.

Decision statuses: ``APPROVED_FOR_MANUAL_REVIEW`` (every implemented check passed -- not an
instruction to trade, no guarantee of execution or profit), ``REJECTED`` (a verified input
fails a rule) and ``UNVERIFIED`` (a mandatory input could not be verified). A proposal is never
approved while any mandatory validation is unverified.

Methodology (documented here, reported in every result):

- **Reference capital**: position sizing uses current equity (0.5% per trade); the daily
  limit uses min(balance, equity) x 2%, so floating profit never raises it. Both must be
  verified in the same fresh snapshot, otherwise daily risk is unverified.
- **Trading day**: the broker server's calendar day (server clock verified against live ticks;
  UTC on the connected Exness server). Daily used risk = today's realised losses (see
  ``_realised``: losing buy/sell deals incl. their swap, commission and fee; negative
  standalone charge/commission/tax deals; negative correction/interest deals; balance, credit
  and bonus excluded; gains never add allowance) + money at risk to stop-loss on open
  positions and pending orders (MT5 calculator; a stop-limit order at the larger of its
  trigger and limit prices). Without a stop-loss the risk is unbounded: no approval.
- **Entry**: market proposal. BUY enters at the ask, SELL at the bid (the executable sides).
  The proposal is valid only at that price or better and until the next candle close.
- **Stop-loss**: beyond the last confirmed swing (``L_last`` for BUY, ``H_last`` for SELL) by
  ``stop_buffer_atr`` x ATR14, rounded away from the entry to the tick size.
- **Take-profit**: the near edge of the level T6's TC6 check selected (nearest qualifying
  opposing level), rounded towards the entry. No such level: rejected.
- **Spread**: a BUY's stop and target trigger on the bid and it entered at the ask, so the loss
  ``ask - SL`` and reward ``TP - ask`` already include the spread (SELL mirrored); it is not
  added again. ``gross_reward_risk`` is measured on these executable distances.
- **Net reward:risk (costs gate)**: commission (a verified round-trip rate per lot only),
  swap (verified terms; direction; holding-period assumption; triple-swap day; credits never
  counted in favour) and slippage (a defensible bound per side only; none exists today, and
  unmodelled slippage is never zero) are added to the loss and deducted from the reward. If
  any cost cannot be established the net ratio is ``unverified`` and the proposal cannot be
  approved, whatever the gross ratio; an estimated net ratio below 1:2 is rejected.
- **Money per lot** at the stop-loss is computed two ways and must agree within
  ``calc_tolerance``: from MT5 tick values (``tick_value_loss`` per ``tick_size``) and from
  the contract size with the verified conversion (profit currency -> USC). For a pair whose
  profit currency is not USD (USDJPYc) the contract figure is also computed at the stop-loss
  price; sizing uses the largest figure (conservative). The MT5 profit calculator must also
  agree for the final volume.
- **Volume** = risk budget / (price loss + applicable costs) per lot, rounded *down* to the
  broker's step; never raised to the minimum. Below the minimum: rejected. Above the maximum:
  capped (lower risk). Without established costs there is no actionable volume (only a
  labelled price-risk-only illustration) and the 0.5% limit is not claimed.
- **Margin** from MT5's margin calculator, reconciled with contract x volume x margin-currency
  rate / leverage. Disagreement or no calculator: unverified.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

APPROVED = "APPROVED_FOR_MANUAL_REVIEW"
REJECTED = "REJECTED"
UNVERIFIED = "UNVERIFIED"
RISK_ENGINE_VERSION = 1
NOTE = (
    "Proposed trade parameters for manual review only. Johanna has not placed and will not "
    "place this trade; approval is not an instruction to trade and guarantees neither "
    "execution nor profit."
)
_TIMEFRAME_SECONDS = {"H1": 3600, "H4": 14400, "D1": 86400}
# Every area must pass for an approval; "costs" is the F1 net reward:risk gate.
MANDATORY_AREAS = (
    "data",
    "construction",
    "per_trade_risk",
    "daily_risk",
    "broker",
    "margin",
    "costs",
)
REWARD_RISK_BASIS = (
    "gross_reward_risk includes the spread once (entry at the executable side, exits at the "
    "trigger side) and excludes commission, swap and slippage"
)
# F2: MT5 deal types (ENUM_DEAL_TYPE) for today's realised losses.
_TRADE_DEAL_TYPES = {0: "buy", 1: "sell"}
_COST_DEAL_TYPES = {
    4: "charge",
    7: "commission",
    8: "daily commission",
    9: "monthly commission",
    10: "daily agent commission",
    11: "monthly agent commission",
    17: "tax",
}
# Meaning cannot be classified reliably (a correction or interest can be a debit or a credit):
# a negative amount is counted as a realised cost (conservative), a positive one is ignored.
_AMBIGUOUS_DEAL_TYPES = {5: "correction", 12: "interest"}
_EXCLUDED_DEAL_TYPES = {2: "balance", 3: "credit", 6: "bonus"}  # deposits, withdrawals, credit
_WEEKDAY_NAMES = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


@dataclass(frozen=True)
class RiskPolicy:
    """The confirmed risk policy. Centralised and validated; not settable from any API or
    dashboard control."""

    max_risk_per_trade: float = 0.005  # 0.5% of equity
    max_daily_risk: float = 0.02  # 2.0% of equity
    min_reward_risk: float = 2.0  # 1:2
    stop_buffer_atr: float = 0.25
    max_quote_age_seconds: float = 10.0
    max_account_age_seconds: float = 10.0
    max_analysis_age_seconds: float = 60.0
    calc_tolerance: float = 0.01  # relative agreement required between methods
    execution: str = "manual_only"
    mt5_access: str = "read_only"

    def __post_init__(self) -> None:
        def number(name: str, low: float, high: float) -> None:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int | float):
                raise ValueError(f"{name} must be a number")
            if not math.isfinite(value) or not low < value <= high:
                raise ValueError(f"{name} must be in ({low}, {high}]")

        number("max_risk_per_trade", 0.0, 0.05)
        number("max_daily_risk", 0.0, 0.10)
        number("min_reward_risk", 0.0, 10.0)
        number("max_quote_age_seconds", 0.0, 120.0)
        number("max_account_age_seconds", 0.0, 120.0)
        number("max_analysis_age_seconds", 0.0, 600.0)
        number("calc_tolerance", 0.0, 0.05)
        if not 0 <= self.stop_buffer_atr <= 3:
            raise ValueError("stop_buffer_atr must be in [0, 3]")
        if self.max_risk_per_trade > self.max_daily_risk:
            raise ValueError("per-trade risk cannot exceed the daily risk limit")
        if self.min_reward_risk < 1:
            raise ValueError("minimum reward:risk must be at least 1")
        if self.execution != "manual_only" or self.mt5_access != "read_only":
            raise ValueError("Johanna is manual-only and read-only")


RISK_POLICY = RiskPolicy()


@dataclass(frozen=True)
class CostTerms:
    """Verified transaction-cost terms used for the net reward:risk (F1).

    Empty by default: MT5 exposes no commission rate and historical zero-commission deals are
    evidence about past trades, not a verified rate; no holding period is known in advance.
    With these defaults every constructed proposal has an *unverified* net reward:risk and is
    never approved. A value may only be supplied together with its source.
    """

    # Round-trip commission per 1.0 lot in the account currency, by exact broker symbol.
    commission_round_trip_per_lot: Mapping[str, float] = field(default_factory=dict)
    commission_source: str | None = None
    # Number of daily swap rollovers the position is assumed to be held across.
    holding_rollovers: int | None = None
    holding_source: str | None = None
    # A defensible adverse-slippage bound in points per execution side, by exact broker symbol.
    # Empty = unmodelled (never zero): no approval and no verified sizing. Reserved for a
    # future, separately authorised estimate; nothing in Johanna supplies one today.
    slippage_bound_points_per_side: Mapping[str, float] = field(default_factory=dict)
    slippage_source: str | None = None

    def __post_init__(self) -> None:
        for symbol, rate in self.commission_round_trip_per_lot.items():
            if not isinstance(symbol, str) or not _finite(rate) or rate < 0:
                raise ValueError("commission rates must be finite, non-negative and per symbol")
        if self.commission_round_trip_per_lot and not self.commission_source:
            raise ValueError("a commission rate needs its verified source")
        for symbol, bound in self.slippage_bound_points_per_side.items():
            if not isinstance(symbol, str) or not _finite(bound) or bound < 0:
                raise ValueError("slippage bounds must be finite, non-negative and per symbol")
        if self.slippage_bound_points_per_side and not self.slippage_source:
            raise ValueError("a slippage bound needs its source")
        if self.holding_rollovers is not None:
            if isinstance(self.holding_rollovers, bool) or not isinstance(
                self.holding_rollovers, int
            ):
                raise ValueError("holding_rollovers must be a whole number")
            if not 0 <= self.holding_rollovers <= 260:
                raise ValueError("holding_rollovers must be between 0 and 260")
            if not self.holding_source:
                raise ValueError("a holding-period assumption needs its source")


COST_TERMS = CostTerms()


@dataclass(frozen=True)
class RiskInputs:
    """Everything the engine uses; nothing else is read."""

    symbol: str  # the internal pair, e.g. EURUSD
    timeframe: str
    analysis: Mapping[str, Any] | None  # MarketAnalysisService envelope (None: unavailable)
    analysis_error: str | None
    account: Mapping[str, Any]  # AccountIntelligenceService.summary()
    instrument: Mapping[str, Any] | None  # AccountIntelligenceService.instrument() payload
    exposure: Mapping[str, Any]  # {"status": "available", "identity", "value"} | unavailable
    evaluated_at: datetime
    day_start_utc: datetime
    policy: RiskPolicy = field(default=RISK_POLICY)
    requested_symbol: str | None = None  # as requested (pair or broker symbol, e.g. EURUSDc)
    costs: CostTerms = field(default=COST_TERMS)
    trading_weekday: int | None = None  # weekday of the broker server day (Monday = 0)


# ====================================================================== helpers
def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _finite(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _verified(report: Any) -> Any:
    """The value of a Phase 3 field report if it is verified, else ``None``."""
    if isinstance(report, Mapping) and report.get("status") == "verified":
        return report.get("value")
    return None


def _round_to_tick(price: float, tick: float, mode: str) -> float:
    step = Decimal(str(tick))
    units = (Decimal(str(price)) / step).to_integral_value(
        rounding=ROUND_FLOOR if mode == "down" else ROUND_CEILING
    )
    return float(units * step)


def floor_volume(theoretical: float, minimum: float, step: float) -> float:
    """Largest ``minimum + k * step`` that does not exceed ``theoretical`` (0 if none)."""
    t, lo, st = Decimal(repr(theoretical)), Decimal(str(minimum)), Decimal(str(step))
    if t < lo:
        return 0.0
    k = ((t - lo) / st).to_integral_value(rounding=ROUND_FLOOR)
    return float(lo + k * st)


def _close(a: float, b: float, tolerance: float, absolute: float = 1e-9) -> bool:
    return abs(a - b) <= max(absolute, tolerance * max(abs(a), abs(b)))


def _calculator_slack(account: Mapping[str, Any]) -> float:
    """MT5's calculators return amounts rounded to the account currency's digits (observed
    live: margin 0.55 for a formula value of 0.549845 USC). Half a unit of the last digit is
    allowed in addition to the relative tolerance; no slack when the digits are unverified."""
    digits = account.get("currency_digits")
    return 0.5 * 10.0**-digits if isinstance(digits, int) and 0 <= digits <= 8 else 1e-9


def trading_day_start(now_utc: datetime, server_clock: Any) -> datetime:
    """Start of the broker server's calendar day containing ``now_utc``, as UTC."""
    wall = server_clock.from_utc(now_utc)
    return server_clock.to_utc(wall - (wall % 86400))


def trading_weekday(day_start_utc: datetime, server_clock: Any) -> int:
    """Weekday (Monday = 0) of the broker server day starting at ``day_start_utc``."""
    wall = server_clock.from_utc(day_start_utc)
    return (datetime(1970, 1, 1) + timedelta(seconds=wall)).weekday()


class _Result:
    """Accumulates the structured result; the final status is derived, never set directly."""

    def __init__(self, inputs: RiskInputs) -> None:
        policy = inputs.policy
        self.rejections: list[dict[str, str]] = []
        self.unverified: list[dict[str, str]] = []
        self.warnings: list[str] = []
        self.data: dict[str, Any] = {
            "risk_engine_version": RISK_ENGINE_VERSION,
            "status": None,
            "symbol": inputs.symbol,
            "requested_symbol": inputs.requested_symbol or inputs.symbol,
            "broker_symbol": None,
            "timeframe": inputs.timeframe,
            "direction": None,
            "evaluated_at_utc": _iso(inputs.evaluated_at),
            "setup": None,
            "provenance": None,
            "policy": asdict(policy),
            "proposal": None,
            "gross_reward_risk": None,
            "reward_risk_basis": REWARD_RISK_BASIS,
            "net_reward_risk": None,
            "sizing": None,
            "costs": None,
            "margin": None,
            "validations": {
                name: {"status": "not_evaluated", "checks": []} for name in MANDATORY_AREAS
            },
            "daily_risk": None,
            "rejection_reasons": self.rejections,
            "unverified": self.unverified,
            "warnings": self.warnings,
            "note": NOTE,
        }

    def check(self, area: str, check_id: str, outcome: str, detail: str) -> None:
        """Record a check: ``passed``, ``failed`` (rejection) or ``unverified``."""
        validation = self.data["validations"][area]
        validation["checks"].append({"id": check_id, "outcome": outcome, "detail": detail})
        states = {c["outcome"] for c in validation["checks"]}
        validation["status"] = (
            "failed" if "failed" in states else "unverified" if "unverified" in states else "passed"
        )
        if outcome == "failed":
            self.rejections.append({"area": area, "id": check_id, "reason": detail})
        elif outcome == "unverified":
            self.unverified.append({"area": area, "id": check_id, "reason": detail})

    def status(self) -> str:
        if self.rejections:
            return REJECTED
        if self.unverified:
            return UNVERIFIED
        if any(self.data["validations"][name]["status"] != "passed" for name in MANDATORY_AREAS):
            return UNVERIFIED
        return APPROVED

    def done(self) -> dict[str, Any]:
        self.data["status"] = self.status()
        return self.data


# ====================================================================== stage 1
def plan_trade(inputs: RiskInputs) -> dict[str, Any]:
    """Validate, construct and size. Returns ``{"result", "calc_requests", "context"}``."""
    r = _Result(inputs)
    policy = inputs.policy
    account = _account(inputs, r)
    instrument = _instrument(inputs, r)
    setup = _setup(inputs, r)
    daily = _daily(inputs, r, account)
    plan: dict[str, Any] = {"result": r, "calc_requests": [], "context": {}}
    if setup is None or instrument is None or account is None:
        return plan  # later stages stay "not_evaluated"; the status is never approved
    trade = _construct(inputs, r, setup, instrument)
    if trade is None:
        return plan
    money = _price_money(inputs, r, account, instrument, trade)
    if money is None:
        return plan
    costs = _cost_components(inputs, account, instrument, trade)
    r.data["costs"] = costs["view"]
    _net_gate(inputs, r, account, trade, money, costs)
    sizing = _size(inputs, r, account, instrument, daily, money, costs)
    if sizing is None:
        return plan
    _broker(r, account, instrument, trade)
    if not sizing["verified"]:
        r.check(
            "margin",
            "margin_volume",
            "unverified",
            "no verified volume (transaction costs not established): margin not evaluated",
        )
        return plan  # no calculator call for a volume that is not actionable
    side, broker = trade["side"], instrument["broker_symbol"]
    volume = sizing["volume"]
    plan["calc_requests"] = [
        ("profit", side, broker, volume, trade["entry"], trade["stop_loss"]),
        ("profit", side, broker, volume, trade["entry"], trade["take_profit"]),
        ("margin", side, broker, volume, trade["entry"]),
    ]
    plan["context"] = {
        "account": account,
        "instrument": instrument,
        "trade": trade,
        "sizing": sizing,
        "policy": policy,
    }
    return plan


def _account(inputs: RiskInputs, r: _Result) -> dict[str, Any] | None:
    payload = inputs.account or {}
    policy = inputs.policy
    if payload.get("status") != "LIVE":
        r.check(
            "data",
            "account_live",
            "unverified",
            f"account information is {payload.get('status') or 'unavailable'}, not live"
            + (f" ({payload.get('reason')})" if payload.get("reason") else ""),
        )
        return None
    provenance = payload.get("provenance") or {}
    if provenance.get("status") != "established":
        r.check("data", "account_provenance", "unverified", "broker provenance not established")
        return None
    retrieved = _parse(payload.get("retrieved_at_utc"))
    age = (inputs.evaluated_at - retrieved).total_seconds() if retrieved else None
    if age is None or age > policy.max_account_age_seconds or age < -5:
        r.check(
            "data",
            "account_fresh",
            "unverified",
            f"account information age {age} s exceeds {policy.max_account_age_seconds} s",
        )
        return None
    fields = payload.get("account") or {}
    values = {
        k: _verified(fields.get(k))
        for k in ("currency", "equity", "margin_free", "margin", "leverage")
    }
    missing = [k for k, v in values.items() if v is None]
    if missing:
        r.check("data", "account_fields", "unverified", f"not verified: {', '.join(missing)}")
        return None
    currency = fields["currency"]
    unit = currency.get("unit") or {}
    if unit.get("confirmation") != "consistent":
        r.check(
            "data",
            "account_currency_unit",
            "unverified",
            f"account currency unit of {values['currency']} not confirmed by tick values",
        )
        return None
    if values["equity"] <= 0:
        r.check("data", "account_equity", "failed", "account equity is not positive")
        return None
    r.check(
        "data",
        "account",
        "passed",
        f"equity, free margin, margin and leverage verified (age {age:.1f} s)",
    )
    so_mode = _verified(fields.get("margin_so_mode"))
    so_call = _verified(fields.get("margin_so_call"))
    trade_allowed = _verified(fields.get("trade_allowed"))
    r.data["provenance"] = {
        "account": {
            "company": provenance.get("company"),
            "server": provenance.get("server"),
            "account_type": provenance.get("account_type"),
            "retrieved_at_utc": payload.get("retrieved_at_utc"),
            "age_seconds": round(age, 1),
        }
    }
    return {
        **values,
        "unit": unit,
        "server": provenance.get("server"),
        "margin_so_mode": so_mode,
        "margin_so_call": so_call,
        "trade_allowed": trade_allowed,
        "currency_digits": _verified(fields.get("currency_digits")),
        # F4: the daily limit uses min(balance, equity); None (unverified) blocks daily risk.
        "balance": _verified(fields.get("balance")),
    }


def _instrument(inputs: RiskInputs, r: _Result) -> dict[str, Any] | None:
    payload = inputs.instrument or {}
    item = payload.get("instrument") if payload else None
    policy = inputs.policy
    if payload.get("status") != "LIVE" or not item:
        r.check("data", "instrument_live", "unverified", "instrument specification not live")
        return None
    r.data["broker_symbol"] = item.get("broker_symbol")
    requested = (inputs.requested_symbol or inputs.symbol).strip()
    if requested.upper() != inputs.symbol.upper() and (
        str(item.get("broker_symbol") or "").upper() != requested.upper()
    ):
        # F3: a broker-form request must name this broker's actual symbol for the pair.
        r.check(
            "data",
            "symbol_resolution",
            "failed",
            f"requested {requested!r} is not this broker's symbol for {inputs.symbol} "
            f"({item.get('broker_symbol')})",
        )
        return None
    if item.get("validation") != "verified":
        r.check(
            "data",
            "instrument_verified",
            "unverified",
            f"instrument specification validation is {item.get('validation')}",
        )
        return None
    fields = item["fields"]
    names = (
        "contract_size",
        "digits",
        "point",
        "tick_size",
        "tick_value_profit",
        "tick_value_loss",
        "volume_min",
        "volume_max",
        "volume_step",
        "currency_base",
        "currency_profit",
        "currency_margin",
        "trade_mode",
        "execution_mode",
        "stops_level",
        "freeze_level",
    )
    values = {k: _verified(fields.get(k)) for k in names}
    missing = [k for k, v in values.items() if v is None]
    if missing:
        r.check("data", "instrument_fields", "unverified", f"not verified: {', '.join(missing)}")
        return None
    quote = item.get("quote") or {}
    bid, ask = _verified(quote.get("bid")), _verified(quote.get("ask"))
    quote_time = _parse(quote.get("quote_time_utc"))
    if bid is None or ask is None or quote_time is None:
        r.check("data", "quote", "unverified", "no verified bid/ask")
        return None
    age = (inputs.evaluated_at - quote_time).total_seconds()
    if age > policy.max_quote_age_seconds:
        r.check(
            "data",
            "quote_fresh",
            "failed",
            f"quote is {age:.1f} s old (limit {policy.max_quote_age_seconds:g} s)",
        )
        return None
    r.check("data", "instrument", "passed", f"specification verified; quote age {age:.1f} s")
    swap = {
        k: _verified(fields.get(k))
        for k in ("swap_mode", "swap_long", "swap_short", "swap_rollover3days")
    }
    r.data["provenance"] = {
        **(r.data["provenance"] or {}),
        "instrument": {
            "broker_symbol": item.get("broker_symbol"),
            "retrieved_at_utc": item.get("retrieved_at_utc"),
            "quote_time_utc": quote.get("quote_time_utc"),
            "quote_age_seconds": round(age, 1),
            "server": (item.get("provenance") or {}).get("server"),
        },
    }
    return {
        **values,
        "broker_symbol": item["broker_symbol"],
        "bid": bid,
        "ask": ask,
        "quote_age": age,
        "spread_points": _verified(quote.get("spread_points")),
        "swap": swap,
        "filling_modes": _verified(fields.get("filling_modes")),
        "server": (item.get("provenance") or {}).get("server"),
    }


def _setup(inputs: RiskInputs, r: _Result) -> dict[str, Any] | None:
    envelope = inputs.analysis
    policy = inputs.policy
    if envelope is None:
        r.check(
            "data",
            "analysis",
            "unverified",
            f"T6 analysis unavailable ({inputs.analysis_error or 'unknown reason'})",
        )
        return None
    result = envelope.get("result") or {}
    decision = result.get("decision") or {}
    opportunity = result.get("opportunity") or {}
    snapshot = envelope.get("snapshot") or {}
    r.data["setup"] = {
        "setup": opportunity.get("setup"),
        "t6_outcome": decision.get("outcome"),
        "t6_rule": decision.get("rule"),
        "t6_reasons": decision.get("reasons"),
        "direction": opportunity.get("direction"),
        "snapshot_id": (result.get("inputs") or {}).get("snapshot_id"),
        "evaluated_at_utc": result.get("evaluated_at_utc"),
        "as_of_utc": result.get("as_of_utc"),
        "setup_id": None,
    }
    if snapshot.get("snapshot_id"):
        r.data["setup"]["snapshot_id"] = snapshot.get("snapshot_id")
    evaluated = _parse(result.get("evaluated_at_utc"))
    if evaluated is None or (inputs.evaluated_at - evaluated).total_seconds() > (
        policy.max_analysis_age_seconds
    ):
        r.check("data", "analysis_fresh", "unverified", "T6 evaluation missing or too old")
        return None
    if decision.get("outcome") != "TRADE_CANDIDATE":
        r.check(
            "data",
            "t6_candidate",
            "failed",
            f"no qualified T6 opportunity: {decision.get('outcome')} (rule {decision.get('rule')})",
        )
        return None
    direction = opportunity.get("direction")
    trusted = (result.get("market_condition") or {}).get("trusted")
    if (
        opportunity.get("status") != "qualified"
        or direction not in ("bullish", "bearish")
        or not trusted
    ):
        r.check("data", "t6_candidate", "failed", "T6 candidate is not a trusted qualified setup")
        return None
    setup_id = f"{opportunity.get('setup')}:{direction}:{r.data['setup']['snapshot_id']}"
    r.data["setup"]["setup_id"] = setup_id
    r.check("data", "t6_candidate", "passed", f"T6 R7 trade candidate ({direction})")
    features = result.get("features") or {}
    return {
        "direction": direction,
        "features": features,
        "opportunity": opportunity,
        "result": result,
    }


def _daily(inputs: RiskInputs, r: _Result, account: dict[str, Any] | None) -> dict[str, Any]:
    """Daily used risk from today's deals and the open/pending exposure (read-only)."""
    exposure = inputs.exposure or {}
    policy = inputs.policy
    daily: dict[str, Any] = {
        "trading_day": {
            "start_utc": _iso(inputs.day_start_utc),
            "end_utc": _iso(inputs.day_start_utc + timedelta(days=1)),
            "policy": "broker server calendar day (server clock verified against live ticks)",
        },
        "reference_capital": None,
        "limit": None,
        "realised_losses": None,
        "realised_breakdown": None,
        "open_position_risk": None,
        "pending_order_risk": None,
        "used": None,
        "remaining": None,
        "proposed": None,
        "positions": None,
        "pending_orders": None,
        "deals_today": None,
        "status": "unverified",
    }
    r.data["daily_risk"] = daily
    if exposure.get("status") != "available":
        r.check(
            "daily_risk",
            "exposure",
            "unverified",
            "open positions/orders/deals could not be read "
            f"({exposure.get('reason') or 'unknown'})",
        )
        return daily
    value = exposure.get("value") or {}
    server = (exposure.get("identity") or {}).get("server")
    if account is None or server != account.get("server"):
        r.check(
            "daily_risk",
            "exposure_provenance",
            "unverified",
            "exposure read not tied to the verified account",
        )
        return daily
    if value.get("day_start_utc") != inputs.day_start_utc:
        r.check(
            "daily_risk", "trading_day", "unverified", "exposure read covers another trading day"
        )
        return daily
    deals = value.get("deals") or []
    breakdown = _realised(deals)
    if breakdown.get("problem"):
        r.check("daily_risk", "deals", "unverified", breakdown["problem"])
        return daily
    realised = breakdown["total"]
    open_risk, pending_risk = 0.0, 0.0
    for kind, items in (
        ("position", value.get("positions") or []),
        ("pending order", value.get("orders") or []),
    ):
        for item in items:
            risk = item.get("risk_to_stop") or {}
            if risk.get("status") == "unbounded":
                r.check(
                    "daily_risk",
                    "open_risk",
                    "failed",
                    f"open {kind} on {item.get('symbol')} has no stop-loss: risk is unbounded",
                )
                return daily
            if risk.get("status") != "verified" or not _finite(risk.get("amount")):
                r.check(
                    "daily_risk",
                    "open_risk",
                    "unverified",
                    f"risk of an open {kind} could not be verified",
                )
                return daily
            if kind == "position":
                open_risk += risk["amount"]
            else:
                pending_risk += risk["amount"]
    daily.update(
        positions=len(value.get("positions") or []),
        pending_orders=len(value.get("orders") or []),
        deals_today=len(deals),
        realised_losses=round(realised, 2),
        realised_breakdown={k: v for k, v in breakdown.items() if k not in ("total", "problem")},
        open_position_risk=round(open_risk, 2),
        pending_order_risk=round(pending_risk, 2),
    )
    # F4: floating profit must not raise the daily limit -> min(balance, equity), both verified
    # in the same fresh account snapshot (the data area checks freshness and provenance).
    balance, equity = account.get("balance"), account.get("equity")
    if not _finite(balance) or not _finite(equity):
        r.check(
            "daily_risk",
            "reference_capital",
            "unverified",
            "balance and equity must both be verified for the daily limit (min of the two)",
        )
        return daily
    reference = min(balance, equity)
    daily["reference_capital"] = {
        "basis": "min(balance, equity)",
        "value": round(reference, 2),
        "from": "balance" if balance <= equity else "equity",
    }
    limit = reference * policy.max_daily_risk
    used = realised + open_risk + pending_risk
    daily.update(limit=round(limit, 2), used=round(used, 2), remaining=round(limit - used, 2))
    daily["status"] = "verified"
    daily["_limit"], daily["_used"] = limit, used
    return daily


def _realised(deals: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """F2: today's realised losses by deal type; each deal is counted exactly once.

    - buy/sell deals: profit + swap + commission + fee of the deal, counted when negative (the
      commission, swap and fee booked on a trade deal belong to that deal only);
    - standalone cost deals (charge, commissions, tax): their amount, counted when negative;
    - correction and interest: a negative amount is counted (conservative), a positive one is
      ignored -- their meaning cannot be classified reliably;
    - balance, credit and bonus operations: excluded (not trading results);
    - any other type with a non-zero amount, or unreadable values: unverified.
    Positive amounts never replenish the allowance.
    """
    out = {
        "total": 0.0,
        "trade_losses": 0.0,
        "cost_deals": 0.0,
        "ambiguous_debits": 0.0,
        "excluded_operations": 0,
        "ignored_credits": 0,
        "problem": None,
    }
    for deal in deals:
        kind = deal.get("type")
        if isinstance(kind, bool) or not isinstance(kind, int):
            out["problem"] = "a deal of today has no readable type"
            return out
        if kind in _EXCLUDED_DEAL_TYPES:
            out["excluded_operations"] += 1
            continue
        parts = [deal.get(k) for k in ("profit", "swap", "commission", "fee")]
        if not all(_finite(p) for p in parts):
            out["problem"] = "a deal of today has non-numeric results"
            return out
        amount = sum(parts)
        if kind in _TRADE_DEAL_TYPES:
            bucket = "trade_losses"
        elif kind in _COST_DEAL_TYPES:
            bucket = "cost_deals"
        elif kind in _AMBIGUOUS_DEAL_TYPES:
            bucket = "ambiguous_debits"
        elif amount == 0:
            continue  # an unclassified record without money effect
        else:
            out["problem"] = f"a deal of today has unclassified type {kind} with a non-zero amount"
            return out
        if amount < 0:
            out[bucket] -= amount
            out["total"] -= amount
        elif amount > 0 and bucket != "trade_losses":
            out["ignored_credits"] += 1
    for key in ("total", "trade_losses", "cost_deals", "ambiguous_debits"):
        out[key] = round(out[key], 10)
    return out


def _construct(
    inputs: RiskInputs, r: _Result, setup: dict[str, Any], ins: dict[str, Any]
) -> dict[str, Any] | None:
    direction = setup["direction"]
    buy = direction == "bullish"
    side = "buy" if buy else "sell"
    r.data["direction"] = "BUY" if buy else "SELL"
    features = setup["features"]
    tick = ins["tick_size"]
    bid, ask = ins["bid"], ins["ask"]
    atr = (features.get("latest") or {}).get("ATR14") or {}
    if not atr.get("usable") or not _finite(atr.get("value")) or atr["value"] <= 0:
        r.check("construction", "atr", "failed", "ATR14 unavailable: no stop-loss buffer")
        return None
    swings = features.get("swings") or {}
    role = "L_last" if buy else "H_last"
    pivot = next((p for p in swings.get("pivots") or [] if p.get("role") == role), None)
    if not swings.get("usable") or pivot is None or not _finite(pivot.get("price")):
        r.check(
            "construction", "stop_structure", "failed", f"no confirmed {role} swing for a stop-loss"
        )
        return None
    tc6 = next(
        (c for c in setup["opportunity"].get("checklist") or [] if c.get("id") == "TC6_clearance"),
        {},
    )
    level = tc6.get("selected_level")
    if not level or not _finite(level.get("low")) or not _finite(level.get("high")):
        r.check(
            "construction",
            "target_structure",
            "failed",
            "no qualifying opposing support/resistance level for a take-profit (TC6)",
        )
        return None
    buffer = inputs.policy.stop_buffer_atr * atr["value"]
    entry = ask if buy else bid
    if buy:
        stop = _round_to_tick(pivot["price"] - buffer, tick, "down")
        target = _round_to_tick(level["low"], tick, "down")
    else:
        stop = _round_to_tick(pivot["price"] + buffer, tick, "up")
        target = _round_to_tick(level["high"], tick, "up")
    digits = ins["digits"]
    entry, stop, target = (round(x, digits) for x in (entry, stop, target))
    risk_distance = entry - stop if buy else stop - entry
    reward_distance = target - entry if buy else entry - target
    trade = {
        "side": side,
        "entry": entry,
        "stop_loss": stop,
        "take_profit": target,
        "risk_distance": risk_distance,
        "reward_distance": reward_distance,
        "pivot": pivot,
        "level": level,
        "atr": atr["value"],
    }
    trigger = bid if buy else ask  # stops and targets trigger on the closing side
    ok_stop = stop < trigger and risk_distance > 0 if buy else stop > trigger and risk_distance > 0
    ok_target = (
        target > entry and reward_distance > 0 if buy else target < entry and reward_distance > 0
    )
    r.data["proposal"] = _proposal_view(inputs, trade, ins)
    if not ok_stop:
        r.check(
            "construction",
            "stop_direction",
            "failed",
            f"stop-loss {stop} is not {'below the bid' if buy else 'above the ask'} "
            f"({trigger}); the swing no longer protects this entry",
        )
        return None
    if not ok_target:
        r.check(
            "construction",
            "target_direction",
            "failed",
            f"take-profit {target} is not {'above' if buy else 'below'} the entry {entry}",
        )
        return None
    rr = reward_distance / risk_distance
    trade["gross_reward_risk"] = rr
    r.data["proposal"]["gross_reward_risk"] = round(rr, 3)
    r.data["gross_reward_risk"] = round(rr, 3)
    if rr < inputs.policy.min_reward_risk - 1e-12:
        # Costs can only lower the ratio, so a gross shortfall is already a rejection.
        r.check(
            "construction",
            "reward_risk",
            "failed",
            f"gross reward:risk {rr:.2f} is below the minimum {inputs.policy.min_reward_risk:g} "
            "(spread included; costs would only lower it)",
        )
        return None
    r.check(
        "construction",
        "levels",
        "passed",
        f"{r.data['direction']}: entry {entry}, SL {stop} ({role} {pivot['price']} - "
        f"{inputs.policy.stop_buffer_atr:g} ATR), TP {target} (TC6 level), "
        f"gross reward:risk {rr:.2f}",
    )
    return trade


def _proposal_view(
    inputs: RiskInputs, trade: dict[str, Any], ins: dict[str, Any]
) -> dict[str, Any]:
    buy = trade["side"] == "buy"
    point = ins["point"]
    seconds = _TIMEFRAME_SECONDS.get(inputs.timeframe)
    as_of = _parse(((inputs.analysis or {}).get("result") or {}).get("as_of_utc"))
    expires = as_of + timedelta(seconds=seconds) if as_of and seconds else None
    return {
        "label": "Proposed trade parameters (not an order)",
        "order_type": "market",
        "entry_price": trade["entry"],
        "entry_side": "ask" if buy else "bid",
        "entry_condition": (
            f"{'BUY' if buy else 'SELL'} at market only while the {'ask' if buy else 'bid'} is "
            f"at or {'below' if buy else 'above'} {trade['entry']} and before "
            f"{_iso(expires) if expires else 'the next candle close'}; otherwise re-evaluate"
        ),
        "valid_until_utc": _iso(expires),
        "current_bid": ins["bid"],
        "current_ask": ins["ask"],
        "stop_loss": trade["stop_loss"],
        "stop_loss_basis": f"{trade['pivot']['role']} swing {trade['pivot']['price']} "
        f"{'-' if buy else '+'} {inputs.policy.stop_buffer_atr:g} x ATR14 ({trade['atr']:.6g})",
        "take_profit": trade["take_profit"],
        "take_profit_basis": f"TC6 level {trade['level']['low']}-{trade['level']['high']} "
        f"(strength {trade['level'].get('strength')}), near edge",
        "stop_loss_distance": round(trade["risk_distance"], 10),
        "stop_loss_distance_points": round(trade["risk_distance"] / point, 1),
        "take_profit_distance": round(trade["reward_distance"], 10),
        "take_profit_distance_points": round(trade["reward_distance"] / point, 1),
        "gross_reward_risk": None,
        "triggers": "stop-loss and take-profit trigger on the "
        f"{'bid' if buy else 'ask'} ({'BUY' if buy else 'SELL'} positions close on that side)",
    }


def _money_per_lot(
    account: dict[str, Any], ins: dict[str, Any], trade: dict[str, Any]
) -> dict[str, Any]:
    """USC per 1.0 lot at stop-loss and take-profit, by tick value and by contract size."""
    tick = ins["tick_size"]
    loss_tick = trade["risk_distance"] / tick * ins["tick_value_loss"]
    reward_tick = trade["reward_distance"] / tick * ins["tick_value_profit"]
    unit = account["unit"]
    factor = 100.0 if unit.get("kind") == "cent" else 1.0
    major = unit.get("major")
    contract = ins["contract_size"]
    out = {"loss_tick": loss_tick, "reward_tick": reward_tick, "conversion": None}
    if ins["currency_profit"] == major:
        rate_loss = rate_profit = rate_exit = factor
        out["conversion"] = (
            f"profit currency {major}: x {factor:g} ({account['currency']} per {major})"
        )
    elif ins["currency_base"] == major:
        rate_loss, rate_profit = factor / ins["bid"], factor / ins["ask"]
        rate_exit = factor / trade["stop_loss"]
        out["conversion"] = (
            f"profit currency {ins['currency_profit']} -> {major} through the symbol's own price "
            f"(loss at bid {ins['bid']}, profit at ask {ins['ask']}; also at the stop-loss price)"
        )
    else:
        return out
    out["loss_contract"] = trade["risk_distance"] * contract * rate_loss
    out["loss_contract_at_exit"] = trade["risk_distance"] * contract * rate_exit
    out["reward_contract"] = trade["reward_distance"] * contract * rate_profit
    return out


def _price_money(
    inputs: RiskInputs,
    r: _Result,
    account: dict[str, Any],
    ins: dict[str, Any],
    trade: dict[str, Any],
) -> dict[str, Any] | None:
    """Price loss / reward per 1.0 lot (spread-inclusive), two methods reconciled."""
    policy = inputs.policy
    money = _money_per_lot(account, ins, trade)
    if "loss_contract" not in money:
        r.check(
            "per_trade_risk",
            "conversion",
            "unverified",
            f"no verified conversion from {ins['currency_profit']} to {account['currency']}",
        )
        return None
    if not _close(money["loss_tick"], money["loss_contract"], policy.calc_tolerance) or not _close(
        money["reward_tick"], money["reward_contract"], policy.calc_tolerance
    ):
        r.check(
            "per_trade_risk",
            "methods_agree",
            "failed",
            "tick-value and contract-size calculations disagree: loss/lot "
            f"{money['loss_tick']:.4f} vs {money['loss_contract']:.4f}, reward/lot "
            f"{money['reward_tick']:.4f} vs {money['reward_contract']:.4f} {account['currency']}",
        )
        return None
    money["price_loss"] = max(
        money["loss_tick"], money["loss_contract"], money["loss_contract_at_exit"]
    )
    money["price_reward"] = min(money["reward_tick"], money["reward_contract"])
    return money


def _size(
    inputs: RiskInputs,
    r: _Result,
    account: dict[str, Any],
    ins: dict[str, Any],
    daily: dict[str, Any],
    money: dict[str, Any],
    costs: dict[str, Any],
) -> dict[str, Any] | None:
    """Cost-inclusive sizing: the largest volume whose price loss *plus* applicable costs
    (commission, negative swap, slippage bound) fits min(0.5% of equity, remaining daily
    allowance), rounded down. Without established costs there is no actionable volume: only a
    labelled, price-risk-only illustrative figure, and the 0.5% limit is not claimed."""
    policy, currency = inputs.policy, account["currency"]
    price_loss, price_reward = money["price_loss"], money["price_reward"]
    cost = costs["cost_per_lot"] if costs["established"] else None
    total_per_lot = price_loss + cost if cost is not None else None
    equity = account["equity"]
    per_trade_limit = equity * policy.max_risk_per_trade
    budget = per_trade_limit
    budget_basis = "per-trade limit"
    if daily.get("status") == "verified" and daily["limit"] is not None:
        remaining = daily["_limit"] - daily["_used"]
        if remaining < budget:
            budget, budget_basis = max(0.0, remaining), "remaining daily allowance"
    basis = total_per_lot if total_per_lot is not None else price_loss
    theoretical = budget / basis
    volume = floor_volume(theoretical, ins["volume_min"], ins["volume_step"])
    capped = False
    if volume > ins["volume_max"]:
        volume = floor_volume(ins["volume_max"], ins["volume_min"], ins["volume_step"])
        capped = True
    verified = cost is not None
    components = costs["components"]
    sizing = {
        "status": "verified" if verified else "unverified",
        "includes_costs": verified,
        "reference_capital": "equity",
        "equity": equity,
        "currency": currency,
        "max_risk_per_trade": round(per_trade_limit, 4),
        "risk_budget": round(budget, 4),
        "risk_budget_basis": budget_basis,
        "loss_per_lot": {
            "tick_value_method": round(money["loss_tick"], 6),
            "contract_size_method": round(money["loss_contract"], 6),
            "contract_size_at_stop_price": round(money["loss_contract_at_exit"], 6),
            "price_component": round(price_loss, 6),
            "commission": components["commission"],
            "swap": components["swap"],
            "slippage": components["slippage"],
            "costs_total": round(cost, 6) if cost is not None else None,
            "total": round(total_per_lot, 6) if total_per_lot is not None else None,
        },
        "reward_per_lot": {
            "tick_value_method": round(money["reward_tick"], 6),
            "contract_size_method": round(money["reward_contract"], 6),
            "price_component": round(price_reward, 6),
            "net": round(price_reward - cost, 6) if cost is not None else None,
        },
        "conversion": money["conversion"],
        "theoretical_volume": round(theoretical, 6) if verified else None,
        "volume": volume if verified and volume else None,
        "illustrative_volume_price_risk_only": None
        if verified
        else {
            "value": volume or None,
            "label": "UNVERIFIED illustration from price risk only (costs not established); "
            "not actionable",
        },
        "volume_rounding": "down to the broker step; never raised to the minimum",
        "volume_capped_at_maximum": capped,
        "broker_volume": {
            "min": ins["volume_min"],
            "max": ins["volume_max"],
            "step": ins["volume_step"],
            "contract_size": ins["contract_size"],
        },
        "estimated_price_loss": None,
        "estimated_costs": None,
        "estimated_loss": None,
        "estimated_reward_gross": None,
        "estimated_reward_net": None,
        "estimated_loss_percent_of_equity": None,
    }
    if verified and volume:
        total = volume * total_per_lot
        sizing.update(
            estimated_price_loss=round(volume * price_loss, 2),
            estimated_costs=round(volume * cost, 2),
            estimated_loss=round(total, 2),
            estimated_reward_gross=round(volume * price_reward, 2),
            estimated_reward_net=round(volume * (price_reward - cost), 2),
            estimated_loss_percent_of_equity=round(total / equity * 100, 4),
        )
    r.data["sizing"] = sizing
    if budget <= 0:
        r.check("daily_risk", "allowance", "failed", "daily risk allowance is exhausted")
        return None
    if volume < ins["volume_min"] or volume == 0:
        # Costs only lower the volume, so a price-only shortfall is already a rejection.
        r.check(
            "per_trade_risk",
            "volume_minimum",
            "failed",
            f"the risk budget {budget:.2f} {currency} allows {theoretical:.4f} lots"
            + (" including costs" if verified else " (price risk alone)")
            + f", below the broker minimum {ins['volume_min']} (not rounded up)",
        )
        return None
    if capped:
        r.warnings.append(f"volume capped at the broker maximum {ins['volume_max']} lots")
    if not verified:
        reason = (
            "0.5% per-trade limit not verified: transaction costs not established "
            f"({'; '.join(costs['problems'])}); no actionable volume"
        )
        r.check("per_trade_risk", "limit", "unverified", reason)
        r.check(
            "daily_risk",
            "limit",
            "unverified",
            "the proposal's daily-risk impact is not verified (transaction costs not established)",
        )
        return {**sizing, "verified": False}
    estimated_loss = volume * total_per_lot
    if estimated_loss > budget * (1 + 1e-9) or estimated_loss > per_trade_limit * (1 + 1e-9):
        r.check(
            "per_trade_risk",
            "limit",
            "failed",
            f"estimated loss incl. costs {estimated_loss:.2f} exceeds the budget {budget:.2f}",
        )
        return None
    r.check(
        "per_trade_risk",
        "limit",
        "passed",
        f"{volume} lots risks {estimated_loss:.2f} {currency} incl. costs "
        f"({volume * price_loss:.2f} price + {volume * cost:.2f} costs; "
        f"{estimated_loss / equity * 100:.3f}% of equity; "
        f"limit {policy.max_risk_per_trade * 100:g}%)",
    )
    if daily.get("status") == "verified":
        total = daily["_used"] + estimated_loss
        daily["proposed"] = round(estimated_loss, 2)
        daily["remaining_after"] = round(daily["_limit"] - total, 2)
        if total > daily["_limit"] * (1 + 1e-9):
            r.check(
                "daily_risk",
                "limit",
                "failed",
                f"daily risk {total:.2f} would exceed the limit {daily['_limit']:.2f}",
            )
            return None
        r.check(
            "daily_risk",
            "limit",
            "passed",
            f"used {daily['_used']:.2f} + proposed {estimated_loss:.2f} (incl. costs) "
            f"<= limit {daily['_limit']:.2f} "
            f"{currency} ({policy.max_daily_risk * 100:g}% of min(balance, equity))",
        )
    return {**sizing, "volume": volume, "verified": True, "estimated_loss_raw": estimated_loss}


def _swap_units(start_weekday: int, rollovers: int, triple_weekday: int) -> int:
    """Swap charges for ``rollovers`` daily rollovers from the current server day.

    Rollovers happen at the end of each Monday-Friday server day (none on Saturday/Sunday);
    the one ending on the broker's triple-swap weekday is charged three times.
    """
    units, day, done = 0, start_weekday, 0
    while done < rollovers:
        if day < 5:  # Monday-Friday
            units += 3 if day == triple_weekday else 1
            done += 1
        day = (day + 1) % 7
    return units


def _cost_components(
    inputs: RiskInputs, account: dict[str, Any], ins: dict[str, Any], trade: dict[str, Any]
) -> dict[str, Any]:
    """Applicable transaction costs per 1.0 lot (F1), shared by the net gate and sizing.

    Spread is already inside the price distances and is never added again. Commission needs a
    verified round-trip rate; swap for the trade's direction needs verified terms and, when it
    is a cost, a holding-period assumption (credits never help); slippage needs a defensible
    bound (``CostTerms.slippage_bound_points_per_side``), applied adversely to the entry and
    the exit. Anything missing -> not established (never zero).
    """
    terms, currency = inputs.costs, account["currency"]
    buy = trade["side"] == "buy"
    broker = ins["broker_symbol"]
    point_value = ins["point"] / ins["tick_size"] * ins["tick_value_loss"]  # per point per lot
    spread_points = ins.get("spread_points")
    spread_per_lot = spread_points * point_value if _finite(spread_points) else None
    problems: list[str] = []
    rate = terms.commission_round_trip_per_lot.get(broker)
    if rate is None:
        commission = {
            "status": "unverified",
            "per_lot_round_trip": None,
            "source": None,
            "treatment": "no verified commission rate: the MT5 symbol specification has none, "
            "and historical zero-commission deals are evidence about past trades, not a rate",
        }
        problems.append("commission rate not verified")
    else:
        commission = {
            "status": "verified",
            "per_lot_round_trip": rate,
            "source": terms.commission_source,
            "treatment": "verified round-trip rate per lot: added to the loss, deducted from "
            "the reward and included in position sizing",
        }
    swap = _swap_cost(inputs, ins, buy)
    if swap["status"] != "established":
        problems.append(swap["reason"])
    bound = terms.slippage_bound_points_per_side.get(broker)
    if bound is None:
        slippage = {
            "status": "unmodelled",
            "bound_points_per_side": None,
            "per_lot": None,
            "source": None,
            "treatment": "unmodelled: no verified estimate or conservative bound is available, "
            "and it is not treated as zero -- the net 1:2 requirement and the 0.5% limit "
            "cannot be established",
        }
        problems.append("slippage unmodelled (no verified estimate or bound)")
    else:
        slippage = {
            "status": "bounded",
            "bound_points_per_side": bound,
            "per_lot": round(2 * bound * point_value, 6),
            "source": terms.slippage_source,
            "treatment": "conservative bound applied adversely to the entry and the exit",
        }
    established = not problems
    components = {
        "commission": rate if rate is not None else None,
        "swap": swap["cost_per_lot"] if swap["status"] == "established" else None,
        "slippage": slippage["per_lot"],
    }
    cost = sum(components.values()) if established else None
    view = {
        "status": "established" if established else "unverified",
        "per_lot_total": round(cost, 6) if cost is not None else None,
        "spread": {
            "points": spread_points,
            "per_lot": round(spread_per_lot, 4) if spread_per_lot is not None else None,
            "treatment": "included once: entry at the executable side, exits at the trigger side",
        },
        "commission": commission,
        "swap": swap,
        "slippage": slippage,
        "assumptions": [
            "spread included once in the price distances (not added again)",
            f"commission: {commission['status']}"
            + (f" ({rate} {currency}/lot round trip, {terms.commission_source})" if rate else ""),
            f"swap: {swap['summary']}",
            f"slippage: {slippage['status']}"
            + (f" ({bound} points per side, {terms.slippage_source})" if bound is not None else ""),
        ],
    }
    return {
        "established": established,
        "problems": problems,
        "cost_per_lot": cost,
        "components": components,
        "view": view,
    }


def _net_gate(
    inputs: RiskInputs,
    r: _Result,
    account: dict[str, Any],
    trade: dict[str, Any],
    money: dict[str, Any],
    costs: dict[str, Any],
) -> None:
    """The mandatory ``costs`` gate: net reward:risk >= 1:2 with every applicable cost
    established (commission, swap and slippage). Anything not established -> unverified."""
    policy = inputs.policy
    net: dict[str, Any] = {
        "status": "unverified",
        "value": None,
        "assumptions": costs["view"]["assumptions"],
    }
    r.data["net_reward_risk"] = net
    if not costs["established"]:
        r.check(
            "costs",
            "net_reward_risk",
            "unverified",
            "net reward:risk unverified (" + "; ".join(costs["problems"]) + "); the gross ratio "
            f"{trade['gross_reward_risk']:.2f} does not establish the 1:2 net requirement",
        )
        return
    cost = costs["cost_per_lot"]
    net_reward, net_risk = money["price_reward"] - cost, money["price_loss"] + cost
    value = net_reward / net_risk if net_risk > 0 else None
    net.update(
        status="estimated",
        value=round(value, 3) if value is not None else None,
        cost_per_lot=round(cost, 6),
        net_reward_per_lot=round(net_reward, 6),
        net_risk_per_lot=round(net_risk, 6),
    )
    if value is None or value < policy.min_reward_risk - 1e-12:
        r.check(
            "costs",
            "net_reward_risk",
            "failed",
            f"estimated net reward:risk {net['value']} is below the minimum "
            f"{policy.min_reward_risk:g} after commission, swap and slippage bound",
        )
        return
    r.check(
        "costs",
        "net_reward_risk",
        "passed",
        f"estimated net reward:risk {value:.2f} >= {policy.min_reward_risk:g} after commission, "
        "swap and the slippage bound (spread in the price distances)",
    )


def _swap_cost(inputs: RiskInputs, ins: dict[str, Any], buy: bool) -> dict[str, Any]:
    """Swap cost per lot for the trade's direction under the holding-period assumption."""
    swap = ins["swap"]
    mode, rate = swap.get("swap_mode"), swap.get("swap_long" if buy else "swap_short")
    triple = swap.get("swap_rollover3days")
    base = {
        "mode": mode,
        "direction_rate": rate,
        "long_rate": swap.get("swap_long"),
        "short_rate": swap.get("swap_short"),
        "triple_swap_day": triple,
        "holding_rollovers": inputs.costs.holding_rollovers,
        "holding_source": inputs.costs.holding_source,
        "cost_per_lot": None,
    }

    def unverified(reason: str) -> dict[str, Any]:
        return {
            **base,
            "status": "unverified",
            "reason": reason,
            "summary": f"unverified ({reason})",
        }

    if mode == "disabled":
        return {
            **base,
            "status": "established",
            "cost_per_lot": 0.0,
            "reason": None,
            "summary": "disabled for this symbol (0)",
        }
    if not _finite(rate):
        return unverified("swap rate for this direction not verified")
    if mode == "points":
        per_rollover = rate * ins["point"] / ins["tick_size"] * ins["tick_value_loss"]
    elif mode == "deposit_currency":
        per_rollover = rate
    else:
        return unverified(f"swap mode {mode!r} cannot be converted to the account currency")
    base["per_rollover_per_lot"] = round(per_rollover, 6)
    if per_rollover >= 0:
        # A swap credit is never counted in the trade's favour (conservative).
        return {
            **base,
            "status": "established",
            "cost_per_lot": 0.0,
            "reason": None,
            "summary": f"{'credit' if per_rollover > 0 else 'zero'} for this direction "
            "(not counted in favour)",
        }
    holding = inputs.costs.holding_rollovers
    if holding is None:
        return unverified("holding period unknown while swap is a cost for this direction")
    if triple not in _WEEKDAY_NAMES or inputs.trading_weekday is None:
        return unverified("triple-swap day or server weekday not verified")
    units = _swap_units(inputs.trading_weekday, holding, _WEEKDAY_NAMES.index(triple))
    cost = -per_rollover * units
    return {
        **base,
        "status": "established",
        "cost_per_lot": round(cost, 6),
        "charge_units": units,
        "reason": None,
        "summary": f"{holding} rollover(s) assumed ({inputs.costs.holding_source}) = {units} "
        f"charge(s) incl. triple on {triple}: {cost:.4f} per lot",
    }


def _broker(
    r: _Result, account: dict[str, Any], ins: dict[str, Any], trade: dict[str, Any]
) -> None:
    buy = trade["side"] == "buy"
    mode = ins["trade_mode"]
    allowed = mode == "full" or (mode == "long_only" and buy) or (mode == "short_only" and not buy)
    r.check(
        "broker",
        "symbol_trade_mode",
        "passed" if allowed else "failed",
        f"symbol trade permission is {mode}",
    )
    if account.get("trade_allowed") is not True:
        r.check(
            "broker", "account_trading", "failed", "account trading is not allowed by the broker"
        )
    else:
        r.check("broker", "account_trading", "passed", "account trading allowed (manual trade)")
    r.check(
        "broker",
        "execution_mode",
        "passed" if ins["execution_mode"] in ("market", "instant") else "failed",
        f"execution mode {ins['execution_mode']} for a market proposal",
    )
    point = ins["point"]
    trigger = ins["bid"] if buy else ins["ask"]
    stop_gap = (trigger - trade["stop_loss"]) if buy else (trade["stop_loss"] - trigger)
    target_gap = (trade["take_profit"] - trigger) if buy else (trigger - trade["take_profit"])
    minimum = max(ins["stops_level"], ins["freeze_level"]) * point
    if min(stop_gap, target_gap) < minimum - 1e-12:
        r.check(
            "broker",
            "stops_level",
            "failed",
            f"stop-loss/take-profit closer than the broker's stops/freeze level ({minimum} price)",
        )
    else:
        r.check(
            "broker",
            "stops_level",
            "passed",
            f"SL {stop_gap / point:.0f} and TP {target_gap / point:.0f} points "
            "from the trigger price "
            f"(stops level {ins['stops_level']}, freeze level {ins['freeze_level']} points)",
        )
    if ins["stops_level"] == 0:
        r.warnings.append(
            "stops level 0 does not guarantee every price is accepted; the broker can still "
            "reject or adjust levels at execution"
        )
    tick = ins["tick_size"]
    aligned = all(
        abs(price / tick - round(price / tick)) < 1e-6
        for price in (trade["entry"], trade["stop_loss"], trade["take_profit"])
    )
    r.check(
        "broker",
        "price_precision",
        "passed" if aligned else "failed",
        f"prices on the {tick} tick grid and {ins['digits']} digits",
    )
    if ins.get("filling_modes") is not None:
        r.warnings.append(
            f"filling modes allowed by the broker: {', '.join(ins['filling_modes']) or 'none'}"
        )


# ====================================================================== stage 2
def finalise(
    plan: dict[str, Any],
    calculations: Sequence[Mapping[str, Any]] | None,
    calc_error: str | None = None,
) -> dict[str, Any]:
    """Reconcile MT5's calculator answers and return the final JSON-safe result."""
    r: _Result = plan["result"]
    if not plan["calc_requests"]:
        return _clean(r.done())
    ctx = plan["context"]
    policy: RiskPolicy = ctx["policy"]
    account, ins, trade, sizing = ctx["account"], ctx["instrument"], ctx["trade"], ctx["sizing"]
    currency = account["currency"]
    if calculations is None or len(calculations) != 3:
        reason = f"MT5 calculators unavailable ({calc_error or 'no answer'})"
        r.check("per_trade_risk", "mt5_profit_calculator", "unverified", reason)
        r.check("margin", "mt5_margin_calculator", "unverified", reason)
        return _clean(r.done())
    profit_sl, profit_tp, margin = calculations
    volume = sizing["volume"]
    expected_loss = volume * sizing["loss_per_lot"]["tick_value_method"]
    expected_reward = volume * sizing["reward_per_lot"]["tick_value_method"]
    if "value" not in profit_sl or "value" not in profit_tp:
        r.check(
            "per_trade_risk", "mt5_profit_calculator", "unverified", "MT5 profit calculator failed"
        )
    else:
        calc_loss, calc_reward = -profit_sl["value"], profit_tp["value"]
        r.data["sizing"]["mt5_calculator"] = {
            "loss_at_stop": round(calc_loss, 4),
            "reward_at_target": round(calc_reward, 4),
        }
        slack = _calculator_slack(account)
        agree = _close(calc_loss, expected_loss, policy.calc_tolerance, slack) and _close(
            calc_reward, expected_reward, policy.calc_tolerance, slack
        )
        r.check(
            "per_trade_risk",
            "mt5_profit_calculator",
            "passed" if agree else "failed",
            f"MT5 calculator loss {calc_loss:.2f} / reward {calc_reward:.2f} vs tick-value "
            f"{expected_loss:.2f} / {expected_reward:.2f} {currency}",
        )
    _margin(r, account, ins, trade, volume, margin, policy)
    return _clean(r.done())


def _margin(
    r: _Result,
    account: dict[str, Any],
    ins: dict[str, Any],
    trade: dict[str, Any],
    volume: float,
    margin: Mapping[str, Any],
    policy: RiskPolicy,
) -> None:
    unit = account["unit"]
    factor = 100.0 if unit.get("kind") == "cent" else 1.0
    major = unit.get("major")
    buy = trade["side"] == "buy"
    price = ins["ask"] if buy else ins["bid"]
    if ins["currency_margin"] == major:
        rate = factor
    elif ins["currency_margin"] == ins["currency_base"] and ins["currency_profit"] == major:
        rate = price * factor
    else:
        rate = None
    formula = (
        ins["contract_size"] * volume * rate / account["leverage"] if rate is not None else None
    )
    view = {
        "mt5_calculator": None,
        "formula": round(formula, 4) if formula is not None else None,
        "formula_basis": "contract size x volume x margin-currency rate / account leverage",
        "estimated": None,
        "free_margin_before": account["margin_free"],
        "free_margin_after": None,
        "margin_level_after_percent": None,
    }
    r.data["margin"] = view
    if "value" not in margin:
        r.check(
            "margin",
            "mt5_margin_calculator",
            "unverified",
            f"MT5 margin calculator failed: {margin.get('error')}",
        )
        return
    view["mt5_calculator"] = round(margin["value"], 4)
    slack = _calculator_slack(account)
    if formula is None or not _close(margin["value"], formula, policy.calc_tolerance, slack):
        r.check(
            "margin",
            "margin_reconciliation",
            "unverified",
            f"MT5 margin {margin['value']:.4f} could not be reconciled with the formula "
            f"({formula if formula is None else round(formula, 4)})",
        )
        return
    estimated = max(margin["value"], formula)
    free_after = account["margin_free"] - estimated
    used_after = account["margin"] + estimated
    level_after = account["equity"] / used_after * 100 if used_after > 0 else None
    view.update(
        estimated=round(estimated, 4),
        free_margin_after=round(free_after, 2),
        margin_level_after_percent=round(level_after, 1) if level_after is not None else None,
    )
    r.check(
        "margin", "margin_reconciliation", "passed", "MT5 margin calculator agrees with the formula"
    )
    if free_after <= 0:
        r.check("margin", "free_margin", "failed", "insufficient free margin for this volume")
        return
    call = account.get("margin_so_call")
    if (
        account.get("margin_so_mode") == "percent"
        and _finite(call)
        and level_after is not None
        and level_after <= call
    ):
        r.check(
            "margin",
            "margin_level",
            "failed",
            f"margin level after the trade {level_after:.0f}% would be at or below "
            f"the margin call {call:g}%",
        )
        return
    r.check(
        "margin",
        "free_margin",
        "passed",
        f"free margin after {free_after:.2f} {account['currency']}"
        + (f"; margin level {level_after:.0f}%" if level_after is not None else ""),
    )


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    """Drop private working values (``_``-prefixed keys) so the result is JSON-safe."""
    daily = data.get("daily_risk")
    if isinstance(daily, dict):
        for key in [k for k in daily if k.startswith("_")]:
            del daily[key]
    return data
