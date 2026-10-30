from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from johanna.adapters.economic_calendar import (
    CachedEconomicCalendar,
    CalendarAdapterError,
    CalendarFetchResult,
    CalendarSnapshot,
    ForexFactoryWeeklyJsonProvider,
    Freshness,
    WeeklySnapshotCache,
    normalize_event,
)

NOW = datetime(2026, 10, 25, 12, tzinfo=UTC)
WEEK = date(2026, 10, 25)


class FakeCalendarProvider:
    source = "synthetic-test-provider"
    source_timezone = "America/New_York"

    def __init__(self, rows: Any = None, error: Exception | None = None):
        self.rows = rows if rows is not None else []
        self.error = error
        self.calls = 0
        self.timeouts: list[float] = []

    def fetch_week(
        self,
        week_start: date,
        *,
        timeout: float,
        etag: str | None = None,
        last_modified: str | None = None,
    ):
        assert week_start == WEEK
        self.calls += 1
        self.timeouts.append(timeout)
        if self.error:
            raise self.error
        return self.rows


def synthetic_row(**overrides: Any) -> dict[str, Any]:
    """Synthetic fixture only; values do not represent a real event or release."""
    return {
        "event_name": "Synthetic CPI Sample",
        "currency": "USD",
        "scheduled_at": "2026-10-26T08:30:00",
        "impact": "high",
        "actual": None,
        "forecast": "2.1%",
        "previous": "2.0%",
        **overrides,
    }


def test_event_normalizes_explicit_source_timezone_to_utc() -> None:
    event = normalize_event(
        synthetic_row(),
        source="synthetic-test-provider",
        retrieved_at=NOW,
        source_timezone="America/New_York",
    )

    assert event.scheduled_at_utc == datetime(2026, 10, 26, 12, 30, tzinfo=UTC)
    assert event.retrieved_at_utc == NOW
    assert event.currency == "USD"
    assert event.actual is None


def test_offset_aware_event_timestamp_is_utc_normalized() -> None:
    event = normalize_event(
        synthetic_row(scheduled_at="2026-10-26T08:30:00-04:00"),
        source="synthetic-test-provider",
        retrieved_at=NOW,
        source_timezone="America/New_York",
    )

    assert event.scheduled_at_utc == datetime(2026, 10, 26, 12, 30, tzinfo=UTC)


def test_naive_event_time_without_declared_timezone_is_rejected() -> None:
    with pytest.raises(CalendarAdapterError, match="without a declared source timezone"):
        normalize_event(
            synthetic_row(),
            source="synthetic-test-provider",
            retrieved_at=NOW,
        )


@pytest.mark.parametrize(
    "row",
    [
        {"currency": "USD", "scheduled_at": "2026-10-26T12:30:00Z"},
        synthetic_row(scheduled_at="not-a-timestamp"),
        synthetic_row(actual={"unexpected": "object"}),
    ],
)
def test_malformed_events_are_rejected(row: dict[str, Any]) -> None:
    with pytest.raises(CalendarAdapterError):
        normalize_event(
            row,
            source="synthetic-test-provider",
            retrieved_at=NOW,
            source_timezone="America/New_York",
        )


def test_cached_weekly_snapshot_deduplicates_and_reuses_fresh_data(tmp_path: Path) -> None:
    row = synthetic_row()
    provider = FakeCalendarProvider([row, dict(row)])
    calendar = CachedEconomicCalendar(
        provider,
        WeeklySnapshotCache(tmp_path),
        clock=lambda: NOW,
    )

    first = calendar.get_week(WEEK)
    second = calendar.get_week(WEEK)

    assert provider.calls == 1
    assert provider.timeouts == [10.0]
    assert len(first.events) == 1
    assert second.events == first.events
    assert first.freshness == Freshness.FRESH
    assert WeeklySnapshotCache(tmp_path).path_for(WEEK).is_file()


def test_stale_cache_is_returned_with_stale_status_when_provider_times_out(tmp_path: Path) -> None:
    cache = WeeklySnapshotCache(tmp_path)
    fetched_at = NOW - timedelta(hours=7)
    event = normalize_event(
        synthetic_row(),
        source="synthetic-test-provider",
        retrieved_at=fetched_at,
        source_timezone="America/New_York",
    )
    cache.write(
        CalendarSnapshot(
            week_start=WEEK,
            retrieved_at_utc=fetched_at,
            source="synthetic-test-provider",
            events=(event,),
            freshness=Freshness.FRESH,
        )
    )
    provider = FakeCalendarProvider(error=TimeoutError("synthetic timeout"))
    calendar = CachedEconomicCalendar(provider, cache, clock=lambda: NOW)

    result = calendar.get_week(WEEK)

    assert result.freshness == Freshness.STALE
    assert result.events[0].freshness == Freshness.STALE
    assert provider.calls == 1


def test_provider_error_without_cache_raises_clear_error(tmp_path: Path) -> None:
    calendar = CachedEconomicCalendar(
        FakeCalendarProvider(error=OSError("synthetic offline error")),
        WeeklySnapshotCache(tmp_path),
        clock=lambda: NOW,
    )

    with pytest.raises(CalendarAdapterError, match="no cached snapshot exists"):
        calendar.get_week(WEEK)


class FakeHTTPResponse:
    def __init__(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None):
        self.status = status
        self.body = body
        self.headers = headers or {}


def test_forex_factory_client_maps_mocked_weekly_json_only() -> None:
    """Fixture content is synthetic and does not represent a real calendar event."""
    raw = (
        b'[{"title":"Synthetic CPI Sample","country":"USD",'
        b'"date":"2026-10-26T08:30:00-04:00","impact":"High",'
        b'"actual":null,"forecast":"2.1%","previous":"2.0%"}]'
    )
    calls: list[tuple[str, dict[str, str], float]] = []

    def transport(url: str, *, headers: dict[str, str], timeout: float) -> FakeHTTPResponse:
        calls.append((url, headers, timeout))
        return FakeHTTPResponse(200, raw, {"ETag": '"synthetic-etag"'})

    provider = ForexFactoryWeeklyJsonProvider(
        authorized_for_local_cache=True,
        transport=transport,
    )
    result = provider.fetch_week(WEEK, timeout=4.0)

    assert calls[0][0] == provider.URL
    assert calls[0][1]["Accept"] == "application/json"
    assert calls[0][2] == 4.0
    assert isinstance(result, CalendarFetchResult)
    assert result.events[0]["event_name"] == "Synthetic CPI Sample"
    assert result.events[0]["actual"] is None
    assert result.etag == '"synthetic-etag"'


def test_forex_factory_client_is_disabled_without_permission() -> None:
    provider = ForexFactoryWeeklyJsonProvider(
        transport=lambda *_args, **_kwargs: pytest.fail("must not make an HTTP request")
    )
    with pytest.raises(CalendarAdapterError, match="written permission"):
        provider.fetch_week(WEEK, timeout=5.0)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (b"not json", "valid UTF-8 JSON"),
        (b'{"events": []}', "JSON array"),
        (b'[{"country":"USD"}]', "missing its title"),
        (
            b'[{"title":"Synthetic","country":"USD","date":"2026-10-26T08:30:00"}]',
            "without a declared source timezone",
        ),
    ],
)
def test_forex_factory_malformed_feed_is_rejected(
    body: bytes, message: str, tmp_path: Path
) -> None:
    provider = ForexFactoryWeeklyJsonProvider(
        authorized_for_local_cache=True,
        transport=lambda *_args, **_kwargs: FakeHTTPResponse(200, body),
    )
    if message == "without a declared source timezone":
        calendar = CachedEconomicCalendar(
            provider,
            WeeklySnapshotCache(tmp_path),
            clock=lambda: NOW,
        )
        with pytest.raises(CalendarAdapterError, match="no cached snapshot exists"):
            calendar.get_week(WEEK)
    else:
        with pytest.raises(CalendarAdapterError, match=message):
            provider.fetch_week(WEEK, timeout=5.0)


def test_forex_factory_timeout_is_reported_without_retry() -> None:
    calls = 0

    def timeout_transport(*_args: Any, **_kwargs: Any) -> FakeHTTPResponse:
        nonlocal calls
        calls += 1
        raise TimeoutError("synthetic timeout")

    provider = ForexFactoryWeeklyJsonProvider(
        authorized_for_local_cache=True,
        transport=timeout_transport,
    )
    with pytest.raises(CalendarAdapterError, match="request failed"):
        provider.fetch_week(WEEK, timeout=0.01)
    assert calls == 1


@pytest.mark.parametrize("status", [403, 429])
def test_forex_factory_does_not_retry_or_bypass_denial(status: int) -> None:
    calls: list[str] = []
    provider = ForexFactoryWeeklyJsonProvider(
        authorized_for_local_cache=True,
        transport=lambda url, **_kwargs: calls.append(url) or FakeHTTPResponse(status),
    )
    with pytest.raises(CalendarAdapterError, match=f"HTTP {status}"):
        provider.fetch_week(WEEK, timeout=5.0)
    assert calls == [provider.URL]


def test_cached_provider_uses_http_validators_and_304(tmp_path: Path) -> None:
    now = [NOW]
    calls: list[dict[str, str]] = []
    replies = [
        FakeHTTPResponse(
            200,
            (
                b'[{"title":"Synthetic CPI Sample","country":"USD",'
                b'"date":"2026-10-26T08:30:00-04:00"}]'
            ),
            {"ETag": '"v1"', "Last-Modified": "Sun, 25 Oct 2026 12:00:00 GMT"},
        ),
        FakeHTTPResponse(304, headers={"ETag": '"v1"'}),
    ]

    def transport(_url: str, *, headers: dict[str, str], timeout: float) -> FakeHTTPResponse:
        calls.append(headers)
        return replies.pop(0)

    provider = ForexFactoryWeeklyJsonProvider(
        authorized_for_local_cache=True,
        transport=transport,
    )
    calendar = CachedEconomicCalendar(
        provider,
        WeeklySnapshotCache(tmp_path),
        clock=lambda: now[0],
    )
    original = calendar.get_week(WEEK)
    now[0] += timedelta(hours=7)
    revalidated = calendar.get_week(WEEK)

    assert calls[1]["If-None-Match"] == '"v1"'
    assert calls[1]["If-Modified-Since"] == "Sun, 25 Oct 2026 12:00:00 GMT"
    assert revalidated.freshness == Freshness.FRESH
    assert revalidated.retrieved_at_utc == now[0]
    assert revalidated.events[0].event_name == original.events[0].event_name
