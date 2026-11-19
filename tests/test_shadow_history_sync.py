"""P6.1.1: missing-slot diagnostics and bounded history-sync retries.

Every verdict comes from the real pipeline: the real ``MarketAnalysisService`` over real T2
snapshots (T4 -> T5 -> T6), driven by the real scheduler on a deterministic clock. The fake
broker returns a *new* snapshot on every read, stamped with the scheduler clock (as an MT5 read
would be), from a scripted sequence of histories. Temporary journals only.
"""

from __future__ import annotations

import copy
import json
from contextlib import closing
from datetime import timedelta

import pytest
from risk_fixtures import PRODUCTION_COSTS, propose
from t6_fixtures import trend_bars
from test_journal_store import raw, started
from test_market_analysis import FixedClock, service
from test_window_validation import snapshot

from johanna import shadow_evaluation as se
from johanna.adapters.mt5 import ServerClock
from johanna.decision_journal import DecisionJournal
from johanna.live_market import BACKGROUND_DEFERRED, MarketUnavailable
from johanna.market_analysis import MarketAnalysisService
from johanna.shadow_evaluation import (
    DATA_RETRY,
    DEADLINES,
    HISTORY_SLOTS_GATE,
    HISTORY_SYNC_GATE,
    ShadowEvaluationService,
    analysis_gates,
    history_slot_gates,
    history_sync_retryable,
    iso_z,
    parse_utc,
    t4_fail_codes,
)

H1 = timedelta(hours=1)


def _labelled(bars):
    """T1 always labels a bar with its close time; the fixture candles leave it empty."""
    return [{**b, "close_t": iso_z(parse_utc(b["t"]) + H1)} for b in bars]


COMPLETE = _labelled(trend_bars())  # 700 H1 candles, newest opens 2026-11-11T12:00Z
CLOSE = parse_utc(COMPLETE[-1]["close_t"])  # the window's candle close (13:00Z)


def without(*indices, bars=COMPLETE):
    drop = set(indices)
    return [b for i, b in enumerate(bars) if i not in drop]


ONE_GAP = without(300)  # 2026-10-19T20:00Z missing
GAP_SLOT = COMPLETE[300]["t"]
MULTI_GAP = without(300, 301, 302, 450)  # a run of three and a single slot
SHORT = COMPLETE[-650:]  # fewer closed candles than T4 requires: insufficient_history
SHORT_WITH_GAP = without(300, bars=SHORT)  # insufficient_history + the history-sync codes


# ====================================================================== real envelopes (pure)
def envelope_for(bars, *, tick=None, read=None):
    snap = snapshot(bars, tick_time=tick, read_at=read) if tick else snapshot(bars)
    analysed = parse_utc(snap["bars"]["retrieved_at_utc"]) + timedelta(seconds=0.5)
    live = type("Live", (), {"snapshot": lambda self, s, t, c: copy.deepcopy(snap)})()
    env, _ = service(live, FixedClock(analysed)).analyse_background("EURUSD", "H1")
    return env


def slot_details(rows):
    return [json.loads(r["detail"]) for r in rows]


def test_one_missing_candle_records_its_exact_timestamp() -> None:
    env = envelope_for(ONE_GAP)
    assert set(t4_fail_codes(env)) == {"gap_unknown", "closed_history_gap"}
    rows = history_slot_gates(env, result="FAIL", attempt=1)
    details = slot_details(rows)
    assert {r["reason_code"] for r in rows} == {"gap_unknown", "closed_history_gap"}
    for d in details:
        assert (d["kind"], d["first"], d["last"], d["count"]) == ("missing", GAP_SLOT, GAP_SLOT, 1)
        assert (d["symbol"], d["timeframe"], d["attempt"]) == ("EURUSD", "H1", 1)
        assert d["window"] == [
            env["window_validation"]["window"]["first_t"],
            env["window_validation"]["window"]["last_t"],
        ]
    assert {d["source"] for d in details} == {"T3", "E1"}


def test_multiple_missing_candles_are_all_recorded_as_exact_runs() -> None:
    env = envelope_for(MULTI_GAP)
    details = slot_details(history_slot_gates(env, result="FAIL", attempt=1))
    runs = {(d["source"], d["first"], d["last"], d["count"]) for d in details}
    t = [COMPLETE[i]["t"] for i in (300, 302, 450)]
    for source in ("T3", "E1"):
        assert (source, t[0], t[1], 3) in runs
        assert (source, t[2], t[2], 1) in runs
    assert len(details) == 4  # two runs, each from T3 and E1; nothing truncated or merged


def test_more_slots_than_t4_lists_are_explicit_never_silently_truncated() -> None:
    env = envelope_for(without(*range(300, 360)))  # 60 consecutive candles missing
    rows = history_slot_gates(env, result="FAIL", attempt=1)
    details = slot_details(rows)
    t3 = [d for d in details if d["source"] == "T3"]
    assert [(d["first"], d["last"], d["count"]) for d in t3] == [
        (COMPLETE[300]["t"], COMPLETE[359]["t"], 60)
    ]
    e1 = [d for d in details if d["source"] == "E1"]
    assert sum(d.get("count", 0) for d in e1) == 50  # T4 lists 50 ...
    assert [d["unlisted"] for d in e1 if "unlisted" in d] == [10]  # ... and says 10 more
    assert all(len(r["detail"]) <= 500 for r in rows)


def test_behind_tick_slots_are_distinguishable_from_missing_candles() -> None:
    full = snapshot(COMPLETE)
    env = envelope_for(
        COMPLETE[:-3],
        tick=parse_utc(full["tick"]["time_utc"]),
        read=parse_utc(full["tick"]["retrieved_at_utc"]),
    )
    assert t4_fail_codes(env) == ["closed_history_behind_tick"]
    (detail,) = slot_details(history_slot_gates(env, result="FAIL", attempt=1))
    assert detail["kind"] == "behind_tick"
    assert (detail["first"], detail["last"], detail["count"]) == (
        COMPLETE[-3]["t"],
        COMPLETE[-1]["t"],
        3,
    )
    assert (detail["after"], detail["expected"]) == (COMPLETE[-4]["t"], COMPLETE[-1]["t"])


@pytest.mark.parametrize("bars", [COMPLETE, ONE_GAP, MULTI_GAP, SHORT, SHORT_WITH_GAP])
def test_t4_verdict_and_reason_codes_are_unchanged(bars) -> None:
    env = envelope_for(bars)
    before = copy.deepcopy(env)
    gates = analysis_gates(env)
    history_slot_gates(env, result="FAIL", attempt=1)
    assert env == before  # diagnostics never alter the envelope
    t4 = next(g for g in gates if g["gate"] == "T4.window_validation")
    assert t4["reason_code"] == env["window_validation"]["verdict"]["state"]
    assert t4["detail"] == (", ".join(env["window_validation"]["reason_codes"]) or None)
    assert not [g for g in gates if g["gate"].startswith("T4.history")]  # added elsewhere


def test_retry_eligibility_is_exclusive() -> None:
    assert history_sync_retryable(envelope_for(ONE_GAP))
    assert history_sync_retryable(envelope_for(MULTI_GAP))
    assert not history_sync_retryable(envelope_for(COMPLETE))  # accepted
    assert not history_sync_retryable(envelope_for(SHORT))  # insufficient_history only
    mixed = envelope_for(SHORT_WITH_GAP)
    assert {"insufficient_history", "gap_unknown"} <= set(t4_fail_codes(mixed))
    assert not history_sync_retryable(mixed)


# ====================================================================== scheduler harness
class Broker:
    """Scripted MT5 history: each read returns the next history (the last one repeats) as a
    new T2 snapshot read just now, or raises the scripted MarketUnavailable."""

    def __init__(self, clock, script) -> None:
        self.clock, self.script, self.reads = clock, list(script), []

    def snapshot(self, symbol, timeframe, count):
        now = self.clock["now"]
        step = self.script[min(len(self.reads), len(self.script) - 1)]
        self.reads.append(now)
        if isinstance(step, Exception):
            raise step
        snap = snapshot(
            step,
            tick_time=now - timedelta(seconds=1),
            read_at=now - timedelta(seconds=0.5),  # bars read 0.2 s later
        )
        snap["snapshot_id"] = f"read-{len(self.reads)}"
        return snap


class KeyedRisk:
    def assess(self, symbol, timeframe, env):
        return propose(env=env, symbol=symbol, timeframe=timeframe, costs=PRODUCTION_COSTS)


def scheduler(tmp_path, monkeypatch, script):
    monkeypatch.setattr(se, "PAIRS", {"EURUSD": "EURUSDc"})
    monkeypatch.setattr(se, "TIMEFRAMES", ("H1",))
    clock = {"now": CLOSE - timedelta(minutes=1)}
    broker = Broker(clock, script)
    analysis = MarketAnalysisService(broker, clock=lambda: clock["now"])
    store = started(tmp_path, clock=lambda: clock["now"])
    service_ = ShadowEvaluationService(
        store,
        DecisionJournal(store, clock=lambda: clock["now"]),
        analysis,
        KeyedRisk(),
        ServerClock(),
        clock=lambda: clock["now"],
    )
    service_.set_enabled(True)
    clock["now"] = CLOSE + timedelta(seconds=30)
    return service_, clock, broker


def only_cycle(service_) -> dict:
    (row,) = service_.cycles()["evaluations"]
    return service_.cycle(row["id"])["evaluation"]


def next_attempt(service_):
    """The PENDING window's next attempt time (journal column; not part of the API rows)."""
    with service_.store.read() as conn:
        (value,) = conn.execute(
            "SELECT next_attempt_utc FROM evaluation_cycle WHERE status='PENDING'"
        ).fetchone()
    return parse_utc(value)


def run_to_end(service_, clock, *, limit=20):
    """Pass after pass, jumping to each scheduled attempt, until the window is terminal."""
    for _ in range(limit):
        service_.run_once()
        cycle = only_cycle(service_)
        if cycle["status"] not in ("PENDING", "RUNNING"):
            return cycle
        clock["now"] = max(clock["now"], next_attempt(service_))
    raise AssertionError("window never finished")


def gates_named(cycle, name):
    return [g for g in cycle["gates"] if g["gate"] == name]


def expected_attempts() -> int:
    """Attempts that fit in an H1 window on the data retry schedule (first one at +30 s)."""
    at, attempts = timedelta(seconds=30), 1
    while (
        at + timedelta(seconds=DATA_RETRY[min(attempts - 1, len(DATA_RETRY) - 1)]) < DEADLINES["H1"]
    ):
        at += timedelta(seconds=DATA_RETRY[min(attempts - 1, len(DATA_RETRY) - 1)])
        attempts += 1
    return attempts


# ====================================================================== retries
def test_gap_then_complete_read_passes_t4_in_the_same_evaluation(tmp_path, monkeypatch) -> None:
    service_, clock, broker = scheduler(tmp_path, monkeypatch, [ONE_GAP, COMPLETE])
    service_.run_once()
    pending = only_cycle(service_)
    assert (pending["status"], pending["reason_code"]) == ("PENDING", "history_sync")
    assert pending["decision_id"] is None  # nothing recorded for the incomplete read
    assert next_attempt(service_) == clock["now"] + timedelta(seconds=15)
    cycle = run_to_end(service_, clock)
    assert (cycle["status"], cycle["data_quality"], cycle["attempts"]) == (
        "COMPLETED",
        "accepted",
        2,
    )
    assert (cycle["t6_outcome"], cycle["t6_rule"]) == ("TRADE_CANDIDATE", "R7")
    assert len(broker.reads) == 2
    (t4,) = gates_named(cycle, "T4.window_validation")
    assert (t4["result"], t4["reason_code"]) == ("PASS", "accepted")
    slots = gates_named(cycle, HISTORY_SLOTS_GATE)
    assert {g["result"] for g in slots} == {"SKIPPED"}  # evidence of the retried attempt only
    assert {json.loads(g["detail"])["attempt"] for g in slots} == {1}
    sync = gates_named(cycle, HISTORY_SYNC_GATE)
    assert [(g["result"], g["reason_code"]) for g in sync] == [
        ("SKIPPED", "retry_scheduled"),
        ("PASS", "recovered"),
    ]
    assert json.loads(sync[1]["detail"]) == {
        "retries": 1,
        "attempts": 2,
        "verdict": "accepted",
        "codes": [],
    }


def test_persistent_gap_ends_as_the_normal_fail_closed_rejection(tmp_path, monkeypatch) -> None:
    service_, clock, broker = scheduler(tmp_path, monkeypatch, [ONE_GAP])
    cycle = run_to_end(service_, clock)
    attempts = expected_attempts()
    assert (cycle["status"], cycle["attempts"], len(broker.reads)) == (
        "COMPLETED",
        attempts,
        attempts,
    )
    assert all(read < CLOSE + DEADLINES["H1"] for read in broker.reads)  # never past the deadline
    assert (cycle["data_quality"], cycle["t6_outcome"], cycle["t6_rule"]) == (
        "rejected",
        "NO_TRADE",
        "R1",
    )
    assert cycle["risk_status"] is None and cycle["decision_id"] is not None
    (t4,) = gates_named(cycle, "T4.window_validation")
    assert (t4["result"], t4["reason_code"]) == ("FAIL", "rejected")
    sync = gates_named(cycle, HISTORY_SYNC_GATE)
    assert [g["reason_code"] for g in sync] == ["retry_scheduled"] * (attempts - 1) + [
        "retries_exhausted"
    ]
    final_slots = [g for g in gates_named(cycle, HISTORY_SLOTS_GATE) if g["result"] == "FAIL"]
    assert {
        (json.loads(g["detail"])["first"], json.loads(g["detail"])["attempt"]) for g in final_slots
    } == {(GAP_SLOT, attempts)}


def test_last_attempt_without_time_for_a_retry_completes_rejected(tmp_path, monkeypatch) -> None:
    service_, clock, broker = scheduler(tmp_path, monkeypatch, [ONE_GAP])
    clock["now"] = CLOSE + DEADLINES["H1"] - timedelta(seconds=10)  # 15 s retry would not fit
    cycle = run_to_end(service_, clock)
    assert (cycle["status"], cycle["data_quality"], len(broker.reads)) == (
        "COMPLETED",
        "rejected",
        1,
    )
    assert [g["reason_code"] for g in gates_named(cycle, HISTORY_SYNC_GATE)] == [
        "deadline_no_retry"
    ]


@pytest.mark.parametrize(
    ("bars", "reason"), [(SHORT_WITH_GAP, "not_retried_mixed"), (SHORT, "not_eligible")]
)
def test_rejections_with_other_failures_are_never_retried(tmp_path, monkeypatch, bars, reason):
    service_, clock, broker = scheduler(tmp_path, monkeypatch, [bars, COMPLETE])
    service_.run_once()
    cycle = only_cycle(service_)
    assert (cycle["status"], cycle["data_quality"], cycle["attempts"]) == (
        "COMPLETED",
        "rejected",
        1,
    )
    assert len(broker.reads) == 1  # no second read, although it would have been complete
    sync = gates_named(cycle, HISTORY_SYNC_GATE)
    assert [(g["result"], g["reason_code"]) for g in sync] == [("SKIPPED", reason)]


def test_every_attempt_judges_a_new_mt5_read(tmp_path, monkeypatch) -> None:
    service_, clock, broker = scheduler(tmp_path, monkeypatch, [ONE_GAP, ONE_GAP, COMPLETE])
    cycle = run_to_end(service_, clock)
    assert cycle["status"] == "COMPLETED" and len(broker.reads) == 3
    reads = [
        parse_utc(json.loads(g["detail"])["bars_retrieved_utc"])
        for g in gates_named(cycle, HISTORY_SYNC_GATE)
        if g["reason_code"] == "retry_scheduled"
    ]
    assert len(reads) == 2 and reads[0] < reads[1]  # each retry recorded its own, newer read


def test_a_read_that_is_not_newer_is_never_judged(tmp_path, monkeypatch) -> None:
    service_, clock, broker = scheduler(tmp_path, monkeypatch, [ONE_GAP, COMPLETE])
    service_.run_once()  # history-sync retry scheduled
    first_read = broker.reads[0]
    clock["now"] = next_attempt(service_)
    real = broker.snapshot

    def replayed(symbol, timeframe, count):  # the earlier observation served again
        saved, clock["now"] = clock["now"], first_read
        try:
            return real(symbol, timeframe, count)
        finally:
            clock["now"] = saved

    broker.snapshot = replayed
    service_.run_once()
    cycle = only_cycle(service_)
    assert (cycle["status"], cycle["reason_code"]) == ("PENDING", "history_sync_stale_read")
    assert cycle["decision_id"] is None and not gates_named(cycle, "T4.window_validation")
    broker.snapshot = real
    clock["now"] = next_attempt(service_)
    assert run_to_end(service_, clock)["data_quality"] == "accepted"


def test_expired_deadline_reads_nothing_more(tmp_path, monkeypatch) -> None:
    service_, clock, broker = scheduler(tmp_path, monkeypatch, [ONE_GAP, COMPLETE])
    service_.run_once()
    clock["now"] = CLOSE + DEADLINES["H1"] + timedelta(seconds=1)  # e.g. the pass was delayed
    service_.run_once()
    cycle = only_cycle(service_)
    assert (cycle["status"], cycle["reason_code"]) == ("SKIPPED", "history_sync")
    assert len(broker.reads) == 1 and cycle["decision_id"] is None
    diag = service_.diagnostics()["history_sync"]
    assert diag["retried_then_not_completed"] == [
        {"status": "SKIPPED", "reason_code": "history_sync", "count": 1}
    ]


def test_busy_worker_on_a_retry_keeps_the_contention_behaviour(tmp_path, monkeypatch) -> None:
    busy = MarketUnavailable(BACKGROUND_DEFERRED, "the worker was used moments ago")
    service_, clock, broker = scheduler(tmp_path, monkeypatch, [ONE_GAP, busy, COMPLETE])
    service_.run_once()
    clock["now"] = next_attempt(service_)
    service_.run_once()
    cycle = only_cycle(service_)
    assert (cycle["status"], cycle["reason_code"]) == ("PENDING", "mt5_worker_busy")
    assert next_attempt(service_) == clock["now"] + se.CONTENTION_RETRY
    final = run_to_end(service_, clock)
    assert (final["status"], final["data_quality"], len(broker.reads)) == (
        "COMPLETED",
        "accepted",
        3,
    )
    assert gates_named(final, HISTORY_SYNC_GATE)[-1]["reason_code"] == "recovered"


def test_worker_failure_on_a_retry_keeps_the_backoff(tmp_path, monkeypatch) -> None:
    failed = MarketUnavailable("timeout", "MT5 did not respond")
    service_, clock, broker = scheduler(tmp_path, monkeypatch, [ONE_GAP, failed, COMPLETE])
    service_.run_once()
    clock["now"] = next_attempt(service_)
    service_.run_once()
    cycle = only_cycle(service_)
    assert (cycle["status"], cycle["reason_code"]) == ("PENDING", "worker_backoff")
    assert service_._pause_until == clock["now"] + se.FAILURE_PAUSE
    clock["now"] += timedelta(minutes=1)
    service_.run_once()  # paused: no read
    assert len(broker.reads) == 2
    final = run_to_end(service_, clock)
    assert (final["status"], final["data_quality"], len(broker.reads)) == (
        "COMPLETED",
        "accepted",
        3,
    )


def test_one_cycle_one_decision_one_outcome_no_duplicate_sequences(tmp_path, monkeypatch) -> None:
    service_, clock, _ = scheduler(tmp_path, monkeypatch, [ONE_GAP, MULTI_GAP, COMPLETE])
    cycle = run_to_end(service_, clock)
    seqs = [g["seq"] for g in cycle["gates"]]
    assert seqs == list(range(1, len(seqs) + 1))  # contiguous, one sequence across attempts
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM evaluation_cycle").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM decision").fetchone()[0] == 1
        terminal = conn.execute(
            "SELECT action FROM audit_log WHERE table_name='evaluation_cycle'"
        ).fetchall()
    assert [r[0] for r in terminal] == ["evaluation.completed"]
    assert service_.store.verify_audit_chain()["valid"] is True


def test_diagnostics_report_retries_without_false_t4_failures(tmp_path, monkeypatch) -> None:
    service_, clock, _ = scheduler(tmp_path, monkeypatch, [ONE_GAP, COMPLETE])
    run_to_end(service_, clock)
    diag = service_.diagnostics()
    assert diag["t4_verdicts"] == {"accepted": 1}  # the recovered window counts once
    assert not [r for r in diag["strategy_and_data_failures"] if r["gate"].startswith("T4.")]
    sync = diag["history_sync"]
    assert (sync["retries_scheduled"], sync["windows_with_retries"]) == (1, 1)
    assert (sync["recovered_after_retry"], sync["rejected_after_retries"]) == (1, 0)
    assert sync["missing_slot_records"] == [
        {"reason_code": "closed_history_gap", "result": "SKIPPED", "count": 1},
        {"reason_code": "gap_unknown", "result": "SKIPPED", "count": 1},
    ]
    assert diag["populations"]["completed_evaluations"] == 1


def test_exhausted_window_is_one_genuine_t4_failure(tmp_path, monkeypatch) -> None:
    service_, clock, _ = scheduler(tmp_path, monkeypatch, [ONE_GAP])
    run_to_end(service_, clock)
    diag = service_.diagnostics()
    assert diag["t4_verdicts"] == {"rejected": 1}
    failures = {(r["gate"], r["result"]): r["count"] for r in diag["strategy_and_data_failures"]}
    assert failures[("T4.window_validation", "FAIL")] == 1
    assert not [g for g, _ in failures if g.startswith("T4.history")]
    assert diag["history_sync"]["rejected_after_retries"] == 1


def test_existing_records_are_untouched_by_a_retried_window(tmp_path, monkeypatch) -> None:
    service_, clock, _ = scheduler(tmp_path, monkeypatch, [COMPLETE, ONE_GAP, COMPLETE])
    run_to_end(service_, clock)  # an earlier, normal window (13:00)
    with closing(raw(tmp_path)) as conn:
        before = {
            t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
            for t in ("evaluation_cycle", "gate_outcome", "decision", "audit_log")
        }
    # the next hour's window (one more candle, 13:00Z): incomplete first, then complete
    last = COMPLETE[-1]
    newest = {**last, "t": last["close_t"], "close_t": iso_z(parse_utc(last["close_t"]) + H1)}
    nxt = [*COMPLETE[1:], newest]
    gap = [b for i, b in enumerate(nxt) if i != 300]
    service_.analysis._live.script = [gap, nxt]
    service_.analysis._live.reads = []
    clock["now"] = CLOSE + H1 + timedelta(seconds=30)
    for _ in range(5):
        service_.run_once()
        rows = service_.cycles()["evaluations"]
        latest = max(rows, key=lambda r: r["window_close_utc"])
        if latest["status"] not in ("PENDING", "RUNNING"):
            break
        clock["now"] = next_attempt(service_)
    assert latest["status"] == "COMPLETED" and latest["data_quality"] == "accepted"
    with closing(raw(tmp_path)) as conn:
        for table, rows in before.items():
            now_rows = [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            assert now_rows[: len(rows)] == rows  # earlier records byte-identical
    assert service_.store.verify_audit_chain()["valid"] is True
