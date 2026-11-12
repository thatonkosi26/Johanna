"""T2: coordinated market-data snapshot, component retrieval times, ages and eligibility.

Service-level tests use FakeMT5 behind the real adapter and LiveMarketService with an
injected stepping clock, so every retrieval and assembly time is deterministic. Pure tests
build snapshots from hand-written bars responses. No terminal is involved.
"""

from __future__ import annotations

import ast
import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from fake_mt5 import READ_ONLY_CALLS, FakeMT5

from johanna.adapters.mt5 import MetaTrader5Adapter
from johanna.analysis.snapshot import (
    LIVE,
    PROVISIONAL,
    REFUSED,
    SnapshotLimits,
    build_snapshot,
    clock_from_tick,
    evaluate_snapshot,
)
from johanna.api import BackendAPI
from johanna.application import JohannaApplication
from johanna.config import AppConfig, StorageConfig
from johanna.live_market import LiveMarketService, MarketUnavailable
from johanna.market_data import ParquetBarStore

BASE = datetime(2026, 11, 11, 10, 31, 30, tzinfo=UTC)  # in the past: no "future" refusals
ANALYSIS_PACKAGE = Path(__file__).resolve().parents[1] / "src" / "johanna" / "analysis"


class SteppingClock:
    """Each read advances 0.1 s; ``jump`` moves time forward."""

    def __init__(self, start: datetime = BASE, step: float = 0.1) -> None:
        self.now = start
        self.step = timedelta(seconds=step)

    def __call__(self) -> datetime:
        self.now += self.step
        return self.now

    def jump(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _service(fake: FakeMT5, clock: SteppingClock, **kwargs) -> LiveMarketService:
    service = LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake),
        terminal_running=kwargs.pop("terminal_running", lambda: True),
        reconnect_interval=0,
        clock=clock,
        **kwargs,
    )
    service.start()
    return service


def _fake(clock: SteppingClock, **kwargs) -> FakeMT5:
    return FakeMT5(now=lambda: clock.now.timestamp(), **kwargs)


def _reasons(snapshot_or_eval) -> set[str]:
    evaluation = snapshot_or_eval.get("eligibility", snapshot_or_eval)
    return {reason["code"] for reason in evaluation["reasons"]}


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _payload(**overrides) -> dict:
    """A minimal live bars response, as LiveMarketService.bars() returns it."""
    tick_time = BASE - timedelta(seconds=0.5)
    payload = {
        "symbol": "EURUSD",
        "broker_symbol": "EURUSDc",
        "timeframe": "H1",
        "digits": 5,
        "point": 0.00001,
        "pip_size": 0.0001,
        "bars": [
            {
                "t": "2026-11-11T09:00:00Z",
                "o": 1.1,
                "h": 1.2,
                "l": 1.0,
                "c": 1.15,
                "v": 10,
                "close_t": "2026-11-11T10:00:00Z",
                "complete": True,
                "closure": "tick_observed",
                "valid": True,
                "issues": [],
                "spread_points": 7,
            },
            {
                "t": "2026-11-11T10:00:00Z",
                "o": 1.15,
                "h": 1.16,
                "l": 1.14,
                "c": 1.155,
                "v": 4,
                "close_t": "2026-11-11T11:00:00Z",
                "complete": False,
                "closure": "forming",
                "valid": True,
                "issues": [],
                "spread_points": 7,
            },
        ],
        "rejected_bars": [],
        "quality": {
            "bars_received": 2,
            "bars_returned": 2,
            "invalid_bars": 0,
            "duplicates_rejected": 0,
            "out_of_order": 0,
        },
        "latest_tick_utc": _iso(tick_time),
        "tick": {
            "bid": 1.155,
            "ask": 1.1551,
            "time_utc": _iso(tick_time),
            "retrieved_at_utc": _iso(BASE),
        },
        "retrieved_at_utc": _iso(BASE + timedelta(seconds=0.2)),
        "source": "mt5",
        "time_basis": "utc",
        "status": "LIVE",
        "fetched_at_utc": _iso(BASE + timedelta(seconds=0.2)),
        "requested_at_utc": _iso(BASE + timedelta(seconds=0.3)),
        "served_from_cache": False,
        "reason": None,
        "message": None,
    }
    payload.update(overrides)
    return payload


def _snap(payload: dict | None = None, at_offset: float = 0.5, **kwargs) -> dict:
    return build_snapshot(
        payload or _payload(),
        snapshot_id="test",
        assembled_at=BASE + timedelta(seconds=at_offset),
        **kwargs,
    )


# ---------------------------------------------------------------- construction
def test_snapshot_from_the_live_service_has_every_component() -> None:
    clock = SteppingClock()
    fake = _fake(clock)
    service = _service(fake, clock)
    try:
        snapshot = service.snapshot("EURUSD", "H1", 50)
    finally:
        service.stop()
    assert len(snapshot["snapshot_id"]) == 32
    assert (snapshot["symbol"], snapshot["broker_symbol"], snapshot["timeframe"]) == (
        "EURUSD",
        "EURUSD",
        "H1",
    )
    assert snapshot["tick"]["bid"] == 1.08512 and snapshot["tick"]["ask"] == 1.08519
    assert snapshot["symbol_info"] == {"digits": 5, "point": 0.00001, "pip_size": 0.0001}
    assert snapshot["bars"]["count"] == 50 and snapshot["bars"]["served_from_cache"] is False
    assert snapshot["bars"]["items"][-1]["closure"] == "forming"  # T1 fields carried through
    assert {"closure", "valid", "issues"} <= set(snapshot["bars"]["items"][0])
    assert snapshot["clock"]["derived_from"] == "snapshot_tick"
    assert snapshot["clock"]["result"] == "verified"
    assert snapshot["eligibility"]["state"] == LIVE, snapshot["eligibility"]
    assert snapshot["eligibility"]["reasons"] == []
    json.dumps(snapshot, allow_nan=False)


def test_component_retrieval_times_are_recorded_separately_and_in_order() -> None:
    clock = SteppingClock()
    service = _service(_fake(clock), clock)
    try:
        snapshot = service.snapshot("EURUSD", "H1", 20)
    finally:
        service.stop()
    tick_at = datetime.fromisoformat(snapshot["tick"]["retrieved_at_utc"].replace("Z", "+00:00"))
    bars_at = datetime.fromisoformat(snapshot["bars"]["retrieved_at_utc"].replace("Z", "+00:00"))
    served = datetime.fromisoformat(snapshot["bars"]["served_at_utc"].replace("Z", "+00:00"))
    assembled = datetime.fromisoformat(snapshot["assembled_at_utc"].replace("Z", "+00:00"))
    assert tick_at < bars_at <= served < assembled  # sequential reads, never "simultaneous"
    ages = snapshot["ages"]
    assert ages["tick_vs_bars_seconds"] == pytest.approx((bars_at - tick_at).total_seconds())
    assert ages["snapshot_age_seconds"] == pytest.approx((assembled - bars_at).total_seconds())


# ---------------------------------------------------------------- clock from the snapshot tick
@pytest.mark.parametrize(
    ("age", "result"),
    [
        (0.5, "verified"),
        (119, "verified"),
        (121, "stale_ticks"),
        (-30, "verified"),
        (-61, "mismatch"),
    ],
)
def test_clock_check_uses_the_snapshot_tick(age, result) -> None:
    tick = {"time_utc": _iso(BASE - timedelta(seconds=age)), "retrieved_at_utc": _iso(BASE)}
    check = clock_from_tick(tick)
    assert check["result"] == result and check["derived_from"] == "snapshot_tick"
    assert check["tick_age_seconds"] == pytest.approx(age)


def test_snapshot_does_not_depend_on_the_quotes_worker() -> None:
    clock = SteppingClock()
    service = _service(_fake(clock), clock)

    def broken(*args, **kwargs):
        raise MarketUnavailable("timeout", "quotes worker is stuck")

    service._quote_runner.run = broken  # an unrelated quote-worker failure
    try:
        assert service.quotes(["EURUSD"])["quotes"][0]["status"] == "UNAVAILABLE"
        snapshot = service.snapshot("EURUSD", "H1", 20)
    finally:
        service.stop()
    assert snapshot["eligibility"]["state"] == LIVE
    assert snapshot["clock"]["result"] == "verified"


def test_stale_snapshot_tick_makes_the_snapshot_provisional() -> None:
    clock = SteppingClock()
    service = _service(_fake(clock, tick_age_seconds=600), clock)  # market closed or quiet
    try:
        snapshot = service.snapshot("EURUSD", "H1", 20)
    finally:
        service.stop()
    assert snapshot["clock"]["result"] == "stale_ticks"
    assert snapshot["eligibility"]["state"] == PROVISIONAL
    assert _reasons(snapshot) == {"clock_stale_ticks"}


def test_placeholder_snapshot_tick_is_refused() -> None:
    clock = SteppingClock()
    fake = _fake(clock)
    fake.symbols["EURUSD"]["loading"] = True  # MT5's all-zero tick before a symbol loads
    service = _service(fake, clock)
    try:
        snapshot = service.snapshot("EURUSD", "H1", 20)
    finally:
        service.stop()
    assert snapshot["tick"]["bid"] is None and snapshot["tick"]["time_utc"] is None
    assert snapshot["clock"]["result"] == "unavailable"
    assert snapshot["eligibility"]["state"] == REFUSED and "tick_missing" in _reasons(snapshot)


# ---------------------------------------------------------------- cached vs new observations
def test_cached_response_keeps_its_original_retrieval_time() -> None:
    clock = SteppingClock()
    fake = _fake(clock)
    running = {"value": True}
    service = _service(fake, clock, terminal_running=lambda: running["value"])
    try:
        first = service.snapshot("EURUSD", "H1", 20)
        clock.jump(2)  # within the 5 s bars cache
        again = service.snapshot("EURUSD", "H1", 20)
        assert again["snapshot_id"] != first["snapshot_id"]
        assert again["bars"]["served_from_cache"] is True
        assert again["bars"]["retrieved_at_utc"] == first["bars"]["retrieved_at_utc"]
        assert again["tick"] == first["tick"]  # the same observation, not a new one
        assert again["bars"]["served_at_utc"] > first["bars"]["served_at_utc"]
        assert again["ages"]["snapshot_age_seconds"] > first["ages"]["snapshot_age_seconds"]
        assert again["eligibility"]["state"] == LIVE  # still within one refresh interval
        assert fake.calls.count("copy_rates_from_pos") == 1
        # MT5 goes away: the old observation is served as CACHED and refused for live use.
        fake.running = running["value"] = False
        clock.jump(6)
        cached = service.snapshot("EURUSD", "H1", 20)
    finally:
        service.stop()
    assert cached["upstream"]["status"] == "CACHED"
    assert cached["bars"]["retrieved_at_utc"] == first["bars"]["retrieved_at_utc"]
    assert cached["eligibility"]["state"] == REFUSED
    assert {"mt5_unavailable_cached", "snapshot_too_old"} <= _reasons(cached)


# ---------------------------------------------------------------- missing / unavailable
def test_unavailable_mt5_is_refused_with_the_reason() -> None:
    clock = SteppingClock()
    service = _service(_fake(clock), clock, terminal_running=lambda: False)
    try:
        snapshot = service.snapshot("EURUSD", "H1", 20)
    finally:
        service.stop()
    assert snapshot["eligibility"]["state"] == REFUSED
    assert "bars_unavailable" in _reasons(snapshot)
    assert snapshot["upstream"]["reason"] == "terminal_not_running"
    assert snapshot["tick"] is None and snapshot["bars"]["count"] == 0


def test_server_clock_mismatch_is_refused() -> None:
    clock = SteppingClock()
    service = _service(_fake(clock, server_offset_seconds=7200), clock)
    try:
        snapshot = service.snapshot("EURUSD", "H1", 20)
    finally:
        service.stop()
    assert snapshot["eligibility"]["state"] == REFUSED and "clock_mismatch" in _reasons(snapshot)


def test_empty_bars_are_refused() -> None:
    clock = SteppingClock()
    fake = _fake(clock)
    fake.rates_hook = lambda symbol, rows: []
    service = _service(fake, clock)
    try:
        snapshot = service.snapshot("EURUSD", "H1", 20)
    finally:
        service.stop()
    assert snapshot["eligibility"]["state"] == REFUSED and _reasons(snapshot) == {"no_bars"}


def test_archived_bars_are_refused_for_live_use(tmp_path) -> None:
    ParquetBarStore(tmp_path / "collectors" / "mt5").ingest(
        [
            {
                "symbol": "EURUSD",
                "timeframe": "H1",
                "timestamp": BASE.replace(minute=0, second=0) - timedelta(hours=5),
                "open": 1.1,
                "high": 1.2,
                "low": 1.0,
                "close": 1.15,
                "tick_volume": 1,
                "real_volume": None,
                "source": "MT5",
                "source_timezone": "UTC",
                "source_timestamp_convention": "bar_open",
            }
        ],
        source="MT5",
    )
    config = replace(AppConfig.load(environ={}), storage=StorageConfig(tmp_path))
    app = JohannaApplication(config)  # conftest: terminal "not running"
    snapshot = app.live_market.snapshot("EURUSD", "H1", 50)
    app.live_market.stop()
    assert snapshot["source"] == "parquet_archive"
    assert snapshot["eligibility"]["state"] == REFUSED
    assert {"mt5_unavailable_cached", "not_live_source", "legacy_time_basis"} <= _reasons(snapshot)


def test_missing_components_are_refused() -> None:
    no_tick = _snap(_payload(tick=None))
    assert no_tick["eligibility"]["state"] == REFUSED and "tick_missing" in _reasons(no_tick)
    no_time = _snap(_payload(retrieved_at_utc=None))
    assert "timestamps_missing" in _reasons(no_time) and no_time["eligibility"]["state"] == REFUSED


# ---------------------------------------------------------------- age limits and consistency
@pytest.mark.parametrize(("offset", "state"), [(5.0, LIVE), (5.3, REFUSED)])
def test_snapshot_age_limit_is_one_refresh_interval(offset, state) -> None:
    snapshot = _snap(at_offset=0.3)  # bars retrieved at BASE + 0.2 s
    evaluation = evaluate_snapshot(snapshot, analysed_at=BASE + timedelta(seconds=offset))
    assert evaluation["state"] == state
    if state == REFUSED:
        assert {r["code"] for r in evaluation["reasons"]} == {"snapshot_too_old"}
    assert evaluation["evaluated_at_utc"] == _iso(BASE + timedelta(seconds=offset))


@pytest.mark.parametrize(("gap", "state"), [(2.0, LIVE), (2.5, REFUSED)])
def test_tick_versus_bars_retrieval_limit(gap, state) -> None:
    payload = _payload(retrieved_at_utc=_iso(BASE + timedelta(seconds=gap)))
    snapshot = _snap(payload, at_offset=gap + 0.1)
    assert snapshot["eligibility"]["state"] == state
    if state == REFUSED:
        assert "tick_bars_skew" in _reasons(snapshot)


def test_clock_versus_bars_limit() -> None:
    snapshot = _snap()
    snapshot["clock"]["checked_at_utc"] = _iso(BASE - timedelta(seconds=31))
    evaluation = evaluate_snapshot(snapshot, analysed_at=BASE + timedelta(seconds=1))
    assert "clock_bars_skew" in {r["code"] for r in evaluation["reasons"]}
    snapshot["clock"]["checked_at_utc"] = _iso(BASE - timedelta(seconds=29))
    evaluation = evaluate_snapshot(snapshot, analysed_at=BASE + timedelta(seconds=1))
    assert "clock_bars_skew" not in {r["code"] for r in evaluation["reasons"]}


def test_inconsistent_retrieval_times_are_refused() -> None:
    tick_after_bars = _payload(retrieved_at_utc=_iso(BASE - timedelta(seconds=1)))
    snapshot = _snap(tick_after_bars)
    assert "inconsistent_timestamps" in _reasons(snapshot)
    assert snapshot["eligibility"]["state"] == REFUSED
    future = _snap(at_offset=0.0)  # bars "retrieved" 0.2 s after the evaluation time
    assert "inconsistent_timestamps" in _reasons(future)


# ---------------------------------------------------------------- provisional outcomes
def test_invalid_bars_and_conflicting_duplicates_are_provisional() -> None:
    payload = _payload(
        quality={
            "bars_received": 3,
            "bars_returned": 2,
            "invalid_bars": 1,
            "duplicates_rejected": 1,
            "out_of_order": 0,
        },
        rejected_bars=[
            {
                "t": "2026-11-11T09:00:00Z",
                "reason": "duplicate_timestamp",
                "identical_to_kept": False,
            }
        ],
    )
    snapshot = _snap(payload)
    assert snapshot["eligibility"]["state"] == PROVISIONAL
    assert _reasons(snapshot) == {"invalid_bars_present", "conflicting_duplicates"}
    identical = _payload(
        rejected_bars=[
            {
                "t": "2026-11-11T09:00:00Z",
                "reason": "duplicate_timestamp",
                "identical_to_kept": True,
            }
        ]
    )
    assert "conflicting_duplicates" not in _reasons(_snap(identical))


def test_refused_outranks_provisional_and_every_reason_is_listed() -> None:
    payload = _payload(
        quality={
            "bars_received": 2,
            "bars_returned": 2,
            "invalid_bars": 1,
            "duplicates_rejected": 0,
            "out_of_order": 0,
        },
        retrieved_at_utc=_iso(BASE + timedelta(seconds=3)),
    )
    snapshot = _snap(payload, at_offset=9)
    assert snapshot["eligibility"]["state"] == REFUSED
    assert {"invalid_bars_present", "tick_bars_skew", "snapshot_too_old"} <= _reasons(snapshot)


# ---------------------------------------------------------------- determinism and contracts
def test_snapshot_and_eligibility_are_deterministic() -> None:
    payload = _payload()
    snapshots = {json.dumps(_snap(copy.deepcopy(payload)), sort_keys=True) for _ in range(100)}
    assert len(snapshots) == 1
    snapshot = _snap(payload)
    at = BASE + timedelta(seconds=3)
    evaluations = {
        json.dumps(evaluate_snapshot(snapshot, analysed_at=at), sort_keys=True) for _ in range(100)
    }
    assert len(evaluations) == 1
    assert build_snapshot(payload, snapshot_id="x", assembled_at=at)["limits"] == {
        "tick_vs_bars_seconds": 2.0,
        "clock_vs_bars_seconds": 30.0,
        "max_snapshot_age_seconds": 5.0,
        "clock_min_tick_age_seconds": -60.0,
        "clock_max_tick_age_seconds": 120.0,
    }


def test_limits_are_configurable() -> None:
    strict = SnapshotLimits(max_snapshot_age_seconds=0.1)
    snapshot = _snap(limits=strict)
    assert snapshot["eligibility"]["state"] == REFUSED and "snapshot_too_old" in _reasons(snapshot)


def test_snapshot_endpoint(tmp_path) -> None:
    clock = SteppingClock()
    config = replace(AppConfig.load(environ={}), storage=StorageConfig(tmp_path))
    live = LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=_fake(clock)),
        terminal_running=lambda: True,
        reconnect_interval=0,
        clock=clock,
    )
    app = JohannaApplication(config, live_market=live)
    app.start()

    def get(query):
        response: dict = {}
        body = b"".join(
            BackendAPI(app)(
                {
                    "PATH_INFO": "/api/market/snapshot",
                    "REQUEST_METHOD": "GET",
                    "QUERY_STRING": query,
                    "wsgi.input": BytesIO(),
                },
                lambda status, headers: response.update(status=status),
            )
        )
        return response["status"], json.loads(body)

    try:
        status, snapshot = get("symbol=EURUSD&timeframe=H4&count=30")
        assert status == "200 OK" and snapshot["timeframe"] == "H4"
        assert snapshot["eligibility"]["state"] == LIVE
        assert get("symbol=EURUSD&timeframe=W1")[0] == "400 Bad Request"
    finally:
        app.stop()


def test_bars_response_gains_provenance_fields_additively() -> None:
    clock = SteppingClock()
    service = _service(_fake(clock), clock)
    try:
        first = service.bars("EURUSD", "H1", 10)
        clock.jump(1)
        second = service.bars("EURUSD", "H1", 10)
    finally:
        service.stop()
    assert first["served_from_cache"] is False and second["served_from_cache"] is True
    assert first["retrieved_at_utc"] == second["retrieved_at_utc"]
    assert first["tick"]["bid"] == 1.08512
    assert {"fetched_at_utc", "requested_at_utc", "latest_tick_utc", "bars"} <= set(first)


def test_analysis_package_imports_no_broker_provider_or_network_code() -> None:
    forbidden = (
        "MetaTrader5",
        "johanna.adapters",
        "johanna.collectors",
        "johanna.live_market",
        "urllib",
        "http",
        "socket",
    )
    for path in ANALYSIS_PACKAGE.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                assert not name.startswith(forbidden), f"{path.name} imports {name}"


def test_only_read_only_mt5_calls_are_used() -> None:
    clock = SteppingClock()
    fake = _fake(clock)
    service = _service(fake, clock)
    try:
        service.snapshot("EURUSD", "H1", 20)
    finally:
        service.stop()
    assert set(fake.calls) <= READ_ONLY_CALLS
