"""T6 companion ``evaluate_market_snapshot_with_validation``: the exact T4 object, one T4 run.

The companion and ``evaluate_market_snapshot`` share one private pipeline. These tests prove the
T6 result is unchanged, T4 runs exactly once, and the returned validation is the very object
T4 produced and T5/T6 consumed. Synthetic data only.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest
from t6_fixtures import FRESH, trend_bars
from test_analysis_engine import history
from test_window_validation import BASE, candle, snapshot

import johanna.analysis.decision as decision
from johanna.analysis.decision import (
    evaluate_market_snapshot,
    evaluate_market_snapshot_with_validation,
)
from johanna.analysis.features import iso_utc

STALE = BASE + timedelta(seconds=0.2 + 5.001)


def _provisional():
    bars = trend_bars()
    last = bars[-1]
    bars[-1] = candle(
        last["t"], o=last["o"], h=last["h"], low=last["l"], c=last["c"], closure="clock_only"
    )
    return bars


CASES = [
    ("R1 insufficient history", lambda: history(660), FRESH, "R1"),
    ("R1 stale snapshot", trend_bars, STALE, "R1"),
    ("R2 provisional", _provisional, FRESH, "R2"),
    ("R4 conflicting", lambda: history(700), FRESH, "R4"),
    ("R6 market condition", lambda: history(661), FRESH, "R6"),
    ("R7 bullish", trend_bars, FRESH, "R7"),
    ("R7 bearish", lambda: trend_bars(sign=-1), FRESH, "R7"),
]


@pytest.fixture
def t4_spy(monkeypatch):
    """Records every T4 call and the object it returned, and every T5 call's validation."""
    record = {"t4_returned": [], "t5_received": []}
    real_t4, real_t5 = decision.validate_window, decision.analyse_window

    def t4(*args, **kwargs):
        out = real_t4(*args, **kwargs)
        record["t4_returned"].append(out)
        return out

    def t5(snap, validation, **kwargs):
        record["t5_received"].append(validation)
        return real_t5(snap, validation, **kwargs)

    monkeypatch.setattr(decision, "validate_window", t4)
    monkeypatch.setattr(decision, "analyse_window", t5)
    return record


@pytest.mark.parametrize(("label", "bars", "at", "rule"), CASES, ids=[c[0] for c in CASES])
def test_companion_result_is_identical_to_the_existing_function(label, bars, at, rule) -> None:
    snap = snapshot(bars())
    plain = evaluate_market_snapshot(snap, analysed_at=at)
    paired = evaluate_market_snapshot_with_validation(snap, analysed_at=at)
    assert set(paired) == {"result", "validation"}
    assert paired["result"] == plain
    assert json.dumps(paired["result"], sort_keys=True) == json.dumps(plain, sort_keys=True)
    assert plain["decision"]["rule"] == rule
    assert "validation" not in paired["result"]  # T4 is never put inside the T6 result


@pytest.mark.parametrize(("label", "bars", "at", "rule"), CASES, ids=[c[0] for c in CASES])
def test_returned_validation_is_the_single_t4_object_t5_and_t6_used(
    t4_spy, label, bars, at, rule
) -> None:
    paired = evaluate_market_snapshot_with_validation(snapshot(bars()), analysed_at=at)
    validation, result = paired["validation"], paired["result"]
    assert len(t4_spy["t4_returned"]) == 1  # T4 exactly once
    assert validation is t4_spy["t4_returned"][0]  # the exact object T4 returned
    assert t4_spy["t5_received"] == [validation]
    assert t4_spy["t5_received"][0] is validation  # the same object reached T5
    # Correspondence with the T6 result of the same evaluation.
    assert validation["snapshot_id"] == result["inputs"]["snapshot_id"]
    assert validation["evaluated_at_utc"] == result["evaluated_at_utc"] == iso_utc(at)
    window = validation["window"]
    assert {k: window[k] for k in ("count", "first_t", "last_t")} == result["inputs"]["window"]
    assert validation["verdict"]["state"] == result["inputs"]["validation_verdict"]
    assert result["decision"]["rule"] == rule


def test_existing_function_also_runs_t4_once(t4_spy) -> None:
    evaluate_market_snapshot(snapshot(trend_bars()), analysed_at=FRESH)
    assert len(t4_spy["t4_returned"]) == 1


@pytest.mark.parametrize(
    ("snap_factory", "at", "reason_start"),
    [
        (lambda: snapshot(trend_bars()), None, "no evaluation time given"),
        (lambda: snapshot(trend_bars()), datetime(2026, 11, 11, 14, 0, 31), "analysed_at must be"),
        (lambda: "not a snapshot", FRESH, "provenance check P1 failed"),
    ],
)
def test_refuse_before_t4_paths_return_no_validation(
    monkeypatch, snap_factory, at, reason_start
) -> None:
    def must_not_run(*args, **kwargs):
        raise AssertionError("T4 must not run on a refuse-before-T4 path")

    monkeypatch.setattr(decision, "validate_window", must_not_run)
    paired = evaluate_market_snapshot_with_validation(snap_factory(), analysed_at=at)
    assert paired["validation"] is None
    assert paired["result"]["decision"]["rule"] == "R1"
    assert paired["result"]["decision"]["reasons"][0].startswith(reason_start)
    assert paired["result"] == evaluate_market_snapshot(snap_factory(), analysed_at=at)


def test_existing_public_signature_is_unchanged() -> None:
    import inspect

    expected = [
        "snapshot",
        "analysed_at",
        "required_count",
        "excluded_opens",
        "peers",
        "parameters",
    ]
    for fn in (evaluate_market_snapshot, evaluate_market_snapshot_with_validation):
        params = inspect.signature(fn).parameters
        assert list(params) == expected
        assert params["analysed_at"].default is inspect.Parameter.empty
