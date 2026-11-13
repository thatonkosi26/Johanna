"""T6 decision engine (revision 2.5 §1.4-§1.6, §8): provenance P1-P10, entry points, R1-R7,
output contract, no-lookahead, determinism and adversarial inputs.

Everything runs through the real T2 -> T4 -> T5 code on synthetic histories (no MT5, no files).
"""

from __future__ import annotations

import copy
import inspect
import json
import time

import pytest
from t6_fixtures import FRESH, pipeline, reflect_bars, trend_bars
from test_analysis_engine import history
from test_window_validation import candle, snapshot

import johanna.analysis.decision as decision
from johanna.analysis.decision import (
    _evaluate,
    evaluate_market,
    evaluate_market_snapshot,
)
from johanna.analysis.engine import analyse_window
from johanna.analysis.window import validate_window

BANNED = ("buy", "sell", "entry", "stop_loss", "take_profit", "target", "lot", "position size")


@pytest.fixture(scope="module")
def bullish():
    return pipeline(trend_bars())


@pytest.fixture(scope="module")
def bearish():
    return pipeline(trend_bars(sign=-1))


def fresh(inputs):
    return copy.deepcopy(inputs)


def failed_checks(result):
    return [c["id"] for c in result["inputs"]["provenance"]["checks"] if not c["passed"]]


def rule(result):
    return result["decision"]["rule"]


# ---------------------------------------------------------------- baseline outcomes
def test_bullish_trend_history_is_a_trade_candidate(bullish) -> None:
    r = evaluate_market(*bullish)
    assert (r["decision"]["outcome"], rule(r)) == ("TRADE_CANDIDATE", "R7"), r["decision"]
    assert r["opportunity"]["direction"] == "bullish"
    assert r["decision"]["actionable"] is False
    assert r["inputs"]["provenance"]["passed"] is True
    assert r["inputs"]["consistency_check"]["passed"] is True
    assert r["market_condition"]["understood"] is True


def test_bearish_trend_history_is_a_trade_candidate(bearish) -> None:
    r = evaluate_market(*bearish)
    assert rule(r) == "R7" and r["opportunity"]["direction"] == "bearish"


def test_reflected_prices_mirror_the_whole_evaluation(bullish) -> None:
    up = evaluate_market(*bullish)
    down = evaluate_market(*pipeline(reflect_bars(trend_bars())))
    assert rule(down) == rule(up) == "R7"
    assert down["opportunity"]["direction"] == "bearish"
    assert [c["result"] for c in down["opportunity"]["checklist"]] == [
        c["result"] for c in up["opportunity"]["checklist"]
    ]
    swap = {"resistance": "support", "above": "below"}
    up_tc6 = up["opportunity"]["checklist"][5]["evidence_id"]
    expected = "_".join(swap.get(part, part) for part in up_tc6.split("_"))
    assert down["opportunity"]["checklist"][5]["evidence_id"] == expected


# ---------------------------------------------------------------- R1-R7
def test_r1_rejected_validation() -> None:
    bars = history(50)
    bars[10] = candle(bars[10]["t"], issues=["non_positive_price"])
    r = evaluate_market(*pipeline(bars))
    assert rule(r) == "R1" and r["gate"]["reasons"][0] == "T5 analysis status 'rejected'"
    assert r["market_condition"] is None and r["opportunity"] is None


def test_r1_consistency_guard_detects_a_tampered_indicator(bullish) -> None:
    snap, v, a = fresh(bullish)
    a["indicators"]["ema"][0]["value"] += 1e-9
    r = evaluate_market(snap, v, a)
    assert rule(r) == "R1" and r["inputs"]["provenance"]["passed"] is True
    assert r["gate"]["reasons"][0].startswith("consistency check failed: EMA20")


def test_r2_provisional_is_diagnostic_only() -> None:
    bars = trend_bars()
    last = bars[-1]
    bars[-1] = candle(
        last["t"], o=last["o"], h=last["h"], low=last["l"], c=last["c"], closure="clock_only"
    )
    r = evaluate_market(*pipeline(bars))
    assert (r["decision"]["outcome"], rule(r)) == ("NO_TRADE", "R2")
    assert (
        r["market_condition"]["trusted"] is False and r["market_condition"]["understood"] is False
    )
    assert all(not d["trusted"] for d in r["market_condition"]["dimensions"].values())
    assert r["opportunity"]["status"] == "not_evaluable" and r["opportunity"]["checklist"] == []


def test_r3_core_conditions_undetermined() -> None:
    r = evaluate_market(*pipeline(history(100)))
    assert (r["decision"]["outcome"], rule(r)) == ("NO_TRADE", "R3") and r["market_condition"][
        "understood"
    ] is False


def test_r4_conflicting_bias() -> None:
    r = evaluate_market(*pipeline(history(700)))
    assert (r["decision"]["outcome"], rule(r)) == ("NO_TRADE", "R4") and r["market_condition"][
        "summary"
    ]["directional_bias"] == "conflicting"


def _swing_tampered(inputs, kind):
    snap, v, a = fresh(inputs)
    confirmed = a["structure"]["swings"]["confirmed"]
    highs = sorted((p for p in confirmed if p["type"] == "high"), key=lambda p: p["pivot_t"])
    lows = sorted((p for p in confirmed if p["type"] == "low"), key=lambda p: p["pivot_t"])
    if kind == "opposite":
        highs[-1]["price"] = highs[-2]["price"] - 0.001
        lows[-1]["price"] = lows[-2]["price"] - 0.001
    else:  # leave a single confirmed low
        keep = lows[-1]
        a["structure"]["swings"]["confirmed"] = [
            p for p in confirmed if p["type"] == "high" or p is keep
        ]
    return snap, v, a


def test_r4_complete_opposite_swing_sequence(bullish) -> None:
    r = evaluate_market(*_swing_tampered(bullish, "opposite"))
    assert (r["decision"]["outcome"], rule(r)) == ("NO_TRADE", "R4")
    assert r["decision"]["reasons"] == ["confirmed swing structure opposes the bullish bias"]
    assert r["opportunity"]["triggered_vetoes"] == ["V2_swing_context_conflict"]


def test_missing_swing_context_is_not_a_conflict(bullish) -> None:
    r = evaluate_market(*_swing_tampered(bullish, "missing"))
    assert rule(r) == "R7"
    assert r["market_condition"]["summary"]["swing_structure"] == "unavailable"
    assert any("swing context unavailable" in x for x in r["limitations"])


def test_r5_ema200_unavailable_at_660_and_evaluable_at_661() -> None:
    r = evaluate_market(*pipeline(history(660)))
    assert (r["decision"]["outcome"], rule(r)) == ("NO_TRADE", "R5")
    assert r["opportunity"]["required_inputs"]["unavailable"][0]["input"] == "EMA200"
    assert rule(evaluate_market(*pipeline(history(661)))) == "R6"


def test_r6_understood_but_not_qualified() -> None:
    r = evaluate_market(*pipeline(history(661)))
    assert r["decision"]["outcome"] == "MARKET_CONDITION"
    assert "failed check: TC1_regime" in r["decision"]["reasons"]


def test_precedence_r4_before_r5_and_r2_before_r3() -> None:
    r = evaluate_market(*pipeline(history(300, seed=2)))
    assert (r["decision"]["outcome"], rule(r)) == ("NO_TRADE", "R4")  # EMA200 also unavailable
    assert not r["opportunity"]["required_inputs"]["all_available"]
    bars = history(100)
    last = bars[-1]
    bars[-1] = candle(
        last["t"], o=last["o"], h=last["h"], low=last["l"], c=last["c"], closure="clock_only"
    )
    assert rule(evaluate_market(*pipeline(bars))) == "R2"  # provisional and undetermined


def test_trusted_empty_resistance_side_passes_tc6_with_explicit_evidence(bullish) -> None:
    snap, v, a = fresh(bullish)
    sr = a["structure"]["support_resistance"]
    sr["resistance"] = []
    sr["resistance_reason"] = "no confirmed swing highs"
    r = evaluate_market(snap, v, a)
    tc6 = r["opportunity"]["checklist"][5]
    assert (tc6["result"], tc6["evidence_id"]) == ("pass", "no_resistance_levels")
    assert any("does not establish" in x for x in r["limitations"])


# ---------------------------------------------------------------- provenance P1-P10
def _mutate(inputs, fn):
    snap, v, a = fresh(inputs)
    fn(snap, v, a)
    return evaluate_market(snap, v, a)


def _sync_window(v, a, **changes):
    v["window"].update(changes)
    a["validation"] = copy.deepcopy(v)
    a["window"] = {k: v["window"][k] for k in ("count", "first_t", "last_t")}


@pytest.mark.parametrize(
    ("check", "fn"),
    [
        ("P1", lambda s, v, a: s.update(snapshot_id="other")),
        ("P1", lambda s, v, a: s.update(snapshot_id="")),
        ("P1", lambda s, v, a: s.update(snapshot_id=7)),
        ("P2", lambda s, v, a: s.update(symbol="GBPUSD")),
        ("P2", lambda s, v, a: a.update(symbol=None)),
        ("P3", lambda s, v, a: s.update(timeframe="H4")),
        ("P4", lambda s, v, a: a.update(broker_symbol="EURUSD.x")),
        ("P4", lambda s, v, a: (s.update(broker_symbol=""), a.update(broker_symbol=""))),
        ("P5", lambda s, v, a: a["validation"].update(note="edited")),
        ("P6", lambda s, v, a: a.update(validation_verdict="provisional")),
        ("P6", lambda s, v, a: a.update(status="provisional")),
        ("P7", lambda s, v, a: _sync_window(v, a, count=999)),
        ("P7", lambda s, v, a: _sync_window(v, a, first_t="2026-01-01T00:00:00Z")),
        ("P7", lambda s, v, a: _sync_window(v, a, candles=v["window"]["candles"][::-1])),
        ("P8", lambda s, v, a: a["window"].update(count=1)),
        ("P9", lambda s, v, a: s["bars"]["items"].pop(-1)),
        ("P10", lambda s, v, a: a["parameters"].update(rsi_period=10)),
        ("P10", lambda s, v, a: s.update(snapshot_version=2)),
        (
            "P10",
            lambda s, v, a: v.update(window_version=2) or a.update(validation=copy.deepcopy(v)),
        ),
    ],
)
def test_each_provenance_check_fails_alone(bullish, check, fn) -> None:
    r = _mutate(bullish, fn)
    assert failed_checks(r) == [check], r["inputs"]["provenance"]["checks"]
    assert rule(r) == "R1" and r["decision"]["outcome"] == "NO_TRADE"
    assert r["gate"]["reasons"][0].startswith(f"provenance check {check} failed")


def test_p7_rejects_an_out_of_order_window_even_when_synchronised(bullish) -> None:
    def fn(s, v, a):
        c = list(v["window"]["candles"])
        c[3], c[4] = c[4], c[3]
        _sync_window(v, a, candles=c)

    assert failed_checks(_mutate(bullish, fn)) == ["P7"]


def test_analysis_from_another_validation_of_the_same_snapshot(bullish) -> None:
    snap, v, _ = fresh(bullish)
    other = validate_window(snap, required_count=680, analysed_at=FRESH)
    r = evaluate_market(snap, v, analyse_window(snap, other))
    assert {"P5", "P8"} <= set(failed_checks(r)) and rule(r) == "R1"


def test_matching_indicator_values_are_not_provenance(bullish) -> None:
    snap, v, a = fresh(bullish)
    twin = dict(copy.deepcopy(snap), snapshot_id="identical-candles-different-snapshot")
    r = evaluate_market(twin, v, a)
    assert failed_checks(r) == ["P1"] and rule(r) == "R1"
    assert r["inputs"]["consistency_check"] is None  # never reached: provenance comes first


def test_provenance_failure_is_reported_before_consistency(bullish) -> None:
    snap, v, a = fresh(bullish)
    a["indicators"]["ema"][0]["value"] += 1.0
    a["broker_symbol"] = "other"
    r = evaluate_market(snap, v, a)
    assert all(x.startswith("provenance check") for x in r["gate"]["reasons"])


@pytest.mark.parametrize(
    "inputs",
    [
        (None, None, None),
        ({}, {}, {}),
        ("snapshot", [], 3),
    ],
)
def test_malformed_inputs_fail_every_check_without_raising(inputs) -> None:
    r = evaluate_market(*inputs)
    assert rule(r) == "R1" and len(failed_checks(r)) == 10
    assert r["decision"]["actionable"] is False


# ---------------------------------------------------------------- adversarial structure (R1)
@pytest.mark.parametrize(
    ("fn", "fragment"),
    [
        (lambda a: a["indicators"]["atr"].update(trusted="true"), "ATR.trusted is not a boolean"),
        (lambda a: a["indicators"]["atr"].update(trusted=False), "contradicts status"),
        (lambda a: a["indicators"]["ema"].pop(), "indicators.ema must hold periods"),
        (
            lambda a: a["structure"]["support_resistance"].update(resistance=[]),
            "resistance empty without resistance_reason",
        ),
        (
            lambda a: a["structure"]["support_resistance"]["support"][0].update(strength=True),
            "malformed support level",
        ),
        (
            lambda a: a["structure"]["swings"]["confirmed"][-1].update(
                confirmed_at_utc="2026-11-12T00:00:00Z"
            ),
            "after as_of (lookahead)",
        ),
        (lambda a: a["structure"].pop("swings"), "swings component missing"),
    ],
)
def test_plausible_but_malformed_analysis_is_rejected(bullish, fn, fragment) -> None:
    snap, v, a = fresh(bullish)
    fn(a)
    r = evaluate_market(snap, v, a)
    assert rule(r) == "R1" and r["inputs"]["provenance"]["passed"] is True
    assert any(fragment in reason for reason in r["gate"]["reasons"]), r["gate"]["reasons"]


def test_non_finite_candle_price_is_rejected(bullish) -> None:
    snap, v, a = fresh(bullish)
    snap["bars"]["items"][-3]["c"] = float("nan")
    r = evaluate_market(snap, v, a)
    assert rule(r) == "R1" and "non-finite prices" in r["gate"]["reasons"][0]


# ---------------------------------------------------------------- entry points (§1.5, §1.6)
def test_constructed_and_supplied_paths_agree_except_provenance() -> None:
    bars = trend_bars()
    built = evaluate_market_snapshot(snapshot(bars), required_count=len(bars), analysed_at=FRESH)
    supplied = evaluate_market(*pipeline(bars))
    assert built["inputs"]["provenance"]["mode"] == "constructed"
    assert built["inputs"]["provenance"]["content_binding"] == "by_construction"
    assert supplied["inputs"]["provenance"]["mode"] == "supplied"
    assert supplied["inputs"]["provenance"]["content_binding"] == "not_verified"
    for key in ("decision", "market_condition", "opportunity", "features", "as_of_utc"):
        assert built[key] == supplied[key], key
    assert [c["passed"] for c in built["inputs"]["provenance"]["checks"]] == [True] * 10


def test_constructed_default_window_is_661_candles() -> None:
    r = evaluate_market_snapshot(snapshot(trend_bars()), analysed_at=FRESH)
    assert r["inputs"]["window"]["count"] == 661


def test_caller_snapshot_is_never_modified_or_referenced() -> None:
    snap = snapshot(trend_bars())
    before = copy.deepcopy(snap)
    r = evaluate_market_snapshot(snap, required_count=700, analysed_at=FRESH)
    assert snap == before
    frozen = json.dumps(r, sort_keys=True)
    snap["bars"]["items"][-1]["c"] = 99.0
    snap["snapshot_id"] = "changed"
    assert json.dumps(r, sort_keys=True) == frozen


def test_caller_mutation_during_evaluation_cannot_reach_the_copy(monkeypatch) -> None:
    snap = snapshot(trend_bars())
    expected = evaluate_market_snapshot(copy.deepcopy(snap), required_count=700, analysed_at=FRESH)
    real = decision.validate_window

    def mutating(work, **kwargs):
        snap["bars"]["items"][-1]["c"] = 99.0  # the caller's object changes mid-evaluation
        return real(work, **kwargs)

    monkeypatch.setattr(decision, "validate_window", mutating)
    assert evaluate_market_snapshot(snap, required_count=700, analysed_at=FRESH) == expected


def test_constructed_path_still_runs_provenance(monkeypatch) -> None:
    real = decision.analyse_window

    def faulty(work, validation):
        return dict(real(work, validation), snapshot_id="injected-fault")

    monkeypatch.setattr(decision, "analyse_window", faulty)
    r = evaluate_market_snapshot(snapshot(trend_bars()), analysed_at=FRESH)
    assert rule(r) == "R1" and failed_checks(r) == ["P1"]
    assert r["inputs"]["provenance"]["mode"] == "constructed"


def test_constructed_path_with_a_non_mapping_snapshot() -> None:
    r = evaluate_market_snapshot("not a snapshot", analysed_at=FRESH)
    assert rule(r) == "R1" and r["inputs"]["provenance"]["mode"] == "constructed"


def test_analysed_at_is_passed_through_and_no_clock_is_read() -> None:
    from datetime import UTC, datetime

    at = datetime(2026, 11, 11, 14, 0, 32, tzinfo=UTC)  # 2 s after assembly: still live
    r = evaluate_market_snapshot(snapshot(trend_bars()), analysed_at=at)
    assert r["evaluated_at_utc"] == "2026-11-11T14:00:32Z" and rule(r) == "R7"
    late = evaluate_market_snapshot(
        snapshot(trend_bars()), analysed_at=datetime(2026, 11, 11, 15, tzinfo=UTC)
    )
    assert rule(late) in ("R1", "R2")  # stale snapshot: never a candidate


def test_provenance_mode_cannot_be_chosen_by_callers() -> None:
    for fn in (evaluate_market, evaluate_market_snapshot):
        assert not any("provenance" in name for name in inspect.signature(fn).parameters)
    with pytest.raises(ValueError, match="unknown provenance mode"):
        _evaluate({}, {}, {}, provenance_mode="by_construction")


# ---------------------------------------------------------------- contract and properties
def _all_results(bullish, bearish):
    return [
        evaluate_market(*bullish),
        evaluate_market(*bearish),
        evaluate_market(*pipeline(history(100))),
        evaluate_market(*pipeline(history(660))),
        evaluate_market(*pipeline(history(661))),
        evaluate_market(*pipeline(history(700))),
        evaluate_market(None, None, None),
    ]


def test_every_outcome_is_non_actionable_strict_json_and_signal_free(bullish, bearish) -> None:
    outcomes = set()
    for r in _all_results(bullish, bearish):
        assert r["decision"]["actionable"] is False
        outcomes.add(r["decision"]["outcome"])
        text = json.dumps(r, allow_nan=False).lower()
        for word in BANNED:
            assert word not in text, word
    assert outcomes == {"NO_TRADE", "MARKET_CONDITION", "TRADE_CANDIDATE"}


def test_output_contract_keys(bullish) -> None:
    r = evaluate_market(*bullish)
    assert set(r) == {
        "decision_version",
        "symbol",
        "broker_symbol",
        "timeframe",
        "as_of_utc",
        "evaluated_at_utc",
        "inputs",
        "gate",
        "market_condition",
        "opportunity",
        "features",
        "decision",
        "parameters",
        "limitations",
        "note",
    }
    assert set(r["decision"]) == {"outcome", "rule", "reasons", "actionable"}
    for c in r["opportunity"]["checklist"]:
        assert {
            "id",
            "observed",
            "threshold",
            "comparison",
            "source",
            "t",
            "detail",
            "result",
            "evidence_id",
        } <= set(c)


def test_repeated_evaluation_is_identical_and_inputs_are_untouched(bullish) -> None:
    snap, v, a = fresh(bullish)
    before = copy.deepcopy((snap, v, a))
    outputs = {json.dumps(evaluate_market(snap, v, a), sort_keys=True) for _ in range(10)}
    assert len(outputs) == 1
    assert (snap, v, a) == before


def test_prefix_results_do_not_depend_on_later_candles() -> None:
    bars = trend_bars(720)
    prefix = bars[:700]
    first = evaluate_market(*pipeline(prefix))
    altered_future = prefix + [
        candle(b["t"], o=b["o"] * 1.01, h=b["h"] * 1.02, low=b["l"] * 1.0, c=b["c"] * 1.015)
        for b in bars[700:]
    ]
    evaluate_market(*pipeline(altered_future))  # evaluating a longer history leaves no state
    again = evaluate_market(*pipeline(altered_future[:700]))
    assert json.dumps(first, sort_keys=True) == json.dumps(again, sort_keys=True)


def test_pending_pivots_never_affect_the_decision(bullish) -> None:
    snap, v, a = fresh(bullish)
    base = evaluate_market(snap, v, a)
    a["structure"]["swings"]["pending"].append(
        {"type": "low", "price": 0.5, "pivot_t": "2026-11-11T11:00:00Z", "confirmed_at_utc": None}
    )
    r = evaluate_market(snap, v, a)
    assert r["decision"] == base["decision"] and r["opportunity"] == base["opportunity"]


def test_performance_on_the_1000_candle_maximum() -> None:
    snap = snapshot(trend_bars(1000))
    started = time.perf_counter()
    r = evaluate_market_snapshot(snap, required_count=1000, analysed_at=FRESH)
    elapsed = time.perf_counter() - started
    assert r["inputs"]["window"]["count"] == 1000
    assert elapsed < 2.0, elapsed
