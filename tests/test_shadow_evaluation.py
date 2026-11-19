"""P6.1 shadow evaluation: scheduling, closure, deduplication, restart, contention and
diagnostics. Temporary journals only; analysis and risk results come from the real T6 and
Phase 4 code (fixtures), served through stubs so the schedule can be driven by a test clock."""

from __future__ import annotations

import copy
import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from risk_fixtures import BUY_LEVEL, NO_TARGET, PRODUCTION_COSTS, envelope, propose
from test_journal_store import Acl, raw, started

from johanna.adapters.mt5 import ServerClock
from johanna.analysis.decision import DECISION_VERSION
from johanna.decision_journal import DecisionJournal
from johanna.live_market import _BACKGROUND, MarketUnavailable, _deferred, in_background
from johanna.risk_engine import RISK_ENGINE_VERSION
from johanna.shadow_evaluation import (
    BANNER,
    PAIRS,
    TIMEFRAMES,
    ShadowEvaluationService,
    iso_z,
    market_closed,
    window_close_at_or_before,
)

# Wednesday 2026-11-18 00:00 UTC closes an H1, an H4 and a D1 candle at once.
MIDNIGHT = datetime(2026, 11, 18, 0, 0, tzinfo=UTC)
CANDIDATE_UNVERIFIED = envelope(level=BUY_LEVEL)  # real R7: Phase 4 UNVERIFIED (costs)
# A checklist claiming TC6 "pass" without a level (T6 cannot produce it since P6.2b.8):
# Phase 4's independent revalidation REJECTS it (target_structure).
CANDIDATE_NO_TARGET = envelope(selected_level=NO_TARGET)
RISK_UNVERIFIED = propose(env=CANDIDATE_UNVERIFIED, costs=PRODUCTION_COSTS)
RISK_NO_TARGET = propose(env=CANDIDATE_NO_TARGET, costs=PRODUCTION_COSTS)


def no_trade() -> dict:
    env = copy.deepcopy(CANDIDATE_UNVERIFIED)
    env["result"]["decision"].update(outcome="NO_TRADE", rule="R2", reasons=["no regime"])
    env["result"]["opportunity"] = {}
    return env


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


def candle(close: datetime, closure: str = "tick_observed") -> dict:
    return {"close_t": iso_z(close), "closure": closure}


class Analysis:
    """Stands in for MarketAnalysisService.analyse_background."""

    def __init__(self, env=None, closure="tick_observed", bars_status="LIVE") -> None:
        self.env = env if env is not None else no_trade()
        self.closure, self.bars_status = closure, bars_status
        self.error: Exception | None = None
        self.calls: list[tuple[str, str]] = []
        self.background_flags: list[bool] = []
        self.clock: Clock | None = None
        self.per_symbol: dict[str, dict] = {}
        self.missing = False

    def analyse_background(self, symbol, timeframe):
        self.calls.append((symbol, timeframe))
        self.background_flags.append(in_background())
        if callable(self.error):
            raise self.error()
        if self.error is not None:
            raise self.error
        env = copy.deepcopy(self.per_symbol.get(symbol, self.env))
        env["symbol"], env["timeframe"] = symbol, timeframe  # the envelope's own key
        env["result"]["symbol"] = symbol
        env["result"]["broker_symbol"] = PAIRS[symbol]
        env["result"]["timeframe"] = timeframe
        close = window_close_at_or_before(self.clock(), timeframe, ServerClock())
        bars = [] if self.missing else [candle(close, self.closure)]
        # the real T2 snapshot shape: source status in "upstream", candles in bars["items"]
        return env, {
            "upstream": {"status": self.bars_status, "reason": None},
            "bars": {"items": bars},
        }


class Risk:
    def __init__(self) -> None:
        self.calls = 0
        self.result = RISK_UNVERIFIED
        self.defer = False

    def assess(self, symbol, timeframe, env):
        self.calls += 1
        if self.defer:  # a contended MT5 read inside the risk inputs
            _deferred(_BACKGROUND.get(), "busy")
        result = copy.deepcopy(self.result)  # real Phase 4 output, keyed to this request
        result["symbol"], result["timeframe"] = symbol, timeframe
        return result


def setup(tmp_path, now=MIDNIGHT - timedelta(minutes=5), *, enable=True, **kwargs):
    clock = Clock(now)
    store = started(tmp_path, clock=clock)
    analysis, risk = Analysis(), Risk()
    analysis.clock = clock
    service = ShadowEvaluationService(
        store,
        DecisionJournal(store, clock=clock),
        analysis,
        risk,
        ServerClock(),
        clock=clock,
        **kwargs,
    )
    if enable:
        service.set_enabled(True)
    return service, clock, analysis, risk


def cycles(service, **filters) -> list[dict]:
    return service.cycles(limit=100, **filters)["evaluations"]


def by_status(service) -> dict[str, int]:
    return service.status()["cycle_counts"]


# ====================================================================== enabled state
def test_disabled_by_default_and_nothing_runs(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, enable=False)
    service.start()
    assert service.enabled() is False and service.status()["state"] == "disabled"
    assert service._thread is None
    clock.now = MIDNIGHT + timedelta(minutes=1)
    service.run_once()
    assert analysis.calls == [] and cycles(service) == []


def test_enabled_state_persists_and_is_audited(tmp_path) -> None:
    service, clock, _, _ = setup(tmp_path, enable=False)
    service.set_enabled(True)
    again = ShadowEvaluationService(
        service.store, service.decisions, Analysis(), Risk(), ServerClock(), clock=clock
    )
    assert again.enabled() is True
    again.set_enabled(False)
    assert service.enabled() is False
    with closing(raw(tmp_path)) as conn:
        rows = conn.execute(
            "SELECT action, actor FROM audit_log WHERE table_name='shadow_scheduler_setting'"
        ).fetchall()
        settings = conn.execute("SELECT enabled FROM shadow_scheduler_setting").fetchall()
    assert [tuple(r) for r in rows] == [
        ("shadow_scheduler.enabled", "user"),
        ("shadow_scheduler.disabled", "user"),
    ]
    assert [r[0] for r in settings] == [1, 0]
    service.set_enabled(False)  # unchanged state adds no record
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM shadow_scheduler_setting").fetchone()[0] == 2


def test_enable_rejects_non_boolean(tmp_path) -> None:
    service, *_ = setup(tmp_path, enable=False)
    with pytest.raises(ValueError):
        service.set_enabled("yes")


# ====================================================================== schedule
def test_all_nine_combinations_at_a_shared_close(tmp_path) -> None:
    service, clock, analysis, risk = setup(tmp_path)
    clock.now = MIDNIGHT + timedelta(seconds=30)
    service.run_once()
    done = cycles(service, status="COMPLETED")
    assert sorted((c["symbol"], c["timeframe"]) for c in done) == sorted(
        (s, tf) for s in PAIRS for tf in TIMEFRAMES
    )
    assert {c["window_close_utc"] for c in done} == {"2026-11-18T00:00:00Z"}
    assert {c["broker_symbol"] for c in done} == {"EURUSDc", "GBPUSDc", "USDJPYc"}
    assert all(analysis.background_flags)  # every analysis ran as a background read
    assert risk.calls == 0  # NO_TRADE: no risk assessment


def test_only_hourly_windows_between_four_hour_closes(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(hours=12, minutes=58))
    clock.now = MIDNIGHT + timedelta(hours=13, seconds=30)
    service.run_once()
    assert {c["timeframe"] for c in cycles(service)} == {"H1"}
    assert len(analysis.calls) == 3


def test_four_hourly_and_daily_boundaries(tmp_path) -> None:
    service, clock, _, _ = setup(tmp_path, now=MIDNIGHT + timedelta(hours=3, minutes=58))
    clock.now = MIDNIGHT + timedelta(hours=4, seconds=30)
    service.run_once()
    assert sorted({c["timeframe"] for c in cycles(service)}) == ["H1", "H4"]
    clock.now = MIDNIGHT + timedelta(days=1, seconds=30)
    service.run_once()
    d1 = cycles(service, timeframe="D1")
    assert {c["window_close_utc"] for c in d1} == {"2026-11-19T00:00:00Z"} and len(d1) == 3


def test_nothing_before_the_start_delay(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path)
    clock.now = MIDNIGHT + timedelta(seconds=10)
    service.run_once()
    assert cycles(service) == [] and analysis.calls == []


def test_dst_server_clock_boundaries_follow_server_wall_time() -> None:
    athens = ServerClock("Europe/Athens")  # UTC+3 in summer, UTC+2 in winter
    summer = datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    winter = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
    assert window_close_at_or_before(summer, "D1", athens) == datetime(2026, 6, 30, 21, tzinfo=UTC)
    assert window_close_at_or_before(winter, "D1", athens) == datetime(2026, 11, 30, 22, tzinfo=UTC)
    utc = ServerClock()
    assert window_close_at_or_before(summer, "H4", utc) == datetime(2026, 7, 1, 12, tzinfo=UTC)


def test_weekend_windows_are_skipped_without_mt5(tmp_path) -> None:
    saturday = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)
    service, clock, analysis, _ = setup(tmp_path, now=saturday - timedelta(minutes=2))
    clock.now = saturday + timedelta(seconds=30)
    service.run_once()
    rows = cycles(service)
    assert rows and {(c["status"], c["reason_code"]) for c in rows} == {
        ("SKIPPED", "market_closed")
    }
    assert analysis.calls == []
    assert market_closed(saturday, "H1", ServerClock(), service.sessions)
    assert not market_closed(MIDNIGHT, "H1", ServerClock(), service.sessions)


# ====================================================================== closure
def test_unconfirmed_closure_is_retried_then_skipped(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(hours=12, minutes=58))
    analysis.closure = "clock_only"
    clock.now = MIDNIGHT + timedelta(hours=13, seconds=30)
    service.run_once()
    pending = cycles(service, status="PENDING")
    assert len(pending) == 3 and {c["reason_code"] for c in pending} == {"closure_unconfirmed"}
    assert cycles(service, status="COMPLETED") == []
    calls = len(analysis.calls)
    clock.advance(seconds=10)
    service.run_once()
    assert len(analysis.calls) == calls  # bounded backoff: not retried yet
    clock.advance(seconds=10)
    service.run_once()
    assert len(analysis.calls) == calls + 3
    clock.now = MIDNIGHT + timedelta(hours=13, minutes=10)
    service.run_once()
    skipped = cycles(service, status="SKIPPED")
    assert len(skipped) == 3 and {c["reason_code"] for c in skipped} == {"closure_unconfirmed"}
    assert cycles(service, status="COMPLETED") == []  # never evaluated as complete


def test_confirmed_after_retry_completes(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(hours=12, minutes=58))
    analysis.closure = "forming"
    clock.now = MIDNIGHT + timedelta(hours=13, seconds=30)
    service.run_once()
    analysis.closure = "tick_observed"
    clock.advance(seconds=20)
    service.run_once()
    done = cycles(service, status="COMPLETED")
    assert len(done) == 3 and {c["attempts"] for c in done} == {2}


def test_missing_candle_and_stale_bars_are_structured_skips(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(hours=12, minutes=58))
    analysis.missing = True
    clock.now = MIDNIGHT + timedelta(hours=13, seconds=30)
    service.run_once()
    assert {c["reason_code"] for c in cycles(service)} == {"candle_not_available"}
    analysis.missing, analysis.bars_status = False, "CACHED"
    clock.now = MIDNIGHT + timedelta(hours=14, seconds=30)
    service.run_once()
    # the 13:00 windows passed their deadline and were closed with their last reason
    assert {c["reason_code"] for c in cycles(service, status="SKIPPED")} == {"candle_not_available"}
    assert {c["reason_code"] for c in cycles(service, status="PENDING")} == {
        "market_data_unavailable"
    }
    clock.now = MIDNIGHT + timedelta(hours=14, minutes=11)
    service.run_once()
    reasons = {(c["status"], c["reason_code"]) for c in cycles(service)}
    assert reasons == {("SKIPPED", "candle_not_available"), ("SKIPPED", "market_data_unavailable")}


# ====================================================================== deduplication
def test_a_window_is_evaluated_once(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path)
    clock.now = MIDNIGHT + timedelta(seconds=30)
    service.run_once()
    service.run_once()
    clock.advance(minutes=5)
    service.run_once()
    assert len(analysis.calls) == 9 and len(cycles(service)) == 9
    with closing(raw(tmp_path)) as conn:
        keys = [r[0] for r in conn.execute("SELECT dedup_key FROM evaluation_cycle")]
    assert len(set(keys)) == 9
    # d2 since P6.2b.8 (TC6 fails without a target); r4 since P6.2b.8
    assert all(k.endswith(f"|a1|d{DECISION_VERSION}|r{RISK_ENGINE_VERSION}") for k in keys)
    assert DECISION_VERSION == 2


def test_concurrent_passes_never_process_the_same_window(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path)
    clock.now = MIDNIGHT + timedelta(seconds=30)
    barrier = threading.Barrier(4)

    def run():
        barrier.wait()
        service.run_once()

    threads = [threading.Thread(target=run) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    service.run_once()
    assert len(analysis.calls) == 9 and len(cycles(service, status="COMPLETED")) == 9


def test_existing_dedup_key_is_never_inserted_twice(tmp_path) -> None:
    service, clock, _, _ = setup(tmp_path)
    clock.now = MIDNIGHT + timedelta(seconds=30)
    service.run_once()
    service._create_window("EURUSD", "H1", MIDNIGHT, clock.now)  # direct second attempt
    assert len(cycles(service, symbol="EURUSD", timeframe="H1")) == 1


# ====================================================================== restart
def test_restart_records_missed_windows_and_never_replays_them(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(minutes=58))
    clock.now = MIDNIGHT + timedelta(hours=1, seconds=30)
    service.run_once()
    service.stop()
    calls = len(analysis.calls)
    clock.now = MIDNIGHT + timedelta(hours=5, minutes=2)  # backend down for four hours
    revived = ShadowEvaluationService(
        service.store, service.decisions, analysis, Risk(), ServerClock(), clock=clock
    )
    revived.start()
    revived.stop()  # the pass is driven below
    revived.run_once()
    h1 = cycles(revived, symbol="EURUSD", timeframe="H1")
    statuses = {c["window_close_utc"]: (c["status"], c["reason_code"]) for c in h1}
    assert statuses["2026-11-18T02:00:00Z"] == ("MISSED", "scheduler_not_running")
    assert statuses["2026-11-18T04:00:00Z"] == ("MISSED", "scheduler_not_running")
    assert statuses["2026-11-18T05:00:00Z"][0] == "COMPLETED"  # current window only
    h4 = cycles(revived, symbol="EURUSD", timeframe="H4")
    assert [(c["window_close_utc"], c["status"]) for c in h4] == [
        ("2026-11-18T04:00:00Z", "MISSED")
    ]
    # only the current H1 window (three instruments) was analysed after the restart
    assert len(analysis.calls) - calls == 3


def test_interrupted_running_cycle_is_adopted_inside_its_window(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(minutes=58))
    analysis.closure = "clock_only"
    clock.now = MIDNIGHT + timedelta(hours=1, seconds=30)
    service.run_once()
    with closing(raw(tmp_path)) as conn:  # simulate a crash in the middle of an attempt
        conn.execute("UPDATE evaluation_cycle SET status='RUNNING' WHERE status='PENDING'")
    analysis.closure = "tick_observed"
    clock.advance(seconds=40)
    revived = ShadowEvaluationService(
        service.store, service.decisions, analysis, Risk(), ServerClock(), clock=clock
    )
    revived._recover_interrupted(clock.now)
    assert {c["status"] for c in cycles(revived)} == {"PENDING"}
    revived.run_once()
    assert {c["status"] for c in cycles(revived)} == {"COMPLETED"}


def test_interrupted_cycle_past_its_deadline_fails_visibly(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(minutes=58))
    analysis.closure = "clock_only"
    clock.now = MIDNIGHT + timedelta(hours=1, seconds=30)
    service.run_once()
    with closing(raw(tmp_path)) as conn:
        conn.execute(
            "UPDATE evaluation_cycle SET status='RUNNING' "
            "WHERE status='PENDING' AND symbol='EURUSD'"
        )
    clock.now = MIDNIGHT + timedelta(hours=1, minutes=30)
    revived = ShadowEvaluationService(
        service.store, service.decisions, analysis, Risk(), ServerClock(), clock=clock
    )
    revived._recover_interrupted(clock.now)
    rows = {c["symbol"]: (c["status"], c["reason_code"]) for c in cycles(revived)}
    assert rows["EURUSD"] == ("FAILED", "interrupted")
    assert rows["GBPUSD"] == ("MISSED", "scheduler_not_running")


def test_disable_while_pending_skips_the_window(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(minutes=58))
    analysis.closure = "clock_only"
    clock.now = MIDNIGHT + timedelta(hours=1, seconds=30)
    service.run_once()
    service.set_enabled(False)
    assert {(c["status"], c["reason_code"]) for c in cycles(service)} == {
        ("SKIPPED", "scheduler_disabled")
    }
    clock.advance(minutes=1)
    service.run_once()
    assert len(cycles(service)) == 3  # no new windows while disabled


def test_never_enabled_journal_after_restart_stays_disabled(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, enable=False)
    service.start()
    service.stop()
    again = ShadowEvaluationService(
        service.store, service.decisions, analysis, Risk(), ServerClock(), clock=clock
    )
    again.start()
    assert again.status()["state"] == "disabled" and again.enabled() is False


def test_thread_runs_in_the_background_and_stops_cleanly(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, tick_seconds=0.01)
    clock.now = MIDNIGHT + timedelta(seconds=30)
    service.start()
    try:
        for _ in range(500):
            if len(cycles(service, status="COMPLETED")) == 9:
                break
            threading.Event().wait(0.01)
        assert service.status()["state"] == "running"
    finally:
        service.stop()
    assert service._thread is None and len(cycles(service, status="COMPLETED")) == 9
    assert service.status()["state"] == "stopped"


# ====================================================================== contention
def test_contention_deferral_retries_within_the_window_then_skips(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(hours=12, minutes=58))

    def busy():
        return _deferred(_BACKGROUND.get(), "the worker is busy")

    analysis.error = busy
    clock.now = MIDNIGHT + timedelta(hours=13, seconds=30)
    service.run_once()
    pending = cycles(service, status="PENDING")
    assert len(pending) == 3 and {c["reason_code"] for c in pending} == {"mt5_worker_busy"}
    assert {c["contention_deferrals"] for c in pending} == {1}
    clock.advance(seconds=5)
    service.run_once()
    assert {c["contention_deferrals"] for c in cycles(service)} == {2}
    clock.now = MIDNIGHT + timedelta(hours=13, minutes=9, seconds=58)
    service.run_once()
    skipped = cycles(service, status="SKIPPED")
    assert len(skipped) == 3 and {c["reason_code"] for c in skipped} == {"mt5_worker_busy"}
    diag = service.diagnostics()
    assert diag["contention"]["skipped_mt5_worker_busy"] == 3
    assert diag["contention"]["total_deferrals"] >= 9


def test_worker_timeout_pauses_the_scheduler(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(hours=12, minutes=58))
    analysis.error = MarketUnavailable("timeout", "MT5 did not respond within 4 s")
    clock.now = MIDNIGHT + timedelta(hours=13, seconds=30)
    service.run_once()
    assert len(analysis.calls) == 1  # the pause stops further attempts in this pass
    assert service.status()["paused_until_utc"] is not None
    clock.advance(minutes=1)
    service.run_once()
    assert len(analysis.calls) == 1


def test_mt5_unavailable_is_a_data_retry(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(hours=12, minutes=58))
    analysis.error = MarketUnavailable("terminal_not_running", "MT5 is not running")
    clock.now = MIDNIGHT + timedelta(hours=13, seconds=30)
    service.run_once()
    assert {c["reason_code"] for c in cycles(service)} == {"market_data_unavailable"}


# ====================================================================== evaluation outcomes
def test_candidate_with_unverified_costs(tmp_path) -> None:
    service, clock, analysis, risk = setup(tmp_path, now=MIDNIGHT + timedelta(hours=12, minutes=58))
    analysis.env = CANDIDATE_UNVERIFIED
    clock.now = MIDNIGHT + timedelta(hours=13, seconds=30)
    service.run_once()
    done = cycles(service, status="COMPLETED")
    assert {(c["t6_outcome"], c["t6_rule"], c["risk_status"]) for c in done} == {
        ("TRADE_CANDIDATE", "R7", "UNVERIFIED")
    }
    assert risk.calls == 3
    detail = service.cycle(done[0]["id"])["evaluation"]
    gates = {(g["stage"], g["gate"]): g for g in detail["gates"]}
    assert gates[("risk", "costs.net_reward_risk")]["result"] == "BLOCKED"
    assert gates[("risk", "phase4.status")]["result"] == "BLOCKED"
    assert gates[("analysis", "T6.R7")]["result"] == "PASS"
    assert gates[("closure", "closure.tick_observed")]["result"] == "PASS"
    assert gates[("strategy", "TC6_clearance")]["result"] == "PASS"
    assert all(g["detail"] is None for g in detail["gates"] if g["stage"] == "risk")
    diag = service.diagnostics()
    assert diag["risk"]["blocked_by_unverified_costs"] == 3
    assert diag["risk"]["approved"] == 0 and diag["risk"]["unverified"] == 3
    journal = DecisionJournal(service.store, clock=clock)
    decision = journal.get(done[0]["decision_id"])["decision"]
    assert decision["capture_source"] == "scheduled_evaluation"
    assert decision["trade_eligibility"] == "NOT_APPROVED"
    assert decision["execution_status"] == "NONE"
    assert decision["risk_assessments"][0]["risk_status"] == "UNVERIFIED"


def test_candidate_rejected_for_missing_target_structure(tmp_path) -> None:
    service, clock, analysis, risk = setup(tmp_path, now=MIDNIGHT + timedelta(hours=12, minutes=58))
    analysis.env, risk.result = CANDIDATE_NO_TARGET, RISK_NO_TARGET
    clock.now = MIDNIGHT + timedelta(hours=13, seconds=30)
    service.run_once()
    done = cycles(service, status="COMPLETED")
    assert {(c["t6_outcome"], c["risk_status"]) for c in done} == {("TRADE_CANDIDATE", "REJECTED")}
    diag = service.diagnostics()
    assert diag["risk"]["blocked_by_target_structure"] == 3
    assert diag["risk"]["rejected"] == 3
    assert diag["populations"]["market_candidates"] == 3  # still candidates
    assert diag["populations"]["risk_approved_proposals"] == 0
    reasons = {(r["gate"], r["result"]) for r in diag["risk"]["rejection_and_block_reasons"]}
    assert ("construction.target_structure", "FAIL") in reasons


def test_risk_input_contention_retries_instead_of_recording_a_distorted_result(tmp_path):
    service, clock, analysis, risk = setup(tmp_path, now=MIDNIGHT + timedelta(hours=12, minutes=58))
    analysis.env, risk.defer = CANDIDATE_UNVERIFIED, True
    clock.now = MIDNIGHT + timedelta(hours=13, seconds=30)
    service.run_once()
    assert {(c["status"], c["reason_code"]) for c in cycles(service)} == {
        ("PENDING", "mt5_worker_busy")
    }
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM risk_assessment").fetchone()[0] == 0
    risk.defer = False
    clock.advance(seconds=6)
    service.run_once()
    assert {c["risk_status"] for c in cycles(service)} == {"UNVERIFIED"}


def test_analysis_error_fails_the_window_visibly(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(hours=12, minutes=58))
    analysis.error = RuntimeError("boom")
    clock.now = MIDNIGHT + timedelta(hours=13, seconds=30)
    service.run_once()
    rows = cycles(service)
    assert {(c["status"], c["reason_code"], c["reason_detail"]) for c in rows} == {
        ("FAILED", "evaluation_error", "RuntimeError")
    }


def test_every_non_completed_window_has_a_structured_reason(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(hours=12, minutes=58))
    analysis.closure = "clock_only"
    clock.now = MIDNIGHT + timedelta(hours=13, seconds=30)
    service.run_once()
    clock.now = MIDNIGHT + timedelta(hours=16, minutes=30)
    service.run_once()
    rows = cycles(service)
    assert rows and all(c["reason_code"] for c in rows if c["status"] != "COMPLETED")
    for row in rows:
        if row["status"] in ("SKIPPED", "MISSED", "FAILED"):
            gates = service.cycle(row["id"])["evaluation"]["gates"]
            assert gates and gates[-1]["reason_code"] == row["reason_code"]


def test_diagnostics_keep_populations_apart_and_never_fabricate_execution(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path)
    analysis.per_symbol = {"EURUSD": CANDIDATE_UNVERIFIED}
    clock.now = MIDNIGHT + timedelta(seconds=30)
    service.run_once()
    diag = service.diagnostics()
    pops = diag["populations"]
    assert pops["evaluation_windows"] == 9 and pops["completed_evaluations"] == 9
    assert pops["market_candidates"] == 3 and pops["risk_proposals"] == 3
    assert pops["risk_approved_proposals"] == 0
    assert pops["executable_opportunities"] is None and pops["actual_trades"] is None
    assert diag["candidate_rate_per_100_completed"] == pytest.approx(33.33)
    assert diag["t6_outcomes"] == {"TRADE_CANDIDATE": 3, "MARKET_CONDITION": 0, "NO_TRADE": 6}
    matrix = {(m["symbol"], m["timeframe"]): m for m in diag["by_instrument_timeframe"]}
    assert matrix[("EURUSD", "H1")]["candidates"] == 1
    assert matrix[("GBPUSD", "H1")]["candidates"] == 0
    assert diag["banner"] == BANNER
    text = repr(diag)
    for word in ("ticket", "login", "position_id", "order_send"):
        assert word not in text


def test_empty_diagnostics_use_explicit_unavailable_semantics(tmp_path) -> None:
    service, *_ = setup(tmp_path, enable=False)
    diag = service.diagnostics()
    assert diag["candidate_rate_per_100_completed"] is None
    assert diag["populations"]["completed_evaluations"] == 0


# ====================================================================== journal integrity
def test_cycle_and_gate_records_are_protected(tmp_path) -> None:
    service, clock, _, _ = setup(tmp_path)
    clock.now = MIDNIGHT + timedelta(seconds=30)
    service.run_once()
    with closing(raw(tmp_path)) as conn:
        for sql in (
            "DELETE FROM evaluation_cycle",
            "UPDATE evaluation_cycle SET status='PENDING'",  # terminal rows never change
            "UPDATE evaluation_cycle SET t6_outcome='TRADE_CANDIDATE'",
            "DELETE FROM gate_outcome",
            "UPDATE gate_outcome SET result='PASS'",
            "DELETE FROM shadow_scheduler_setting",
            "UPDATE shadow_scheduler_setting SET enabled=0",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
        with pytest.raises(sqlite3.IntegrityError):  # cycles start PENDING/SKIPPED/MISSED
            conn.execute(
                "INSERT INTO evaluation_cycle(id, dedup_key, symbol, timeframe, window_close_utc,"
                " analysis_version, decision_version, risk_version, status, deadline_utc,"
                " created_utc, t6_outcome, completed_utc) VALUES ('x','k','EURUSD','H1','t',1,1,1,"
                "'COMPLETED','t','t','NO_TRADE','t')"
            )
    assert service.store.verify_audit_chain()["valid"] is True


def test_refused_journal_blocks_the_scheduler(tmp_path) -> None:
    store = started(tmp_path, acl=Acl(fail=True))
    service = ShadowEvaluationService(store, None, Analysis(), Risk(), ServerClock())
    service.start()
    assert service.status()["state"] == "blocked"
    with pytest.raises(Exception):
        service.set_enabled(True)


def test_an_envelope_for_another_key_is_never_recorded(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(hours=12, minutes=58))
    original = analysis.analyse_background

    def wrong(symbol, timeframe):
        env, snapshot = original(symbol, timeframe)
        env["symbol"] = "GBPUSD" if symbol != "GBPUSD" else "EURUSD"
        return env, snapshot

    analysis.analyse_background = wrong
    clock.now = MIDNIGHT + timedelta(hours=13, seconds=30)
    service.run_once()
    assert {(c["status"], c["reason_code"]) for c in cycles(service)} == {
        ("FAILED", "evaluation_error")
    }
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM decision").fetchone()[0] == 0
