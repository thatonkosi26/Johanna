"""P6.1 MT5 contention safeguards: background reads use the existing workers, are admitted only
when a worker is idle and quiet, never wait or queue, never change the dashboard's connection,
clock-check or cache state, and never fall back to cached or archived bars."""

from __future__ import annotations

import statistics
import threading
import time

import pytest
from fake_mt5 import FakeMT5

from johanna.adapters.mt5 import MetaTrader5Adapter
from johanna.live_market import (
    BACKGROUND_DEFERRED,
    LiveMarketService,
    MarketUnavailable,
    ProcessSessionRunner,
    ThreadSessionRunner,
    background_reads,
)
from johanna.market_analysis import MarketAnalysisService


def live_service(fake: FakeMT5, **kwargs) -> LiveMarketService:
    return LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake),
        terminal_running=lambda: fake.running,
        reconnect_interval=0,
        **kwargs,
    )


class BlockingSession:
    """A session whose ``slow`` call blocks until released (stands in for a long MT5 call)."""

    def __init__(self) -> None:
        self.release = threading.Event()
        self.started = threading.Event()

    def slow(self):
        self.started.set()
        self.release.wait(5)
        return "slow"

    def fast(self):
        return "fast"

    def close(self):
        pass


# ====================================================================== ThreadSessionRunner
def test_background_is_deferred_at_once_while_a_foreground_job_runs() -> None:
    session = BlockingSession()
    runner = ThreadSessionRunner(lambda: session)
    worker = threading.Thread(target=runner.run, args=("slow", (), 5))
    worker.start()
    assert session.started.wait(2)
    began = time.monotonic()
    with background_reads(quiet_seconds=0) as reads:
        with pytest.raises(MarketUnavailable) as info:
            runner.run("fast", (), 5)
    assert time.monotonic() - began < 0.5  # never waited for the worker
    assert info.value.code == BACKGROUND_DEFERRED and reads.deferrals == 1
    session.release.set()
    worker.join()
    runner.close()


def test_background_respects_the_quiet_window_then_is_admitted() -> None:
    session = BlockingSession()
    runner = ThreadSessionRunner(lambda: session)
    assert runner.run("fast", (), 5) == "fast"  # foreground job just finished
    with background_reads(quiet_seconds=0.3) as reads:
        with pytest.raises(MarketUnavailable):
            runner.run("fast", (), 5)
        time.sleep(0.35)
        assert runner.run("fast", (), 5) == "fast"
    assert (reads.deferrals, reads.admitted) == (1, 1)
    runner.close()


def test_foreground_requests_are_unchanged_outside_background_mode() -> None:
    session = BlockingSession()
    runner = ThreadSessionRunner(lambda: session)
    for _ in range(3):
        assert runner.run("fast", (), 5) == "fast"  # no quiet window, no deferral
    runner.close()


# ====================================================================== ProcessSessionRunner
class Mono:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def test_process_runner_admission_never_waits_or_queues() -> None:
    mono = Mono()
    runner = ProcessSessionRunner(monotonic=mono)  # no worker is spawned by _acquire
    with background_reads(quiet_seconds=0.75) as reads:
        runner._acquire(4)  # idle and never used: admitted
        assert runner._lock.locked()
        runner._last_release = mono()
        runner._lock.release()
        with pytest.raises(MarketUnavailable) as quiet:
            runner._acquire(4)  # used moments ago
        mono.now += 1.0
        runner._lock.acquire()  # a foreground job is running
        began = time.monotonic()
        with pytest.raises(MarketUnavailable) as busy:
            runner._acquire(4)
        assert time.monotonic() - began < 0.2
        runner._lock.release()
        runner._waiting = 1  # a foreground caller is waiting for the lock
        with pytest.raises(MarketUnavailable) as waiting:
            runner._acquire(4)
        runner._waiting = 0
    assert {quiet.value.code, busy.value.code, waiting.value.code} == {BACKGROUND_DEFERRED}
    assert (reads.admitted, reads.deferrals) == (1, 3)
    # foreground acquisition still waits for the lock as before
    runner._acquire(1)
    runner._lock.release()
    assert runner.spawn_count == 0


# ====================================================================== LiveMarketService state
def test_background_bars_never_touch_dashboard_state_or_cache() -> None:
    fake = FakeMT5()
    live = live_service(fake)
    before = live.status()
    with background_reads(quiet_seconds=0):
        payload = live.bars("EURUSD", "H1", 50)
    assert payload["status"] == "LIVE" and payload["bars"]
    assert payload["served_from_cache"] is False
    assert live._bars == {}  # never stored in the dashboard's bars cache
    after = live.status()
    assert after["last_success_utc"] == before["last_success_utc"] is None
    assert after["state"] == before["state"] and after["time_check"] == before["time_check"]
    live.stop()


def test_background_failure_never_falls_back_or_marks_unavailable() -> None:
    fake = FakeMT5()
    archive_calls = []

    def archive(pair, timeframe):
        archive_calls.append(pair)
        return [], None

    live = live_service(fake, cached_bars=archive)
    live.bars("EURUSD", "H1", 50)  # foreground: connected, cached
    status = live.status()
    fake.running = False
    with background_reads(quiet_seconds=0):
        with pytest.raises(MarketUnavailable) as info:
            live.bars("EURUSD", "H1", 60)  # not cached: must reach MT5
    assert info.value.code != BACKGROUND_DEFERRED  # a real MT5 failure, raised as such
    assert archive_calls == []  # no archived fallback
    after = live.status()
    assert (
        after["state"] == status["state"]
        and after["last_success_utc"] == status["last_success_utc"]
    )
    assert live.status()["unavailable_reason"] is None
    live.stop()


def test_background_worker_read_leaves_connection_state_alone() -> None:
    fake = FakeMT5()
    live = live_service(fake)
    with background_reads(quiet_seconds=0):
        live.broker_read(["EURUSD"])
    assert live.status()["last_success_utc"] is None
    live.stop()


# ====================================================================== analysis single-flight
def test_background_analysis_never_joins_or_is_joined_by_the_dashboard() -> None:
    gate = threading.Event()
    entered = threading.Event()

    class Live:
        def snapshot(self, symbol, timeframe, count):
            entered.set()
            gate.wait(5)
            raise MarketUnavailable(BACKGROUND_DEFERRED, "deferred")

    service = MarketAnalysisService(Live(), clock=lambda: None)
    errors = []

    def background():
        try:
            service.analyse_background("EURUSD", "H1")
        except MarketUnavailable as exc:
            errors.append(exc.code)

    thread = threading.Thread(target=background)
    thread.start()
    assert entered.wait(2)
    assert service.waiting("EURUSD", "H1") is None  # not registered as an in-flight computation
    gate.set()
    thread.join()
    assert errors == [BACKGROUND_DEFERRED]


# ====================================================================== latency (simulation)
def test_simulated_quote_latency_p95_increase_is_within_100_ms() -> None:
    """In-process simulation of the A/B check (the live A/B is part of live verification)."""
    fake = FakeMT5()
    fake.delay = 0.004  # every MT5 call takes a few milliseconds
    live = live_service(fake, quote_ttl=__import__("datetime").timedelta(seconds=0))

    def sample(n=60) -> list[float]:
        out = []
        for _ in range(n):
            began = time.perf_counter()
            live.quotes(["EURUSD", "USDJPY"])
            out.append(time.perf_counter() - began)
            time.sleep(0.01)
        return out

    def p95(values):
        return statistics.quantiles(values, n=20)[-1]

    baseline = sample()
    stop = threading.Event()
    attempts = {"admitted": 0, "deferred": 0}

    def scheduler():
        while not stop.is_set():
            with background_reads() as reads:
                try:
                    live.broker_read(["EURUSD", "USDJPY"])
                except MarketUnavailable:
                    pass
            attempts["admitted"] += reads.admitted
            attempts["deferred"] += reads.deferrals
            time.sleep(0.005)

    worker = threading.Thread(target=scheduler)
    worker.start()
    try:
        loaded = sample()
    finally:
        stop.set()
        worker.join()
        live.stop()
    assert attempts["deferred"] > 0  # the scheduler yielded to dashboard traffic
    assert p95(loaded) - p95(baseline) <= 0.100


# ====================================================================== real pipeline shapes
def test_real_background_analysis_feeds_the_closure_gate(tmp_path) -> None:
    """The real stack (fake MT5): background snapshot shapes match what the scheduler reads."""
    from test_decision_journal import _app

    from johanna.shadow_evaluation import iso_z, parse_utc

    app = _app(tmp_path)
    app.start()
    try:
        before = app.live_market.status()
        with background_reads(quiet_seconds=0):
            env, snapshot = app.market_analysis.analyse_background("EURUSD", "H1")
        assert snapshot["upstream"]["status"] == "LIVE"
        confirmed = [b for b in snapshot["bars"]["items"] if b["closure"] == "tick_observed"]
        assert confirmed and env["result"]["decision"]["outcome"]
        assert app.live_market.status()["last_success_utc"] == before["last_success_utc"]
        close = parse_utc(confirmed[-1]["close_t"])
        with background_reads(quiet_seconds=0) as reads:
            outcome = app.shadow._evaluate("EURUSD", "H1", close, reads)
        assert outcome["closure"] == "tick_observed"
        assert iso_z(close) == confirmed[-1]["close_t"]
    finally:
        app.stop()


def test_consecutive_background_reads_do_not_throttle_each_other() -> None:
    session = BlockingSession()
    runner = ThreadSessionRunner(lambda: session)
    with background_reads(quiet_seconds=0.5) as reads:
        for _ in range(3):
            assert runner.run("fast", (), 5) == "fast"
    assert (reads.admitted, reads.deferrals) == (3, 0)
    runner.close()


def test_end_to_end_pass_through_the_real_stack(tmp_path) -> None:
    """One scheduler pass with the real live market, analysis, risk and journal (fake MT5)."""
    from datetime import timedelta

    from test_decision_journal import _app

    from johanna.shadow_evaluation import parse_utc

    app = _app(tmp_path)
    app.start()
    try:
        with background_reads(quiet_seconds=0):
            _, snapshot = app.market_analysis.analyse_background("EURUSD", "H1")
        closed = [b for b in snapshot["bars"]["items"] if b["closure"] == "tick_observed"]
        boundary = parse_utc(closed[-1]["close_t"])
        clock = {"now": boundary - timedelta(minutes=1)}
        app.shadow._clock = lambda: clock["now"]
        app.shadow.set_enabled(True)
        app.shadow.stop()  # drive the pass directly
        clock["now"] = boundary + timedelta(seconds=30)
        app.shadow.run_once()
        rows = app.shadow.cycles(symbol="EURUSD", timeframe="H1")["evaluations"]
        assert [(r["status"], r["window_close_utc"]) for r in rows] == [
            ("COMPLETED", closed[-1]["close_t"])
        ]
        decision = app.decisions.get(rows[0]["decision_id"])["decision"]
        assert decision["capture_source"] == "scheduled_evaluation"
        assert rows[0]["contention_deferrals"] == 0
        from johanna.shadow_evaluation import T4_STATES

        detail = app.shadow.cycle(rows[0]["id"])["evaluation"]
        t4 = [g for g in detail["gates"] if g["gate"] == "T4.window_validation"]
        assert len(t4) == 1 and t4[0]["reason_code"] in T4_STATES  # the real verdict state
        assert detail["data_quality"] == t4[0]["reason_code"]
        assert t4[0]["result"] == ("PASS" if t4[0]["reason_code"] == "accepted" else "FAIL")
    finally:
        app.stop()
