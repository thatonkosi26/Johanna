from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from johanna.ingestion import HISTDATA_TIMEZONE, ingest_historical_file
from johanna.market_data import ParquetBarStore

FIXTURES = Path(__file__).parent / "fixtures"
INGESTED_AT = datetime(2025, 2, 1, tzinfo=UTC)


def test_mt5_export_maps_fields_filters_dates_and_records_provenance(tmp_path: Path) -> None:
    file_path = FIXTURES / "synthetic_mt5_bars.csv"
    store = ParquetBarStore(tmp_path / "store")
    report = ingest_historical_file(
        file_path,
        store=store,
        file_format="mt5",
        symbol="EURUSD.demo",
        timeframe="M1",
        source_timezone="UTC",
        start_date=date(2025, 1, 2),
        end_date=date(2025, 1, 2),
        acquired_from="synthetic test fixture",
        license_note="Synthetic; no market source",
        ingested_at=INGESTED_AT,
    )

    rows = store.read().to_pylist()
    assert report.rows_read == report.rows_selected == 2
    assert rows[0]["symbol"] == "EURUSD.demo"
    assert rows[0]["timestamp"] == datetime(2025, 1, 2, tzinfo=UTC)
    assert (rows[0]["tick_volume"], rows[0]["real_volume"]) == (12, 3)
    assert rows[0]["source_timestamp_convention"] == "bar_open"
    assert report.manifest["acquisition_info"]["acquired_from"] == "synthetic test fixture"
    assert report.manifest["acquisition_info"]["filename"] == file_path.name
    assert report.manifest["acquisition_info"]["input_file_copied"] is False
    assert file_path.exists()


def test_histdata_ascii_m1_maps_fixed_est_and_does_not_guess_volume(tmp_path: Path) -> None:
    file_path = FIXTURES / "synthetic_histdata_generic_ascii_m1.csv"
    store = ParquetBarStore(tmp_path / "store")
    report = ingest_historical_file(
        file_path,
        store=store,
        file_format="histdata-ascii-m1",
        symbol="EURUSD",
        timeframe="M1",
        timestamp_convention="bar_open",
        ingested_at=INGESTED_AT,
    )

    rows = store.read().to_pylist()
    assert report.rows_selected == 2
    assert rows[0]["timestamp"] == datetime(2025, 1, 2, 5, tzinfo=UTC)
    assert rows[0]["source_timezone"] == HISTDATA_TIMEZONE
    assert rows[0]["tick_volume"] is None
    assert rows[0]["real_volume"] is None
    assert report.warnings
    assert "not mapped" in report.manifest["acquisition_info"]["volume_note"]


def test_histdata_requires_explicit_timestamp_convention_and_m1(tmp_path: Path) -> None:
    file_path = FIXTURES / "synthetic_histdata_generic_ascii_m1.csv"
    args = {
        "store": ParquetBarStore(tmp_path / "store"),
        "file_format": "histdata-ascii-m1",
        "symbol": "EURUSD",
        "timeframe": "M1",
    }
    with pytest.raises(ValueError, match="not established"):
        ingest_historical_file(file_path, **args)
    with pytest.raises(ValueError, match="timeframe M1"):
        ingest_historical_file(
            file_path, timestamp_convention="bar_open", **{**args, "timeframe": "M5"}
        )


def test_invalid_bars_are_skipped_and_manifest_records_validation_errors(tmp_path: Path) -> None:
    file_path = tmp_path / "synthetic_bad_mt5.csv"
    file_path.write_text(
        "<DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>\n"
        "2025.01.02 00:00:00 1.1 1.0 1.0 1.1 12 3 8\n"
        "2025.01.02 00:01:00 1.1 1.2 1.0 1.15 12 3 8\n",
        encoding="utf-8",
    )
    store = ParquetBarStore(tmp_path / "store")
    report = ingest_historical_file(
        file_path,
        store=store,
        file_format="mt5",
        symbol="EURUSD",
        timeframe="M1",
        source_timezone="UTC",
        ingested_at=INGESTED_AT,
    )
    assert report.rows_selected == 1
    assert len(report.validation_errors) == 1
    assert report.manifest["validation_error_count"] == 1
    assert store.read().num_rows == 1


def test_ambiguous_mt5_local_time_is_recorded_and_not_guessed(tmp_path: Path) -> None:
    file_path = tmp_path / "synthetic_dst_mt5.csv"
    file_path.write_text(
        "<DATE> <TIME> <OPEN> <HIGH> <LOW> <CLOSE> <TICKVOL> <VOL> <SPREAD>\n"
        "2025.11.02 01:30:00 1.1 1.2 1.0 1.15 12 3 8\n",
        encoding="utf-8",
    )
    store = ParquetBarStore(tmp_path / "store")
    report = ingest_historical_file(
        file_path,
        store=store,
        file_format="mt5",
        symbol="EURUSD",
        timeframe="M1",
        source_timezone="America/New_York",
        ingested_at=INGESTED_AT,
    )
    assert store.read().num_rows == 0
    assert "ambiguous" in report.validation_errors[0]
    assert report.manifest["validation_error_count"] == 1


def test_repeated_file_ingestion_is_idempotent(tmp_path: Path) -> None:
    file_path = FIXTURES / "synthetic_mt5_bars.csv"
    store = ParquetBarStore(tmp_path / "store")
    args = {
        "store": store,
        "file_format": "mt5",
        "symbol": "EURUSD",
        "timeframe": "M1",
        "source_timezone": "UTC",
        "ingested_at": INGESTED_AT,
    }
    ingest_historical_file(file_path, **args)
    ingest_historical_file(file_path, **args)
    assert store.read().num_rows == 2
