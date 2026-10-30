from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

import pytest

import johanna.market_data as market_data
from johanna.adapters.economic_calendar import CachedEconomicCalendar, WeeklySnapshotCache
from johanna.collectors import (
    CalendarCollector,
    CalendarSettings,
    CollectorConfig,
    CollectorConfigError,
    MT5Collector,
    MT5Settings,
    run_configured_source,
)
from johanna.market_data import ParquetBarStore

NOW = datetime(2025, 1, 2, 12, tzinfo=UTC)


def _bar(symbol: str = "EURUSD", timeframe: str = "M5") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": datetime(2025, 1, 2, 11, 55, tzinfo=UTC),
        "open": 1.1,
        "high": 1.2,
        "low": 1.0,
        "close": 1.15,
        "tick_volume": 10,
        "real_volume": None,
        "source": "MT5",
        "source_timezone": "UTC",
        "source_timestamp_convention": "bar_open",
    }


class MockAdapter:
    def __init__(self, bars: list[dict[str, Any]] | None = None):
        self.bars = bars or [_bar()]
        self.failures: dict[str, int] = {}
        self.initialize_count = 0
        self.shutdown_count = 0
        self.selected: list[str] = []
        self.get_bars_calls = 0
        self.get_tick_calls = 0
        self.tick_error = False
        self.stop_on_retrieval: Event | None = None

    def initialize(self) -> None:
        self.initialize_count += 1

    def connection_status(self) -> Any:
        return None

    def is_connected(self) -> bool:
        return True

    def list_symbols(self) -> list[str]:
        return ["EURUSD"]

    def select_symbol(self, symbol: str) -> None:
        self.selected.append(symbol)

    def get_tick(self, symbol: str) -> dict[str, Any]:
        self.get_tick_calls += 1
        if self.tick_error:
            raise RuntimeError("synthetic tick failure")
        return {
            "time_utc": NOW,
            "time_msc": 1_735_812_000_000,
            "bid": 1.1,
            "ask": 1.1001,
            "last": 0.0,
            "volume": 0,
            "volume_real": 0.0,
            "flags": 6,
        }

    def get_bars(self, symbol: str, timeframe: str, count: int) -> list[dict[str, Any]]:
        self.get_bars_calls += 1
        remaining = self.failures.get(symbol, 0)
        if remaining:
            self.failures[symbol] = remaining - 1
            raise RuntimeError(f"temporary {symbol} adapter failure")
        if self.stop_on_retrieval:
            self.stop_on_retrieval.set()
        return [dict(bar, symbol=symbol, timeframe=timeframe) for bar in self.bars[:count]]

    def shutdown(self) -> None:
        self.shutdown_count += 1


def _collector(
    tmp_path: Path,
    adapter: MockAdapter,
    *,
    symbols: tuple[str, ...] = ("EURUSD",),
    max_retries: int = 1,
    sleep=lambda seconds: None,
) -> MT5Collector:
    return MT5Collector(
        adapter,
        ParquetBarStore(tmp_path / "mt5"),
        symbols=symbols,
        timeframes=("M5",),
        poll_seconds=5,
        bars_per_poll=3,
        max_retries=max_retries,
        retry_backoff_seconds=0.5,
        state_path=tmp_path / "state" / "mt5.json",
        clock=lambda: NOW,
        sleep=sleep,
    )


def test_mt5_collector_retries_tracks_success_and_deduplicates(tmp_path: Path) -> None:
    adapter = MockAdapter()
    adapter.failures["EURUSD"] = 1
    delays: list[float] = []
    collector = _collector(tmp_path, adapter, sleep=delays.append)

    first = collector.poll_once()
    second = collector.poll_once()

    assert first["targets"]["EURUSD:M5"]["status"] == "ok"
    assert first["targets"]["EURUSD:M5"]["last_successful_retrieval_utc"] == "2025-01-02T12:00:00Z"
    assert delays == [0.5]
    assert adapter.initialize_count == 1
    assert second["targets"]["EURUSD:M5"]["rows_received"] == 1
    assert ParquetBarStore(tmp_path / "mt5").read().num_rows == 1
    assert (tmp_path / "mt5_ticks").joinpath("clean/manifest.json").is_file()
    assert collector.tick_store.read().num_rows == 1
    assert adapter.get_tick_calls == 2


def test_tick_snapshot_failure_does_not_block_bar_collection(tmp_path: Path) -> None:
    adapter = MockAdapter()
    adapter.tick_error = True
    collector = _collector(tmp_path, adapter)

    state = collector.poll_once()

    assert state["tick_targets"]["EURUSD"]["status"] == "error"
    assert state["targets"]["EURUSD:M5"]["status"] == "ok"
    assert ParquetBarStore(tmp_path / "mt5").read().num_rows == 1


def test_target_failure_does_not_block_other_symbol(tmp_path: Path) -> None:
    adapter = MockAdapter()
    adapter.failures["BAD"] = 2
    collector = _collector(tmp_path, adapter, symbols=("BAD", "GOOD"), max_retries=1)

    state = collector.poll_once()

    assert state["targets"]["BAD:M5"]["status"] == "error"
    assert state["targets"]["GOOD:M5"]["status"] == "ok"
    assert ParquetBarStore(tmp_path / "mt5").read().num_rows == 1


def test_run_forever_shuts_adapter_down_on_stop(tmp_path: Path) -> None:
    adapter = MockAdapter()
    stop = Event()
    adapter.stop_on_retrieval = stop
    collector = _collector(tmp_path, adapter)

    collector.run_forever(stop)

    assert adapter.shutdown_count == 1
    assert not collector.initialized


def test_interrupted_storage_write_recovers_on_same_next_poll(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ParquetBarStore(tmp_path / "mt5")
    adapter = MockAdapter()
    collector = MT5Collector(
        adapter,
        store,
        symbols=("EURUSD",),
        timeframes=("M5",),
        state_path=tmp_path / "state.json",
        clock=lambda: NOW,
        sleep=lambda seconds: None,
    )
    real_write = market_data._atomic_parquet
    calls = 0

    def fail_once(path: Path, rows: list[dict[str, Any]]) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("synthetic interruption")
        real_write(path, rows)

    monkeypatch.setattr(market_data, "_atomic_parquet", fail_once)
    assert collector.poll_once()["targets"]["EURUSD:M5"]["status"] == "error"
    assert store.manifest()["status"] == "in_progress"
    restarted_adapter = MockAdapter()
    restarted = MT5Collector(
        restarted_adapter,
        store,
        symbols=("EURUSD",),
        timeframes=("M5",),
        state_path=tmp_path / "state.json",
        clock=lambda: NOW,
        sleep=lambda seconds: None,
    )
    assert restarted.poll_once()["targets"]["EURUSD:M5"]["status"] == "ok"
    assert restarted_adapter.get_bars_calls == 0
    assert store.manifest()["status"] == "complete"
    assert store.read().num_rows == 1


class CalendarProvider:
    source = "synthetic-calendar-test"
    source_timezone = "UTC"

    def __init__(self):
        self.calls = 0

    def fetch_week(self, week_start, *, timeout, etag=None, last_modified=None):
        self.calls += 1
        return [
            {
                "event_name": "Synthetic release",
                "currency": "USD",
                "scheduled_at": "2025-01-06T12:00:00Z",
            }
        ]


def test_calendar_collector_uses_weekly_cache_instead_of_refetching(tmp_path: Path) -> None:
    provider = CalendarProvider()
    cached = CachedEconomicCalendar(
        provider,
        WeeklySnapshotCache(tmp_path / "calendar"),
        clock=lambda: NOW,
    )
    collector = CalendarCollector(
        cached,
        poll_seconds=60,
        state_path=tmp_path / "state.json",
        clock=lambda: NOW,
        sleep=lambda seconds: None,
    )

    first = collector.poll_once()
    second = collector.poll_once()

    assert first["events"] == second["events"] == 1
    assert first["freshness"] == "fresh"
    assert provider.calls == 1


def test_dry_run_uses_synthetic_source_without_mt5_or_network(tmp_path: Path) -> None:
    config = CollectorConfig(
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
        mt5=MT5Settings(symbols=("EURUSD",), timeframes=("M5",), bars_per_poll=2),
        calendar=CalendarSettings(enabled=True),
    )
    run_configured_source(config, "mt5", once=True, dry_run=True)
    table = ParquetBarStore(tmp_path / "output" / "mt5").read()
    assert table.num_rows == 2
    assert set(table.column("source").to_pylist()) == {"synthetic_mt5_mock"}
    from johanna.ticks import TickParquetStore

    ticks = TickParquetStore(tmp_path / "output" / "mt5_ticks").read()
    assert ticks.num_rows == 1
    assert ticks.column("source").to_pylist() == ["MT5"]


def test_calendar_dry_run_uses_synthetic_provider(tmp_path: Path) -> None:
    config = CollectorConfig(
        output_dir=tmp_path / "output",
        state_dir=tmp_path / "state",
        calendar=CalendarSettings(enabled=True),
    )
    run_configured_source(config, "calendar", once=True, dry_run=True)
    cache_files = list((tmp_path / "output" / "calendar" / "snapshots").glob("week-*.json"))
    assert len(cache_files) == 1
    assert "synthetic_calendar_mock" in cache_files[0].read_text(encoding="utf-8")


def test_configuration_rejects_bad_timeframes_and_unknown_settings(tmp_path: Path) -> None:
    bad_timeframe = tmp_path / "bad-timeframe.toml"
    bad_timeframe.write_text('[mt5]\ntimeframes = ["M2"]\n', encoding="utf-8")
    with pytest.raises(CollectorConfigError, match="Unsupported MT5 timeframes"):
        CollectorConfig.from_file(bad_timeframe)

    unknown = tmp_path / "unknown.toml"
    unknown.write_text("[mt5]\nmagic_poll = 1\n", encoding="utf-8")
    with pytest.raises(CollectorConfigError, match=r"Unknown \[mt5\]"):
        CollectorConfig.from_file(unknown)
