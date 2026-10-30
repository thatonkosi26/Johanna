"""Normalized economic-calendar contract, cache, and gated weekly JSON client.

The Forex Factory client only requests its publicly linked JSON export, never
the calendar HTML. Live use is disabled pending explicit permission for local
copying and caching.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from johanna.time import to_utc

SCHEMA_VERSION = 1


class CalendarAdapterError(RuntimeError):
    """Clear error from calendar normalization, provider, or snapshot storage."""


class Freshness(StrEnum):
    FRESH = "fresh"
    STALE = "stale"


@dataclass(frozen=True)
class CalendarEvent:
    """Normalized event; missing values remain ``None`` and are never inferred."""

    event_name: str
    currency: str | None
    scheduled_at_utc: datetime | None
    impact: str | None
    actual: str | None
    forecast: str | None
    previous: str | None
    source: str
    retrieved_at_utc: datetime
    freshness: Freshness
    source_event_id: str | None = None


@dataclass(frozen=True)
class CalendarSnapshot:
    week_start: date
    retrieved_at_utc: datetime
    source: str
    events: tuple[CalendarEvent, ...]
    freshness: Freshness
    etag: str | None = None
    last_modified: str | None = None


@dataclass(frozen=True)
class CalendarFetchResult:
    events: Sequence[Mapping[str, Any]]
    etag: str | None = None
    last_modified: str | None = None
    not_modified: bool = False


class EconomicCalendarProvider(Protocol):
    """Provider contract. Naive event times require a declared IANA timezone."""

    source: str
    source_timezone: str | None

    def fetch_week(
        self,
        week_start: date,
        *,
        timeout: float,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> Sequence[Mapping[str, Any]] | CalendarFetchResult: ...


@dataclass(frozen=True)
class _HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


def _http_get(url: str, *, headers: Mapping[str, str], timeout: float) -> _HTTPResponse:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return _HTTPResponse(response.status, response.headers, response.read(2_000_001))
    except HTTPError as response:
        return _HTTPResponse(response.code, response.headers, response.read(2_000_001))
    except (URLError, TimeoutError, OSError) as exc:
        raise CalendarAdapterError("Forex Factory weekly export request failed") from exc


class ForexFactoryWeeklyJsonProvider:
    """Client for the publicly linked weekly JSON file, never the HTML page.

    Live access stays disabled unless the caller explicitly confirms written
    permission for automated retrieval and local caching. It does not retry
    403/429 responses or attempt to bypass either response.
    """

    source = "Forex Factory weekly JSON export"
    source_timezone = None
    URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

    def __init__(
        self,
        *,
        authorized_for_local_cache: bool = False,
        transport: Callable[..., _HTTPResponse] | None = None,
    ) -> None:
        self._authorized = authorized_for_local_cache
        self._transport = transport or _http_get

    def fetch_week(
        self,
        week_start: date,
        *,
        timeout: float,
        etag: str | None = None,
        last_modified: str | None = None,
    ) -> CalendarFetchResult:
        """Fetch the feed's current week; ``week_start`` labels the cache only."""
        del week_start  # The published endpoint defines the current week itself.
        if not self._authorized:
            raise CalendarAdapterError(
                "Forex Factory fetching is disabled until written permission for "
                "local caching is confirmed"
            )
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        headers = {
            "Accept": "application/json",
            "User-Agent": "Johanna-EconomicCalendar/0.1 (weekly export client)",
        }
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        try:
            response = self._transport(self.URL, headers=headers, timeout=timeout)
        except CalendarAdapterError:
            raise
        except (OSError, TimeoutError) as exc:
            raise CalendarAdapterError("Forex Factory weekly export request failed") from exc

        if response.status == 304:
            return CalendarFetchResult(
                events=(),
                etag=response.headers.get("ETag") or etag,
                last_modified=response.headers.get("Last-Modified") or last_modified,
                not_modified=True,
            )
        if response.status in (403, 429):
            raise CalendarAdapterError(
                f"Forex Factory weekly export returned HTTP {response.status}; request stopped"
            )
        if response.status != 200:
            raise CalendarAdapterError(
                f"Forex Factory weekly export returned HTTP {response.status}"
            )
        if len(response.body) > 2_000_000:
            raise CalendarAdapterError("Forex Factory weekly export exceeded the 2 MB safety limit")
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise CalendarAdapterError(
                "Forex Factory weekly export is not valid UTF-8 JSON"
            ) from exc
        if not isinstance(payload, list):
            raise CalendarAdapterError("Forex Factory weekly export must be a JSON array")

        rows: list[Mapping[str, Any]] = []
        for row in payload:
            if not isinstance(row, dict):
                raise CalendarAdapterError("Forex Factory weekly export contains a malformed event")
            title = row.get("title")
            if not isinstance(title, str) or not title.strip():
                raise CalendarAdapterError("Forex Factory event is missing its title")
            date_value = row.get("date")
            if date_value is not None and not isinstance(date_value, str):
                raise CalendarAdapterError(
                    "Forex Factory event date must be an ISO timestamp or null"
                )
            rows.append(
                {
                    "event_name": title,
                    "currency": row.get("country"),
                    "scheduled_at": date_value,
                    "impact": row.get("impact"),
                    "actual": row.get("actual"),
                    "forecast": row.get("forecast"),
                    "previous": row.get("previous"),
                    "source_event_id": row.get("id"),
                }
            )
        return CalendarFetchResult(
            events=rows,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )


def _aware_utc(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CalendarAdapterError(f"{label} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, str | int | float):
        return str(value)
    raise CalendarAdapterError("Calendar event values must be strings or numbers")


def normalize_event(
    record: Mapping[str, Any],
    *,
    source: str,
    retrieved_at: datetime,
    source_timezone: str | None = None,
) -> CalendarEvent:
    """Validate a provider row and normalize an established event time to UTC."""
    if not isinstance(record, Mapping):
        raise CalendarAdapterError("Calendar event must be an object")
    name = record.get("event_name")
    if not isinstance(name, str) or not name.strip():
        raise CalendarAdapterError("Calendar event is missing a non-empty event_name")
    if not isinstance(source, str) or not source.strip():
        raise CalendarAdapterError("Calendar provider must declare a non-empty source")
    retrieved_utc = _aware_utc(retrieved_at, "retrieved_at")

    scheduled_value = record.get("scheduled_at")
    scheduled_utc: datetime | None = None
    if scheduled_value not in (None, ""):
        if isinstance(scheduled_value, datetime):
            parsed = scheduled_value
        elif isinstance(scheduled_value, str):
            try:
                parsed = datetime.fromisoformat(scheduled_value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise CalendarAdapterError("Calendar event has an invalid scheduled_at") from exc
        else:
            raise CalendarAdapterError("scheduled_at must be an ISO timestamp or null")
        try:
            timezone_for_naive_value = (
                source_timezone if parsed.tzinfo is None or parsed.utcoffset() is None else None
            )
            scheduled_utc = to_utc(parsed, source_timezone=timezone_for_naive_value)
        except ValueError as exc:
            raise CalendarAdapterError(
                "Calendar event time is invalid or naive without a declared source timezone"
            ) from exc

    currency = _optional_text(record.get("currency"))
    if currency is not None:
        currency = currency.strip().upper()
    return CalendarEvent(
        event_name=name.strip(),
        currency=currency,
        scheduled_at_utc=scheduled_utc,
        impact=_optional_text(record.get("impact")),
        actual=_optional_text(record.get("actual")),
        forecast=_optional_text(record.get("forecast")),
        previous=_optional_text(record.get("previous")),
        source=source.strip(),
        retrieved_at_utc=retrieved_utc,
        freshness=Freshness.FRESH,
        source_event_id=_optional_text(record.get("source_event_id")),
    )


def deduplicate_events(events: Sequence[CalendarEvent]) -> tuple[CalendarEvent, ...]:
    """Remove repeated provider rows deterministically and sort chronologically."""
    unique: dict[str, CalendarEvent] = {}
    for event in events:
        if event.source_event_id is not None:
            identity = f"id:{event.source}:{event.source_event_id}"
        else:
            identity = "|".join(
                (
                    event.source,
                    " ".join(event.event_name.casefold().split()),
                    event.currency or "",
                    event.scheduled_at_utc.isoformat()
                    if event.scheduled_at_utc
                    else "unknown-time",
                )
            )
        unique.setdefault(identity, event)
    return tuple(
        sorted(
            unique.values(),
            key=lambda event: (
                event.scheduled_at_utc is None,
                event.scheduled_at_utc or datetime.max.replace(tzinfo=UTC),
                event.currency or "",
                event.event_name.casefold(),
            ),
        )
    )


class WeeklySnapshotCache:
    """Atomic JSON snapshots, one file per explicitly requested week start."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def path_for(self, week_start: date) -> Path:
        return self.directory / f"week-{week_start.isoformat()}.json"

    def write(self, snapshot: CalendarSnapshot) -> Path:
        path = self.path_for(snapshot.week_start)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "week_start": snapshot.week_start.isoformat(),
            "retrieved_at_utc": _iso(snapshot.retrieved_at_utc),
            "source": snapshot.source,
            "etag": snapshot.etag,
            "last_modified": snapshot.last_modified,
            "events": [_event_dict(event) for event in snapshot.events],
        }
        temp: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
            ) as stream:
                temp = Path(stream.name)
                json.dump(payload, stream, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        return path

    def read(self, week_start: date) -> CalendarSnapshot | None:
        path = self.path_for(week_start)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("unsupported schema version")
            retrieved = datetime.fromisoformat(payload["retrieved_at_utc"].replace("Z", "+00:00"))
            source = payload["source"]
            events = tuple(_event_from_dict(row) for row in payload["events"])
            return CalendarSnapshot(
                week_start=date.fromisoformat(payload["week_start"]),
                retrieved_at_utc=_aware_utc(retrieved, "snapshot retrieved_at"),
                source=source,
                events=events,
                freshness=Freshness.FRESH,
                etag=payload.get("etag"),
                last_modified=payload.get("last_modified"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CalendarAdapterError(f"Invalid calendar snapshot cache: {path.name}") from exc


class CachedEconomicCalendar:
    """Cache-aware facade around an explicitly injected, authorized provider."""

    def __init__(
        self,
        provider: EconomicCalendarProvider,
        cache: WeeklySnapshotCache,
        *,
        max_age: timedelta = timedelta(hours=6),
        min_fetch_interval: timedelta = timedelta(hours=6),
        timeout: float = 10.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_age.total_seconds() < 0 or min_fetch_interval.total_seconds() < 0 or timeout <= 0:
            raise ValueError("cache intervals must be non-negative and timeout must be positive")
        self.provider = provider
        self.cache = cache
        self.max_age = max_age
        self.min_fetch_interval = min_fetch_interval
        self.timeout = timeout
        self._clock = clock or (lambda: datetime.now(UTC))

    def get_week(self, week_start: date, *, force_refresh: bool = False) -> CalendarSnapshot:
        """Get fresh data or return an explicitly stale cached snapshot on failure."""
        now = _aware_utc(self._clock(), "clock")
        cached = self.cache.read(week_start)
        if cached is not None:
            age = now - cached.retrieved_at_utc
            is_fresh = timedelta(0) <= age <= self.max_age
            if age >= timedelta(0) and age < self.min_fetch_interval:
                return _mark_freshness(cached, Freshness.FRESH if is_fresh else Freshness.STALE)
            if is_fresh and not force_refresh:
                return _mark_freshness(cached, Freshness.FRESH)

        try:
            raw_result = self.provider.fetch_week(
                week_start,
                timeout=self.timeout,
                etag=cached.etag if cached is not None else None,
                last_modified=cached.last_modified if cached is not None else None,
            )
            if isinstance(raw_result, CalendarFetchResult) and raw_result.not_modified:
                if cached is None:
                    raise CalendarAdapterError("Provider returned not-modified without a cache")
                retrieved = _aware_utc(self._clock(), "clock")
                refreshed_events = tuple(
                    replace(event, retrieved_at_utc=retrieved, freshness=Freshness.FRESH)
                    for event in cached.events
                )
                snapshot = replace(
                    cached,
                    retrieved_at_utc=retrieved,
                    events=refreshed_events,
                    freshness=Freshness.FRESH,
                    etag=raw_result.etag or cached.etag,
                    last_modified=raw_result.last_modified or cached.last_modified,
                )
                self.cache.write(snapshot)
                return snapshot
            raw_events = (
                raw_result.events if isinstance(raw_result, CalendarFetchResult) else raw_result
            )
            if not isinstance(raw_events, Sequence) or isinstance(raw_events, str | bytes):
                raise CalendarAdapterError(
                    "Calendar provider response must be a sequence of events"
                )
            retrieved = _aware_utc(self._clock(), "clock")
            normalized = [
                normalize_event(
                    row,
                    source=self.provider.source,
                    retrieved_at=retrieved,
                    source_timezone=self.provider.source_timezone,
                )
                for row in raw_events
            ]
            snapshot = CalendarSnapshot(
                week_start=week_start,
                retrieved_at_utc=retrieved,
                source=self.provider.source,
                events=deduplicate_events(normalized),
                freshness=Freshness.FRESH,
                etag=raw_result.etag if isinstance(raw_result, CalendarFetchResult) else None,
                last_modified=(
                    raw_result.last_modified
                    if isinstance(raw_result, CalendarFetchResult)
                    else None
                ),
            )
            self.cache.write(snapshot)
            return snapshot
        except (TimeoutError, OSError, CalendarAdapterError, ValueError) as exc:
            if cached is not None:
                return _mark_freshness(cached, Freshness.STALE)
            raise CalendarAdapterError(
                "Calendar provider failed and no cached snapshot exists"
            ) from exc


def _mark_freshness(snapshot: CalendarSnapshot, freshness: Freshness) -> CalendarSnapshot:
    return replace(
        snapshot,
        freshness=freshness,
        events=tuple(replace(event, freshness=freshness) for event in snapshot.events),
    )


def _iso(value: datetime) -> str:
    return _aware_utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _event_dict(event: CalendarEvent) -> dict[str, Any]:
    return {
        "event_name": event.event_name,
        "currency": event.currency,
        "scheduled_at_utc": _iso(event.scheduled_at_utc) if event.scheduled_at_utc else None,
        "impact": event.impact,
        "actual": event.actual,
        "forecast": event.forecast,
        "previous": event.previous,
        "source": event.source,
        "retrieved_at_utc": _iso(event.retrieved_at_utc),
        "freshness": event.freshness.value,
        "source_event_id": event.source_event_id,
    }


def _event_from_dict(row: Mapping[str, Any]) -> CalendarEvent:
    if not isinstance(row, Mapping):
        raise ValueError("event row must be an object")
    scheduled_raw = row.get("scheduled_at_utc")
    scheduled = (
        datetime.fromisoformat(scheduled_raw.replace("Z", "+00:00")) if scheduled_raw else None
    )
    return CalendarEvent(
        event_name=row["event_name"],
        currency=row.get("currency"),
        scheduled_at_utc=_aware_utc(scheduled, "scheduled_at_utc") if scheduled else None,
        impact=row.get("impact"),
        actual=row.get("actual"),
        forecast=row.get("forecast"),
        previous=row.get("previous"),
        source=row["source"],
        retrieved_at_utc=_aware_utc(
            datetime.fromisoformat(row["retrieved_at_utc"].replace("Z", "+00:00")),
            "retrieved_at_utc",
        ),
        freshness=Freshness(row["freshness"]),
        source_event_id=row.get("source_event_id"),
    )
