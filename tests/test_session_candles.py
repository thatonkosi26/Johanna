"""T4.1: candles lying entirely inside configured market closures (``session_candles``).

Hand-written UTC timestamps; the real T3 configuration (EXNESS_T0_PROVISIONAL), no mocked
closures. Reference schedule (New York 17:00 close / 17:05 reopen):
- US summer time (EDT): Friday close 21:00 UTC, Sunday reopen 21:05 UTC.
- US winter time (EST): Friday close 22:00 UTC, Sunday reopen 22:05 UTC.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from test_window_validation import candle, snapshot

from johanna.analysis.sessions import EXNESS_T0_PROVISIONAL
from johanna.analysis.window import (
    ACCEPTED,
    PROVISIONAL,
    REJECTED,
    session_candle_check,
    validate_window,
)

SECONDS = {"M1": 60, "H1": 3600, "H4": 14400, "D1": 86400}


def u(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def check(open_time: str, timeframe: str) -> dict:
    return session_candle_check([u(open_time)], SECONDS[timeframe], EXNESS_T0_PROVISIONAL)


def kind(open_time: str, timeframe: str) -> str:
    result = check(open_time, timeframe)
    if result["unexpected"]:
        return "unexpected"
    return "overlap" if result["boundary_overlaps"] else "clear"


# ---------------------------------------------------------------- required cases
@pytest.mark.parametrize(
    ("label", "open_time", "timeframe", "expected"),
    [
        ("1 H1 inside Saturday closure", "2026-11-14T10:00:00", "H1", "unexpected"),
        ("2 H4 inside Saturday closure", "2026-11-14T08:00:00", "H4", "unexpected"),
        ("3 Friday H1 ending exactly at the close (EST)", "2026-11-13T21:00:00", "H1", "clear"),
        ("3b Friday H1 ending exactly at the close (EDT)", "2026-10-23T20:00:00", "H1", "clear"),
        ("4 Friday H4 across the close (EST)", "2026-11-13T20:00:00", "H4", "overlap"),
        ("4b Friday H4 across the close (EDT)", "2026-10-23T20:00:00", "H4", "overlap"),
        ("5 Sunday H1 across the reopen (EST)", "2026-11-15T22:00:00", "H1", "overlap"),
        ("5b Sunday H1 across the reopen (EDT)", "2026-10-25T21:00:00", "H1", "overlap"),
        ("6 Sunday H4 across the reopen (EST)", "2026-11-15T20:00:00", "H4", "overlap"),
        ("7 Sunday D1 00:00-24:00 UTC", "2026-11-15T00:00:00", "D1", "overlap"),
        ("8 Saturday D1 inside the weekend", "2026-11-14T00:00:00", "D1", "unexpected"),
        ("9 Christmas H1 inside the holiday closure", "2025-12-25T10:00:00", "H1", "unexpected"),
        ("10 Christmas H1 across the holiday reopen", "2025-12-25T22:00:00", "H1", "overlap"),
        ("11 Christmas D1 with open time after 22:05", "2025-12-25T00:00:00", "D1", "overlap"),
        ("11b Christmas Eve D1 (open until 22:00)", "2025-12-24T00:00:00", "D1", "overlap"),
        ("11c New Year H1 inside the closure", "2026-01-01T12:00:00", "H1", "unexpected"),
        ("13 Wednesday H1 outside all closures", "2026-11-11T10:00:00", "H1", "clear"),
        ("13b Wednesday D1", "2026-11-11T00:00:00", "D1", "clear"),
    ],
)
def test_required_boundary_cases(label, open_time, timeframe, expected) -> None:
    assert kind(open_time, timeframe) == expected, label


@pytest.mark.parametrize(
    ("label", "open_time", "timeframe", "expected"),
    [
        # US spring forward (Sunday 2026-03-08): closes in EST (22:00), reopens in EDT (21:05).
        ("Friday H1 21:00 before spring forward", "2026-03-06T21:00:00", "H1", "clear"),
        ("Friday H1 22:00 before spring forward", "2026-03-06T22:00:00", "H1", "unexpected"),
        ("Sunday H1 20:00 after spring forward", "2026-03-08T20:00:00", "H1", "unexpected"),
        ("Sunday H1 21:00 after spring forward", "2026-03-08T21:00:00", "H1", "overlap"),
        # US fall back (Sunday 2026-11-01): closes in EDT (21:00), reopens in EST (22:05).
        ("Friday H1 20:00 before fall back", "2026-10-30T20:00:00", "H1", "clear"),
        ("Friday H1 21:00 before fall back", "2026-10-30T21:00:00", "H1", "unexpected"),
        ("Sunday H1 21:00 after fall back", "2026-11-01T21:00:00", "H1", "unexpected"),
        ("Sunday H1 22:00 after fall back", "2026-11-01T22:00:00", "H1", "overlap"),
    ],
)
def test_us_daylight_saving_transitions(label, open_time, timeframe, expected) -> None:
    assert kind(open_time, timeframe) == expected, label


@pytest.mark.parametrize(
    ("open_time", "expected"),
    [
        ("2026-11-13T21:59:00", "clear"),  # [21:59, 22:00) ends exactly at the close
        ("2026-11-13T22:00:00", "unexpected"),  # starts exactly at the close
        ("2026-11-15T22:04:00", "unexpected"),  # [22:04, 22:05) ends exactly at the reopen
        ("2026-11-15T22:05:00", "clear"),  # starts exactly at the reopen
        ("2026-11-15T22:10:00", "clear"),  # reopen tolerance is open time, not closed time
    ],
)
def test_half_open_interval_boundaries(open_time, expected) -> None:
    assert kind(open_time, "M1") == expected


def test_open_timestamp_inside_a_closure_is_not_enough_to_reject() -> None:
    # The Sunday H1 opens at 22:00 (closed) but its interval reaches past the 22:05 reopen.
    assert kind("2026-11-15T22:00:00", "H1") == "overlap"


def test_monday_holiday_closes_the_whole_sunday() -> None:
    # 2023-12-25 (Monday): the holiday closure (Sunday 17:00 NY) joins the weekend closure.
    result = check("2023-12-24T00:00:00", "D1")
    (record,) = result["unexpected"]
    assert {c["kind"] for c in record["closures"]} == {"weekly", "holiday"}
    assert record["closed_parts"] == [["2023-12-24T00:00:00Z", "2023-12-25T00:00:00Z"]]


# ---------------------------------------------------------------- evidence
def test_unexpected_candle_evidence() -> None:
    (record,) = check("2026-11-14T10:00:00", "H1")["unexpected"]
    assert record == {
        "t": "2026-11-14T10:00:00Z",
        "interval": ["2026-11-14T10:00:00Z", "2026-11-14T11:00:00Z"],
        "closures": [
            {"kind": "weekly", "start": "2026-11-13T22:00:00Z", "end": "2026-11-15T22:05:00Z"}
        ],
        "closed_parts": [["2026-11-14T10:00:00Z", "2026-11-14T11:00:00Z"]],
        "open_parts": [],
    }


def test_boundary_overlap_evidence_explains_closed_and_open_parts() -> None:
    (record,) = check("2026-11-13T20:00:00", "H4")["boundary_overlaps"]
    assert record["closed_parts"] == [["2026-11-13T22:00:00Z", "2026-11-14T00:00:00Z"]]
    assert record["open_parts"] == [["2026-11-13T20:00:00Z", "2026-11-13T22:00:00Z"]]
    (sunday,) = check("2026-11-15T00:00:00", "D1")["boundary_overlaps"]
    assert sunday["open_parts"] == [["2026-11-15T22:05:00Z", "2026-11-16T00:00:00Z"]]


# ---------------------------------------------------------------- window integration
def H1(*times: str) -> list[dict]:
    return [candle(f"{t}Z") for t in times]


def test_boundary_overlap_is_informational_not_provisional() -> None:
    bars = H1(
        "2026-11-13T20:00:00", "2026-11-13T21:00:00", "2026-11-15T22:00:00", "2026-11-15T23:00:00"
    )
    result = validate_window(snapshot(bars), required_count=4)
    assert result["verdict"]["state"] == ACCEPTED, result["verdict"]
    session = result["dimensions"]["session_candles"]
    assert session["status"] == "pass" and session["reasons"] == []
    assert [o["t"] for o in session["evidence"]["boundary_overlaps"]] == ["2026-11-15T22:00:00Z"]


def test_window_with_unexpected_closure_candles_is_rejected_and_all_are_reported() -> None:
    bars = H1(
        "2026-11-13T20:00:00",
        "2026-11-13T21:00:00",
        "2026-11-14T10:00:00",
        "2026-11-14T11:00:00",
        "2026-11-15T22:00:00",
    )
    result = validate_window(snapshot(bars), required_count=5)
    assert result["verdict"]["state"] == REJECTED
    session = result["dimensions"]["session_candles"]
    assert session["status"] == "fail"
    (reason,) = session["reasons"]
    assert reason["code"] == "unexpected_candle_in_closure" and reason["timeframe"] == "H1"
    assert [c["t"] for c in reason["candles"]] == ["2026-11-14T10:00:00Z", "2026-11-14T11:00:00Z"]
    assert all(c["closures"][0]["kind"] == "weekly" for c in reason["candles"])
    assert {"dimension": "session_candles"}.items() <= result["verdict"]["reasons"][-1].items()
    raw = [b["t"] for b in bars]
    assert result["window"]["candles"] == raw  # nothing removed or altered
    json.dumps(result, allow_nan=False)


def test_unexpected_d1_saturday_rejects_a_d1_window() -> None:
    days = [candle(f"{d}T00:00:00Z") for d in ("2026-11-13", "2026-11-14", "2026-11-15")]
    result = validate_window(snapshot(days, timeframe="D1"), required_count=3)
    assert result["verdict"]["state"] == REJECTED
    assert result["dimensions"]["session_candles"]["status"] == "fail"
    assert result["dimensions"]["continuity"]["status"] == "pass"  # separate responsibilities


def test_existing_verdicts_are_unchanged_without_closure_candles() -> None:
    weekday = [candle(f"2026-11-10T{h:02d}:00:00Z") for h in range(10)]
    assert validate_window(snapshot(weekday), required_count=10)["verdict"]["state"] == ACCEPTED
    clock = list(weekday)
    clock[-1] = candle(clock[-1]["t"], closure="clock_only")
    provisional = validate_window(snapshot(clock), required_count=10)
    assert provisional["verdict"]["state"] == PROVISIONAL
    assert {r["code"] for r in provisional["verdict"]["reasons"]} == {"clock_only_closure"}
    bad = list(weekday)
    bad[3] = candle(bad[3]["t"], issues=["non_positive_price"])
    rejected = validate_window(snapshot(bad), required_count=10)
    # Existing behaviour: an invalid candle also makes the T2 snapshot provisional.
    assert {r["code"] for r in rejected["verdict"]["reasons"]} == {
        "invalid_ohlc",
        "snapshot_provisional",
    }
    assert rejected["verdict"]["state"] == REJECTED
    for result in (provisional, rejected):
        assert result["dimensions"]["session_candles"]["status"] == "pass"


def test_session_check_is_deterministic() -> None:
    times = [u("2026-11-13T20:00:00"), u("2026-11-14T10:00:00"), u("2026-11-15T22:00:00")]
    outputs = {
        json.dumps(session_candle_check(times, 3600, EXNESS_T0_PROVISIONAL), sort_keys=True)
        for _ in range(50)
    }
    assert len(outputs) == 1
