"""Import locally supplied MT5 and HistData OHLC bar text files.

No files are downloaded or copied by this module. MT5 timestamps require a
caller-declared source timezone. HistData Generic ASCII M1 timestamps use the
provider's fixed EST (UTC-05:00, no DST) clock; its generic volume column is not
mapped because HistData says it removes volume information from delivered data.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from johanna.market_data import ParquetBarStore, validate_bar
from johanna.time import get_timezone, to_utc

HISTDATA_TIMEZONE = "Etc/GMT+5"  # IANA Etc/GMT signs are intentionally reversed.
_MT5_SOURCE = "mt5_export"
_HISTDATA_SOURCE = "histdata_generic_ascii_m1"
_FIELD_NAMES = {
    "date": "date",
    "time": "time",
    "open": "open",
    "high": "high",
    "low": "low",
    "close": "close",
    "tickvol": "tick_volume",
    "tick_volume": "tick_volume",
    "vol": "real_volume",
    "volume": "real_volume",
    "spread": "spread",
}


class HistoricalImportError(ValueError):
    """An unsupported or malformed historical bar file."""


@dataclass(frozen=True)
class ImportReport:
    source: str
    rows_read: int
    rows_selected: int
    validation_errors: tuple[str, ...]
    warnings: tuple[str, ...]
    manifest: Mapping[str, Any]


def _split_row(line: str) -> list[str]:
    if "\t" in line:
        return [part.strip() for part in next(csv.reader([line], delimiter="\t"))]
    if ";" in line:
        return [part.strip() for part in next(csv.reader([line], delimiter=";"))]
    if "," in line:
        return [part.strip() for part in next(csv.reader([line], delimiter=","))]
    return line.split()


def _parse_mt5_datetime(date_text: str, time_text: str) -> datetime:
    for time_format in ("%H:%M:%S.%f", "%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(f"{date_text} {time_text}", f"%Y.%m.%d {time_format}")
        except ValueError:
            pass
    raise HistoricalImportError("expected MT5 timestamp as YYYY.MM.DD HH:MM[:SS[.fff]]")


def _as_price(raw: str, name: str) -> float:
    try:
        value = float(raw)
    except ValueError as exc:
        raise HistoricalImportError(f"invalid {name} price") from exc
    return value


def _as_volume(raw: str, name: str) -> int | None:
    if raw == "":
        return None
    if not re.fullmatch(r"\d+", raw):
        raise HistoricalImportError(f"{name} must be a non-negative integer")
    return int(raw)


def _mt5_records(
    path: Path,
    *,
    symbol: str,
    timeframe: str,
    source_timezone: str,
) -> Iterator[dict[str, Any]]:
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    nonempty = [(n, line.strip()) for n, line in enumerate(lines, start=1) if line.strip()]
    if not nonempty:
        return
    first_tokens = _split_row(nonempty[0][1])
    header_map: dict[str, int] | None = None
    if any(token.startswith("<") and token.endswith(">") for token in first_tokens):
        header_map = {}
        for index, token in enumerate(first_tokens):
            key = token.strip("<> ").lower().replace(" ", "_")
            normalized = _FIELD_NAMES.get(key)
            if normalized:
                header_map[normalized] = index
        if not {"date", "time", "open", "high", "low", "close"} <= header_map.keys():
            raise HistoricalImportError("MT5 header is missing required DATE/TIME/OHLC columns")
        data = nonempty[1:]
    else:
        header_map = {
            "date": 0,
            "time": 1,
            "open": 2,
            "high": 3,
            "low": 4,
            "close": 5,
            "tick_volume": 6,
            "real_volume": 7,
            "spread": 8,
        }
        data = nonempty

    for line_number, line in data:
        fields = _split_row(line)
        required_length = max(header_map.values()) + 1
        if len(fields) != required_length:
            raise HistoricalImportError(
                f"line {line_number}: expected {required_length} fields, found {len(fields)}"
            )
        try:
            local_time = _parse_mt5_datetime(fields[header_map["date"]], fields[header_map["time"]])
            timestamp = to_utc(local_time, source_timezone)
            tick_volume = (
                _as_volume(fields[header_map["tick_volume"]], "tick_volume")
                if "tick_volume" in header_map
                else None
            )
            real_volume = (
                _as_volume(fields[header_map["real_volume"]], "real_volume")
                if "real_volume" in header_map
                else None
            )
            if "spread" in header_map:
                _as_volume(fields[header_map["spread"]], "spread")
            yield {
                "symbol": symbol,
                "timeframe": timeframe,
                "timestamp": timestamp,
                "open": _as_price(fields[header_map["open"]], "open"),
                "high": _as_price(fields[header_map["high"]], "high"),
                "low": _as_price(fields[header_map["low"]], "low"),
                "close": _as_price(fields[header_map["close"]], "close"),
                "tick_volume": tick_volume,
                "real_volume": real_volume,
                "source": _MT5_SOURCE,
                "source_timezone": source_timezone,
                "source_timestamp_convention": "bar_open",
            }
        except (KeyError, IndexError) as exc:
            raise HistoricalImportError(f"line {line_number}: malformed MT5 export row") from exc


def _histdata_records(
    path: Path,
    *,
    symbol: str,
    timeframe: str,
    timestamp_convention: str,
) -> Iterator[dict[str, Any]]:
    if timeframe != "M1":
        raise HistoricalImportError("HistData Generic ASCII bar files are M1; use timeframe M1")
    if timestamp_convention != "bar_open":
        raise HistoricalImportError(
            "HistData timestamp convention must be explicitly established as bar_open"
        )
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        fields = _split_row(line)
        if len(fields) != 6:
            raise HistoricalImportError(
                f"line {line_number}: HistData Generic ASCII M1 expects 6 semicolon fields"
            )
        try:
            naive = datetime.strptime(fields[0], "%Y%m%d %H%M%S")
        except ValueError as exc:
            raise HistoricalImportError(
                f"line {line_number}: expected HistData timestamp YYYYMMDD HHMMSS"
            ) from exc
        # Validate the provider's placeholder field but do not mislabel it as
        # tick or trade volume; the canonical schema has no generic volume field.
        _as_volume(fields[5], "HistData generic volume placeholder")
        yield {
            "symbol": symbol,
            "timeframe": timeframe,
            "timestamp": to_utc(naive, HISTDATA_TIMEZONE),
            "open": _as_price(fields[1], "open"),
            "high": _as_price(fields[2], "high"),
            "low": _as_price(fields[3], "low"),
            "close": _as_price(fields[4], "close"),
            "tick_volume": None,
            "real_volume": None,
            "source": _HISTDATA_SOURCE,
            "source_timezone": HISTDATA_TIMEZONE,
            "source_timestamp_convention": timestamp_convention,
        }


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parse_aware_datetime(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp with a timezone") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must include a timezone offset")
    return parsed.astimezone(UTC)


def ingest_historical_file(
    input_file: str | Path,
    *,
    store: ParquetBarStore,
    file_format: str,
    symbol: str,
    timeframe: str,
    source_timezone: str | None = None,
    timestamp_convention: str | None = None,
    start_date: date | None = None,
    end_date: date | None = None,
    acquired_from: str | None = None,
    acquired_at: str | None = None,
    license_note: str | None = None,
    ingested_at: datetime | None = None,
) -> ImportReport:
    """Import a user-provided local file; errors are recorded and bad rows skipped."""
    path = Path(input_file)
    if not path.is_file():
        raise FileNotFoundError(f"Input file does not exist: {path}")
    if not symbol.strip() or not timeframe.strip():
        raise ValueError("symbol and timeframe must be non-empty")
    if start_date and end_date and start_date > end_date:
        raise ValueError("start_date must not be after end_date")

    if file_format == "mt5":
        if not source_timezone:
            raise ValueError("MT5 exports require an explicit --source-timezone")
        get_timezone(source_timezone)
        source = _MT5_SOURCE
        records = _mt5_records(
            path, symbol=symbol, timeframe=timeframe, source_timezone=source_timezone
        )
        convention = "bar_open (MetaQuotes MqlRates period-start semantics)"
        timezone_name = source_timezone
    elif file_format == "histdata-ascii-m1":
        if source_timezone not in (None, HISTDATA_TIMEZONE):
            raise ValueError("HistData timestamps use fixed EST (Etc/GMT+5), without DST")
        if timeframe != "M1":
            raise ValueError("HistData Generic ASCII bar files are M1; use timeframe M1")
        if timestamp_convention is None:
            raise ValueError(
                "HistData timestamp meaning is not established by its format; "
                "explicitly pass --timestamp-convention bar_open only if verified"
            )
        if timestamp_convention != "bar_open":
            raise ValueError(
                "HistData timestamp convention must be explicitly established as bar_open"
            )
        source = _HISTDATA_SOURCE
        records = _histdata_records(
            path,
            symbol=symbol,
            timeframe=timeframe,
            timestamp_convention=timestamp_convention,
        )
        convention = timestamp_convention
        timezone_name = HISTDATA_TIMEZONE
    else:
        raise ValueError("file_format must be 'mt5' or 'histdata-ascii-m1'")

    valid_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    rows_read = 0
    try:
        for line_number, record in enumerate(records, 1):
            rows_read += 1
            try:
                normalized = validate_bar(record, source=source)
                day = normalized["timestamp"].date()
                if start_date and day < start_date or end_date and day > end_date:
                    continue
                valid_rows.append(normalized)
            except (ValueError, OverflowError) as exc:
                if len(errors) < 100:
                    errors.append(f"row {line_number}: {exc}")
    except (HistoricalImportError, ValueError, OverflowError) as exc:
        rows_read += 1
        if len(errors) < 100:
            errors.append(str(exc))

    metadata = {
        "format": file_format,
        "filename": path.name,
        "file_sha256": _file_sha256(path),
        "acquired_from": acquired_from,
        "acquired_at_utc": (
            _parse_aware_datetime(acquired_at, "acquired_at").isoformat().replace("+00:00", "Z")
            if acquired_at
            else None
        ),
        "license_or_permission_note": license_note,
        "symbol": symbol,
        "timeframe": timeframe,
        "source_timezone": timezone_name,
        "source_timestamp_convention": convention,
        "requested_date_range_utc": {
            "start": start_date.isoformat() if start_date else None,
            "end": end_date.isoformat() if end_date else None,
        },
        "input_file_copied": False,
        "volume_note": (
            "HistData Generic ASCII volume field is not mapped; provider says delivered "
            "data has no volume information."
            if file_format == "histdata-ascii-m1"
            else "MT5 tick_volume and real_volume are mapped separately when present."
        ),
    }
    if file_format == "histdata-ascii-m1":
        metadata["import_warnings"] = [
            "HistData timestamp convention was explicitly supplied by the operator.",
            "HistData's Volume column is not assigned to tick_volume or real_volume.",
        ]
    manifest = store.ingest(
        valid_rows,
        source=source,
        ingested_at=ingested_at,
        acquisition_info=metadata,
        validation_errors=errors,
    )
    warnings = tuple(metadata.get("import_warnings", []))
    return ImportReport(source, rows_read, len(valid_rows), tuple(errors), warnings, manifest)


def _date_argument(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must use YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import a local historical OHLC bar file")
    parser.add_argument("--file", required=True, type=Path, help="local file supplied by the user")
    parser.add_argument(
        "--format", required=True, choices=("mt5", "histdata-ascii-m1"), dest="file_format"
    )
    parser.add_argument("--symbol", required=True, help="exact symbol label to store")
    parser.add_argument("--timeframe", required=True, help="bar timeframe, e.g. M1")
    parser.add_argument(
        "--source-timezone", help="required for MT5 exports, e.g. UTC or Europe/London"
    )
    parser.add_argument(
        "--timestamp-convention",
        choices=("bar_open",),
        help="required for HistData after verifying the file labels bar-open times",
    )
    parser.add_argument("--start-date", type=_date_argument, help="inclusive UTC date filter")
    parser.add_argument("--end-date", type=_date_argument, help="inclusive UTC date filter")
    parser.add_argument("--acquired-from", help="where/how this file was legally obtained")
    parser.add_argument("--acquired-at", help="known acquisition time as offset-aware ISO-8601")
    parser.add_argument("--license-note", help="permission or license note, when known")
    parser.add_argument("--root", type=Path, default=Path("data"), help="Parquet store root")
    args = parser.parse_args(argv)
    try:
        report = ingest_historical_file(
            args.file,
            store=ParquetBarStore(args.root),
            file_format=args.file_format,
            symbol=args.symbol,
            timeframe=args.timeframe,
            source_timezone=args.source_timezone,
            timestamp_convention=args.timestamp_convention,
            start_date=args.start_date,
            end_date=args.end_date,
            acquired_from=args.acquired_from,
            acquired_at=args.acquired_at,
            license_note=args.license_note,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Import failed: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "source": report.source,
                "rows_read": report.rows_read,
                "rows_selected": report.rows_selected,
                "validation_error_count": len(report.validation_errors),
                "validation_errors": report.validation_errors,
                "warnings": report.warnings,
                "manifest": report.manifest,
            },
            indent=2,
        )
    )
    return 1 if report.validation_errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
