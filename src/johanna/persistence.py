"""Versioned local persistence for durable settings and recoverable provider state."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LOGGER = logging.getLogger("johanna.persistence")
SCHEMA_VERSION = 1
PREFERENCE_DEFAULTS: dict[str, object] = {
    "selected_currency_pair": "EURUSD",
    "selected_chart_timeframe": "H1",
    "dashboard_preferences": {},
    "theme": "dark",
    "user_timezone": "Africa/Johannesburg",
    "operating_mode": "manual",
    "autonomous_execution_enabled": False,
    "market_monitoring_enabled": True,
    "currency_pair_watchlist": [
        "EURUSD",
        "GBPUSD",
        "USDJPY",
        "USDCHF",
        "AUDUSD",
        "USDCAD",
        "NZDUSD",
    ],
}
PREFERENCE_KEYS = frozenset(PREFERENCE_DEFAULTS)
# Broker symbols such as EURUSD, EURUSD.a, XAU/USD or US30-cash; no spaces or markup.
SYMBOL_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/#-]{1,31}")
# One limit for the whole pipeline: saved watchlist, quotes API and live quote retrieval.
MAX_WATCHLIST = 200


class PersistenceError(RuntimeError):
    """State could not be safely read or written."""


class ApplicationStore:
    """SQLite system of record; existing Parquet stores remain for bar history."""

    name = "persistence"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._started = False

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(self.path)
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                self._migrate(connection)
                result = connection.execute("PRAGMA integrity_check").fetchone()
                if result is None or result[0] != "ok":
                    raise sqlite3.DatabaseError("integrity check failed")
                connection.commit()
            finally:
                connection.close()
        except sqlite3.DatabaseError as exc:
            if not self._looks_corrupt(exc):
                raise PersistenceError("Application database is unavailable") from exc
            self._quarantine_corrupt_file()
            try:
                connection = sqlite3.connect(self.path)
                try:
                    connection.execute("PRAGMA journal_mode = WAL")
                    self._migrate(connection)
                    connection.commit()
                finally:
                    connection.close()
            except sqlite3.DatabaseError as retry_exc:
                raise PersistenceError("Could not recreate the application database") from retry_exc
            LOGGER.error("Corrupt application database quarantined; defaults restored")
        self._started = True

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version > SCHEMA_VERSION:
            raise PersistenceError(
                f"Database schema version {version} is newer than supported {SCHEMA_VERSION}"
            )
        if version == 0:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS preferences (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS watchlist (
                    symbol TEXT PRIMARY KEY, position INTEGER NOT NULL UNIQUE
                );
                CREATE TABLE IF NOT EXISTS application_config (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS service_config (
                    service TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_state (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS provider_cache (
                    cache_key TEXT PRIMARY KEY, provider TEXT NOT NULL, kind TEXT NOT NULL,
                    payload TEXT NOT NULL, updated_at_utc TEXT NOT NULL
                );
                PRAGMA user_version = 1;
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO preferences(key, value) VALUES (?, ?)",
                ("operating_mode", '"manual"'),
            )
            for key, value in PREFERENCE_DEFAULTS.items():
                if key == "currency_pair_watchlist":
                    # Seeded only when the table is empty (first run or legacy upgrade).
                    if not connection.execute("SELECT 1 FROM watchlist LIMIT 1").fetchone():
                        connection.executemany(
                            "INSERT INTO watchlist(symbol, position) VALUES (?, ?)",
                            [(symbol, index) for index, symbol in enumerate(value)],
                        )
                else:
                    connection.execute(
                        "INSERT OR IGNORE INTO preferences(key, value) VALUES (?, ?)",
                        (key, json.dumps(value, separators=(",", ":"))),
                    )
            old_mode = connection.execute(
                "SELECT value FROM preferences WHERE key = 'trading_mode'"
            ).fetchone()
            if old_mode:
                connection.execute(
                    "UPDATE preferences SET value = ? WHERE key = 'operating_mode'",
                    (old_mode[0],),
                )
        elif version != SCHEMA_VERSION:
            raise PersistenceError(f"No migration path from database schema version {version}")

    @staticmethod
    def _looks_corrupt(error: sqlite3.DatabaseError) -> bool:
        message = str(error).lower()
        return any(
            marker in message
            for marker in ("malformed", "not a database", "encrypted", "integrity check failed")
        )

    def _quarantine_corrupt_file(self) -> None:
        if not self.path.exists():
            return
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        backup = self.path.with_name(f"{self.path.name}.corrupt-{stamp}")
        try:
            self.path.replace(backup)
        except OSError as exc:
            raise PersistenceError("Corrupt application database could not be quarantined") from exc
        for suffix in ("-wal", "-shm"):
            sidecar = Path(f"{self.path}{suffix}")
            if sidecar.exists():
                sidecar.unlink(missing_ok=True)

    def stop(self) -> None:
        try:
            if self.path.exists():
                connection = sqlite3.connect(self.path)
                try:
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                finally:
                    connection.close()
        finally:
            self._started = False

    def status(self) -> dict[str, object]:
        return {
            "name": self.name,
            "state": "running" if self._started else "stopped",
            "schema_version": SCHEMA_VERSION,
        }

    def get_preference(self, key: str, default: object = None) -> object:
        if key not in PREFERENCE_KEYS:
            raise ValueError(f"Unsupported preference: {key}")
        value = self._read_json("preferences", "key", key)
        if value is None:
            return PREFERENCE_DEFAULTS.get(key, default)
        return value

    def get_preferences(self) -> dict[str, object]:
        result = dict(PREFERENCE_DEFAULTS)
        with self._transaction() as connection:
            rows = connection.execute("SELECT key, value FROM preferences").fetchall()
        for key, raw in rows:
            if key not in PREFERENCE_KEYS:
                continue
            try:
                result[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                LOGGER.warning("Ignoring malformed persisted preference: %s", key)
        result["currency_pair_watchlist"] = self.get_watchlist()
        return result

    def save_preferences(self, values: dict[str, object]) -> None:
        """Validate every value first, then write all of them in one transaction.

        A request with any invalid value changes nothing.
        """
        unknown = set(values) - PREFERENCE_KEYS
        if unknown:
            raise ValueError(f"Unsupported preferences: {sorted(unknown)}")
        watchlist: list[str] | None = None
        if "currency_pair_watchlist" in values:
            watchlist = _validated_watchlist(values["currency_pair_watchlist"])
        to_write = {key: value for key, value in values.items() if key != "currency_pair_watchlist"}
        if "selected_currency_pair" in to_write:
            symbol = to_write["selected_currency_pair"]
            if not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol.strip()):
                raise ValueError("selected_currency_pair must be a valid instrument symbol")
            to_write["selected_currency_pair"] = symbol.strip()
        if "selected_chart_timeframe" in to_write:
            timeframe = to_write["selected_chart_timeframe"]
            if not isinstance(timeframe, str) or not re.fullmatch(r"[A-Za-z0-9]+", timeframe):
                raise ValueError("selected_chart_timeframe must be a valid timeframe label")
        if "theme" in to_write and to_write["theme"] not in {"light", "dark", "system"}:
            raise ValueError("theme must be light, dark, or system")
        if "user_timezone" in to_write:
            timezone = to_write["user_timezone"]
            if not isinstance(timezone, str):
                raise ValueError("user_timezone must be an IANA timezone name")
            try:
                ZoneInfo(timezone)
            except (ZoneInfoNotFoundError, ValueError) as exc:
                raise ValueError("user_timezone must be an IANA timezone name") from exc
        if "dashboard_preferences" in to_write and not isinstance(
            to_write["dashboard_preferences"], dict
        ):
            raise ValueError("dashboard_preferences must be an object")
        if "operating_mode" in to_write and to_write["operating_mode"] not in {
            "manual",
            "autonomous",
        }:
            raise ValueError("operating_mode must be manual or autonomous")
        for key in ("autonomous_execution_enabled", "market_monitoring_enabled"):
            if key in to_write and not isinstance(to_write[key], bool):
                raise ValueError(f"{key} must be boolean")
        with self._transaction() as connection:
            if watchlist is not None:
                _replace_watchlist(connection, watchlist)
            connection.executemany(
                "INSERT INTO preferences(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                [
                    (key, json.dumps(value, separators=(",", ":")))
                    for key, value in to_write.items()
                ],
            )

    def get_watchlist(self) -> list[str]:
        with self._transaction() as connection:
            rows = connection.execute("SELECT symbol FROM watchlist ORDER BY position").fetchall()
        return [row[0] for row in rows]

    def set_watchlist(self, symbols: object) -> None:
        normalized = _validated_watchlist(symbols)
        with self._transaction() as connection:
            _replace_watchlist(connection, normalized)

    def save_application_config(self, config: dict[str, object]) -> None:
        self._reject_secrets(config)
        self._replace_json_table("application_config", "key", config)

    def get_application_config(self) -> dict[str, object]:
        return self._read_json_table("application_config", "key")

    def set_service_config(self, service: str, config: dict[str, object]) -> None:
        self._reject_secrets(config)
        self._write_json("service_config", "service", service, config)

    def get_service_config(self) -> dict[str, object]:
        return self._read_json_table("service_config", "service")

    def set_runtime_state(self, key: str, value: object) -> None:
        encoded = json.dumps(value, separators=(",", ":"))
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO runtime_state(key, value, updated_at_utc) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
                "updated_at_utc=excluded.updated_at_utc",
                (key, encoded, _now()),
            )

    def get_runtime_state(self) -> dict[str, object]:
        return self._read_json_table("runtime_state", "key")

    def mark_provider_update(self, provider: str, updated_at_utc: str | None = None) -> None:
        self.set_runtime_state(f"provider_update:{provider}", updated_at_utc or _now())

    def set_cached_data(self, cache_key: str, provider: str, kind: str, payload: object) -> None:
        encoded = json.dumps(payload, separators=(",", ":"))
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO provider_cache(cache_key, provider, kind, payload, updated_at_utc) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
                "provider=excluded.provider, kind=excluded.kind, payload=excluded.payload, "
                "updated_at_utc=excluded.updated_at_utc",
                (cache_key, provider, kind, encoded, _now()),
            )

    def get_cached_data(self, cache_key: str) -> dict[str, object] | None:
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT provider, kind, payload, updated_at_utc FROM provider_cache "
                "WHERE cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        try:
            payload = json.loads(row[2])
        except (json.JSONDecodeError, TypeError):
            LOGGER.warning("Ignoring malformed provider cache entry: %s", cache_key)
            return None
        return {"provider": row[0], "kind": row[1], "payload": payload, "updated_at_utc": row[3]}

    def get_all_cached_data(self) -> dict[str, dict[str, object]]:
        with self._transaction() as connection:
            keys = [row[0] for row in connection.execute("SELECT cache_key FROM provider_cache")]
        return {key: record for key in keys if (record := self.get_cached_data(key)) is not None}

    def _connect(self) -> sqlite3.Connection:
        if not self.path.exists():
            raise PersistenceError("Application database has not been initialized")
        try:
            return sqlite3.connect(self.path, timeout=10)
        except sqlite3.DatabaseError as exc:
            raise PersistenceError("Application database is unavailable") from exc

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        """Commit on success, roll back on error, and always close the connection.

        ``with sqlite3.connect(...)`` alone commits but never closes the connection.
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _read_json(self, table: str, key_column: str, key: str) -> Any:
        with self._transaction() as connection:
            row = connection.execute(
                f"SELECT value FROM {table} WHERE {key_column} = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            LOGGER.warning("Ignoring malformed persisted value: %s", key)
            return None

    def _write_json(self, table: str, key_column: str, key: str, value: object) -> None:
        if table not in {"preferences", "service_config"}:
            raise ValueError("unsupported JSON table")
        encoded = json.dumps(value, separators=(",", ":"))
        with self._transaction() as connection:
            connection.execute(
                f"INSERT INTO {table}({key_column}, value) VALUES (?, ?) "
                f"ON CONFLICT({key_column}) DO UPDATE SET value=excluded.value",
                (key, encoded),
            )

    def _replace_json_table(self, table: str, key_column: str, values: dict[str, object]) -> None:
        if table != "application_config":
            raise ValueError("unsupported configuration table")
        with self._transaction() as connection:
            connection.execute(f"DELETE FROM {table}")
            connection.executemany(
                f"INSERT INTO {table}({key_column}, value) VALUES (?, ?)",
                [(key, json.dumps(value, separators=(",", ":"))) for key, value in values.items()],
            )

    def _read_json_table(self, table: str, key_column: str) -> dict[str, object]:
        if table not in {"application_config", "service_config", "runtime_state"}:
            raise ValueError("unsupported JSON table")
        value_column = "value"
        with self._transaction() as connection:
            rows = connection.execute(
                f"SELECT {key_column}, {value_column} FROM {table}"
            ).fetchall()
        result: dict[str, object] = {}
        for key, raw in rows:
            try:
                result[key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                LOGGER.warning("Ignoring malformed persisted value: %s", key)
        return result

    @classmethod
    def _reject_secrets(cls, value: object) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
                if any(token in normalized for token in ("secret", "password", "apikey", "token")):
                    raise ValueError(
                        f"Secret-like field is not allowed in persistent config: {key}"
                    )
                cls._reject_secrets(nested)
        elif isinstance(value, list):
            for nested in value:
                cls._reject_secrets(nested)


def _validated_watchlist(symbols: object) -> list[str]:
    if not isinstance(symbols, list) or any(
        not isinstance(symbol, str) or not symbol.strip() for symbol in symbols
    ):
        raise ValueError("watchlist must be a list of non-empty currency-pair symbols")
    normalized = [symbol.strip() for symbol in symbols]
    invalid = [symbol for symbol in normalized if not SYMBOL_PATTERN.fullmatch(symbol)]
    if invalid:
        raise ValueError(f"watchlist contains invalid instrument symbols: {invalid[:5]}")
    if len(set(normalized)) != len(normalized):
        raise ValueError("watchlist symbols must be unique")
    if len(normalized) > MAX_WATCHLIST:
        raise ValueError(
            f"watchlist cannot contain more than {MAX_WATCHLIST} symbols "
            f"(received {len(normalized)})"
        )
    return normalized


def _replace_watchlist(connection: sqlite3.Connection, symbols: list[str]) -> None:
    connection.execute("DELETE FROM watchlist")
    connection.executemany(
        "INSERT INTO watchlist(symbol, position) VALUES (?, ?)",
        [(symbol, index) for index, symbol in enumerate(symbols)],
    )


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
