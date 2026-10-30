from datetime import UTC, datetime, timedelta, timezone

import pytest

from johanna.time import (
    AmbiguousLocalTimeError,
    BarTimestampConvention,
    NonexistentLocalTimeError,
    from_utc,
    parse_bar_timestamp_convention,
    strictly_increasing_utc,
    to_utc,
)

UTC = UTC


def test_aware_timestamp_normalizes_to_utc() -> None:
    local = datetime(2025, 1, 15, 12, tzinfo=timezone(timedelta(hours=2)))

    assert to_utc(local) == datetime(2025, 1, 15, 10, tzinfo=UTC)


def test_converts_using_explicit_source_and_target_timezones() -> None:
    utc_value = to_utc(datetime(2025, 1, 15, 12), "Africa/Johannesburg")

    assert utc_value == datetime(2025, 1, 15, 10, tzinfo=UTC)
    assert from_utc(utc_value, "Asia/Tokyo").isoformat() == "2025-01-15T19:00:00+09:00"


def test_daylight_saving_offsets_follow_timezone_database() -> None:
    winter = to_utc(datetime(2025, 1, 15, 12), "America/New_York")
    summer = to_utc(datetime(2025, 7, 15, 12), "America/New_York")

    assert winter == datetime(2025, 1, 15, 17, tzinfo=UTC)
    assert summer == datetime(2025, 7, 15, 16, tzinfo=UTC)


def test_ambiguous_dst_time_requires_fold_and_converts_both_occurrences() -> None:
    repeated = datetime(2025, 11, 2, 1, 30)

    with pytest.raises(AmbiguousLocalTimeError):
        to_utc(repeated, "America/New_York")

    assert to_utc(repeated, "America/New_York", fold=0) == datetime(2025, 11, 2, 5, 30, tzinfo=UTC)
    assert to_utc(repeated, "America/New_York", fold=1) == datetime(2025, 11, 2, 6, 30, tzinfo=UTC)


def test_nonexistent_dst_time_is_rejected() -> None:
    with pytest.raises(NonexistentLocalTimeError):
        to_utc(datetime(2025, 3, 9, 2, 30), "America/New_York")


@pytest.mark.parametrize(
    ("value", "source_timezone"),
    [
        (datetime(2025, 1, 1), None),
        (datetime(2025, 1, 1), "Not/A_Timezone"),
    ],
)
def test_naive_or_invalid_timezone_input_is_rejected(
    value: datetime, source_timezone: str | None
) -> None:
    with pytest.raises(ValueError):
        to_utc(value, source_timezone)


def test_timestamp_order_is_normalized_and_strict() -> None:
    result = strictly_increasing_utc(
        [
            datetime(2025, 1, 1, 12, tzinfo=timezone(timedelta(hours=2))),
            datetime(2025, 1, 1, 11, tzinfo=UTC),
        ]
    )

    assert result == (
        datetime(2025, 1, 1, 10, tzinfo=UTC),
        datetime(2025, 1, 1, 11, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "values",
    [
        [datetime(2025, 1, 1, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)],
        [datetime(2025, 1, 2, tzinfo=UTC), datetime(2025, 1, 1, tzinfo=UTC)],
    ],
)
def test_timestamp_order_rejects_duplicate_or_decreasing_values(
    values: list[datetime],
) -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        strictly_increasing_utc(values)


def test_bar_timestamp_convention_must_be_declared() -> None:
    assert parse_bar_timestamp_convention("bar_open") is BarTimestampConvention.BAR_OPEN
    assert parse_bar_timestamp_convention("bar_close") is BarTimestampConvention.BAR_CLOSE

    with pytest.raises(ValueError, match="explicitly declared"):
        parse_bar_timestamp_convention("unknown")
