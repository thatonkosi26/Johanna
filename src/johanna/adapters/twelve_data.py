"""Twelve Data REST client with conservative, thread-safe credit accounting.

Reservations are made before each network attempt and are not refunded: the
provider may charge credits even when an attempt fails or times out.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


class TwelveDataError(RuntimeError):
    """Safe Twelve Data adapter error that never contains request credentials."""


class CreditLimitExceeded(TwelveDataError):
    """A single request cannot fit under the configured credit caps."""


@dataclass(frozen=True)
class _Response:
    status_code: int
    body: bytes
    headers: Mapping[str, str]

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def _urlopen_transport(url: str, *, headers: Mapping[str, str], timeout: float) -> _Response:
    request = Request(url, headers=dict(headers), method="GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return _Response(response.status, response.read(), response.headers)
    except HTTPError as response:
        return _Response(response.code, response.read(), response.headers)
    except URLError as exc:
        raise OSError("Twelve Data network request failed") from exc


class RequestCreditLimiter:
    """Persist and atomically reserve credits under minute and UTC-day ceilings.

    Instances and processes share reservations when they use the same SQLite
    state path. Defaults leave one minute credit and ten daily credits unused.
    """

    def __init__(
        self,
        *,
        minute_limit: int = 8,
        daily_limit: int = 800,
        minute_safety_margin: int = 1,
        daily_safety_margin: int = 10,
        daily_reset_hour_utc: int = 0,
        state_path: str | Path = "data/twelve_data_credit_ledger.sqlite3",
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        for name, value in (
            ("minute_limit", minute_limit),
            ("daily_limit", daily_limit),
            ("minute_safety_margin", minute_safety_margin),
            ("daily_safety_margin", daily_safety_margin),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if minute_limit <= minute_safety_margin or daily_limit <= daily_safety_margin:
            raise ValueError("Safety margins must leave positive effective credit limits")
        if isinstance(daily_reset_hour_utc, bool) or not 0 <= daily_reset_hour_utc <= 23:
            raise ValueError("daily_reset_hour_utc must be from 0 through 23")
        self.minute_capacity = minute_limit - minute_safety_margin
        self.daily_capacity = daily_limit - daily_safety_margin
        self.daily_reset_hour_utc = daily_reset_hour_utc
        self.state_path = Path(state_path)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.state_path, timeout=30)
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS credit_reservations ("
            "id INTEGER PRIMARY KEY, reserved_at_utc REAL NOT NULL, credits INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS credit_reservations_time "
            "ON credit_reservations(reserved_at_utc)"
        )
        return connection

    def reserve(self, credits: int = 1) -> None:
        """Wait as needed, then atomically charge ``credits`` to both windows."""
        if isinstance(credits, bool) or not isinstance(credits, int) or credits <= 0:
            raise ValueError("credits must be a positive integer")
        if credits > self.minute_capacity or credits > self.daily_capacity:
            raise CreditLimitExceeded("Request credit cost exceeds the configured safe capacity")

        while True:
            with self._lock:
                now = self._clock()
                if now.tzinfo is None or now.utcoffset() is None:
                    raise ValueError("Credit limiter clock must return an aware datetime")
                now = now.astimezone(UTC)
                cutoff = now - timedelta(seconds=60)
                day_start = datetime(
                    now.year,
                    now.month,
                    now.day,
                    self.daily_reset_hour_utc,
                    tzinfo=UTC,
                )
                if now < day_start:
                    day_start -= timedelta(days=1)

                connection: sqlite3.Connection | None = None
                try:
                    connection = self._connect()
                    connection.execute("BEGIN IMMEDIATE")
                    retain_from = min(cutoff.timestamp(), day_start.timestamp())
                    connection.execute(
                        "DELETE FROM credit_reservations WHERE reserved_at_utc < ?",
                        (retain_from,),
                    )
                    minute_rows = connection.execute(
                        "SELECT reserved_at_utc, credits FROM credit_reservations "
                        "WHERE reserved_at_utc > ? ORDER BY reserved_at_utc, id",
                        (cutoff.timestamp(),),
                    ).fetchall()
                    daily_used = connection.execute(
                        "SELECT COALESCE(SUM(credits), 0) FROM credit_reservations "
                        "WHERE reserved_at_utc >= ?",
                        (day_start.timestamp(),),
                    ).fetchone()[0]
                    minute_used = sum(cost for _, cost in minute_rows)
                    if (
                        minute_used + credits <= self.minute_capacity
                        and daily_used + credits <= self.daily_capacity
                    ):
                        connection.execute(
                            "INSERT INTO credit_reservations(reserved_at_utc, credits) "
                            "VALUES (?, ?)",
                            (now.timestamp(), credits),
                        )
                        connection.commit()
                        return

                    waits: list[float] = []
                    if minute_used + credits > self.minute_capacity:
                        remaining = credits
                        for reserved_at, cost in minute_rows:
                            remaining -= cost
                            if remaining <= 0:
                                waits.append(max(0.001, reserved_at + 60 - now.timestamp()))
                                break
                    if daily_used + credits > self.daily_capacity:
                        next_day = day_start + timedelta(days=1)
                        waits.append(max(0.001, (next_day - now).total_seconds()))
                    connection.rollback()
                except (sqlite3.Error, OSError):
                    if connection is not None:
                        connection.rollback()
                    raise TwelveDataError(
                        "Credit limiter state could not be read or written; refusing request"
                    ) from None
                finally:
                    if connection is not None:
                        connection.close()
            self._sleep(min(waits))


class TwelveDataAdapter:
    """Small GET-only Twelve Data client; no credentials appear in URLs/logs."""

    BASE_URL = "https://api.twelvedata.com"
    DEFAULT_ENDPOINT_COSTS = {"time_series": 1, "quote": 1, "price": 1}

    def __init__(
        self,
        *,
        api_key: str | None = None,
        key_environment_variable: str = "TWELVE_DATA_API_KEY",
        endpoint_costs: Mapping[str, int] | None = None,
        per_symbol_endpoints: set[str] | frozenset[str] | None = None,
        limiter: RequestCreditLimiter | None = None,
        timeout: float = 10.0,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        transport: Callable[..., _Response] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else os.getenv(key_environment_variable)
        if not self._api_key:
            raise TwelveDataError(
                f"Set {key_environment_variable} in the environment before using Twelve Data"
            )
        if timeout <= 0 or max_retries < 0 or backoff_seconds < 0:
            raise ValueError("timeout must be positive; retries and backoff must be non-negative")
        self._endpoint_costs = dict(self.DEFAULT_ENDPOINT_COSTS)
        if endpoint_costs:
            self._endpoint_costs.update(endpoint_costs)
        for endpoint, cost in self._endpoint_costs.items():
            if not endpoint or isinstance(cost, bool) or not isinstance(cost, int) or cost < 1:
                raise ValueError("endpoint costs must map names to positive integers")
        self._per_symbol_endpoints = frozenset(
            {"time_series", "quote", "price"}
            if per_symbol_endpoints is None
            else per_symbol_endpoints
        )
        self._limiter = limiter or RequestCreditLimiter()
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_seconds = backoff_seconds
        self._transport = transport or _urlopen_transport
        self._sleep = sleep or time.sleep

    def get_json(self, endpoint: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """GET JSON from a configured endpoint, reserving credits before each try."""
        endpoint = endpoint.strip("/")
        if not endpoint or "?" in endpoint or ".." in endpoint:
            raise ValueError("endpoint must be a simple endpoint path")
        try:
            cost = self._endpoint_costs[endpoint]
        except KeyError:
            raise TwelveDataError(f"No credit cost configured for endpoint {endpoint!r}") from None
        if endpoint in self._per_symbol_endpoints:
            symbols = params.get("symbol")
            if not isinstance(symbols, str) or not symbols.strip():
                raise ValueError(f"Endpoint {endpoint!r} requires a non-empty symbol parameter")
            cost *= len([symbol for symbol in symbols.split(",") if symbol.strip()])
        query = urlencode({key: value for key, value in params.items() if value is not None})
        url = f"{self.BASE_URL}/{endpoint}"
        if query:
            url = f"{url}?{query}"
        headers = {"Authorization": f"apikey {self._api_key}", "Accept": "application/json"}

        for attempt in range(self.max_retries + 1):
            self._limiter.reserve(cost)
            try:
                response = self._transport(url, headers=headers, timeout=self.timeout)
            except (OSError, TimeoutError):
                if attempt >= self.max_retries:
                    raise TwelveDataError(
                        "Twelve Data request failed after bounded retries"
                    ) from None
                self._sleep(self.backoff_seconds * (2**attempt))
                continue

            if response.status_code == 429 or response.status_code >= 500:
                if attempt >= self.max_retries:
                    raise TwelveDataError(
                        f"Twelve Data returned HTTP {response.status_code} after bounded retries"
                    )
                self._sleep(self.backoff_seconds * (2**attempt))
                continue
            if response.status_code < 200 or response.status_code >= 300:
                raise TwelveDataError(f"Twelve Data returned HTTP {response.status_code}")
            try:
                payload = response.json()
            except (ValueError, UnicodeError):
                raise TwelveDataError("Twelve Data returned invalid JSON") from None
            if not isinstance(payload, dict):
                raise TwelveDataError("Twelve Data response must be a JSON object")
            if (
                str(payload.get("status", "")).lower() == "error"
                or "code" in payload
                and "message" in payload
            ):
                raise TwelveDataError("Twelve Data reported an API error")
            return payload
        raise AssertionError("unreachable")

    def get_time_series(
        self, symbol: str, interval: str, *, outputsize: int = 5000, **params: Any
    ) -> dict[str, Any]:
        """Fetch and minimally validate a time-series response."""
        if not symbol or not interval:
            raise ValueError("symbol and interval are required")
        if isinstance(outputsize, bool) or not isinstance(outputsize, int) or outputsize < 1:
            raise ValueError("outputsize must be a positive integer")
        result = self.get_json(
            "time_series",
            {"symbol": symbol, "interval": interval, "outputsize": outputsize, **params},
        )
        values = result.get("values")
        if not isinstance(result.get("meta"), dict) or not isinstance(values, list):
            raise TwelveDataError("Twelve Data time_series response is missing meta or values")
        for row in values:
            if not isinstance(row, dict) or any(
                key not in row for key in ("datetime", "open", "high", "low", "close")
            ):
                raise TwelveDataError("Twelve Data time_series contains a malformed bar")
        return result
