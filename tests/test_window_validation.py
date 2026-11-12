"""T4: analysis-window validation (pure; no MT5, no terminal, no data/ access).

Snapshots are built with T2's ``build_snapshot`` from hand-written bars responses in the T1
wire format, so every case is explicit. One integration test runs the real LiveMarketService
over the in-memory FakeMT5.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fake_mt5 import FakeMT5

from johanna.adapters.mt5 import MetaTrader5Adapter
from johanna.analysis.snapshot import build_snapshot
from johanna.analysis.window import (
    ACCEPTED,
    D1_CONVENTION,
    PROVISIONAL,
    REJECTED,
    validate_window,
)
from johanna.live_market import LiveMarketService

BASE = datetime(2026, 11, 11, 14, 0, 30, tzinfo=UTC)  # assembly reference (Wednesday)
STEP = {"M1": 60, "H1": 3600, "D1": 86400}


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def candle(
    t, *, o=1.1, h=1.2, low=1.0, c=1.15, v=10, spread=7, closure="tick_observed", issues=()
) -> dict:
    t = t if isinstance(t, str) else iso(t)
    return {
        "t": t,
        "o": o,
        "h": h,
        "l": low,
        "c": c,
        "v": v,
        "spread_points": spread,
        "close_t": None,
        "complete": closure != "forming",
        "closure": closure,
        "valid": not issues,
        "issues": list(issues),
    }


def hourly(start: str, count: int, step: str = "H1") -> list[dict]:
    first = datetime.fromisoformat(start).replace(tzinfo=UTC)
    return [candle(first + timedelta(seconds=STEP[step] * i)) for i in range(count)]


def snapshot(candles, *, timeframe="H1", rejected=(), tick_age=0.5, **overrides) -> dict:
    """A T2 snapshot from a bars response; live unless the inputs say otherwise."""
    invalid = sum(
        1 for c in candles if c.get("issues") and set(c["issues"]) - {"duplicate_timestamp"}
    )
    payload = {
        "symbol": "EURUSD",
        "broker_symbol": "EURUSDc",
        "timeframe": timeframe,
        "digits": 5,
        "point": 0.00001,
        "pip_size": 0.0001,
        "bars": list(candles),
        "rejected_bars": list(rejected),
        "quality": {
            "bars_received": len(candles) + len(rejected),
            "bars_returned": len(candles),
            "invalid_bars": invalid,
            "duplicates_rejected": len(rejected),
            "out_of_order": 0,
        },
        "latest_tick_utc": iso(BASE - timedelta(seconds=tick_age)),
        "tick": {
            "bid": 1.15,
            "ask": 1.1501,
            "time_utc": iso(BASE - timedelta(seconds=tick_age)),
            "retrieved_at_utc": iso(BASE),
        },
        "retrieved_at_utc": iso(BASE + timedelta(seconds=0.2)),
        "requested_at_utc": iso(BASE + timedelta(seconds=0.3)),
        "served_from_cache": False,
        "source": "mt5",
        "time_basis": "utc",
        "status": "LIVE",
        "reason": None,
        "message": None,
    }
    payload.update(overrides)
    return build_snapshot(payload, snapshot_id="snap-1", assembled_at=BASE + timedelta(seconds=0.5))


def codes(result, dimension=None) -> set[str]:
    return {
        r["code"]
        for r in result["verdict"]["reasons"]
        if dimension is None or r["dimension"] == dimension
    }


def status(result, dimension) -> str:
    return result["dimensions"][dimension]["status"]


WEEKDAY = "2026-11-10T00:00:00"  # Tuesday: 24 clean H1 candles fit inside one session day


# ---------------------------------------------------------------- baseline
def test_clean_live_window_is_accepted_with_every_dimension_reported() -> None:
    result = validate_window(snapshot(hourly(WEEKDAY, 20)), required_count=20)
    assert result["verdict"]["state"] == ACCEPTED, result["verdict"]
    assert set(result["dimensions"]) == {
        "price",
        "timestamps",
        "closure",
        "continuity",
        "completeness",
        "snapshot",
        "session_candles",  # T4.1: the required seventh dimension
    }
    assert all(d["status"] == "pass" for d in result["dimensions"].values())
    assert result["window"]["count"] == 20
    assert "not proof" in result["note"] and "trading readiness" in result["note"]
    json.dumps(result, allow_nan=False)


# ---------------------------------------------------------------- duplicates
def test_identical_duplicates_keep_one_canonical_candle_and_are_recorded() -> None:
    bars = hourly(WEEKDAY, 10)
    dup = {k: bars[4][k] for k in ("t", "o", "h", "l", "c", "v", "spread_points")}
    result = validate_window(
        snapshot(
            bars, rejected=[{**dup, "reason": "duplicate_timestamp", "identical_to_kept": True}]
        ),
        required_count=10,
    )
    assert result["verdict"]["state"] == PROVISIONAL
    assert codes(result) == {"identical_duplicates_collapsed"}
    (record,) = result["dimensions"]["timestamps"]["evidence"]["duplicates"]
    assert record == {
        **record,
        "t": bars[4]["t"],
        "occurrences": 2,
        "ohlc_identical": True,
        "metadata_identical": True,
        "policy": "canonical_retained",
    }
    assert result["window"]["count"] == 10  # one canonical candle per open time


def test_conflicting_duplicates_reject_the_whole_window() -> None:
    bars = hourly(WEEKDAY, 10)
    conflict = {
        "t": bars[3]["t"],
        "o": 1.1,
        "h": 1.25,
        "l": 1.0,
        "c": 1.2,
        "v": 10,
        "spread_points": 7,
        "reason": "duplicate_timestamp",
        "identical_to_kept": False,
    }
    result = validate_window(snapshot(bars, rejected=[conflict]), required_count=10)
    assert result["verdict"]["state"] == REJECTED
    (reason,) = [r for r in result["verdict"]["reasons"] if r["code"] == "conflicting_duplicates"]
    (record,) = reason["duplicates"]
    assert record["policy"] == "window_rejected" and record["ohlc_identical"] is False
    assert [v["c"] for v in record["values"]] == [1.15, 1.2]  # both values reported, none chosen


@pytest.mark.parametrize(("field", "value"), [("v", 99), ("spread_points", 12)])
def test_same_ohlc_with_differing_metadata_is_not_a_price_conflict(field, value) -> None:
    bars = hourly(WEEKDAY, 10)
    dup = {k: bars[2][k] for k in ("t", "o", "h", "l", "c", "v", "spread_points")}
    dup[field] = value
    result = validate_window(
        snapshot(
            bars, rejected=[{**dup, "reason": "duplicate_timestamp", "identical_to_kept": False}]
        ),
        required_count=10,
    )
    assert codes(result, "timestamps") == {"duplicate_metadata_differs"}
    assert result["verdict"]["state"] == PROVISIONAL
    assert "conflicting_duplicates" not in codes(result)


def test_multiple_conflicting_duplicates_are_all_reported() -> None:
    bars = hourly(WEEKDAY, 10)
    rejected = [
        {
            "t": bars[i]["t"],
            "o": 1.3,
            "h": 1.4,
            "l": 1.2,
            "c": 1.35,
            "v": 1,
            "spread_points": 7,
            "reason": "duplicate_timestamp",
            "identical_to_kept": False,
        }
        for i in (1, 5, 8)
    ]
    result = validate_window(snapshot(bars, rejected=rejected), required_count=10)
    (reason,) = [r for r in result["verdict"]["reasons"] if r["code"] == "conflicting_duplicates"]
    assert [d["t"] for d in reason["duplicates"]] == [bars[i]["t"] for i in (1, 5, 8)]


def test_duplicates_in_the_candle_list_itself_are_handled_too() -> None:
    bars = hourly(WEEKDAY, 6)
    bars.insert(3, dict(bars[2], c=1.19))  # same open time, different close, inside items
    result = validate_window(snapshot(bars), required_count=6)
    assert "conflicting_duplicates" in codes(result) and "out_of_order" not in codes(result)


def test_duplicate_results_are_deterministic() -> None:
    bars = hourly(WEEKDAY, 10)
    rejected = [
        {
            "t": bars[2]["t"],
            "o": 1.1,
            "h": 1.2,
            "l": 1.0,
            "c": 1.15,
            "v": 3,
            "spread_points": 7,
            "reason": "duplicate_timestamp",
            "identical_to_kept": False,
        }
    ]
    snap = snapshot(bars, rejected=rejected)
    outputs = {
        json.dumps(validate_window(snap, required_count=10), sort_keys=True) for _ in range(50)
    }
    assert len(outputs) == 1


# ---------------------------------------------------------------- price / OHLC validity
@pytest.mark.parametrize(
    "issue",
    [
        "non_finite_price",
        "non_positive_price",
        "high_below_open_close_or_low",
        "low_above_open_close_or_high",
        "negative_tick_volume",
    ],
)
def test_every_t1_price_failure_rejects_the_window(issue) -> None:
    bars = hourly(WEEKDAY, 10)
    bars[6] = candle(bars[6]["t"], issues=[issue])
    result = validate_window(snapshot(bars), required_count=10)
    assert result["verdict"]["state"] == REJECTED
    assert codes(result, "price") == {"invalid_ohlc"}
    assert status(result, "timestamps") == "pass"  # price problems stay in their dimension


def test_multiple_and_mixed_invalid_candles() -> None:
    bars = hourly(WEEKDAY, 10)
    bars[1] = candle(bars[1]["t"], c=None, issues=["non_finite_price"])
    bars[7] = candle(bars[7]["t"], h=0.9, issues=["high_below_open_close_or_low"])
    result = validate_window(snapshot(bars), required_count=10)
    (reason,) = [r for r in result["verdict"]["reasons"] if r["code"] == "invalid_ohlc"]
    assert [c["t"] for c in reason["candles"]] == [bars[1]["t"], bars[7]["t"]]


def test_invalid_candle_does_not_disappear_behind_an_identical_duplicate() -> None:
    bars = hourly(WEEKDAY, 10)
    bars[4] = candle(
        bars[4]["t"], h=0.9, issues=["high_below_open_close_or_low", "duplicate_timestamp"]
    )
    dup = {k: bars[4][k] for k in ("t", "o", "h", "l", "c", "v", "spread_points")}
    result = validate_window(
        snapshot(
            bars, rejected=[{**dup, "reason": "duplicate_timestamp", "identical_to_kept": True}]
        ),
        required_count=10,
    )
    assert result["verdict"]["state"] == REJECTED and "invalid_ohlc" in codes(result, "price")


def test_out_of_order_candle_is_a_timestamp_failure_not_a_price_failure() -> None:
    bars = hourly(WEEKDAY, 10)
    bars[5] = candle(bars[5]["t"], issues=["out_of_order"])
    result = validate_window(snapshot(bars), required_count=10)
    assert result["verdict"]["state"] == REJECTED
    assert status(result, "price") == "pass" and codes(result, "timestamps") == {"out_of_order"}


# ---------------------------------------------------------------- timestamp validity
@pytest.mark.parametrize(
    ("value", "error"),
    [
        (None, "missing"),
        ("", "missing"),
        ("yesterday", "malformed_or_timezone_naive"),
        ("2026-11-10T05:00:00", "malformed_or_timezone_naive"),
        ("2026-11-10T05:30:00Z", "not_aligned_to_timeframe"),
    ],
)
def test_invalid_timestamps_reject_the_window(value, error) -> None:
    bars = hourly(WEEKDAY, 10)
    bars.append(dict(candle(bars[0]["t"]), t=value))
    result = validate_window(snapshot(bars), required_count=10)
    assert result["verdict"]["state"] == REJECTED
    (reason,) = [r for r in result["verdict"]["reasons"] if r["code"] == "invalid_timestamps"]
    assert reason["candles"][0]["error"] == error


def test_out_of_order_candle_list_is_rejected() -> None:
    bars = hourly(WEEKDAY, 10)
    bars[3], bars[4] = bars[4], bars[3]
    result = validate_window(snapshot(bars), required_count=10)
    assert "out_of_order" in codes(result, "timestamps") and result["verdict"]["state"] == REJECTED


# ---------------------------------------------------------------- completeness
def test_window_completeness() -> None:
    bars = hourly(WEEKDAY, 20) + [candle("2026-11-10T20:00:00Z", closure="forming")]
    exact = validate_window(snapshot(bars), required_count=20)
    assert exact["verdict"]["state"] == ACCEPTED and exact["window"]["count"] == 20
    assert exact["excluded_candles"] == [{"t": "2026-11-10T20:00:00Z", "reason": "forming"}]
    short = validate_window(snapshot(bars), required_count=21)
    assert codes(short, "completeness") == {"insufficient_history"}
    assert short["dimensions"]["completeness"]["evidence"]["available_closed"] == 20
    warm = validate_window(snapshot(bars), required_count=15, warmup_count=10)
    assert codes(warm, "completeness") == {"insufficient_warmup"}
    assert warm["window"]["count"] == 20  # never silently shortened to "what is there"
    empty = validate_window(snapshot([]), required_count=5)
    assert "empty_window" in codes(empty, "completeness") and empty["verdict"]["state"] == REJECTED


def test_window_takes_the_newest_closed_candles() -> None:
    result = validate_window(snapshot(hourly(WEEKDAY, 20)), required_count=5, warmup_count=3)
    assert result["window"]["first_t"] == "2026-11-10T12:00:00Z"
    assert result["window"]["last_t"] == "2026-11-10T19:00:00Z"


def test_complete_window_with_invalid_data_is_still_rejected() -> None:
    bars = hourly(WEEKDAY, 20)
    bars[10] = candle(bars[10]["t"], issues=["non_positive_price"])
    result = validate_window(snapshot(bars), required_count=20)
    assert status(result, "completeness") == "pass" and result["verdict"]["state"] == REJECTED


@pytest.mark.parametrize("bad", [0, -1, 1.5, True])
def test_invalid_requested_counts_are_errors(bad) -> None:
    with pytest.raises(ValueError):
        validate_window(snapshot(hourly(WEEKDAY, 5)), required_count=bad)


# ---------------------------------------------------------------- snapshot integration
def test_snapshot_states_carry_into_the_window() -> None:
    bars = hourly(WEEKDAY, 10)
    provisional = validate_window(snapshot(bars, tick_age=600), required_count=10)
    assert provisional["verdict"]["state"] == PROVISIONAL
    (reason,) = provisional["dimensions"]["snapshot"]["reasons"]
    assert (
        reason["code"] == "snapshot_provisional"
        and "clock_stale_ticks" in reason["snapshot_reasons"]
    )
    refused = validate_window(
        snapshot(bars, status="UNAVAILABLE", reason="terminal_not_running"), required_count=10
    )
    assert refused["verdict"]["state"] == REJECTED and "snapshot_refused" in codes(refused)


def test_stale_snapshot_is_rejected_when_re_evaluated_at_analysis_time() -> None:
    snap = snapshot(hourly(WEEKDAY, 10))
    fresh = validate_window(snap, required_count=10, analysed_at=BASE + timedelta(seconds=3))
    assert fresh["verdict"]["state"] == ACCEPTED
    stale = validate_window(snap, required_count=10, analysed_at=BASE + timedelta(seconds=30))
    assert stale["verdict"]["state"] == REJECTED
    (reason,) = stale["dimensions"]["snapshot"]["reasons"]
    assert "snapshot_too_old" in reason["snapshot_reasons"]
    assert stale["evaluated_at_utc"] == iso(BASE + timedelta(seconds=30))


def test_missing_snapshot_metadata_is_rejected() -> None:
    snap = snapshot(hourly(WEEKDAY, 10))
    for key in ("eligibility", "snapshot_id"):
        damaged = {k: v for k, v in snap.items() if k != key}
        result = validate_window(damaged, required_count=10)
        assert result["verdict"]["state"] == REJECTED
        assert "snapshot_metadata_missing" in codes(result, "snapshot")
    weird = dict(snap, timeframe="W1")
    assert validate_window(weird, required_count=10)["verdict"]["state"] == REJECTED


def test_verdicts_are_deterministic() -> None:
    snap = snapshot(hourly(WEEKDAY, 20), tick_age=600)
    at = BASE + timedelta(seconds=2)
    outputs = {
        json.dumps(validate_window(snap, required_count=20, analysed_at=at), sort_keys=True)
        for _ in range(50)
    }
    assert len(outputs) == 1


def test_window_from_the_live_service_snapshot() -> None:
    now = datetime(2026, 11, 11, 10, 31, 30, tzinfo=UTC)
    fake = FakeMT5(now=now.timestamp)
    service = LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake),
        terminal_running=lambda: True,
        reconnect_interval=0,
        clock=lambda: now,
    )
    service.start()
    try:
        snap = service.snapshot("EURUSD", "H1", 12)
    finally:
        service.stop()
    result = validate_window(snap, required_count=10)  # 11 closed + 1 forming
    assert result["verdict"]["state"] == ACCEPTED, result["verdict"]
    assert result["excluded_candles"][0]["reason"] == "forming"


# ---------------------------------------------------------------- closure
def test_closure_evidence_dimension() -> None:
    bars = hourly(WEEKDAY, 10)
    bars[-1] = candle(bars[-1]["t"], closure="clock_only")
    clock = validate_window(snapshot(bars), required_count=10)
    assert clock["verdict"]["state"] == PROVISIONAL and codes(clock) == {"clock_only_closure"}
    bars = hourly(WEEKDAY, 10)
    bars[4] = candle(bars[4]["t"], closure="forming")
    inside = validate_window(snapshot(bars), required_count=10)
    assert "forming_candle_inside_window" in codes(inside, "closure")
    assert inside["verdict"]["state"] == REJECTED


# ---------------------------------------------------------------- continuity integration
def test_configured_weekend_closure_inside_the_window_is_accepted() -> None:
    bars = hourly("2026-11-13T15:00:00", 7) + hourly("2026-11-15T22:00:00", 5)
    result = validate_window(snapshot(bars), required_count=12)
    assert result["verdict"]["state"] == ACCEPTED
    assert result["dimensions"]["continuity"]["evidence"]["gaps_by_class"] == {
        "configured_closure": 1
    }
    assert "not proof" in result["dimensions"]["continuity"]["evidence"]["note"]


def test_expected_rollover_break_is_accepted() -> None:
    bars = hourly("2026-11-10T21:50:00", 10, "M1") + hourly("2026-11-10T22:05:00", 5, "M1")
    result = validate_window(snapshot(bars, timeframe="M1"), required_count=15)
    assert result["verdict"]["state"] == ACCEPTED
    assert result["dimensions"]["continuity"]["evidence"]["gaps_by_class"] == {
        "expected_no_tick": 1
    }


def test_unexplained_gaps_are_never_accepted() -> None:
    bars = [b for b in hourly(WEEKDAY, 20) if b["t"] != "2026-11-10T09:00:00Z"]
    unknown = validate_window(snapshot(bars), required_count=19)
    assert unknown["verdict"]["state"] == REJECTED and "gap_unknown" in codes(unknown)
    full = [b["t"] for b in hourly(WEEKDAY, 20)]
    holed = [b["t"] for b in bars]
    missing = validate_window(snapshot(bars), required_count=19, peers={"GBPUSD": full})
    assert "gap_symbol_missing" in codes(missing) and missing["verdict"]["state"] == REJECTED
    wide = validate_window(snapshot(bars), required_count=19, peers={"GBPUSD": holed})
    assert wide["verdict"]["state"] == PROVISIONAL and codes(wide) == {"gap_market_wide_unverified"}


# ---------------------------------------------------------------- Sunday D1
def d1(*dates: str) -> list[dict]:
    return [candle(f"{d}T00:00:00Z") for d in dates]


def test_d1_convention_is_reported_with_its_unresolved_points() -> None:
    result = validate_window(
        snapshot(d1("2026-11-05", "2026-11-06", "2026-11-08", "2026-11-09"), timeframe="D1"),
        required_count=4,
    )
    assert result["d1_convention"] == D1_CONVENTION
    assert D1_CONVENTION["status"] == "observed" and D1_CONVENTION["unresolved"]
    assert "00:00 UTC" in D1_CONVENTION["candle_open"]


@pytest.mark.parametrize(
    ("dates", "state", "gap_classes"),
    [
        (("2026-11-06", "2026-11-08"), ACCEPTED, {"configured_closure": 1}),  # Fri -> Sun
        (("2026-11-08", "2026-11-09"), ACCEPTED, {}),  # Sun -> Mon
        (("2026-11-06", "2026-11-09"), REJECTED, {"unknown": 1}),  # Fri -> Mon, Sunday missing
        (
            ("2026-10-30", "2026-11-01", "2026-11-02"),
            ACCEPTED,
            {"configured_closure": 1},
        ),  # US fall-back
        (
            ("2026-03-06", "2026-03-08", "2026-03-09"),
            ACCEPTED,
            {"configured_closure": 1},
        ),  # US spring
        (
            ("2023-12-22", "2023-12-25"),
            ACCEPTED,
            {"configured_closure": 1},
        ),  # Monday holiday: no Sunday
    ],
)
def test_sunday_d1_sequences(dates, state, gap_classes) -> None:
    result = validate_window(snapshot(d1(*dates), timeframe="D1"), required_count=len(dates))
    assert result["verdict"]["state"] == state, result["verdict"]
    assert result["dimensions"]["continuity"]["evidence"]["gaps_by_class"] == gap_classes


def test_explicit_sunday_exclusion_is_caller_controlled() -> None:
    bars = d1("2026-11-06", "2026-11-09")
    result = validate_window(
        snapshot(bars, timeframe="D1"), required_count=2, excluded_opens=["2026-11-08T00:00:00Z"]
    )
    assert result["verdict"]["state"] == ACCEPTED
    assert result["dimensions"]["continuity"]["evidence"]["gaps_by_class"] == {
        "analytical_exclusion": 1
    }


@pytest.mark.parametrize("bad", ["2026-11-08T21:00:00Z", "2026-11-08T22:05:00Z"])
def test_d1_candle_at_the_reopen_time_instead_of_midnight_is_rejected(bad) -> None:
    bars = d1("2026-11-06") + [candle(bad)] + d1("2026-11-09")
    result = validate_window(snapshot(bars, timeframe="D1"), required_count=2)
    assert result["verdict"]["state"] == REJECTED
    (reason,) = [r for r in result["verdict"]["reasons"] if r["code"] == "invalid_timestamps"]
    assert reason["candles"][0]["error"] == "not_aligned_to_timeframe"
