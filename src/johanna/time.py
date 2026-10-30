"""Timezone-safe timestamp utilities for Johanna.

Internal timestamps are timezone-aware UTC ``datetime`` objects. Naive local
timestamps require an explicit IANA source timezone; ambiguous times require an
explicit ``fold`` value, and nonexistent times are rejected.

The canonical bar timestamp convention is bar-open time, as specified in the
research plan. A data source's convention must be established and declared as
``bar_open`` or ``bar_close`` before its timestamps are interpreted. This module
does not infer a source convention or shift close timestamps to open timestamps.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC as _UTC
from datetime import datetime
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = _UTC


class AmbiguousLocalTimeError(ValueError):
    """Raised when a local clock time occurs twice during a DST transition."""


class NonexistentLocalTimeError(ValueError):
    """Raised when a local clock time is skipped during a DST transition."""


class BarTimestampConvention(StrEnum):
    """Explicit timestamp meaning for market bars reported by a source."""

    BAR_OPEN = "bar_open"
    BAR_CLOSE = "bar_close"


def get_timezone(name: str) -> ZoneInfo:
    """Return an IANA timezone, failing clearly if the name is invalid."""
    if not name or not name.strip():
        raise ValueError("Timezone name must be a non-empty IANA timezone identifier")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"Unknown IANA timezone: {name!r}") from exc


def parse_bar_timestamp_convention(value: str) -> BarTimestampConvention:
    """Parse a source-declared bar convention; unknown or missing values are errors."""
    try:
        return BarTimestampConvention(value)
    except ValueError as exc:
        raise ValueError(
            "Bar timestamp convention must be explicitly declared as 'bar_open' or 'bar_close'"
        ) from exc


def _require_aware(value: datetime) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Timestamp must be timezone-aware")


def _localize(value: datetime, zone: ZoneInfo, fold: int | None) -> datetime:
    """Attach a zone to a naive wall time while detecting DST gaps and folds."""
    if fold not in (None, 0, 1):
        raise ValueError("fold must be 0, 1, or None")

    valid: dict[datetime, int] = {}
    for candidate_fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=candidate_fold)
        utc_value = candidate.astimezone(UTC)
        if utc_value.astimezone(zone).replace(tzinfo=None) == value:
            valid[utc_value] = candidate_fold

    if not valid:
        raise NonexistentLocalTimeError(
            f"Local time {value!s} does not exist in timezone {zone.key!r}"
        )
    if len(valid) > 1:
        if fold is None:
            raise AmbiguousLocalTimeError(
                f"Local time {value!s} is ambiguous in timezone {zone.key!r}; "
                "specify fold=0 or fold=1"
            )
        selected = value.replace(tzinfo=zone, fold=fold)
        return selected
    if fold is not None:
        only_utc, only_fold = next(iter(valid.items()))
        selected = value.replace(tzinfo=zone, fold=fold)
        if selected.astimezone(UTC) != only_utc:
            raise ValueError("fold=1 is only meaningful for an ambiguous local time")
        # Either fold identifies the same instant on an ordinary local time.
        return value.replace(tzinfo=zone, fold=only_fold)
    return value.replace(tzinfo=zone, fold=next(iter(valid.values())))


def to_utc(
    value: datetime,
    source_timezone: str | None = None,
    *,
    fold: int | None = None,
) -> datetime:
    """Normalize an aware timestamp, or localize a naive timestamp, to UTC.

    ``source_timezone`` is required for naive timestamps and uses IANA names such
    as ``America/New_York``. For repeated DST wall times pass ``fold=0`` for the
    earlier occurrence or ``fold=1`` for the later occurrence.
    """
    if value.tzinfo is not None and value.utcoffset() is not None:
        if source_timezone is not None:
            raise ValueError("source_timezone is only valid for a naive local timestamp")
        if fold is not None:
            raise ValueError("fold is only valid for an ambiguous naive local timestamp")
        return value.astimezone(UTC)

    if source_timezone is None:
        raise ValueError("Naive timestamp rejected; provide an explicit source_timezone")
    zone = get_timezone(source_timezone)
    return _localize(value, zone, fold).astimezone(UTC)


def from_utc(value: datetime, target_timezone: str) -> datetime:
    """Convert an aware timestamp to an explicitly selected IANA timezone."""
    _require_aware(value)
    return value.astimezone(get_timezone(target_timezone))


def strictly_increasing_utc(values: Iterable[datetime]) -> tuple[datetime, ...]:
    """Normalize aware timestamps to UTC and reject duplicates or regressions."""
    normalized: list[datetime] = []
    previous: datetime | None = None
    for index, value in enumerate(values):
        current = to_utc(value)
        if previous is not None and current <= previous:
            raise ValueError(
                f"Timestamps must be strictly increasing; item {index} is not after its predecessor"
            )
        normalized.append(current)
        previous = current
    return tuple(normalized)
