"""Broker session model (T3): when the market is scheduled to be closed.

Pure and deterministic. All rules are expressed in one IANA zone (``America/New_York`` for
Exness, per T0) and converted to UTC per date with ``zoneinfo``, so US daylight-saving changes
move the UTC times automatically, and EU changes do not affect them. Session boundaries fall at
17:00/17:05 local time, never inside a DST transition (02:00), so no local time used here is
ambiguous or nonexistent.

The configuration is *provisional and evidence-based*: it describes what T0 observed in Exness
history, not a guarantee about future sessions. Every rule carries its evidence; anything the
rules do not explain stays unexplained (see ``johanna.analysis.continuity``).

Holiday closures run from the weekly-close time on the eve to the reopen time on the holiday
(T0: 17:00 New York on 24/31 December to about 17:05 on 25 December / 1 January). No other
holidays, early closes or special sessions are assumed.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

EVIDENCE_PATH = "docs/evidence/t0-exness-sessions/"


@dataclass(frozen=True)
class SessionConfig:
    """Weekly closure, reopen tolerance, daily rollover window and full-day holiday closures."""

    name: str
    zone: str
    weekly_close_weekday: int  # Monday = 0 ... Sunday = 6
    weekly_close_time: time
    weekly_reopen_weekday: int
    weekly_reopen_time: time
    # The first candle after a reopen may appear this long after the scheduled reopen.
    reopen_tolerance: timedelta
    # Expected short intraday break (no ticks): gaps starting in [start, end) local time,
    # lasting at most ``rollover_max``.
    rollover_start: time
    rollover_end: time
    rollover_max: timedelta
    holiday_closures: tuple[tuple[int, int], ...]  # (month, day), full-day closures
    evidence: tuple[tuple[str, str], ...]
    provisional: bool = True

    def __post_init__(self) -> None:
        ZoneInfo(self.zone)  # raises for an unknown zone
        for weekday in (self.weekly_close_weekday, self.weekly_reopen_weekday):
            if not 0 <= weekday <= 6:
                raise ValueError("weekdays are 0 (Monday) to 6 (Sunday)")
        if self.reopen_tolerance < timedelta(0) or self.rollover_max < timedelta(0):
            raise ValueError("tolerances must not be negative")
        if self.rollover_end <= self.rollover_start:
            raise ValueError("rollover window must end after it starts")
        for month, day in self.holiday_closures:
            date(2024, month, day)  # raises for an impossible date (2024 is a leap year)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.zone)


EXNESS_T0_PROVISIONAL = SessionConfig(
    name="exness-t0-provisional-2026-11-12",
    zone="America/New_York",
    weekly_close_weekday=4,
    weekly_close_time=time(17, 0),
    weekly_reopen_weekday=6,
    weekly_reopen_time=time(17, 5),
    reopen_tolerance=timedelta(minutes=20),
    rollover_start=time(17, 0),
    rollover_end=time(17, 30),
    rollover_max=timedelta(minutes=15),
    holiday_closures=((12, 25), (1, 1)),
    evidence=(
        (
            "weekly_close",
            "Friday 17:00 New York: last M1 bar 16:59; M5 58 weekends (2025-10-17..2026-11-06), "
            "H1 141-255 weekends (2022/2024..2026), 7 symbols; unchanged across US DST.",
        ),
        (
            "weekly_reopen",
            "Sunday 17:05 New York: first M5/M1 bar at 17:05 in most weeks; later first bars "
            "seen up to 17:15 (USDJPY), once 17:25 (spring-forward weekend); tolerance 20 min.",
        ),
        (
            "dst",
            "Session follows US DST (UTC times shift on 2025-11-02, 2026-03-08, 2026-11-01); "
            "EU changes (2025-03-30, 2025-10-26, 2026-03-29, 2026-10-25) do not move it.",
        ),
        (
            "rollover",
            "All intraday gaps (M1 21 days x 7 symbols; M5 ~281 days x 3 symbols) started "
            "17:00-17:30 New York; 0 outside; max 15 min (EURUSD M1); absent on many days.",
        ),
        (
            "holidays",
            "Full-day closures on 25 Dec and 1 Jan, 17:00 New York on the eve to ~17:05: "
            "2023-12-25, 2024-01-01 (EURUSD only), 2024-12-25, 2025-01-01, 2025-12-25, "
            "2026-01-01 (7 symbols). No other holiday closures or early closes observed; "
            "minute-level holiday data only from 2025-10.",
        ),
        ("source", f"T0 read-only verification, Exness-MT5Real21, 2026-11-12; {EVIDENCE_PATH}"),
    ),
)


@dataclass(frozen=True)
class Closure:
    """A scheduled closure in UTC. ``reopen_deadline`` = end + reopen tolerance."""

    kind: str  # "weekly" | "holiday"
    start: datetime
    end: datetime
    reopen_deadline: datetime


def _local(day: date, moment: time, config: SessionConfig) -> datetime:
    return datetime.combine(day, moment, tzinfo=config.tz).astimezone(UTC)


def _days(first: date, last: date) -> Iterator[date]:
    day = first
    while day <= last:
        yield day
        day += timedelta(days=1)


def closures_overlapping(
    start: datetime, end: datetime, config: SessionConfig = EXNESS_T0_PROVISIONAL
) -> list[Closure]:
    """Scheduled closures whose span, including the reopen tolerance, overlaps [start, end)."""
    if start.tzinfo is None or end.tzinfo is None:
        raise ValueError("start and end must be timezone-aware")
    tz = config.tz
    first = start.astimezone(tz).date() - timedelta(days=8)
    last = end.astimezone(tz).date() + timedelta(days=8)
    closures: list[Closure] = []
    reopen_after = (config.weekly_reopen_weekday - config.weekly_close_weekday) % 7
    for day in _days(first, last):
        if day.weekday() == config.weekly_close_weekday:
            closures.append(
                _closure(
                    "weekly",
                    _local(day, config.weekly_close_time, config),
                    _local(day + timedelta(days=reopen_after), config.weekly_reopen_time, config),
                    config,
                )
            )
    for year in range(first.year, last.year + 1):
        for month, day_of_month in config.holiday_closures:
            holiday = date(year, month, day_of_month)
            if not first <= holiday <= last:
                continue
            closures.append(
                _closure(
                    "holiday",
                    _local(holiday - timedelta(days=1), config.weekly_close_time, config),
                    _local(holiday, config.weekly_reopen_time, config),
                    config,
                )
            )
    return sorted(
        (c for c in closures if c.start < end and c.reopen_deadline > start),
        key=lambda c: (c.start, c.kind),
    )


def _closure(kind: str, start: datetime, end: datetime, config: SessionConfig) -> Closure:
    return Closure(kind, start, end, end + config.reopen_tolerance)


def is_scheduled_closed(moment: datetime, config: SessionConfig = EXNESS_T0_PROVISIONAL) -> bool:
    """Whether ``moment`` falls inside a scheduled closure (tolerance not included)."""
    window_end = moment + timedelta(microseconds=1)
    return any(c.start <= moment < c.end for c in closures_overlapping(moment, window_end, config))


def in_rollover_window(moment: datetime, config: SessionConfig = EXNESS_T0_PROVISIONAL) -> bool:
    """Whether ``moment`` (local wall time) lies in the configured rollover window."""
    local = moment.astimezone(config.tz).time()
    return config.rollover_start <= local < config.rollover_end
