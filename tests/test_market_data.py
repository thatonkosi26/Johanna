from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pyarrow as pa
import pytest

import johanna.market_data as market_data
from johanna.market_data import BAR_SCHEMA, ParquetBarStore, validate_bar


def synthetic_bar(
    timestamp: datetime,
    *,
    source: str = "synthetic-feed",
    symbol: str = "EURUSD",
    timeframe: str = "M1",
    open_price: float = 1.1,
    high: float = 1.2,
    low: float = 1.0,
    close: float = 1.15,
    tick_volume: int | None = 42,
    real_volume: int | None = None,
) -> dict[str, object]:
    """Return synthetic test data only; never represents observed market prices."""
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": timestamp,
        "open": open_price,
        "high": high,
        "low": low,
        "close": close,
        "tick_volume": tick_volume,
        "real_volume": real_volume,
        "source": source,
        "source_timezone": "UTC",
        "source_timestamp_convention": "bar_open",
    }


TEST_INGESTION_TIME = datetime(2025, 2, 1, 12, tzinfo=UTC)


def test_schema_documents_utc_bar_open_and_separate_volume_fields() -> None:
    assert BAR_SCHEMA.field("timestamp").type == pa.timestamp("us", tz="UTC")
    assert BAR_SCHEMA.field("tick_volume").type == pa.int64()
    assert BAR_SCHEMA.field("real_volume").type == pa.int64()
    assert BAR_SCHEMA.metadata[b"timestamp_semantics"] == b"UTC bar-open time"


def test_validation_normalizes_utc_and_preserves_volume_meanings() -> None:
    local_timestamp = datetime(2025, 1, 1, 12, tzinfo=timezone(timedelta(hours=2)))
    row = validate_bar(synthetic_bar(local_timestamp, tick_volume=77, real_volume=5))

    assert row["timestamp"] == datetime(2025, 1, 1, 10, tzinfo=UTC)
    assert row["tick_volume"] == 77
    assert row["real_volume"] == 5


@pytest.mark.parametrize(
    "changes",
    [
        {"high": 1.14},
        {"low": 1.16},
        {"open": "1.1"},
        {"close": float("nan")},
        {"tick_volume": -1},
        {"real_volume": 1.5},
        {"timestamp": "2025-01-01T00:00:00Z"},
        {"timestamp": datetime(2025, 1, 1)},
        {"source_timestamp_convention": "bar_close"},
        {"unknown": "field"},
    ],
)
def test_validation_rejects_invalid_fields_and_ohlc(changes: dict[str, object]) -> None:
    row = synthetic_bar(datetime(2025, 1, 1, tzinfo=UTC))
    row.update(changes)

    with pytest.raises(ValueError):
        validate_bar(row)


def test_validation_rejects_missing_required_field() -> None:
    row = synthetic_bar(datetime(2025, 1, 1, tzinfo=UTC))
    del row["close"]

    with pytest.raises(ValueError, match="missing required fields"):
        validate_bar(row)


def test_ingest_writes_sorted_partitioned_parquet_and_manifest(tmp_path: Path) -> None:
    store = ParquetBarStore(tmp_path / "market")
    later = synthetic_bar(datetime(2025, 1, 1, 0, 2, tzinfo=UTC))
    earlier = synthetic_bar(datetime(2025, 1, 1, 0, 1, tzinfo=UTC))

    manifest = store.ingest(
        [later, earlier], source="synthetic-feed", ingested_at=TEST_INGESTION_TIME
    )
    table = store.read()

    assert table.schema.equals(BAR_SCHEMA, check_metadata=True)
    assert [row["timestamp"] for row in table.to_pylist()] == [
        datetime(2025, 1, 1, 0, 1, tzinfo=UTC),
        datetime(2025, 1, 1, 0, 2, tzinfo=UTC),
    ]
    assert manifest == {
        "status": "complete",
        "source": "synthetic-feed",
        "ingestion_time_utc": "2025-02-01T12:00:00Z",
        "date_range_utc": {
            "start": "2025-01-01T00:01:00Z",
            "end": "2025-01-01T00:02:00Z",
        },
        "schema_version": "1.0.0",
        "row_count": 2,
    }
    assert list(store.clean_root.glob("symbol=EURUSD/timeframe=M1/year=2025/month=01/bars.parquet"))
    assert not store.raw_root.exists()


def test_reingestion_is_idempotent_and_same_source_correction_upserts(tmp_path: Path) -> None:
    store = ParquetBarStore(tmp_path / "market")
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    original = synthetic_bar(timestamp)
    store.ingest([original], source="synthetic-feed", ingested_at=TEST_INGESTION_TIME)
    store.ingest([original], source="synthetic-feed", ingested_at=TEST_INGESTION_TIME)
    assert store.read().num_rows == 1

    corrected = synthetic_bar(timestamp, close=1.16)
    store.ingest([corrected], source="synthetic-feed", ingested_at=TEST_INGESTION_TIME)
    assert store.read().num_rows == 1
    assert store.read().to_pylist()[0]["close"] == 1.16


def test_conflicting_duplicate_within_batch_is_rejected(tmp_path: Path) -> None:
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    store = ParquetBarStore(tmp_path / "market")

    with pytest.raises(ValueError, match="Conflicting duplicate"):
        store.ingest(
            [synthetic_bar(timestamp), synthetic_bar(timestamp, close=1.17)],
            source="synthetic-feed",
            ingested_at=TEST_INGESTION_TIME,
        )


def test_empty_ingestion_creates_manifest_without_parquet_files(tmp_path: Path) -> None:
    store = ParquetBarStore(tmp_path / "market")

    manifest = store.ingest([], source="synthetic-feed", ingested_at=TEST_INGESTION_TIME)

    assert store.read().num_rows == 0
    assert manifest["row_count"] == 0
    assert manifest["date_range_utc"] == {"start": None, "end": None}
    assert not list(store.clean_root.rglob("*.parquet"))


def test_interrupted_multi_partition_ingestion_is_detectable_and_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ParquetBarStore(tmp_path / "market")
    bars = [
        synthetic_bar(datetime(2025, 1, 1, tzinfo=UTC)),
        synthetic_bar(datetime(2025, 2, 1, tzinfo=UTC)),
    ]
    actual_writer = market_data._atomic_parquet
    calls = 0

    def fail_on_second_partition(path: Path, rows: list[dict[str, object]]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("synthetic interruption")
        actual_writer(path, rows)  # type: ignore[arg-type]

    monkeypatch.setattr(market_data, "_atomic_parquet", fail_on_second_partition)
    with pytest.raises(OSError, match="synthetic interruption"):
        store.ingest(bars, source="synthetic-feed", ingested_at=TEST_INGESTION_TIME)
    assert store.manifest()["status"] == "in_progress"
    with pytest.raises(RuntimeError, match="Retry that exact batch"):
        store.ingest(
            [synthetic_bar(datetime(2025, 3, 1, tzinfo=UTC))],
            source="synthetic-feed",
            ingested_at=TEST_INGESTION_TIME,
        )
    assert store.manifest()["status"] == "in_progress"

    monkeypatch.setattr(market_data, "_atomic_parquet", actual_writer)
    manifest = store.ingest(bars, source="synthetic-feed", ingested_at=TEST_INGESTION_TIME)

    assert manifest["status"] == "complete"
    assert "pending_batch_sha256" not in manifest
    assert store.read().num_rows == 2
