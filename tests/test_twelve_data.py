from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from johanna.adapters.twelve_data import (
    CreditLimitExceeded,
    RequestCreditLimiter,
    TwelveDataAdapter,
    TwelveDataError,
)


class FakeClock:
    def __init__(self, current: datetime):
        self.current = current
        self.lock = threading.Lock()
        self.sleeps: list[float] = []

    def now(self) -> datetime:
        with self.lock:
            return self.current

    def sleep(self, seconds: float) -> None:
        with self.lock:
            self.sleeps.append(seconds)
            self.current += timedelta(seconds=seconds)


class FakeResponse:
    def __init__(self, status_code: int, payload: Any):
        self.status_code = status_code
        self.payload = payload

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


def _limiter(tmp_path, **kwargs) -> RequestCreditLimiter:
    return RequestCreditLimiter(state_path=tmp_path / "credit-ledger.sqlite3", **kwargs)


def test_rolling_minute_waits_before_reserving_over_capacity(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, 12, tzinfo=UTC))
    limiter = _limiter(
        tmp_path,
        minute_limit=8,
        daily_limit=800,
        minute_safety_margin=1,
        daily_safety_margin=10,
        clock=clock.now,
        sleep=clock.sleep,
    )

    for _ in range(7):
        limiter.reserve()
    assert clock.sleeps == []
    limiter.reserve()

    assert clock.sleeps == [60]
    assert clock.now() == datetime(2026, 1, 1, 12, 1, tzinfo=UTC)


def test_daily_window_resets_at_configured_utc_midnight(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, 23, 59, 58, tzinfo=UTC))
    limiter = _limiter(
        tmp_path,
        minute_limit=20,
        daily_limit=3,
        minute_safety_margin=1,
        daily_safety_margin=1,
        clock=clock.now,
        sleep=clock.sleep,
    )
    limiter.reserve()
    limiter.reserve()
    limiter.reserve()

    assert clock.sleeps == [2]
    assert clock.now() == datetime(2026, 1, 2, tzinfo=UTC)
    limiter.reserve()


def test_daily_reset_hour_can_be_configured(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, 5, 59, tzinfo=UTC))
    limiter = _limiter(
        tmp_path,
        minute_limit=20,
        daily_limit=3,
        minute_safety_margin=1,
        daily_safety_margin=1,
        daily_reset_hour_utc=6,
        clock=clock.now,
        sleep=clock.sleep,
    )
    limiter.reserve()
    limiter.reserve()
    limiter.reserve()

    assert clock.sleeps == [60]
    assert clock.now() == datetime(2026, 1, 1, 6, tzinfo=UTC)


def test_request_cost_over_safe_capacity_is_rejected_without_waiting(tmp_path) -> None:
    limiter = _limiter(tmp_path, minute_limit=8, minute_safety_margin=1)

    with pytest.raises(CreditLimitExceeded):
        limiter.reserve(8)


def test_multi_symbol_request_reserves_cost_per_symbol(tmp_path) -> None:
    adapter = TwelveDataAdapter(
        api_key="fake",
        limiter=_limiter(tmp_path, minute_limit=3, minute_safety_margin=1),
        transport=lambda *_args, **_kwargs: pytest.fail("must reject before HTTP"),
    )

    with pytest.raises(CreditLimitExceeded):
        adapter.get_json("quote", {"symbol": "EUR/USD,AUD/USD,GBP/USD,USD/JPY"})


def test_concurrent_reservations_stay_within_daily_safe_capacity(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    limiter = _limiter(
        tmp_path,
        minute_limit=100,
        daily_limit=9,
        minute_safety_margin=1,
        daily_safety_margin=1,
        clock=clock.now,
        sleep=clock.sleep,
    )
    barrier = threading.Barrier(20)

    def reserve() -> None:
        barrier.wait()
        limiter.reserve()

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(lambda _: reserve(), range(20)))

    # The daily capacity is eight; each wait advances the deterministic clock
    # to the next UTC day before another batch may reserve.
    assert clock.now() >= datetime(2026, 1, 3, tzinfo=UTC)
    assert all(0 < wait <= 86400 for wait in clock.sleeps)


def test_failed_http_attempt_is_charged_and_retry_reserves_again(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    calls: list[tuple[str, dict[str, str], float]] = []
    responses = [
        FakeResponse(503, {}),
        FakeResponse(200, {"meta": {}, "values": []}),
        FakeResponse(200, {"price": "1.2"}),
    ]

    def transport(url: str, *, headers: dict[str, str], timeout: float) -> FakeResponse:
        calls.append((url, dict(headers), timeout))
        return responses.pop(0)

    adapter = TwelveDataAdapter(
        api_key="fake-secret",
        transport=transport,
        limiter=_limiter(
            tmp_path,
            minute_limit=3,
            daily_limit=10,
            minute_safety_margin=1,
            daily_safety_margin=1,
            clock=clock.now,
            sleep=clock.sleep,
        ),
        max_retries=1,
        backoff_seconds=0,
        sleep=lambda _seconds: None,
    )

    adapter.get_time_series("EUR/USD", "5min")
    adapter.get_json("price", {"symbol": "EUR/USD"})

    assert len(calls) == 3
    assert calls[0][0].endswith("symbol=EUR%2FUSD&interval=5min&outputsize=5000")
    assert calls[0][1]["Authorization"] == "apikey fake-secret"
    assert "fake-secret" not in calls[0][0]
    assert all(call[2] == 10.0 for call in calls)
    assert clock.sleeps == [60]


def test_separate_limiter_instances_share_persistent_reservations(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, 12, tzinfo=UTC))
    state_path = tmp_path / "shared-credits.sqlite3"
    first = RequestCreditLimiter(
        minute_limit=3,
        minute_safety_margin=1,
        state_path=state_path,
        clock=clock.now,
        sleep=clock.sleep,
    )
    second = RequestCreditLimiter(
        minute_limit=3,
        minute_safety_margin=1,
        state_path=state_path,
        clock=clock.now,
        sleep=clock.sleep,
    )

    first.reserve()
    first.reserve()
    second.reserve()

    assert clock.sleeps == [60]
    assert state_path.is_file()


def test_credit_limiter_fails_closed_when_ledger_is_unavailable(tmp_path) -> None:
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("block", encoding="utf-8")
    limiter = RequestCreditLimiter(state_path=blocker / "ledger.sqlite3")

    with pytest.raises(TwelveDataError, match="refusing request"):
        limiter.reserve()


def test_concurrent_limiter_instances_share_daily_capacity(tmp_path) -> None:
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=UTC))
    state_path = tmp_path / "concurrent-credits.sqlite3"
    barrier = threading.Barrier(12)
    limiters = [
        RequestCreditLimiter(
            minute_limit=100,
            daily_limit=4,
            minute_safety_margin=1,
            daily_safety_margin=1,
            state_path=state_path,
            clock=clock.now,
            sleep=clock.sleep,
        )
        for _ in range(12)
    ]

    def reserve(limiter):
        barrier.wait()
        limiter.reserve()

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(reserve, limiters))

    with sqlite3.connect(state_path) as connection:
        totals = connection.execute(
            "SELECT date(reserved_at_utc, 'unixepoch'), SUM(credits) "
            "FROM credit_reservations GROUP BY date(reserved_at_utc, 'unixepoch')"
        ).fetchall()
    assert totals
    assert all(credits <= 3 for _, credits in totals)


def test_http_error_is_clear_and_does_not_include_secret(tmp_path) -> None:
    secret = "never-print-this"
    adapter = TwelveDataAdapter(
        api_key=secret,
        limiter=_limiter(tmp_path),
        transport=lambda *_args, **_kwargs: FakeResponse(400, {}),
        max_retries=0,
    )

    with pytest.raises(TwelveDataError, match="HTTP 400") as exc:
        adapter.get_json("quote", {"symbol": "EUR/USD"})
    assert secret not in str(exc.value)


def test_malformed_json_responses_are_rejected(tmp_path) -> None:
    adapter = TwelveDataAdapter(
        api_key="fake",
        limiter=_limiter(tmp_path),
        transport=lambda *_args, **_kwargs: FakeResponse(200, []),
    )
    with pytest.raises(TwelveDataError, match="JSON object"):
        adapter.get_json("quote", {"symbol": "EUR/USD"})

    malformed_series = TwelveDataAdapter(
        api_key="fake",
        limiter=_limiter(tmp_path),
        transport=lambda *_args, **_kwargs: FakeResponse(
            200, {"meta": {}, "values": [{"open": 1}]}
        ),
    )
    with pytest.raises(TwelveDataError, match="malformed bar"):
        malformed_series.get_time_series("EUR/USD", "5min")


def test_unknown_endpoint_requires_explicit_credit_cost() -> None:
    adapter = TwelveDataAdapter(api_key="fake")
    with pytest.raises(TwelveDataError, match="No credit cost configured"):
        adapter.get_json("high_cost_endpoint", {})
