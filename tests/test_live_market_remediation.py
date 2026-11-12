"""Regression tests for the live-market audit remediation (H1/M1 are covered elsewhere).

- M2: the intended MT5 terminal must be confirmed running, by full path, before connecting.
- M3: clock verification reflects the current connection, never a past one.
- M4: one watchlist limit (200) across preferences, the quotes API and live retrieval.
- M7: every bar states whether it is complete; completion comes from time and ticks.

All MT5 access uses the in-memory FakeMT5; no terminal is involved.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pytest
from fake_mt5 import FakeMT5

import johanna.live_market as live_market
from johanna.adapters.mt5 import MetaTrader5Adapter, ServerClock
from johanna.api import BackendAPI
from johanna.application import JohannaApplication
from johanna.config import AppConfig, StorageConfig
from johanna.live_market import (
    CACHED,
    COMPLETION_GRACE,
    LIVE,
    QUOTE_BATCH,
    TIMEFRAME_SECONDS,
    LiveMarketService,
    MarketUnavailable,
    bar_is_complete,
)
from johanna.live_market import find_running_terminal as real_find
from johanna.persistence import MAX_WATCHLIST

# ``real_find`` is bound at import, before conftest swaps the module attribute for a
# refusal that keeps every other test away from a real terminal.
REPO = Path(__file__).resolve().parents[1]
EXNESS = r"C:\Program Files\MetaTrader 5 EXNESS\terminal64.exe"
OTHER = r"C:\Program Files\Other Broker MT5\terminal64.exe"


def _service(fake: FakeMT5, *, running=lambda: True, **kwargs) -> LiveMarketService:
    service = LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake),
        terminal_running=running,
        reconnect_interval=0,
        **kwargs,
    )
    service.start()
    return service


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


# ================================================================ M2: terminal launch guard
def _processes(*paths: str):
    return lambda: list(paths)


def test_configured_terminal_is_matched_by_full_path_case_and_slash_insensitive() -> None:
    running = _processes(r"C:\Windows\explorer.exe", EXNESS.upper())
    found = real_find(EXNESS.replace("\\", "/"), processes=running)
    assert found == EXNESS.upper()  # the running process's own path is used to attach


def test_other_running_terminal_does_not_satisfy_the_configured_one() -> None:
    with pytest.raises(MarketUnavailable) as caught:
        real_find(EXNESS, processes=_processes(OTHER))
    assert caught.value.code == "terminal_path_mismatch"
    assert EXNESS in caught.value.message


@pytest.mark.parametrize(
    ("running", "expected"),
    [
        ((OTHER,), OTHER),
        ((EXNESS, OTHER), "multiple_terminals"),
        ((r"C:\Windows\explorer.exe",), "terminal_not_running"),
        ((), "terminal_not_running"),
    ],
)
def test_unconfigured_terminal_requires_exactly_one_running(running, expected) -> None:
    if expected.startswith("C:"):
        assert real_find(None, processes=_processes(*running)) == expected
        return
    with pytest.raises(MarketUnavailable) as caught:
        real_find(None, processes=_processes(*running))
    assert caught.value.code == expected


def test_unlistable_processes_refuse_rather_than_risk_a_launch() -> None:
    def broken() -> list[str]:
        raise OSError("access denied")

    with pytest.raises(MarketUnavailable) as caught:
        real_find(EXNESS, processes=broken)
    assert caught.value.code == "terminal_unverified"


@pytest.mark.skipif(os.name != "nt", reason="Windows process enumeration")
def test_windows_process_listing_sees_real_processes() -> None:
    import sys

    paths = {os.path.normcase(path) for path in live_market._process_image_paths()}
    assert os.path.normcase(sys.executable) in paths or any(
        path.endswith("python.exe") for path in paths
    )


def _patched_real_session(monkeypatch, fake: FakeMT5, running: tuple[str, ...]):
    monkeypatch.setattr(
        live_market,
        "find_running_terminal",
        lambda executable: real_find(executable, processes=_processes(*running)),
    )
    monkeypatch.setattr(
        live_market,
        "MetaTrader5Adapter",
        lambda path, server_clock: MetaTrader5Adapter(
            path, mt5_module=fake, server_clock=server_clock
        ),
    )


def test_worker_session_never_initializes_an_unverified_terminal(monkeypatch) -> None:
    fake = FakeMT5()
    _patched_real_session(monkeypatch, fake, running=(OTHER,))
    session = live_market._real_session(EXNESS, "UTC", 0)
    with pytest.raises(MarketUnavailable) as caught:
        session.quotes(("EURUSD",))
    assert caught.value.code == "terminal_path_mismatch"
    assert "initialize" not in fake.calls


def test_worker_session_attaches_by_the_verified_running_path(monkeypatch) -> None:
    fake = FakeMT5()
    _patched_real_session(monkeypatch, fake, running=(EXNESS,))
    session = live_market._real_session(None, "UTC", 0)  # unconfigured: the single terminal
    reply = session.quotes(("EURUSD",))
    assert reply["value"]["EURUSD"]["bid"] == 1.08512
    assert fake.initialize_paths == [EXNESS]  # never initialize() without a path


def _load_verify_script():
    spec = importlib.util.spec_from_file_location(
        "verify_live_market", REPO / "scripts" / "verify_live_market.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # main() only runs under __main__
    return module


def test_verify_script_cannot_start_mt5() -> None:
    script = _load_verify_script()
    script.find_running_terminal = real_find
    quotes = {"quotes": [{"symbol": "EURUSD", "broker_symbol": "EURUSD", "bid": 1.08512}]}
    fake = FakeMT5()
    result = script.direct_read(quotes, EXNESS, mt5=fake, processes=_processes(OTHER))
    assert result["skipped"].startswith("terminal_path_mismatch")
    result = script.direct_read(quotes, None, mt5=fake, processes=_processes())
    assert result["skipped"].startswith("terminal_not_running")
    assert "initialize" not in fake.calls
    result = script.direct_read(quotes, EXNESS, mt5=fake, processes=_processes(EXNESS))
    assert result["cross"][0]["direct_bid"] == 1.08512
    assert fake.initialize_paths == [EXNESS]


# ================================================================ M3: clock verification
def _check(service: LiveMarketService) -> dict:
    return service.status()["time_check"]


def test_verification_is_dropped_when_the_terminal_disconnects() -> None:
    fake = FakeMT5()
    running = {"value": True}
    service = _service(fake, running=lambda: running["value"], quote_ttl=timedelta(0))
    try:
        service.quotes(["EURUSD"])
        assert _check(service)["result"] == "verified"
        verified_at = _check(service)["last_verified_utc"]
        fake.running = False
        running["value"] = False
        payload = service.quotes(["EURUSD"])
        assert payload["quotes"][0]["status"] == CACHED  # cached ticks shown, not re-verified
        check = payload["connection"]["time_check"]
        assert check["result"] == "unavailable" and check["reason"] == "connection_lost"
        assert check["last_verified_utc"] == verified_at  # history, not a current claim
        check = service.quotes(["EURUSD"])["connection"]["time_check"]
        assert check["result"] == "unavailable" and check["reason"] == "terminal_not_running"
    finally:
        service.stop()


def test_verification_is_dropped_when_the_worker_times_out() -> None:
    fake = FakeMT5()
    service = _service(fake, quote_ttl=timedelta(0), request_timeout=0.3)
    try:
        service.quotes(["EURUSD"])
        assert _check(service)["result"] == "verified"
        fake.delay = 0.6
        check = service.quotes(["EURUSD"])["connection"]["time_check"]
        assert check["result"] == "unavailable" and check["reason"] == "timeout"
    finally:
        fake.delay = 0
        service.stop()


def test_stale_ticks_are_not_verification() -> None:
    fake = FakeMT5()
    service = _service(fake, quote_ttl=timedelta(0))
    try:
        service.quotes(["EURUSD"])
        assert _check(service)["result"] == "verified"
        fake.tick_age_seconds = 600  # market closed or quiet
        check = service.quotes(["EURUSD"])["connection"]["time_check"]
        assert check["result"] == "stale_ticks" and check["freshest_quote_age_seconds"] >= 599
    finally:
        service.stop()


def test_clock_mismatch_replaces_an_earlier_verification() -> None:
    fake = FakeMT5()
    service = _service(fake, quote_ttl=timedelta(0))
    try:
        service.quotes(["EURUSD"])
        assert _check(service)["result"] == "verified"
        fake.server_offset_seconds = 7200  # server clock 2 h ahead of the configured UTC
        payload = service.quotes(["EURUSD"])
        assert payload["connection"]["time_check"]["result"] == "mismatch"
        assert payload["quotes"][0]["status"] == CACHED
        assert payload["quotes"][0]["reason"] == "server_clock_mismatch"
    finally:
        service.stop()


def test_reconnection_with_a_fresh_tick_restores_verification() -> None:
    fake = FakeMT5()
    running = {"value": True}
    service = _service(fake, running=lambda: running["value"], quote_ttl=timedelta(0))
    try:
        service.quotes(["EURUSD"])
        first = _check(service)["last_verified_utc"]
        running["value"] = fake.running = False
        service.quotes(["EURUSD"])
        assert _check(service)["result"] == "unavailable"
        time.sleep(0.01)
        running["value"] = fake.running = True
        payload = service.quotes(["EURUSD"])
        assert payload["quotes"][0]["status"] == LIVE
        check = payload["connection"]["time_check"]
        assert check["result"] == "verified" and check["last_verified_utc"] > first
    finally:
        service.stop()


def test_verification_expires_without_a_fresh_recheck() -> None:
    fake = FakeMT5()
    service = _service(fake, verification_ttl=timedelta(seconds=0.2))
    try:
        service.quotes(["EURUSD"])
        assert _check(service)["result"] == "verified"
        time.sleep(0.3)
        check = _check(service)
        assert check["result"] == "unavailable" and check["reason"] == "not_rechecked"
    finally:
        service.stop()


def test_cached_quotes_within_the_ttl_do_not_reverify() -> None:
    fake = FakeMT5()
    service = _service(fake, quote_ttl=timedelta(seconds=30))
    try:
        service.quotes(["EURUSD"])
        checked_at = _check(service)["checked_at_utc"]
        time.sleep(0.01)
        service.quotes(["EURUSD"])  # served from the in-memory cache
        assert _check(service)["checked_at_utc"] == checked_at
    finally:
        service.stop()


# ================================================================ M4: watchlist limit
def _app(tmp_path, fake: FakeMT5) -> JohannaApplication:
    config = replace(AppConfig.load(environ={}), storage=StorageConfig(tmp_path))
    live = LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake),
        terminal_running=lambda: True,
        reconnect_interval=0,
    )
    return JohannaApplication(config, live_market=live)


def _get(api, path, query=""):
    response: dict = {}
    body = b"".join(
        api(
            {
                "PATH_INFO": path,
                "REQUEST_METHOD": "GET",
                "QUERY_STRING": query,
                "wsgi.input": BytesIO(),
            },
            lambda status, headers: response.update(status=status),
        )
    )
    return response["status"], json.loads(body)


def _post(api, path, payload):
    raw = json.dumps(payload).encode()
    response: dict = {}
    body = b"".join(
        api(
            {
                "PATH_INFO": path,
                "REQUEST_METHOD": "POST",
                "CONTENT_TYPE": "application/json",
                "CONTENT_LENGTH": str(len(raw)),
                "wsgi.input": BytesIO(raw),
            },
            lambda status, headers: response.update(status=status),
        )
    )
    return response["status"], json.loads(body)


def _symbols(count: int) -> list[str]:
    return [f"PAIR{index:03d}" for index in range(count)]


def test_maximum_watchlist_is_quoted_in_batches(tmp_path) -> None:
    assert MAX_WATCHLIST == 200
    spec = {"bid": 1.1, "ask": 1.1001, "digits": 5, "point": 0.00001}
    fake = FakeMT5(symbols={symbol: dict(spec) for symbol in _symbols(MAX_WATCHLIST)})
    app = _app(tmp_path, fake)
    app.start()
    try:
        app.save_preferences({"currency_pair_watchlist": _symbols(MAX_WATCHLIST)})
        runner = app.live_market._quote_runner
        calls = []
        original = runner.run
        runner.run = lambda op, args, timeout: (
            calls.append(len(args[0])) or original(op, args, timeout)
        )
        status, payload = _get(BackendAPI(app), "/api/market/quotes")
        assert status == "200 OK"
        assert len(payload["quotes"]) == MAX_WATCHLIST
        assert {row["status"] for row in payload["quotes"]} == {LIVE}
        assert calls == [QUOTE_BATCH] * (MAX_WATCHLIST // QUOTE_BATCH)
    finally:
        app.stop()


def test_one_beyond_the_limit_is_refused_clearly_and_nothing_is_lost(tmp_path) -> None:
    app = _app(tmp_path, FakeMT5())
    app.start()
    try:
        api = BackendAPI(app)
        status, _ = _post(api, "/api/preferences", {"currency_pair_watchlist": _symbols(200)})
        assert status == "200 OK"
        status, body = _post(api, "/api/preferences", {"currency_pair_watchlist": _symbols(201)})
        assert status == "400 Bad Request" and body["error"] == "invalid_preferences"
        assert "more than 200" in body["message"] and "201" in body["message"]
        assert app.store.get_watchlist() == _symbols(200)  # saved list untouched
        status, body = _get(api, "/api/market/quotes", "symbols=" + ",".join(_symbols(201)))
        assert status == "400 Bad Request" and body["error"] == "invalid_symbols"
        assert "At most 200" in body["message"]
        status, body = _get(api, "/api/market/quotes", "symbols=EURUSD,<x>")
        assert status == "400 Bad Request" and "<X>" in body["message"]
    finally:
        app.stop()


def test_a_failed_batch_only_affects_its_own_pairs() -> None:
    spec = {"bid": 1.1, "ask": 1.1001, "digits": 5, "point": 0.00001}
    fake = FakeMT5(symbols={symbol: dict(spec) for symbol in _symbols(30)})
    service = _service(fake)
    original = service._quote_runner.run
    batches = {"count": 0}

    def flaky(op, args, timeout):
        batches["count"] += 1
        if batches["count"] == 2:
            raise MarketUnavailable("timeout", "synthetic timeout")
        return original(op, args, timeout)

    service._quote_runner.run = flaky
    try:
        rows = service.quotes(_symbols(30))["quotes"]
    finally:
        service.stop()
    assert {row["status"] for row in rows[:QUOTE_BATCH]} == {LIVE}
    assert {(row["status"], row["reason"]) for row in rows[QUOTE_BATCH:]} == {
        ("UNAVAILABLE", "timeout")
    }


# ================================================================ M7: candle completion
MID_BAR = datetime(2026, 11, 11, 10, 31, 30, tzinfo=UTC)  # inside a bar on every timeframe


@pytest.mark.parametrize("timeframe", ["M1", "M5", "M15", "H1", "H4", "D1"])
def test_forming_and_completed_candles_are_marked(timeframe) -> None:
    fake = FakeMT5(now=MID_BAR.timestamp)
    service = _service(fake, clock=lambda: MID_BAR)
    try:
        payload = service.bars("EURUSD", timeframe, 50)
    finally:
        service.stop()
    bars = payload["bars"]
    seconds = TIMEFRAME_SECONDS[timeframe]
    forming = [bar for bar in bars if not bar["complete"]]
    assert len(forming) == 1 and forming[0] is bars[-1]
    opened = _parse(forming[0]["t"])
    assert opened <= MID_BAR < opened + timedelta(seconds=seconds)
    assert all(bar["complete"] for bar in bars[:-1])
    for bar, following in zip(bars, bars[1:], strict=False):
        assert _parse(bar["close_t"]) - _parse(bar["t"]) == timedelta(seconds=seconds)
        assert bar["close_t"] == following["t"]
    assert payload["latest_tick_utc"] == _iso(MID_BAR - timedelta(seconds=0.5))


@pytest.mark.parametrize("timeframe", ["M1", "M5", "M15", "H1", "H4", "D1"])
def test_newest_candle_with_no_new_tick_completes_only_after_its_close_plus_grace(
    timeframe,
) -> None:
    seconds = TIMEFRAME_SECONDS[timeframe]
    fake = FakeMT5(now=MID_BAR.timestamp)  # MT5 still returns MID_BAR's bar as the newest
    opened = datetime.fromtimestamp(MID_BAR.timestamp() // seconds * seconds, UTC)
    close = opened + timedelta(seconds=seconds)
    for now, complete in (
        (close + COMPLETION_GRACE / 2, False),  # quiet market, clock just past the close
        (close + COMPLETION_GRACE + timedelta(seconds=1), True),
    ):
        service = _service(fake, clock=lambda now=now: now)
        try:
            newest = service.bars("EURUSD", timeframe, 10)["bars"][-1]
        finally:
            service.stop()
        assert newest["t"] == _iso(opened) and newest["complete"] is complete, (now, newest)


def test_a_tick_after_the_close_proves_completion_immediately() -> None:
    opened = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
    close = opened + timedelta(hours=1)
    fake = FakeMT5(now=(opened + timedelta(minutes=30)).timestamp)
    fake.tick_age_seconds = -(close - (opened + timedelta(minutes=30))).total_seconds() - 1
    service = _service(fake, clock=lambda: close + timedelta(seconds=5))
    try:
        newest = service.bars("EURUSD", "H1", 10)["bars"][-1]
    finally:
        service.stop()
    assert newest["t"] == _iso(opened) and newest["complete"] is True


def test_completion_rule() -> None:
    close = datetime(2026, 11, 11, 11, tzinfo=UTC)
    assert not bar_is_complete(close, close - timedelta(seconds=1), close)
    assert bar_is_complete(close, close, close)  # a tick at the close proves the move-on
    assert not bar_is_complete(close, None, close + COMPLETION_GRACE - timedelta(seconds=1))
    assert bar_is_complete(close, None, close + COMPLETION_GRACE)


def test_cached_forming_candle_is_never_promoted_to_complete() -> None:
    fake = FakeMT5(now=MID_BAR.timestamp)
    clock = {"now": MID_BAR}
    running = {"value": True}
    service = _service(
        fake, running=lambda: running["value"], clock=lambda: clock["now"], bars_ttl=timedelta(0)
    )
    try:
        assert service.bars("EURUSD", "H1", 10)["bars"][-1]["complete"] is False
        running["value"] = fake.running = False
        clock["now"] = MID_BAR + timedelta(hours=3)  # long after that candle closed
        payload = service.bars("EURUSD", "H1", 10)
    finally:
        service.stop()
    assert payload["status"] == CACHED
    assert payload["bars"][-1]["complete"] is False  # its values are partial: keep it forming


def test_server_clock_bar_close_follows_server_wall_time() -> None:
    utc = ServerClock()
    opened = datetime(2026, 11, 11, tzinfo=UTC)
    assert utc.bar_close_utc(opened, 86400) == opened + timedelta(days=1)
    assert utc.to_utc(utc.from_utc(opened)) == opened
    ny7 = ServerClock("America/New_York", 7)
    # Server Friday 20:00 H4 bar in US summer (UTC+3): 17:00-21:00 UTC.
    summer = datetime(2026, 10, 30, 17, tzinfo=UTC)
    assert ny7.to_utc(ny7.from_utc(summer)) == summer
    assert ny7.bar_close_utc(summer, 14400) == datetime(2026, 10, 30, 21, tzinfo=UTC)
    # Server Monday D1 bar after the US DST change (UTC+2): Sun 22:00 - Mon 22:00 UTC.
    winter = datetime(2026, 11, 1, 22, tzinfo=UTC)
    assert ny7.bar_close_utc(winter, 86400) == datetime(2026, 11, 2, 22, tzinfo=UTC)
