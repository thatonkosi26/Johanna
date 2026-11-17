"""Phase 4 risk engine: sizing, policy, daily risk, construction, broker and margin checks.

Synthetic data only (FakeMT5 + real T6 envelopes reshaped per test). Nothing here places,
modifies or closes an order; FakeMT5 has no order functions at all.
"""

from __future__ import annotations

import ast
import json
import math
import pathlib
import time
from datetime import UTC, datetime, timedelta

import pytest
from fake_mt5 import READ_ONLY_CALLS
from risk_fixtures import (
    BUY_LEVEL,
    REAL_BUY,
    SELL_LEVEL,
    SELL_SYMBOLS,
    VERIFIED_COSTS,
    StubAnalysis,
    check_ids,
    envelope,
    make_fake,
    propose,
    stack,
)

from johanna import risk_engine
from johanna.adapters.mt5 import ServerClock
from johanna.risk_engine import (
    APPROVED,
    REJECTED,
    UNVERIFIED,
    RiskPolicy,
    floor_volume,
    trading_day_start,
)

SRC = pathlib.Path(risk_engine.__file__).parent


def approved(result: dict) -> None:
    assert result["status"] == APPROVED, (result["rejection_reasons"], result["unverified"])


def deal(profit: float, *, commission: float = 0.0, swap: float = 0.0, kind: int = 1, ago=60):
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
        "fee": 0.0,
    }


# ---------------------------------------------------------------- position sizing
def test_eurusd_buy_is_sized_in_usc_and_approved() -> None:
    fake = make_fake()
    result = propose(fake)
    approved(result)
    p, s = result["proposal"], result["sizing"]
    assert result["direction"] == "BUY" and result["broker_symbol"] == "EURUSDc"
    assert (p["entry_price"], p["entry_side"]) == (1.23798, "ask")
    assert p["stop_loss"] == 1.23432  # L_last 1.23455 - 0.25 x ATR, rounded down to the tick
    assert p["take_profit"] == 1.246  # near edge of the TC6 level
    assert p["gross_reward_risk"] == pytest.approx(802 / 366, abs=1e-3)
    assert result["gross_reward_risk"] == p["gross_reward_risk"]
    assert result["net_reward_risk"]["status"] == "estimated"
    # 366 points x 1 USC per point per lot (contract 1000, USD profit, cent account)
    assert s["loss_per_lot"] == {
        "tick_value_method": 366.0,
        "contract_size_method": 366.0,
        "contract_size_at_stop_price": 366.0,
        "price_component": 366.0,
        "commission": 0.0,
        "swap": 0.0,
        "slippage": 0.0,
        "costs_total": 0.0,
        "total": 366.0,
    }
    assert s["max_risk_per_trade"] == 5.0  # 0.5% of equity 1000 USC
    assert s["theoretical_volume"] == pytest.approx(5 / 366, abs=1e-6)
    assert s["volume"] == 0.01 and s["estimated_loss"] == 3.66
    assert s["estimated_reward_gross"] == s["estimated_reward_net"] == 8.02  # zero synthetic costs
    assert s["mt5_calculator"] == {"loss_at_stop": 3.66, "reward_at_target": 8.02}
    assert result["margin"]["mt5_calculator"] == pytest.approx(0.619, abs=1e-3)
    assert set(fake.calls) <= READ_ONLY_CALLS
    json.dumps(result, allow_nan=False)


def test_gbpusd_sell_uses_the_bid_and_mirrored_levels() -> None:
    fake = make_fake(SELL_SYMBOLS)
    result = propose(fake, envelope("bearish", level=SELL_LEVEL), symbol="GBPUSD")
    approved(result)
    p = result["proposal"]
    assert result["direction"] == "SELL" and result["broker_symbol"] == "GBPUSDc"
    assert (p["entry_price"], p["entry_side"]) == (0.962, "bid")
    assert p["stop_loss"] == 0.96568  # H_last 0.96545 + 0.25 ATR, rounded up
    assert p["take_profit"] == 0.954  # near (upper) edge of the support level
    assert p["stop_loss"] > p["entry_price"] > p["take_profit"]
    assert "trigger on the ask" in p["triggers"]


def test_usdjpy_uses_price_dependent_tick_values_and_the_conservative_figure() -> None:
    fake = make_fake()
    fake.account.update(balance=10_000.0, equity=10_000.0, margin_free=10_000.0)
    env = envelope(level=(161.000, 161.100), pivot=158.300, atr=0.32, symbol="USDJPY")
    result = propose(fake, env, symbol="USDJPY")
    approved(result)
    s = result["sizing"]
    risk_distance = 159.129 - 158.219  # SL = 158.300 - 0.08 = 158.220 -> floor 158.22
    assert result["proposal"]["stop_loss"] == 158.22
    loss_tick = risk_distance / 0.001 * (100 / 159.119)  # tick_value_loss uses the bid
    assert s["loss_per_lot"]["tick_value_method"] == pytest.approx(
        (159.129 - 158.22) / 0.001 * 100 / 159.119, rel=1e-6
    )
    assert s["loss_per_lot"]["contract_size_at_stop_price"] == pytest.approx(
        (159.129 - 158.22) * 1000 * 100 / 158.22, rel=1e-6
    )
    # the stop-loss price converts JPY at a lower USDJPY: the larger, conservative figure
    assert s["loss_per_lot"]["price_component"] == s["loss_per_lot"]["contract_size_at_stop_price"]
    assert loss_tick > 0 and "JPY" in s["conversion"]


@pytest.mark.parametrize(
    ("equity", "volume"),
    [(1000.0, 0.01), (10_000.0, 0.13), (100_000.0, 1.36), (73_200.0, 1.0)],
)
def test_volume_is_rounded_down_for_different_budgets(equity, volume) -> None:
    fake = make_fake()
    fake.account.update(balance=equity, equity=equity, margin_free=equity)
    result = propose(fake)
    approved(result)
    assert result["sizing"]["volume"] == volume
    assert result["sizing"]["estimated_loss"] <= equity * 0.005 + 1e-9


def test_different_stop_distances_change_the_volume() -> None:
    fake = make_fake()
    fake.account.update(balance=100_000.0, equity=100_000.0, margin_free=100_000.0)
    near = propose(fake, envelope(level=(1.2430, 1.2440), pivot=1.2360, atr=0.0004))
    far = propose(fake, envelope(level=(1.2600, 1.2610), pivot=1.2320, atr=0.0004))
    approved(near)
    approved(far)
    assert near["proposal"]["stop_loss_distance"] < far["proposal"]["stop_loss_distance"]
    assert near["sizing"]["volume"] > far["sizing"]["volume"]


def test_below_minimum_volume_is_rejected_not_rounded_up() -> None:
    fake = make_fake()
    fake.account.update(balance=500.0, equity=500.0, margin_free=500.0)
    result = propose(fake)
    assert result["status"] == REJECTED
    assert "volume_minimum" in check_ids(result)
    assert result["sizing"]["volume"] is None and result["sizing"]["estimated_loss"] is None


def test_above_maximum_volume_is_capped_with_a_warning() -> None:
    fake = make_fake()
    big = 2_000_000_000.0
    fake.account.update(balance=big, equity=big, margin_free=big)
    result = propose(fake)
    approved(result)
    assert result["sizing"]["volume"] == 200.0
    assert result["sizing"]["volume_capped_at_maximum"] is True
    assert any("broker maximum" in w for w in result["warnings"])


def test_volume_step_violation_fails_closed() -> None:
    fake = make_fake()
    fake.symbols["EURUSDc"]["info"] = {"volume_step": 0.03}  # minimum 0.01 is not a whole step
    result = propose(fake)
    assert result["status"] == UNVERIFIED
    assert "instrument_verified" in check_ids(result, "unverified")


@pytest.mark.parametrize(
    ("theoretical", "expected"),
    [(0.0137, 0.01), (0.3, 0.3), (0.29999999, 0.29), (1.999, 1.99), (0.0099, 0.0), (2.0, 2.0)],
)
def test_floor_volume(theoretical, expected) -> None:
    assert floor_volume(theoretical, 0.01, 0.01) == expected


def test_floor_volume_respects_a_minimum_offset_grid() -> None:
    assert floor_volume(0.27, 0.1, 0.05) == 0.25
    assert floor_volume(0.09, 0.1, 0.05) == 0.0


# ---------------------------------------------------------------- risk policy
def test_exactly_at_the_per_trade_limit_is_approved() -> None:
    fake = make_fake()
    fake.account.update(balance=732.0, equity=732.0, margin_free=732.0)  # 0.5% = 3.66
    result = propose(fake)
    approved(result)
    assert result["sizing"]["estimated_loss"] == result["sizing"]["max_risk_per_trade"] == 3.66


def test_slightly_above_the_per_trade_limit_is_rejected() -> None:
    fake = make_fake()
    fake.account.update(balance=731.9, equity=731.9, margin_free=731.9)  # 0.01 lot = 0.5001%
    result = propose(fake)
    assert result["status"] == REJECTED and "volume_minimum" in check_ids(result)


def test_exactly_at_the_daily_limit_is_approved() -> None:
    fake = make_fake()
    fake.deals = [deal(-16.34)]  # 16.34 + 3.66 = 20.00 = 2% of 1000
    result = propose(fake)
    approved(result)
    daily = result["daily_risk"]
    assert daily["realised_losses"] == 16.34 and daily["limit"] == 20.0
    assert daily["remaining"] == 3.66 and daily["remaining_after"] == 0.0
    assert result["sizing"]["risk_budget_basis"] == "remaining daily allowance"


def test_daily_limit_exceeded_is_rejected() -> None:
    fake = make_fake()
    fake.deals = [deal(-16.35)]  # only 3.65 left: below the 0.01-lot loss of 3.66
    result = propose(fake)
    assert result["status"] == REJECTED and "volume_minimum" in check_ids(result)
    fake.deals = [deal(-25.0)]
    result = propose(fake)
    assert result["status"] == REJECTED and "allowance" in check_ids(result)


def test_realised_losses_are_gross_and_exclude_non_trade_deals() -> None:
    fake = make_fake()
    fake.deals = [
        deal(-4.0, commission=-1.0),  # a 5.00 loss
        deal(10.0),  # gains never add allowance
        deal(500.0, kind=2),  # a balance operation, not a trade
        deal(-3.0, ago=3 * 86400),  # an earlier trading day
    ]
    result = propose(fake)
    approved(result)
    assert result["daily_risk"]["realised_losses"] == 5.0
    assert result["daily_risk"]["deals_today"] == 3


def test_open_position_and_pending_order_risk_are_counted() -> None:
    fake = make_fake()
    fake.positions = [
        {
            "symbol": "EURUSDc",
            "type": 0,
            "volume": 0.02,
            "price_open": 1.2400,
            "sl": 1.2350,
            "tp": 0.0,
            "price_current": 1.2379,
            "profit": -4.2,
            "swap": 0.0,
            "time": 0,
        },
    ]
    fake.orders = [
        {
            "symbol": "GBPUSDc",
            "type": 3,
            "volume_current": 0.01,
            "price_open": 1.3200,
            "sl": 1.3250,
            "tp": 1.3100,
            "time_setup": 0,
        },
    ]
    result = propose(fake)
    approved(result)
    daily = result["daily_risk"]
    assert daily["open_position_risk"] == pytest.approx(10.0)  # 0.0050 x 1000 x 0.02 x 100
    assert daily["pending_order_risk"] == pytest.approx(5.0)
    assert daily["used"] == pytest.approx(15.0) and daily["remaining"] == pytest.approx(5.0)


@pytest.mark.parametrize("kind", ["position", "order"])
def test_exposure_without_stop_loss_is_unbounded_and_rejected(kind) -> None:
    fake = make_fake()
    if kind == "position":
        fake.positions = [
            {
                "symbol": "EURUSDc",
                "type": 1,
                "volume": 0.01,
                "price_open": 1.2,
                "sl": 0.0,
                "tp": 0.0,
                "price_current": 1.2,
                "profit": 0.0,
                "swap": 0.0,
                "time": 0,
            }
        ]
    else:
        fake.orders = [
            {
                "symbol": "EURUSDc",
                "type": 2,
                "volume_current": 0.01,
                "price_open": 1.2,
                "sl": 0.0,
                "tp": 0.0,
                "time_setup": 0,
            }
        ]
    result = propose(fake)
    assert result["status"] == REJECTED and "open_risk" in check_ids(result)
    assert "unbounded" in result["rejection_reasons"][0]["reason"]


@pytest.mark.parametrize("read", ["positions_get", "orders_get", "history_deals_get"])
def test_missing_exposure_is_unverified(read) -> None:
    fake = make_fake()
    fake.failing_reads.add(read)
    result = propose(fake)
    assert result["status"] == UNVERIFIED
    assert result["daily_risk"]["status"] == "unverified"
    assert "exposure" in check_ids(result, "unverified")


def test_deal_history_range_is_the_broker_server_day() -> None:
    fake = make_fake()
    propose(fake)
    start, end = fake.deal_ranges[-1]
    today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    assert start == int(today.timestamp()) and end >= start


def test_trading_day_start_follows_the_server_clock() -> None:
    now = datetime(2026, 11, 17, 1, 30, tzinfo=UTC)
    assert trading_day_start(now, ServerClock()) == datetime(2026, 11, 17, tzinfo=UTC)
    # a server two hours ahead of UTC starts its day at 22:00 UTC the evening before
    shifted = ServerClock("UTC", 2)
    assert trading_day_start(now, shifted) == datetime(2026, 11, 16, 22, tzinfo=UTC)


# ---------------------------------------------------------------- trade construction
def test_stop_loss_on_the_wrong_side_is_rejected() -> None:
    fake = make_fake()
    fake.symbols["EURUSDc"].update(bid=1.23400, ask=1.23408)  # price fell through the swing
    result = propose(fake)
    assert result["status"] == REJECTED and "stop_direction" in check_ids(result)


def test_take_profit_on_the_wrong_side_is_rejected() -> None:
    result = propose(env=envelope(level=(1.2300, 1.2310)))  # "resistance" below the entry
    assert result["status"] == REJECTED and "target_direction" in check_ids(result)


def test_reward_risk_below_two_is_rejected() -> None:
    result = propose(env=envelope(level=(1.2450, 1.2460)))  # 702 / 366 = 1.92
    assert result["status"] == REJECTED and "reward_risk" in check_ids(result)
    assert result["proposal"]["gross_reward_risk"] == pytest.approx(702 / 366, abs=1e-3)


def test_prices_respect_tick_size_and_digits() -> None:
    env = envelope(level=(1.246004, 1.24700), pivot=1.2345567, atr=0.000888888)
    result = propose(env=env)
    approved(result)
    p = result["proposal"]
    for price in (p["entry_price"], p["stop_loss"], p["take_profit"]):
        assert round(price, 5) == price
    assert p["stop_loss"] == 1.23433  # 1.2345567 - 0.000222222 = 1.2343345 -> down
    assert p["take_profit"] == 1.246  # 1.246004 -> down (towards the entry)
    fake = make_fake()
    fake.account.update(balance=10_000.0, equity=10_000.0, margin_free=10_000.0)
    usd_jpy = propose(
        fake,
        envelope(level=(161.0004, 161.1), pivot=158.3006, atr=0.32, symbol="USDJPY"),
        symbol="USDJPY",
    )
    approved(usd_jpy)
    assert usd_jpy["proposal"]["stop_loss"] == 158.22  # 158.2206 -> down to the 0.001 tick
    assert usd_jpy["proposal"]["take_profit"] == 161.0


def test_spread_is_included_once_through_execution_sides() -> None:
    result = propose()
    p, costs = result["proposal"], result["costs"]
    assert p["stop_loss_distance"] == pytest.approx(1.23798 - 1.23432)  # from the ask
    assert costs["spread"]["points"] == 8.0 and costs["spread"]["per_lot"] == pytest.approx(8.0)
    assert "included once" in costs["spread"]["treatment"]
    assert costs["commission"]["status"] == "verified"  # synthetic fixture terms
    assert costs["slippage"]["status"] == "bounded"  # synthetic fixture bound


def test_missing_technical_structure_is_rejected() -> None:
    no_level = propose(env=envelope())  # the real R7: no qualifying resistance -> no target
    assert no_level["status"] == REJECTED and "target_structure" in check_ids(no_level)
    env = envelope(level=BUY_LEVEL)
    env["result"]["features"]["swings"]["pivots"] = [
        p for p in env["result"]["features"]["swings"]["pivots"] if p["role"] != "L_last"
    ]
    assert "stop_structure" in check_ids(propose(env=env))
    env = envelope(level=BUY_LEVEL)
    env["result"]["features"]["latest"]["ATR14"]["usable"] = False
    assert "atr" in check_ids(propose(env=env))


def test_stale_quote_is_rejected() -> None:
    fake = make_fake(tick_age_seconds=30)
    result = propose(fake)
    assert result["status"] == REJECTED and "quote_fresh" in check_ids(result)


def test_no_t6_candidate_is_rejected_and_unavailable_analysis_is_unverified() -> None:
    env = envelope(level=BUY_LEVEL)
    env["result"]["decision"].update(outcome="MARKET_CONDITION", rule="R6")
    result = propose(env=env)
    assert result["status"] == REJECTED and "t6_candidate" in check_ids(result)
    assert result["proposal"] is None and result["daily_risk"]["status"] == "verified"
    fake = make_fake()
    _, _, risk = stack(fake, StubAnalysis(error=RuntimeError("snapshot unavailable")))
    result = risk.proposal("EURUSD", "H1")
    assert result["status"] == UNVERIFIED and "analysis" in check_ids(result, "unverified")


def test_stale_analysis_is_unverified() -> None:
    env = envelope(level=BUY_LEVEL, evaluated_at=datetime.now(UTC) - timedelta(minutes=2))
    result = propose(env=env)
    assert result["status"] == UNVERIFIED and "analysis_fresh" in check_ids(result, "unverified")


# ---------------------------------------------------------------- account and broker
def test_missing_equity_is_unverified() -> None:
    fake = make_fake()
    fake.account_missing.add("equity")
    result = propose(fake)
    assert result["status"] == UNVERIFIED and "account_fields" in check_ids(result, "unverified")


def test_invalid_account_currency_is_unverified() -> None:
    fake = make_fake()
    fake.account["currency"] = "usc"
    result = propose(fake)
    assert result["status"] == UNVERIFIED and result["sizing"] is None


def test_stale_cached_account_information_is_unverified() -> None:
    fake = make_fake()
    _, account, risk = stack(fake, StubAnalysis(envelope(level=BUY_LEVEL)))
    account.summary()  # a good snapshot exists
    account._fetched_at -= timedelta(minutes=1)
    fake.running = False  # MT5 away: account answers CACHED
    result = risk.proposal("EURUSD", "H1")
    assert result["status"] == UNVERIFIED and "account_live" in check_ids(result, "unverified")


def test_incorrect_contract_size_fails_closed() -> None:
    fake = make_fake()
    fake.symbols["EURUSDc"]["info"] = {"trade_contract_size": 100_000.0}  # standard-lot size
    result = propose(fake)
    assert result["status"] == UNVERIFIED  # Phase 3 tick-value reconciliation flags it
    assert "instrument_verified" in check_ids(result, "unverified")


def test_methods_disagreeing_inside_the_engine_reject() -> None:
    fake = make_fake()
    live, account, _ = stack(fake, StubAnalysis())
    instrument = account.instrument("EURUSD")
    instrument["instrument"]["fields"]["contract_size"]["value"] = 100_000.0
    inputs = risk_engine.RiskInputs(
        symbol="EURUSD",
        timeframe="H1",
        analysis=envelope(level=BUY_LEVEL),
        analysis_error=None,
        account=account.summary(),
        instrument=instrument,
        exposure={"status": "available", **live.exposure_read(_day())},
        evaluated_at=datetime.now(UTC),
        day_start_utc=_day(),
    )
    result = risk_engine.finalise(risk_engine.plan_trade(inputs), None)
    assert result["status"] == REJECTED and "methods_agree" in check_ids(result)


def _day() -> datetime:
    return datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def test_missing_tick_values_are_unverified() -> None:
    fake = make_fake()
    fake.symbols["EURUSDc"]["info_missing"] = ["trade_tick_value_loss"]
    result = propose(fake)
    assert result["status"] == UNVERIFIED


def test_no_conversion_path_is_unverified() -> None:
    fake = make_fake()
    live, account, _ = stack(fake, StubAnalysis())
    instrument = account.instrument("EURUSD")
    fields = instrument["instrument"]["fields"]
    fields["currency_profit"]["value"], fields["currency_base"]["value"] = "GBP", "EUR"
    inputs = risk_engine.RiskInputs(
        symbol="EURUSD",
        timeframe="H1",
        analysis=envelope(level=BUY_LEVEL),
        analysis_error=None,
        account=account.summary(),
        instrument=instrument,
        exposure={"status": "available", **live.exposure_read(_day())},
        evaluated_at=datetime.now(UTC),
        day_start_utc=_day(),
    )
    result = risk_engine.finalise(risk_engine.plan_trade(inputs), None)
    assert result["status"] == UNVERIFIED and "conversion" in check_ids(result, "unverified")


def test_invalid_conversion_price_is_unverified() -> None:
    fake = make_fake()
    fake.symbols["USDJPYc"]["loading"] = True  # no real tick: no price to convert JPY with
    env = envelope(level=(161.000, 161.100), pivot=158.300, atr=0.32, symbol="USDJPY")
    result = propose(fake, env, symbol="USDJPY")
    # no price: Phase 3 cannot reconcile the JPY tick value, so the instrument is unverified
    assert result["status"] == UNVERIFIED
    assert "instrument_verified" in check_ids(result, "unverified")
    assert result["sizing"] is None


def test_insufficient_free_margin_is_rejected() -> None:
    fake = make_fake()
    fake.account["leverage"] = 1  # 0.01 lot needs 1238 USC of margin; 1000 free
    result = propose(fake)
    assert result["status"] == REJECTED and "free_margin" in check_ids(result)


def test_margin_that_cannot_be_reconciled_is_unverified() -> None:
    fake = make_fake()
    fake.calc_hook = lambda name, args, value: value * 1.5 if name == "margin" else value
    result = propose(fake)
    assert result["status"] == UNVERIFIED
    assert "margin_reconciliation" in check_ids(result, "unverified")


def test_calculator_results_rounded_to_currency_digits_still_reconcile() -> None:
    # Real MT5 rounds calculator results to the account currency's 2 digits (observed live).
    fake = make_fake()
    fake.calc_hook = lambda name, args, value: round(value, 2)
    fake.account["leverage"] = 20_000  # margin 0.0619 -> 0.06: 3% off, inside half a cent
    result = propose(fake)
    approved(result)
    assert result["margin"]["mt5_calculator"] == 0.06
    fake.calc_hook = lambda name, args, value: round(value, 2) + (0.01 if name == "margin" else 0)
    assert propose(fake)["status"] == UNVERIFIED  # a whole cent off is not rounding


def test_profit_calculator_disagreement_is_rejected() -> None:
    fake = make_fake()
    fake.calc_hook = lambda name, args, value: value * 1.2 if name == "profit" else value
    result = propose(fake)
    assert result["status"] == REJECTED and "mt5_profit_calculator" in check_ids(result)


def test_calculators_unavailable_is_unverified() -> None:
    fake = make_fake()
    # verified (synthetic) costs, so a volume exists and the calculators would be asked
    _, _, risk = stack(fake, StubAnalysis(envelope(level=BUY_LEVEL)), costs=VERIFIED_COSTS)

    def broken(requests):
        raise risk_engine_unavailable()

    risk.live_market.risk_calculations = broken
    result = risk.proposal("EURUSD", "H1")
    assert result["status"] == UNVERIFIED
    assert {"mt5_profit_calculator", "mt5_margin_calculator"} <= set(
        check_ids(result, "unverified")
    )


def risk_engine_unavailable():
    from johanna.live_market import MarketUnavailable

    return MarketUnavailable("timeout", "synthetic")


def test_symbol_and_account_permissions_are_enforced() -> None:
    fake = make_fake()
    fake.symbols["EURUSDc"]["info"] = {"trade_mode": 3}  # close only
    assert "symbol_trade_mode" in check_ids(propose(fake))
    fake = make_fake()
    fake.account["trade_allowed"] = False
    assert "account_trading" in check_ids(propose(fake))


def test_stops_level_is_enforced() -> None:
    fake = make_fake()
    fake.symbols["EURUSDc"]["info"] = {"trade_stops_level": 500}  # SL is 358 points away
    result = propose(fake)
    assert result["status"] == REJECTED and "stops_level" in check_ids(result)


def test_zero_stops_level_is_warned_about() -> None:
    assert any("stops level 0" in w for w in propose()["warnings"])


# ---------------------------------------------------------------- policy
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_risk_per_trade": 0.06},
        {"max_risk_per_trade": 0.0},
        {"max_daily_risk": 0.004},  # below the per-trade limit
        {"min_reward_risk": 0.5},
        {"max_risk_per_trade": math.nan},
        {"execution": "automatic"},
        {"mt5_access": "read_write"},
    ],
)
def test_invalid_policies_are_refused(kwargs) -> None:
    with pytest.raises(ValueError):
        RiskPolicy(**kwargs)


def test_confirmed_policy_values() -> None:
    policy = risk_engine.RISK_POLICY
    assert (policy.max_risk_per_trade, policy.max_daily_risk, policy.min_reward_risk) == (
        0.005,
        0.02,
        2.0,
    )
    assert (policy.execution, policy.mt5_access) == ("manual_only", "read_only")
    with pytest.raises(AttributeError):
        policy.max_risk_per_trade = 0.05  # frozen


def test_a_rejection_or_unverified_check_can_never_be_approved() -> None:
    r = risk_engine._Result(
        risk_engine.RiskInputs("EURUSD", "H1", None, None, {}, None, {}, datetime.now(UTC), _day())
    )
    for area in r.data["validations"]:
        r.check(area, "x", "passed", "ok")
    assert r.status() == APPROVED
    r.check("margin", "y", "unverified", "no calculator")
    assert r.status() == UNVERIFIED
    r.check("broker", "z", "failed", "no")
    assert r.status() == REJECTED


# ---------------------------------------------------------------- safety
FORBIDDEN_CALLS = ("order_send", "order_check", "positions_close", "order_modify", "order_delete")


def test_no_order_function_exists_or_is_called() -> None:
    fake = make_fake()
    fake.positions = [
        {
            "symbol": "EURUSDc",
            "type": 0,
            "volume": 0.01,
            "price_open": 1.24,
            "sl": 1.235,
            "tp": 0.0,
            "price_current": 1.2379,
            "profit": 0.0,
            "swap": 0.0,
            "time": 0,
        }
    ]
    propose(fake)
    assert set(fake.calls) <= READ_ONLY_CALLS
    for name in FORBIDDEN_CALLS:
        assert not hasattr(fake, name)  # an accidental call would raise AttributeError
    for path in SRC.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for name in FORBIDDEN_CALLS:
            assert name not in text, f"{name} referenced in {path.name}"


def test_risk_modules_have_no_io_clock_or_providers() -> None:
    engine = (SRC / "risk_engine.py").read_text(encoding="utf-8")
    tree = ast.parse(engine)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in (
            node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")]
        )
    }
    assert imported <= {
        "__future__",
        "math",
        "collections",
        "dataclasses",
        "datetime",
        "decimal",
        "typing",
    }
    assert ".now(" not in engine and "open(" not in engine
    for name in ("risk_engine.py", "risk_service.py"):
        text = (SRC / name).read_text(encoding="utf-8")
        for token in (
            "urllib",
            "import requests",
            "http.client",
            "socket",
            "Path(",
            "data/",
            "twelve",
            "fred",
            "gemini",
        ):
            assert token not in text, f"{token} in {name}"


def test_exposure_payload_has_no_tickets_or_comments() -> None:
    fake = make_fake()
    fake.positions = [
        {
            "symbol": "EURUSDc",
            "type": 0,
            "volume": 0.01,
            "price_open": 1.24,
            "sl": 1.235,
            "tp": 0.0,
            "price_current": 1.2379,
            "profit": 0.0,
            "swap": 0.0,
            "time": 0,
        }
    ]
    fake.deals = [deal(-1.0)]
    live, _, _ = stack(fake, StubAnalysis())
    text = json.dumps(live.exposure_read(_day())["value"], default=str)
    for secret in ("900001", "900003", "900004", "900005", "private", "ticket", "comment", "login"):
        assert secret not in text


def test_proposal_is_never_executed_and_says_so() -> None:
    result = propose()
    assert "has not placed and will not place" in result["note"]
    assert result["proposal"]["label"] == "Proposed trade parameters (not an order)"
    assert result["policy"]["execution"] == "manual_only"


def test_real_envelope_is_not_modified() -> None:
    before = json.dumps(REAL_BUY, sort_keys=True, default=str)
    propose(env=envelope(level=BUY_LEVEL))
    assert json.dumps(REAL_BUY, sort_keys=True, default=str) == before
