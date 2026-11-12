"""Application composition root and lifecycle."""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from johanna.adapters.mt5 import ServerClock
from johanna.collectors import CollectorConfig, run_configured_source
from johanna.config import AppConfig, ConfigurationError
from johanna.data_services import CalendarService, MarketDataService, iso_utc
from johanna.domain.execution import DisabledExecutionService
from johanna.domain.intelligence import UnimplementedIntelligenceService
from johanna.domain.news import UnimplementedNewsService
from johanna.domain.risk import UnimplementedRiskService
from johanna.domain.rules import UnimplementedRulesService
from johanna.domain.signals import UnimplementedSignalService
from johanna.instance_lock import InstanceLock
from johanna.live_market import LiveMarketService
from johanna.persistence import PREFERENCE_DEFAULTS, ApplicationStore
from johanna.services import ServiceManager

LOGGER = logging.getLogger("johanna.application")
Task = Any  # Callable[[threading.Event], None]; kept loose for test injection


class JohannaApplication:
    """Composes persistence, data services and placeholder domain services.

    Startup order: instance lock, persistence, market data, calendar, then the
    not-yet-implemented domain placeholders. Shutdown runs in reverse.
    """

    def __init__(
        self,
        config: AppConfig,
        services: ServiceManager | None = None,
        *,
        market_monitor_task: Task | None = None,
        calendar_refresh_task: Task | None = None,
        clock: Any = None,
        live_market: LiveMarketService | None = None,
    ) -> None:
        self.config = config
        data_dir = config.storage.data_dir
        self.store = ApplicationStore(data_dir / "johanna.sqlite3")
        self.instance_lock = InstanceLock(data_dir / "johanna.lock")
        collector_config = _collector_config(config)
        output_dir, state_dir = _collector_dirs(config, collector_config)
        self.market_data = MarketDataService(
            bar_roots=(data_dir, output_dir / "mt5"),
            collector_state_path=state_dir / "mt5.json",
            provider=config.integrations.market_data_provider,
            monitor_task=market_monitor_task or _monitor_task(config, collector_config),
            clock=clock,
        )
        calendar_max_age = (
            timedelta(hours=collector_config.calendar.max_age_hours)
            if collector_config is not None
            else timedelta(hours=6)
        )
        self.calendar = CalendarService(
            snapshot_dir=output_dir / "calendar" / "snapshots",
            collector_state_path=state_dir / "calendar.json",
            enabled=config.integrations.calendar_enabled,
            refresh_task=calendar_refresh_task or _calendar_task(config, collector_config),
            max_age=calendar_max_age,
            clock=clock,
        )
        integrations = config.integrations
        self.live_market = live_market or LiveMarketService(
            terminal_path=integrations.mt5_terminal_path,
            server_clock=ServerClock(
                integrations.mt5_server_time_zone, integrations.mt5_server_time_shift_hours
            ),
            cached_bars=self._archived_bars,
        )
        if services is not None:
            configured_services = [self.store, *services.services]
        else:
            configured_services = _services(
                config, self.store, self.market_data, self.calendar, self.live_market
            )
        self.services = ServiceManager(configured_services)
        self._started = False
        self._lifecycle_lock = threading.RLock()
        self.preferences = dict(PREFERENCE_DEFAULTS)
        self.service_configuration: dict[str, dict[str, object]] = {}
        self.cached_data: dict[str, dict[str, object]] = {}
        self.runtime_state: dict[str, object] = {}

    def start(self) -> None:
        with self._lifecycle_lock:
            if self._started:
                return
            self.instance_lock.acquire()
            try:
                self.services.start()
                try:
                    self.preferences = self.store.get_preferences()
                    self.store.save_application_config(_config_snapshot(self.config))
                    self._seed_service_config()
                    self._sync_monitoring(bool(self.preferences["market_monitoring_enabled"]))
                    self.cached_data = self.store.get_all_cached_data()
                    self.runtime_state = self.store.get_runtime_state()
                    self._started = True
                except Exception:
                    self.services.stop()
                    raise
            except Exception:
                self.instance_lock.release()
                raise

    def stop(self) -> None:
        with self._lifecycle_lock:
            if not self._started:
                return
            try:
                self.services.stop()
            finally:
                self._started = False
                self.instance_lock.release()

    def _seed_service_config(self) -> None:
        if not self.store.get_service_config():
            integrations = self.config.integrations
            self.store.set_service_config(
                "market_data", {"provider": integrations.market_data_provider}
            )
            self.store.set_service_config(
                "economic_calendar",
                {
                    "enabled": integrations.calendar_enabled,
                    "forex_factory_authorized": integrations.forex_factory_authorized,
                },
            )
            self.store.set_service_config("news", {"enabled": integrations.news_enabled})
            self.store.set_service_config(
                "gemini_intelligence", {"enabled": integrations.gemini_enabled}
            )

    def _sync_monitoring(self, enabled: bool) -> None:
        """The persisted preference is the single source of truth for monitoring.

        It drives the monitoring worker and is mirrored into the market-data service
        configuration so the two can never disagree.
        """
        self.market_data.set_monitoring(enabled)
        market_config = dict(self.store.get_service_config().get("market_data") or {})
        market_config.setdefault("provider", self.config.integrations.market_data_provider)
        market_config["monitoring_enabled"] = enabled
        self.store.set_service_config("market_data", market_config)
        self.service_configuration = self.store.get_service_config()

    def save_preferences(self, values: dict[str, object]) -> None:
        self.store.save_preferences(values)
        self.preferences = self.store.get_preferences()
        if "market_monitoring_enabled" in values:
            self._sync_monitoring(bool(self.preferences["market_monitoring_enabled"]))

    def cache_provider_data(
        self, cache_key: str, provider: str, kind: str, payload: object
    ) -> None:
        self.store.set_cached_data(cache_key, provider, kind, payload)
        cached = self.store.get_cached_data(cache_key)
        if cached is not None:
            self.cached_data[cache_key] = cached

    def mark_provider_update(self, provider: str, updated_at_utc: str | None = None) -> None:
        self.store.mark_provider_update(provider, updated_at_utc)
        self.runtime_state = self.store.get_runtime_state()

    def save_service_configuration(self, service: str, values: dict[str, object]) -> None:
        self.store.set_service_config(service, values)
        self.service_configuration = self.store.get_service_config()

    def _archived_bars(self, pair: str, timeframe: str) -> tuple[list[dict[str, Any]], str | None]:
        """Archived Parquet bars for the CACHED fallback when MT5 is unavailable."""
        rows, error = self.market_data.read_bars()
        return [r for r in rows if r["symbol"] == pair and r["timeframe"] == timeframe], error

    def health(self) -> tuple[bool, dict[str, object]]:
        """Healthy only when running, persistence is up and no service is in error."""
        return self._health(self.services.status() if self._started else {})

    def _health(self, statuses: dict[str, dict[str, object]]) -> tuple[bool, dict[str, object]]:
        if not self._started:
            return False, {"status": "unavailable", "reason": "application_not_running"}
        if statuses.get("persistence", {}).get("state") != "running":
            return False, {"status": "unavailable", "reason": "persistence_not_running"}
        failed = sorted(name for name, item in statuses.items() if item.get("state") == "error")
        if failed:
            return False, {"status": "degraded", "failed_services": failed}
        return True, {"status": "ok"}

    def status(self) -> dict[str, object]:
        services = self.services.status()
        market = services.get("market_data") or self.market_data.status()
        calendar = services.get("economic_calendar") or self.calendar.status()
        _, health = self._health(services)
        updates = {
            key.removeprefix("provider_update:"): value
            for key, value in self.runtime_state.items()
            if key.startswith("provider_update:")
        }
        if market.get("last_successful_update_utc"):
            updates["market_data"] = market["last_successful_update_utc"]
        if calendar.get("snapshot_retrieved_at_utc"):
            updates["economic_calendar"] = calendar["snapshot_retrieved_at_utc"]
        return {
            "application": "johanna",
            "state": "running" if self._started else "stopped",
            "health": health["status"],
            "trading_mode": self.preferences.get("operating_mode", "manual"),
            "autonomous_execution_preference": self.preferences.get(
                "autonomous_execution_enabled", False
            ),
            "autonomous_execution_active": False,
            "market_monitoring_enabled": self.preferences.get("market_monitoring_enabled", True),
            "market_monitoring_state": market.get("monitoring_state", "not_configured"),
            "market_data_freshness": market.get("freshness"),
            "calendar_freshness": calendar.get("freshness"),
            "last_provider_updates": updates,
            "services": services,
        }

    def dashboard_data(self) -> dict[str, object]:
        preferences = self.preferences
        symbol = str(preferences.get("selected_currency_pair", "EURUSD"))
        timeframe = str(preferences.get("selected_chart_timeframe", "H1"))
        watchlist = preferences.get("currency_pair_watchlist", [symbol])
        if not isinstance(watchlist, list):
            watchlist = [symbol]
        bars, data_error = self.market_data.read_bars()
        chart_rows = [
            row for row in bars if row.get("symbol") == symbol and row.get("timeframe") == timeframe
        ][-240:]
        watch = [_watch_row(pair, bars, timeframe) for pair in watchlist]
        calendar, calendar_error = self.calendar.events()
        news = [
            {"key": key, **entry}
            for key, entry in self.cached_data.items()
            if entry.get("kind") == "news"
        ]
        status = self.status()
        return {
            "status": status,
            "preferences": preferences,
            "service_configuration": self.service_configuration,
            "market": {
                "watchlist": watch,
                "chart": {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "bars": [
                        {**row, "timestamp": _iso_timestamp(row.get("timestamp"))}
                        for row in chart_rows
                    ],
                    "available_timeframes": sorted(
                        {str(row["timeframe"]) for row in bars if row.get("symbol") == symbol}
                    ),
                    "error": data_error,
                },
                "status": status["services"].get("market_data"),
            },
            "calendar": {
                "events": calendar,
                "error": calendar_error,
                "status": status["services"].get("economic_calendar"),
            },
            "news": news,
            "activity": [
                {"provider": provider, "updated_at_utc": updated_at}
                for provider, updated_at in sorted(
                    status["last_provider_updates"].items(),
                    key=lambda item: str(item[1]),
                    reverse=True,
                )
            ],
        }


def _watch_row(pair: str, bars: list[dict[str, Any]], timeframe: str) -> dict[str, object]:
    """Latest price and change for one pair, never mixing timeframes.

    Uses the selected chart timeframe when the pair has it; otherwise the timeframe whose
    newest bar is most recent.
    """
    by_timeframe: dict[str, list[dict[str, Any]]] = {}
    for row in bars:
        if row.get("symbol") == pair:
            by_timeframe.setdefault(str(row["timeframe"]), []).append(row)
    if not by_timeframe:
        return {"symbol": pair, "timeframe": None, "price": None, "change": None, "timestamp": None}
    chosen = (
        timeframe
        if timeframe in by_timeframe
        else max(by_timeframe, key=lambda key: by_timeframe[key][-1]["timestamp"])
    )
    rows = by_timeframe[chosen]
    newest = rows[-1]
    previous = rows[-2] if len(rows) > 1 else None
    return {
        "symbol": pair,
        "timeframe": chosen,
        "price": newest.get("close"),
        "change": newest["close"] - previous["close"] if previous else None,
        "timestamp": _iso_timestamp(newest.get("timestamp")),
    }


def _collector_config(config: AppConfig) -> CollectorConfig | None:
    path = config.integrations.collectors_config
    if path is None:
        return None
    try:
        return CollectorConfig.from_file(path)
    except Exception as exc:
        raise ConfigurationError(f"Invalid collectors configuration {path}: {exc}") from exc


def _collector_dirs(
    config: AppConfig, collector_config: CollectorConfig | None
) -> tuple[Path, Path]:
    """Where collectors write. Defaults match collectors.example.toml under data_dir."""
    if collector_config is not None:
        return collector_config.output_dir, collector_config.state_dir
    data_dir = config.storage.data_dir
    return data_dir / "collectors", data_dir / "collector_state"


def _monitor_task(config: AppConfig, collector_config: CollectorConfig | None) -> Task | None:
    """Existing read-only MT5 collector as the market-monitoring worker, if configured."""
    if collector_config is None or config.integrations.market_data_provider != "mt5":
        return None

    def run(stop_event: threading.Event) -> None:
        run_configured_source(collector_config, "mt5", stop_event=stop_event)

    return run


def _calendar_task(config: AppConfig, collector_config: CollectorConfig | None) -> Task | None:
    """Existing cache-aware calendar collector as the refresh worker, if configured."""
    integrations = config.integrations
    if collector_config is None or not integrations.calendar_enabled:
        return None

    def run(stop_event: threading.Event) -> None:
        if not integrations.forex_factory_authorized:
            raise PermissionError(
                "integrations.forex_factory_authorized is false; calendar refresh refused"
            )
        run_configured_source(collector_config, "calendar", stop_event=stop_event)

    return run


def _services(
    config: AppConfig,
    store: ApplicationStore,
    market_data: MarketDataService,
    calendar: CalendarService,
    live_market: LiveMarketService,
) -> list[Any]:
    integrations = config.integrations
    return [
        store,
        live_market,
        market_data,
        calendar,
        # Typed Phase 1 contracts with refusing stubs (see johanna.domain).
        UnimplementedNewsService(enabled=integrations.news_enabled),
        UnimplementedIntelligenceService(enabled=integrations.gemini_enabled),
        UnimplementedSignalService(),
        UnimplementedRulesService(),
        UnimplementedRiskService(),
        DisabledExecutionService(),
    ]


def _config_snapshot(config: AppConfig) -> dict[str, object]:
    snapshot = asdict(config)
    snapshot["storage"]["data_dir"] = str(config.storage.data_dir)
    collectors = config.integrations.collectors_config
    snapshot["integrations"]["collectors_config"] = str(collectors) if collectors else None
    return snapshot


def _iso_timestamp(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return iso_utc(value)
    return str(value)
