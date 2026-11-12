from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import replace

import pytest

from johanna.api import BackendAPI
from johanna.application import JohannaApplication
from johanna.config import AppConfig, ConfigurationError, StorageConfig
from johanna.persistence import ApplicationStore
from johanna.services import BackgroundService, ServiceManager


def _config(data_dir) -> AppConfig:
    return replace(AppConfig.load(environ={}), storage=StorageConfig(data_dir))


class FakeService:
    def __init__(self, name: str, *, fail: bool = False) -> None:
        self.name = name
        self.fail = fail
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")
        if self.fail:
            raise RuntimeError("startup failed")

    def stop(self) -> None:
        self.calls.append("stop")

    def status(self) -> dict[str, object]:
        return {"name": self.name, "state": "running"}


def test_configuration_defaults_and_environment_override() -> None:
    config = AppConfig.load(environ={"JOHANNA_SERVER_PORT": "8766"})
    assert config.server.port == 8766
    assert config.server.host == "127.0.0.1"
    assert config.integrations.execution_enabled is False


def test_configuration_validates_toml_and_unknown_keys(tmp_path) -> None:
    path = tmp_path / "johanna.toml"
    path.write_text("[server]\nport = 70000\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="server.port"):
        AppConfig.load(path, environ={})
    path.write_text("[unknown]\nvalue = 1\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="Unknown"):
        AppConfig.load(path, environ={})


def test_application_initializes_domain_services_and_disables_execution(tmp_path) -> None:
    application = JohannaApplication(_config(tmp_path))
    application.start()
    status = application.status()
    assert status["state"] == "running"
    assert status["autonomous_execution_active"] is False
    assert set(status["services"]) == {
        "live_market",
        "market_data",
        "economic_calendar",
        "news",
        "gemini_intelligence",
        "trading_signals",
        "trading_rules",
        "risk_management",
        "broker_execution",
        "persistence",
    }
    # market_data is now a real service over stored bars. With no collectors_config there
    # is no monitoring worker, so it serves stored data ("idle") and says monitoring is
    # not configured instead of pretending to be connected.
    assert status["services"]["market_data"]["state"] == "idle"
    assert status["services"]["market_data"]["monitoring_state"] == "not_configured"
    assert status["services"]["trading_signals"]["state"] == "disabled"
    assert status["services"]["broker_execution"]["state"] == "disabled"
    assert status["services"]["persistence"]["state"] == "running"
    application.stop()


def test_application_startup_is_idempotent_and_shutdown_runs_reverse_order(tmp_path) -> None:
    first, second = FakeService("first"), FakeService("second")
    application = JohannaApplication(_config(tmp_path), ServiceManager([first, second]))
    application.start()
    application.start()
    application.stop()
    application.stop()
    assert first.calls == ["start", "stop"]
    assert second.calls == ["start", "stop"]


def test_startup_failure_rolls_back_started_services() -> None:
    first, failing = FakeService("first"), FakeService("failing", fail=True)
    manager = ServiceManager([first, failing])
    with pytest.raises(RuntimeError, match="startup failed"):
        manager.start()
    assert first.calls == ["start", "stop"]
    assert failing.calls == ["start", "stop"]


def test_health_and_status_endpoints(tmp_path) -> None:
    application = JohannaApplication(_config(tmp_path))
    application.start()
    api = BackendAPI(application)

    def request(path: str, method: str = "GET"):
        response: dict[str, object] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            response["status"] = status
            response["headers"] = headers

        body = b"".join(api({"PATH_INFO": path, "REQUEST_METHOD": method}, start_response))
        return response, json.loads(body)

    health, payload = request("/health")
    assert health["status"] == "200 OK"
    assert payload == {"status": "ok"}
    status, payload = request("/status")
    assert status["status"] == "200 OK"
    assert payload["application"] == "johanna"
    missing, _ = request("/missing")
    assert missing["status"] == "404 Not Found"
    method, _ = request("/status", "POST")
    assert method["status"] == "405 Method Not Allowed"
    application.stop()


def test_application_preferences_and_watchlist_survive_store_restart(tmp_path) -> None:
    path = tmp_path / "state" / "johanna.sqlite3"
    first = ApplicationStore(path)
    first.start()
    assert first.get_preference("operating_mode") == "manual"
    first.save_preferences(
        {
            "selected_currency_pair": "GBPUSD",
            "selected_chart_timeframe": "M15",
            "dashboard_preferences": {"show_calendar": True},
            "theme": "light",
            "user_timezone": "Europe/London",
            "operating_mode": "autonomous",
            "autonomous_execution_enabled": True,
            "market_monitoring_enabled": False,
            "currency_pair_watchlist": ["EURUSD", "GBPUSD"],
        }
    )
    first.stop()

    restarted = ApplicationStore(path)
    restarted.start()
    restored = restarted.get_preferences()
    assert restored["selected_currency_pair"] == "GBPUSD"
    assert restored["selected_chart_timeframe"] == "M15"
    assert restored["dashboard_preferences"] == {"show_calendar": True}
    assert restored["theme"] == "light"
    assert restored["user_timezone"] == "Europe/London"
    assert restored["operating_mode"] == "autonomous"
    assert restored["autonomous_execution_enabled"] is True
    assert restored["market_monitoring_enabled"] is False
    assert restarted.get_watchlist() == ["EURUSD", "GBPUSD"]
    restarted.stop()


def test_watchlist_rejects_empty_or_duplicate_symbols(tmp_path) -> None:
    store = ApplicationStore(tmp_path / "state.sqlite3")
    store.start()
    with pytest.raises(ValueError, match="unique"):
        store.set_watchlist(["EURUSD", " EURUSD "])
    store.stop()


def test_cached_market_and_calendar_data_and_provider_times_survive_restart(tmp_path) -> None:
    path = tmp_path / "state.sqlite3"
    store = ApplicationStore(path)
    store.start()
    store.set_cached_data("EURUSD:M5", "MT5", "market_bars", [{"close": 1.25}])
    store.set_cached_data(
        "calendar:2026-W45", "ForexFactory", "economic_calendar", [{"title": "CPI"}]
    )
    store.mark_provider_update("MT5", "2026-11-11T08:00:00Z")
    store.stop()

    recovered = ApplicationStore(path)
    recovered.start()
    assert recovered.get_cached_data("EURUSD:M5")["payload"] == [{"close": 1.25}]
    assert recovered.get_cached_data("calendar:2026-W45")["payload"] == [{"title": "CPI"}]
    assert recovered.get_runtime_state()["provider_update:MT5"] == "2026-11-11T08:00:00Z"
    recovered.stop()


def test_application_restores_saved_preferences_after_restart(tmp_path) -> None:
    config = _config(tmp_path)
    first = JohannaApplication(config)
    first.start()
    first.save_preferences({"operating_mode": "autonomous", "selected_currency_pair": "USDJPY"})
    first.cache_provider_data("USDJPY:H1", "MT5", "market_bars", [{"close": 150.25}])
    first.mark_provider_update("MT5", "2026-11-11T08:00:00Z")
    first.stop()

    restarted = JohannaApplication(config)
    restarted.start()
    status = restarted.status()
    assert status["trading_mode"] == "autonomous"
    assert status["autonomous_execution_preference"] is False
    assert status["autonomous_execution_active"] is False
    assert restarted.preferences["selected_currency_pair"] == "USDJPY"
    assert restarted.cached_data["USDJPY:H1"]["payload"] == [{"close": 150.25}]
    assert restarted.runtime_state["provider_update:MT5"] == "2026-11-11T08:00:00Z"
    assert restarted.store.get_application_config()["server"]["port"] == 8765
    assert "api_key" not in json.dumps(restarted.store.get_application_config()).lower()
    restarted.stop()


def test_service_configuration_persists_and_secret_fields_are_rejected(tmp_path) -> None:
    store = ApplicationStore(tmp_path / "state.sqlite3")
    store.start()
    store.set_service_config("market_data", {"provider": "twelve_data", "poll_seconds": 30})
    with pytest.raises(ValueError, match="Secret-like"):
        store.set_service_config("market_data", {"api_key": "should-not-persist"})
    store.stop()
    recovered = ApplicationStore(tmp_path / "state.sqlite3")
    recovered.start()
    assert recovered.get_service_config()["market_data"] == {
        "provider": "twelve_data",
        "poll_seconds": 30,
    }
    recovered.stop()


def test_corrupt_database_is_quarantined_and_recreated_safely(tmp_path) -> None:
    path = tmp_path / "johanna.sqlite3"
    path.write_bytes(b"not a sqlite database")
    store = ApplicationStore(path)
    store.start()
    assert store.get_preferences()["operating_mode"] == "manual"
    assert path.exists()
    assert list(tmp_path.glob("johanna.sqlite3.corrupt-*"))
    store.stop()


def test_schema_migration_preserves_previous_operating_mode(tmp_path) -> None:
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE preferences (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute(
            "CREATE TABLE watchlist (symbol TEXT PRIMARY KEY, position INTEGER NOT NULL)"
        )
        connection.execute(
            "INSERT INTO preferences(key, value) VALUES ('trading_mode', '\"autonomous\"')"
        )
    store = ApplicationStore(path)
    store.start()
    assert store.status()["schema_version"] == 1
    assert store.get_preferences()["operating_mode"] == "autonomous"
    # An empty legacy watchlist is seeded with the default major pairs.
    assert store.get_watchlist() == [
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "USDCHF",
        "AUDUSD",
        "USDCAD",
        "NZDUSD",
    ]
    store.stop()


def test_malformed_preference_falls_back_to_default(tmp_path) -> None:
    store = ApplicationStore(tmp_path / "state.sqlite3")
    store.start()
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE preferences SET value = 'not-json' WHERE key = 'selected_chart_timeframe'"
        )
    assert store.get_preference("selected_chart_timeframe") == "H1"
    store.stop()


def test_duplicate_services_are_deduplicated() -> None:
    first, duplicate = FakeService("market_data"), FakeService("market_data")
    manager = ServiceManager([first, duplicate])
    manager.start()
    manager.start()
    manager.stop()
    assert manager.status().keys() == {"market_data"}
    assert first.calls == ["start", "stop"]
    assert duplicate.calls == []


def test_background_service_start_is_idempotent_and_stops_cleanly() -> None:
    started = threading.Event()
    calls = 0

    def task(stop_event: threading.Event) -> None:
        nonlocal calls
        calls += 1
        started.set()
        stop_event.wait()

    worker = BackgroundService("market_data", task)
    worker.start()
    assert started.wait(timeout=1)
    worker.start()
    worker.stop()
    assert calls == 1
