"""Validated application configuration loaded from TOML and environment variables."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


class ConfigurationError(ValueError):
    """Invalid or unreadable Johanna configuration."""


@dataclass(frozen=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765


@dataclass(frozen=True)
class StorageConfig:
    data_dir: Path = Path("data")


@dataclass(frozen=True)
class IntegrationConfig:
    market_data_provider: str = "mt5"
    calendar_enabled: bool = False
    forex_factory_authorized: bool = False
    news_enabled: bool = False
    gemini_enabled: bool = False
    execution_enabled: bool = False
    mt5_terminal_path: str | None = None
    # Existing collectors.toml. When set, its output/state directories are used to read
    # collected data and its read-only collectors become the monitoring/refresh workers.
    collectors_config: Path | None = None
    # MT5 epochs are the trade server's wall clock. UTC (the default) matches brokers whose
    # servers run on GMT+0, e.g. Exness. See johanna.adapters.mt5.ServerClock.
    mt5_server_time_zone: str = "UTC"
    mt5_server_time_shift_hours: int = 0


@dataclass(frozen=True)
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    storage: StorageConfig = field(default_factory=StorageConfig)
    integrations: IntegrationConfig = field(default_factory=IntegrationConfig)
    log_level: str = "INFO"

    @classmethod
    def load(
        cls,
        path: str | Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> AppConfig:
        env = os.environ if environ is None else environ
        raw: dict = {}
        if path is not None and Path(path).exists():
            try:
                raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError) as exc:
                raise ConfigurationError(f"Could not read configuration: {exc}") from exc
        elif path is not None:
            raise ConfigurationError(f"Configuration file does not exist: {path}")
        if not isinstance(raw, dict):
            raise ConfigurationError("Configuration must contain TOML tables")
        _unknown(raw, {"server", "storage", "integrations", "application"}, "root")
        server = _table(raw, "server")
        storage = _table(raw, "storage")
        integrations = _table(raw, "integrations")
        application = _table(raw, "application")
        _unknown(server, {"host", "port"}, "server")
        _unknown(storage, {"data_dir"}, "storage")
        _unknown(
            integrations,
            {
                "market_data_provider",
                "calendar_enabled",
                "forex_factory_authorized",
                "news_enabled",
                "gemini_enabled",
                "execution_enabled",
                "mt5_terminal_path",
                "collectors_config",
                "mt5_server_time_zone",
                "mt5_server_time_shift_hours",
            },
            "integrations",
        )
        _unknown(application, {"log_level"}, "application")

        host = env.get("JOHANNA_SERVER_HOST", server.get("host", "127.0.0.1"))
        port_value: object = server.get("port", 8765)
        if "JOHANNA_SERVER_PORT" in env:
            try:
                port_value = int(env["JOHANNA_SERVER_PORT"])
            except ValueError as exc:
                raise ConfigurationError("server.port must be an integer from 1 to 65535") from exc
        port = _integer(port_value, "server.port", 1, 65535)
        data_dir_value = env.get("JOHANNA_DATA_DIR", storage.get("data_dir", "data"))
        if not isinstance(data_dir_value, str) or not data_dir_value.strip():
            raise ConfigurationError("storage.data_dir must be a non-empty path string")
        data_dir = Path(data_dir_value)
        log_level = str(env.get("JOHANNA_LOG_LEVEL", application.get("log_level", "INFO"))).upper()
        provider = str(
            env.get("JOHANNA_MARKET_DATA_PROVIDER", integrations.get("market_data_provider", "mt5"))
        ).lower()
        if not isinstance(host, str) or not host.strip():
            raise ConfigurationError("server.host must be a non-empty string")
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigurationError("application.log_level is invalid")
        if provider not in {"mt5", "twelve_data"}:
            raise ConfigurationError("integrations.market_data_provider must be mt5 or twelve_data")

        def boolean(key: str, default: bool) -> bool:
            env_key = "JOHANNA_" + key.upper()
            value = env.get(env_key, integrations.get(key, default))
            if isinstance(value, str) and env_key in env:
                normalized = value.strip().lower()
                if normalized in {"true", "1", "yes", "on"}:
                    return True
                if normalized in {"false", "0", "no", "off"}:
                    return False
                raise ConfigurationError(f"{env_key} must be a boolean")
            if not isinstance(value, bool):
                raise ConfigurationError(f"integrations.{key} must be true or false")
            return value

        terminal_path = env.get("MT5_TERMINAL_PATH", integrations.get("mt5_terminal_path"))
        if terminal_path is not None and (
            not isinstance(terminal_path, str) or not terminal_path.strip()
        ):
            raise ConfigurationError("integrations.mt5_terminal_path must be a non-empty string")
        collectors_value = env.get(
            "JOHANNA_COLLECTORS_CONFIG", integrations.get("collectors_config")
        )
        collectors_config: Path | None = None
        if collectors_value is not None:
            if not isinstance(collectors_value, str) or not collectors_value.strip():
                raise ConfigurationError("integrations.collectors_config must be a path string")
            collectors_config = Path(collectors_value.strip())
            if not collectors_config.is_file():
                raise ConfigurationError(
                    f"integrations.collectors_config does not exist: {collectors_config}"
                )
        server_zone = env.get(
            "JOHANNA_MT5_SERVER_TIME_ZONE", integrations.get("mt5_server_time_zone", "UTC")
        )
        shift_value: object = integrations.get("mt5_server_time_shift_hours", 0)
        if "JOHANNA_MT5_SERVER_TIME_SHIFT_HOURS" in env:
            try:
                shift_value = int(env["JOHANNA_MT5_SERVER_TIME_SHIFT_HOURS"])
            except ValueError as exc:
                raise ConfigurationError("mt5_server_time_shift_hours must be an integer") from exc
        shift = _integer(shift_value, "integrations.mt5_server_time_shift_hours", -12, 14)
        try:
            from johanna.adapters.mt5 import ServerClock

            ServerClock(str(server_zone), shift)
        except (ValueError, KeyError) as exc:
            raise ConfigurationError(f"Invalid MT5 server clock: {exc}") from exc
        execution_enabled = boolean("execution_enabled", False)
        if execution_enabled:
            raise ConfigurationError(
                "integrations.execution_enabled cannot be true; broker execution is not implemented"
            )
        return cls(
            ServerConfig(host.strip(), port),
            StorageConfig(data_dir),
            IntegrationConfig(
                provider,
                boolean("calendar_enabled", False),
                boolean("forex_factory_authorized", False),
                boolean("news_enabled", False),
                boolean("gemini_enabled", False),
                execution_enabled,
                terminal_path,
                collectors_config,
                str(server_zone),
                shift,
            ),
            log_level,
        )


def _table(raw: dict, name: str) -> dict:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"[{name}] must be a TOML table")
    return value


def _unknown(values: dict, allowed: set[str], section: str) -> None:
    extra = set(values) - allowed
    if extra:
        raise ConfigurationError(f"Unknown {section} configuration keys: {sorted(extra)}")


def _integer(value: object, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ConfigurationError(f"{name} must be an integer from {minimum} to {maximum}")
    return value
