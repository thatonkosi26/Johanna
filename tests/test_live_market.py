"""Live MT5 market workspace: on-demand quotes and bars, caching, honest states, safety.

Uses the real MetaTrader5Adapter over the in-memory FakeMT5 module; no terminal is touched.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.request
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from fake_mt5 import READ_ONLY_CALLS, FakeMT5

from johanna.__main__ import ExclusiveWSGIServer
from johanna.adapters.mt5 import MetaTrader5Adapter, ServerClock
from johanna.api import BackendAPI
from johanna.application import JohannaApplication
from johanna.config import AppConfig, StorageConfig
from johanna.live_market import CACHED, LIVE, STALE, UNAVAILABLE, LiveMarketService
from johanna.market_data import ParquetBarStore
from johanna.persistence import ApplicationStore


def _service(fake: FakeMT5, *, running=lambda: True, clock=None, **kwargs) -> LiveMarketService:
    server_clock = kwargs.pop("server_clock", ServerClock())
    service = LiveMarketService(
        server_clock=server_clock,
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake, server_clock=server_clock),
        terminal_running=running,
        reconnect_interval=0,
        **kwargs,
    )
    service.start()
    return service


@pytest.fixture
def fake():
    return FakeMT5()


def test_live_quotes_have_bid_ask_spread_and_age(fake) -> None:
    service = _service(fake)
    try:
        payload = service.quotes(["EURUSD", "USDJPY"])
    finally:
        service.stop()
    assert payload["status"] == LIVE
    eur, jpy = payload["quotes"]
    assert (eur["bid"], eur["ask"], eur["spread_pips"], eur["digits"]) == (1.08512, 1.08519, 0.7, 5)
    assert (jpy["spread_pips"], jpy["digits"]) == (1.5, 3)
    assert eur["status"] == LIVE and 0 <= eur["quote_age_seconds"] < 5
    assert eur["quote_time_utc"].endswith("Z")
    connection = payload["connection"]
    assert connection["state"] == "connected"
    assert connection["server"] == {
        "company": "Exness Technologies Ltd",
        "server": "Exness-MT5Trial9",
        "account_type": "demo",
        "terminal_connected": True,
    }
    assert "12345678" not in json.dumps(payload) and "Private Person" not in json.dumps(payload)
    assert connection["time_check"]["result"] == "verified"


def test_broker_suffix_symbols_are_resolved_and_missing_pairs_reported() -> None:
    fake = FakeMT5(
        symbols={
            "EURUSDm": {"bid": 1.1, "ask": 1.1001, "digits": 5, "point": 0.00001, "visible": False}
        }
    )
    service = _service(fake)
    try:
        payload = service.quotes(["EURUSD", "NZDUSD"])
    finally:
        service.stop()
    eur, nzd = payload["quotes"]
    assert eur["broker_symbol"] == "EURUSDm" and eur["status"] == LIVE
    assert fake.symbols["EURUSDm"]["visible"] is True  # selected into Market Watch for data
    assert nzd == {"symbol": "NZDUSD", "status": UNAVAILABLE, "reason": "symbol_not_offered"}


def test_quotes_are_cached_briefly_not_polled(fake) -> None:
    service = _service(fake, quote_ttl=timedelta(seconds=30))
    try:
        service.quotes(["EURUSD"])
        service.quotes(["EURUSD"])
        service.quotes(["EURUSD"])
    finally:
        service.stop()
    assert fake.calls.count("symbol_info_tick") == 1


def test_old_tick_is_stale_not_live() -> None:
    fake = FakeMT5(tick_age_seconds=3600)  # market closed: last tick an hour ago
    service = _service(fake)
    try:
        row = service.quotes(["EURUSD"])["quotes"][0]
        check = service.status()["time_check"]
    finally:
        service.stop()
    assert row["status"] == STALE and row["quote_age_seconds"] >= 3599
    assert check["result"] == "stale_ticks"


def test_placeholder_tick_of_a_loading_symbol_is_never_shown_as_a_price(fake) -> None:
    fake.symbols["USDJPY"]["loading"] = True
    service = _service(fake)
    rows = {row["symbol"]: row for row in service.quotes(["EURUSD", "USDJPY"])["quotes"]}
    assert rows["EURUSD"]["status"] == LIVE
    assert rows["USDJPY"] == {"symbol": "USDJPY", "status": UNAVAILABLE, "reason": "symbol_loading"}

    fake.symbols["USDJPY"]["loading"] = False
    time.sleep(1.1)  # past the quote cache
    rows = {row["symbol"]: row for row in service.quotes(["EURUSD", "USDJPY"])["quotes"]}
    assert rows["USDJPY"]["status"] == LIVE
    assert rows["USDJPY"]["bid"] == 151.234


def test_closed_terminal_is_unavailable_and_never_launched(fake) -> None:
    service = _service(fake, running=lambda: False)
    try:
        payload = service.quotes(["EURUSD"])
        bars = service.bars("EURUSD", "H1", 50)
    finally:
        service.stop()
    assert payload["status"] == UNAVAILABLE
    assert payload["quotes"][0]["reason"] == "terminal_not_running"
    assert payload["connection"]["state"] == "unavailable"
    assert bars["status"] == UNAVAILABLE and bars["bars"] == []
    assert "initialize" not in fake.calls  # MetaTrader5.initialize could start the terminal


def test_terminal_closed_mid_session_shows_cached_then_unavailable(fake) -> None:
    running = {"value": True}
    service = _service(fake, running=lambda: running["value"])
    try:
        assert service.quotes(["EURUSD"])["status"] == LIVE
        service.bars("EURUSD", "M5", 20)
        fake.running = False
        running["value"] = False
        time.sleep(1.1)  # past the 1 s quote cache
        row = service.quotes(["EURUSD"])["quotes"][0]
        assert row["status"] == CACHED and row["bid"] == 1.08512
        assert row["reason"] == "connection_lost"
        time.sleep(5.1)  # past the 5 s bar cache
        bars = service.bars("EURUSD", "M5", 20)
        assert bars["status"] == CACHED and bars["source"] == "mt5" and len(bars["bars"]) == 20
        row = service.quotes(["EURUSD"])["quotes"][0]
        assert row["status"] == CACHED and row["reason"] == "terminal_not_running"
        assert service.quotes(["USDJPY"])["quotes"][0]["status"] == UNAVAILABLE
    finally:
        service.stop()


def test_terminal_offline_from_server_is_unavailable(fake) -> None:
    fake.terminal_connected = False
    service = _service(fake)
    try:
        payload = service.quotes(["EURUSD"])
    finally:
        service.stop()
    assert payload["quotes"][0]["reason"] == "not_connected_to_server"


def test_unconfigured_server_clock_is_reported_as_mismatch_not_shown_shifted() -> None:
    fake = FakeMT5(server_offset_seconds=7200)
    service = _service(fake)
    try:
        payload = service.quotes(["EURUSD"])
    finally:
        service.stop()
    assert payload["status"] == UNAVAILABLE
    assert payload["quotes"][0]["reason"] == "server_clock_mismatch"
    assert payload["connection"]["time_check"]["result"] == "mismatch"


def test_slow_mt5_times_out_without_blocking(fake) -> None:
    fake.delay = 0.5
    service = _service(fake, request_timeout=0.2)
    try:
        started = time.monotonic()
        payload = service.quotes(["EURUSD"])
        assert time.monotonic() - started < 0.45
        assert payload["quotes"][0]["reason"] == "timeout"
        time.sleep(0.3)
        busy = service.quotes(["USDJPY"])["quotes"][0]["reason"]
        assert busy in {"mt5_busy", "timeout"}
    finally:
        fake.delay = 0
        service.stop()


def test_bars_are_fetched_on_demand_in_utc(fake) -> None:
    service = _service(fake)
    try:
        payload = service.bars("EURUSD", "H1", 120)
        again = service.bars("EURUSD", "H1", 120)
    finally:
        service.stop()
    assert payload["status"] == LIVE and payload["source"] == "mt5"
    assert payload["time_basis"] == "utc" and payload["digits"] == 5
    times = [datetime.fromisoformat(bar["t"].replace("Z", "+00:00")) for bar in payload["bars"]]
    assert len(times) == 120 and times == sorted(times)
    assert times[-1] <= datetime.now(UTC) < times[-1] + timedelta(hours=1)  # forming bar
    assert all(
        bar["l"] <= min(bar["o"], bar["c"]) <= max(bar["o"], bar["c"]) <= bar["h"]
        for bar in payload["bars"]
    )
    assert fake.calls.count("copy_rates_from_pos") == 1  # second call served from cache
    assert again["bars"] == payload["bars"]


def test_archived_parquet_bars_are_cached_and_flagged(tmp_path) -> None:
    ts = datetime(2026, 10, 30, 20, tzinfo=UTC)
    ParquetBarStore(tmp_path / "collectors" / "mt5").ingest(
        [
            {
                "symbol": "EURUSD",
                "timeframe": "H1",
                "timestamp": ts,
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
    app = JohannaApplication(config)  # default runner; conftest guarantees "not running"
    payload = app.live_market.bars("EURUSD", "H1", 50)
    assert payload["status"] == CACHED and payload["source"] == "parquet_archive"
    assert payload["time_basis"] == "legacy_server_time"
    assert payload["reason"] == "terminal_not_running"
    assert len(payload["bars"]) == 1
    assert payload["bars"][0]["complete"] is True  # the collector archives closed bars only
    app.live_market.stop()


def test_only_read_only_mt5_calls_are_made(fake) -> None:
    service = _service(fake)
    try:
        service.quotes(["EURUSD", "USDJPY"])
        service.bars("EURUSD", "M15", 30)
    finally:
        service.stop()
    assert set(fake.calls) <= READ_ONLY_CALLS
    assert "shutdown" in fake.calls  # connection closed on stop


# --- application and API -----------------------------------------------------------------


def _app(tmp_path, fake=None, running=lambda: True) -> JohannaApplication:
    config = replace(AppConfig.load(environ={}), storage=StorageConfig(tmp_path))
    fake = fake or FakeMT5()
    live = LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake),
        terminal_running=running,
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


def test_api_serves_watchlist_quotes_and_bars_without_collectors(tmp_path) -> None:
    app = _app(tmp_path)
    app.start()
    try:
        assert app.config.integrations.collectors_config is None
        assert app.status()["market_monitoring_state"] == "not_configured"
        api = BackendAPI(app)
        status, payload = _get(api, "/api/market/quotes")
        assert status == "200 OK"
        assert [row["symbol"] for row in payload["quotes"]] == [
            "EURUSD",
            "GBPUSD",
            "USDJPY",
            "USDCHF",
            "AUDUSD",
            "USDCAD",
            "NZDUSD",
        ]
        statuses = {row["symbol"]: row["status"] for row in payload["quotes"]}
        assert statuses["EURUSD"] == LIVE and statuses["GBPUSD"] == UNAVAILABLE  # not in fake
        status, bars = _get(api, "/api/market/bars", "symbol=EURUSD&timeframe=M5&count=60")
        assert status == "200 OK" and bars["status"] == LIVE and len(bars["bars"]) == 60
        services = app.status()["services"]
        assert services["live_market"]["state"] == "connected"
    finally:
        app.stop()


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/api/market/bars", "symbol=EURUSD&timeframe=W1"),
        ("/api/market/bars", "symbol=EURUSD&timeframe=H1&count=5000"),
        ("/api/market/bars", "symbol=<x>&timeframe=H1"),
        ("/api/market/quotes", "symbols=EURUSD,<script>"),
    ],
)
def test_api_rejects_invalid_market_requests(tmp_path, path, query) -> None:
    app = _app(tmp_path)
    status, _ = _get(BackendAPI(app), path, query)
    assert status == "400 Bad Request"


def test_slow_mt5_does_not_block_other_requests(tmp_path) -> None:
    fake = FakeMT5()
    fake.delay = 1.5
    app = _app(tmp_path, fake)
    app.live_market.request_timeout = 3.0
    server = ExclusiveWSGIServer(
        ("127.0.0.1", 0), __import__("wsgiref.simple_server").simple_server.WSGIRequestHandler
    )
    server.set_app(BackendAPI(app))
    app.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        slow = threading.Thread(
            target=lambda: urllib.request.urlopen(base + "/api/market/quotes", timeout=10).read()
        )
        slow.start()
        time.sleep(0.2)
        started = time.monotonic()
        with urllib.request.urlopen(base + "/health", timeout=5) as response:
            assert response.status == 200
        assert time.monotonic() - started < 0.5
        slow.join(timeout=15)
    finally:
        fake.delay = 0
        server.shutdown()
        server.server_close()
        app.stop()


def test_default_watchlist_for_new_install_and_existing_watchlist_preserved(tmp_path) -> None:
    fresh = ApplicationStore(tmp_path / "new.sqlite3")
    fresh.start()
    assert fresh.get_watchlist() == [
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "USDCHF",
        "AUDUSD",
        "USDCAD",
        "NZDUSD",
    ]
    fresh.stop()
    existing = ApplicationStore(tmp_path / "existing.sqlite3")
    existing.start()
    existing.set_watchlist(["GBPJPY", "EURUSD"])
    existing.stop()
    reopened = ApplicationStore(tmp_path / "existing.sqlite3")
    reopened.start()
    assert reopened.get_watchlist() == ["GBPJPY", "EURUSD"]
    reopened.stop()


def test_server_clock_configuration_is_validated(tmp_path) -> None:
    config = AppConfig.load(environ={"JOHANNA_MT5_SERVER_TIME_ZONE": "Europe/Athens"})
    assert config.integrations.mt5_server_time_zone == "Europe/Athens"
    with pytest.raises(Exception, match="server clock"):
        AppConfig.load(environ={"JOHANNA_MT5_SERVER_TIME_ZONE": "Mars/Olympus"})
    assert AppConfig.load(environ={}).integrations.mt5_server_time_zone == "UTC"
