"""Overlay V1 phase 2: the additive ``envelope.overlay`` payload (EMA series and confirmed swings).

Everything is checked against T5 itself (its EMA function over the validated window and its
own swing records). Synthetic snapshots only; no MT5, no files.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from t6_fixtures import FRESH, trend_bars
from test_analysis_engine import history
from test_market_analysis import FRESH_AT, FakeLive, service
from test_window_validation import snapshot

from johanna import market_analysis
from johanna.analysis import decision
from johanna.analysis.decision import (
    capture_analysis,
    evaluate_market_snapshot,
    evaluate_market_snapshot_with_validation,
)
from johanna.analysis.engine import analyse_window
from johanna.analysis.indicators import ema_series
from johanna.market_analysis import OVERLAY_LIMIT, overlay_payload


def _captured(bars, **kwargs):
    snap = snapshot(bars)
    with capture_analysis() as captured:
        evaluated = evaluate_market_snapshot_with_validation(snap, analysed_at=FRESH, **kwargs)
    return snap, evaluated, captured


def _window_closes(snap, validation):
    by_t = {c["t"]: c for c in snap["bars"]["items"]}
    times = validation["window"]["candles"]
    return times, [float(by_t[t]["c"]) for t in times]


def test_ema_points_are_exactly_t5_values_at_their_own_timestamps() -> None:
    snap, evaluated, captured = _captured(trend_bars())
    payload = overlay_payload(evaluated["result"], evaluated["validation"], captured)
    times, closes = _window_closes(snap, evaluated["validation"])
    covered = times[-OVERLAY_LIMIT:]
    latest = evaluated["result"]["features"]["latest"]
    for period in (20, 50, 200):
        expected = dict(zip(times, ema_series(closes, period), strict=True))
        points = payload["ema"][f"EMA{period}"]["points"]
        assert [t for t, _ in points] == covered  # exact window timestamps, in order
        assert all(value == expected[t] for t, value in points)  # bit-for-bit T5 values
        assert points[-1] == [latest[f"EMA{period}"]["t"], latest[f"EMA{period}"]["value"]]


def test_missing_values_stay_null_and_partial_status_is_explicit() -> None:
    snap, evaluated, captured = _captured(trend_bars(250), required_count=250)
    payload = overlay_payload(evaluated["result"], evaluated["validation"], captured)
    times, closes = _window_closes(snap, evaluated["validation"])
    ema200 = payload["ema"]["EMA200"]
    assert [v for _, v in ema200["points"][:199]] == [None] * 199  # T5 has no value yet
    assert ema200["points"][199][1] == ema_series(closes, 200)[199]
    assert payload["status"] == "partial"
    assert "EMA200: not_converged" in payload["reasons"]
    assert ema200["converged_from_t"] is None  # never converged inside this window
    assert payload["coverage"] == {
        "first_t": times[0],
        "last_t": times[-1],
        "count": 250,
        "limit": OVERLAY_LIMIT,
        "truncated": False,
        "selection": payload["coverage"]["selection"],
    }


def test_convergence_point_comes_from_t5() -> None:
    snap, evaluated, captured = _captured(trend_bars())
    payload = overlay_payload(evaluated["result"], evaluated["validation"], captured)
    times = evaluated["validation"]["window"]["candles"]
    for summary in captured["analysis"]["indicators"]["ema"]:
        required = summary["convergence"]["observations_required"]
        assert payload["ema"][summary["name"]]["converged_from_t"] == times[required - 1]
    assert payload["ema"]["EMA200"]["converged_from_t"] == times[-1]  # 661: only at the end


def test_swings_are_t5_confirmed_records_with_distinct_pivot_and_confirmation() -> None:
    snap, evaluated, captured = _captured(trend_bars())
    payload = overlay_payload(evaluated["result"], evaluated["validation"], captured)
    covered = set(evaluated["validation"]["window"]["candles"][-OVERLAY_LIMIT:])
    independent = analyse_window(snap, evaluated["validation"])["structure"]["swings"]
    expected = [s for s in independent["confirmed"] if s["pivot_t"] in covered]
    window = evaluated["validation"]["window"]["candles"]
    got = payload["swings"]["confirmed"]
    assert len(got) == len(expected) > 0
    for record, t5 in zip(got, expected, strict=True):
        for field in ("type", "price", "pivot_t", "confirmed_by_t", "confirmed_at_utc"):
            assert record[field] == t5[field]
        assert record["confirmation_status"] == "confirmed"
        assert record["id"] == f"{t5['type']}@{t5['pivot_t']}"
        # confirmed by the k-th following *candle* (k = 2; weekends skipped), at its close
        assert record["confirmed_by_t"] == window[window.index(record["pivot_t"]) + 2]
        pivot = datetime.fromisoformat(record["pivot_t"].replace("Z", "+00:00"))
        by = datetime.fromisoformat(record["confirmed_by_t"].replace("Z", "+00:00"))
        at = datetime.fromisoformat(record["confirmed_at_utc"].replace("Z", "+00:00"))
        assert at == by + timedelta(hours=1) and at > by > pivot
    assert payload["swings"]["pending_excluded"] == len(independent["pending"])
    assert len({r["id"] for r in got}) == len(got)


def test_cap_is_deterministic_and_metadata_is_preserved() -> None:
    first = service(FakeLive()).analyse("EURUSD", "H1")
    second = service(FakeLive()).analyse("EURUSD", "H1")
    assert json.dumps(first["overlay"], sort_keys=True) == json.dumps(
        second["overlay"], sort_keys=True
    )
    payload, result = first["overlay"], first["result"]
    window = result["inputs"]["window"]
    assert payload["coverage"]["count"] == OVERLAY_LIMIT and payload["coverage"]["truncated"]
    assert payload["coverage"]["last_t"] == window["last_t"] == payload["last_analysed_t"]
    assert payload["window"] == window
    assert payload["snapshot_id"] == result["inputs"]["snapshot_id"]
    assert payload["snapshot_id"] == first["window_validation"]["snapshot_id"]
    assert (payload["symbol"], payload["broker_symbol"], payload["timeframe"]) == (
        result["symbol"],
        result["broker_symbol"],
        result["timeframe"],
    )
    assert payload["validation_verdict"] == first["window_validation"]["verdict"]["state"]
    assert payload["as_of_utc"] == result["as_of_utc"]
    assert payload["provenance"] == {"mode": "constructed", "passed": True}
    assert payload["status"] == "available" and payload["reasons"] == []


def test_rejected_window_gives_an_explicit_unavailable_payload() -> None:
    env = service(FakeLive(lambda s, t, c, n: snapshot(history(660)))).analyse("EURUSD", "H1")
    payload = env["overlay"]
    assert payload["status"] == "unavailable" and payload["coverage"] is None
    assert payload["ema"] == {} and payload["swings"] is None
    assert "rejected" in payload["reasons"][0]


def test_missing_or_foreign_capture_is_unavailable_not_guessed() -> None:
    snap, evaluated, captured = _captured(trend_bars())
    result, validation = evaluated["result"], evaluated["validation"]
    assert overlay_payload(result, validation, None)["status"] == "unavailable"
    assert overlay_payload(result, dict(validation), captured)["status"] == "unavailable"
    twice = {**captured, "runs": 2}
    assert overlay_payload(result, validation, twice)["status"] == "unavailable"


def test_t5_runs_once_per_analysis_and_the_result_is_unchanged(monkeypatch) -> None:
    calls = []
    real = decision.analyse_window

    def counting(*args, **kwargs):
        calls.append(kwargs.get("include_series", False))
        return real(*args, **kwargs)

    monkeypatch.setattr(decision, "analyse_window", counting)
    env = service(FakeLive()).analyse("EURUSD", "H1")
    assert calls == [True]  # one T5 run, series kept inside the capture
    calls.clear()
    plain = evaluate_market_snapshot(snapshot(trend_bars()), analysed_at=FRESH)
    assert calls == [False]  # outside the capture: the accepted path, unchanged
    assert env["result"]["decision"] == plain["decision"]
    assert env["result"]["features"] == plain["features"]


def test_capture_does_not_change_the_companion_or_t6_result() -> None:
    snap = snapshot(trend_bars())
    plain = evaluate_market_snapshot_with_validation(snap, analysed_at=FRESH)
    with capture_analysis() as captured:
        inside = evaluate_market_snapshot_with_validation(snap, analysed_at=FRESH)
    assert set(plain) == set(inside) == {"result", "validation"}
    assert json.dumps(plain["result"], sort_keys=True) == json.dumps(
        inside["result"], sort_keys=True
    )
    assert captured["runs"] == 1 and captured["validation"] is inside["validation"]
    assert captured["result"] is inside["result"]
    assert "series" not in plain["result"]["features"]["latest"]["EMA20"]


def test_existing_envelope_fields_are_unchanged_by_the_overlay() -> None:
    env = service(FakeLive()).analyse("EURUSD", "H1")  # the service clock reads FRESH_AT
    plain = evaluate_market_snapshot(snapshot(trend_bars()), analysed_at=FRESH_AT)
    assert json.dumps(env["result"], sort_keys=True) == json.dumps(plain, sort_keys=True)
    assert "overlay" not in env["window_validation"] and "overlay" not in env["result"]
    assert market_analysis.OVERLAY_VERSION == env["overlay"]["overlay_version"] == 1
