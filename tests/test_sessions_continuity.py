"""T3: session model and continuity classification (pure; no MT5, no terminal).

Boundary expectations are written as explicit UTC times taken from the T0 evidence
(docs/evidence/t0-exness-sessions/), not derived from the code under test.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, time, timedelta

import pytest

from johanna.analysis.continuity import (
    ANALYTICAL_EXCLUSION,
    CONFIGURED_CLOSURE,
    EXPECTED_NO_TICK,
    MARKET_WIDE_UNVERIFIED,
    SYMBOL_MISSING,
    UNKNOWN,
    analyse_continuity,
)
from johanna.analysis.sessions import (
    EXNESS_T0_PROVISIONAL,
    SessionConfig,
    closures_overlapping,
    in_rollover_window,
    is_scheduled_closed,
)


def u(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def bars(*times: str) -> list[dict]:
    return [{"t": t if t.endswith("Z") else f"{t}Z", "v": 10} for t in times]


def series(start: str, end: str, step_minutes: int, skip=()) -> list[dict]:
    """Every slot from start to end inclusive, except ``skip``."""
    out, moment, step = [], u(start), timedelta(minutes=step_minutes)
    skipped = {u(s) for s in skip}
    while moment <= u(end):
        if moment not in skipped:
            out.append({"t": iso(moment), "v": 10})
        moment += step
    return out


def only_gap(result: dict) -> dict:
    assert len(result["gaps"]) == 1, result["gaps"]
    return result["gaps"][0]


# ---------------------------------------------------------------- schedule and DST
@pytest.mark.parametrize(
    ("moment", "closed"),
    [
        ("2026-10-23T20:59:00", False),  # Friday, US summer time (EDT): close at 21:00 UTC
        ("2026-10-23T21:00:00", True),
        ("2026-10-25T21:04:00", True),  # Sunday reopen 17:05 New York = 21:05 UTC
        ("2026-10-25T21:05:00", False),
        ("2026-11-13T21:59:00", False),  # US winter time (EST): close at 22:00 UTC
        ("2026-11-13T22:00:00", True),
        ("2026-11-15T22:04:00", True),
        ("2026-11-15T22:05:00", False),
        ("2026-11-11T10:00:00", False),  # an ordinary Wednesday
        ("2026-11-14T12:00:00", True),  # Saturday
    ],
)
def test_weekly_closure_in_both_us_dst_states(moment, closed) -> None:
    assert is_scheduled_closed(u(moment)) is closed


@pytest.mark.parametrize(
    ("window", "close", "reopen"),
    [
        # US fall-back weekend: closes in EDT, reopens in EST.
        (("2026-10-30", "2026-11-02"), "2026-10-30T21:00:00", "2026-11-01T22:05:00"),
        (("2025-10-31", "2025-11-03"), "2025-10-31T21:00:00", "2025-11-02T22:05:00"),
        # US spring-forward weekend: closes in EST, reopens in EDT.
        (("2026-03-06", "2026-03-09"), "2026-03-06T22:00:00", "2026-03-08T21:05:00"),
        # EU changes do not move the New York schedule.
        (("2026-10-23", "2026-10-26"), "2026-10-23T21:00:00", "2026-10-25T21:05:00"),
        (("2026-03-27", "2026-03-30"), "2026-03-27T21:00:00", "2026-03-29T21:05:00"),
        (("2025-10-24", "2025-10-27"), "2025-10-24T21:00:00", "2025-10-26T21:05:00"),
    ],
)
def test_dst_transition_and_eu_change_weekends(window, close, reopen) -> None:
    (closure,) = [c for c in closures_overlapping(u(window[0]), u(window[1])) if c.kind == "weekly"]
    assert (closure.start, closure.end) == (u(close).replace(tzinfo=UTC), u(reopen))
    assert closure.reopen_deadline == u(reopen) + timedelta(minutes=20)


def test_weekly_gap_across_the_fall_back_weekend_is_a_configured_closure() -> None:
    result = analyse_continuity(bars("2026-10-30T20:00:00", "2026-11-01T22:00:00"), "H1")
    gap = only_gap(result)
    assert gap["classification"] == CONFIGURED_CLOSURE and gap["closure_kinds"] == ["weekly"]
    assert result["summary"]["verdict"] == "no_unexplained_gaps_detected"


def test_missing_first_hour_after_a_dst_reopen_is_not_a_closure() -> None:
    # Spring 2026: reopen Sunday 21:05 UTC, so the 21:00 H1 candle is expected.
    gap = only_gap(analyse_continuity(bars("2026-03-06T21:00:00", "2026-03-08T22:00:00"), "H1"))
    assert gap["classification"] == UNKNOWN and gap["required_missing_slots"] == 1


# ---------------------------------------------------------------- reopen tolerance
def test_late_first_candle_within_20_minutes_is_a_reopen_delay() -> None:
    gap = only_gap(analyse_continuity(bars("2026-10-23T20:59:00", "2026-10-25T21:24:00"), "M1"))
    assert gap["classification"] == CONFIGURED_CLOSURE
    assert gap["closure_kinds"] == ["reopen_delay", "weekly"]


def test_first_candle_beyond_the_tolerance_leaves_required_slots_missing() -> None:
    gap = only_gap(analyse_continuity(bars("2026-10-23T20:59:00", "2026-10-25T21:26:00"), "M1"))
    assert gap["classification"] == UNKNOWN
    assert gap["required_missing_slots"] == 1  # the 21:25 slot


# ---------------------------------------------------------------- rollover
@pytest.mark.parametrize(
    ("previous", "following", "expected"),
    [
        ("2026-11-03T21:59:00", "2026-11-03T22:05:00", EXPECTED_NO_TICK),  # EST 17:00-17:05 NY
        ("2026-10-20T20:59:00", "2026-10-20T21:05:00", EXPECTED_NO_TICK),  # EDT 17:00-17:05 NY
        ("2026-11-03T22:13:00", "2026-11-03T22:29:00", EXPECTED_NO_TICK),  # starts 17:14, 15 min
        ("2026-11-03T22:13:00", "2026-11-03T22:30:00", UNKNOWN),  # 16 min: above maximum
        ("2026-11-03T22:28:00", "2026-11-03T22:32:00", EXPECTED_NO_TICK),  # starts 17:29
        ("2026-11-03T22:29:00", "2026-11-03T22:33:00", UNKNOWN),  # starts 17:30: outside
        ("2026-11-03T20:59:00", "2026-11-03T21:05:00", UNKNOWN),  # 16:00 NY: outside
    ],
)
def test_rollover_window_and_maximum_duration(previous, following, expected) -> None:
    gap = only_gap(analyse_continuity(bars(previous, following), "M1"))
    assert gap["classification"] == expected, gap
    if expected == EXPECTED_NO_TICK:
        assert gap["possible_causes"] == ["daily_rollover_break"]


def test_rollover_rule_does_not_apply_to_hourly_candles() -> None:
    # A missing 22:00 H1 candle (EST, contains the rollover) is a missing observation.
    gap = only_gap(analyse_continuity(bars("2026-11-03T21:00:00", "2026-11-03T23:00:00"), "H1"))
    assert gap["classification"] == UNKNOWN


def test_rollover_window_uses_new_york_wall_time() -> None:
    assert in_rollover_window(u("2026-07-07T21:10:00"))  # 17:10 EDT
    assert in_rollover_window(u("2026-01-06T22:10:00"))  # 17:10 EST
    assert not in_rollover_window(u("2026-01-06T21:10:00"))


# ---------------------------------------------------------------- holidays
@pytest.mark.parametrize(
    ("previous", "following", "kinds"),
    [
        ("2025-12-24T21:00:00", "2025-12-25T22:00:00", ["holiday"]),  # Thursday Christmas
        ("2024-12-31T21:00:00", "2025-01-01T22:00:00", ["holiday"]),  # Wednesday New Year
        ("2023-12-22T21:00:00", "2023-12-25T22:00:00", ["holiday", "weekly"]),  # Monday
    ],
)
def test_christmas_and_new_year_closures(previous, following, kinds) -> None:
    gap = only_gap(analyse_continuity(bars(previous, following), "H1"))
    assert gap["classification"] == CONFIGURED_CLOSURE and gap["closure_kinds"] == kinds


def test_no_other_holidays_are_assumed() -> None:
    # 2026-07-03/04 (US Independence Day): no configured closure, so a missing day is unknown.
    gap = only_gap(analyse_continuity(bars("2026-07-02T20:00:00", "2026-07-03T20:00:00"), "H1"))
    assert gap["classification"] == UNKNOWN


# ---------------------------------------------------------------- unexplained gaps
def test_gap_inside_the_session_is_never_reported_as_continuous() -> None:
    result = analyse_continuity(
        series("2026-11-10T00:00:00", "2026-11-10T20:00:00", 60, skip=["2026-11-10T10:00:00"]), "H1"
    )
    gap = only_gap(result)
    assert gap["classification"] == UNKNOWN
    assert gap["possible_causes"] == ["feed_interruption", "missing_history"]
    assert result["summary"]["verdict"] == "unknown"
    assert result["candles"][10]["gap_before"] == UNKNOWN


def test_market_wide_versus_symbol_specific_gaps() -> None:
    hole = "2026-11-11T10:00:00"
    mine = series("2026-11-11T00:00:00", "2026-11-11T20:00:00", 60, skip=[hole])
    peer_full = [b["t"] for b in series("2026-11-11T00:00:00", "2026-11-11T20:00:00", 60)]
    peer_hole = [b["t"] for b in mine]
    shared = analyse_continuity(mine, "H1", peers={"GBPUSD": peer_hole, "USDJPY": peer_hole})
    gap = only_gap(shared)
    assert gap["classification"] == MARKET_WIDE_UNVERIFIED  # simultaneous: NOT a closure
    assert gap["peers_without_data"] == ["GBPUSD", "USDJPY"]
    assert shared["summary"]["verdict"] == "unexplained_gaps"
    specific = only_gap(analyse_continuity(mine, "H1", peers={"GBPUSD": peer_full}))
    assert specific["classification"] == SYMBOL_MISSING
    assert specific["peers_with_data"] == ["GBPUSD"]
    # A peer whose data does not span the gap cannot inform it.
    short_peer = [t for t in peer_full if t > "2026-11-11T12"]
    assert (
        only_gap(analyse_continuity(mine, "H1", peers={"X": short_peer}))["classification"]
        == UNKNOWN
    )


def test_m1_gap_reports_no_tick_versus_interruption_uncertainty() -> None:
    gap = only_gap(analyse_continuity(bars("2026-11-11T10:00:00", "2026-11-11T10:03:00"), "M1"))
    assert gap["classification"] == UNKNOWN and gap["certainty"] == "uncertain"
    assert gap["possible_causes"] == ["no_tick_interval", "feed_interruption"]
    peer = bars("2026-11-11T10:00:00", "2026-11-11T10:01:00", "2026-11-11T10:02:00")
    specific = only_gap(
        analyse_continuity(
            bars("2026-11-11T10:00:00", "2026-11-11T10:03:00"),
            "M1",
            peers={"GBPUSD": [b["t"] for b in peer] + ["2026-11-11T10:03:00Z"]},
        )
    )
    assert specific["classification"] == SYMBOL_MISSING
    assert specific["possible_causes"] == ["no_tick_interval", "feed_interruption"]


def test_random_in_session_gaps_are_never_silently_continuous() -> None:
    rng = random.Random(20261112)
    week = series("2026-11-09T00:00:00", "2026-11-13T21:00:00", 60)  # Mon 00:00 - Fri 21:00 EST
    for _ in range(50):
        holes = sorted(rng.sample(range(1, len(week) - 1), rng.randint(1, 4)))
        remaining = [b for i, b in enumerate(week) if i not in holes]
        result = analyse_continuity(remaining, "H1")
        assert result["summary"]["verdict"] != "no_unexplained_gaps_detected"
        assert all(g["classification"] == UNKNOWN for g in result["gaps"])


# ---------------------------------------------------------------- expected schedules
def test_h1_week_schedule_has_only_the_weekend_closure() -> None:
    # EST week: Sunday 22:00 first candle (reopen 22:05) ... Friday 21:00 last candle.
    week = series("2026-11-15T22:00:00", "2026-11-20T21:00:00", 60)
    following = series("2026-11-22T22:00:00", "2026-11-22T23:00:00", 60)
    result = analyse_continuity(week + following, "H1")
    assert [g["classification"] for g in result["gaps"]] == [CONFIGURED_CLOSURE]
    assert result["summary"]["verdict"] == "no_unexplained_gaps_detected"


def test_h4_week_schedule() -> None:
    # EDT week: Sunday 20:00 H4 (contains reopen 21:05) ... Friday 20:00 (close 21:00).
    week = series("2026-10-18T20:00:00", "2026-10-23T20:00:00", 240)
    result = analyse_continuity(week + bars("2026-10-25T20:00:00"), "H4")
    assert [g["classification"] for g in result["gaps"]] == [CONFIGURED_CLOSURE]
    missing_friday = [b for b in week if b["t"] != "2026-10-23T20:00:00Z"]
    result = analyse_continuity(missing_friday + bars("2026-10-25T20:00:00"), "H4")
    assert result["gaps"][-1]["classification"] == UNKNOWN  # Friday 20:00 H4 is required


def test_d1_schedule_sunday_candles_and_friday_to_monday() -> None:
    days = bars(
        "2026-11-06T00:00:00",  # Friday
        "2026-11-08T00:00:00",  # Sunday (partial day, reopen to midnight UTC)
        "2026-11-09T00:00:00",  # Monday
    )
    result = analyse_continuity(days, "D1")
    assert [g["classification"] for g in result["gaps"]] == [CONFIGURED_CLOSURE]  # Saturday
    assert result["candles"][1]["notes"] == ["sunday_partial_day"]
    assert result["candles"][2]["gap_before"] is None
    # Without the Sunday candle the Friday-to-Monday step leaves a required day missing.
    no_sunday = analyse_continuity([days[0], days[2]], "D1")
    assert only_gap(no_sunday)["classification"] == UNKNOWN
    # A caller that deliberately excluded it (future analysis policy) gets no data gap.
    excluded = analyse_continuity([days[0], days[2]], "D1", excluded_opens=["2026-11-08T00:00:00Z"])
    gap = only_gap(excluded)
    assert gap["classification"] == ANALYTICAL_EXCLUSION and gap["excluded_slots"] == 1
    assert excluded["summary"]["verdict"] == "no_unexplained_gaps_detected"


def test_d1_holiday_days_are_expected_candles() -> None:
    # Christmas Day reopens at ~22:05 UTC, so its D1 candle exists (T0: 2 H1 bars).
    days = bars("2025-12-24T00:00:00", "2025-12-26T00:00:00")
    assert only_gap(analyse_continuity(days, "D1"))["classification"] == UNKNOWN


# ---------------------------------------------------------------- inputs and determinism
def test_missing_and_malformed_timestamps_are_reported() -> None:
    result = analyse_continuity(
        [
            {"t": "2026-11-11T10:00:00Z"},
            {"t": None},
            {"t": "not a time"},
            {"t": "2026-11-11T11:00:00"},  # no timezone
            {"t": "2026-11-11T11:30:00Z"},  # not on the H1 grid
            {"t": "2026-11-11T12:00:00Z"},
            {"t": "2026-11-11T12:00:00Z"},  # duplicate
            {"t": "2026-11-11T11:00:00Z"},  # out of order
            {},
        ],
        "H1",
    )
    errors = [e["error"] for e in result["input_errors"]]
    assert errors == [
        "missing_or_malformed_timestamp",
        "missing_or_malformed_timestamp",
        "missing_or_malformed_timestamp",
        "not_aligned_to_timeframe",
        "duplicate_timestamp",
        "out_of_order",
        "missing_or_malformed_timestamp",
    ]
    assert result["summary"]["verdict"] == "unknown"
    assert [c["t"] for c in result["candles"]] == [
        "2026-11-11T10:00:00Z",
        "2026-11-11T11:00:00Z",
        "2026-11-11T12:00:00Z",
    ]


def test_tick_volume_is_noted_but_never_proof_of_coverage() -> None:
    result = analyse_continuity(
        [{"t": "2026-11-11T10:00:00Z", "v": 0}, {"t": "2026-11-11T11:00:00Z", "v": 500}], "H1"
    )
    assert result["candles"][0]["notes"] == ["first_in_window", "zero_tick_volume"]
    assert result["summary"]["verdict"] == "no_unexplained_gaps_detected"
    assert "not proof" in result["summary"]["note"]


def test_results_are_deterministic() -> None:
    week = series("2026-11-09T00:00:00", "2026-11-13T21:00:00", 60, skip=["2026-11-11T05:00:00"])
    peers = {"GBPUSD": [b["t"] for b in week]}
    outputs = {
        json.dumps(analyse_continuity(week, "H1", peers=peers), sort_keys=True) for _ in range(50)
    }
    assert len(outputs) == 1


def test_configuration_is_explicit_validated_and_evidenced() -> None:
    config = EXNESS_T0_PROVISIONAL
    assert config.zone == "America/New_York" and config.provisional is True
    assert (config.weekly_close_time, config.weekly_reopen_time) == (time(17, 0), time(17, 5))
    assert config.reopen_tolerance == timedelta(minutes=20)
    assert config.rollover_max == timedelta(minutes=15)
    assert config.holiday_closures == ((12, 25), (1, 1))
    assert {key for key, _ in config.evidence} >= {"weekly_close", "rollover", "holidays", "dst"}
    with pytest.raises(Exception):
        SessionConfig(**{**config.__dict__, "zone": "Not/AZone"})
    with pytest.raises(ValueError):
        SessionConfig(**{**config.__dict__, "rollover_end": time(16, 0)})
    with pytest.raises(ValueError):
        analyse_continuity([], "W1")


def test_alternative_configuration_changes_the_classification() -> None:
    no_holidays = SessionConfig(**{**EXNESS_T0_PROVISIONAL.__dict__, "holiday_closures": ()})
    gap = only_gap(
        analyse_continuity(
            bars("2025-12-24T21:00:00", "2025-12-25T22:00:00"), "H1", config=no_holidays
        )
    )
    assert gap["classification"] == UNKNOWN  # only configured rules can explain a gap
