"""Reproducible quality checks for Johanna's canonical Parquet bar store.

The report never repairs or rewrites input data. Gaps remain unresolved unless a
caller supplies an explicit UTC closure interval and evidence description.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from johanna.market_data import BAR_SCHEMA, SCHEMA_VERSION, validate_bar


def _iso(value: Any) -> str | None:
    return value.isoformat().replace("+00:00", "Z") if isinstance(value, datetime) else None


def _timeframe_delta(value: str) -> timedelta | None:
    match = re.fullmatch(r"([MHDW])([1-9][0-9]*)", value.upper())
    if not match:
        return None
    unit, amount_text = match.groups()
    amount = int(amount_text)
    return {
        "M": timedelta(minutes=amount),
        "H": timedelta(hours=amount),
        "D": timedelta(days=amount),
        "W": timedelta(weeks=amount),
    }[unit]


def _closure_evidence(items: list[dict[str, str]] | None) -> list[dict[str, Any]]:
    parsed = []
    for item in items or []:
        try:
            start = datetime.fromisoformat(item["start"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(item["end"].replace("Z", "+00:00"))
            evidence = item["evidence"].strip()
            if start.tzinfo is None or end.tzinfo is None or start >= end or not evidence:
                raise ValueError
        except (KeyError, ValueError, AttributeError) as exc:
            raise ValueError(
                "Closure evidence needs aware start/end and a non-empty evidence field"
            ) from exc
        parsed.append(
            {"start": start.astimezone(UTC), "end": end.astimezone(UTC), "evidence": evidence}
        )
    return parsed


def build_quality_report(
    root: str | Path,
    *,
    now: datetime | None = None,
    stale_after_hours: float = 48,
    closure_evidence: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Inspect all clean Parquet partitions under ``root`` without changing them."""
    root = Path(root)
    clean = root / "clean"
    manifest_path = clean / "manifest.json"
    manifest: dict[str, Any] | None = None
    confirmed: list[dict[str, Any]] = []
    unresolved: list[dict[str, Any]] = []
    expected: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    rows: list[tuple[dict[str, Any], str, int]] = []
    current_time = now or datetime.now(UTC)
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    now_utc = current_time.astimezone(UTC)
    closures = _closure_evidence(closure_evidence)

    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                raise ValueError("manifest root must be an object")
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            confirmed.append({"code": "manifest_invalid", "detail": str(exc)})
    else:
        confirmed.append(
            {"code": "manifest_missing", "detail": "No clean-store manifest was found."}
        )

    files = sorted(clean.glob("symbol=*/timeframe=*/year=*/month=*/bars.parquet"))
    if not files:
        unresolved.append(
            {"code": "dataset_empty", "detail": "No clean Parquet partitions were found."}
        )
    expected_names = set(BAR_SCHEMA.names)
    for file in files:
        try:
            table = pq.ParquetFile(file).read()
        except Exception as exc:  # report corrupt/unreadable files rather than hiding them
            confirmed.append({"code": "parquet_unreadable", "file": str(file), "detail": str(exc)})
            exclusions.append({"file": str(file), "rows": None, "reason": "file could not be read"})
            continue
        names = set(table.column_names)
        missing = sorted(expected_names - names)
        extra = sorted(names - expected_names)
        if missing or extra:
            confirmed.append(
                {"code": "schema_columns", "file": str(file), "missing": missing, "extra": extra}
            )
        for field in BAR_SCHEMA:
            if field.name in table.column_names:
                actual_field = table.schema.field(field.name)
                if actual_field.type != field.type:
                    confirmed.append(
                        {
                            "code": "schema_type",
                            "file": str(file),
                            "field": field.name,
                            "expected": str(field.type),
                            "actual": str(actual_field.type),
                        }
                    )
                if actual_field.nullable != field.nullable:
                    confirmed.append(
                        {
                            "code": "schema_nullability",
                            "file": str(file),
                            "field": field.name,
                            "expected_nullable": field.nullable,
                            "actual_nullable": actual_field.nullable,
                        }
                    )
        actual_metadata = table.schema.metadata or {}
        for key in (b"schema_version", b"timestamp_semantics", b"volume_semantics"):
            if actual_metadata.get(key) != (BAR_SCHEMA.metadata or {}).get(key):
                confirmed.append(
                    {
                        "code": "schema_metadata",
                        "file": str(file),
                        "field": key.decode(),
                        "expected": (BAR_SCHEMA.metadata or {}).get(key, b"").decode(),
                        "actual": actual_metadata.get(key, b"").decode(),
                    }
                )
        for index, record in enumerate(table.to_pylist()):
            rows.append((record, str(file), index))

    valid_time_groups: dict[tuple[str, str, str], list[tuple[datetime, str, int]]] = defaultdict(
        list
    )
    duplicate_map: dict[tuple[Any, ...], list[tuple[str, int]]] = defaultdict(list)
    observed_times: list[datetime] = []
    symbols: set[str] = set()
    timeframes: set[str] = set()
    sources: set[str] = set()
    optional_nulls = {name: 0 for name in ("tick_volume", "real_volume")}
    spread_field_seen = False
    for record, file, index in rows:
        location = {"file": file, "row": index}
        for name in ("symbol", "timeframe", "source"):
            value = record.get(name)
            if isinstance(value, str) and value:
                {"symbol": symbols, "timeframe": timeframes, "source": sources}[name].add(value)
        stamp = record.get("timestamp")
        try:
            if not isinstance(stamp, datetime) or stamp.tzinfo is None or stamp.utcoffset() is None:
                raise ValueError("timestamp is absent or timezone-naive")
            stamp = stamp.astimezone(UTC)
            observed_times.append(stamp)
            symbol, timeframe, source = (
                record.get("symbol"),
                record.get("timeframe"),
                record.get("source"),
            )
            if all(isinstance(x, str) and x for x in (symbol, timeframe, source)):
                valid_time_groups[(symbol, timeframe, source)].append((stamp, file, index))
                duplicate_map[(symbol, timeframe, stamp, source)].append((file, index))
        except (ValueError, TypeError) as exc:
            confirmed.append({"code": "invalid_timestamp", **location, "detail": str(exc)})
            exclusions.append(
                {**location, "reason": "timestamp cannot be used for ordering or gap checks"}
            )
        try:
            validate_bar(record)
        except (ValueError, TypeError, OverflowError) as exc:
            confirmed.append({"code": "invalid_bar", **location, "detail": str(exc)})
        for name in optional_nulls:
            if record.get(name) is None:
                optional_nulls[name] += 1
        if "spread" in record:
            spread_field_seen = True
            spread = record.get("spread")
            if spread is not None and (
                not isinstance(spread, int | float) or not math.isfinite(spread) or spread < 0
            ):
                confirmed.append({"code": "invalid_spread", **location, "value": str(spread)})

    for key, locations in duplicate_map.items():
        if len(locations) > 1:
            confirmed.append(
                {
                    "code": "duplicate_bar",
                    "symbol": key[0],
                    "timeframe": key[1],
                    "timestamp_utc": _iso(key[2]),
                    "source": key[3],
                    "records": [{"file": f, "row": i} for f, i in locations],
                }
            )

    for (symbol, timeframe, source), items in valid_time_groups.items():
        previous: datetime | None = None
        previous_file: str | None = None
        delta = _timeframe_delta(timeframe)
        for stamp, file, index in items:
            if previous is not None:
                if stamp < previous:
                    confirmed.append(
                        {
                            "code": "non_monotonic_timestamp",
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "source": source,
                            "previous_utc": _iso(previous),
                            "timestamp_utc": _iso(stamp),
                            "file": file,
                            "row": index,
                        }
                    )
                elif delta and stamp - previous > delta:
                    gap_start, gap_end = previous + delta, stamp
                    gap = {
                        "code": "bar_gap",
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "source": source,
                        "after_utc": _iso(previous),
                        "before_utc": _iso(stamp),
                        "missing_intervals_estimate": max(1, int((stamp - previous) / delta) - 1),
                        "file_before": previous_file,
                        "file_after": file,
                        "crosses_parquet_partition": previous_file != file,
                        "source_file_boundary_status": (
                            "unknown: source filename is not stored per bar"
                        ),
                        "collection_interruption_evidence": "not available in bar records",
                    }
                    matching = next(
                        (c for c in closures if c["start"] <= gap_start and c["end"] >= gap_end),
                        None,
                    )
                    if matching:
                        expected.append(
                            {
                                **gap,
                                "classification": "expected_closure",
                                "evidence": matching["evidence"],
                            }
                        )
                    else:
                        unresolved.append(
                            {
                                **gap,
                                "classification": "unresolved",
                                "detail": "No session or closure evidence covers this interval.",
                            }
                        )
                previous = stamp
                previous_file = file
            else:
                previous = stamp
                previous_file = file
        if delta is None:
            unresolved.append(
                {
                    "code": "gap_check_unsupported_timeframe",
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "source": source,
                }
            )

    if manifest:
        status = manifest.get("status", "unknown")
        if status != "complete":
            confirmed.append({"code": "ingestion_not_complete", "status": status})
        if manifest.get("schema_version") != SCHEMA_VERSION:
            confirmed.append(
                {
                    "code": "manifest_schema_version",
                    "expected": SCHEMA_VERSION,
                    "actual": manifest.get("schema_version"),
                }
            )
        ingested = manifest.get("ingestion_time_utc")
        try:
            ingested_dt = datetime.fromisoformat(ingested.replace("Z", "+00:00"))
            if ingested_dt.tzinfo is None:
                raise ValueError
            age_hours = (now_utc - ingested_dt.astimezone(UTC)).total_seconds() / 3600
            if age_hours > stale_after_hours:
                unresolved.append(
                    {
                        "code": "ingestion_stale",
                        "age_hours": round(age_hours, 3),
                        "threshold_hours": stale_after_hours,
                    }
                )
        except (AttributeError, ValueError, TypeError):
            unresolved.append(
                {
                    "code": "ingestion_time_unknown",
                    "detail": "Manifest ingestion time is missing or invalid.",
                }
            )
        if manifest.get("row_count") != len(rows):
            confirmed.append(
                {
                    "code": "manifest_row_count_mismatch",
                    "manifest": manifest.get("row_count"),
                    "readable_rows": len(rows),
                }
            )
        if manifest.get("validation_errors"):
            confirmed.append(
                {
                    "code": "manifest_validation_errors",
                    "count": len(manifest["validation_errors"]),
                    "errors": manifest["validation_errors"],
                }
            )
        if manifest.get("source") is None:
            unresolved.append({"code": "source_provenance_unknown"})
        declared_range = manifest.get("date_range_utc") or {}
        actual_range = {
            "start": _iso(min(observed_times)) if observed_times else None,
            "end": _iso(max(observed_times)) if observed_times else None,
        }
        if (
            declared_range.get("start") != actual_range["start"]
            or declared_range.get("end") != actual_range["end"]
        ):
            confirmed.append(
                {
                    "code": "manifest_date_range_mismatch",
                    "manifest": declared_range,
                    "observed": actual_range,
                }
            )
    else:
        unresolved.append({"code": "dataset_manifest_unknown"})

    if rows:
        # The canonical schema has no spread field, so no spread threshold is inferred.
        spread_check = (
            "available; only invalid negative/non-finite values checked"
            if spread_field_seen
            else "not applicable: spread absent from canonical records"
        )
    else:
        spread_check = "not assessed: no records"
    return {
        "report_version": "1.0",
        "generated_at_utc": _iso(now_utc),
        "dataset_root": str(root),
        "manifest": manifest,
        "dataset": {
            "files": len(files),
            "readable_rows": len(rows),
            "observed_date_range_utc": {
                "start": _iso(min(observed_times)) if observed_times else None,
                "end": _iso(max(observed_times)) if observed_times else None,
            },
            "symbols": sorted(symbols),
            "timeframes": sorted(timeframes),
            "sources": sorted(sources),
            "optional_null_counts": optional_nulls,
            "spread_check": spread_check,
            "file_boundary_provenance": (
                "per-row source file is not recorded by canonical schema; "
                "source-file boundary attribution is limited"
            ),
        },
        "confirmed_problems": confirmed,
        "expected_closures": expected,
        "unresolved_gaps": unresolved,
        "exclusions": exclusions,
    }


def render_report(report: dict[str, Any]) -> str:
    data = report["dataset"]
    date_range = data["observed_date_range_utc"]
    lines = [
        "Johanna market-data quality report",
        f"Generated: {report['generated_at_utc']}",
        f"Dataset: {report['dataset_root']}",
        f"Files / readable rows: {data['files']} / {data['readable_rows']}",
        f"Symbols: {data['symbols']}; timeframes: {data['timeframes']}; sources: {data['sources']}",
        f"Observed UTC range: {date_range['start']} to {date_range['end']}",
        f"Ingestion status: {(report.get('manifest') or {}).get('status', 'unknown')}",
        f"Spread check: {data['spread_check']}",
        "",
        f"Confirmed problems ({len(report['confirmed_problems'])}):",
    ]
    lines.extend(f"- {x['code']}: {x.get('detail', x)}" for x in report["confirmed_problems"])
    lines.append(f"Expected closures ({len(report['expected_closures'])}):")
    lines.extend(
        f"- {x['after_utc']} → {x['before_utc']}: {x['evidence']}"
        for x in report["expected_closures"]
    )
    lines.append(f"Unresolved gaps / unknowns ({len(report['unresolved_gaps'])}):")
    lines.extend(f"- {x['code']}: {x.get('detail', x)}" for x in report["unresolved_gaps"])
    lines.append(f"Excluded from specific checks ({len(report['exclusions'])}):")
    lines.extend(f"- {x}" for x in report["exclusions"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="data", help="Parquet store root (contains clean/)")
    parser.add_argument(
        "--json-out", default="data/quality_report.json", help="Machine-readable JSON output path"
    )
    parser.add_argument("--stale-after-hours", type=float, default=48)
    parser.add_argument(
        "--closure-evidence", help="Optional JSON list of evidenced UTC closure intervals"
    )
    args = parser.parse_args(argv)
    evidence = (
        json.loads(Path(args.closure_evidence).read_text(encoding="utf-8"))
        if args.closure_evidence
        else None
    )
    report = build_quality_report(
        args.root, stale_after_hours=args.stale_after_hours, closure_evidence=evidence
    )
    print(render_report(report))
    output = Path(args.json_out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"JSON report written to {output}")
    return 1 if report["confirmed_problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
