"""Final F1 safeguards: the slippage approval gate and cost-inclusive position sizing.

Synthetic data only (FakeMT5, real T6 envelopes reshaped per test, synthetic cost terms).
"""

from __future__ import annotations

import pytest
from risk_fixtures import (
    BUY_LEVEL,
    PRODUCTION_COSTS,
    StubAnalysis,
    check_ids,
    envelope,
    make_fake,
    negative_swap,
)
from test_risk_api import _api, _call
from test_risk_corrections import deal, fixed_rollovers, run, terms

from johanna import risk_engine
from johanna.risk_engine import APPROVED, REJECTED, UNVERIFIED


def rich(equity: float = 100_000.0):
    # the swap safeguards in this file need the long swap to be a cost (fake default: zero)
    fake = negative_swap(make_fake())
    fake.account.update(balance=equity, equity=equity, margin_free=equity)
    return fake


# ====================================================================== A. slippage gate
def test_established_commission_and_swap_with_unmodelled_slippage_cannot_be_approved() -> None:
    result = run(costs=terms(commission=0.0, holding=0, slippage=None))
    assert result["status"] == UNVERIFIED
    assert result["costs"]["commission"]["status"] == "verified"
    assert result["costs"]["swap"]["status"] == "established"
    assert result["costs"]["slippage"]["status"] == "unmodelled"
    assert result["net_reward_risk"] == {
        "status": "unverified",
        "value": None,
        "assumptions": result["costs"]["assumptions"],
    }
    reason = next(u["reason"] for u in result["unverified"] if u["area"] == "costs")
    assert "slippage unmodelled (no slippage evidence)" in reason


def test_a_high_gross_ratio_cannot_override_the_slippage_gate() -> None:
    env = envelope(level=(1.2600, 1.2610))  # gross ~6
    result = run(env=env, costs=terms(holding=0, slippage=None))
    assert result["gross_reward_risk"] > 5 and result["status"] == UNVERIFIED


def test_a_supplied_slippage_bound_can_reach_the_approval_branch() -> None:
    result = run(costs=terms(commission=0.0, holding=0, slippage=2.0))  # 2 points per side
    slippage = result["costs"]["slippage"]
    assert slippage["status"] == "bounded" and slippage["per_lot"] == pytest.approx(4.0)
    assert result["net_reward_risk"]["status"] == "estimated"
    assert result["net_reward_risk"]["value"] == pytest.approx((802 - 4) / (366 + 4), abs=1e-3)
    assert result["status"] == APPROVED


def test_missing_slippage_is_never_zero() -> None:
    result = run(rich(), costs=terms(holding=0, slippage=None))
    sizing = result["sizing"]
    assert result["costs"]["slippage"]["per_lot"] is None
    assert sizing["loss_per_lot"]["slippage"] is None and sizing["loss_per_lot"]["total"] is None
    assert sizing["volume"] is None and sizing["estimated_loss"] is None
    assert result["costs"]["per_lot_total"] is None


@pytest.mark.parametrize(
    "change",
    [{"source_reference": ""}, {"value": -1.0}, {"value": float("inf")}],
)
def test_slippage_bounds_are_validated(change) -> None:
    from johanna.cost_evidence import CostComponent  # noqa: PLC0415

    slippage = terms().components[1]
    with pytest.raises(ValueError):
        CostComponent(**{**slippage.__dict__, **change})


def test_api_shows_the_unverified_state(tmp_path) -> None:
    _, _, _, api = _api(tmp_path, costs=terms(holding=0, slippage=None))
    status, body, text, _ = _call(api, "/api/risk/proposal", "symbol=EURUSDc&timeframe=H1")
    assert status == "200 OK" and body["status"] == UNVERIFIED
    assert body["net_reward_risk"]["status"] == "unverified"
    assert body["costs"]["slippage"]["status"] == "unmodelled"
    assert body["sizing"]["status"] == "unverified" and body["sizing"]["volume"] is None
    illustrative = body["sizing"]["illustrative_volume_price_risk_only"]
    assert illustrative["value"] == 0.01 and "not actionable" in illustrative["label"]
    assert "NaN" not in text and "Infinity" not in text


def test_existing_cost_gate_behaviour_is_intact() -> None:
    assert run(costs=terms(commission=None, slippage=2.0))["status"] == UNVERIFIED
    no_holding = run(negative_swap(make_fake()), costs=terms(holding=None, slippage=2.0))
    assert no_holding["status"] == UNVERIFIED  # long swap < 0 needs a holding period
    low = run(costs=terms(commission=30.0, holding=0, slippage=0.0))  # net 1.95
    assert low["status"] == REJECTED and "net_reward_risk" in check_ids(low)


# ====================================================================== B. cost-inclusive sizing
def sized(fake=None, **cost_kwargs) -> dict:
    result = run(fake if fake is not None else rich(), costs=terms(**cost_kwargs))
    return result


def test_commission_reduces_the_volume() -> None:
    base = sized(commission=0.0, holding=0)
    with_commission = sized(commission=20.0, holding=0)
    assert base["sizing"]["volume"] == 1.36  # 500 / 366
    assert with_commission["sizing"]["volume"] == 1.29  # 500 / 386
    assert with_commission["sizing"]["loss_per_lot"]["total"] == pytest.approx(386.0)


def test_negative_swap_reduces_the_volume(monkeypatch) -> None:
    monkeypatch.setattr(risk_engine, "rollover_charges", fixed_rollovers(4))
    result = sized(commission=0.0, holding=2)  # Tue + Wed(x3) = 4 x 5.7 = 22.8 per lot
    assert result["sizing"]["loss_per_lot"]["swap"] == pytest.approx(22.8)
    assert result["sizing"]["volume"] == 1.28  # 500 / 388.8


def test_commission_and_swap_are_included_together(monkeypatch) -> None:
    monkeypatch.setattr(risk_engine, "rollover_charges", fixed_rollovers(4))
    result = sized(commission=20.0, holding=2, slippage=1.0)  # + 2 x 1 point slippage
    lpl = result["sizing"]["loss_per_lot"]
    assert (lpl["commission"], lpl["swap"], lpl["slippage"]) == (20.0, pytest.approx(22.8), 2.0)
    assert lpl["total"] == pytest.approx(366 + 20 + 22.8 + 2)
    assert result["sizing"]["volume"] == 1.21  # 500 / 410.8


def test_spread_is_counted_exactly_once() -> None:
    result = sized(commission=20.0, holding=0, slippage=0.0)
    lpl = result["sizing"]["loss_per_lot"]
    assert lpl["price_component"] == 366.0  # ask entry to bid-triggered stop: spread inside
    assert lpl["total"] == pytest.approx(366.0 + 20.0)  # spread (8 per lot) not added again
    assert result["costs"]["spread"]["per_lot"] == pytest.approx(8.0)


def test_positive_swap_never_increases_the_budget_or_volume() -> None:
    fake = rich()
    fake.symbols["EURUSDc"]["info"] = {"swap_long": 4.0}  # a credit for BUY
    credit = sized(fake, commission=0.0, holding=5)
    assert credit["sizing"]["loss_per_lot"]["swap"] == 0.0
    assert credit["sizing"]["risk_budget"] == 500.0 and credit["sizing"]["volume"] == 1.36


def test_volume_is_always_rounded_down() -> None:
    result = sized(commission=18.62, holding=0)  # 500 / 384.62 = 1.29998...
    assert result["sizing"]["theoretical_volume"] == pytest.approx(1.29998, abs=1e-5)
    assert result["sizing"]["volume"] == 1.29


def test_volume_below_minimum_after_costs_is_rejected() -> None:
    far = envelope(level=(1.2600, 1.2610))  # a distant target keeps the net ratio above 2
    result = run(env=far, costs=terms(commission=200.0, holding=0))  # 5 / 566 = 0.0088 lots
    assert result["status"] == REJECTED and check_ids(result) == ["volume_minimum"]
    assert "including costs" in result["rejection_reasons"][0]["reason"]


def test_minimum_volume_only_when_the_total_loss_fits() -> None:
    far = envelope(level=(1.2600, 1.2610))  # net ratio stays above 2 with these costs
    fits = run(env=far, costs=terms(commission=134.0, holding=0))  # 0.01 x 500 = 5.00 = budget
    assert fits["status"] == APPROVED and fits["sizing"]["estimated_loss"] == 5.0
    over = run(env=far, costs=terms(commission=134.01, holding=0))
    assert over["status"] == REJECTED and check_ids(over) == ["volume_minimum"]


def test_above_maximum_is_capped_and_still_within_budget() -> None:
    fake = rich(2_000_000_000.0)
    result = sized(fake, commission=20.0, holding=0)
    assert result["sizing"]["volume"] == 200.0 and result["sizing"]["volume_capped_at_maximum"]
    assert result["sizing"]["estimated_loss"] <= result["sizing"]["risk_budget"]


@pytest.mark.parametrize(
    ("equity", "commission", "holding"),
    [(1000.0, 0.0, 0), (5_000.0, 7.5, 1), (73_200.0, 20.0, 2), (100_000.0, 3.3, 3)],
)
def test_total_expected_loss_never_exceeds_the_budget(equity, commission, holding) -> None:
    result = sized(rich(equity), commission=commission, holding=holding)
    s = result["sizing"]
    total = s["volume"] * s["loss_per_lot"]["total"]
    assert total <= s["risk_budget"] + 1e-9
    # estimated_loss = round(volume x raw total, 2); the view's per-lot total is rounded to
    # 6 decimals, so the two differ by at most half a cent plus that rounding (Phase 1 M-10:
    # the old exact comparison failed at half-cent boundaries depending on the swap count)
    assert abs(s["estimated_loss"] - total) <= 0.005 + s["volume"] * 5e-7 + 1e-9
    assert s["estimated_loss"] == pytest.approx(
        s["estimated_price_loss"] + s["estimated_costs"], abs=0.011
    )


@pytest.mark.parametrize(
    ("costs", "reason"),
    [
        ({"commission": None}, "commission not verified"),
        ({"holding": None}, "holding period not approved"),
        ({"slippage": None}, "slippage unmodelled"),
    ],
)
def test_unestablished_costs_prevent_verified_sizing(costs, reason) -> None:
    fake = rich()
    result = run(fake, costs=terms(**{"commission": 0.0, "holding": 1, "slippage": 0.0, **costs}))
    s = result["sizing"]
    assert s["status"] == "unverified" and s["volume"] is None and s["includes_costs"] is False
    assert s["illustrative_volume_price_risk_only"]["value"] == 1.36
    per_trade = next(u for u in result["unverified"] if u["id"] == "limit")
    assert (
        "0.5% per-trade limit not verified" in per_trade["reason"] and reason in per_trade["reason"]
    )
    assert result["status"] == UNVERIFIED
    assert "order_calc_profit" not in fake.calls  # no calculator for a non-actionable volume


def test_unsupported_swap_mode_prevents_verified_sizing() -> None:
    fake = rich()
    fake.symbols["EURUSDc"]["info"] = {"swap_mode": 6}
    result = run(fake, costs=terms())
    assert result["sizing"]["status"] == "unverified" and result["status"] == UNVERIFIED


def test_missing_or_stale_equity_prevents_sizing() -> None:
    fake = make_fake()
    fake.account_missing.add("equity")
    result = run(fake, costs=terms())
    assert result["sizing"] is None and result["status"] == UNVERIFIED
    fake = make_fake()
    from risk_fixtures import stack  # noqa: PLC0415

    _, account, risk = stack(fake, StubAnalysis(envelope(level=BUY_LEVEL)), costs=terms())
    account.summary()
    account._fetched_at = account._fetched_at.replace(year=2000)
    fake.running = False
    stale = risk.proposal("EURUSD", "H1")
    assert stale["sizing"] is None and stale["status"] == UNVERIFIED


def test_remaining_daily_allowance_binds_when_lower() -> None:
    fake = rich(10_000.0)  # per-trade 50; daily limit 200
    fake.deals = [deal(1, -190.0)]  # 10 left
    result = run(fake, costs=terms(commission=20.0, holding=0))
    s = result["sizing"]
    assert s["risk_budget"] == 10.0 and s["risk_budget_basis"] == "remaining daily allowance"
    assert s["volume"] == 0.02  # 10 / 386 = 0.0259
    assert s["estimated_loss"] <= 10.0 and result["status"] == APPROVED
    assert result["daily_risk"]["proposed"] == s["estimated_loss"]


def test_no_cost_is_counted_twice(monkeypatch) -> None:
    monkeypatch.setattr(risk_engine, "rollover_charges", fixed_rollovers(4))
    result = sized(commission=20.0, holding=2, slippage=1.0)
    s, net = result["sizing"], result["net_reward_risk"]
    cost = 20.0 + 22.8 + 2.0
    assert s["loss_per_lot"]["costs_total"] == pytest.approx(cost)
    assert net["cost_per_lot"] == pytest.approx(cost)  # the same components feed the net gate
    assert net["net_risk_per_lot"] == pytest.approx(366 + cost)
    assert net["net_reward_per_lot"] == pytest.approx(802 - cost)
    assert s["estimated_costs"] == pytest.approx(round(s["volume"] * cost, 2))
    assert result["daily_risk"]["proposed"] == s["estimated_loss"]  # costs once in daily too


def test_production_default_never_yields_a_volume() -> None:
    result = run(rich(), costs=PRODUCTION_COSTS)
    assert result["status"] == UNVERIFIED and result["sizing"]["volume"] is None
    assert result["validations"]["margin"]["status"] == "unverified"
