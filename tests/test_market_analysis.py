"""I1: T6 market analysis behind ``GET /api/market/analysis`` (synthetic data only; no MT5).

Snapshots come from the accepted T2 builder (``test_window_validation.snapshot``) and the
accepted T6 fixtures; the real ``LiveMarketService`` over ``FakeMT5`` covers the wiring path.
T2 freshness is never reimplemented here: outcomes come from the accepted pipeline.
"""

from __future__ import annotations

import ast
import json
import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from fake_mt5 import FakeMT5
from t6_fixtures import trend_bars
from test_analysis_engine import history
from test_window_validation import BASE, candle, hourly, snapshot

import johanna.analysis.decision as decision_module
import johanna.application as application_module
import johanna.market_analysis as market_analysis
from johanna.adapters.mt5 import MetaTrader5Adapter
from johanna.analysis.decision import (
    evaluate_market_snapshot,
    evaluate_market_snapshot_with_validation,
    required_candles,
)
from johanna.api import BackendAPI
from johanna.application import JohannaApplication
from johanna.config import AppConfig, StorageConfig
from johanna.live_market import LiveMarketService
from johanna.market_analysis import MarketAnalysisError, MarketAnalysisService

RETRIEVED = BASE + timedelta(seconds=0.2)  # bars.retrieved_at_utc of the snapshot helper
FRESH_AT = RETRIEVED + timedelta(seconds=0.8)


# ---------------------------------------------------------------- fakes
class FixedClock:
    def __init__(self, at: datetime, events: list | None = None) -> None:
        self.at, self.events, self.reads = at, events, 0
        self._lock = threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            self.reads += 1
            if self.events is not None:
                self.events.append("clock")
            return self.at


class SteppingClock:
    """Thread-safe: each read advances 0.1 s (so every read is distinguishable)."""

    def __init__(self, start: datetime) -> None:
        self.now, self._lock = start, threading.Lock()

    def __call__(self) -> datetime:
        with self._lock:
            current = self.now
            self.now += timedelta(seconds=0.1)
            return current


class FakeLive:
    """Stands in for LiveMarketService.snapshot; builds accepted T2 snapshots."""

    def __init__(self, make=None, *, gate=None, fail=None, events=None) -> None:
        self.make = make or (lambda symbol, timeframe, count, n: snapshot(trend_bars()))
        self.gate, self.fail, self.events = gate, fail, events
        self.calls: list[tuple[str, str, int]] = []
        self.started = threading.Event()
        self._lock = threading.Lock()

    def snapshot(self, symbol, timeframe, count):
        with self._lock:
            self.calls.append((symbol, timeframe, count))
            n = len(self.calls)
        self.started.set()
        if self.gate is not None:
            assert self.gate.wait(10), "test gate never released"
        if self.fail is not None:
            raise self.fail
        snap = dict(self.make(symbol, timeframe, count, n), snapshot_id=f"snap-{n}")
        if self.events is not None:
            self.events.append("snapshot_returned")
        return snap


def service(live, clock=None, **kwargs) -> MarketAnalysisService:
    return MarketAnalysisService(live, clock=clock or FixedClock(FRESH_AT), **kwargs)


def run_threads(fn, count):
    results, errors = [None] * count, [None] * count

    def work(i):
        try:
            results[i] = fn()
        except BaseException as exc:  # noqa: BLE001 - collected for assertions
            errors[i] = exc

    threads = [threading.Thread(target=work, args=(i,)) for i in range(count)]
    return threads, results, errors


def wait_until(predicate, timeout=10.0):
    done = threading.Event()
    for _ in range(int(timeout / 0.005)):
        if predicate():
            return True
        done.wait(0.005)
    return predicate()


# ---------------------------------------------------------------- A. invocation and output
def test_snapshot_request_count_clock_order_and_unchanged_result(monkeypatch) -> None:
    events: list[str] = []
    live = FakeLive(events=events)
    clock = FixedClock(FRESH_AT, events)
    seen = {}

    def spy(snap, *, analysed_at, parameters):
        seen.update(snapshot=snap, analysed_at=analysed_at)
        return evaluate_market_snapshot_with_validation(
            snap, analysed_at=analysed_at, parameters=parameters
        )

    monkeypatch.setattr(market_analysis, "evaluate_market_snapshot_with_validation", spy)
    envelope = service(live, clock).analyse("EURUSD", "H1")
    assert live.calls == [("EURUSD", "H1", required_candles() + 1)] == [("EURUSD", "H1", 662)]
    assert events.index("snapshot_returned") < events.index("clock")  # clock read afterwards
    assert seen["analysed_at"] is FRESH_AT
    direct = evaluate_market_snapshot(seen["snapshot"], analysed_at=FRESH_AT)
    assert envelope["result"] == direct  # nothing filtered, renamed or recalculated
    assert envelope["result"]["evaluated_at_utc"] == "2026-11-11T14:00:31Z"
    assert envelope["result"]["decision"]["actionable"] is False


def test_envelope_shape_and_snapshot_summary() -> None:
    envelope = service(FakeLive()).analyse("EURUSD", "H1")
    assert set(envelope) == {
        "symbol",
        "timeframe",
        "served_at_utc",
        "service",
        "snapshot",
        "window_validation",  # additive T4 diagnostic field
        "result",
    }
    assert set(envelope["service"]) == {
        "joined_in_flight",
        "requested_count",
        "timing_ms",
        "data_age_at_evaluation_ms",
    }
    assert set(envelope["service"]["timing_ms"]) == {"snapshot", "evaluation", "total"}
    assert envelope["snapshot"] == {
        "snapshot_id": "snap-1",
        "upstream_status": "LIVE",
        "upstream_reason": None,
        "source": "mt5",
        "bars_retrieved_at_utc": "2026-11-11T14:00:30.200000Z",
        "bars_served_from_cache": False,
        "eligibility_at_assembly": "live",
    }
    assert envelope["service"]["joined_in_flight"] is False
    assert envelope["service"]["requested_count"] == 662
    assert envelope["service"]["data_age_at_evaluation_ms"] == 800.0
    json.dumps(envelope, allow_nan=False)


def test_timing_uses_the_monotonic_clock_and_is_consistent() -> None:
    ticks = iter([10.0, 10.8125, 10.8705])
    envelope = service(FakeLive(), monotonic=lambda: next(ticks)).analyse("EURUSD", "H1")
    assert envelope["service"]["timing_ms"] == {
        "snapshot": 812.5,
        "evaluation": 58.0,
        "total": 870.5,
    }


def test_one_info_log_per_computation_without_prices(caplog) -> None:
    caplog.set_level("INFO", logger="johanna.market_analysis")
    service(FakeLive()).analyse("EURUSD", "H1")
    records = [r for r in caplog.records if r.name == "johanna.market_analysis"]
    assert len(records) == 1
    message = records[0].getMessage()
    assert message.startswith("market analysis EURUSD H1: TRADE_CANDIDATE R7 (snapshot ")
    assert "1.1" not in message  # no prices


def test_unsupported_timeframe_is_refused_before_any_snapshot() -> None:
    live = FakeLive()
    for timeframe in ("M1", "M5", "M15", "M30"):
        with pytest.raises(ValueError):
            service(live).analyse("EURUSD", timeframe)
    assert live.calls == []


# ---------------------------------------------------------------- B. no completed-result reuse
def test_sequential_requests_always_compute_again(monkeypatch) -> None:
    evaluations = []
    real = market_analysis.evaluate_market_snapshot_with_validation

    def counting(snap, **kwargs):
        evaluations.append(snap["snapshot_id"])
        return real(snap, **kwargs)

    monkeypatch.setattr(market_analysis, "evaluate_market_snapshot_with_validation", counting)
    live = FakeLive()
    svc = service(live)
    first = svc.analyse("EURUSD", "H1")
    second = svc.analyse("EURUSD", "H1")  # immediately afterwards, same clock reading
    assert len(live.calls) == 2 and evaluations == ["snap-1", "snap-2"]
    assert first["result"]["inputs"]["snapshot_id"] == "snap-1"
    assert second["result"]["inputs"]["snapshot_id"] == "snap-2"
    assert first["result"] is not second["result"]
    assert svc.waiting("EURUSD", "H1") is None  # nothing retained after completion


# ---------------------------------------------------------------- C. single-flight
@pytest.mark.parametrize("count", [2, 6])
def test_concurrent_requests_share_one_computation(monkeypatch, caplog, count) -> None:
    caplog.set_level("INFO", logger="johanna.market_analysis")
    evaluations = []
    real = market_analysis.evaluate_market_snapshot_with_validation

    def counting(snap, **kwargs):
        evaluations.append(1)
        return real(snap, **kwargs)

    monkeypatch.setattr(market_analysis, "evaluate_market_snapshot_with_validation", counting)
    gate = threading.Event()
    live = FakeLive(gate=gate)
    svc = service(live, SteppingClock(RETRIEVED + timedelta(seconds=0.5)))
    threads, results, errors = run_threads(lambda: svc.analyse("EURUSD", "H1"), count)
    threads[0].start()
    assert live.started.wait(10)  # the leader is inside snapshot()
    for t in threads[1:]:
        t.start()
    assert wait_until(lambda: svc.waiting("EURUSD", "H1") == count - 1)
    gate.set()
    for t in threads:
        t.join(10)
    assert errors == [None] * count
    assert len(live.calls) == 1 and len(evaluations) == 1
    assert sorted(r["service"]["joined_in_flight"] for r in results) == [False] + [True] * (
        count - 1
    )
    leader = next(r for r in results if not r["service"]["joined_in_flight"])
    for r in results:
        assert r["result"] == leader["result"]  # identical underlying T6 result
        assert r["result"]["inputs"]["snapshot_id"] == "snap-1"
        assert r["result"]["evaluated_at_utc"] == leader["result"]["evaluated_at_utc"]
        assert r["snapshot"] == leader["snapshot"]
    served = [r["served_at_utc"] for r in results]
    assert len(set(served)) == count  # generated per response
    assert leader["result"]["evaluated_at_utc"] not in served  # never substituted
    assert svc.waiting("EURUSD", "H1") is None
    assert len([r for r in caplog.records if r.name == "johanna.market_analysis"]) == 1


def test_different_keys_compute_independently_and_concurrently() -> None:
    gbp_started = threading.Event()

    def make(symbol, timeframe, count, n):
        if symbol == "EURUSD":  # blocks until the other key's fetch is running
            assert gbp_started.wait(10), "keys blocked each other"
        else:
            gbp_started.set()
        return snapshot(trend_bars(), symbol=symbol)

    live = FakeLive(make)
    svc = service(live)
    out = {}
    eur = threading.Thread(target=lambda: out.update(eur=svc.analyse("EURUSD", "H1")))
    gbp = threading.Thread(target=lambda: out.update(gbp=svc.analyse("GBPUSD", "H1")))
    eur.start()
    gbp.start()
    eur.join(15)
    gbp.join(15)
    assert {c[0] for c in live.calls} == {"EURUSD", "GBPUSD"} and len(live.calls) == 2
    assert out["eur"]["service"]["joined_in_flight"] is False
    assert out["gbp"]["service"]["joined_in_flight"] is False
    assert out["gbp"]["result"]["symbol"] == "GBPUSD"


# ---------------------------------------------------------------- D. cleanup
def test_failure_reaches_every_waiter_and_is_not_cached() -> None:
    gate = threading.Event()
    boom = RuntimeError("worker exploded")
    live = FakeLive(gate=gate, fail=boom)
    svc = service(live)
    threads, results, errors = run_threads(lambda: svc.analyse("EURUSD", "H1"), 4)
    threads[0].start()
    assert live.started.wait(10)
    for t in threads[1:]:
        t.start()
    assert wait_until(lambda: svc.waiting("EURUSD", "H1") == 3)
    gate.set()
    for t in threads:
        t.join(10)
    assert results == [None] * 4
    assert all(isinstance(e, MarketAnalysisError) and e.__cause__ is boom for e in errors)
    assert svc.waiting("EURUSD", "H1") is None
    live.fail, live.gate = None, None
    recovered = svc.analyse("EURUSD", "H1")  # a new computation, not the cached failure
    assert recovered["result"]["decision"]["rule"] == "R7" and len(live.calls) == 2


def test_interrupted_leader_releases_waiters() -> None:
    class Interrupt(BaseException):
        pass

    gate = threading.Event()
    live = FakeLive(gate=gate, fail=Interrupt())
    svc = service(live)
    threads, results, errors = run_threads(lambda: svc.analyse("EURUSD", "H1"), 2)
    threads[0].start()
    assert live.started.wait(10)
    threads[1].start()
    assert wait_until(lambda: svc.waiting("EURUSD", "H1") == 1)
    gate.set()
    for t in threads:
        t.join(10)
    assert isinstance(errors[0], Interrupt)  # BaseException is not swallowed
    assert isinstance(errors[1], MarketAnalysisError) and "did not complete" in str(errors[1])
    assert svc.waiting("EURUSD", "H1") is None


# ---------------------------------------------------------------- F. freshness (T2 rule, unchanged)
def analyse_at(age_seconds, **snap_kwargs):
    live = FakeLive(lambda s, t, c, n: snapshot(trend_bars(), **snap_kwargs))
    return service(live, FixedClock(RETRIEVED + timedelta(seconds=age_seconds))).analyse(
        "EURUSD", "H1"
    )


def test_exactly_five_seconds_is_evaluated() -> None:
    env = analyse_at(5.0)
    assert env["result"]["decision"]["rule"] == "R7"
    assert env["result"]["inputs"]["evaluation_time"]["passed"] is True
    assert env["service"]["data_age_at_evaluation_ms"] == 5000.0


def test_five_point_zero_zero_one_seconds_is_refused() -> None:
    env = analyse_at(5.001)
    assert (env["result"]["decision"]["outcome"], env["result"]["decision"]["rule"]) == (
        "NO_TRADE",
        "R1",
    )
    assert env["result"]["inputs"]["validation_verdict"] == "rejected"
    assert env["service"]["data_age_at_evaluation_ms"] == 5001.0


def test_cached_observation_older_than_five_seconds_is_refused() -> None:
    env = analyse_at(5.5, served_from_cache=True)
    assert env["snapshot"]["bars_served_from_cache"] is True
    assert env["result"]["decision"]["rule"] == "R1"
    assert env["service"]["data_age_at_evaluation_ms"] == 5500.0


def test_negative_age_fails_closed() -> None:
    env = analyse_at(-0.001)
    assert env["result"]["decision"]["rule"] == "R1"
    assert env["service"]["data_age_at_evaluation_ms"] == -1.0


# ---------------------------------------------------------------- G. upstream outcomes
@pytest.mark.parametrize(
    ("bars", "overrides", "upstream"),
    [
        (trend_bars, {"status": "CACHED", "reason": "mt5_not_running"}, "CACHED"),
        (lambda: [], {"status": "UNAVAILABLE", "reason": "mt5_not_running"}, "UNAVAILABLE"),
        (
            trend_bars,
            {"status": "CACHED", "reason": "mt5_not_running", "source": "parquet_archive"},
            "CACHED",
        ),
    ],
)
def test_mt5_unavailable_paths_give_r1(bars, overrides, upstream) -> None:
    live = FakeLive(lambda s, t, c, n: snapshot(bars(), **overrides))
    env = service(live).analyse("EURUSD", "H1")
    assert env["snapshot"]["upstream_status"] == upstream
    assert env["snapshot"]["eligibility_at_assembly"] == "refused"
    assert (env["result"]["decision"]["outcome"], env["result"]["decision"]["rule"]) == (
        "NO_TRADE",
        "R1",
    )
    assert env["result"]["market_condition"] is None and env["result"]["opportunity"] is None


def _provisional_bars():
    bars = trend_bars()
    last = bars[-1]
    bars[-1] = candle(
        last["t"], o=last["o"], h=last["h"], low=last["l"], c=last["c"], closure="clock_only"
    )
    return bars


@pytest.mark.parametrize("count", [100, 660])
def test_insufficient_history_is_refused_by_t4_before_t6(count) -> None:
    # Production asks T4 for 661 closed candles, so a shorter history is rejected by T4's
    # completeness check (insufficient_history) and T6 reports R1. R3/R5 (seen in the T6 unit
    # tests, which validate exactly the candles they have) are not reachable through this path.
    live = FakeLive(lambda s, t, c, n: snapshot(history(count)))
    env = service(live).analyse("EURUSD", "H1")
    assert (env["result"]["decision"]["rule"], env["result"]["inputs"]["validation_verdict"]) == (
        "R1",
        "rejected",
    )


@pytest.mark.parametrize(
    ("bars", "rule", "outcome"),
    [
        (_provisional_bars, "R2", "NO_TRADE"),
        (lambda: history(700), "R4", "NO_TRADE"),
        (lambda: history(661), "R6", "MARKET_CONDITION"),
        (trend_bars, "R7", "TRADE_CANDIDATE"),
        (lambda: trend_bars(sign=-1), "R7", "TRADE_CANDIDATE"),
    ],
)
def test_accepted_t6_outcomes_pass_through(bars, rule, outcome) -> None:
    live = FakeLive(lambda s, t, c, n: snapshot(bars()))
    env = service(live).analyse("EURUSD", "H1")
    assert (env["result"]["decision"]["rule"], env["result"]["decision"]["outcome"]) == (
        rule,
        outcome,
    )
    assert env["result"]["inputs"]["provenance"]["mode"] == "constructed"


# ---------------------------------------------------------------- H. API
def _app(tmp_path, live=None, running=lambda: True) -> JohannaApplication:
    config = replace(AppConfig.load(environ={}), storage=StorageConfig(tmp_path))
    live = live or LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=FakeMT5()),
        terminal_running=running,
        reconnect_interval=0,
    )
    return JohannaApplication(config, live_market=live)


def _call(api, path, query="", method="GET", **environ):
    response: dict = {}
    body = b"".join(
        api(
            {
                "PATH_INFO": path,
                "REQUEST_METHOD": method,
                "QUERY_STRING": query,
                "wsgi.input": BytesIO(),
                **environ,
            },
            lambda status, headers: response.update(status=status),
        )
    )
    return response["status"], json.loads(body)


def _api_with(tmp_path, live, clock=None):
    app = _app(tmp_path)
    app.market_analysis = service(live, clock)
    return BackendAPI(app)


@pytest.mark.parametrize("timeframe", ["H1", "H4", "D1"])
def test_api_valid_requests_return_the_envelope(tmp_path, timeframe) -> None:
    live = FakeLive()
    status, body = _call(
        _api_with(tmp_path, live), "/api/market/analysis", f"symbol=eurusd&timeframe={timeframe}"
    )
    assert status == "200 OK"
    assert (body["symbol"], body["timeframe"]) == ("EURUSD", timeframe)
    assert live.calls == [("EURUSD", timeframe, 662)]
    assert body["result"]["decision"]["actionable"] is False


def test_api_result_is_unchanged_and_query_extras_are_ignored(tmp_path, monkeypatch) -> None:
    seen = {}
    real = market_analysis.evaluate_market_snapshot_with_validation

    def spy(snap, **kwargs):
        seen.update(snapshot=snap, analysed_at=kwargs["analysed_at"])
        return real(snap, **kwargs)

    monkeypatch.setattr(market_analysis, "evaluate_market_snapshot_with_validation", spy)
    live = FakeLive()
    query = "symbol=EURUSD&timeframe=H1&count=5&analysed_at=2030-01-01T00:00:00Z"
    status, body = _call(_api_with(tmp_path, live), "/api/market/analysis", query)
    assert status == "200 OK"
    assert live.calls == [("EURUSD", "H1", 662)]  # count ignored
    assert seen["analysed_at"] is FRESH_AT  # the backend clock, not the query
    direct = evaluate_market_snapshot(seen["snapshot"], analysed_at=FRESH_AT)
    assert body["result"] == json.loads(json.dumps(direct))


@pytest.mark.parametrize(
    ("query", "error"),
    [
        ("symbol=&timeframe=H1", "invalid_analysis_request"),
        ("symbol=EUR%20USD&timeframe=H1", "invalid_analysis_request"),
        ("symbol=EURUSD&timeframe=W1", "invalid_analysis_request"),
        ("symbol=EURUSD&timeframe=M1", "analysis_unsupported_timeframe"),
        ("symbol=EURUSD&timeframe=M5", "analysis_unsupported_timeframe"),
        ("symbol=EURUSD&timeframe=M30", "analysis_unsupported_timeframe"),
    ],
)
def test_api_rejects_bad_requests_before_any_snapshot(tmp_path, query, error) -> None:
    live = FakeLive()
    status, body = _call(_api_with(tmp_path, live), "/api/market/analysis", query)
    assert status == "400 Bad Request"
    assert body["error"] == error and body["message"]
    assert live.calls == []


def test_api_is_get_only_and_keeps_host_and_origin_checks(tmp_path) -> None:
    live = FakeLive()
    api = _api_with(tmp_path, live)
    status, body = _call(api, "/api/market/analysis", "symbol=EURUSD&timeframe=H1", method="POST")
    assert status == "405 Method Not Allowed" and body == {"error": "method_not_allowed"}
    status, body = _call(
        api, "/api/market/analysis", "symbol=EURUSD&timeframe=H1", HTTP_HOST="evil.example"
    )
    assert status.startswith("421") and body == {"error": "host_not_allowed"}
    status, _ = _call(
        api,
        "/api/market/analysis",
        "symbol=EURUSD&timeframe=H1",
        HTTP_HOST="127.0.0.1:8765",
        HTTP_ORIGIN="http://evil.example",
    )
    assert status.startswith("421")
    assert live.calls == []


def test_api_unexpected_exception_is_a_500(tmp_path) -> None:
    live = FakeLive(fail=RuntimeError("boom"))
    status, body = _call(
        _api_with(tmp_path, live), "/api/market/analysis", "symbol=EURUSD&timeframe=H1"
    )
    assert status == "500 Internal Server Error" and body == {"error": "internal_error"}


def test_api_through_the_real_live_service_over_fake_mt5(tmp_path) -> None:
    app = _app(tmp_path)
    app.start()
    try:
        status, body = _call(BackendAPI(app), "/api/market/analysis", "symbol=EURUSD&timeframe=H1")
    finally:
        app.stop()
    assert status == "200 OK"
    assert body["service"]["requested_count"] == 662
    assert body["snapshot"]["source"] == "mt5" and body["snapshot"]["upstream_status"] == "LIVE"
    assert body["result"]["inputs"]["provenance"]["mode"] == "constructed"
    assert body["result"]["decision"]["actionable"] is False


def test_api_with_the_terminal_down_gives_r1(tmp_path) -> None:
    app = _app(tmp_path, running=lambda: False)
    app.start()
    try:
        status, body = _call(BackendAPI(app), "/api/market/analysis", "symbol=EURUSD&timeframe=H1")
    finally:
        app.stop()
    assert status == "200 OK"
    assert body["snapshot"]["upstream_status"] == "UNAVAILABLE"
    assert body["result"]["decision"]["rule"] == "R1"


# ---------------------------------------------------------------- clock wiring
def test_application_injects_one_clock_into_both_services(tmp_path) -> None:
    config = replace(AppConfig.load(environ={}), storage=StorageConfig(tmp_path))
    app = JohannaApplication(config)  # default live service; not started, no MT5 contact
    assert app.live_market._clock is application_module._utc_now
    assert app.market_analysis._clock is app.live_market._clock
    injected = _app(tmp_path / "other")
    assert injected.market_analysis._clock is injected.live_market._clock


# ---------------------------------------------------------------- I. read-only inspection
def test_module_imports_only_the_live_service_contract_t6_and_stdlib() -> None:
    source = Path(market_analysis.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module)
    assert imported == {
        "__future__",
        "copy",
        "logging",
        "threading",
        "time",
        "collections.abc",
        "dataclasses",
        "datetime",
        "typing",
        "johanna.analysis.decision",  # the accepted T6 entry point
        "johanna.analysis.engine",  # SUPPORTED_TIMEFRAMES constant only
        "johanna.analysis.snapshot",  # T2 evaluate_snapshot, for the diagnostic data age
    }
    calls = {
        node.func.attr
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    live_calls = {c for c in calls if c in {"snapshot", "bars", "quotes", "_request", "run"}}
    assert live_calls == {"snapshot"}
    for forbidden in (
        "order_send",
        "order_check",
        "positions_get",
        "orders_get",
        "account_info",
        "MetaTrader5",
        "adapters",
        "initialize",
        "login",
        "_bar_runner",
    ):
        assert forbidden not in source


# ---------------------------------------------------------------- T4 diagnostic exposure
_WV_KEYS = {
    "available",
    "window_version",
    "snapshot_id",
    "evaluated_at_utc",
    "requested",
    "window",
    "excluded_candles",
    "verdict",
    "reason_codes",
    "dimensions",
    "recency",
}


@pytest.fixture
def companion_spy(monkeypatch):
    """Captures what the service received from the companion, and counts T4 calls."""
    record = {"paired": [], "t4_calls": 0}
    real_companion = market_analysis.evaluate_market_snapshot_with_validation
    real_t4 = decision_module.validate_window

    def companion(snap, **kwargs):
        out = real_companion(snap, **kwargs)
        record["paired"].append(out)
        return out

    def t4(*args, **kwargs):
        record["t4_calls"] += 1
        return real_t4(*args, **kwargs)

    monkeypatch.setattr(market_analysis, "evaluate_market_snapshot_with_validation", companion)
    monkeypatch.setattr(decision_module, "validate_window", t4)
    return record


def _diagnostics(bars_factory, *, age=0.8, timeframe="H1", **snap_kwargs):
    live = FakeLive(lambda s, t, c, n: snapshot(bars_factory(), timeframe=timeframe, **snap_kwargs))
    clock = FixedClock(RETRIEVED + timedelta(seconds=age))
    return service(live, clock).analyse("EURUSD", timeframe)


def _assert_verbatim(wv, validation) -> None:
    """Every exposed field equals the companion's actual T4 object (minus omitted parts)."""
    assert wv["available"] is True
    for name in ("window_version", "snapshot_id", "evaluated_at_utc", "requested"):
        assert wv[name] == validation[name]
    assert wv["window"] == {k: validation["window"][k] for k in ("count", "first_t", "last_t")}
    assert wv["excluded_candles"] == validation["excluded_candles"]
    assert wv["verdict"] == validation["verdict"]
    assert wv["reason_codes"] == [r["code"] for r in validation["verdict"]["reasons"]]
    assert wv["dimensions"] == {
        name: {"status": d["status"], "reasons": d["reasons"]}
        for name, d in validation["dimensions"].items()
    }
    assert ("d1_convention" in wv) == ("d1_convention" in validation)
    if "d1_convention" in wv:
        assert wv["d1_convention"] == validation["d1_convention"]
    assert set(wv) - {"d1_convention"} == _WV_KEYS


def test_insufficient_history_is_exposed_from_t4(companion_spy) -> None:
    env = _diagnostics(lambda: history(660))
    wv = env["window_validation"]
    _assert_verbatim(wv, companion_spy["paired"][0]["validation"])
    assert wv["reason_codes"] == ["insufficient_history"]
    assert wv["verdict"] == {
        "state": "rejected",
        "reasons": [
            {
                "dimension": "completeness",
                "code": "insufficient_history",
                "severity": "fail",
                "detail": "660 closed candle(s) available, 661 required",
            }
        ],
    }
    assert wv["dimensions"]["completeness"]["status"] == "fail"
    assert env["result"]["decision"]["rule"] == "R1"  # T6 result unchanged


def test_stale_snapshot_exposes_snapshot_refused_with_t2_reasons(companion_spy) -> None:
    env = _diagnostics(trend_bars, age=5.001)
    wv = env["window_validation"]
    _assert_verbatim(wv, companion_spy["paired"][0]["validation"])
    assert wv["reason_codes"] == ["snapshot_refused"]
    assert wv["verdict"]["reasons"][0]["snapshot_reasons"] == ["snapshot_too_old"]
    assert wv["dimensions"]["snapshot"]["status"] == "fail"


def test_exactly_five_seconds_keeps_t2_eligibility(companion_spy) -> None:
    env = _diagnostics(trend_bars, age=5.0)
    wv = env["window_validation"]
    _assert_verbatim(wv, companion_spy["paired"][0]["validation"])
    assert wv["verdict"] == {"state": "accepted", "reasons": []}
    assert wv["reason_codes"] == []  # T4 reported no reasons
    assert wv["dimensions"]["snapshot"] == {"status": "pass", "reasons": []}
    assert env["result"]["decision"]["rule"] == "R7"


def test_clock_only_closure_exposes_the_provisional_reason(companion_spy) -> None:
    # E1: a T1-consistent clock_only close: tick 30 s before the 13:00 close, read 90 s after it
    # (tick 120 s old: verified); the service clock reads 0.8 s after the bars (as elsewhere).
    read = datetime(2026, 11, 11, 13, 1, 30, tzinfo=UTC)
    env = _diagnostics(
        _provisional_bars,
        age=(read + timedelta(seconds=0.2) - RETRIEVED).total_seconds() + 0.8,
        tick_time=datetime(2026, 11, 11, 12, 59, 30, tzinfo=UTC),
        read_at=read,
    )
    wv = env["window_validation"]
    _assert_verbatim(wv, companion_spy["paired"][0]["validation"])
    assert wv["verdict"]["state"] == "provisional"
    assert wv["reason_codes"] == ["clock_only_closure"]
    assert wv["dimensions"]["closure"]["status"] == "provisional"
    assert env["result"]["decision"]["rule"] == "R2"


def test_all_seven_dimensions_and_omissions(companion_spy) -> None:
    wv = _diagnostics(trend_bars)["window_validation"]
    assert set(wv["dimensions"]) == {
        "price",
        "timestamps",
        "closure",
        "continuity",
        "completeness",
        "snapshot",
        "session_candles",
        "recency",  # E1
    }
    assert "candles" not in wv["window"]
    assert all(set(d) == {"status", "reasons"} for d in wv["dimensions"].values())
    assert "evidence" not in json.dumps(wv)


def test_d1_convention_only_when_t4_supplies_it(companion_spy) -> None:
    d1 = _diagnostics(lambda: hourly("2024-01-01T00:00:00", 700, "D1"), timeframe="D1")
    validation = companion_spy["paired"][0]["validation"]
    assert "d1_convention" in validation
    _assert_verbatim(d1["window_validation"], validation)
    h1 = _diagnostics(trend_bars)
    assert "d1_convention" not in companion_spy["paired"][1]["validation"]
    assert "d1_convention" not in h1["window_validation"]


def test_diagnostics_belong_to_the_same_evaluation(companion_spy) -> None:
    env = service(FakeLive(), SteppingClock(RETRIEVED + timedelta(seconds=0.5))).analyse(
        "EURUSD", "H1"
    )
    wv, result = env["window_validation"], env["result"]
    assert companion_spy["t4_calls"] == 1
    assert wv["snapshot_id"] == result["inputs"]["snapshot_id"] == env["snapshot"]["snapshot_id"]
    assert wv["evaluated_at_utc"] == result["evaluated_at_utc"]
    assert wv["evaluated_at_utc"] != env["served_at_utc"]  # never the serving time
    assert wv["window"] == result["inputs"]["window"]
    assert wv["verdict"]["state"] == result["inputs"]["validation_verdict"]
    assert "window_validation" not in result and "validation" not in result
    assert result is companion_spy["paired"][0]["result"]  # the leader's object, unmodified


def test_api_round_trip_preserves_result_and_diagnostics(tmp_path, companion_spy) -> None:
    status, body = _call(
        _api_with(tmp_path, FakeLive(lambda s, t, c, n: snapshot(history(660)))),
        "/api/market/analysis",
        "symbol=EURUSD&timeframe=H1",
    )
    assert status == "200 OK"
    paired = companion_spy["paired"][0]
    assert body["result"] == json.loads(json.dumps(paired["result"]))
    _assert_verbatim(body["window_validation"], json.loads(json.dumps(paired["validation"])))
    assert body["window_validation"]["reason_codes"] == ["insufficient_history"]


def test_joiners_get_identical_diagnostics_without_shared_objects(companion_spy) -> None:
    gate = threading.Event()
    live = FakeLive(gate=gate)
    svc = service(live)
    threads, results, errors = run_threads(lambda: svc.analyse("EURUSD", "H1"), 4)
    threads[0].start()
    assert live.started.wait(10)
    for t in threads[1:]:
        t.start()
    assert wait_until(lambda: svc.waiting("EURUSD", "H1") == 3)
    gate.set()
    for t in threads:
        t.join(10)
    assert errors == [None] * 4
    assert companion_spy["t4_calls"] == 1  # one computation, one T4 run
    first = results[0]["window_validation"]
    for r in results[1:]:
        assert r["window_validation"] == first
        assert r["window_validation"] is not first
        assert r["window_validation"]["verdict"] is not first["verdict"]


def test_sequential_requests_run_t4_separately_and_cache_nothing(companion_spy) -> None:
    svc = service(FakeLive())
    first = svc.analyse("EURUSD", "H1")["window_validation"]
    second = svc.analyse("EURUSD", "H1")["window_validation"]
    assert companion_spy["t4_calls"] == 2
    assert (first["snapshot_id"], second["snapshot_id"]) == ("snap-1", "snap-2")
    paired = companion_spy["paired"]
    assert paired[0]["validation"] is not paired[1]["validation"]


def test_refuse_before_t4_is_represented_with_the_t6_reason(companion_spy) -> None:
    naive = FixedClock(datetime(2026, 11, 11, 14, 0, 31))  # no tzinfo: refused before T4
    env = service(FakeLive(), naive).analyse("EURUSD", "H1")
    assert companion_spy["t4_calls"] == 0
    reason = env["result"]["decision"]["reasons"][0]
    assert env["window_validation"] == {"available": False, "reason": f"T4 did not run: {reason}"}
    assert reason.startswith("analysed_at must be a timezone-aware datetime")
