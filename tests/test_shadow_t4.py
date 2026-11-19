"""P6.1 T4 integration correction and historical-defect diagnostics.

Envelopes, snapshots, closure labels and T4 verdicts all come from the real pipeline: the real
``MarketAnalysisService.analyse_background`` over real T2 snapshots (T4 -> T5 -> T6), driven by
the real scheduler. Temporary journals only.
"""

from __future__ import annotations

import copy
from contextlib import closing
from datetime import timedelta

import pytest
from risk_fixtures import PRODUCTION_COSTS, propose
from t6_fixtures import trend_bars
from test_browser_analysis import ENVELOPES, _provisional
from test_journal_store import raw, started
from test_market_analysis import FakeLive, FixedClock, service
from test_window_validation import snapshot

from johanna import shadow_evaluation as se
from johanna.adapters.mt5 import ServerClock
from johanna.decision_journal import DecisionJournal
from johanna.shadow_evaluation import (
    ShadowEvaluationService,
    analysis_gates,
    parse_utc,
    t4_state,
)


# ====================================================================== t4_state / gates (pure)
@pytest.mark.parametrize(
    ("name", "state", "result"),
    [
        ("R7", "accepted", "PASS"),
        ("R6", "accepted", "PASS"),
        ("R4", "accepted", "PASS"),
        ("R2", "provisional", "FAIL"),
        ("R1_history", "rejected", "FAIL"),
        ("R1_stale", "rejected", "FAIL"),
    ],
)
def test_t4_gate_maps_the_real_verdict_state(name, state, result) -> None:
    env = ENVELOPES[name]
    assert env["window_validation"]["verdict"]["state"] == state  # the real T4 field
    assert t4_state(env) == state
    gate = next(g for g in analysis_gates(env) if g["gate"] == "T4.window_validation")
    assert (gate["result"], gate["reason_code"]) == (result, state)
    assert gate["detail"] == (", ".join(env["window_validation"]["reason_codes"]) or None)


def test_t4_not_run_and_missing_state_are_explicit() -> None:
    env = copy.deepcopy(ENVELOPES["R7"])
    env["window_validation"] = {"available": False, "reason": "T4 did not run: x"}
    assert t4_state(env) == "not_run"
    env = copy.deepcopy(ENVELOPES["R7"])
    del env["window_validation"]["verdict"]["state"]
    gate = next(g for g in analysis_gates(env) if g["gate"] == "T4.window_validation")
    assert (gate["result"], gate["reason_code"]) == ("FAIL", "state_missing")


# ====================================================================== real scheduler cycles
class KeyedRisk:
    """Real Phase 4 output (production costs) for the requested key."""

    def __init__(self) -> None:
        self.calls = 0

    def assess(self, symbol, timeframe, env):
        self.calls += 1
        return propose(env=env, symbol=symbol, timeframe=timeframe, costs=PRODUCTION_COSTS)


def _real_scheduler(tmp_path, bars, *, delay=0.8, monkeypatch):
    """The real analysis service over a real T2 snapshot of ``bars`` (EURUSD H1 only)."""
    monkeypatch.setattr(se, "PAIRS", {"EURUSD": "EURUSDc"})
    monkeypatch.setattr(se, "TIMEFRAMES", ("H1",))
    # T1 always labels a bar with its close time; the T4 fixture candles leave it empty
    bars = [{**b, "close_t": se.iso_z(parse_utc(b["t"]) + timedelta(hours=1))} for b in bars]
    snap = snapshot(bars)
    read = parse_utc(snap["bars"]["retrieved_at_utc"])
    live = FakeLive(lambda s, t, c, n: copy.deepcopy(snap))
    analysis = service(live, FixedClock(read + timedelta(seconds=delay)))
    env, _ = analysis.analyse_background("EURUSD", "H1")  # probe: the window it analyses
    items = snap["bars"]["items"]
    close = parse_utc(items[-1]["close_t"])  # newest candle of the snapshot
    clock = {"now": close - timedelta(minutes=1)}
    store = started(tmp_path, clock=lambda: clock["now"])
    journal = DecisionJournal(store, clock=lambda: clock["now"])
    scheduler = ShadowEvaluationService(
        store, journal, analysis, KeyedRisk(), ServerClock(), clock=lambda: clock["now"]
    )
    scheduler.set_enabled(True)
    clock["now"] = close + timedelta(seconds=30)
    return scheduler, clock, env, items[-1]


def _cycle(scheduler) -> dict:
    rows = scheduler.cycles()["evaluations"]
    assert len(rows) == 1
    return scheduler.cycle(rows[0]["id"])["evaluation"]


def _t4_gate(cycle) -> dict:
    gates = [g for g in cycle["gates"] if g["gate"] == "T4.window_validation"]
    assert len(gates) == 1
    return gates[0]


def test_accepted_verdict_completes_with_pass_and_data_quality(tmp_path, monkeypatch) -> None:
    scheduler, _, env, candle = _real_scheduler(tmp_path, trend_bars(), monkeypatch=monkeypatch)
    assert candle["closure"] == "tick_observed" and t4_state(env) == "accepted"
    scheduler.run_once()
    cycle = _cycle(scheduler)
    assert cycle["status"] == "COMPLETED" and cycle["data_quality"] == "accepted"
    assert (cycle["t6_outcome"], cycle["t6_rule"]) == ("TRADE_CANDIDATE", "R7")
    assert cycle["risk_status"] == "UNVERIFIED"  # production costs: still unverified
    gate = _t4_gate(cycle)
    assert (gate["result"], gate["reason_code"]) == ("PASS", "accepted")
    diag = scheduler.diagnostics()
    assert diag["t4_verdicts"] == {"accepted": 1}
    assert diag["historical_recording_defects"]["t4_gate_empty_reason_code"]["count"] == 0
    assert not [r for r in diag["strategy_and_data_failures"] if r["gate"].startswith("T4.")]


def test_rejected_verdict_completes_with_fail_and_is_a_genuine_failure(tmp_path, monkeypatch):
    # analysed 5.001 s after the bars were read: T2 stale -> T4 rejected -> T6 R1
    scheduler, _, env, candle = _real_scheduler(
        tmp_path, trend_bars(), delay=5.001, monkeypatch=monkeypatch
    )
    assert candle["closure"] == "tick_observed" and t4_state(env) == "rejected"
    scheduler.run_once()
    cycle = _cycle(scheduler)
    assert cycle["status"] == "COMPLETED" and cycle["data_quality"] == "rejected"
    assert (cycle["t6_outcome"], cycle["t6_rule"], cycle["risk_status"]) == ("NO_TRADE", "R1", None)
    gate = _t4_gate(cycle)
    assert (gate["result"], gate["reason_code"]) == ("FAIL", "rejected")
    diag = scheduler.diagnostics()
    assert diag["t4_verdicts"] == {"rejected": 1}
    failures = {(r["gate"], r["result"]): r["count"] for r in diag["strategy_and_data_failures"]}
    assert failures[("T4.window_validation", "FAIL")] == 1  # counted as a genuine failure
    assert diag["historical_recording_defects"]["t4_gate_empty_reason_code"]["count"] == 0


def test_provisional_window_never_completes_as_an_opportunity(tmp_path, monkeypatch) -> None:
    scheduler, clock, env, candle = _real_scheduler(
        tmp_path, _provisional(), monkeypatch=monkeypatch
    )
    assert t4_state(env) == "provisional" and candle["closure"] == "clock_only"
    scheduler.run_once()
    cycle = _cycle(scheduler)
    assert (cycle["status"], cycle["reason_code"]) == ("PENDING", "closure_unconfirmed")
    clock["now"] += timedelta(minutes=10)
    scheduler.run_once()
    cycle = _cycle(scheduler)
    assert (cycle["status"], cycle["reason_code"]) == ("SKIPPED", "closure_unconfirmed")
    assert cycle["data_quality"] is None and cycle["decision_id"] is None
    assert not [g for g in cycle["gates"] if g["gate"] == "T4.window_validation"]


# ====================================================================== historical defects
def _legacy_cycle(tmp_path, monkeypatch):
    """Reproduce the defective build's recording: an empty T4 state (it read 'status')."""
    scheduler, clock, _, _ = _real_scheduler(tmp_path, trend_bars(), monkeypatch=monkeypatch)
    monkeypatch.setattr(se, "t4_state", lambda env: None)
    scheduler.run_once()
    monkeypatch.undo()  # the corrected code again, for everything that follows
    monkeypatch.setattr(se, "PAIRS", {"EURUSD": "EURUSDc"})
    monkeypatch.setattr(se, "TIMEFRAMES", ("H1",))
    return scheduler, clock


def _snapshot_rows(tmp_path):
    with closing(raw(tmp_path)) as conn:
        gates = [tuple(r) for r in conn.execute("SELECT * FROM gate_outcome ORDER BY rowid")]
        cycles = [tuple(r) for r in conn.execute("SELECT * FROM evaluation_cycle ORDER BY rowid")]
        audit = [tuple(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY seq")]
    return gates, cycles, audit


def test_legacy_records_are_flagged_excluded_and_never_rewritten(tmp_path, monkeypatch) -> None:
    scheduler, clock = _legacy_cycle(tmp_path, monkeypatch)
    legacy = _cycle(scheduler)
    gate = _t4_gate(legacy)
    assert (gate["result"], gate["reason_code"], legacy["data_quality"]) == ("FAIL", None, None)
    gates_before, cycles_before, audit_before = _snapshot_rows(tmp_path)

    diag = scheduler.diagnostics()
    defects = diag["historical_recording_defects"]["t4_gate_empty_reason_code"]
    assert defects["count"] == 1 and defects["evaluation_ids"] == [legacy["id"]]
    assert not [r for r in diag["strategy_and_data_failures"] if r["gate"].startswith("T4.")]
    assert diag["t4_verdicts"] == {}  # its verdict is unknown, never recalculated

    # the corrected build then records a new window correctly, next to the legacy row
    clock["now"] += timedelta(hours=1)
    scheduler.run_once()
    diag = scheduler.diagnostics()
    assert diag["historical_recording_defects"]["t4_gate_empty_reason_code"]["count"] == 1
    gates_after, cycles_after, audit_after = _snapshot_rows(tmp_path)
    assert gates_after[: len(gates_before)] == gates_before  # legacy gate rows untouched
    assert cycles_after[: len(cycles_before)] == cycles_before  # legacy cycle untouched
    assert audit_after[: len(audit_before)] == audit_before  # audit history untouched
    assert scheduler.store.verify_audit_chain()["valid"] is True


def test_corrected_records_are_never_classified_as_legacy_defects(tmp_path, monkeypatch) -> None:
    scheduler, _, _, _ = _real_scheduler(tmp_path, trend_bars(), monkeypatch=monkeypatch)
    scheduler.run_once()
    cycle = _cycle(scheduler)
    # an empty-reason T4 row on a cycle whose data_quality is recorded lacks the evidence
    with closing(raw(tmp_path)) as conn:
        conn.execute(
            "INSERT INTO gate_outcome(id, evaluation_id, seq, stage, gate, result, reason_code, "
            "detail, recorded_utc) VALUES ('X1', ?, 99, 'data', 'T4.window_validation', 'FAIL', "
            "NULL, NULL, 't')",
            (cycle["id"],),
        )
    diag = scheduler.diagnostics()
    assert diag["historical_recording_defects"]["t4_gate_empty_reason_code"]["count"] == 0
