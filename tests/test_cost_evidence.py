"""P6.2b.1 cost evidence: broker rollover events, timeframe holding assumptions, structured cost
components (currency, unit, basis), independent evidence status and fail-closed integration.

Deterministic timezone-aware timestamps for the schedule; the real Phase 4 pipeline on the
fake MT5 for integration; synthetic evidence only -- production COST_TERMS stays empty."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta, timezone

import pytest
from risk_fixtures import (
    BUY_LEVEL,
    PRODUCTION_COSTS,
    check_ids,
    envelope,
    fixture_broker_terms,
    make_fake,
    negative_swap,
    propose,
    synthetic_terms,
)

from johanna.cost_evidence import (
    ACCOUNT_TYPE,
    HOLDING_PROPOSALS,
    CostComponent,
    CostTerms,
    HoldingAssumption,
    assess_component,
    assess_holding,
    commission_round_trip_usc,
    rollover_charges,
    slippage_points_per_side,
)
from johanna.risk_engine import APPROVED, COST_TERMS, RISK_POLICY, UNVERIFIED
from johanna.shadow_intents import build_intent

WED = 2


def utc(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def charges(start: datetime, hours: float) -> dict:
    return rollover_charges(start, start + timedelta(hours=hours), WED)


# ====================================================================== rollover schedule
def test_summer_rollover_is_21_utc_and_winter_22_utc() -> None:
    summer = charges(utc(2026, 7, 14, 20, 0), 2)  # Tuesday, US daylight saving
    assert [e["at_utc"] for e in summer["events"]] == ["2026-07-14T21:00:00Z"]
    winter = charges(utc(2026, 1, 13, 20, 0), 3)  # Tuesday, US standard time
    assert [e["at_utc"] for e in winter["events"]] == ["2026-01-13T22:00:00Z"]
    assert summer["units"] == winter["units"] == 1


@pytest.mark.parametrize(
    ("start", "units"),
    [
        (utc(2026, 7, 14, 20, 59, 59), 1),  # immediately before: counted
        (utc(2026, 7, 14, 21, 0, 0), 1),  # exactly at the rollover: counted (conservative)
        (utc(2026, 7, 14, 21, 0, 1), 0),  # immediately after: already happened, not counted
    ],
)
def test_rollover_boundary(start, units) -> None:
    assert charges(start, 0.5)["units"] == units


def test_wednesday_rollover_is_triple() -> None:
    result = charges(utc(2026, 7, 15, 20, 0), 2)
    assert result["units"] == 3 and result["events"][0]["weekday"] == "wednesday"


def test_the_p62b_defect_case_counts_the_next_real_rollover() -> None:
    # Tuesday 22:30 UTC in summer: Tuesday's rollover (21:00 UTC) has passed; the next one is
    # Wednesday's triple. The old UTC-day model counted Tuesday's single charge instead.
    result = charges(utc(2026, 7, 14, 22, 30), 24)
    assert result["units"] == 3
    assert [e["local_date"] for e in result["events"]] == ["2026-07-15"]


def test_friday_weekend_and_monday_resumption() -> None:
    after_friday = utc(2026, 7, 17, 21, 30)  # Friday, after its rollover
    assert charges(after_friday, 48)["units"] == 0  # Saturday and Sunday: no rollover
    monday = charges(after_friday, 72)  # to Monday 21:30: Monday's rollover counts
    assert monday["units"] == 1 and monday["events"][0]["weekday"] == "monday"
    friday = charges(utc(2026, 7, 17, 20, 0), 2)
    assert friday["units"] == 1 and friday["events"][0]["weekday"] == "friday"


def test_a_full_week_is_seven_charges_and_no_rollover_is_counted_twice() -> None:
    result = charges(utc(2026, 7, 13, 0, 0), 7 * 24)
    dates = [e["local_date"] for e in result["events"]]
    assert result["units"] == 7 and len(dates) == 5 == len(set(dates))


def test_month_and_year_boundaries() -> None:
    new_year = charges(utc(2025, 12, 31, 21, 30), 26)  # Wed 31 Dec 22:00 UTC (winter), Thu
    assert [e["at_utc"] for e in new_year["events"]] == [
        "2025-12-31T22:00:00Z",
        "2026-01-01T22:00:00Z",
    ]
    assert new_year["units"] == 3 + 1
    assert new_year["holiday_adjustments"] == "not_modelled"  # 1 January is not special-cased
    month_end = charges(utc(2026, 4, 30, 20, 0), 2)  # Thursday 30 April, summer
    assert [e["at_utc"] for e in month_end["events"]] == ["2026-04-30T21:00:00Z"]


def test_daylight_saving_transitions() -> None:
    # US clocks change on Sunday 8 March 2026: Friday 6 March at 22:00 UTC, Monday 9 at 21:00.
    week = charges(utc(2026, 3, 6, 12, 0), 4 * 24)
    assert [e["at_utc"] for e in week["events"]] == [
        "2026-03-06T22:00:00Z",
        "2026-03-09T21:00:00Z",
    ]
    # US back to standard time on Sunday 1 November 2026.
    autumn = charges(utc(2026, 10, 30, 12, 0), 4 * 24)
    assert [e["at_utc"] for e in autumn["events"]] == [
        "2026-10-30T21:00:00Z",
        "2026-11-02T22:00:00Z",
    ]


def test_us_uk_clock_gap_is_reported_and_the_larger_count_used() -> None:
    # 10 March 2026: New York is on daylight time (17:00 = 21:00 UTC), London is not yet
    # (22:00 GMT = 22:00 UTC). A 21:30-22:30 UTC window contains only the London reading.
    gap = charges(utc(2026, 3, 10, 21, 30), 1)
    assert gap["units_primary"] == 0 and gap["units_alternative"] == 1
    assert gap["units"] == 1 and gap["seasonal_ambiguity"] is True
    aligned = charges(utc(2026, 7, 14, 20, 0), 3)  # both readings agree in July
    assert aligned["seasonal_ambiguity"] is False


@pytest.mark.parametrize(
    ("start", "end", "triple"),
    [
        (datetime(2026, 7, 14, 20, 0), utc(2026, 7, 15), WED),  # naive start
        (utc(2026, 7, 14), datetime(2026, 7, 15), WED),  # naive end
        (utc(2026, 7, 15), utc(2026, 7, 14), WED),  # inverted window
        (utc(2026, 1, 1), utc(2026, 6, 1), WED),  # beyond the sanity bound
        (utc(2026, 7, 14), utc(2026, 7, 15), 5),  # Saturday is not a triple day
        (utc(2026, 7, 14), utc(2026, 7, 15), True),
    ],
)
def test_invalid_schedule_inputs_raise(start, end, triple) -> None:
    with pytest.raises(ValueError):
        rollover_charges(start, end, triple)


def test_offset_aware_inputs_in_other_zones_are_equivalent() -> None:
    plus2 = timezone(timedelta(hours=2))
    a = charges(datetime(2026, 7, 14, 22, 0, tzinfo=plus2), 2)  # = 20:00 UTC
    b = charges(utc(2026, 7, 14, 20, 0), 2)
    assert a["events"] == b["events"]


# ====================================================================== holding assumptions
def test_holding_proposals_are_drafts_and_never_active() -> None:
    assert {tf: h.max_hours for tf, h in HOLDING_PROPOSALS.items()} == {
        "H1": 24.0,
        "H4": 120.0,
        "D1": 480.0,
    }
    assert {h.status for h in HOLDING_PROPOSALS.values()} == {"PROPOSED"}
    assert COST_TERMS.holding == {} and not COST_TERMS.configured
    view = assess_holding(HOLDING_PROPOSALS["H1"], "H1", utc(2026, 11, 20))
    assert view["usable"] is False and "PROPOSED" in view["reason"]


def test_proposed_holding_cannot_approve_a_trade() -> None:
    base = synthetic_terms()
    terms = CostTerms(components=base.components, holding=dict(HOLDING_PROPOSALS))
    result = propose(negative_swap(make_fake()), costs=terms)  # EURUSD BUY: swap is a cost
    assert result["status"] == UNVERIFIED and result["sizing"]["volume"] is None
    swap = result["costs"]["swap"]
    assert swap["status"] == "unverified" and "PROPOSED" in swap["reason"]


def test_no_holding_for_the_timeframe_is_never_substituted() -> None:
    base = synthetic_terms(holding_hours=24.0)
    h4_only = CostTerms(components=base.components, holding={"H4": base.holding["H4"]})
    result = propose(negative_swap(make_fake()), costs=h4_only)  # an H1 proposal
    assert result["status"] == UNVERIFIED
    assert "no holding-period assumption for H1" in result["costs"]["swap"]["reason"]


def test_expired_holding_approval_fails_closed() -> None:
    old = datetime.now(UTC) - timedelta(days=31)
    stale = synthetic_terms(holding_hours=24.0)
    holding = {
        tf: HoldingAssumption(**{**h.__dict__, "approved_at_utc": old, "effective_utc": old})
        for tf, h in stale.holding.items()
    }
    result = propose(
        negative_swap(make_fake()), costs=CostTerms(components=stale.components, holding=holding)
    )
    assert result["status"] == UNVERIFIED and "expired" in result["costs"]["swap"]["reason"]


def test_holding_assumption_validation() -> None:
    h = HOLDING_PROPOSALS["H1"]
    for bad in (
        {"timeframe": "M5"},
        {"max_hours": 0},
        {"max_hours": float("nan")},
        {"max_hours": True},
        {"status": "ACTIVE"},
        {"status": "APPROVED", "approved_at_utc": None},
        {"effective_utc": datetime(2026, 11, 18)},
        {"policy_version": " "},
    ):
        with pytest.raises(ValueError):
            HoldingAssumption(**{**h.__dict__, **bad})


# ====================================================================== swap integration
def test_negative_swap_uses_the_live_rate_and_rollover_events() -> None:
    result = propose(negative_swap(make_fake()), costs=synthetic_terms(holding_hours=7 * 24.0))
    swap = result["costs"]["swap"]
    assert swap["rate_source"] == "live MT5 symbol specification"
    assert swap["per_rollover_per_lot"] == pytest.approx(-5.7)  # fake EURUSDc long rate
    assert swap["charge_units"] == 7 and swap["cost_per_lot"] == pytest.approx(39.9)


def test_positive_and_zero_swap_never_reduce_risk() -> None:
    fake = make_fake()
    fake.symbols["EURUSDc"]["info"] = {"swap_long": 3.0}
    credit = propose(fake, costs=synthetic_terms(holding_hours=7 * 24.0))
    assert credit["costs"]["swap"]["cost_per_lot"] == 0.0 and credit["status"] == APPROVED
    fake = make_fake()
    fake.symbols["EURUSDc"]["info"] = {"swap_long": 0.0}
    zero = propose(fake, costs=synthetic_terms(holding_hours=None))  # no holding needed
    assert zero["costs"]["swap"]["status"] == "established" and zero["status"] == APPROVED


def test_usdjpy_swap_uses_the_price_dependent_point_value() -> None:
    env = envelope(level=(161.000, 161.100), pivot=158.300, atr=0.32, symbol="USDJPY")
    result = propose(
        negative_swap(make_fake(), "USDJPYc"),
        env=env,
        symbol="USDJPY",
        costs=synthetic_terms(holding_hours=7 * 24.0),
    )
    swap = result["costs"]["swap"]
    point_value = 0.001 * 1000 * 100 / 159.119  # USC per point per lot at the bid
    assert swap["per_rollover_per_lot"] == pytest.approx(-5.7 * point_value, rel=1e-6)
    assert swap["cost_per_lot"] == pytest.approx(5.7 * point_value * 7, rel=1e-6)


def test_missing_triple_day_fails_closed_when_swap_is_a_cost() -> None:
    fake = make_fake()
    negative_swap(fake, swap_rollover3days=6)  # Sunday: not a broker weekday
    result = propose(fake, costs=synthetic_terms(holding_hours=24.0))
    assert result["costs"]["swap"]["status"] == "unverified" and result["status"] == UNVERIFIED


# ====================================================================== cost components
def _commission(**change) -> CostComponent:
    return CostComponent(**{**synthetic_terms().components[0].__dict__, **change})


def test_usd_and_per_side_conversions_are_explicit() -> None:
    usd_side = commission_round_trip_usc(_commission(value=0.035, currency="USD", basis="per_side"))
    assert usd_side["usc_per_lot_round_trip"] == pytest.approx(7.0)
    assert usd_side["usc_per_0_01_lot_round_trip"] == pytest.approx(0.07)
    assert usd_side["steps"] == [
        "0.035 USD per lot of 1000 units, per_side",
        "x 100 USC per USD = 3.5 USC per lot",
        "x 2 sides = 7 USC per lot round trip",
    ]
    usc = commission_round_trip_usc(_commission(value=7.0))
    assert usc["usc_per_lot_round_trip"] == 7.0 and len(usc["steps"]) == 1  # no conversion


def test_usd_commission_is_converted_once_in_the_pipeline() -> None:
    terms = synthetic_terms(
        commission=0.035, commission_currency="USD", commission_basis="per_side"
    )
    result = propose(costs=terms)
    assert result["costs"]["commission"]["per_lot_round_trip"] == pytest.approx(7.0)
    lpl = result["sizing"]["loss_per_lot"]
    assert lpl["total"] == pytest.approx(lpl["price_component"] + 7.0)  # spread not added again
    assert result["net_reward_risk"]["cost_per_lot"] == pytest.approx(7.0)


def test_slippage_pips_and_round_trip_conversions() -> None:
    base = synthetic_terms().components[1]
    pips = CostComponent(**{**base.__dict__, "value": 0.2, "unit": "pips"})
    assert slippage_points_per_side(pips, 5)["points_per_side"] == pytest.approx(2.0)
    assert slippage_points_per_side(pips, 3)["points_per_side"] == pytest.approx(2.0)
    assert slippage_points_per_side(pips, 2) is None  # no defined pip for this precision
    rt = CostComponent(**{**base.__dict__, "value": 4.0, "basis": "round_trip"})
    assert slippage_points_per_side(rt, 5)["points_per_side"] == 2.0
    view = assess_component(
        pips,
        "slippage",
        base.approved_at_utc,
        "EURUSDc",
        2,
        live_terms=fixture_broker_terms()["EURUSDc"],
    )
    assert view["status"] == "UNCONVERTIBLE" and view["usable"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"currency": None},  # commission without a currency
        {"currency": "EUR"},  # unsupported currency
        {"unit": "points"},  # wrong unit for commission
        {"unit": "amount_per_volume_unit"},  # per volume unit: unsupported
        {"unit": ""},
        {"basis": "per_trade"},
        {"basis": None},
        {"value": -0.01},
        {"value": float("nan")},
        {"value": math.inf},
        {"value": True},
        {"instruments": ("XAUUSDc",)},
        {"instruments": ()},
        {"instruments": ["EURUSDc"]},
        {"instruments": ("EURUSDc", "EURUSDc")},
        {"account_type": "exness_raw_spread"},
        {"source_reference": ""},
        {"source_description": None},
        {"evidence_date": datetime(2026, 11, 1)},
        {"effective_utc": None},
        {"status": "ACTIVE"},
        {"status": "VERIFIED", "approved_at_utc": None},
        {"component": "swap"},  # swap is never configured: live MT5 data only
    ],
)
def test_invalid_commission_records_are_rejected(change) -> None:
    with pytest.raises(ValueError):
        _commission(**change)


def test_slippage_needs_points_or_pips_and_no_currency() -> None:
    base = synthetic_terms().components[1]
    for bad in ({"currency": "USC"}, {"unit": "amount_per_lot"}, {"basis": "per_rollover"}):
        with pytest.raises(ValueError):
            CostComponent(**{**base.__dict__, **bad})


def test_approval_cannot_precede_the_evidence() -> None:
    c = synthetic_terms().components[0]
    with pytest.raises(ValueError):
        _commission(approved_at_utc=c.evidence_date - timedelta(days=1))


@pytest.mark.parametrize("status", ["UNVERIFIED", "PROVISIONAL", "REJECTED"])
def test_non_verified_evidence_never_satisfies_a_requirement(status) -> None:
    base = synthetic_terms()
    commission = CostComponent(**{**base.components[0].__dict__, "status": status})
    terms = CostTerms(components=(commission, base.components[1]), holding=base.holding)
    result = propose(costs=terms)
    assert result["status"] == UNVERIFIED and result["sizing"]["volume"] is None
    assert result["costs"]["commission"]["evidence"]["status"] == status
    assert result["costs"]["slippage"]["evidence"]["status"] == "VERIFIED"  # independent
    reason = next(u["reason"] for u in result["unverified"] if u["area"] == "costs")
    assert f"commission evidence is {status}" in reason and "slippage" not in reason


def test_missing_components_are_missing_not_zero() -> None:
    for kwargs, missing in (({"commission": None}, "commission"), ({"slippage": None}, "slippage")):
        result = propose(costs=synthetic_terms(**kwargs))
        assert result["status"] == UNVERIFIED
        assert result["costs"][missing]["evidence"]["status"] == "MISSING"
        assert result["sizing"]["loss_per_lot"][missing] is None
        assert result["sizing"]["volume"] is None


def test_out_of_scope_evidence_does_not_apply() -> None:
    result = propose(costs=synthetic_terms(symbols=("GBPUSDc",)))  # an EURUSD proposal
    assert result["costs"]["commission"]["evidence"]["status"] == "MISSING"
    assert result["status"] == UNVERIFIED
    view = assess_component(
        synthetic_terms(symbols=("GBPUSDc",)).components[0],
        "commission",
        datetime.now(UTC),
        "EURUSDc",
    )
    assert view["status"] == "OUT_OF_SCOPE" and view["usable"] is False


def test_two_records_for_one_cost_are_refused() -> None:
    c = synthetic_terms().components[0]
    with pytest.raises(ValueError):
        CostTerms(components=(c, _commission(instruments=("EURUSDc",))))


# ====================================================================== production safety
def test_production_stays_unverified_with_no_volume_and_no_approved_intent() -> None:
    result = propose(env=envelope(level=BUY_LEVEL), costs=PRODUCTION_COSTS)
    assert result["status"] == UNVERIFIED and result["sizing"]["volume"] is None
    costs = result["costs"]
    assert costs["commission"]["evidence"]["status"] == "MISSING"
    assert costs["slippage"]["evidence"]["status"] == "MISSING"
    assert costs["swap"]["holding"]["status"] == "MISSING"
    assert costs["terms"]["version"] == "unavailable"
    assert "net_reward_risk" in check_ids(result, "unverified")
    intent = build_intent(
        result,
        evaluation_id="E",
        decision_id="D",
        risk_assessment_id="R",
        window_close_utc="2026-11-18T13:00:00Z",
    )
    assert intent.eligibility == "UNVERIFIED" and intent.volume is None


def test_policy_values_and_account_type_unchanged() -> None:
    assert (RISK_POLICY.max_risk_per_trade, RISK_POLICY.max_daily_risk) == (0.005, 0.02)
    assert RISK_POLICY.min_reward_risk == 2.0 and ACCOUNT_TYPE == "exness_standard_cent"
