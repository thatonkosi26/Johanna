"""Independent, read-only data collectors for MT5 quotes, bars, and calendar snapshots."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from johanna.adapters.base import MarketDataAdapter
from johanna.adapters.economic_calendar import (
    CachedEconomicCalendar,
    ForexFactoryWeeklyJsonProvider,
    Freshness,
    WeeklySnapshotCache,
)
from johanna.adapters.mt5 import MetaTrader5Adapter, ServerClock
from johanna.fsutil import replace_with_retry
from johanna.instance_lock import FileLock, LockBusyError
from johanna.market_data import ParquetBarStore
from johanna.ticks import TickParquetStore, validate_tick
from johanna.time import strictly_increasing_utc, to_utc

LOGGER = logging.getLogger("johanna.collectors")
SUPPORTED_TIMEFRAMES = ("M1", "M5", "M15", "H1")


class CollectorConfigError(ValueError):
    """Invalid collector configuration with a setting-specific explanation."""


class CollectorBusyError(LockBusyError):
    """Another collector (CLI or backend worker) is already collecting this source."""


class SourceLock(FileLock):
    """One active collector per source, across processes and threads.

    The lock file is ``<state_dir>/<source>.lock``: the same state directory that holds the
    source's progress file, so every collector writing that progress file and its output
    store (the standalone CLI and the backend monitoring worker) contends for the same
    lock, while ``mt5`` and ``calendar`` never block each other.
    """

    busy_error = CollectorBusyError

    def __init__(self, state_dir: str | Path, source: str) -> None:
        super().__init__(Path(state_dir) / f"{source}.lock", purpose=f"collector:{source}")
        self.source = source

    def _busy_message(self) -> str:
        return (
            f"The {self.source} collector is already running (CLI or Johanna backend "
            f"monitoring); lock {self.path}"
        )


@dataclass(frozen=True)
class MT5Settings:
    enabled: bool = True
    collect_ticks: bool = True
    symbols: tuple[str, ...] = ("EURUSD",)
    timeframes: tuple[str, ...] = ("M5",)
    poll_seconds: float = 60.0
    bars_per_poll: int = 10
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0
    terminal_path: str | None = None
    # MT5 epochs are trade-server wall clock, not UTC (see ServerClock).
    server_time_zone: str = "UTC"
    server_time_shift_hours: int = 0

    @property
    def server_clock(self) -> ServerClock:
        return ServerClock(self.server_time_zone, self.server_time_shift_hours)


@dataclass(frozen=True)
class CalendarSettings:
    enabled: bool = False
    authorized_for_local_cache: bool = False
    poll_seconds: float = 900.0
    max_age_hours: float = 6.0
    min_fetch_interval_hours: float = 6.0
    timeout_seconds: float = 10.0
    max_retries: int = 2
    retry_backoff_seconds: float = 1.0


@dataclass(frozen=True)
class CollectorConfig:
    output_dir: Path = Path("data/collectors")
    state_dir: Path = Path("data/collector_state")
    mt5: MT5Settings = MT5Settings()
    calendar: CalendarSettings = CalendarSettings()

    @classmethod
    def from_file(cls, path: str | Path) -> CollectorConfig:
        try:
            raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise CollectorConfigError(f"Could not read collector configuration: {exc}") from exc
        if not isinstance(raw, dict):
            raise CollectorConfigError("Collector configuration must be a TOML table")
        allowed_root = {"collector", "mt5", "calendar"}
        unknown_root = set(raw) - allowed_root
        if unknown_root:
            raise CollectorConfigError(f"Unknown configuration sections: {sorted(unknown_root)}")

        collector = _table(raw, "collector")
        _reject_unknown(collector, {"output_dir", "state_dir"}, "collector")
        output_dir = _path_value(collector.get("output_dir", "data/collectors"), "output_dir")
        state_dir = _path_value(collector.get("state_dir", "data/collector_state"), "state_dir")

        mt5_data = _table(raw, "mt5")
        _reject_unknown(
            mt5_data,
            {
                "enabled",
                "collect_ticks",
                "symbols",
                "timeframes",
                "poll_seconds",
                "bars_per_poll",
                "max_retries",
                "retry_backoff_seconds",
                "terminal_path",
                "server_time_zone",
                "server_time_shift_hours",
            },
            "mt5",
        )
        symbols = _string_list(mt5_data.get("symbols", ["EURUSD"]), "mt5.symbols")
        timeframes = _string_list(mt5_data.get("timeframes", ["M5"]), "mt5.timeframes")
        if not symbols or len(set(symbols)) != len(symbols):
            raise CollectorConfigError("mt5.symbols must be non-empty and contain no duplicates")
        if not timeframes or len(set(timeframes)) != len(timeframes):
            raise CollectorConfigError("mt5.timeframes must be non-empty and contain no duplicates")
        invalid_timeframes = set(timeframes) - set(SUPPORTED_TIMEFRAMES)
        if invalid_timeframes:
            raise CollectorConfigError(
                f"Unsupported MT5 timeframes: {sorted(invalid_timeframes)}; "
                f"supported: {', '.join(SUPPORTED_TIMEFRAMES)}"
            )
        mt5 = MT5Settings(
            enabled=_bool_value(mt5_data.get("enabled", True), "mt5.enabled"),
            collect_ticks=_bool_value(mt5_data.get("collect_ticks", True), "mt5.collect_ticks"),
            symbols=symbols,
            timeframes=timeframes,
            poll_seconds=_number(mt5_data.get("poll_seconds", 60), "mt5.poll_seconds", 5, 86400),
            bars_per_poll=_int_value(
                mt5_data.get("bars_per_poll", 10), "mt5.bars_per_poll", 1, 10000
            ),
            max_retries=_int_value(mt5_data.get("max_retries", 2), "mt5.max_retries", 0, 5),
            retry_backoff_seconds=_number(
                mt5_data.get("retry_backoff_seconds", 1), "mt5.retry_backoff_seconds", 0, 60
            ),
            terminal_path=_optional_string(mt5_data.get("terminal_path"), "mt5.terminal_path"),
            server_time_zone=_optional_string(
                mt5_data.get("server_time_zone", "UTC"), "mt5.server_time_zone"
            )
            or "UTC",
            server_time_shift_hours=_int_value(
                mt5_data.get("server_time_shift_hours", 0), "mt5.server_time_shift_hours", -12, 14
            ),
        )
        try:
            mt5.server_clock  # validates the IANA zone and shift
        except (ValueError, KeyError) as exc:
            raise CollectorConfigError(f"Invalid MT5 server clock: {exc}") from exc

        calendar_data = _table(raw, "calendar")
        _reject_unknown(
            calendar_data,
            {
                "enabled",
                "authorized_for_local_cache",
                "poll_seconds",
                "max_age_hours",
                "min_fetch_interval_hours",
                "timeout_seconds",
                "max_retries",
                "retry_backoff_seconds",
            },
            "calendar",
        )
        calendar = CalendarSettings(
            enabled=_bool_value(calendar_data.get("enabled", False), "calendar.enabled"),
            authorized_for_local_cache=_bool_value(
                calendar_data.get("authorized_for_local_cache", False),
                "calendar.authorized_for_local_cache",
            ),
            poll_seconds=_number(
                calendar_data.get("poll_seconds", 900), "calendar.poll_seconds", 60, 86400
            ),
            max_age_hours=_number(
                calendar_data.get("max_age_hours", 6), "calendar.max_age_hours", 0, 168
            ),
            min_fetch_interval_hours=_number(
                calendar_data.get("min_fetch_interval_hours", 6),
                "calendar.min_fetch_interval_hours",
                1,
                168,
            ),
            timeout_seconds=_number(
                calendar_data.get("timeout_seconds", 10), "calendar.timeout_seconds", 0.1, 120
            ),
            max_retries=_int_value(
                calendar_data.get("max_retries", 2), "calendar.max_retries", 0, 5
            ),
            retry_backoff_seconds=_number(
                calendar_data.get("retry_backoff_seconds", 1),
                "calendar.retry_backoff_seconds",
                0,
                60,
            ),
        )
        return cls(output_dir, state_dir, mt5, calendar)


def _table(raw: Mapping[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise CollectorConfigError(f"[{name}] must be a TOML table")
    return value


def _reject_unknown(values: Mapping[str, Any], allowed: set[str], section: str) -> None:
    unknown = set(values) - allowed
    if unknown:
        raise CollectorConfigError(f"Unknown [{section}] settings: {sorted(unknown)}")


def _path_value(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise CollectorConfigError(f"{name} must be a non-empty path string")
    return Path(value)


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise CollectorConfigError(f"{name} must be a list of non-empty strings")
    return tuple(item.strip() for item in value)


def _bool_value(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise CollectorConfigError(f"{name} must be true or false")
    return value


def _int_value(value: Any, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise CollectorConfigError(f"{name} must be an integer from {minimum} to {maximum}")
    return value


def _number(value: Any, name: str, minimum: float, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, float | int)
        or not minimum <= value <= maximum
    ):
        raise CollectorConfigError(f"{name} must be a number from {minimum} to {maximum}")
    return float(value)


def _optional_string(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise CollectorConfigError(f"{name} must be a non-empty string when set")
    return value.strip()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return to_utc(value).isoformat().replace("+00:00", "Z")


def _atomic_state(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        replace_with_retry(temp, path)
    finally:
        temp.unlink(missing_ok=True)


class MT5Collector:
    """Read-only MT5 polling loop. Each symbol/timeframe target fails independently."""

    def __init__(
        self,
        adapter: MarketDataAdapter,
        store: ParquetBarStore,
        *,
        symbols: Sequence[str],
        timeframes: Sequence[str],
        poll_seconds: float = 60,
        bars_per_poll: int = 10,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1,
        state_path: str | Path,
        tick_store: TickParquetStore | None = None,
        collect_ticks: bool = True,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not symbols or not timeframes:
            raise ValueError("symbols and timeframes must be non-empty")
        if poll_seconds < 5 or bars_per_poll < 1 or max_retries < 0:
            raise ValueError("invalid MT5 collector interval, bar count, or retry count")
        self.adapter = adapter
        self.store = store
        self.symbols = tuple(symbols)
        self.timeframes = tuple(timeframes)
        self.poll_seconds = poll_seconds
        self.bars_per_poll = bars_per_poll
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.state_path = Path(state_path)
        self.tick_store = tick_store or TickParquetStore(store.root.parent / "mt5_ticks")
        self.collect_ticks = collect_ticks
        self.clock = clock
        self.sleep = sleep
        self.initialized = False
        self.state = self._load_state()

    def _load_state(self) -> dict[str, Any]:
        if self.state_path.is_file():
            try:
                saved = json.loads(self.state_path.read_text(encoding="utf-8"))
                if (
                    isinstance(saved, dict)
                    and saved.get("source") == "MT5"
                    and isinstance(saved.get("targets"), dict)
                ):
                    return saved
            except (OSError, json.JSONDecodeError):
                LOGGER.warning("Ignoring unreadable MT5 collector state at %s", self.state_path)
        return {"source": "MT5", "updated_at_utc": None, "targets": {}}

    def _retry(self, action: Callable[[], Any]) -> Any:
        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                return action()
            except Exception as exc:
                last_error = exc
                if attempt < self.max_retries:
                    delay = min(self.retry_backoff_seconds * (2**attempt), 60)
                    LOGGER.warning(
                        "MT5 collection attempt %d failed; retrying in %.1fs", attempt + 1, delay
                    )
                    if delay:
                        self.sleep(delay)
        assert last_error is not None
        raise RuntimeError(
            f"collection failed after {self.max_retries + 1} attempts: {last_error}"
        ) from None

    def _target_status(self, key: str) -> dict[str, Any]:
        return self.state["targets"].setdefault(
            key,
            {
                "status": "pending",
                "last_attempt_utc": None,
                "last_successful_retrieval_utc": None,
                "rows_received": 0,
                "dataset_row_count": 0,
                "last_error": None,
                "pending_bars": None,
                "pending_retrieval_utc": None,
            },
        )

    @staticmethod
    def _serialize_bars(bars: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [{**bar, "timestamp": _iso(bar["timestamp"])} for bar in bars]

    @staticmethod
    def _restore_bars(bars: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                **bar,
                "timestamp": datetime.fromisoformat(str(bar["timestamp"]).replace("Z", "+00:00")),
            }
            for bar in bars
        ]

    @staticmethod
    def _serialize_tick(tick: Mapping[str, Any]) -> dict[str, Any]:
        return {**tick, "timestamp": _iso(tick["timestamp"])}

    @staticmethod
    def _restore_tick(tick: Mapping[str, Any]) -> dict[str, Any]:
        return {
            **tick,
            "timestamp": datetime.fromisoformat(str(tick["timestamp"]).replace("Z", "+00:00")),
        }

    def _tick_status(self, symbol: str) -> dict[str, Any]:
        return self.state.setdefault("tick_targets", {}).setdefault(
            symbol,
            {
                "status": "pending",
                "last_attempt_utc": None,
                "last_successful_retrieval_utc": None,
                "last_tick_utc": None,
                "dataset_row_count": 0,
                "last_error": None,
                "pending_tick": None,
                "pending_retrieval_utc": None,
            },
        )

    def _poll_tick(self, symbol: str) -> None:
        status = self._tick_status(symbol)
        status["last_attempt_utc"] = _iso(self.clock())
        try:
            pending = status.get("pending_tick")
            if pending:
                tick = self._restore_tick(pending)
                retrieved_at = datetime.fromisoformat(
                    status["pending_retrieval_utc"].replace("Z", "+00:00")
                )
            else:

                def retrieve() -> dict[str, Any]:
                    self.adapter.select_symbol(symbol)
                    raw = self.adapter.get_tick(symbol)
                    if not isinstance(raw, Mapping):
                        raise ValueError("MT5 returned a tick in an unsupported format")
                    tick_row = {
                        "symbol": symbol,
                        "timestamp": raw.get("time_utc", raw.get("timestamp")),
                        "time_msc": raw.get("time_msc"),
                        "bid": raw.get("bid"),
                        "ask": raw.get("ask"),
                        "last": raw.get("last"),
                        "volume": raw.get("volume"),
                        "volume_real": raw.get("volume_real"),
                        "flags": raw.get("flags"),
                        "source": "MT5",
                    }
                    return validate_tick(tick_row)

                tick = self._retry(retrieve)
                retrieved_at = self.clock()
                status.update(
                    status="pending_write",
                    pending_tick=self._serialize_tick(tick),
                    pending_retrieval_utc=_iso(retrieved_at),
                )
                self._save_state()

            manifest = self.tick_store.ingest([tick], source="MT5", ingested_at=retrieved_at)
            status.update(
                status="ok",
                last_successful_retrieval_utc=_iso(retrieved_at),
                last_tick_utc=_iso(tick["timestamp"]),
                dataset_row_count=manifest["row_count"],
                last_error=None,
                pending_tick=None,
                pending_retrieval_utc=None,
            )
        except Exception as exc:
            status.update(status="error", last_error=str(exc))
            LOGGER.error("MT5 tick snapshot for %s failed: %s", symbol, exc)
        self._save_state()

    def _recover_pending(self, key: str, status: dict[str, Any]) -> None:
        restored = self._restore_bars(status["pending_bars"])
        source = restored[0]["source"]
        retrieved_at = datetime.fromisoformat(
            status["pending_retrieval_utc"].replace("Z", "+00:00")
        )
        manifest = self.store.ingest(restored, source=source, ingested_at=retrieved_at)
        status.update(
            status="ok",
            last_successful_retrieval_utc=_iso(retrieved_at),
            rows_received=len(restored),
            dataset_row_count=manifest["row_count"],
            last_error=None,
            pending_bars=None,
            pending_retrieval_utc=None,
        )
        LOGGER.info("Recovered pending MT5 write for %s", key)
        self._save_state()

    def _save_state(self) -> None:
        self.state["updated_at_utc"] = _iso(self.clock())
        _atomic_state(self.state_path, self.state)

    def poll_once(self) -> dict[str, Any]:
        """Fetch and persist one bounded bar window for each configured target."""
        if not self.initialized:
            try:
                self._retry(self.adapter.initialize)
                self.initialized = True
            except Exception as exc:
                for symbol in self.symbols:
                    if self.collect_ticks:
                        tick_status = self._tick_status(symbol)
                        tick_status.update(
                            status="error",
                            last_attempt_utc=_iso(self.clock()),
                            last_error=str(exc),
                        )
                    for timeframe in self.timeframes:
                        status = self._target_status(f"{symbol}:{timeframe}")
                        status.update(
                            status="error",
                            last_attempt_utc=_iso(self.clock()),
                            last_error=str(exc),
                        )
                self._save_state()
                LOGGER.error("MT5 initialization failed: %s", exc)
                return self.state

        if self.collect_ticks:
            for symbol in self.symbols:
                self._poll_tick(symbol)

        configured_targets = {
            f"{symbol}:{timeframe}" for symbol in self.symbols for timeframe in self.timeframes
        }
        for key, status in self.state["targets"].items():
            if status.get("pending_bars") and key not in configured_targets:
                try:
                    self._recover_pending(key, status)
                except Exception as exc:
                    status.update(status="error", last_error=str(exc))
                    self._save_state()
                    LOGGER.error("Could not recover pending MT5 write for %s: %s", key, exc)
                    return self.state

        for symbol in self.symbols:
            for timeframe in self.timeframes:
                key = f"{symbol}:{timeframe}"
                status = self._target_status(key)
                status["last_attempt_utc"] = _iso(self.clock())
                try:
                    pending_bars = status.get("pending_bars")
                    if pending_bars:
                        self._recover_pending(key, status)
                        continue

                    def retrieve() -> list[dict[str, Any]]:
                        self.adapter.select_symbol(symbol)
                        bars = self.adapter.get_bars(symbol, timeframe, self.bars_per_poll)
                        strictly_increasing_utc([bar["timestamp"] for bar in bars])
                        return bars

                    bars = self._retry(retrieve)
                    retrieved_at = self.clock()
                    source = bars[0]["source"] if bars else "MT5"
                    if bars:
                        status["pending_bars"] = self._serialize_bars(bars)
                        status["pending_retrieval_utc"] = _iso(retrieved_at)
                        status["status"] = "pending_write"
                        self._save_state()
                    manifest = self.store.ingest(bars, source=source, ingested_at=retrieved_at)
                    status.update(
                        status="ok",
                        last_successful_retrieval_utc=_iso(retrieved_at),
                        rows_received=len(bars),
                        dataset_row_count=manifest["row_count"],
                        last_error=None,
                        pending_bars=None,
                        pending_retrieval_utc=None,
                    )
                    LOGGER.info(
                        "MT5 %s %s: fetched %d bars; dataset has %d rows",
                        symbol,
                        timeframe,
                        len(bars),
                        manifest["row_count"],
                    )
                except Exception as exc:
                    status.update(status="error", last_error=str(exc))
                    LOGGER.error("MT5 %s %s failed: %s", symbol, timeframe, exc)
                self._save_state()
        return self.state

    def run_forever(self, stop_event: threading.Event) -> None:
        """Poll until signalled, always shutting down the adapter cleanly."""
        try:
            while not stop_event.is_set():
                self.poll_once()
                if stop_event.wait(self.poll_seconds):
                    break
        finally:
            if self.initialized:
                self.adapter.shutdown()
                self.initialized = False


class CalendarCollector:
    """Cache-aware weekly calendar poller; repeated loops do not imply API calls."""

    def __init__(
        self,
        calendar: CachedEconomicCalendar,
        *,
        poll_seconds: float = 900,
        max_retries: int = 2,
        retry_backoff_seconds: float = 1,
        state_path: str | Path,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if poll_seconds < 60 or max_retries < 0:
            raise ValueError(
                "calendar collector requires poll_seconds >= 60 and non-negative retries"
            )
        self.calendar = calendar
        self.poll_seconds = poll_seconds
        self.max_retries = max_retries
        self.retry_backoff_seconds = retry_backoff_seconds
        self.state_path = Path(state_path)
        self.clock = clock
        self.sleep = sleep
        self.state: dict[str, Any] = {
            "source": "economic_calendar",
            "updated_at_utc": None,
            "status": "pending",
            "last_attempt_utc": None,
            "last_successful_retrieval_utc": None,
            "events": 0,
            "freshness": None,
            "last_error": None,
        }

    def poll_once(self) -> dict[str, Any]:
        now = to_utc(self.clock())
        self.state["last_attempt_utc"] = _iso(now)
        monday = now.date() - timedelta(days=now.weekday())
        error: Exception | None = None
        succeeded = False
        for attempt in range(self.max_retries + 1):
            try:
                snapshot = self.calendar.get_week(monday)
                self.state.update(
                    status="ok" if snapshot.freshness is Freshness.FRESH else "stale",
                    last_successful_retrieval_utc=_iso(snapshot.retrieved_at_utc),
                    events=len(snapshot.events),
                    freshness=snapshot.freshness.value,
                    last_error=None,
                    week_start=monday.isoformat(),
                )
                LOGGER.info(
                    "Calendar week %s: %d events (%s)",
                    monday,
                    len(snapshot.events),
                    snapshot.freshness.value,
                )
                succeeded = True
                break
            except Exception as exc:
                error = exc
                cause = exc
                while cause.__cause__ is not None:
                    cause = cause.__cause__
                # A provider denial (403) or rate limit (429) is final for this poll: never
                # re-request. Previously the loop skipped only the backoff and retried at once.
                denied = "HTTP 403" in str(cause) or "HTTP 429" in str(cause)
                if denied or attempt >= self.max_retries:
                    break
                delay = min(self.retry_backoff_seconds * (2**attempt), 60)
                LOGGER.warning("Calendar poll failed; retrying in %.1fs", delay)
                if delay:
                    self.sleep(delay)
        if not succeeded:
            self.state.update(status="error", last_error=str(error))
            LOGGER.error("Calendar poll failed: %s", error)
        self.state["updated_at_utc"] = _iso(self.clock())
        _atomic_state(self.state_path, self.state)
        return self.state

    def run_forever(self, stop_event: threading.Event) -> None:
        """Poll until signalled; the facade enforces calendar fetch caching."""
        while not stop_event.is_set():
            self.poll_once()
            if stop_event.wait(self.poll_seconds):
                break


class _DryRunMT5:
    """Synthetic adapter used only for explicit offline dry runs."""

    def __init__(self, clock: Callable[[], datetime]):
        self.clock = clock
        self.closed = False

    def initialize(self) -> None:
        return None

    def connection_status(self) -> Any:
        return None

    def is_connected(self) -> bool:
        return True

    def list_symbols(self) -> list[str]:
        return ["SYNTHETIC_EURUSD"]

    def select_symbol(self, symbol: str) -> None:
        return None

    def get_tick(self, symbol: str) -> dict[str, Any]:
        return {"symbol": symbol, "time_utc": self.clock()}

    def get_bars(self, symbol: str, timeframe: str, count: int) -> list[dict[str, Any]]:
        now = self.clock().astimezone(UTC).replace(second=0, microsecond=0)
        duration = (
            timedelta(minutes=int(timeframe[1:]))
            if timeframe.startswith("M")
            else timedelta(hours=1)
        )
        output = []
        for index in range(count):
            timestamp = now - duration * (count - index)
            output.append(
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "timestamp": timestamp,
                    "open": 1.1,
                    "high": 1.101,
                    "low": 1.099,
                    "close": 1.1005,
                    "tick_volume": 12,
                    "real_volume": None,
                    "source": "synthetic_mt5_mock",
                    "source_timezone": "UTC",
                    "source_timestamp_convention": "bar_open",
                }
            )
        return output

    def shutdown(self) -> None:
        self.closed = True


class _DryRunCalendarProvider:
    source = "synthetic_calendar_mock"
    source_timezone = "UTC"

    def fetch_week(
        self,
        week_start: date,
        *,
        timeout: float,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> Sequence[Mapping[str, Any]]:
        del timeout, etag, last_modified
        event_time = datetime.combine(week_start, datetime.min.time(), UTC) + timedelta(hours=12)
        return [
            {
                "event_name": "SYNTHETIC TEST EVENT",
                "currency": "USD",
                "scheduled_at": event_time.isoformat(),
                "impact": "synthetic",
                "actual": None,
                "forecast": None,
                "previous": None,
            }
        ]


def _calendar_client(config: CollectorConfig, *, dry_run: bool) -> CachedEconomicCalendar:
    calendar_config = config.calendar
    if calendar_config.min_fetch_interval_hours < 1:
        raise CollectorConfigError("Calendar minimum fetch interval must be at least one hour")
    cache = WeeklySnapshotCache(config.output_dir / "calendar" / "snapshots")
    if dry_run:
        provider: Any = _DryRunCalendarProvider()
    else:
        if not calendar_config.authorized_for_local_cache:
            raise CollectorConfigError(
                "Set calendar.authorized_for_local_cache=true only if written permission "
                "covers retrieval and caching"
            )
        provider = ForexFactoryWeeklyJsonProvider(authorized_for_local_cache=True)
    return CachedEconomicCalendar(
        provider,
        cache,
        max_age=timedelta(hours=calendar_config.max_age_hours),
        min_fetch_interval=timedelta(hours=calendar_config.min_fetch_interval_hours),
        timeout=calendar_config.timeout_seconds,
    )


def run_configured_source(
    config: CollectorConfig,
    source: str,
    *,
    once: bool = False,
    dry_run: bool = False,
    stop_event: threading.Event | None = None,
) -> None:
    if source not in {"mt5", "calendar"}:
        raise CollectorConfigError("source must be 'mt5' or 'calendar'")
    # Held for the whole run (one cycle or continuous) and released on any exit path.
    with SourceLock(config.state_dir, source):
        _run_source(config, source, once=once, dry_run=dry_run, stop_event=stop_event)


def _run_source(
    config: CollectorConfig,
    source: str,
    *,
    once: bool,
    dry_run: bool,
    stop_event: threading.Event | None,
) -> None:
    stop = stop_event or threading.Event()
    if source == "mt5":
        settings = config.mt5
        if not settings.enabled:
            raise CollectorConfigError("MT5 collector is disabled in configuration")
        if not dry_run:
            _refuse_legacy_mt5_data(config)
        adapter: Any = (
            _DryRunMT5(_utc_now)
            if dry_run
            else MetaTrader5Adapter(settings.terminal_path, server_clock=settings.server_clock)
        )
        collector = MT5Collector(
            adapter,
            ParquetBarStore(config.output_dir / "mt5"),
            symbols=("SYNTHETIC_EURUSD",) if dry_run else settings.symbols,
            timeframes=settings.timeframes,
            poll_seconds=settings.poll_seconds,
            bars_per_poll=settings.bars_per_poll,
            max_retries=settings.max_retries,
            retry_backoff_seconds=settings.retry_backoff_seconds,
            state_path=config.state_dir / "mt5.json",
            tick_store=TickParquetStore(config.output_dir / "mt5_ticks"),
            collect_ticks=settings.collect_ticks,
        )
        if once:
            try:
                state = collector.poll_once()
            finally:
                if collector.initialized:
                    adapter.shutdown()
            if any(target["status"] == "error" for target in state["targets"].values()) or any(
                target["status"] == "error" for target in state.get("tick_targets", {}).values()
            ):
                raise RuntimeError(
                    "One or more MT5 collection targets failed; see collector state and logs"
                )
            return
        collector.run_forever(stop)
        return
    if source == "calendar":
        if not config.calendar.enabled:
            raise CollectorConfigError("Calendar collector is disabled in configuration")
        collector = CalendarCollector(
            _calendar_client(config, dry_run=dry_run),
            poll_seconds=config.calendar.poll_seconds,
            max_retries=config.calendar.max_retries,
            retry_backoff_seconds=config.calendar.retry_backoff_seconds,
            state_path=config.state_dir / "calendar.json",
        )
        if once:
            state = collector.poll_once()
            if state["status"] == "error":
                raise RuntimeError("Calendar collection failed; see collector state and logs")
        else:
            collector.run_forever(stop)
        return
    raise CollectorConfigError("source must be 'mt5' or 'calendar'")


def _refuse_legacy_mt5_data(config: CollectorConfig) -> None:
    """Never mix UTC-correct bars into a store holding uncorrected server-time bars.

    Bars written before server-clock conversion existed carry ``source_timezone == "UTC"``
    while their timestamps are server wall clock. When a non-UTC server clock is configured,
    collecting into such a store (or replaying such pending bars) would silently mix two
    time bases, so collection is refused until the data is migrated or a new output
    directory is used.
    """
    clock = config.mt5.server_clock
    if clock.is_utc:
        return
    legacy = 0
    store = ParquetBarStore(config.output_dir / "mt5")
    if store.clean_root.exists():
        for row in store.read().to_pylist():
            if row["source"] == "MT5" and row["source_timezone"] == "UTC":
                legacy += 1
    state_path = config.state_dir / "mt5.json"
    if state_path.is_file():
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        for target in (state.get("targets") or {}).values():
            for bar in (target or {}).get("pending_bars") or []:
                if bar.get("source_timezone") == "UTC":
                    legacy += 1
    if legacy:
        raise CollectorConfigError(
            f"{legacy} MT5 bar(s) under {config.output_dir / 'mt5'} or {state_path} were "
            f"stored before server-time conversion (server clock is now {clock.describe()}). "
            "Run `python -m johanna.mt5_timestamp_migration` (see docs/mt5-timestamps.md) "
            "or point collector.output_dir/state_dir at new directories."
        )


def _stop_handler(stop: threading.Event) -> Callable[[int, Any], None]:
    def handle_signal(signum: int, frame: Any) -> None:
        del signum, frame
        LOGGER.info("Shutdown requested; finishing current collection and closing connections")
        stop.set()

    return handle_signal


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one independent Johanna data collector")
    parser.add_argument("--config", type=Path, default=Path("collectors.toml"))
    parser.add_argument("--source", choices=("mt5", "calendar"), required=True)
    parser.add_argument("--once", action="store_true", help="collect one cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="use synthetic offline providers")
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = CollectorConfig.from_file(args.config)
        stop = threading.Event()
        previous_int = signal.signal(signal.SIGINT, _stop_handler(stop))
        previous_term = None
        if hasattr(signal, "SIGTERM"):
            previous_term = signal.signal(signal.SIGTERM, _stop_handler(stop))
        try:
            run_configured_source(
                config, args.source, once=args.once, dry_run=args.dry_run, stop_event=stop
            )
        finally:
            signal.signal(signal.SIGINT, previous_int)
            if previous_term is not None:
                signal.signal(signal.SIGTERM, previous_term)
    except CollectorBusyError as exc:
        LOGGER.error("Collector not started: %s", exc)
        return 3
    except (CollectorConfigError, OSError, ValueError, RuntimeError) as exc:
        LOGGER.error("Collector could not start: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
