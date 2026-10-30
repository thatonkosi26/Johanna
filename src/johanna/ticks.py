"""Validated storage for sampled MT5 quote ticks.

These records are snapshots of the most recent quote at each collector poll. They
are not a lossless exchange tick stream.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pyarrow as pa
import pyarrow.parquet as pq

from johanna.time import to_utc

TICK_SCHEMA_VERSION = "1.0.0"
TICK_SCHEMA = pa.schema(
    [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("timestamp", pa.timestamp("us", tz="UTC"), nullable=False),
        pa.field("time_msc", pa.int64(), nullable=True),
        pa.field("bid", pa.float64(), nullable=True),
        pa.field("ask", pa.float64(), nullable=True),
        pa.field("last", pa.float64(), nullable=True),
        pa.field("volume", pa.int64(), nullable=True),
        pa.field("volume_real", pa.float64(), nullable=True),
        pa.field("flags", pa.int64(), nullable=True),
        pa.field("source", pa.string(), nullable=False),
    ],
    metadata={
        b"schema_version": TICK_SCHEMA_VERSION.encode(),
        b"timestamp_semantics": b"UTC quote snapshot time",
        b"collection_semantics": b"latest tick sampled at each collector poll",
        b"volume_semantics": b"provider tick volume and real volume; not bar aggregates",
    },
)


def _optional_number(value: Any, field: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field} must be a finite number or null")
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValueError(f"{field} must be finite and non-negative or null")
    return result


def _optional_integer(value: Any, field: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer or null")
    return value


def validate_tick(row: Mapping[str, Any], *, source: str = "MT5") -> dict[str, Any]:
    """Validate one quote snapshot and normalize its timestamp to UTC."""
    if not isinstance(row, Mapping):
        raise ValueError("Each tick must be a mapping")
    allowed = set(TICK_SCHEMA.names) | {"time_utc"}
    unknown = set(row) - allowed
    if unknown:
        raise ValueError(f"Tick has unknown fields: {', '.join(sorted(unknown))}")
    symbol = row.get("symbol")
    if not isinstance(symbol, str) or not symbol.strip():
        raise ValueError("symbol must be a non-empty string")
    timestamp = row.get("timestamp", row.get("time_utc"))
    if not isinstance(timestamp, datetime):
        raise ValueError("tick timestamp must be a timezone-aware datetime")
    row_source = row.get("source", source)
    if row_source != source:
        raise ValueError("tick source does not match the ingestion source")
    source = row_source
    if not isinstance(source, str) or not source.strip():
        raise ValueError("source must be a non-empty string")
    if source != source.strip():
        raise ValueError("source must not have leading or trailing whitespace")

    result: dict[str, Any] = {
        "symbol": symbol.strip(),
        "timestamp": to_utc(timestamp),
        "time_msc": _optional_integer(row.get("time_msc"), "time_msc"),
        "bid": _optional_number(row.get("bid"), "bid"),
        "ask": _optional_number(row.get("ask"), "ask"),
        "last": _optional_number(row.get("last"), "last"),
        "volume": _optional_integer(row.get("volume"), "volume"),
        "volume_real": _optional_number(row.get("volume_real"), "volume_real"),
        "flags": _optional_integer(row.get("flags"), "flags"),
        "source": source.strip(),
    }
    if result["bid"] is not None and result["ask"] is not None:
        if result["ask"] < result["bid"]:
            raise ValueError("ask must not be lower than bid")
    return result


def _key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(row[field] for field in TICK_SCHEMA.names)


def _sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    values = tuple("" if row[name] is None else repr(row[name]) for name in TICK_SCHEMA.names)
    return (row["symbol"], row["timestamp"], row["time_msc"] or -1, values)


def _atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as stream:
            temp = Path(stream.name)
            json.dump(document, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _atomic_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False) as stream:
            temp = Path(stream.name)
        pq.write_table(pa.Table.from_pylist(rows, schema=TICK_SCHEMA), temp, compression="zstd")
        with temp.open("rb+") as stream:
            os.fsync(stream.fileno())
        temp.replace(path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def _partition_symbol(symbol: str) -> str:
    encoded = quote(symbol, safe="-_.")
    if encoded in {".", ".."}:
        encoded = "_" + encoded.replace(".", "dot")
    return encoded


class TickParquetStore:
    """Idempotent Parquet store for sampled quote snapshots."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.clean_root = self.root / "clean"
        self.manifest_path = self.clean_root / "manifest.json"

    def _partition_path(self, row: Mapping[str, Any]) -> Path:
        stamp = row["timestamp"]
        return (
            self.clean_root
            / f"symbol={_partition_symbol(row['symbol'])}"
            / f"year={stamp:%Y}"
            / f"month={stamp:%m}"
            / "ticks.parquet"
        )

    def read(self) -> pa.Table:
        paths = sorted(self.clean_root.glob("symbol=*/year=*/month=*/ticks.parquet"))
        if not paths:
            return pa.Table.from_pylist([], schema=TICK_SCHEMA)
        rows = [row for path in paths for row in pq.ParquetFile(path).read().to_pylist()]
        rows.sort(key=_sort_key)
        return pa.Table.from_pylist(rows, schema=TICK_SCHEMA)

    def manifest(self) -> dict[str, Any] | None:
        if not self.manifest_path.is_file():
            return None
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    @staticmethod
    def _summary(
        rows: list[dict[str, Any]], *, source: str, ingested_at: datetime, status: str
    ) -> dict[str, Any]:
        stamps = [row["timestamp"] for row in rows]
        return {
            "status": status,
            "source": source,
            "ingestion_time_utc": ingested_at.isoformat().replace("+00:00", "Z"),
            "date_range_utc": {
                "start": min(stamps).isoformat().replace("+00:00", "Z") if stamps else None,
                "end": max(stamps).isoformat().replace("+00:00", "Z") if stamps else None,
            },
            "schema_version": TICK_SCHEMA_VERSION,
            "row_count": len(rows),
        }

    def ingest(
        self,
        ticks: Iterable[Mapping[str, Any]],
        *,
        source: str = "MT5",
        ingested_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Validate and idempotently upsert snapshots with atomic partitions."""
        if not isinstance(source, str) or not source.strip():
            raise ValueError("source must be a non-empty string")
        ingestion_time = to_utc(ingested_at or datetime.now(UTC))
        incoming = [validate_tick(row, source=source) for row in ticks]
        incoming_by_key = {_key(row): row for row in incoming}
        previous_manifest = self.manifest()
        current = self.read().to_pylist()
        rows_by_key = {_key(row): row for row in current}
        changed: set[tuple[Any, ...]] = set()
        for key, row in incoming_by_key.items():
            if key not in rows_by_key:
                changed.add(key)
                rows_by_key[key] = row
        merged = sorted(rows_by_key.values(), key=_sort_key)
        if not incoming:
            summary = self._summary(
                merged, source=source, ingested_at=ingestion_time, status="complete"
            )
            _atomic_json(self.manifest_path, summary)
            return summary

        if not changed and previous_manifest and previous_manifest.get("status") == "complete":
            return previous_manifest

        _atomic_json(
            self.manifest_path,
            self._summary(merged, source=source, ingested_at=ingestion_time, status="in_progress"),
        )
        touched = {self._partition_path(incoming_by_key[key]) for key in changed}
        for path in sorted(touched):
            _atomic_parquet(path, [row for row in merged if self._partition_path(row) == path])
        summary = self._summary(
            merged, source=source, ingested_at=ingestion_time, status="complete"
        )
        _atomic_json(self.manifest_path, summary)
        return summary
