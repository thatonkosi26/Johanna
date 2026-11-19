"""Phase 4 corrections F1-F4: net costs gate, cost deals, broker symbols, daily capital and
stop-limit exposure. Synthetic data only (FakeMT5 + real T6 envelopes reshaped per test)."""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime

import pytest
from fake_mt5 import READ_ONLY_CALLS
from risk_fixtures import (
    BUY_LEVEL,
    INTRADAY_HOURS,
    PRODUCTION_COSTS,
    SELL_LEVEL,
    SELL_SYMBOLS,
    VERIFIED_COSTS,
    StubAnalysis,
    check_ids,
    envelope,
    make_fake,
    negative_swap,
    stack,
    synthetic_terms,
)
from test_risk_api import _api, _call

from johanna import risk_engine
from johanna.cost_evidence import CostComponent, HoldingAssumption
from johanna.risk_engine import APPROVED, REJECTED, UNVERIFIED, CostTerms, RiskInputs

SYMBOLS = ("EURUSDc", "GBPUSDc", "USDJPYc")
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


def terms(
    commission: float | None = 0.0,
    holding: int | None = 1,
    symbols=SYMBOLS,
    slippage: float | None = 0.0,
) -> CostTerms:
    """Synthetic VERIFIED cost evidence (test-only). ``slippage``: points per side;
    ``holding``: days held (0 = intraday, no rollover; None = no holding assumption)."""
    return synthetic_terms(
        commission=commission,
        slippage=slippage,
        holding_hours=None
        if holding is None
        else (INTRADAY_HOURS if holding == 0 else 24.0 * holding),
        symbols=symbols,
    )


def fixed_rollovers(units: int):
    """Pin the rollover count for tests whose expected numbers need an exact count (the real
    broker schedule is tested deterministically in test_cost_evidence.py)."""

    def charges(start, end, triple_weekday):
        return {"units": units, "events": [{"units": units}], "seasonal_ambiguity": False}

    return charges


def _day() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def run(fake=None, env=None, *, costs=VERIFIED_COSTS, symbol="EURUSD", requested=None) -> dict:
    """The real two-stage engine (swap rollovers counted from the evaluation time)."""
    fake = fake if fake is not None else make_fake()
    env = env if env is not None else envelope(level=BUY_LEVEL)
    live, account, _ = stack(fake, StubAnalysis())
    inputs = RiskInputs(
        symbol=symbol,
        timeframe="H1",
        analysis=env,
        analysis_error=None,
        account=account.summary(),
        instrument=account.instrument(symbol),
        exposure={"status": "available", **live.exposure_read(_day())},
        evaluated_at=datetime.now(UTC),
        day_start_utc=_day(),
        requested_symbol=requested,
        costs=costs,
    )
    plan = risk_engine.plan_trade(inputs)
    calcs = (
        live.risk_calculations(plan["calc_requests"])["value"] if plan["calc_requests"] else None
    )
    result = risk_engine.finalise(plan, calcs)
    json.dumps(result, allow_nan=False)  # JSON-safe: no NaN/Infinity anywhere
    assert set(fake.calls) <= READ_ONLY_CALLS
    return result


def sell_fake():
    return make_fake(SELL_SYMBOLS)


def deal(kind: int, profit: float = 0.0, *, commission=0.0, swap=0.0, fee=0.0, ago=60) -> dict:
    return {
        "time": int(time.time()) - ago,
        "type": kind,
        "entry": 1,
        "symbol": "EURUSDc",
        "volume": 0.01,
        "price": 1.2,
        "profit": profit,
        "swap": swap,
        "commission": commission,
        "fee": fee,
    }


# ====================================================================== F1 costs gate
def test_gross_ratio_includes_spread_once_and_equals_net_without_costs() -> None:
    result = run(sell_fake(), envelope("bearish", level=SELL_LEVEL), symbol="GBPUSD")
    assert result["status"] == APPROVED
    gross = (0.962 - 0.954) / (0.96568 - 0.962)  # bid entry, ask-side exits: spread once
    assert result["gross_reward_risk"] == pytest.approx(gross, abs=1e-3)
    # zero commission and a zero swap for this direction: nothing more is subtracted
    assert result["net_reward_risk"]["value"] == pytest.approx(gross, abs=1e-3)
    assert result["costs"]["spread"]["treatment"].startswith("included once")


def test_gross_ratio_is_never_presented_as_net() -> None:
    result = run(costs=PRODUCTION_COSTS)
    assert "reward_risk" not in result["proposal"]  # no unqualified ratio anywhere
    assert result["proposal"]["gross_reward_risk"] == result["gross_reward_risk"]
    assert "excludes commission, swap and slippage" in result["reward_risk_basis"]
    assert result["net_reward_risk"]["status"] == "unverified"
    assert result["net_reward_risk"]["value"] is None


def test_verified_commission_is_included_on_both_sides() -> None:
    result = run(costs=terms(commission=20.0, holding=0))
    net = result["net_reward_risk"]
    assert net["status"] == "estimated" and net["cost_per_lot"] == 20.0
    assert net["value"] == pytest.approx((802 - 20) / (366 + 20), abs=1e-3)
    assert result["costs"]["commission"]["source"] == "test fixture (synthetic, not a broker term)"
    record = result["costs"]["commission"]["evidence"]["record"]
    assert (record["currency"], record["unit"], record["basis"]) == (
        "USC",
        "amount_per_lot",
        "round_trip",
    )
    assert result["status"] == APPROVED


def test_unknown_commission_blocks_approval() -> None:
    result = run(costs=terms(commission=0.0, symbols=("GBPUSDc",)))  # no rate for EURUSDc
    assert result["status"] == UNVERIFIED
    assert result["validations"]["costs"]["status"] == "unverified"
    assert "commission not verified" in result["unverified"][0]["reason"]


def test_historical_zero_commission_alone_is_not_a_verified_rate() -> None:
    fake = make_fake()
    fake.deals = [deal(0, commission=0.0), deal(1, 1.0, commission=0.0)]  # zero-commission trades
    result = run(fake, costs=PRODUCTION_COSTS)
    assert result["status"] == UNVERIFIED
    assert result["costs"]["commission"]["status"] == "unverified"
    assert "evidence about past trades" in result["costs"]["commission"]["treatment"]


def test_swap_direction_decides_whether_a_holding_period_is_needed() -> None:
    no_holding = terms(holding=None)
    sell = run(
        sell_fake(), envelope("bearish", level=SELL_LEVEL), symbol="GBPUSD", costs=no_holding
    )
    assert sell["costs"]["swap"]["status"] == "established"  # short swap 0: not a cost
    assert sell["status"] == APPROVED
    buy = run(negative_swap(make_fake()), costs=no_holding)  # long swap -5.7: a cost
    assert buy["costs"]["swap"]["status"] == "unverified" and buy["status"] == UNVERIFIED
    fake = make_fake()
    fake.symbols["EURUSDc"]["info"] = {"swap_long": 2.0, "swap_short": -3.0}
    credit = run(fake, costs=no_holding)
    assert credit["costs"]["swap"]["status"] == "established"
    assert credit["costs"]["swap"]["cost_per_lot"] == 0.0  # a credit never helps the ratio
    assert credit["status"] == APPROVED


# The real rollover schedule (17:00 New York, Wednesday triple, weekends, DST, boundaries) is
# tested with fixed timestamps in test_cost_evidence.py; here its count is pinned.
def test_holding_period_and_triple_swap_cost(monkeypatch) -> None:
    monkeypatch.setattr(risk_engine, "rollover_charges", fixed_rollovers(4))  # e.g. Tue + Wed x3
    tue = run(negative_swap(make_fake()), costs=terms(holding=2))
    assert tue["costs"]["swap"]["charge_units"] == 4
    assert tue["costs"]["swap"]["cost_per_lot"] == pytest.approx(5.7 * 4)
    assert tue["net_reward_risk"]["value"] == pytest.approx((802 - 22.8) / (366 + 22.8), abs=1e-3)
    monkeypatch.setattr(risk_engine, "rollover_charges", fixed_rollovers(3))
    wed = run(negative_swap(make_fake()), costs=terms(holding=1))
    assert (
        wed["costs"]["swap"]["charge_units"] == 3 and "wednesday" in wed["costs"]["swap"]["summary"]
    )


def test_real_rollover_schedule_is_used_from_the_evaluation_time() -> None:
    # a full week always contains the Wednesday triple (whatever the wall-clock time)
    result = run(negative_swap(make_fake()), costs=terms(holding=7))
    swap = result["costs"]["swap"]
    assert swap["charge_units"] == 7 and swap["rollovers"]["holiday_adjustments"] == "not_modelled"
    assert swap["cost_per_lot"] == pytest.approx(5.7 * 7)
    assert swap["holding"]["status"] == "APPROVED" and swap["holding"]["max_hours"] == 168.0


def test_unknown_holding_period_blocks_approval_when_swap_is_a_cost() -> None:
    result = run(negative_swap(make_fake()), costs=terms(commission=0.0, holding=None))
    assert result["status"] == UNVERIFIED
    assert "holding period not approved" in result["unverified"][0]["reason"]
    assert result["gross_reward_risk"] > 2  # a passing gross ratio does not override the gate


def test_unsupported_swap_mode_is_unverified_and_disabled_swap_is_zero() -> None:
    fake = make_fake()
    fake.symbols["EURUSDc"]["info"] = {"swap_mode": 5}  # interest: not convertible here
    assert run(fake)["costs"]["swap"]["status"] == "unverified"
    fake = make_fake()
    fake.symbols["EURUSDc"]["info"] = {"swap_mode": 0}
    disabled = run(fake, costs=terms(holding=None))
    assert disabled["costs"]["swap"]["cost_per_lot"] == 0.0 and disabled["status"] == APPROVED


def test_unmodelled_slippage_is_disclosed_and_blocks_the_net_ratio() -> None:
    result = run(costs=terms(slippage=None))  # commission and swap established, no bound
    assert result["costs"]["slippage"]["status"] == "unmodelled"
    assert result["net_reward_risk"]["status"] == "unverified"
    assert result["status"] == UNVERIFIED
    assert any("slippage: unmodelled" in a for a in result["net_reward_risk"]["assumptions"])
    bounded = run()  # a supplied (synthetic) bound: estimated, never "verified"
    assert bounded["net_reward_risk"]["status"] == "estimated"


def test_net_ratio_below_two_is_rejected() -> None:
    result = run(costs=terms(commission=30.0, holding=0))  # (802-30)/(366+30) = 1.95
    assert result["status"] == REJECTED and "net_reward_risk" in check_ids(result)
    assert result["gross_reward_risk"] > 2


def test_net_ratio_at_two_passes_only_when_every_other_gate_passes() -> None:
    commission = (802 - 2 * 366) / 3  # net exactly 2.0
    exact = terms(commission=commission, holding=0)
    result = run(costs=exact)
    assert result["net_reward_risk"]["value"] == pytest.approx(2.0, abs=1e-6)
    assert result["status"] == APPROVED
    stale = run(make_fake(tick_age_seconds=30), costs=exact)
    assert stale["status"] == UNVERIFIED  # the stale quote gate still decides (M3: unverified)


def test_unverified_costs_prevent_approval_although_all_else_passes() -> None:
    result = run(costs=PRODUCTION_COSTS)
    assert result["status"] == UNVERIFIED and result["gross_reward_risk"] == pytest.approx(
        2.191, abs=1e-3
    )
    validations = {k: v["status"] for k, v in result["validations"].items()}
    assert {validations[k] for k in ("data", "construction", "broker")} == {"passed"}
    # costs not established: the net gate, the 0.5% sizing claim, the daily impact and margin
    # (no actionable volume) are all unverified
    for area in ("costs", "per_trade_risk", "daily_risk", "margin"):
        assert validations[area] == "unverified"


def test_production_service_default_has_no_verified_cost_terms() -> None:
    _, _, risk = stack(make_fake(), StubAnalysis(envelope(level=BUY_LEVEL)))
    assert risk.costs == risk_engine.COST_TERMS
    assert risk.costs.components == () and risk.costs.holding == {}  # nothing configured
    assert risk.proposal("EURUSD", "H1")["status"] == UNVERIFIED


@pytest.mark.parametrize(
    "change",
    [
        {"source_reference": ""},  # no source
        {"value": -1.0},
        {"value": float("nan")},
    ],
)
def test_cost_terms_are_validated(change) -> None:
    commission = synthetic_terms().components[0]
    with pytest.raises(ValueError):
        CostComponent(**{**commission.__dict__, **change})
    holding = synthetic_terms().holding["H1"]
    for bad in ({"max_hours": -1.0}, {"max_hours": 1e9}, {"rationale": ""}):
        with pytest.raises(ValueError):
            HoldingAssumption(**{**holding.__dict__, **bad})
    with pytest.raises(ValueError):
        CostTerms(components=(commission, commission))  # two values for one cost


# ====================================================================== F2 cost deals
@pytest.mark.parametrize("kind", [4, 7, 8, 9, 10, 11, 17])
def test_each_standalone_cost_deal_is_counted_once(kind) -> None:
    fake = make_fake()
    fake.deals = [deal(kind, -2.5)]
    result = run(fake)
    daily = result["daily_risk"]
    assert daily["realised_losses"] == 2.5
    assert daily["realised_breakdown"]["cost_deals"] == 2.5
    fake.deals = [deal(kind, 2.5)]  # a refund never adds allowance
    daily = run(fake)["daily_risk"]
    assert daily["realised_losses"] == 0.0 and daily["realised_breakdown"]["ignored_credits"] == 1


@pytest.mark.parametrize("kind", [5, 12])
def test_correction_and_interest_count_debits_only(kind) -> None:
    fake = make_fake()
    fake.deals = [deal(kind, -1.25), deal(kind, 4.0)]
    daily = run(fake)["daily_risk"]
    assert daily["realised_losses"] == 1.25
    assert daily["realised_breakdown"]["ambiguous_debits"] == 1.25
    assert daily["realised_breakdown"]["ignored_credits"] == 1


def test_losing_and_winning_trades_with_attached_costs() -> None:
    fake = make_fake()
    fake.deals = [deal(1, -4.0, commission=-0.5, swap=-0.3, fee=-0.2)]  # one 5.00 loss
    assert run(fake)["daily_risk"]["realised_losses"] == 5.0
    fake.deals = [deal(1, 10.0, commission=-0.5, swap=-0.3, fee=-0.2)]  # net +9: no loss
    assert run(fake)["daily_risk"]["realised_losses"] == 0.0


def test_mixed_trades_and_cost_deals_without_double_counting() -> None:
    fake = make_fake()
    fake.deals = [
        deal(1, -4.0, commission=-1.0),  # 5.00 (its own commission once)
        deal(7, -2.0),  # a separate commission deal: 2.00, counted once more
        deal(0, 6.0),  # a win: never replenishes
        deal(4, -0.75),  # a charge
    ]
    daily = run(fake)["daily_risk"]
    assert daily["realised_losses"] == 7.75
    breakdown = daily["realised_breakdown"]
    assert (breakdown["trade_losses"], breakdown["cost_deals"]) == (5.0, 2.75)


@pytest.mark.parametrize("kind", [2, 3, 6])
def test_balance_credit_and_bonus_are_excluded(kind) -> None:
    fake = make_fake()
    fake.deals = [deal(kind, -500.0), deal(kind, 500.0)]  # a withdrawal and a deposit
    daily = run(fake)["daily_risk"]
    assert daily["realised_losses"] == 0.0
    assert daily["realised_breakdown"]["excluded_operations"] == 2


@pytest.mark.parametrize(
    "bad",
    [
        {"type": None},
        {"type": "7"},
        {"profit": float("nan")},
        {"commission": None},
        {"type": 13, "profit": -1.0},  # unclassified type with money effect
        {"type": 15, "profit": 3.0},
    ],
)
def test_incomplete_or_unclassified_deals_are_unverified(bad) -> None:
    fake = make_fake()
    fake.deals = [{**deal(4, -1.0), **bad}]
    result = run(fake)
    assert result["daily_risk"]["status"] == "unverified" and result["status"] == UNVERIFIED
    assert "deals" in check_ids(result, "unverified")


def test_unclassified_deal_without_amount_is_ignored() -> None:
    fake = make_fake()
    fake.deals = [deal(13, 0.0)]
    assert run(fake)["daily_risk"]["status"] == "verified"


def test_daily_allowance_after_cost_deals() -> None:
    fake = make_fake()
    fake.deals = [deal(4, -10.0), deal(17, -6.34)]  # 16.34 + 3.66 = 20.00 exactly
    result = run(fake)
    assert result["status"] == APPROVED
    assert (
        result["daily_risk"]["remaining"] == 3.66 and result["daily_risk"]["remaining_after"] == 0.0
    )
    fake.deals = [deal(4, -10.0), deal(17, -6.35)]
    assert "volume_minimum" in check_ids(run(fake))


# ====================================================================== F3 broker symbols
@pytest.mark.parametrize(
    ("requested", "pair"),
    [("EURUSDc", "EURUSD"), ("GBPUSDc", "GBPUSD"), ("USDJPYc", "USDJPY"), ("eurusdc", "EURUSD")],
)
def test_broker_symbols_map_to_the_pair_and_keep_the_broker_symbol(
    tmp_path, requested, pair
) -> None:
    fake, stub, _, api = _api(tmp_path, env=envelope(level=BUY_LEVEL, symbol=pair))
    status, body, _, _ = _call(api, "/api/risk/proposal", f"symbol={requested}&timeframe=H1")
    assert status == "200 OK"
    assert (body["symbol"], body["requested_symbol"]) == (pair, requested)
    assert body["broker_symbol"] == f"{pair}c"
    assert stub.calls == [(pair, "H1")]  # T6 runs on the pair


def test_canonical_symbols_still_work(tmp_path) -> None:
    _, _, _, api = _api(tmp_path)
    _, body, _, _ = _call(api, "/api/risk/proposal", "symbol=EURUSD&timeframe=H1")
    assert (body["symbol"], body["requested_symbol"], body["broker_symbol"]) == (
        "EURUSD",
        "EURUSD",
        "EURUSDc",
    )


@pytest.mark.parametrize("symbol", ["EURGBPc", "XAUUSDc", "EUR", "USDCHFc"])
def test_unsupported_broker_symbols_reject_before_mt5(tmp_path, symbol) -> None:
    fake, stub, _, api = _api(tmp_path)
    status, body, _, _ = _call(api, "/api/risk/proposal", f"symbol={symbol}&timeframe=H1")
    assert status == "400 Bad Request" and fake.calls == [] and stub.calls == []


@pytest.mark.parametrize("requested", ["EURUSDm", "EURUSD.a", "EURUSDcx"])
def test_no_accidental_symbol_substitution(requested) -> None:
    result = run(requested=requested)
    assert result["status"] == REJECTED and "symbol_resolution" in check_ids(result)
    assert result["proposal"] is None


def test_broker_symbol_is_used_for_quotes_and_calculations() -> None:
    fake = make_fake()
    run(fake, requested="EURUSDc")
    assert "symbol_info_tick" in fake.calls and "order_calc_margin" in fake.calls


# ====================================================================== F4 daily capital
def _account(fake, *, balance, equity):
    fake.account.update(balance=balance, equity=equity, profit=equity - balance, margin_free=equity)


@pytest.mark.parametrize(
    ("balance", "equity", "limit", "source"),
    [
        (900.0, 1000.0, 18.0, "balance"),
        (1000.0, 900.0, 18.0, "equity"),
        (1000.0, 1000.0, 20.0, "balance"),
    ],
)
def test_daily_reference_is_min_of_balance_and_equity(balance, equity, limit, source) -> None:
    fake = make_fake()
    _account(fake, balance=balance, equity=equity)
    result = run(fake)
    daily = result["daily_risk"]
    assert daily["reference_capital"] == {
        "basis": "min(balance, equity)",
        "value": min(balance, equity),
        "from": source,
    }
    assert daily["limit"] == limit
    assert result["sizing"]["max_risk_per_trade"] == equity * 0.005  # sizing stays on equity


@pytest.mark.parametrize("problem", ["missing", "nan"])
def test_unverified_balance_makes_daily_risk_unverified(problem) -> None:
    fake = make_fake()
    if problem == "missing":
        fake.account_missing.add("balance")
    else:
        fake.account["balance"] = float("nan")
    result = run(fake)
    assert result["daily_risk"]["status"] == "unverified" and result["daily_risk"]["limit"] is None
    assert result["status"] == UNVERIFIED


def test_unverified_equity_or_stale_account_is_unverified() -> None:
    fake = make_fake()
    fake.account_missing.add("equity")
    assert run(fake)["status"] == UNVERIFIED
    fake = make_fake()
    _, account, risk = stack(fake, StubAnalysis(envelope(level=BUY_LEVEL)), costs=VERIFIED_COSTS)
    account.summary()
    account._fetched_at = account._fetched_at.replace(year=2000)
    fake.running = False  # the account answers CACHED
    result = risk.proposal("EURUSD", "H1")
    assert result["status"] == UNVERIFIED and result["daily_risk"]["status"] == "unverified"


def test_floating_profit_does_not_raise_the_daily_limit() -> None:
    fake = make_fake()
    _account(fake, balance=1000.0, equity=1500.0)
    assert run(fake)["daily_risk"]["limit"] == 20.0  # 2% of the balance, not of 1500


# ====================================================================== F4 stop-limit orders
def _stop_limit(kind, trigger, limit, sl=None, **extra):
    order = {
        "symbol": "EURUSDc",
        "type": kind,
        "volume_current": 0.01,
        "price_open": trigger,
        "sl": sl,
        "tp": 0.0,
        "time_setup": 0,
        **extra,
    }
    if limit is not None:
        order["price_stoplimit"] = limit
    return order


@pytest.mark.parametrize(
    ("kind", "trigger", "limit", "sl", "at_trigger", "at_limit"),
    [
        (6, 1.2500, 1.2480, 1.2450, 5.0, 3.0),  # BUY stop-limit: limit below trigger
        (6, 1.2480, 1.2500, 1.2450, 3.0, 5.0),  # limit above trigger: the limit is worse
        (7, 1.2000, 1.2020, 1.2050, 5.0, 3.0),  # SELL stop-limit
        (7, 1.2020, 1.2000, 1.2050, 3.0, 5.0),
    ],
)
def test_stop_limit_risk_is_the_larger_of_trigger_and_limit(
    kind, trigger, limit, sl, at_trigger, at_limit
) -> None:
    fake = make_fake()
    fake.orders = [_stop_limit(kind, trigger, limit, sl)]
    live, _, _ = stack(fake, StubAnalysis())
    risk = live.exposure_read(_day())["value"]["orders"][0]["risk_to_stop"]
    assert risk["risk_at_trigger"] == pytest.approx(at_trigger)
    assert risk["risk_at_limit"] == pytest.approx(at_limit)
    assert risk["amount"] == pytest.approx(max(at_trigger, at_limit))
    result = run(fake)
    assert result["daily_risk"]["pending_order_risk"] == pytest.approx(5.0)


@pytest.mark.parametrize("limit", [None, 0.0, -1.0, float("nan")])
def test_stop_limit_without_a_usable_limit_price_is_unverified(limit) -> None:
    fake = make_fake()
    fake.orders = [_stop_limit(6, 1.25, limit, 1.245)]
    result = run(fake)
    assert result["status"] == UNVERIFIED and "open_risk" in check_ids(result, "unverified")


def test_stop_limit_without_stop_loss_is_unbounded() -> None:
    fake = make_fake()
    fake.orders = [_stop_limit(7, 1.20, 1.202, 0.0)]
    result = run(fake)
    assert result["status"] == REJECTED and "unbounded" in result["rejection_reasons"][0]["reason"]


def test_invalid_pending_order_type_is_unverified() -> None:
    fake = make_fake()
    fake.orders = [_stop_limit(9, 1.25, 1.248, 1.245)]
    assert "open_risk" in check_ids(run(fake), "unverified")


def test_ordinary_pending_orders_are_unchanged() -> None:
    fake = make_fake()
    fake.orders = [
        {
            "symbol": "EURUSDc",
            "type": 2,
            "volume_current": 0.01,
            "price_open": 1.25,
            "sl": 1.245,
            "tp": 0.0,
            "time_setup": 0,
            "price_stoplimit": 1.30,
        }
    ]
    live, _, _ = stack(fake, StubAnalysis())
    risk = live.exposure_read(_day())["value"]["orders"][0]["risk_to_stop"]
    assert risk["amount"] == pytest.approx(5.0) and "risk_at_limit" not in risk
