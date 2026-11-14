"""E1: the single authoritative closure rule (``johanna.analysis.closure``) and T1's use of it.

Behavioural equivalence only (no source-text assertions): the constant is the same object, T1's
``bar_is_complete`` and labels agree with ``classify_closure`` on a boundary grid, and snapshots
produced by the real LiveMarketService over FakeMT5 at boundary timings carry closure labels that
recency validation finds T1-consistent.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fake_mt5 import FakeMT5

from johanna import live_market
from johanna.adapters.mt5 import MetaTrader5Adapter
from johanna.analysis import closure
from johanna.analysis.closure import (
    CLOCK_ONLY,
    COMPLETION_GRACE,
    FORMING,
    TICK_OBSERVED,
    classify_closure,
    is_complete,
)
from johanna.analysis.window import validate_window
from johanna.live_market import LiveMarketService, bar_is_complete

CLOSE = datetime(2026, 11, 11, 14, 0, tzinfo=UTC)
MS = timedelta(milliseconds=1)
RCV_CODES = {
    "forming_candle_behind_tick",
    "forming_candle_past_clock_close",
    "tick_closure_unsupported",
    "clock_closure_premature",
    "closure_precedence_mismatch",
    "closure_flag_mismatch",
    "close_time_mismatch",
}


def test_grace_is_one_shared_object_of_sixty_seconds() -> None:
    assert live_market.COMPLETION_GRACE is closure.COMPLETION_GRACE
    assert COMPLETION_GRACE == timedelta(seconds=60)


@pytest.mark.parametrize(
    ("tick", "now", "label"),
    [
        (CLOSE - MS, CLOSE + COMPLETION_GRACE - MS, FORMING),
        (CLOSE, CLOSE, TICK_OBSERVED),  # T1's inclusive >= on the tick
        (CLOSE - MS, CLOSE + COMPLETION_GRACE, CLOCK_ONLY),  # inclusive >= on the clock
        (CLOSE - MS, CLOSE + COMPLETION_GRACE + MS, CLOCK_ONLY),
        (None, CLOSE + COMPLETION_GRACE - MS, FORMING),
        (None, CLOSE + COMPLETION_GRACE, CLOCK_ONLY),
        (CLOSE + MS, CLOSE, TICK_OBSERVED),  # tick precedence over the clock
        (CLOSE, CLOSE + 2 * COMPLETION_GRACE, TICK_OBSERVED),
    ],
)
def test_classify_closure_boundaries(tick, now, label) -> None:
    assert classify_closure(CLOSE, tick, now) == label
    assert is_complete(CLOSE, tick, now) == (label != FORMING)


def test_t1_bar_is_complete_agrees_with_the_shared_rule_on_a_grid() -> None:
    grace = COMPLETION_GRACE
    offsets = [timedelta(0), MS, -MS, grace, grace - MS, grace + MS]
    ticks = [None] + [CLOSE + o for o in offsets] + [CLOSE - o for o in offsets]
    nows = [CLOSE + o for o in offsets] + [CLOSE - o for o in offsets]
    for tick in ticks:
        for now in nows:
            assert bar_is_complete(CLOSE, tick, now) == is_complete(CLOSE, tick, now)


@pytest.mark.parametrize(
    ("now", "tick_age", "label_1300"),
    [
        (datetime(2026, 11, 11, 14, 0, 30, tzinfo=UTC), 0.5, TICK_OBSERVED),
        (datetime(2026, 11, 11, 14, 0, 59, 999000, tzinfo=UTC), 120.0, FORMING),
        (datetime(2026, 11, 11, 14, 1, 0, tzinfo=UTC), 120.0, CLOCK_ONLY),
        (datetime(2026, 11, 11, 14, 1, 0, 1000, tzinfo=UTC), 120.0, CLOCK_ONLY),
    ],
)
def test_live_service_snapshot_round_trip_has_no_closure_contradictions(
    now, tick_age, label_1300
) -> None:
    fake = FakeMT5(now=now.timestamp, tick_age_seconds=tick_age)
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
    labels = {b["t"]: b["closure"] for b in snap["bars"]["items"]}
    assert labels["2026-11-11T13:00:00Z"] == label_1300
    result = validate_window(snap, required_count=10)
    recency = result["dimensions"]["recency"]
    assert not {r["code"] for r in recency["reasons"]} & RCV_CODES, recency["reasons"]
    assert result["recency"]["contradictory_candles"] == 0
