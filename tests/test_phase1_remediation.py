"""Deterministic, offline tests for the Phase 1 remediation (Claude Code, 2026-11-11).

All market bars and calendar events here are synthetic; nothing calls MT5, Forex
Factory, Twelve Data or any network service.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from wsgiref.simple_server import make_server

import pytest

from johanna.__main__ import ExclusiveWSGIServer
from johanna.adapters.economic_calendar import (
    CalendarSnapshot,
    Freshness,
    WeeklySnapshotCache,
    normalize_event,
)
from johanna.api import BackendAPI
from johanna.application import JohannaApplication
from johanna.config import AppConfig, ConfigurationError, StorageConfig
from johanna.data_services import week_start_utc
from johanna.instance_lock import InstanceLockError
from johanna.logging_config import JsonFormatter
from johanna.market_data import ParquetBarStore

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)  # a Wednesday


def _config(data_dir, **integrations) -> AppConfig:
    base = AppConfig.load(environ={})
    return replace(
        base,
        storage=StorageConfig(data_dir),
        integrations=replace(base.integrations, **integrations),
    )


def _bar(ts: datetime, close: float, *, timeframe: str = "M5", source: str = "synthetic"):
    """Synthetic bar only; not an observed market price."""
    return {
        "symbol": "EURUSD",
        "timeframe": timeframe,
        "timestamp": ts,
        "open": close,
        "high": close + 0.001,
        "low": close - 0.001,
        "close": close,
        "tick_volume": 1,
        "real_volume": None,
        "source": source,
        "source_timezone": "UTC",
        "source_timestamp_convention": "bar_open",
    }


def _seed_collector_bars(data_dir, *, ingested_at: datetime) -> None:
    store = ParquetBarStore(data_dir / "collectors" / "mt5")
    bars = [_bar(NOW - timedelta(minutes=5 * (3 - i)), 1.1 + i / 1000) for i in range(3)]
    bars.append(_bar(NOW - timedelta(hours=1), 1.2, timeframe="H1"))
    store.ingest(bars, source="synthetic", ingested_at=ingested_at)


def _write_snapshot(data_dir, *, retrieved_at: datetime) -> None:
    event = normalize_event(
        {
            "event_name": "Synthetic CPI Sample",
            "currency": "USD",
            "scheduled_at": "2026-11-12T13:30:00Z",
            "impact": "high",
        },
        source="synthetic-test-provider",
        retrieved_at=retrieved_at,
    )
    WeeklySnapshotCache(data_dir / "collectors" / "calendar" / "snapshots").write(
        CalendarSnapshot(
            week_start=week_start_utc(NOW),
            retrieved_at_utc=retrieved_at,
            source="synthetic-test-provider",
            events=(event,),
            freshness=Freshness.FRESH,
        )
    )


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


def _call(api, path, method="GET", body=None, headers=None):
    response: dict[str, object] = {}
    environ = {"PATH_INFO": path, "REQUEST_METHOD": method, **(headers or {})}
    if body is not None:
        raw = json.dumps(body).encode()
        environ.update(CONTENT_LENGTH=str(len(raw)), **{"wsgi.input": BytesIO(raw)})
        environ.setdefault("CONTENT_TYPE", "application/json")

    def start_response(status, response_headers):
        response["status"] = status
        response["headers"] = dict(response_headers)

    payload = b"".join(api(environ, start_response))
    return response, json.loads(payload)


# --- market-data continuity -------------------------------------------------------------


def test_existing_collector_bars_reach_status_api_and_dashboard(tmp_path) -> None:
    _seed_collector_bars(tmp_path, ingested_at=NOW - timedelta(minutes=2))
    app = JohannaApplication(_config(tmp_path), clock=Clock(NOW))
    app.start()
    app.save_preferences({"selected_chart_timeframe": "M5"})
    dashboard = app.dashboard_data()
    chart = dashboard["market"]["chart"]
    assert [bar["close"] for bar in chart["bars"]] == pytest.approx([1.1, 1.101, 1.102])
    assert chart["available_timeframes"] == ["H1", "M5"]
    market = dashboard["market"]["status"]
    assert market["freshness"] == "fresh"
    assert market["bar_count"] == 4
    assert market["last_successful_update_utc"] == "2026-11-11T11:58:00Z"
    assert dashboard["status"]["last_provider_updates"]["market_data"] == "2026-11-11T11:58:00Z"
    app.stop()


def test_watchlist_price_and_change_never_mix_timeframes(tmp_path) -> None:
    _seed_collector_bars(tmp_path, ingested_at=NOW)
    app = JohannaApplication(_config(tmp_path), clock=Clock(NOW))
    app.start()
    app.save_preferences({"selected_chart_timeframe": "M5"})
    row = app.dashboard_data()["market"]["watchlist"][0]
    assert row["timeframe"] == "M5"
    assert row["price"] == pytest.approx(1.102)
    assert row["change"] == pytest.approx(0.001)
    app.save_preferences({"selected_chart_timeframe": "H1"})
    row = app.dashboard_data()["market"]["watchlist"][0]
    assert row["timeframe"] == "H1"
    assert row["change"] is None  # single H1 bar; never diffed against an M5 bar
    app.stop()


def test_live_collector_bar_overrides_imported_bar_with_same_key(tmp_path) -> None:
    ts = NOW - timedelta(minutes=5)
    ParquetBarStore(tmp_path).ingest([_bar(ts, 1.0, source="import")], source="import")
    ParquetBarStore(tmp_path / "collectors" / "mt5").ingest(
        [_bar(ts, 2.0, source="MT5")], source="MT5"
    )
    app = JohannaApplication(_config(tmp_path), clock=Clock(NOW))
    rows, error = app.market_data.read_bars()
    assert error is None
    assert len(rows) == 1 and rows[0]["close"] == 2.0


def test_market_data_freshness_states(tmp_path) -> None:
    clock = Clock(NOW)
    app = JohannaApplication(_config(tmp_path), clock=clock)
    assert app.market_data.status()["freshness"] == "no_data"
    _seed_collector_bars(tmp_path, ingested_at=NOW - timedelta(minutes=10))
    assert app.market_data.status()["freshness"] == "fresh"
    clock.value = NOW + timedelta(minutes=6)  # 16 minutes after ingestion
    assert app.market_data.status()["freshness"] == "stale"
    # A newer successful collector retrieval makes it fresh again.
    state_dir = tmp_path / "collector_state"
    state_dir.mkdir()
    (state_dir / "mt5.json").write_text(
        json.dumps(
            {
                "targets": {
                    "EURUSD:M5": {
                        "status": "ok",
                        "last_successful_retrieval_utc": "2026-11-11T12:05:00Z",
                        "last_error": None,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    assert app.market_data.status()["freshness"] == "fresh"


def test_unreadable_market_history_is_reported_not_hidden(tmp_path) -> None:
    _seed_collector_bars(tmp_path, ingested_at=NOW)
    partition = next((tmp_path / "collectors" / "mt5" / "clean").rglob("bars.parquet"))
    partition.write_bytes(b"not parquet")
    app = JohannaApplication(_config(tmp_path), clock=Clock(NOW))
    app.start()
    status = app.market_data.status()
    assert status["state"] == "error" and status["freshness"] == "error"
    assert (
        app.dashboard_data()["market"]["chart"]["error"] == "Stored market data could not be read."
    )
    assert app.health()[1] == {"status": "degraded", "failed_services": ["market_data"]}
    app.stop()


# --- calendar continuity ----------------------------------------------------------------


def test_calendar_snapshot_freshness_matches_adapter_rule(tmp_path) -> None:
    clock = Clock(NOW)
    app = JohannaApplication(_config(tmp_path), clock=clock)
    assert app.calendar.status()["freshness"] == "no_snapshot"
    _write_snapshot(tmp_path, retrieved_at=NOW - timedelta(hours=1))
    events, error = app.calendar.events()
    assert error is None
    assert events[0]["event_name"] == "Synthetic CPI Sample"
    assert events[0]["freshness"] == "fresh"
    status = app.calendar.status()
    assert status["event_count"] == 1
    assert status["snapshot_retrieved_at_utc"] == "2026-11-11T11:00:00Z"
    assert status["state"] == "disabled"  # refresh disabled, cache still served
    clock.value = NOW + timedelta(hours=5, minutes=1)  # age 6h01m > 6h
    assert app.calendar.status()["freshness"] == "stale"
    assert app.calendar.events()[0][0]["freshness"] == "stale"
    clock.value = NOW - timedelta(hours=2)  # snapshot from the future is never fresh
    assert app.calendar.status()["freshness"] == "stale"


def test_corrupt_calendar_snapshot_is_an_error_state(tmp_path) -> None:
    snapshots = tmp_path / "collectors" / "calendar" / "snapshots"
    snapshots.mkdir(parents=True)
    (snapshots / f"week-{week_start_utc(NOW).isoformat()}.json").write_text("{", encoding="utf-8")
    app = JohannaApplication(_config(tmp_path), clock=Clock(NOW))
    assert app.calendar.status()["freshness"] == "error"
    assert app.calendar.events() == ([], "The saved calendar snapshot could not be read.")


def test_collectors_config_paths_are_used_for_reading(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    collectors = tmp_path / "collectors.toml"
    collectors.write_text(
        '[collector]\noutput_dir = "elsewhere/out"\nstate_dir = "elsewhere/state"\n'
        "[mt5]\nenabled = false\n",
        encoding="utf-8",
    )
    ParquetBarStore(tmp_path / "elsewhere" / "out" / "mt5").ingest(
        [_bar(NOW, 1.3)], source="synthetic", ingested_at=NOW
    )
    app = JohannaApplication(
        _config(tmp_path / "data", collectors_config=collectors), clock=Clock(NOW)
    )
    assert app.market_data.status()["bar_count"] == 1


def test_missing_collectors_config_is_rejected(tmp_path) -> None:
    path = tmp_path / "johanna.toml"
    path.write_text('[integrations]\ncollectors_config = "missing.toml"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="collectors_config"):
        AppConfig.load(path, environ={})


# --- monitoring state and worker failures -----------------------------------------------


def _blocking_task(calls: list[str]):
    def task(stop_event: threading.Event) -> None:
        calls.append("start")
        stop_event.wait()
        calls.append("stop")

    return task


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_monitoring_preference_drives_worker_and_service_config(tmp_path) -> None:
    calls: list[str] = []
    config = _config(tmp_path)
    app = JohannaApplication(config, market_monitor_task=_blocking_task(calls))
    app.start()
    assert _wait_for(lambda: app.status()["market_monitoring_state"] == "running")
    assert app.status()["services"]["market_data"]["state"] == "running"

    app.save_preferences({"market_monitoring_enabled": False})
    assert app.status()["market_monitoring_state"] == "paused"
    assert calls == ["start", "stop"]
    assert app.store.get_service_config()["market_data"]["monitoring_enabled"] is False
    app.stop()

    restarted = JohannaApplication(config, market_monitor_task=_blocking_task(calls))
    restarted.start()
    assert restarted.preferences["market_monitoring_enabled"] is False
    assert restarted.status()["market_monitoring_state"] == "paused"
    assert calls == ["start", "stop"]  # paused monitoring is not restarted
    restarted.save_preferences({"market_monitoring_enabled": True})
    assert _wait_for(lambda: restarted.status()["market_monitoring_state"] == "running")
    restarted.stop()
    assert calls == ["start", "stop", "start", "stop"]


def test_stale_service_config_is_reconciled_with_preference_on_start(tmp_path) -> None:
    config = _config(tmp_path)
    app = JohannaApplication(config)
    app.start()
    app.store.save_preferences({"market_monitoring_enabled": False})
    app.store.set_service_config("market_data", {"provider": "mt5", "monitoring_enabled": True})
    app.stop()
    restarted = JohannaApplication(config)
    restarted.start()
    assert restarted.service_configuration["market_data"]["monitoring_enabled"] is False
    restarted.stop()


def test_worker_failure_is_logged_visible_and_fails_health(tmp_path, caplog) -> None:
    def failing(stop_event: threading.Event) -> None:
        raise RuntimeError("synthetic provider crash")

    app = JohannaApplication(_config(tmp_path), market_monitor_task=failing)
    api = BackendAPI(app)
    with caplog.at_level(logging.ERROR, logger="johanna.services"):
        app.start()
        assert _wait_for(lambda: app.status()["market_monitoring_state"] == "error")
    market = app.status()["services"]["market_data"]
    assert market["state"] == "error"
    assert market["monitor"]["error"] == "RuntimeError"
    assert market["monitor"]["failed_at_utc"]
    assert "Background service market_monitor failed" in caplog.text
    assert "synthetic provider crash" in caplog.text  # traceback is in the log
    response, payload = _call(api, "/health")
    assert response["status"] == "503 Service Unavailable"
    assert payload == {"status": "degraded", "failed_services": ["market_data"]}
    app.stop()


def test_worker_that_returns_unexpectedly_is_not_reported_running(tmp_path) -> None:
    app = JohannaApplication(_config(tmp_path), market_monitor_task=lambda stop_event: None)
    app.start()
    assert _wait_for(lambda: app.status()["market_monitoring_state"] == "exited")
    assert app.health()[0] is False
    app.stop()


def test_collector_target_errors_degrade_running_monitor(tmp_path) -> None:
    calls: list[str] = []
    state_dir = tmp_path / "collector_state"
    state_dir.mkdir()
    (state_dir / "mt5.json").write_text(
        json.dumps({"targets": {"EURUSD:M5": {"status": "error", "last_error": "timeout"}}}),
        encoding="utf-8",
    )
    app = JohannaApplication(_config(tmp_path), market_monitor_task=_blocking_task(calls))
    app.start()
    assert _wait_for(lambda: app.status()["services"]["market_data"]["state"] == "degraded")
    errors = app.status()["services"]["market_data"]["target_errors"]
    assert errors[0]["target"] == "targets:EURUSD:M5" and errors[0]["error"] == "timeout"
    assert app.health()[0] is True  # degraded provider target is visible but not an outage
    app.stop()


def test_calendar_refresh_needs_forex_factory_authorization(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    collectors = tmp_path / "collectors.toml"
    collectors.write_text("[mt5]\nenabled = false\n[calendar]\nenabled = true\n", encoding="utf-8")
    app = JohannaApplication(
        _config(tmp_path / "data", calendar_enabled=True, collectors_config=collectors)
    )
    app.start()
    assert _wait_for(lambda: app.calendar.status()["state"] == "error")
    assert app.calendar.status()["refresh"]["error"] == "PermissionError"
    app.stop()


def test_no_monitor_without_collectors_config_or_for_twelve_data(tmp_path) -> None:
    assert not JohannaApplication(_config(tmp_path)).market_data.monitor_configured
    collectors = tmp_path / "collectors.toml"
    collectors.write_text("[mt5]\nenabled = false\n", encoding="utf-8")
    twelve = _config(tmp_path, market_data_provider="twelve_data", collectors_config=collectors)
    assert not JohannaApplication(twelve).market_data.monitor_configured
    mt5 = _config(tmp_path, collectors_config=collectors)
    assert JohannaApplication(mt5).market_data.monitor_configured


# --- lifecycle --------------------------------------------------------------------------


def test_persistence_starts_first_and_stops_last(tmp_path) -> None:
    app = JohannaApplication(_config(tmp_path))
    names = [service.name for service in app.services.services]
    assert names[:4] == ["persistence", "live_market", "market_data", "economic_calendar"]


def test_second_application_on_same_data_dir_is_refused(tmp_path) -> None:
    first = JohannaApplication(_config(tmp_path))
    first.start()
    second = JohannaApplication(_config(tmp_path))
    with pytest.raises(InstanceLockError):
        second.start()
    assert second.status()["state"] == "stopped"
    first.stop()
    second.start()  # lock released on stop
    assert second.status()["state"] == "running"
    second.stop()


def test_health_is_unavailable_until_started(tmp_path) -> None:
    app = JohannaApplication(_config(tmp_path))
    api = BackendAPI(app)
    response, payload = _call(api, "/health")
    assert response["status"] == "503 Service Unavailable"
    assert payload["status"] == "unavailable"
    app.start()
    response, payload = _call(api, "/health")
    assert response["status"] == "200 OK" and payload == {"status": "ok"}
    app.stop()


def test_exclusive_server_refuses_shared_port() -> None:
    with make_server("127.0.0.1", 0, lambda e, s: [], server_class=ExclusiveWSGIServer) as first:
        port = first.server_address[1]
        with pytest.raises(OSError):
            make_server("127.0.0.1", port, lambda e, s: [], server_class=ExclusiveWSGIServer)
    probe = socket.socket()
    probe.bind(("127.0.0.1", port))  # released after close
    probe.close()


# --- persistence correctness ------------------------------------------------------------


def test_rejected_preference_request_changes_nothing(tmp_path) -> None:
    config = _config(tmp_path)
    app = JohannaApplication(config)
    app.start()
    app.save_preferences({"currency_pair_watchlist": ["EURUSD", "GBPUSD"]})
    with pytest.raises(ValueError, match="theme"):
        app.save_preferences({"currency_pair_watchlist": ["ZZZ"], "theme": "neon"})
    assert app.store.get_watchlist() == ["EURUSD", "GBPUSD"]
    assert app.preferences["currency_pair_watchlist"] == ["EURUSD", "GBPUSD"]
    app.stop()
    restarted = JohannaApplication(config)
    restarted.start()
    assert restarted.preferences["currency_pair_watchlist"] == ["EURUSD", "GBPUSD"]
    restarted.stop()


@pytest.mark.parametrize("symbol", ["NOT A PAIR!!", "<script>", "a"])
def test_invalid_instrument_symbols_are_rejected(tmp_path, symbol) -> None:
    app = JohannaApplication(_config(tmp_path))
    app.start()
    with pytest.raises(ValueError):
        app.save_preferences({"currency_pair_watchlist": ["EURUSD", symbol]})
    with pytest.raises(ValueError):
        app.save_preferences({"selected_currency_pair": symbol})
    app.save_preferences({"currency_pair_watchlist": ["EURUSD.a", "XAU/USD", "US30-cash"]})
    app.stop()


# --- API security -----------------------------------------------------------------------


def test_api_method_and_content_type_restrictions(tmp_path) -> None:
    app = JohannaApplication(_config(tmp_path))
    app.start()
    api = BackendAPI(app)
    response, _ = _call(api, "/status", "POST", body={})
    assert response["status"] == "405 Method Not Allowed"
    assert response["headers"]["Allow"] == "GET"
    response, _ = _call(api, "/api/preferences", "GET")
    assert response["status"] == "405 Method Not Allowed"
    response, payload = _call(
        api,
        "/api/preferences",
        "POST",
        body={"theme": "light"},
        headers={"CONTENT_TYPE": "text/plain"},
    )
    assert response["status"] == "415 Unsupported Media Type"
    assert app.preferences["theme"] == "dark"
    response, _ = _call(api, "/api/preferences", "POST", body={"theme": "light"})
    assert response["status"] == "200 OK"
    app.stop()


@pytest.mark.parametrize(
    ("headers", "allowed"),
    [
        ({"HTTP_HOST": "127.0.0.1:8765"}, True),
        ({"HTTP_HOST": "localhost:8765", "HTTP_ORIGIN": "http://localhost:8765"}, True),
        ({"HTTP_HOST": "[::1]:8765"}, True),
        ({"HTTP_HOST": "attacker.example:8765"}, False),  # DNS rebinding
        ({"HTTP_HOST": "127.0.0.1:8765", "HTTP_ORIGIN": "https://attacker.example"}, False),
        ({"HTTP_HOST": "127.0.0.1:8765", "HTTP_ORIGIN": "null"}, False),
    ],
)
def test_api_host_and_origin_checks(tmp_path, headers, allowed) -> None:
    app = JohannaApplication(_config(tmp_path))
    app.start()
    api = BackendAPI(app)
    response, _ = _call(api, "/api/preferences", "POST", body={"theme": "light"}, headers=headers)
    assert (response["status"] == "200 OK") is allowed
    assert (app.preferences["theme"] == "light") is allowed
    app.stop()


def test_static_assets_send_frame_and_content_security_headers(tmp_path) -> None:
    api = BackendAPI(JohannaApplication(_config(tmp_path)))
    response: dict[str, object] = {}
    api(
        {"PATH_INFO": "/", "REQUEST_METHOD": "GET"},
        lambda status, headers: response.update(headers=dict(headers)),
    )
    assert response["headers"]["X-Frame-Options"] == "DENY"
    assert "frame-ancestors 'none'" in response["headers"]["Content-Security-Policy"]


def test_execution_remains_unavailable(tmp_path) -> None:
    app = JohannaApplication(_config(tmp_path))
    app.start()
    app.save_preferences({"operating_mode": "autonomous", "autonomous_execution_enabled": True})
    status = app.status()
    assert status["autonomous_execution_active"] is False
    assert status["services"]["broker_execution"]["state"] == "disabled"
    app.stop()
    with pytest.raises(ConfigurationError, match="execution_enabled"):
        AppConfig.load(environ={"JOHANNA_EXECUTION_ENABLED": "true"})


# --- logging ----------------------------------------------------------------------------


def test_json_logs_keep_extra_fields() -> None:
    record = logging.LogRecord("johanna", logging.INFO, __file__, 1, "started", None, None)
    record.port = 8765
    payload = json.loads(JsonFormatter().format(record))
    assert payload["message"] == "started" and payload["port"] == 8765
