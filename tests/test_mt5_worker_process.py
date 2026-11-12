"""MT5 worker processes: real IPC, GIL isolation, timeout recovery and clean shutdown.

The real MetaTrader5 package holds the GIL for the whole of each call (measured 0.25-3 s for
bar requests), so MT5 must run outside the backend process. These tests spawn real child
processes running an ``MT5Session`` over ``FakeMT5``; no terminal is involved.
"""

from __future__ import annotations

import multiprocessing
import threading
import time

import pytest
from fake_mt5_session import (
    make_failing_session,
    make_fake_session,
    make_flag_session,
    make_offline_session,
)

from johanna.live_market import MarketUnavailable, ProcessSessionRunner


@pytest.fixture
def runner_factory():
    runners: list[ProcessSessionRunner] = []

    def make(*args, **kwargs) -> ProcessSessionRunner:
        runner = ProcessSessionRunner(*args, **kwargs)
        runners.append(runner)
        return runner

    yield make
    for runner in runners:
        runner.close()


def test_worker_process_serves_quotes_and_bars(runner_factory) -> None:
    runner = runner_factory(make_fake_session, (), name="test")
    quotes = runner.run("quotes", (("EURUSD", "GBPUSD"),), timeout=20)
    assert quotes["identity"]["server"] == "Exness-MT5Trial9"
    assert quotes["value"]["EURUSD"]["bid"] == 1.08512
    assert quotes["value"]["GBPUSD"] == {"error": "symbol_not_offered"}
    bars = runner.run("bars", ("EURUSD", "H1", 50), timeout=20)
    assert len(bars["value"]["bars"]) == 50
    assert runner.pid is not None


def test_slow_mt5_call_in_worker_does_not_stall_the_backend(runner_factory) -> None:
    runner = runner_factory(make_fake_session, (1.5,), name="test")
    runner.run("quotes", (("EURUSD",),), timeout=20)  # warm up: worker started, connected
    gaps: list[float] = []
    done = threading.Event()

    def ticker() -> None:
        last = time.perf_counter()
        while not done.is_set():
            time.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    thread = threading.Thread(target=ticker)
    thread.start()
    started = time.perf_counter()
    runner.run("quotes", (("EURUSD",),), timeout=20)  # child spins 1.5 s holding *its* GIL
    elapsed = time.perf_counter() - started
    done.set()
    thread.join()
    assert elapsed >= 1.4
    assert max(gaps) < 0.3, f"backend thread stalled {max(gaps):.2f} s"


class _Clock:
    """Injected monotonic clock for deterministic backoff tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_timed_out_worker_is_terminated_and_replaced_after_backoff(runner_factory) -> None:
    clock = _Clock()
    runner = runner_factory(make_fake_session, (3.0,), name="test", monotonic=clock)
    runner.run("bars", ("EURUSD", "H1", 10), timeout=20)
    first_pid = runner.pid
    started = time.perf_counter()
    with pytest.raises(MarketUnavailable) as caught:
        runner.run("quotes", (("EURUSD",),), timeout=1.0)
    assert caught.value.code == "timeout"
    assert time.perf_counter() - started < 2.5
    assert runner.pid is None  # killed
    assert runner.backoff_seconds == pytest.approx(2.0)
    # Within the backoff: answered at once, honestly, without spawning another process.
    started = time.perf_counter()
    with pytest.raises(MarketUnavailable) as caught:
        runner.run("bars", ("EURUSD", "H1", 10), timeout=20)
    assert caught.value.code == "timeout" and "Next attempt in 2 s" in caught.value.message
    assert time.perf_counter() - started < 0.5
    assert runner.spawn_count == 1
    clock.now += 2.0
    runner.run("bars", ("EURUSD", "H1", 10), timeout=20)  # a fresh worker answers
    assert runner.pid not in (None, first_pid)
    assert runner.spawn_count == 2 and runner.backoff_seconds == 0


def test_repeated_start_failures_back_off_exponentially_up_to_a_bound(runner_factory) -> None:
    clock = _Clock()
    runner = runner_factory(
        make_failing_session,
        (),
        name="test",
        backoff_initial=1.0,
        backoff_max=4.0,
        monotonic=clock,
    )
    delays = []
    for _ in range(5):
        with pytest.raises(MarketUnavailable) as caught:
            runner.run("quotes", (("EURUSD",),), timeout=20)
        assert caught.value.code == "worker_failed"
        delays.append(runner.backoff_seconds)
        spawned = runner.spawn_count
        for _ in range(3):  # requests during the backoff never spawn
            with pytest.raises(MarketUnavailable) as caught:
                runner.run("quotes", (("EURUSD",),), timeout=20)
            assert caught.value.code == "worker_failed" and "Next attempt" in caught.value.message
        assert runner.spawn_count == spawned
        clock.now += runner.backoff_seconds
    assert delays == [1.0, 2.0, 4.0, 4.0, 4.0]
    assert runner.spawn_count == 5


def test_failed_worker_recovers_without_restarting_the_application(
    runner_factory, tmp_path
) -> None:
    clock = _Clock()
    flag = tmp_path / "broken"
    flag.write_text("x")
    runner = runner_factory(make_flag_session, (str(flag),), name="test", monotonic=clock)
    for _ in range(2):
        with pytest.raises(MarketUnavailable):
            runner.run("quotes", (("EURUSD",),), timeout=20)
        clock.now += runner.backoff_seconds
    flag.unlink()  # the cause goes away
    reply = runner.run("quotes", (("EURUSD",),), timeout=20)
    assert reply["value"]["EURUSD"]["bid"] == 1.08512
    assert runner.backoff_seconds == 0  # success resets the backoff
    flag.write_text("x")
    runner.close()  # next failure starts again from the initial delay
    with pytest.raises(MarketUnavailable):
        runner.run("quotes", (("EURUSD",),), timeout=20)
    assert runner.backoff_seconds == pytest.approx(2.0)


def test_mt5_unavailability_from_a_healthy_worker_is_not_a_worker_failure(
    runner_factory,
) -> None:
    runner = runner_factory(make_offline_session, (), name="test")
    for _ in range(3):
        with pytest.raises(MarketUnavailable) as caught:
            runner.run("quotes", (("EURUSD",),), timeout=20)
        assert caught.value.code == "terminal_not_running"
    assert runner.spawn_count == 1 and runner.backoff_seconds == 0


def test_no_worker_process_survives_failures_and_close(runner_factory) -> None:
    clock = _Clock()
    runner = runner_factory(make_fake_session, (3.0,), name="cleanup", monotonic=clock)
    runner.run("bars", ("EURUSD", "H1", 10), timeout=20)
    with pytest.raises(MarketUnavailable):
        runner.run("quotes", (("EURUSD",),), timeout=1.0)  # timeout: worker terminated
    clock.now += 10
    runner.run("bars", ("EURUSD", "H1", 10), timeout=20)  # replacement worker
    runner.close()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(
        child.name == "johanna-cleanup-worker" for child in multiprocessing.active_children()
    ):
        time.sleep(0.05)
    assert not [c for c in multiprocessing.active_children() if c.name == "johanna-cleanup-worker"]


def test_worker_that_cannot_start_is_reported(runner_factory) -> None:
    runner = runner_factory(make_failing_session, (), name="test")
    with pytest.raises(MarketUnavailable) as caught:
        runner.run("quotes", (("EURUSD",),), timeout=20)
    assert caught.value.code == "worker_failed"


def test_close_stops_the_worker(runner_factory) -> None:
    runner = runner_factory(make_fake_session, (), name="test")
    runner.run("quotes", (("EURUSD",),), timeout=20)
    process = runner._process
    runner.close()
    process.join(timeout=5)
    assert not process.is_alive()
