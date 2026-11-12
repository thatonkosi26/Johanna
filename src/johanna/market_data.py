"""Validated market-bar schema and local partitioned Parquet storage.

Clean bars use UTC-aware, bar-open timestamps. The source must explicitly declare
``bar_open``; source-close timestamps must be converted using source-specific rules
before ingestion. ``tick_volume`` counts price updates and is distinct from
``real_volume``, which represents traded quantity when the provider supplies it.
Missing volume measures are null, never substituted for one another.

The clean store is written under ``<root>/clean``. ``<root>/raw`` is kept separate
for future immutable source captures; this module does not fabricate or archive raw
records because no source adapters are part of the current project phase.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from johanna.fsutil import replace_with_retry
from johanna.time import (
    BarTimestampConvention,
    get_timezone,
    parse_bar_timestamp_convention,
    to_utc,
)

SCHEMA_VERSION = "1.0.0"
BAR_SCHEMA = pa.schema(
    [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("timeframe", pa.string(), nullable=False),
        pa.field("timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("open", pa.float64(), nullable=False),
        pa.field("high", pa.float64(), nullable=False),
        pa.field("low", pa.float64(), nullable=False),
        pa.field("close", pa.float64(), nullable=False),
        pa.field("tick_volume", pa.int64(), nullable=True),
        pa.field("real_volume", pa.int64(), nullable=True),
        pa.field("source", pa.string(), nullable=False),
        pa.field("source_timezone", pa.string(), nullable=False),
        pa.field("source_timestamp_convention", pa.string(), nullable=False),
    ],
    metadata={
        b"schema_version": SCHEMA_VERSION.encode(),
        b"timestamp_semantics": b"UTC bar-open time",
        b"volume_semantics": b"tick_volume is update count; real_volume is traded quantity",
    },
)

_SAFE_PARTITION_VALUE = re.compile(r"^[A-Za-z0-9._-]+$")
_REQUIRED_FIELDS = {
    "symbol",
    "timeframe",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "source",
    "source_timezone",
    "source_timestamp_convention",
}
_OPTIONAL_FIELDS = {"tick_volume", "real_volume"}
_KEY_FIELDS = ("symbol", "timeframe", "timestamp", "source")
_MANIFEST_NAME = "manifest.json"


def _nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _partition_value(value: str, field: str) -> str:
    if not _SAFE_PARTITION_VALUE.fullmatch(value):
        raise ValueError(f"{field} contains characters unsafe for a partition path: {value!r}")
    return value


def _finite_price(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"{field} must be finite and greater than zero")
    return result


def _optional_volume(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer or null")
    return value


def validate_bar(row: Mapping[str, Any], *, source: str | None = None) -> dict[str, Any]:
    """Validate and normalize one bar mapping to the documented schema."""
    if not isinstance(row, Mapping):
        raise ValueError("Each bar must be a mapping of schema field names to values")
    keys = set(row)
    missing = _REQUIRED_FIELDS - keys
    unknown = keys - _REQUIRED_FIELDS - _OPTIONAL_FIELDS
    if missing:
        raise ValueError(f"Bar is missing required fields: {', '.join(sorted(missing))}")
    if unknown:
        raise ValueError(f"Bar has unknown fields: {', '.join(sorted(unknown))}")

    symbol = _partition_value(_nonempty_string(row["symbol"], "symbol"), "symbol")
    timeframe = _partition_value(_nonempty_string(row["timeframe"], "timeframe"), "timeframe")
    row_source = _partition_value(_nonempty_string(row["source"], "source"), "source")
    if source is not None and row_source != source:
        raise ValueError(f"Bar source {row_source!r} does not match ingestion source {source!r}")

    source_timezone = _nonempty_string(row["source_timezone"], "source_timezone")
    get_timezone(source_timezone)
    convention = parse_bar_timestamp_convention(row["source_timestamp_convention"])
    if convention is not BarTimestampConvention.BAR_OPEN:
        raise ValueError(
            "Clean-store timestamps use bar-open semantics; normalize source bar-close "
            "timestamps explicitly before ingestion"
        )

    timestamp = row["timestamp"]
    if not isinstance(timestamp, datetime):
        raise ValueError("timestamp must be a timezone-aware datetime, not a string or number")
    timestamp_utc = to_utc(timestamp)

    prices = {name: _finite_price(row[name], name) for name in ("open", "high", "low", "close")}
    if prices["high"] < max(prices["open"], prices["close"], prices["low"]):
        raise ValueError("high must be greater than or equal to open, close, and low")
    if prices["low"] > min(prices["open"], prices["close"], prices["high"]):
        raise ValueError("low must be less than or equal to open, close, and high")

    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "timestamp": timestamp_utc,
        **prices,
        "tick_volume": _optional_volume(row.get("tick_volume"), "tick_volume"),
        "real_volume": _optional_volume(row.get("real_volume"), "real_volume"),
        "source": row_source,
        "source_timezone": source_timezone,
        "source_timestamp_convention": convention.value,
    }


def _row_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row[field] for field in _KEY_FIELDS)


def _content(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row[field] for field in BAR_SCHEMA.names)


def _sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (row["symbol"], row["timeframe"], row["timestamp"], row["source"])


def _batch_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    canonical_rows = []
    for row in sorted(rows, key=_sort_key):
        canonical_rows.append(
            {
                **row,
                "timestamp": row["timestamp"].isoformat(),
            }
        )
    payload = json.dumps(canonical_rows, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(payload).hexdigest()


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as stream:
            temp_path = Path(stream.name)
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _atomic_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
            temp_path = Path(stream.name)
        table = pa.Table.from_pylist(rows, schema=BAR_SCHEMA)
        pq.write_table(table, temp_path, compression="zstd")
        with temp_path.open("rb+") as stream:
            os.fsync(stream.fileno())
        replace_with_retry(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


class ParquetBarStore:
    """Idempotent clean-bar store with symbol/timeframe/year/month partitions.

    Repeated identical source bars are no-ops. A later bar for the same
    ``(symbol, timeframe, timestamp, source)`` replaces that source's earlier
    value, allowing explicit data corrections without retaining duplicates.
    Writes use same-directory temporary files and atomic file replacement. A
    manifest marked ``in_progress`` signals an interrupted multi-partition write;
    the next successful ingestion rebuilds its summary from the readable files.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.clean_root = self.root / "clean"
        self.raw_root = self.root / "raw"
        self.manifest_path = self.clean_root / _MANIFEST_NAME

    def read(self) -> pa.Table:
        """Read all clean partitions in deterministic schema and row order."""
        paths = sorted(self.clean_root.glob("symbol=*/timeframe=*/year=*/month=*/bars.parquet"))
        if not paths:
            return pa.Table.from_pylist([], schema=BAR_SCHEMA)
        rows = [row for path in paths for row in pq.ParquetFile(path).read().to_pylist()]
        rows.sort(key=_sort_key)
        return pa.Table.from_pylist(rows, schema=BAR_SCHEMA)

    def manifest(self) -> dict[str, Any] | None:
        """Return the last atomically written manifest, if one exists."""
        if not self.manifest_path.exists():
            return None
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def _partition_path(self, row: Mapping[str, Any]) -> Path:
        timestamp = row["timestamp"]
        return (
            self.clean_root
            / f"symbol={row['symbol']}"
            / f"timeframe={row['timeframe']}"
            / f"year={timestamp:%Y}"
            / f"month={timestamp:%m}"
            / "bars.parquet"
        )

    def _summary(
        self,
        rows: list[dict[str, Any]],
        *,
        source: str,
        ingested_at: datetime,
        status: str,
        acquisition_info: Mapping[str, Any] | None = None,
        validation_errors: Iterable[str] = (),
    ) -> dict[str, Any]:
        timestamps = [row["timestamp"] for row in rows]
        summary = {
            "status": status,
            "source": source,
            "ingestion_time_utc": ingested_at.isoformat().replace("+00:00", "Z"),
            "date_range_utc": {
                "start": min(timestamps).isoformat().replace("+00:00", "Z") if rows else None,
                "end": max(timestamps).isoformat().replace("+00:00", "Z") if rows else None,
            },
            "schema_version": SCHEMA_VERSION,
            "row_count": len(rows),
        }
        errors = list(validation_errors)
        if acquisition_info is not None:
            summary["acquisition_info"] = dict(acquisition_info)
        if errors:
            summary["validation_errors"] = errors
            summary["validation_error_count"] = len(errors)
        return summary

    def ingest(
        self,
        bars: Iterable[Mapping[str, Any]],
        *,
        source: str,
        ingested_at: datetime | None = None,
        acquisition_info: Mapping[str, Any] | None = None,
        validation_errors: Iterable[str] = (),
    ) -> dict[str, Any]:
        """Validate, upsert, sort, and persist clean bars; empty input is valid.

        ``ingested_at`` is injectable for deterministic tests and must be aware.
        It is manifest metadata only and does not alter bar records.
        """
        source = _partition_value(_nonempty_string(source, "source"), "source")
        ingestion_time = to_utc(ingested_at or datetime.now(UTC))
        error_records = tuple(validation_errors)
        incoming = [validate_bar(row, source=source) for row in bars]

        incoming_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in incoming:
            key = _row_key(row)
            previous = incoming_by_key.get(key)
            if previous is not None and _content(previous) != _content(row):
                raise ValueError(f"Conflicting duplicate bars in one input batch: {key!r}")
            incoming_by_key[key] = row

        previous_manifest = self.manifest()
        pending_batch_hash = _batch_digest(incoming_by_key.values())
        if previous_manifest and previous_manifest.get("status") == "in_progress":
            expected_hash = previous_manifest.get("pending_batch_sha256")
            if pending_batch_hash != expected_hash:
                raise RuntimeError(
                    "A prior ingestion was interrupted. Retry that exact batch before "
                    "starting another ingestion."
                )

        current = self.read().to_pylist()
        rows_by_key = {_row_key(row): row for row in current}
        changed_keys: set[tuple[Any, ...]] = set()
        for key, row in incoming_by_key.items():
            if key not in rows_by_key or _content(rows_by_key[key]) != _content(row):
                changed_keys.add(key)
                rows_by_key[key] = row

        merged_rows = sorted(rows_by_key.values(), key=_sort_key)
        if not incoming:
            # Empty input creates a valid empty manifest but no Parquet fragments.
            _atomic_json(
                self.manifest_path,
                self._summary(
                    merged_rows,
                    source=source,
                    ingested_at=ingestion_time,
                    status="complete",
                    acquisition_info=acquisition_info,
                    validation_errors=error_records,
                ),
            )
            return self.manifest() or {}

        touched_keys = changed_keys
        if not touched_keys:
            _atomic_json(
                self.manifest_path,
                self._summary(
                    merged_rows,
                    source=source,
                    ingested_at=ingestion_time,
                    status="complete",
                    acquisition_info=acquisition_info,
                    validation_errors=error_records,
                ),
            )
            return self.manifest() or {}

        pending_manifest = self._summary(
            current,
            source=source,
            ingested_at=ingestion_time,
            status="in_progress",
            acquisition_info=acquisition_info,
            validation_errors=error_records,
        )
        pending_manifest["pending_batch_sha256"] = pending_batch_hash
        pending_manifest["pending_row_count"] = len(incoming_by_key)
        _atomic_json(self.manifest_path, pending_manifest)

        touched_partitions = {self._partition_path(incoming_by_key[key]) for key in touched_keys}
        for partition_path in sorted(touched_partitions):
            partition_rows = [
                row for row in merged_rows if self._partition_path(row) == partition_path
            ]
            _atomic_parquet(partition_path, partition_rows)

        _atomic_json(
            self.manifest_path,
            self._summary(
                merged_rows,
                source=source,
                ingested_at=ingestion_time,
                status="complete",
                acquisition_info=acquisition_info,
                validation_errors=error_records,
            ),
        )
        return self.manifest() or {}
