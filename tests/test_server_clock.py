"""MT5 server-clock conversion: epoch (server wall clock) -> UTC, with DST and no TZ leakage."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

import pytest
from fake_mt5 import FakeMT5

from johanna.adapters.base import MT5AdapterError
from johanna.adapters.mt5 import MetaTrader5Adapter, MT5TimeBaseError, ServerClock


def server_epoch(wall: str) -> int:
    """Epoch MT5 would report for a server wall-clock time (wall time encoded as if UTC)."""
    return int(datetime.fromisoformat(wall).replace(tzinfo=UTC).timestamp())


@pytest.mark.parametrize("epoch", [0, 1_735_689_600, 1_794_432_208.481, 2_000_000_000])
def test_utc_clock_is_exact_epoch_conversion(epoch) -> None:
    converted = ServerClock().to_utc(epoch)
    assert converted == datetime.fromtimestamp(epoch, UTC)
    assert converted.tzinfo is UTC


@pytest.mark.parametrize(
    ("server_wall", "expected_utc"),
    [
        ("2026-03-06T23:00:00", "2026-03-06T21:00:00"),  # US winter: server = UTC+2
        ("2026-03-09T00:00:00", "2026-03-08T21:00:00"),  # after US DST start (8 Mar): UTC+3
        ("2026-10-30T22:00:00", "2026-10-30T19:00:00"),  # EU DST already over, US not: UTC+3
        ("2026-11-02T00:00:00", "2026-11-01T22:00:00"),  # after US DST end (1 Nov): UTC+2
    ],
)
def test_new_york_anchored_clock_follows_us_dst(server_wall, expected_utc) -> None:
    clock = ServerClock("America/New_York", 7)
    result = clock.to_utc(server_epoch(server_wall))
    assert result == datetime.fromisoformat(expected_utc).replace(tzinfo=UTC)


@pytest.mark.parametrize(
    ("server_wall", "expected_utc"),
    [
        ("2026-03-28T12:00:00", "2026-03-28T10:00:00"),  # EET (UTC+2)
        ("2026-03-30T12:00:00", "2026-03-30T09:00:00"),  # EEST after 29 Mar (UTC+3)
        ("2026-10-24T12:00:00", "2026-10-24T09:00:00"),  # EEST
        ("2026-10-26T12:00:00", "2026-10-26T10:00:00"),  # EET after 25 Oct
    ],
)
def test_iana_zone_clock_follows_its_own_dst(server_wall, expected_utc) -> None:
    result = ServerClock("Europe/Athens").to_utc(server_epoch(server_wall))
    assert result == datetime.fromisoformat(expected_utc).replace(tzinfo=UTC)


@pytest.mark.parametrize(
    "server_wall",
    [
        "2026-03-08T09:30:00",  # New York 02:30 does not exist (spring forward)
        "2026-11-01T08:30:00",  # New York 01:30 happens twice (fall back)
    ],
)
def test_dst_gap_or_repeated_hour_is_refused_not_guessed(server_wall) -> None:
    with pytest.raises(MT5AdapterError, match="cannot be converted unambiguously"):
        ServerClock("America/New_York", 7).to_utc(server_epoch(server_wall))


def test_invalid_server_clock_is_rejected() -> None:
    with pytest.raises(Exception):
        ServerClock("Not/AZone")
    with pytest.raises(ValueError):
        ServerClock("UTC", 2.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        ServerClock("UTC", 15)


_SUBPROCESS = """
import json
from datetime import UTC, datetime
from johanna.adapters.mt5 import ServerClock
epochs = [1794432208.481, 1772920800, 1793570400]
print(json.dumps([[ServerClock().to_utc(e).isoformat(),
                   ServerClock("America/New_York", 7).to_utc(e).isoformat(),
                   ServerClock("Europe/Athens").to_utc(e).isoformat()] for e in epochs]))
"""


def test_conversion_does_not_depend_on_machine_timezone() -> None:
    outputs = set()
    for tz in ("UTC0", "EST5EDT", "JST-9", None):
        env = dict(os.environ)
        env.pop("TZ", None)
        if tz:
            env["TZ"] = tz
        result = subprocess.run(
            [sys.executable, "-c", _SUBPROCESS],
            capture_output=True,
            text=True,
            env=env,
            check=True,
            timeout=60,
        )
        outputs.add(result.stdout.strip())
    assert len(outputs) == 1, outputs
    assert json.loads(outputs.pop())[0][0] == "2026-11-11T21:23:28.481000+00:00"


def test_tick_and_bar_conversion_is_single_and_utc() -> None:
    fake = FakeMT5()
    adapter = MetaTrader5Adapter(mt5_module=fake)
    adapter.initialize()
    tick = adapter.get_tick("EURUSD")
    assert tick["time_utc"].tzinfo is UTC
    assert abs((tick["time_utc"] - datetime.now(UTC)).total_seconds()) < 5
    assert tick["time_utc"] == datetime.fromtimestamp(tick["time_msc"] / 1000, UTC)
    bars = adapter.get_bars("EURUSD", "H1", 5, include_current=True)
    assert all(bar["timestamp"].tzinfo is UTC for bar in bars)
    assert bars[-1]["timestamp"] <= datetime.now(UTC)
    assert bars[-1]["timestamp"] > datetime.now(UTC) - timedelta(hours=1)  # forming bar
    assert bars[0]["source_timezone"] == "UTC"


def test_server_time_ahead_of_utc_is_refused_until_configured() -> None:
    fake = FakeMT5(server_offset_seconds=2 * 3600)  # a server on UTC+2 wall clock
    adapter = MetaTrader5Adapter(mt5_module=fake)
    adapter.initialize()
    with pytest.raises(MT5TimeBaseError, match="ahead of this computer's UTC clock"):
        adapter.get_tick("EURUSD")
    with pytest.raises(MT5TimeBaseError):
        adapter.get_bars("EURUSD", "M5", 5)
    # Configured correctly (fixed UTC+2 is IANA "Etc/GMT-2"), the same data converts.
    fixed = MetaTrader5Adapter(mt5_module=fake, server_clock=ServerClock("Etc/GMT-2"))
    fixed.initialize()
    assert abs((fixed.get_tick("EURUSD")["time_utc"] - datetime.now(UTC)).total_seconds()) < 5
