"""Read-only client for FRED series metadata and observations.

Observation dates are periods, not publication timestamps. FRED's observations
endpoint does not provide exact release instants, so this adapter leaves those
unknown and retains only the API's separate vintage date fields.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class FREDError(RuntimeError):
    """Safe FRED adapter error; API keys and response bodies are not included."""


@dataclass(frozen=True)
class FREDObservation:
    """One normalized observation; missing provider values stay explicit."""

    series_id: str
    observation_date: date
    value: Decimal | None
    unit: str | None
    retrieved_at_utc: datetime
    source: str
    provider_value: str
    missing_value_marker: str | None
    vintage_start_date: date | None
    vintage_end_date: date | None
    publication_timestamp_utc: datetime | None = None
    publication_time_status: str = "not provided by FRED observations endpoint"


@dataclass(frozen=True)
class _HTTPResponse:
    status_code: int
    body: bytes


def _http_get(url: str, *, timeout: float) -> _HTTPResponse:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return _HTTPResponse(response.status, response.read())
    except HTTPError as response:
        return _HTTPResponse(response.code, response.read())
    except URLError as exc:
        raise OSError("FRED network request failed") from exc


class FREDAdapter:
    """Fetch explicitly selected FRED series and date ranges using API v1."""

    BASE_URL = "https://api.stlouisfed.org/fred"
    SOURCE = "Federal Reserve Bank of St. Louis FRED API v1"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        key_environment_variable: str = "FRED_API_KEY",
        transport: Callable[..., _HTTPResponse] | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        max_retries: int = 2,
    ) -> None:
        key = api_key if api_key is not None else os.getenv(key_environment_variable)
        if not key or not key.strip():
            raise FREDError(f"Set {key_environment_variable} in the environment before using FRED")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self._api_key = key.strip()
        self._transport = transport or _http_get
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep
        self._max_retries = max_retries

    def get_observations(
        self,
        series_id: str,
        observation_start: date,
        observation_end: date,
        *,
        timeout: float = 10.0,
        units: str = "lin",
    ) -> tuple[FREDObservation, ...]:
        """Get observations for one selected series and inclusive date range."""
        if not isinstance(series_id, str) or not series_id.strip():
            raise ValueError("series_id must be a non-empty string")
        if not isinstance(observation_start, date) or isinstance(observation_start, datetime):
            raise ValueError("observation_start must be a date")
        if not isinstance(observation_end, date) or isinstance(observation_end, datetime):
            raise ValueError("observation_end must be a date")
        if observation_start > observation_end:
            raise ValueError("observation_start must not be after observation_end")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        if units not in {"lin", "chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log"}:
            raise ValueError("units must be a supported FRED units transformation")

        clean_series_id = series_id.strip()
        metadata = self._request(
            "series",
            {"series_id": clean_series_id},
            timeout,
        )
        series_rows = metadata.get("seriess")
        if not isinstance(series_rows, list) or len(series_rows) != 1:
            raise FREDError("FRED series metadata response has an invalid schema")
        series_row = series_rows[0]
        if not isinstance(series_row, dict) or series_row.get("id") != clean_series_id:
            raise FREDError("FRED series metadata did not match the requested series")
        unit_value = series_row.get("units")
        unit = unit_value if isinstance(unit_value, str) and unit_value else None

        payload = self._request(
            "series/observations",
            {
                "series_id": clean_series_id,
                "observation_start": observation_start.isoformat(),
                "observation_end": observation_end.isoformat(),
                "units": units,
            },
            timeout,
        )
        rows = payload.get("observations")
        if not isinstance(rows, list):
            raise FREDError("FRED observations response has an invalid schema")

        retrieved = self._clock()
        if retrieved.tzinfo is None or retrieved.utcoffset() is None:
            raise ValueError("FRED clock must return a timezone-aware datetime")
        retrieved = retrieved.astimezone(UTC)
        observations: list[FREDObservation] = []
        for row in rows:
            if not isinstance(row, dict):
                raise FREDError("FRED observations response contains a malformed row")
            raw_date, raw_value = row.get("date"), row.get("value")
            if not isinstance(raw_date, str) or not isinstance(raw_value, str):
                raise FREDError("FRED observation is missing its date or string value")
            try:
                observed_on = date.fromisoformat(raw_date)
            except ValueError as exc:
                raise FREDError("FRED returned an invalid observation date") from exc
            if not observation_start <= observed_on <= observation_end:
                raise FREDError("FRED returned an observation outside the requested date range")

            missing_marker = raw_value if raw_value == "." else None
            if missing_marker:
                numeric_value = None
            else:
                try:
                    numeric_value = Decimal(raw_value)
                except InvalidOperation as exc:
                    raise FREDError("FRED returned a non-numeric observation value") from exc
                if not numeric_value.is_finite():
                    raise FREDError("FRED returned a non-finite observation value")

            observations.append(
                FREDObservation(
                    series_id=clean_series_id,
                    observation_date=observed_on,
                    value=numeric_value,
                    unit=unit,
                    retrieved_at_utc=retrieved,
                    source=self.SOURCE,
                    provider_value=raw_value,
                    missing_value_marker=missing_marker,
                    vintage_start_date=self._optional_date(row.get("realtime_start")),
                    vintage_end_date=self._optional_date(row.get("realtime_end")),
                )
            )
        return tuple(observations)

    def get_many(
        self,
        series_ids: tuple[str, ...] | list[str],
        observation_start: date,
        observation_end: date,
        *,
        timeout: float = 10.0,
        units: str = "lin",
    ) -> tuple[FREDObservation, ...]:
        """Fetch each explicitly listed series in order, preserving row order."""
        if not series_ids:
            raise ValueError("series_ids must contain at least one series ID")
        return tuple(
            observation
            for series_id in series_ids
            for observation in self.get_observations(
                series_id,
                observation_start,
                observation_end,
                timeout=timeout,
                units=units,
            )
        )

    @staticmethod
    def _optional_date(raw: Any) -> date | None:
        if raw in (None, ""):
            return None
        if not isinstance(raw, str):
            raise FREDError("FRED returned a malformed vintage date")
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise FREDError("FRED returned an invalid vintage date") from exc

    def _request(self, endpoint: str, params: Mapping[str, str], timeout: float) -> dict[str, Any]:
        query = urlencode({**params, "api_key": self._api_key, "file_type": "json"})
        url = f"{self.BASE_URL}/{endpoint}?{query}"
        for attempt in range(self._max_retries + 1):
            try:
                response = self._transport(url, timeout=timeout)
            except (OSError, TimeoutError):
                if attempt < self._max_retries:
                    self._sleep(min(0.25 * (2**attempt), 2.0))
                    continue
                raise FREDError("FRED request failed after bounded retries") from None

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < self._max_retries:
                    self._sleep(min(0.25 * (2**attempt), 2.0))
                    continue
                raise FREDError(f"FRED returned HTTP {response.status_code} after bounded retries")
            if response.status_code != 200:
                raise FREDError(f"FRED returned HTTP {response.status_code}")
            try:
                payload = json.loads(response.body.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                raise FREDError("FRED returned invalid UTF-8 JSON") from None
            if not isinstance(payload, dict):
                raise FREDError("FRED returned a JSON response with an invalid schema")
            if "error_code" in payload or "error_message" in payload:
                error_code = payload.get("error_code")
                if error_code in (429, "429") and attempt < self._max_retries:
                    self._sleep(min(0.25 * (2**attempt), 2.0))
                    continue
                raise FREDError(
                    "FRED API rejected the request; check the series, parameters, and API key"
                )
            return payload
        raise FREDError("FRED request failed after bounded retries")
