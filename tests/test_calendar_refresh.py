"""Calendar refresh end to end: backend worker -> existing collector -> Forex Factory
provider -> snapshot -> CalendarService -> API, with a fake HTTP transport.

No network request is made: ``economic_calendar._http_get`` is replaced, and MT5 is
disabled (and booby-trapped) so the monitoring worker can never touch a terminal.
"""

from __future__ import annotations

import json
import textwrap
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import johanna.adapters.economic_calendar as economic_calendar
import johanna.collectors as collectors
from johanna.adapters.economic_calendar import (
    CalendarSnapshot,
    Freshness,
    WeeklySnapshotCache,
    normalize_event,
)
from johanna.api import BackendAPI
from johanna.application import JohannaApplication
from johanna.config import AppConfig, StorageConfig
from johanna.data_services import week_start_utc

# Synthetic feed content in the Forex Factory weekly JSON shape; not real events.
FEED = [
    {
        "title": "Synthetic CPI Sample",
        "country": "USD",
        "date": "2026-11-12T08:30:00-05:00",
        "impact": "High",
        "actual": None,
        "forecast": "2.1%",
        "previous": "2.0%",
    },
    {  # exact duplicate: must be removed
        "title": "Synthetic CPI Sample",
        "country": "USD",
        "date": "2026-11-12T08:30:00-05:00",
        "impact": "High",
        "actual": None,
        "forecast": "2.1%",
        "previous": "2.0%",
    },
    {
        "title": "Synthetic Rate Decision",
        "country": "EUR",
        "date": "2026-11-13T13:15:00+01:00",
        "impact": "Medium",
        "actual": None,
        "forecast": None,
        "previous": None,
    },
]


@dataclass
class FakeResponse:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


class FakeTransport:
    def __init__(self, *responses) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, str]] = []

    def __call__(self, url: str, *, headers: dict[str, str], timeout: float):
        assert url == economic_calendar.ForexFactoryWeeklyJsonProvider.URL
        self.calls.append(dict(headers))
        response = self.responses[min(len(self.calls), len(self.responses)) - 1]
        if isinstance(response, BaseException):
            raise response
        return response


def _forbid_mt5(*args, **kwargs):
    raise AssertionError("MT5 must never be used by calendar tests")


@pytest.fixture
def backend(tmp_path, monkeypatch):
    monkeypatch.setattr(collectors, "MetaTrader5Adapter", _forbid_mt5)
    apps: list[JohannaApplication] = []

    def make(transport: FakeTransport, *, collector_authorized: bool = True):
        monkeypatch.setattr(economic_calendar, "_http_get", transport)
        path = tmp_path / "collectors.toml"
        path.write_text(
            textwrap.dedent(
                f"""
                [collector]
                output_dir = "{(tmp_path / "data" / "collectors").as_posix()}"
                state_dir = "{(tmp_path / "data" / "collector_state").as_posix()}"
                [mt5]
                enabled = false
                [calendar]
                enabled = true
                authorized_for_local_cache = {"true" if collector_authorized else "false"}
                poll_seconds = 900
                max_age_hours = 6
                min_fetch_interval_hours = 6
                max_retries = 2
                retry_backoff_seconds = 0
                """
            ),
            encoding="utf-8",
        )
        base = AppConfig.load(environ={})
        config = replace(
            base,
            storage=StorageConfig(tmp_path / "data"),
            integrations=replace(
                base.integrations,
                calendar_enabled=True,
                forex_factory_authorized=True,
                collectors_config=path,
            ),
        )
        app = JohannaApplication(config)
        app.start()
        app.save_preferences({"market_monitoring_enabled": False})
        apps.append(app)
        return app

    yield make
    for app in apps:
        app.stop()


def _snapshots(tmp_path: Path) -> Path:
    return tmp_path / "data" / "collectors" / "calendar" / "snapshots"


def _seed_snapshot(tmp_path: Path, *, age: timedelta, etag: str | None = None) -> None:
    retrieved = datetime.now(UTC) - age
    event = normalize_event(
        {"event_name": "Cached Synthetic Event", "scheduled_at": "2026-11-12T10:00:00Z"},
        source="Forex Factory weekly JSON export",
        retrieved_at=retrieved,
    )
    WeeklySnapshotCache(_snapshots(tmp_path)).write(
        CalendarSnapshot(
            week_start=week_start_utc(datetime.now(UTC)),
            retrieved_at_utc=retrieved,
            source="Forex Factory weekly JSON export",
            events=(event,),
            freshness=Freshness.FRESH,
            etag=etag,
        )
    )


def _wait_status(app, predicate, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        status = app.calendar.status()
        if predicate(status):
            return status
        if time.monotonic() > deadline:
            raise AssertionError(f"calendar status did not converge: {status}")
        time.sleep(0.02)


def test_successful_refresh_normalizes_deduplicates_and_persists(backend, tmp_path) -> None:
    transport = FakeTransport(
        FakeResponse(200, json.dumps(FEED).encode(), {"ETag": '"synthetic-1"'})
    )
    app = backend(transport)
    status = _wait_status(app, lambda s: s["freshness"] == "fresh")
    assert status["state"] == "running"
    assert status["event_count"] == 2  # duplicate removed
    assert status["collector_status"] == "ok"
    assert len(transport.calls) == 1
    assert transport.calls[0]["Accept"] == "application/json"

    events, error = app.calendar.events()
    assert error is None
    by_name = {event["event_name"]: event for event in events}
    assert by_name["Synthetic CPI Sample"]["scheduled_at_utc"] == "2026-11-12T13:30:00Z"
    assert by_name["Synthetic Rate Decision"]["scheduled_at_utc"] == "2026-11-13T12:15:00Z"
    assert by_name["Synthetic CPI Sample"]["actual"] is None  # never inferred
    assert by_name["Synthetic CPI Sample"]["impact"] == "High"

    files = list(_snapshots(tmp_path).glob("week-*.json"))
    assert len(files) == 1
    saved = json.loads(files[0].read_text(encoding="utf-8"))
    assert saved["etag"] == '"synthetic-1"'
    assert saved["retrieved_at_utc"].endswith("Z")
    assert (
        app.status()["last_provider_updates"]["economic_calendar"]
        == (status["snapshot_retrieved_at_utc"])
    )
    assert app.health()[0] is True


def test_rate_limit_is_not_retried_and_is_visible(backend) -> None:
    transport = FakeTransport(FakeResponse(429))
    app = backend(transport)
    status = _wait_status(app, lambda s: s["collector_status"] == "error")
    time.sleep(0.2)  # would expose any immediate re-request
    assert len(transport.calls) == 1
    assert status["state"] == "degraded"
    assert status["freshness"] == "no_snapshot"
    assert "no cached snapshot" in status["collector_last_error"]
    response: dict = {}
    body = b"".join(
        BackendAPI(app)(
            {"PATH_INFO": "/api/dashboard", "REQUEST_METHOD": "GET"},
            lambda code, headers: response.update(code=code),
        )
    )
    calendar = json.loads(body)["calendar"]
    assert calendar["events"] == [] and calendar["status"]["state"] == "degraded"


def test_authorization_denial_serves_existing_snapshot_as_stale(backend, tmp_path) -> None:
    _seed_snapshot(tmp_path, age=timedelta(hours=8))  # old enough to be re-fetched
    transport = FakeTransport(FakeResponse(403))
    app = backend(transport)
    status = _wait_status(app, lambda s: s["collector_status"] is not None)
    assert len(transport.calls) == 1
    assert status["freshness"] == "stale"
    assert status["collector_status"] == "stale"
    events, _ = app.calendar.events()
    assert events[0]["event_name"] == "Cached Synthetic Event"
    assert events[0]["freshness"] == "stale"


def test_network_errors_are_retried_with_bounds(backend) -> None:
    transport = FakeTransport(OSError("synthetic network down"))
    app = backend(transport)
    status = _wait_status(app, lambda s: s["collector_status"] == "error")
    assert len(transport.calls) == 3  # first attempt + max_retries (2)
    assert status["state"] == "degraded"


def test_fresh_snapshot_prevents_unnecessary_requests(backend, tmp_path) -> None:
    _seed_snapshot(tmp_path, age=timedelta(hours=1))
    transport = FakeTransport(FakeResponse(500))
    app = backend(transport)
    status = _wait_status(app, lambda s: s["collector_status"] is not None)
    assert transport.calls == []
    assert status["freshness"] == "fresh"
    assert status["state"] == "running"


def test_not_modified_refreshes_snapshot_with_validators(backend, tmp_path) -> None:
    _seed_snapshot(tmp_path, age=timedelta(hours=7), etag='"cached-etag"')
    transport = FakeTransport(FakeResponse(304, headers={"ETag": '"cached-etag"'}))
    app = backend(transport)
    status = _wait_status(app, lambda s: s["freshness"] == "fresh")
    assert transport.calls[0]["If-None-Match"] == '"cached-etag"'
    assert status["event_count"] == 1
    assert app.calendar.events()[0][0]["event_name"] == "Cached Synthetic Event"


def test_collector_permission_flag_is_still_enforced(backend) -> None:
    transport = FakeTransport(FakeResponse(200, b"[]"))
    app = backend(transport, collector_authorized=False)
    status = _wait_status(app, lambda s: s["state"] == "error")
    assert status["refresh"]["error"] == "CollectorConfigError"
    assert transport.calls == []
