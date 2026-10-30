from datetime import UTC, date, datetime
from decimal import Decimal
from urllib.parse import parse_qs, urlsplit

import pytest

from johanna.adapters.fred import FREDAdapter, FREDError, _HTTPResponse


def _responses() -> list[_HTTPResponse]:
    return [
        _HTTPResponse(
            200,
            b'{"seriess":[{"id":"UNRATE","units":"Percent"}]}',
        ),
        _HTTPResponse(
            200,
            b'{"observations":['
            b'{"date":"2025-01-01","value":"4.1","realtime_start":"2025-01-10","realtime_end":"9999-12-31"},'
            b'{"date":"2025-02-01","value":".","realtime_start":"2025-02-07","realtime_end":"9999-12-31"}]}',
        ),
    ]


def test_observations_normalize_metadata_missing_values_and_dates() -> None:
    responses = _responses()
    requests: list[tuple[str, float]] = []

    def transport(url: str, *, timeout: float) -> _HTTPResponse:
        requests.append((url, timeout))
        return responses.pop(0)

    retrieved_at = datetime(2025, 3, 1, 12, tzinfo=UTC)
    adapter = FREDAdapter(
        api_key="test-key",
        transport=transport,
        clock=lambda: retrieved_at,
    )
    observations = adapter.get_observations(
        "UNRATE", date(2025, 1, 1), date(2025, 2, 28), timeout=3
    )

    assert len(observations) == 2
    assert observations[0].value == Decimal("4.1")
    assert observations[0].unit == "Percent"
    assert observations[0].observation_date == date(2025, 1, 1)
    assert observations[0].vintage_start_date == date(2025, 1, 10)
    assert observations[0].retrieved_at_utc == retrieved_at
    assert observations[0].publication_timestamp_utc is None
    assert "not provided" in observations[0].publication_time_status
    assert observations[1].value is None
    assert observations[1].provider_value == "."
    assert observations[1].missing_value_marker == "."
    assert len(requests) == 2
    observation_query = parse_qs(urlsplit(requests[1][0]).query)
    assert observation_query["series_id"] == ["UNRATE"]
    assert observation_query["observation_start"] == ["2025-01-01"]
    assert observation_query["observation_end"] == ["2025-02-28"]
    assert observation_query["units"] == ["lin"]
    assert requests[0][1] == requests[1][1] == 3


def test_multiple_series_are_explicitly_requested() -> None:
    responses = _responses() * 2
    seen_series: list[str] = []

    def transport(url: str, *, timeout: float) -> _HTTPResponse:
        seen_series.append(parse_qs(urlsplit(url).query)["series_id"][0])
        return responses.pop(0)

    adapter = FREDAdapter(api_key="test-key", transport=transport)
    rows = adapter.get_many(["UNRATE", "UNRATE"], date(2025, 1, 1), date(2025, 2, 28))
    assert len(rows) == 4
    assert seen_series == ["UNRATE"] * 4


def test_api_key_required_and_never_in_error_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    with pytest.raises(FREDError, match="FRED_API_KEY"):
        FREDAdapter(key_environment_variable="FRED_API_KEY")

    monkeypatch.setenv("FRED_API_KEY", "environment-test-key")
    configured = FREDAdapter(transport=lambda *args, **kwargs: _HTTPResponse(200, b"{}"))
    assert configured._api_key == "environment-test-key"

    def broken_transport(url: str, *, timeout: float) -> _HTTPResponse:
        raise OSError("network failed; secret-key-value")

    adapter = FREDAdapter(api_key="secret-key-value", transport=broken_transport, max_retries=0)
    with pytest.raises(FREDError) as error:
        adapter.get_observations("UNRATE", date(2025, 1, 1), date(2025, 1, 31))
    assert "secret-key-value" not in str(error.value)


def test_bounded_retries_retry_server_failures_then_succeed() -> None:
    responses = [
        _HTTPResponse(503, b"temporary failure"),
        *_responses(),
    ]
    sleep_calls: list[float] = []

    def transport(url: str, *, timeout: float) -> _HTTPResponse:
        return responses.pop(0)

    adapter = FREDAdapter(
        api_key="test-key", transport=transport, sleep=sleep_calls.append, max_retries=1
    )
    # The first request (series metadata) retries on 503; subsequent requests
    # receive metadata and observation payloads.
    observations = adapter.get_observations("UNRATE", date(2025, 1, 1), date(2025, 2, 28))
    assert len(observations) == 2
    assert len(sleep_calls) == 1


def test_malformed_response_and_invalid_range_raise_clear_errors() -> None:
    malformed = FREDAdapter(
        api_key="test-key",
        transport=lambda url, *, timeout: _HTTPResponse(200, b'{"seriess":[]}'),
        max_retries=0,
    )
    with pytest.raises(FREDError, match="metadata response"):
        malformed.get_observations("UNRATE", date(2025, 1, 1), date(2025, 1, 31))

    adapter = FREDAdapter(api_key="test-key", transport=lambda *args, **kwargs: None)
    with pytest.raises(ValueError, match="must not be after"):
        adapter.get_observations("UNRATE", date(2025, 2, 1), date(2025, 1, 1))
