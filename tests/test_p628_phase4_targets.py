"""P6.2b.8 Phase 4: independent structural-target revalidation through the shared contract.

Synthetic data only: real-pipeline R7 envelopes reshaped by ``risk_fixtures`` and a FakeMT5.
Phase 4 never trusts TC6's "pass": missing, weak, wrong-side or malformed target evidence is a
structured REJECTION (never an exception, an approval or an unverified result). Every other
safeguard -- structural stop, buffer, gross and net 1:2, verified costs -- is unchanged.
"""

from __future__ import annotations

import copy
import math
from datetime import UTC, datetime, timedelta

import pytest
from fake_mt5 import READ_ONLY_CALLS
from risk_fixtures import (
    BUY_LEVEL,
    NO_TARGET,
    PRODUCTION_COSTS,
    REAL_BUY,
    REAL_SELL,
    SELL_LEVEL,
    SELL_SYMBOLS,
    check_ids,
    envelope,
    make_fake,
    propose,
    synthetic_terms,
)
from test_risk_api import _api, _call

from johanna.analysis.features import DEFAULT_DECISION_PARAMETERS
from johanna.analysis.targets import validate_structural_target
from johanna.risk_engine import APPROVED, REJECTED, RISK_POLICY, UNVERIFIED


def approved(result: dict) -> None:
    assert result["status"] == APPROVED, (result["rejection_reasons"], result["unverified"])


def rejected_for(result: dict, check_id: str, reason: str) -> None:
    assert result["status"] == REJECTED, (result["rejection_reasons"], result["unverified"])
    assert check_ids(result) == [check_id]
    assert result["target_validation"]["valid"] is False
    assert result["target_validation"]["reason"] == reason
    assert result["proposal"] is None and result["gross_reward_risk"] is None  # nothing built
    assert result["validations"]["construction"]["status"] == "failed"
    assert result["validations"]["costs"]["status"] == "not_evaluated"


def sell(**kwargs) -> dict:
    kwargs.setdefault("level", SELL_LEVEL)
    return propose(fake=make_fake(SELL_SYMBOLS), env=envelope("bearish", **kwargs))


# ---------------------------------------------------------------- valid construction
def test_valid_buy_construction_is_unchanged() -> None:
    result = propose(env=envelope(level=BUY_LEVEL))
    approved(result)
    p = result["proposal"]
    assert (p["entry_price"], p["stop_loss"], p["take_profit"]) == (1.23798, 1.23432, 1.246)
    assert result["gross_reward_risk"] == pytest.approx(0.00802 / 0.00366, abs=1e-3)
    tv = result["target_validation"]
    assert tv == {
        "contract": "johanna.analysis.targets.validate_structural_target",
        "reference": "ask",
        "valid": True,
        "reason": "structural_target_valid",
        "detail": tv["detail"],
    }


def test_valid_sell_construction_is_unchanged() -> None:
    result = sell()
    approved(result)
    p = result["proposal"]
    assert (p["entry_price"], p["stop_loss"], p["take_profit"]) == (0.962, 0.96568, 0.954)
    assert result["target_validation"]["reference"] == "bid"


def test_genuine_pipeline_targets_construct_in_both_directions() -> None:
    # REAL_BUY / REAL_SELL carry the target T6 itself selected (no reshaping of the level)
    buy = propose(env=envelope())
    approved(buy)
    assert buy["proposal"]["take_profit"] == 1.2622  # TC6's resistance, strength 6
    down = propose(fake=make_fake(SELL_SYMBOLS), env=envelope("bearish"))
    approved(down)
    assert down["proposal"]["take_profit"] == 0.9378


@pytest.mark.parametrize("direction", ["bullish", "bearish"])
def test_stop_loss_construction_is_unchanged(direction) -> None:
    bull = direction == "bullish"
    result = propose(env=envelope(level=BUY_LEVEL)) if bull else sell()
    atr = (REAL_BUY if bull else REAL_SELL)["result"]["features"]["latest"]["ATR14"]["value"]
    pivot = 1.23455 if bull else 0.96545
    buffer = RISK_POLICY.stop_buffer_atr * atr
    assert RISK_POLICY.stop_buffer_atr == 0.25
    expected = (
        math.floor((pivot - buffer) * 1e5) / 1e5
        if bull
        else (math.ceil((pivot + buffer) * 1e5) / 1e5)
    )
    assert result["proposal"]["stop_loss"] == pytest.approx(expected, abs=1e-9)


# ---------------------------------------------------------------- rejections
def test_missing_target_is_rejected() -> None:
    rejected_for(
        propose(env=envelope(selected_level=NO_TARGET)), "target_structure", "no_structural_target"
    )
    rejected_for(sell(selected_level=NO_TARGET), "target_structure", "no_structural_target")


def test_tc6_entry_absent_is_rejected() -> None:
    env = envelope(level=BUY_LEVEL)
    opp = env["result"]["opportunity"]
    opp["checklist"] = [c for c in opp["checklist"] if c["id"] != "TC6_clearance"]
    rejected_for(propose(env=env), "target_structure", "no_structural_target")


@pytest.mark.parametrize("checklist", ["TC6_clearance", 42, {"id": "TC6_clearance"}, [None, 3]])
def test_malformed_checklist_is_rejected_without_exception(checklist) -> None:
    env = envelope(level=BUY_LEVEL)
    env["result"]["opportunity"]["checklist"] = checklist
    rejected_for(propose(env=env), "target_structure", "no_structural_target")


@pytest.mark.parametrize("strength", [1, 0, 1.5])
def test_weak_target_is_rejected(strength) -> None:
    level = {"low": 1.246, "high": 1.247, "level": 1.2465, "strength": strength}
    rejected_for(
        propose(env=envelope(selected_level=level)),
        "target_structure",
        "target_strength_below_minimum",
    )


def test_declared_stricter_strength_is_honoured_and_a_lower_one_never_loosens() -> None:
    stricter = envelope(level=BUY_LEVEL)  # fixture level strength 3
    stricter["result"]["parameters"]["decision"]["min_level_strength"] = 4
    rejected_for(propose(env=stricter), "target_structure", "target_strength_below_minimum")
    looser = envelope(selected_level={"low": 1.246, "high": 1.247, "strength": 1})
    looser["result"]["parameters"]["decision"]["min_level_strength"] = 1
    rejected_for(propose(env=looser), "target_structure", "target_strength_below_minimum")
    assert DEFAULT_DECISION_PARAMETERS.min_level_strength == 2


def test_wrong_side_target_is_rejected() -> None:
    rejected_for(
        propose(env=envelope(level=(1.2300, 1.2310))), "target_direction", "target_wrong_side"
    )
    rejected_for(sell(level=(0.9700, 0.9710)), "target_direction", "target_wrong_side")
    # a zone containing the executable entry is not beyond it
    rejected_for(
        propose(env=envelope(level=(1.2370, 1.2390))), "target_direction", "target_wrong_side"
    )


@pytest.mark.parametrize(
    "bad",
    [
        "1.246",
        1.246,
        ["low", 1.246],
        (1.246, 1.247),
        True,
        {},
        {"low": 1.246},
        {"low": None, "high": None, "strength": 3},
        {"low": "1.246", "high": "1.247", "strength": 3},
        {"low": 1.246, "high": 1.247},  # strength missing
        {"low": 1.246, "high": 1.247, "strength": "3"},
        {"low": False, "high": 1.247, "strength": 3},
        {"low": 1.247, "high": 1.246, "strength": 3},  # inverted
    ],
)
def test_malformed_target_evidence_is_rejected_without_exception(bad) -> None:
    result = propose(env=envelope(selected_level=copy.deepcopy(bad)))
    rejected_for(result, "target_structure", "invalid_target_evidence")


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
@pytest.mark.parametrize("key", ["low", "high", "strength"])
def test_nan_and_infinite_target_is_rejected(key, value) -> None:
    level = {"low": 1.246, "high": 1.247, "level": 1.2465, "strength": 3, key: value}
    rejected_for(
        propose(env=envelope(selected_level=level)), "target_structure", "invalid_target_evidence"
    )


def test_checklist_claiming_success_never_substitutes_for_evidence() -> None:
    env = envelope(selected_level={"low": math.nan, "high": 1.247, "strength": 3})
    tc6 = next(c for c in env["result"]["opportunity"]["checklist"] if c["id"] == "TC6_clearance")
    tc6.update(result="pass", evidence_id="resistance_clearance_sufficient")
    tc6["opposing_level_check"] = {"valid": True, "code": "opposing_level_valid"}
    rejected_for(propose(env=env), "target_structure", "invalid_target_evidence")


def test_malformed_target_through_the_api_is_a_structured_rejection_not_a_500(tmp_path) -> None:
    for bad in ("not-a-mapping", {"low": math.inf, "high": 1.247, "strength": 3}, None):
        env = envelope(selected_level=NO_TARGET if bad is None else bad)
        fake, _, _, api = _api(tmp_path / str(id(bad)), env=env)
        status, body, text, _ = _call(api, "/api/risk/proposal", "symbol=EURUSD&timeframe=H1")
        assert status == "200 OK", body
        assert body["status"] == REJECTED and body["proposal"] is None
        assert [r["id"] for r in body["rejection_reasons"]] == ["target_structure"]
        assert "NaN" not in text and "Infinity" not in text
        assert set(fake.calls) <= READ_ONLY_CALLS


# ---------------------------------------------------------------- downstream checks unchanged
def test_gross_reward_risk_failure_remains_rejected() -> None:
    result = propose(env=envelope(level=(1.2450, 1.2460)))  # 702 / 366 = 1.92
    assert result["status"] == REJECTED and check_ids(result) == ["reward_risk"]
    assert result["target_validation"]["valid"] is True  # a valid target, too close for 1:2
    assert result["gross_reward_risk"] == pytest.approx(702 / 366, abs=1e-3)
    assert RISK_POLICY.min_reward_risk == 2.0


def test_net_reward_risk_failure_remains_rejected() -> None:
    costs = synthetic_terms(slippage=20.0, when=datetime.now(UTC) - timedelta(hours=1))
    result = propose(env=envelope(level=BUY_LEVEL), costs=costs)  # gross 2.19 passes
    assert result["status"] == REJECTED and check_ids(result) == ["net_reward_risk"]
    assert result["gross_reward_risk"] == pytest.approx(2.191, abs=1e-3)
    assert result["net_reward_risk"]["value"] < 2.0


def test_missing_cost_evidence_remains_unverified() -> None:
    result = propose(env=envelope(level=BUY_LEVEL), costs=PRODUCTION_COSTS)
    assert result["status"] == UNVERIFIED and result["rejection_reasons"] == []
    assert "net_reward_risk" in check_ids(result, "unverified")
    assert result["target_validation"]["valid"] is True


def test_phase4_contract_agrees_with_the_shared_function() -> None:
    env = envelope(level=BUY_LEVEL)
    tc6 = next(c for c in env["result"]["opportunity"]["checklist"] if c["id"] == "TC6_clearance")
    direct = validate_structural_target(
        tc6["selected_level"], direction="bullish", reference_price=1.23798, min_strength=2
    )
    result = propose(env=env)
    assert result["target_validation"]["reason"] == direct["reason"] == "structural_target_valid"


# ---------------------------------------------------------------- symbols, price scale, TFs
def _scaled(direction: str, symbol: str) -> tuple[dict, dict]:
    """Synthetic per-instrument levels on each instrument's own price scale."""
    if direction == "bullish":
        fake = make_fake()
        shapes = {
            "EURUSD": {"level": BUY_LEVEL},
            "GBPUSD": {"level": (1.32900, 1.33000), "pivot": 1.31600, "atr": 0.0009},
            "USDJPY": {"level": (161.0004, 161.1), "pivot": 158.3006, "atr": 0.32},
        }
    else:
        fake = make_fake(SELL_SYMBOLS)
        shapes = {
            "EURUSD": {"level": SELL_LEVEL},
            "GBPUSD": {"level": SELL_LEVEL},
            "USDJPY": {"level": (157.4, 157.5004), "pivot": 159.8, "atr": 0.30},
        }
    fake.account.update(balance=10_000.0, equity=10_000.0, margin_free=10_000.0)
    return fake, envelope(direction, symbol=symbol, **shapes[symbol])


@pytest.mark.parametrize("timeframe", ["H1", "H4", "D1"])
@pytest.mark.parametrize("symbol", ["EURUSD", "GBPUSD", "USDJPY"])
@pytest.mark.parametrize("direction", ["bullish", "bearish"])
def test_valid_targets_construct_for_every_instrument_and_timeframe(direction, symbol, timeframe):
    fake, env = _scaled(direction, symbol)
    result = propose(fake=fake, env=env, symbol=symbol, timeframe=timeframe)
    approved(result)
    assert result["broker_symbol"] == f"{symbol}c" and result["timeframe"] == timeframe
    assert result["target_validation"]["valid"] is True
    p = result["proposal"]
    digits = 3 if symbol == "USDJPY" else 5
    for price in (p["entry_price"], p["stop_loss"], p["take_profit"]):
        assert round(price, digits) == price


@pytest.mark.parametrize("timeframe", ["H1", "H4", "D1"])
@pytest.mark.parametrize("symbol", ["EURUSD", "GBPUSD", "USDJPY"])
@pytest.mark.parametrize("direction", ["bullish", "bearish"])
def test_missing_or_invalid_targets_are_rejected_for_every_instrument(direction, symbol, timeframe):
    fake, env = _scaled(direction, symbol)
    for bad, reason in ((NO_TARGET, "no_structural_target"), ("x", "invalid_target_evidence")):
        broken = copy.deepcopy(env)
        tc6 = next(
            c for c in broken["result"]["opportunity"]["checklist"] if c["id"] == "TC6_clearance"
        )
        tc6["selected_level"] = None if bad is NO_TARGET else bad
        result = propose(fake=copy.deepcopy(fake), env=broken, symbol=symbol, timeframe=timeframe)
        rejected_for(result, "target_structure", reason)


def test_usdjpy_three_digit_tick_rounding_buy_and_sell() -> None:
    fake, env = _scaled("bullish", "USDJPY")
    buy = propose(fake=fake, env=env, symbol="USDJPY")
    approved(buy)
    assert buy["proposal"]["entry_price"] == 159.129  # ask
    assert buy["proposal"]["stop_loss"] == 158.22  # 158.3006 - 0.08 = 158.2206 -> down
    assert buy["proposal"]["take_profit"] == 161.0  # 161.0004 -> down, towards the entry
    fake, env = _scaled("bearish", "USDJPY")
    down = propose(fake=fake, env=env, symbol="USDJPY")
    approved(down)
    assert down["proposal"]["entry_price"] == 159.119  # bid
    assert down["proposal"]["stop_loss"] == 159.875  # 159.8 + 0.075 -> up (already on tick)
    assert down["proposal"]["take_profit"] == 157.501  # 157.5004 -> up, towards the entry


def test_usdjpy_scale_wrong_side_and_too_close_are_rejected() -> None:
    fake, env = _scaled("bullish", "USDJPY")
    wrong = envelope(level=(158.5, 158.6), pivot=158.3006, atr=0.32, symbol="USDJPY")
    rejected_for(
        propose(fake=copy.deepcopy(fake), env=wrong, symbol="USDJPY"),
        "target_direction",
        "target_wrong_side",
    )
    close = envelope(level=(159.6, 159.7), pivot=158.3006, atr=0.32, symbol="USDJPY")
    result = propose(fake=fake, env=close, symbol="USDJPY")
    assert result["status"] == REJECTED and check_ids(result) == ["reward_risk"]
    # a target one tick beyond the ask is valid evidence but fails the gross 1:2 minimum
    assert result["target_validation"]["valid"] is True
