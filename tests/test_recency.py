"""E1 market-data recency (design revision 8): deterministic matrices, the exhaustive R2/R4
aggregation table, exclusion precedence and property tests.

Every snapshot is synthetic, built with T2's ``build_snapshot`` through the shared fixture helper,
with explicit tick and retrieval times; consistent candles get the closure label T1 would give
them (the shared rule). No MT5, no network, no data/ access.
"""

from __future__ import annotations

import itertools
import json
import random
from datetime import UTC, datetime, timedelta

import pytest
from browser_harness import Browser
from fake_mt5 import FakeMT5
from t6_fixtures import trend_bars
from test_browser_analysis import BROWSER, StubAnalysis, _panel_text, _ready, _show
from test_browser_smoke import _serve
from test_market_analysis import RETRIEVED, FakeLive, FixedClock, service
from test_window_validation import BASE, candle, iso, snapshot

from johanna.analysis.closure import FORMING, classify_closure
from johanna.analysis.continuity import TIMEFRAME_SECONDS, slot_required
from johanna.analysis.recency import (
    NOT_APPLICABLE,
    NOT_TRIGGERED,
    PASS_STATUS,
    RuleResult,
    aggregate,
)
from johanna.analysis.sessions import closures_overlapping
from johanna.analysis.window import ACCEPTED, PROVISIONAL, REJECTED, validate_window
from johanna.market_analysis import _window_validation

H1, H4, D1 = (timedelta(seconds=TIMEFRAME_SECONDS[tf]) for tf in ("H1", "H4", "D1"))
STEPS = {"H1": H1, "H4": H4, "D1": D1}
MS = timedelta(milliseconds=1)
S = timedelta(seconds=1)


def u(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=UTC)


def required_slots_back(last: datetime, count: int, step: timedelta) -> list[datetime]:
    """``count`` schedule-required slots ending at ``last`` (inclusive), oldest first."""
    closures = closures_overlapping(last - step * (count * 3 + 400), last + step)
    out, slot = [], last
    while len(out) < count:
        if slot_required(slot, step, closures):
            out.append(slot)
        slot -= step
    return out[::-1]


def bars(slots, step, tick, t_bars, labels=None) -> list[dict]:
    """Candles at ``slots`` labelled as T1 would (``labels`` overrides: slot -> label)."""
    labels = labels or {}
    out = []
    for slot in slots:
        label = labels.get(slot) or classify_closure(slot + step, tick, t_bars)
        c = candle(slot, closure=label)
        if label == "unknown":
            c["complete"] = True  # T1's archived path; unknown is never recomputed
        out.append(c)
    return out


def run(candles, *, tick, read=None, timeframe="H1", required=None, excluded=()):
    read = read or tick + timedelta(seconds=0.5)
    snap = snapshot(candles, timeframe=timeframe, tick_time=tick, read_at=read)
    closed = [c for c in candles if c["closure"] != FORMING] or candles
    return validate_window(
        snap, required_count=required or max(1, len(closed)), excluded_opens=excluded
    )


def rec(result) -> dict:
    return result["dimensions"]["recency"]


def rcodes(result) -> set[str]:
    return {r["code"] for r in rec(result)["reasons"]}


def window(last, count, step, tick, read=None, forming=True, labels=None, drop=()):
    """A consistent window of ``count`` required slots ending at ``last``, plus the forming
    candle of the next required slot when ``forming``."""
    read = read or tick + timedelta(seconds=0.5)
    t_bars = read + timedelta(seconds=0.2)
    slots = required_slots_back(last, count, step)
    if forming:
        nxt = last + step
        while not slot_required(nxt, step, closures_overlapping(nxt, nxt + step)):
            nxt += step
        if nxt + step > tick:  # only a candle still open at the tick can be forming
            slots.append(nxt)
    slots = [s for s in slots if s not in set(drop)]
    return bars(slots, step, tick, t_bars, labels)


WED = u("2026-11-11T00:00:00")  # winter time: close Fri 22:00Z, reopen Sun 22:05Z


# ================================================================ normal operation (N)
@pytest.mark.parametrize(
    ("tf", "tick", "last"),
    [
        ("H1", u("2026-11-11T14:00:05"), u("2026-11-11T13:00:00")),
        ("H4", u("2026-11-11T14:00:05"), u("2026-11-11T08:00:00")),
        ("D1", u("2026-11-11T14:00:05"), u("2026-11-10T00:00:00")),
    ],
)
def test_current_window_with_forming_candle_is_accepted(tf, tick, last) -> None:
    result = run(window(last, 30, STEPS[tf], tick), tick=tick, timeframe=tf)
    assert rec(result)["status"] == "pass", rec(result)["reasons"]
    assert result["recency"]["classification"] == "current"
    assert result["recency"]["rules"]["R2"]["status"] == PASS_STATUS
    assert result["recency"]["rules"]["R4"]["status"] == NOT_TRIGGERED
    assert result["recency"]["rules"]["R2"]["basis"]["E_verified"] is True
    assert result["verdict"]["state"] == ACCEPTED


def test_gbpusd_l1_replica_is_rejected_with_32_missing_slots() -> None:
    tick = u("2026-11-13T18:47:22.330")
    read = tick + timedelta(seconds=7.909)
    candles = window(u("2026-11-12T09:00:00"), 40, H1, tick, read=read, forming=False)
    result = run(candles, tick=tick, read=read)
    (reason,) = [r for r in rec(result)["reasons"] if r["code"] == "closed_history_behind_tick"]
    assert reason["severity"] == "fail"
    assert reason["missing_required_slots"]["count"] == 32
    assert reason["expected_latest_closed_t"] == "2026-11-13T17:00:00Z"
    assert result["verdict"]["state"] == REJECTED
    assert result["recency"]["classification"] == "stale_closed_history"


def test_current_forming_candle_cannot_hide_missing_closed_history() -> None:
    """Revision 1 flaw: closed 09:00, forming 18:00, tick 18:47 -> 10:00..17:00 missing."""
    tick = u("2026-11-11T18:47:00")
    candles = window(u("2026-11-11T09:00:00"), 30, H1, tick, forming=False)
    candles.append(candle(u("2026-11-11T18:00:00"), closure="forming"))
    result = run(candles, tick=tick)
    (reason,) = [r for r in rec(result)["reasons"] if r["code"] == "closed_history_behind_tick"]
    assert reason["missing_required_slots"]["count"] == 8
    assert result["verdict"]["state"] == REJECTED


@pytest.mark.parametrize(
    ("tf", "last", "missing"),
    [
        ("H1", u("2026-11-11T13:00:00"), u("2026-11-11T12:00:00")),  # exactly one missing
        ("H4", u("2026-11-11T08:00:00"), u("2026-11-11T04:00:00")),
        ("D1", u("2026-11-10T00:00:00"), u("2026-11-09T00:00:00")),
    ],
)
def test_newer_verified_candle_never_hides_an_earlier_missing_slot(tf, last, missing) -> None:
    tick = u("2026-11-11T14:00:05")
    result = run(window(last, 30, STEPS[tf], tick, drop=[missing]), tick=tick, timeframe=tf)
    (reason,) = [r for r in rec(result)["reasons"] if r["code"] == "closed_history_gap"]
    assert reason["missing_required_slots"]["slots"] == [iso(missing)]
    assert result["verdict"]["state"] == REJECTED


@pytest.mark.parametrize(
    ("tf", "last", "stale_last"),
    [
        ("H1", u("2026-11-11T13:00:00"), u("2026-11-11T12:00:00")),
        ("H4", u("2026-11-11T08:00:00"), u("2026-11-11T04:00:00")),
        ("D1", u("2026-11-10T00:00:00"), u("2026-11-09T00:00:00")),
    ],
)
def test_one_slot_behind_is_stale_with_or_without_forming(tf, last, stale_last) -> None:
    tick = u("2026-11-11T14:00:05")
    for forming in (False, True):
        candles = window(stale_last, 30, STEPS[tf], tick, forming=False)
        if forming:  # a current forming candle in the tick's slot does not help
            slot = datetime.fromtimestamp(
                tick.timestamp() - tick.timestamp() % STEPS[tf].total_seconds(), tz=UTC
            )
            candles.append(candle(slot, closure="forming"))
        result = run(candles, tick=tick, timeframe=tf)
        assert "closed_history_behind_tick" in rcodes(result)
        assert result["verdict"]["state"] == REJECTED


# ================================================================ closures (C)
def test_saturday_read_keeps_the_closed_market_treatment() -> None:
    tick = u("2026-11-13T21:59:58")  # last Friday tick (close 22:00Z)
    read = u("2026-11-14T12:00:00")
    candles = window(u("2026-11-13T21:00:00"), 30, H1, tick, read=read, forming=False)
    assert candles[-1]["closure"] == "clock_only"  # T1: closed by the clock
    result = run(candles, tick=tick, read=read)
    assert rec(result)["status"] == "pass", rec(result)["reasons"]
    assert result["recency"]["classification"] == "current_market_closed"
    assert result["recency"]["rules"]["R4"]["status"] == NOT_TRIGGERED
    assert result["verdict"]["state"] == PROVISIONAL  # existing T2 stale ticks + clock_only


def test_sunday_after_reopen_is_accepted_and_weekend_slots_are_not_required() -> None:
    tick = u("2026-11-15T22:06:10")
    candles = window(u("2026-11-13T21:00:00"), 30, H1, tick, forming=False)
    candles.append(candle(u("2026-11-15T22:00:00"), closure="forming"))
    result = run(candles, tick=tick)
    assert rec(result)["status"] == "pass", rec(result)["reasons"]
    assert result["verdict"]["state"] == ACCEPTED


def test_sunday_reopen_with_missing_friday_candles_is_rejected() -> None:
    tick = u("2026-11-15T22:06:10")
    candles = window(u("2026-11-13T18:00:00"), 30, H1, tick, forming=False)
    candles.append(candle(u("2026-11-15T22:00:00"), closure="forming"))
    result = run(candles, tick=tick)
    (reason,) = [r for r in rec(result)["reasons"] if r["code"] == "closed_history_behind_tick"]
    assert reason["missing_required_slots"]["slots"] == [
        "2026-11-13T19:00:00Z",
        "2026-11-13T20:00:00Z",
        "2026-11-13T21:00:00Z",
    ]


@pytest.mark.parametrize(
    ("read", "expected", "code"),
    [
        (u("2026-11-15T22:20:00"), "pass", None),  # within the reopen tolerance
        (u("2026-11-15T22:40:00"), "provisional", "tick_stale_in_open_session"),
        (u("2026-11-15T23:30:00"), "fail", "history_behind_clock"),  # Sun 22:00 slot closed, absent
    ],
)
def test_reopen_without_new_ticks(read, expected, code) -> None:
    tick = u("2026-11-13T21:59:58")
    candles = window(u("2026-11-13T21:00:00"), 30, H1, tick, read=read, forming=False)
    result = run(candles, tick=tick, read=read)
    assert rec(result)["status"] == expected, rec(result)["reasons"]
    if code:
        assert code in rcodes(result)


def test_daily_rollover_silence_is_not_stale() -> None:
    tick = u("2026-11-11T22:00:05")  # 17:00:05 New York (winter)
    read = u("2026-11-11T22:09:00")
    candles = window(u("2026-11-11T21:00:00"), 30, H1, tick, read=read)
    result = run(candles, tick=tick, read=read)
    assert rec(result)["status"] == "pass", rec(result)["reasons"]
    assert result["recency"]["classification"] == "current_rollover"


def test_christmas_closure_is_current_market_closed() -> None:
    tick = u("2026-12-24T21:59:50")
    read = u("2026-12-25T15:00:00")
    candles = window(u("2026-12-24T21:00:00"), 30, H1, tick, read=read, forming=False)
    result = run(candles, tick=tick, read=read)
    assert rec(result)["status"] == "pass", rec(result)["reasons"]
    assert result["recency"]["classification"] == "current_market_closed"


def test_d1_monday_expects_the_sunday_candle() -> None:
    tick = u("2026-11-16T00:30:00")
    candles = window(u("2026-11-15T00:00:00"), 30, D1, tick)
    assert run(candles, tick=tick, timeframe="D1")["dimensions"]["recency"]["status"] == "pass"
    missing = window(u("2026-11-13T00:00:00"), 30, D1, tick)  # Friday last: Sunday omitted
    result = run(missing, tick=tick, timeframe="D1")
    (reason,) = [r for r in rec(result)["reasons"] if r["code"] == "closed_history_behind_tick"]
    assert reason["missing_required_slots"]["kinds"] == ["sunday_partial_d1"]


# ================================================================ closure labels (CS)
def _cs(labels, tick, read):
    t_bars = read + timedelta(seconds=0.2)
    slots = required_slots_back(u("2026-11-11T12:00:00"), 20, H1)
    candles = bars(slots, H1, tick, t_bars)
    for slot, label in labels.items():
        candles = [c for c in candles if c["t"] != iso(slot)]
        candles.append(candle(slot, closure=label))
    return run(sorted(candles, key=lambda c: c["t"]), tick=tick, read=read)


def test_cs1_two_consistent_forming_candles() -> None:
    tick, read = u("2026-11-11T13:59:30"), u("2026-11-11T14:00:29.8")
    result = _cs(
        {u("2026-11-11T13:00:00"): "forming", u("2026-11-11T14:00:00"): "forming"}, tick, read
    )
    assert rec(result)["status"] == "pass", rec(result)["reasons"]
    assert "forming_candle_inside_window" in {r["code"] for r in result["verdict"]["reasons"]}
    assert result["verdict"]["state"] == REJECTED


@pytest.mark.parametrize(
    ("label", "tick", "read", "code"),
    [
        ("forming", "13:59:30", "14:01:59.8", "forming_candle_past_clock_close"),  # CS2
        ("tick_observed", "13:59:30", "14:00:29.8", "tick_closure_unsupported"),  # CS4
        ("clock_only", "13:59:30", "14:00:29.8", "clock_closure_premature"),  # CS5
        ("clock_only", "14:00:05", "14:00:05.5", "closure_precedence_mismatch"),  # CS6c (D1)
        ("forming", "14:00:05", "14:00:05.5", "forming_candle_behind_tick"),  # R4-E
    ],
)
def test_closure_contradictions_fail(label, tick, read, code) -> None:
    result = _cs(
        {u("2026-11-11T13:00:00"): label}, u(f"2026-11-11T{tick}"), u(f"2026-11-11T{read}")
    )
    assert code in rcodes(result)
    assert result["recency"]["classification"] == "closure_contradiction"
    assert result["verdict"]["state"] == REJECTED


def test_flag_and_close_time_contradictions_fail() -> None:
    tick = u("2026-11-11T14:00:05")
    candles = window(u("2026-11-11T13:00:00"), 20, H1, tick)
    candles[-1]["complete"] = True  # forming but complete
    assert "closure_flag_mismatch" in rcodes(run(candles, tick=tick))
    candles = window(u("2026-11-11T13:00:00"), 20, H1, tick)
    candles[5]["close_t"] = "2020-01-01T00:00:00Z"  # not open + 1 h
    assert "close_time_mismatch" in rcodes(run(candles, tick=tick))


def test_unknown_closure_stays_provisional() -> None:
    tick = u("2026-11-11T14:00:05")
    labels = {u("2026-11-11T13:00:00"): "unknown"}
    result = run(window(u("2026-11-11T13:00:00"), 20, H1, tick, labels=labels), tick=tick)
    assert rec(result)["status"] == "provisional"
    assert "recency_relies_on_unverified_closure" in rcodes(result)
    assert result["verdict"]["state"] == PROVISIONAL


# ================================================================ R4 grace boundaries (B)
@pytest.mark.parametrize("tf", ["H1", "H4", "D1"])
@pytest.mark.parametrize(
    ("offset", "present", "status", "code"),
    [
        (-MS, True, "provisional", "tick_stale_in_open_session"),  # B1
        (timedelta(0), True, "provisional", "tick_stale_in_open_session"),  # B2
        (timedelta(seconds=30), True, "provisional", "tick_stale_in_open_session"),  # B3
        (timedelta(seconds=60) - MS, False, "provisional", "tick_stale_in_open_session"),  # B4x
        (timedelta(seconds=60), True, "provisional", "tick_stale_in_open_session"),  # B5
        (timedelta(seconds=60), False, "fail", "history_behind_clock"),  # B5x
        (timedelta(seconds=60) + MS, False, "fail", "history_behind_clock"),  # B6x
    ],
)
def test_r4_grace_adjusted_expectation(tf, offset, present, status, code) -> None:
    step = STEPS[tf]
    k = {
        "H1": u("2026-11-11T13:00:00"),
        "H4": u("2026-11-11T12:00:00"),
        "D1": u("2026-11-11T00:00:00"),
    }[tf]
    c = k + step
    tick = c - timedelta(minutes=3)  # stale (> 120 s) during scheduled open time
    read = c + offset
    candles = window(k - step, 30, step, tick, read=read, forming=False)
    if present:
        candles += bars([k], step, tick, read + timedelta(seconds=0.2))
    result = run(candles, tick=tick, read=read, timeframe=tf)
    assert rec(result)["status"] == status, rec(result)["reasons"]
    assert code in rcodes(result)
    assert result["verdict"]["state"] != ACCEPTED


# ================================================================ empty V and future windows
def test_only_unknown_candles_is_provisional_and_never_pass() -> None:
    tick = u("2026-11-11T14:00:05")
    slots = required_slots_back(u("2026-11-11T13:00:00"), 15, H1)
    candles = bars(slots, H1, tick, tick, {s: "unknown" for s in slots})
    result = run(candles, tick=tick)
    assert rec(result)["status"] == "provisional"


def test_only_forming_candles_is_reference_missing() -> None:
    tick = u("2026-11-11T14:00:05")
    result = run([candle(u("2026-11-11T14:00:00"), closure="forming")], tick=tick)
    (reason,) = [r for r in rec(result)["reasons"] if r["code"] == "recency_reference_missing"]
    assert "closed_or_unverified_candle" in reason["missing"]


def test_window_after_the_reference_with_r4_not_triggered_fails() -> None:
    """Rev 7 3c / Q1: a newer candle does not prove coverage through E."""
    tick, read = u("2026-11-13T21:59:58"), u("2026-11-14T12:00:00")
    candles = bars([u("2026-11-13T21:00:00")], H1, tick, read + timedelta(seconds=0.2))
    result = run(candles, tick=tick, read=read, required=1)
    assert result["recency"]["rules"]["R2"]["status"] == NOT_APPLICABLE
    (reason,) = [r for r in rec(result)["reasons"] if r["code"] == "recency_reference_missing"]
    assert reason["missing"] == ["reference_before_window"]
    assert result["verdict"]["state"] == REJECTED


def test_window_after_the_reference_with_r4_applicable_is_provisional() -> None:
    tick, read = u("2026-11-11T12:57:00"), u("2026-11-11T15:01:00")
    slots = [u("2026-11-11T12:00:00"), u("2026-11-11T13:00:00"), u("2026-11-11T14:00:00")]
    candles = bars(slots, H1, tick, read + timedelta(seconds=0.2))
    result = run(candles, tick=tick, read=read, required=3)
    rules = result["recency"]["rules"]
    assert (rules["R2"]["status"], rules["R4"]["status"]) == (NOT_APPLICABLE, PASS_STATUS)
    assert rec(result)["status"] == "provisional"


def test_r2_and_r4_disagree_on_a_stale_tick() -> None:
    tick, read = u("2026-11-11T12:57:00"), u("2026-11-11T15:01:00")
    t_bars = read + timedelta(seconds=0.2)
    slots = required_slots_back(u("2026-11-11T12:00:00"), 20, H1)
    candles = bars(slots, H1, tick, t_bars) + bars(
        [u("2026-11-11T13:00:00")], H1, tick, t_bars, {u("2026-11-11T13:00:00"): "unknown"}
    )
    result = run(candles, tick=tick, read=read)
    rules = result["recency"]["rules"]
    assert rules["R2"]["status"] == PASS_STATUS and rules["R4"]["status"] == "fail"
    assert "history_behind_clock" in rcodes(result)
    assert result["verdict"]["state"] == REJECTED


# ================================================================ exclusion precedence (X)
X_TICK = u("2026-11-11T14:00:05")
X_E = u("2026-11-11T13:00:00")
X_INNER = u("2026-11-11T10:00:00")


def _x(slot, kind, extra_excluded=()):
    labels = {}
    drop = []
    if kind == "unknown":
        labels[slot] = "unknown"
    elif kind == "forming":
        labels[slot] = "forming"  # always contradictory at a slot <= E
    elif kind == "contradictory":
        labels[slot] = "clock_only"  # tick already past the close
    elif kind == "absent":
        drop = [slot]
    candles = window(X_E, 20, H1, X_TICK, labels=labels, drop=drop)
    return run(candles, tick=X_TICK, excluded=[iso(slot), *extra_excluded])


@pytest.mark.parametrize(
    ("slot", "kind", "status", "codes_in", "t4"),
    [
        (X_E, "verified", "provisional", {"expected_slot_excluded"}, PROVISIONAL),  # X1
        (X_E, "unknown", "provisional", {"expected_slot_excluded"}, PROVISIONAL),  # X2
        (X_E, "forming", "fail", {"forming_candle_behind_tick"}, REJECTED),  # X3
        (X_E, "contradictory", "fail", {"closure_precedence_mismatch"}, REJECTED),  # X4
        (X_E, "absent", "provisional", {"expected_slot_excluded"}, PROVISIONAL),  # X5
        (X_INNER, "verified", "pass", set(), ACCEPTED),  # X6
        (X_INNER, "unknown", "pass", set(), PROVISIONAL),  # X7: T4 closure_unknown still applies
        (X_INNER, "forming", "fail", {"forming_candle_behind_tick"}, REJECTED),  # X8
        (X_INNER, "contradictory", "fail", {"closure_precedence_mismatch"}, REJECTED),  # X9
        (X_INNER, "absent", "pass", set(), ACCEPTED),  # X10
    ],
)
def test_exclusion_precedence(slot, kind, status, codes_in, t4) -> None:
    result = _x(slot, kind)
    assert rec(result)["status"] == status, rec(result)["reasons"]
    assert codes_in <= rcodes(result)
    assert "recency_relies_on_unverified_closure" not in rcodes(result)  # excluded U unused
    for reason in rec(result)["reasons"]:
        slots = reason.get("missing_required_slots", {}).get("slots", [])
        assert iso(slot) not in slots  # an excluded slot is never a gap
    assert result["verdict"]["state"] == t4


def test_every_slot_excluded_is_reference_interval_failure() -> None:
    candles = window(X_E, 20, H1, X_TICK)
    everything = [c["t"] for c in candles]
    result = run(candles, tick=X_TICK, excluded=everything)
    (reason,) = [r for r in rec(result)["reasons"] if r["code"] == "recency_reference_missing"]
    assert reason["missing"] == ["reference_interval"]


def test_r4_expected_slot_excluded() -> None:
    tick, read = u("2026-11-11T12:57:00"), u("2026-11-11T15:01:00")
    candles = window(u("2026-11-11T13:00:00"), 20, H1, tick, read=read, forming=False)
    result = run(candles, tick=tick, read=read, excluded=["2026-11-11T14:00:00Z"])
    assert result["recency"]["rules"]["R4"]["status"] == "provisional"
    assert "expected_slot_excluded" in rcodes(result)


# ================================================================ aggregation truth table
def _rule(rule, status):
    reasons = {
        "fail": [{"code": "closed_history_gap", "severity": "fail", "detail": "", "rule": [rule]}],
        "provisional": [
            {
                "code": "recency_relies_on_unverified_closure",
                "severity": "provisional",
                "detail": "",
                "rule": [rule],
            }
        ],
    }.get(status, [])
    return RuleResult(rule, status, reasons, na_reason="reference_before_window")


TRUTH = {
    # (R2, R4): overall
    ("pass", NOT_TRIGGERED): "pass",
    ("provisional", NOT_TRIGGERED): "provisional",
    ("fail", NOT_TRIGGERED): "fail",
    (NOT_APPLICABLE, NOT_TRIGGERED): "fail",
    ("pass", "pass"): "provisional",
    ("provisional", "pass"): "provisional",
    ("fail", "pass"): "fail",
    (NOT_APPLICABLE, "pass"): "provisional",
    ("pass", "provisional"): "provisional",
    ("provisional", "provisional"): "provisional",
    ("fail", "provisional"): "fail",
    (NOT_APPLICABLE, "provisional"): "provisional",
    ("pass", "fail"): "fail",
    ("provisional", "fail"): "fail",
    ("fail", "fail"): "fail",
    (NOT_APPLICABLE, "fail"): "fail",
    ("pass", NOT_APPLICABLE): "fail",
    ("provisional", NOT_APPLICABLE): "fail",
    ("fail", NOT_APPLICABLE): "fail",
    (NOT_APPLICABLE, NOT_APPLICABLE): "fail",
}


def test_truth_table_is_complete() -> None:
    r2s = ("pass", "provisional", "fail", NOT_APPLICABLE)
    r4s = ("pass", "provisional", "fail", NOT_APPLICABLE, NOT_TRIGGERED)
    assert set(TRUTH) == set(itertools.product(r2s, r4s)) and len(TRUTH) == 20


@pytest.mark.parametrize(("r2", "r4"), sorted(TRUTH))
def test_aggregate(r2, r4) -> None:
    status, reasons, row = aggregate(_rule("R2", r2), _rule("R4", r4))
    assert status == TRUTH[(r2, r4)]
    assert row == f"R2={r2},R4={r4}"
    codes = {r["code"] for r in reasons}
    assert ("tick_stale_in_open_session" in codes) == (r4 != NOT_TRIGGERED)  # stale-tick cap
    if (r2 == NOT_APPLICABLE and r4 == NOT_TRIGGERED) or r4 == NOT_APPLICABLE:
        assert "recency_reference_missing" in codes
    severities = {r["severity"] for r in reasons}
    derived = "fail" if "fail" in severities else "provisional" if severities else "pass"
    assert derived == status  # the reasons alone reproduce the verdict
    assert (status == "pass") == ((r2, r4) == ("pass", NOT_TRIGGERED))


# ================================================================ properties
STATES = ("V", "U", "F", "X", "absent", "excluded")


def _oracle(states):
    """Independent expectation of the recency status for 4 slots ending at E (fresh tick)."""
    present = [i for i, s in enumerate(states) if s in ("V", "U", "F", "X")]
    if not present:
        return "fail"
    if not any(states[i] in ("V", "U") for i in present):
        return "fail"
    failed = any(states[i] in ("F", "X") for i in present)
    provisional = False
    checked = 0
    for i in range(min(present), 4):
        state = states[i]
        if state == "excluded":
            continue
        checked += 1
        if state == "U":
            provisional = True
        elif state != "V":
            failed = True
    if checked == 0:
        failed = True
    if states[3] == "excluded":
        provisional = True
    return "fail" if failed else "provisional" if provisional else "pass"


def _world(states, excluded_label="V"):
    tick = u("2026-11-11T14:00:05")
    slots = [u("2026-11-11T10:00:00") + i * H1 for i in range(4)]
    candles, excluded = [], []
    for slot, state in zip(slots, states, strict=True):
        if state == "absent":
            continue
        if state == "excluded":
            excluded.append(iso(slot))
            continue  # excluded slots carry no candle in this world
        label = {"V": "tick_observed", "U": "unknown", "F": "forming", "X": "clock_only"}[state]
        c = candle(slot, closure=label)
        if label == "unknown":
            c["complete"] = True
        candles.append(c)
    if not candles:
        candles = []
    return candles, excluded, tick


def test_exhaustive_small_world_matches_the_oracle() -> None:
    mismatches = []
    for states in itertools.product(STATES, repeat=4):
        candles, excluded, tick = _world(states)
        if not candles:
            continue
        # the whole small world is the window (T4 takes the newest `required` candles)
        result = run(candles, tick=tick, excluded=excluded, required=len(candles))
        got = rec(result)["status"]
        if got != _oracle(states):
            mismatches.append((states, got, _oracle(states)))
        if got == "pass":  # P2: PASS has an evidence basis
            rules = result["recency"]["rules"]
            assert rules["R2"]["status"] == PASS_STATUS and rules["R4"]["status"] == NOT_TRIGGERED
            assert rules["R2"]["basis"]["E_verified"] is True
    assert not mismatches, mismatches[:10]


@pytest.mark.parametrize("tf", ["H1", "H4", "D1"])
def test_property_truncation_and_single_gaps_always_fail(tf) -> None:
    step = STEPS[tf]
    tick = u("2026-11-11T14:00:05")
    last = {
        "H1": u("2026-11-11T13:00:00"),
        "H4": u("2026-11-11T08:00:00"),
        "D1": u("2026-11-10T00:00:00"),
    }[tf]
    full = window(last, 12, step, tick, forming=False)
    assert rec(run(full, tick=tick, timeframe=tf))["status"] == "pass"
    for k in range(1, 4):  # remove the last k closed candles, with and without a forming one
        for forming in (False, True):
            cut = full[:-k]
            if forming:
                slot = datetime.fromtimestamp(
                    tick.timestamp() - tick.timestamp() % step.total_seconds(), tz=UTC
                )
                cut = [*cut, candle(slot, closure="forming")]
            assert rec(run(cut, tick=tick, timeframe=tf))["status"] == "fail"
    for i in range(1, len(full) - 1):  # remove any single inner candle
        holed = full[:i] + full[i + 1 :]
        assert rec(run(holed, tick=tick, timeframe=tf))["status"] == "fail"
        unknown = [dict(c) for c in full]
        unknown[i] = {**unknown[i], "closure": "unknown"}
        assert rec(run(unknown, tick=tick, timeframe=tf))["status"] == "provisional"


def test_property_random_worlds_keep_the_invariants() -> None:
    rng = random.Random(20261114)
    for _ in range(400):
        tf = rng.choice(["H1", "H4", "D1"])
        step = STEPS[tf]
        stale = rng.random() < 0.4
        base = u("2026-11-11T14:00:05")
        tick = base - (timedelta(minutes=rng.choice([3, 10, 75])) if stale else timedelta(0))
        read = base + timedelta(seconds=0.5)
        t_bars = read + timedelta(seconds=0.2)
        last = (
            datetime.fromtimestamp(
                read.timestamp() - read.timestamp() % step.total_seconds(), tz=UTC
            )
            - step
        )
        slots = required_slots_back(last, 8, step)
        candles, excluded = [], []
        for slot in slots:
            choice = rng.choice(STATES)
            if choice == "absent":
                continue
            if choice == "excluded":
                excluded.append(iso(slot))
                choice = rng.choice(["V", "U", "absent"])
                if choice == "absent":
                    continue
            if choice == "V":
                c = candle(slot, closure=classify_closure(slot + step, tick, t_bars))
            else:
                label = {"U": "unknown", "F": "forming", "X": "clock_only"}[choice]
                c = candle(slot, closure=label)
                if label == "unknown":
                    c["complete"] = True
            candles.append(c)
        if not candles:
            continue
        result = run(candles, tick=tick, read=read, timeframe=tf, excluded=excluded, required=1)
        recency = result["recency"]
        if recency["status"] == "pass":
            assert recency["rules"]["R2"]["status"] == PASS_STATUS
            assert recency["rules"]["R4"]["status"] == NOT_TRIGGERED
            assert recency["rules"]["R2"]["basis"]["E_verified"] is True
            assert recency["rules"]["R2"]["strict_gap"]["count"] == 0
        if not recency.get("latest_verified_closed_t"):
            assert recency["status"] != "pass"  # empty V never passes
        if recency["rules"]["R4"]["status"] not in (NOT_TRIGGERED, "not_evaluated"):
            assert recency["status"] != "pass"  # stale tick never passes
        for reason in rec(result)["reasons"]:
            gap = reason.get("missing_required_slots", {}).get("slots", [])
            assert not set(gap) & set(excluded)
        json.dumps(result, allow_nan=False)


def test_recency_is_deterministic_and_independent_of_analysed_at() -> None:
    tick = u("2026-11-11T14:00:05")
    candles = window(u("2026-11-11T13:00:00"), 20, H1, tick)
    snap = snapshot(candles, tick_time=tick, read_at=tick + timedelta(seconds=0.5))
    outputs = set()
    for delta in (0.8, 1.5, 3.0):
        at = tick + timedelta(seconds=delta)
        result = validate_window(snap, required_count=20, analysed_at=at)
        outputs.add(json.dumps(result["recency"], sort_keys=True))
    assert len(outputs) == 1


# ================================================================ I1 projection
def test_i1_projection_carries_recency_without_an_evidence_key() -> None:
    tick = u("2026-11-11T14:00:05")
    validation = run(window(u("2026-11-11T12:00:00"), 20, H1, tick, forming=False), tick=tick)
    wv = _window_validation(validation, None)
    assert wv["recency"] == validation["recency"]
    assert set(wv["dimensions"]["recency"]) == {"status", "reasons"}
    assert "closed_history_behind_tick" in wv["reason_codes"]
    assert "evidence" not in json.dumps(wv)


# ================================================================ dashboard (I2)


def _stale_envelope():
    """A real I1 envelope: candles end 11:00 while the BASE tick expects 12:00 (stale)."""
    bars_ = trend_bars(end=datetime(2026, 11, 11, 12, 0, tzinfo=UTC))
    live = FakeLive(
        lambda s, t, c, n: snapshot(bars_, tick_time=BASE - timedelta(seconds=30.5), read_at=BASE)
    )
    return service(live, FixedClock(RETRIEVED + timedelta(seconds=0.8))).analyse("EURUSD", "H1")


@pytest.fixture(scope="module")
def recency_browser():
    instance = Browser(BROWSER)
    yield instance
    instance.close()


@pytest.mark.browser
@pytest.mark.skipif(BROWSER is None, reason="no Chromium-family browser installed")
def test_dashboard_never_shows_stale_history_as_current(recency_browser, tmp_path) -> None:
    envelope = _stale_envelope()
    assert envelope["result"]["decision"]["rule"] == "R1"
    assert "closed_history_behind_tick" in envelope["window_validation"]["reason_codes"]
    assert envelope["window_validation"]["recency"]["classification"] == "stale_closed_history"
    page = recency_browser
    page.set_viewport(1600, 1000)
    stub = StubAnalysis(envelope)
    with _serve(tmp_path, fake=FakeMT5()) as (url, app):
        app.market_analysis = stub
        _ready(page, url)  # the chart itself is LIVE
        _show(page, stub, envelope, "unavailable")
        assert page.evaluate("analysisStatus().state") == "unavailable"
        text = _panel_text(page)
        assert "History stale: expected closed candles missing" in text
        assert "closed_history_behind_tick" in text
        assert "recency: fail" in text
        assert "Current" not in page.evaluate(
            "document.querySelector('#analysis-full-body [data-analysis-state]').textContent"
        )
