from datetime import UTC, datetime

import pytest

from johanna.ticks import TICK_SCHEMA, TickParquetStore, validate_tick

TICK_TIME = datetime(2025, 1, 2, 12, 0, tzinfo=UTC)


def _tick(**overrides):
    return {
        "symbol": "EURUSD",
        "timestamp": TICK_TIME,
        "time_msc": 1_735_812_000_000,
        "bid": 1.1,
        "ask": 1.1001,
        "last": 0.0,
        "volume": 0,
        "volume_real": 0.0,
        "flags": 6,
        "source": "MT5",
        **overrides,
    }


def test_tick_store_validates_sorts_and_deduplicates(tmp_path):
    store = TickParquetStore(tmp_path / "ticks")
    later = _tick(timestamp=datetime(2025, 1, 2, 12, 1, tzinfo=UTC), time_msc=1001)
    first = _tick()

    manifest = store.ingest([later, first, first], ingested_at=TICK_TIME)
    repeated = store.ingest([first, later], ingested_at=TICK_TIME)

    table = store.read()
    assert table.schema == TICK_SCHEMA
    assert table.num_rows == 2
    assert table.column("timestamp").to_pylist() == [TICK_TIME, later["timestamp"]]
    assert manifest["row_count"] == repeated["row_count"] == 2
    assert repeated["status"] == "complete"
    assert store.manifest() == repeated


def test_tick_validation_rejects_naive_time_bad_spread_and_source_mismatch():
    with pytest.raises(ValueError, match="Naive timestamp rejected"):
        validate_tick(_tick(timestamp=datetime(2025, 1, 2, 12)))
    with pytest.raises(ValueError, match="ask must not be lower"):
        validate_tick(_tick(ask=1.0))
    with pytest.raises(ValueError, match="source does not match"):
        TickParquetStore("unused").ingest([_tick(source="other")], source="MT5")


def test_tick_store_recovers_after_interrupted_partition_write(tmp_path, monkeypatch):
    import johanna.ticks as tick_module

    store = TickParquetStore(tmp_path / "ticks")
    jan = _tick()
    feb = _tick(
        timestamp=datetime(2025, 2, 1, 12, tzinfo=UTC),
        time_msc=2,
    )
    writer = tick_module._atomic_parquet
    calls = 0

    def fail_on_second_partition(path, rows):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic interrupted tick write")
        writer(path, rows)

    monkeypatch.setattr(tick_module, "_atomic_parquet", fail_on_second_partition)
    with pytest.raises(OSError, match="synthetic interrupted"):
        store.ingest([jan, feb], ingested_at=TICK_TIME)
    assert store.manifest()["status"] == "in_progress"

    monkeypatch.setattr(tick_module, "_atomic_parquet", writer)
    manifest = store.ingest([jan, feb], ingested_at=TICK_TIME)
    assert manifest["status"] == "complete"
    assert manifest["row_count"] == store.read().num_rows == 2
