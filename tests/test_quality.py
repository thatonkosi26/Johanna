"""Offline quality-report checks using synthetic bars only."""

import json
from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq

from johanna.market_data import BAR_SCHEMA
from johanna.quality import build_quality_report, render_report

T0 = datetime(2025, 1, 6, 10, tzinfo=UTC)


def synthetic_record(minute: int, *, close: float = 1.1) -> dict[str, object]:
    """Create synthetic test data; these are not market observations."""
    return {
        "symbol": "EURUSD",
        "timeframe": "M1",
        "timestamp": T0 + timedelta(minutes=minute),
        "open": 1.1,
        "high": 1.2,
        "low": 1.0,
        "close": close,
        "tick_volume": 3,
        "real_volume": None,
        "source": "synthetic-fixture",
        "source_timezone": "UTC",
        "source_timestamp_convention": "bar_open",
    }


def write_dataset(tmp_path, records):
    clean = tmp_path / "clean"
    partition = clean / "symbol=EURUSD" / "timeframe=M1" / "year=2025" / "month=01"
    partition.mkdir(parents=True)
    pq.write_table(pa.Table.from_pylist(records, schema=BAR_SCHEMA), partition / "bars.parquet")
    (clean / "manifest.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "source": "synthetic-fixture",
                "schema_version": "1.0.0",
                "ingestion_time_utc": "2025-01-06T11:00:00Z",
                "row_count": len(records),
                "date_range_utc": {
                    "start": T0.isoformat().replace("+00:00", "Z"),
                    "end": (T0 + timedelta(minutes=max(0, len(records) - 1)))
                    .isoformat()
                    .replace("+00:00", "Z"),
                },
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_report_counts_unresolved_gap_and_duplicate_without_dropping_rows(tmp_path):
    root = write_dataset(tmp_path, [synthetic_record(0), synthetic_record(2), synthetic_record(2)])
    report = build_quality_report(root, now=datetime(2025, 1, 6, 11, tzinfo=UTC))
    codes = [item["code"] for item in report["confirmed_problems"]]
    assert "duplicate_bar" in codes
    assert report["dataset"]["readable_rows"] == 3
    assert report["unresolved_gaps"][0]["code"] == "bar_gap"
    assert report["expected_closures"] == []
    assert "Unresolved gaps" in render_report(report)


def test_closure_is_expected_only_with_explicit_evidence(tmp_path):
    root = write_dataset(tmp_path, [synthetic_record(0), synthetic_record(2)])
    report = build_quality_report(
        root,
        now=datetime(2025, 1, 6, 11, tzinfo=UTC),
        closure_evidence=[
            {
                "start": "2025-01-06T10:01:00Z",
                "end": "2025-01-06T10:02:00Z",
                "evidence": "synthetic test schedule entry",
            }
        ],
    )
    assert len(report["expected_closures"]) == 1
    assert report["unresolved_gaps"] == []


def test_invalid_ohlc_is_reported_and_row_remains_counted(tmp_path):
    bad = synthetic_record(0)
    bad["high"] = 1.05
    root = write_dataset(tmp_path, [bad])
    report = build_quality_report(root, now=datetime(2025, 1, 6, 11, tzinfo=UTC))
    assert report["dataset"]["readable_rows"] == 1
    assert any(problem["code"] == "invalid_bar" for problem in report["confirmed_problems"])


def test_non_monotonic_physical_order_is_reported(tmp_path):
    root = write_dataset(tmp_path, [synthetic_record(1), synthetic_record(0)])
    report = build_quality_report(root, now=datetime(2025, 1, 6, 11, tzinfo=UTC))
    assert any(
        problem["code"] == "non_monotonic_timestamp" for problem in report["confirmed_problems"]
    )


def test_schema_column_violation_is_reported(tmp_path):
    root = write_dataset(tmp_path, [synthetic_record(0)])
    parquet_file = next((root / "clean").glob("symbol=*/timeframe=*/year=*/month=*/bars.parquet"))
    record = synthetic_record(0)
    del record["close"]
    pq.write_table(pa.Table.from_pylist([record]), parquet_file)
    report = build_quality_report(root, now=datetime(2025, 1, 6, 11, tzinfo=UTC))
    assert any(
        problem["code"] == "schema_columns" and "close" in problem["missing"]
        for problem in report["confirmed_problems"]
    )


def test_manifest_absent_and_dataset_empty_are_explicit(tmp_path):
    report = build_quality_report(tmp_path, now=datetime(2025, 1, 6, 11, tzinfo=UTC))
    assert {item["code"] for item in report["confirmed_problems"]} == {"manifest_missing"}
    assert {item["code"] for item in report["unresolved_gaps"]} >= {
        "dataset_empty",
        "dataset_manifest_unknown",
    }
