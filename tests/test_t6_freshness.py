"""T6 freshness: an explicit evaluation time is required, and T2/T4 judge freshness at it.

Uses the existing T2 rule unchanged: snapshot age = evaluation time - bars retrieval time, rounded
to the millisecond; refused when age > 5.0 s (``max_snapshot_age_seconds``) or negative. The
fixtures retrieve the bars at BASE + 0.2 s, so the exact boundary is BASE + 5.2 s.
"""

from __future__ import annotations

import inspect
import json
from datetime import datetime, timedelta

import pytest
from t6_fixtures import FRESH, pipeline, trend_bars
from test_window_validation import BASE, snapshot

import johanna.analysis.decision as decision
from johanna.analysis.decision import evaluate_market, evaluate_market_snapshot
from johanna.analysis.engine import analyse_window
from johanna.analysis.snapshot import DEFAULT_LIMITS
from johanna.analysis.window import validate_window

RETRIEVED = BASE + timedelta(seconds=0.2)  # bars.retrieved_at_utc in the fixtures


def at(age_seconds: float) -> datetime:
    return RETRIEVED + timedelta(seconds=age_seconds)


def run(age_seconds: float, *, sign: int = 1):
    return evaluate_market_snapshot(snapshot(trend_bars(sign=sign)), analysed_at=at(age_seconds))


def test_the_existing_t2_limit_is_used_unchanged() -> None:
    assert DEFAULT_LIMITS.max_snapshot_age_seconds == 5.0
    assert snapshot(trend_bars())["bars"]["retrieved_at_utc"] == "2026-11-11T14:00:30.200000Z"


# ---------------------------------------------------------------- missing or invalid time
def test_analysed_at_is_a_required_argument() -> None:
    param = inspect.signature(evaluate_market_snapshot).parameters["analysed_at"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        evaluate_market_snapshot(snapshot(trend_bars()))  # type: ignore[call-arg]


def test_explicit_none_gives_no_trade_without_evaluating_anything(monkeypatch) -> None:
    def must_not_run(*args, **kwargs):
        raise AssertionError("T4/T5 must not run without an evaluation time")

    monkeypatch.setattr(decision, "validate_window", must_not_run)
    monkeypatch.setattr(decision, "analyse_window", must_not_run)
    r = evaluate_market_snapshot(snapshot(trend_bars()), analysed_at=None)
    assert (r["decision"]["outcome"], r["decision"]["rule"]) == ("NO_TRADE", "R1")
    assert r["decision"]["reasons"][0].startswith("no evaluation time given")
    assert r["decision"]["actionable"] is False
    assert r["inputs"]["evaluation_time"]["passed"] is False
    assert (
        r["inputs"]["provenance"]["checks"] == [] and r["inputs"]["provenance"]["passed"] is False
    )
    assert r["inputs"]["provenance"]["mode"] == "constructed"
    assert r["market_condition"] is None and r["opportunity"] is None
    json.dumps(r, allow_nan=False)


@pytest.mark.parametrize("bad", [datetime(2026, 11, 11, 14, 0, 31), "2026-11-11T14:00:31Z", 0])
def test_naive_or_non_datetime_times_are_refused(bad) -> None:
    r = evaluate_market_snapshot(snapshot(trend_bars()), analysed_at=bad)
    assert r["decision"]["rule"] == "R1"
    assert "timezone-aware datetime" in r["decision"]["reasons"][0]


# ---------------------------------------------------------------- the freshness boundary
def test_fresh_snapshot_with_an_explicit_time() -> None:
    r = run(0.8)
    assert (r["decision"]["outcome"], r["decision"]["rule"]) == ("TRADE_CANDIDATE", "R7")
    assert r["evaluated_at_utc"] == "2026-11-11T14:00:31Z"
    assert r["inputs"]["evaluation_time"] == {
        "passed": True,
        "evaluated_at_utc": "2026-11-11T14:00:31Z",
        "re_evaluated": True,
        "problems": [],
    }


def test_exactly_at_the_boundary_is_still_fresh() -> None:
    assert run(5.0)["decision"]["rule"] == "R7"  # age 5.000 s: not > 5.0


def test_t2_millisecond_resolution_is_respected() -> None:
    # T2 rounds ages to 3 decimals: 5.0004 s is 5.0 (fresh); 5.001 s is the first stale value.
    assert run(5.0004)["decision"]["rule"] == "R7"
    assert run(5.001)["decision"]["rule"] == "R1"


def test_just_beyond_the_boundary_is_refused() -> None:
    r = run(5.001)
    assert (r["decision"]["outcome"], r["decision"]["rule"]) == ("NO_TRADE", "R1")
    assert r["decision"]["reasons"][0] == "T5 analysis status 'rejected'"
    snap = snapshot(trend_bars())
    validation = validate_window(snap, required_count=661, analysed_at=at(5.001))
    snapshot_reason = next(
        x for x in validation["verdict"]["reasons"] if x["code"] == "snapshot_refused"
    )
    assert snapshot_reason["snapshot_reasons"] == ["snapshot_too_old"]


def test_evaluation_before_the_bars_were_retrieved_is_refused() -> None:
    assert run(-0.001)["decision"]["rule"] == "R1"


@pytest.mark.parametrize("sign", [1, -1])
@pytest.mark.parametrize("age", [5.001, 6.0, 60.0, 3600.0, 86400.0, 30 * 86400.0])
def test_a_stale_snapshot_never_produces_a_candidate_or_condition(sign, age) -> None:
    assert run(0.8, sign=sign)["decision"]["outcome"] == "TRADE_CANDIDATE"  # same data, fresh
    r = run(age, sign=sign)
    assert r["decision"]["outcome"] == "NO_TRADE"
    assert r["market_condition"] is None and r["opportunity"] is None


# ---------------------------------------------------------------- supplied inputs
def test_supplied_validation_without_an_evaluation_time_is_refused() -> None:
    snap = snapshot(trend_bars())
    validation = validate_window(snap, required_count=700)  # stored eligibility only
    r = evaluate_market(snap, validation, analyse_window(snap, validation))
    assert r["decision"]["rule"] == "R1"
    assert r["inputs"]["provenance"]["passed"] is True
    assert r["decision"]["reasons"][0].startswith(
        "evaluation time: snapshot freshness was not re-evaluated"
    )


def test_supplied_analysis_time_must_match_the_freshness_evaluation_time() -> None:
    snap = snapshot(trend_bars())
    validation = validate_window(snap, required_count=700, analysed_at=FRESH)
    later = analyse_window(snap, validation, analysed_at_utc="2026-11-12T00:00:00Z")
    r = evaluate_market(snap, validation, later)
    assert r["decision"]["rule"] == "R1"
    assert "differs from the freshness evaluation time" in r["decision"]["reasons"][0]


def test_supplied_stale_validation_is_refused_too() -> None:
    r = evaluate_market(*pipeline(trend_bars(), analysed_at=at(6.0)))
    assert (r["decision"]["outcome"], r["decision"]["rule"]) == ("NO_TRADE", "R1")


# ---------------------------------------------------------------- determinism
def test_identical_inputs_and_time_give_identical_output() -> None:
    snap = snapshot(trend_bars())
    outputs = {
        json.dumps(evaluate_market_snapshot(snap, analysed_at=at(2.0)), sort_keys=True)
        for _ in range(10)
    }
    assert len(outputs) == 1
    assert json.loads(outputs.pop())["evaluated_at_utc"] == "2026-11-11T14:00:32.200000Z"
