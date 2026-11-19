"""P5.1 fingerprint investigation: what ``as_of_utc`` is and how decision identity behaves.

Every envelope comes from the real T2 -> T4 -> T5 -> T6 pipeline (``MarketAnalysisService``)
with synthetic bars; journals live in temporary directories.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta

from t6_fixtures import END, trend_bars
from test_decision_journal import journal
from test_market_analysis import FakeLive, FixedClock, service
from test_window_validation import candle, snapshot


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def analyse(bars, *, delay: float = 0.8, snapshot_id: str | None = None, tick_age=None) -> dict:
    """Real analysis of ``bars``, evaluated ``delay`` seconds after the bars were read."""
    snap = snapshot(bars, **({"tick_age": tick_age} if tick_age is not None else {}))
    if snapshot_id is not None:
        snap["snapshot_id"] = snapshot_id
    clock = FixedClock(_utc(snap["bars"]["retrieved_at_utc"]) + timedelta(seconds=delay))
    live = FakeLive(lambda s, t, c, n: copy.deepcopy(snap))
    return service(live, clock).analyse("EURUSD", "H1")


def as_of(env: dict) -> str:
    return env["result"]["as_of_utc"]


# ---------------------------------------------------------------- what as_of_utc is
def test_as_of_is_the_close_of_the_newest_closed_candle_in_the_window() -> None:
    env = analyse(trend_bars())
    result = env["result"]
    last_t = result["inputs"]["window"]["last_t"]
    assert as_of(env) == "2026-11-11T13:00:00Z"
    assert _utc(as_of(env)) == _utc(last_t) + timedelta(hours=1)  # last window candle + 1 H1
    # it is not the request/evaluation time, which is a separate field
    assert result["evaluated_at_utc"] != as_of(env)
    assert _utc(result["evaluated_at_utc"]) > _utc(as_of(env))


def test_a_forming_candle_is_not_part_of_the_window_and_does_not_move_as_of() -> None:
    closed = trend_bars()
    forming = [
        *closed,
        candle(
            "2026-11-11T13:00:00Z",
            o=closed[-1]["c"],
            h=closed[-1]["c"],
            low=closed[-1]["c"],
            c=closed[-1]["c"],
            closure="forming",
        ),
    ]
    env = analyse(forming)
    assert as_of(env) == "2026-11-11T13:00:00Z"
    assert env["result"]["inputs"]["window"]["last_t"] == "2026-11-11T12:00:00Z"


# ---------------------------------------------------------------- the five scenarios
def test_s1_identical_data_different_snapshot_ids_increment_observations(tmp_path) -> None:
    j = journal(tmp_path)
    ids = {
        j.record_analysis(analyse(trend_bars(), snapshot_id=f"snap-{i}"), "analysis_request")
        for i in range(5)
    }
    assert len(ids) == 1
    assert [d["observations"] for d in j.list()["decisions"]] == [5]


def test_s2_different_evaluation_times_increment_observations(tmp_path) -> None:
    j = journal(tmp_path)
    envs = [analyse(trend_bars(), delay=d) for d in (0.3, 0.8, 2.5, 4.9)]
    assert len({e["result"]["evaluated_at_utc"] for e in envs}) == 4  # request times differ
    assert len({as_of(e) for e in envs}) == 1  # the analysed window does not
    for env in envs:
        j.record_analysis(env, "analysis_request")
    assert [d["observations"] for d in j.list()["decisions"]] == [4]


def test_s3_unchanged_market_reference_increments_observations(tmp_path) -> None:
    """Different tick ages and a newly opened (still forming) candle: same closed window."""
    j = journal(tmp_path)
    closed = trend_bars()
    with_forming = [
        *closed,
        candle(
            "2026-11-11T13:00:00Z",
            o=closed[-1]["c"],
            h=closed[-1]["c"],
            low=closed[-1]["c"],
            c=closed[-1]["c"],
            closure="forming",
        ),
    ]
    envs = [analyse(closed, tick_age=0.5), analyse(closed, tick_age=3.0), analyse(with_forming)]
    assert {as_of(e) for e in envs} == {"2026-11-11T13:00:00Z"}
    decisions = {j.record_analysis(env, "analysis_request") for env in envs}
    assert len(decisions) == 1
    assert [d["observations"] for d in j.list()["decisions"]] == [3]


def test_s4_a_new_closed_candle_window_creates_a_new_decision(tmp_path) -> None:
    j = journal(tmp_path)
    first_env = analyse(trend_bars())
    next_env = analyse(trend_bars(end=END + timedelta(hours=1)))  # one more closed candle
    assert _utc(as_of(next_env)) == _utc(as_of(first_env)) + timedelta(hours=1)
    first = j.record_analysis(first_env, "analysis_request")
    second = j.record_analysis(next_env, "analysis_request")
    assert second != first
    items = {d["id"]: d for d in j.list()["decisions"]}
    assert items[second]["previous_decision_id"] == first
    assert items[first]["observations"] == items[second]["observations"] == 1


def test_s5_a_materially_changed_t6_decision_creates_a_new_decision(tmp_path) -> None:
    j = journal(tmp_path)
    up = analyse(trend_bars(sign=1))
    down = analyse(trend_bars(sign=-1))  # same window times, opposite market
    assert as_of(up) == as_of(down)
    assert up["result"]["opportunity"]["direction"] != down["result"]["opportunity"]["direction"]
    first = j.record_analysis(up, "analysis_request")
    second = j.record_analysis(down, "analysis_request")
    assert second != first
    detail = j.get(second)["decision"]
    assert detail["previous_decision_id"] == first and detail["market_direction"] == "SELL"
