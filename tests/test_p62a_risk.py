"""P6.2a risk-engine corrections: M1 final budget, M3 classifications, M4 version evidence and
the 30-day cost-terms expiry. Real Phase 4 pipeline on the fake MT5; synthetic cost terms only
(never production terms)."""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

import pytest
from risk_fixtures import (
    BUY_LEVEL,
    NO_TARGET,
    PRODUCTION_COSTS,
    VERIFIED_COSTS,
    check_ids,
    envelope,
    make_fake,
    propose,
    synthetic_terms,
)

from johanna.cost_evidence import CostComponent
from johanna.decision_journal import risk_record
from johanna.risk_engine import (
    APPROVED,
    COST_TERMS,
    COST_TERMS_MAX_AGE,
    COST_TERMS_UNAVAILABLE,
    REJECTED,
    RISK_ENGINE_VERSION,
    RISK_POLICY,
    UNVERIFIED,
    RiskPolicy,
    cost_terms_version,
    policy_fingerprint,
)
from johanna.shadow_evaluation import risk_gates

# Stop-loss 125 points below the ask: 0.04 lot x 125 USC/lot = exactly the 5.00 USC budget
# (0.5% of the fake account's 1000 USC equity).
EXACT_SL = 1.23673


def exact_env() -> dict:
    """Built per test: the envelope is timed "now" (T6 analyses older than 60 s are refused)."""
    return envelope(level=(1.25000, 1.25100), pivot=1.23683, atr=0.0004)


def _mt5_loss_factor(factor: float):
    """MT5's profit calculator returning ``factor`` x the loss at the exact stop-loss."""

    def hook(name, args, value):
        if name == "profit" and abs(args[4] - EXACT_SL) < 1e-9:
            return value * factor
        return value

    return hook


def _checks(result, area):
    return {c["id"]: c["outcome"] for c in result["validations"][area]["checks"]}


# ====================================================================== M1
def test_baseline_estimate_equals_the_budget() -> None:
    result = propose(env=exact_env())
    sizing = result["sizing"]
    assert result["proposal"]["stop_loss"] == EXACT_SL
    assert sizing["volume"] == 0.04 and sizing["estimated_loss"] == 5.0
    assert sizing["risk_budget"] == 5.0 and result["status"] == APPROVED
    assert sizing["final_loss"] == 5.0


def test_m1_mt5_loss_within_tolerance_but_above_the_budget_is_rejected() -> None:
    fake = make_fake()
    fake.calc_hook = _mt5_loss_factor(1.0098)  # Johanna 5.00 USC, MT5 5.049 USC, budget 5.00
    result = propose(fake, exact_env())
    sizing = result["sizing"]
    assert sizing["estimated_loss"] == 5.0 and sizing["risk_budget"] == 5.0
    assert sizing["mt5_calculator"]["loss_at_stop"] == pytest.approx(5.049)
    checks = _checks(result, "per_trade_risk")
    assert checks["mt5_profit_calculator"] == "passed"  # numerical agreement (1% tolerance)...
    assert checks["final_budget"] == "failed"  # ...is not permission to exceed the budget
    assert result["status"] == REJECTED and "final_budget" in check_ids(result)
    assert sizing["final_loss"] == pytest.approx(5.049)


def test_m1_final_loss_is_the_larger_amount() -> None:
    fake = make_fake()
    fake.calc_hook = _mt5_loss_factor(0.995)  # MT5 lower than the estimate: estimate decides
    result = propose(fake, exact_env())
    assert result["status"] == APPROVED
    assert result["sizing"]["final_loss"] == pytest.approx(5.0)
    assert _checks(result, "per_trade_risk")["final_budget"] == "passed"
    assert _checks(result, "daily_risk")["final_limit"] == "passed"


def test_m1_final_loss_includes_verified_costs() -> None:
    costs = synthetic_terms(commission=7.0)
    fake = make_fake()
    fake.calc_hook = _mt5_loss_factor(1.004)
    result = propose(fake, exact_env(), costs=costs)
    sizing = result["sizing"]
    volume = sizing["volume"]
    calc = sizing["mt5_calculator"]["loss_at_stop"]
    expected = max(calc, sizing["estimated_price_loss"]) + volume * 7.0
    assert sizing["final_loss"] == pytest.approx(expected, abs=1e-3)
    assert sizing["final_loss"] <= sizing["risk_budget"] + 1e-9


def test_m1_no_final_check_without_a_calculator_answer() -> None:
    fake = make_fake()
    fake.calc_hook = lambda name, args, value: None if name == "profit" else value
    result = propose(fake, exact_env())
    assert result["status"] == UNVERIFIED
    assert "final_budget" not in _checks(result, "per_trade_risk")


def test_policy_limits_are_unchanged() -> None:
    assert RISK_POLICY.max_risk_per_trade == 0.005
    assert RISK_POLICY.max_daily_risk == 0.02
    assert RISK_POLICY.min_reward_risk == 2.0
    assert RISK_POLICY.execution == "manual_only" and RISK_POLICY.mt5_access == "read_only"
    assert COST_TERMS == PRODUCTION_COSTS and not COST_TERMS.configured  # nothing configured
    assert COST_TERMS.components == () and COST_TERMS.holding == {}


def test_production_costs_never_give_an_approved_volume() -> None:
    result = propose(env=envelope(level=BUY_LEVEL), costs=PRODUCTION_COSTS)
    assert result["status"] == UNVERIFIED
    assert result["sizing"]["volume"] is None and result["sizing"]["final_loss"] is None
    assert result["net_reward_risk"]["status"] == "unverified"


def test_no_structural_target_is_still_rejected_without_a_fallback() -> None:
    # no TC6 level although the checklist claims "pass" (T6 cannot produce this since P6.2b.8)
    result = propose(env=envelope(selected_level=NO_TARGET), costs=VERIFIED_COSTS)
    assert result["status"] == REJECTED and "target_structure" in check_ids(result)
    assert result["proposal"] is None and result["gross_reward_risk"] is None


# ====================================================================== M3
def test_m3_stale_quote_is_unverified_and_gates_agree() -> None:
    result = propose(make_fake(tick_age_seconds=30))
    assert result["status"] == UNVERIFIED and result["rejection_reasons"] == []
    assert "quote_fresh" in check_ids(result, "unverified")
    gates = {g["gate"]: g for g in risk_gates(result)}
    assert gates["data.quote_fresh"]["result"] == "BLOCKED"
    assert gates["phase4.status"]["result"] == "BLOCKED"
    record = risk_record(result)
    assert record["risk_status"] == "UNVERIFIED"
    assert {"area": "data", "id": "quote_fresh"} in record["risk_evidence"]["unverified"]


def test_m3_calculation_disagreements_are_unverified() -> None:
    fake = make_fake()
    fake.calc_hook = lambda name, args, value: value * 1.2 if name == "profit" else value
    result = propose(fake)
    assert result["status"] == UNVERIFIED
    assert "mt5_profit_calculator" in check_ids(result, "unverified")
    assert {g["gate"]: g["result"] for g in risk_gates(result)}[
        "per_trade_risk.mt5_profit_calculator"
    ] == "BLOCKED"


def test_m3_genuine_policy_violations_stay_rejected() -> None:
    low_rr = propose(env=envelope(level=(1.24000, 1.24100)))  # gross reward:risk below 1:2
    assert low_rr["status"] == REJECTED and "reward_risk" in check_ids(low_rr)
    fake = make_fake()
    fake.account["leverage"] = 1
    margin = propose(fake)
    assert margin["status"] == REJECTED and "free_margin" in check_ids(margin)


# ====================================================================== M4
def test_m4_versions_are_reported_and_stable() -> None:
    first = propose(costs=PRODUCTION_COSTS)["versions"]
    second = propose(costs=PRODUCTION_COSTS)["versions"]
    assert first == second
    assert first["risk_engine_version"] == RISK_ENGINE_VERSION == 4  # 4 since P6.2b.8
    assert first["policy_fingerprint"] == policy_fingerprint(RISK_POLICY)
    assert first["policy_fingerprint"].startswith("sha256:")
    assert first["cost_terms_version"] == COST_TERMS_UNAVAILABLE == "unavailable"


def test_m4_changed_policy_or_terms_change_the_fingerprint() -> None:
    assert policy_fingerprint(RiskPolicy()) == policy_fingerprint(RISK_POLICY)
    assert policy_fingerprint(RiskPolicy(stop_buffer_atr=0.3)) != policy_fingerprint(RISK_POLICY)
    assert policy_fingerprint(RiskPolicy(max_quote_age_seconds=5)) != policy_fingerprint(
        RISK_POLICY
    )
    when = datetime(2026, 11, 18, tzinfo=UTC)
    base = cost_terms_version(synthetic_terms(when=when))
    assert base.startswith("sha256:") and base == cost_terms_version(synthetic_terms(when=when))
    changed = [
        synthetic_terms(commission=0.5, when=when),
        synthetic_terms(slippage_source="another source", when=when),
        synthetic_terms(holding_hours=24.0, when=when),
        synthetic_terms(when=when - timedelta(1)),
        synthetic_terms(commission_currency="USD", when=when),
        synthetic_terms(commission_basis="per_side", when=when),
    ]
    versions = {cost_terms_version(t) for t in changed}
    assert len(versions) == 6 and base not in versions
    assert cost_terms_version(COST_TERMS) == "unavailable"


def test_m4_new_risk_evidence_carries_versions_and_old_evidence_is_unchanged() -> None:
    result = propose(costs=PRODUCTION_COSTS)
    evidence = risk_record(result)["risk_evidence"]
    assert evidence["versions"] == result["versions"]
    old = copy.deepcopy(result)
    del old["versions"]  # an engine-v1 result (as recorded before P6.2a)
    legacy = risk_record(old)
    assert "versions" not in legacy["risk_evidence"]
    assert legacy["risk_fingerprint"] != risk_record(result)["risk_fingerprint"]
    assert legacy["risk_fingerprint"] == risk_record(copy.deepcopy(old))["risk_fingerprint"]


def test_m4_fingerprints_contain_no_account_data() -> None:
    result = propose(costs=VERIFIED_COSTS)
    text = repr(result["versions"])
    for secret in ("12345678", "Exness", "MT5Trial", "login", "server"):
        assert secret not in text


# ====================================================================== cost-terms expiry
def test_terms_within_thirty_days_are_current() -> None:
    terms = synthetic_terms(when=datetime.now(UTC) - timedelta(days=29))
    result = propose(costs=terms)
    assert result["status"] == APPROVED
    assert result["costs"]["commission"]["evidence"]["status"] == "VERIFIED"
    assert result["costs"]["terms"]["max_age_days"] == COST_TERMS_MAX_AGE.days == 30


@pytest.mark.parametrize(
    ("when", "status"),
    [
        (lambda: datetime.now(UTC) - timedelta(days=31), "EXPIRED"),
        (lambda: datetime.now(UTC) + timedelta(days=2), "NOT_YET_EFFECTIVE"),
    ],
)
def test_expired_or_not_yet_effective_terms_fail_closed(when, status) -> None:
    result = propose(costs=synthetic_terms(when=when()))
    assert result["status"] == UNVERIFIED
    assert result["costs"]["status"] == "unverified"
    assert result["costs"]["commission"]["evidence"]["status"] == status
    assert result["costs"]["slippage"]["evidence"]["status"] == status
    assert result["sizing"]["volume"] is None
    assert "net_reward_risk" in check_ids(result, "unverified")


def test_unconfigured_terms_report_the_unavailable_state() -> None:
    result = propose(costs=PRODUCTION_COSTS)
    assert result["costs"]["terms"]["configured"] is False
    assert result["costs"]["terms"]["version"] == "unavailable"
    assert result["costs"]["commission"]["evidence"]["status"] == "MISSING"


def test_approval_time_must_be_present_and_timezone_aware() -> None:
    commission = VERIFIED_COSTS.components[0]
    with pytest.raises(ValueError):
        CostComponent(**{**commission.__dict__, "approved_at_utc": datetime(2026, 11, 1)})
    with pytest.raises(ValueError):  # a VERIFIED record without its approval time
        CostComponent(**{**commission.__dict__, "approved_at_utc": None})
