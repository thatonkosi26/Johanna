"""Analysis Overlay V1 phase 2 in a real headless browser: historical EMA lines and confirmed
swings from ``envelope.overlay``, exact candle mapping, fail-closed payload checks, analysis states
and interaction preservation. Envelopes are real I1 envelopes rebased onto the chart's own candles
(as in the phase 1 tests) with an overlay payload built from those same candles (test data only;
the payload schema is the backend's).
"""

from __future__ import annotations

import copy
import json
from datetime import datetime

import pytest
from browser_harness import Browser
from fake_mt5 import FakeMT5
from test_browser_analysis import BROWSER, ENVELOPES, StubAnalysis, _ready
from test_browser_overlay import _chart, _enable, _x_of, rebased
from test_browser_smoke import _box, _drag, _plot_x, _serve, _wheel

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(BROWSER is None, reason="no Chromium-family browser installed"),
]


@pytest.fixture(scope="module")
def browser():
    instance = Browser(BROWSER)
    yield instance
    instance.close()


@pytest.fixture
def page(browser):
    browser.set_viewport(1600, 1000)
    return browser


@pytest.fixture
def served(tmp_path):
    stub = StubAnalysis(ENVELOPES["R7"])
    with _serve(tmp_path, fake=FakeMT5()) as (url, app):
        app.market_analysis = stub
        yield url, app, stub


def _ms(iso: str) -> float:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()


def with_payload(env, chart, *, null=(), drop=(), swings=None, converged=None):
    """Attach an overlay payload consistent with ``env`` and the chart's own candles."""
    env = copy.deepcopy(env)
    r = env["result"]
    window = r["inputs"]["window"]
    closed = [b for b in chart["bars"] if b["complete"] is True]
    in_window = [
        b for b in closed if _ms(window["first_t"]) <= _ms(b["t"]) <= _ms(window["last_t"])
    ]
    covered = in_window[-300:]
    step = _ms(closed[1]["t"]) - _ms(closed[0]["t"])

    def points(offset):
        return [
            [b["t"], None if b["t"] in null else round(b["c"] + offset, 5)]
            for b in covered
            if b["t"] not in drop
        ]

    converged = converged or {
        "EMA20": covered[0]["t"],
        "EMA50": covered[0]["t"],
        "EMA200": covered[-1]["t"],
    }
    li = len(in_window) - 1
    if swings is None:
        swings = []
        for i, kind in ((li - 40, "high"), (li - 35, "low"), (li - 28, "high"), (li - 12, "low")):
            by = in_window[i + 2]["t"]
            swings.append(
                {
                    "id": f"{kind}@{in_window[i]['t']}",
                    "type": kind,
                    "price": in_window[i]["h"] if kind == "high" else in_window[i]["l"],
                    "pivot_t": in_window[i]["t"],
                    "confirmed_by_t": by,
                    "confirmed_at_utc": _iso(_ms(by) + step),  # close of the confirming candle
                    "confirmation_status": "confirmed",
                }
            )
    env["overlay"] = {
        "overlay_version": 1,
        "status": "available",
        "reasons": [],
        "snapshot_id": env["snapshot"]["snapshot_id"],
        "symbol": chart["symbol"],
        "broker_symbol": r["broker_symbol"],
        "timeframe": chart["timeframe"],
        "window": dict(window),
        "last_analysed_t": window["last_t"],
        "as_of_utc": r["as_of_utc"],
        "validation_verdict": env["window_validation"]["verdict"]["state"],
        "analysis_status": r["inputs"].get("analysis_status"),
        "provenance": {"mode": "constructed", "passed": True},
        "coverage": {
            "first_t": covered[0]["t"],
            "last_t": covered[-1]["t"],
            "count": len(covered),
            "limit": 300,
            "truncated": len(in_window) > 300,
            "selection": "test",
        },
        "ema": {
            name: {
                "period": p,
                "status": "available",
                "trusted": True,
                "converged_from_t": converged[name],
                "points": points(off),
            }
            for name, p, off in (
                ("EMA20", 20, 0.0003),
                ("EMA50", 50, 0.0009),
                ("EMA200", 200, 0.002),
            )
        },
        "swings": {
            "status": "available",
            "trusted": True,
            "k": 2,
            "confirmed": swings,
            "pending_excluded": 0,
        },
    }
    return env


def _iso(seconds: float) -> str:
    from datetime import UTC

    return datetime.fromtimestamp(seconds, tz=UTC).isoformat().replace("+00:00", "Z")


_SEQ = iter(range(1, 10**6))


def _show(page, stub, envelope, mode, timeout=10) -> dict:
    """Deliver ``envelope`` (snapshot ids re-marked consistently, overlay included) and wait for
    the overlay to reflect exactly it."""
    envelope = copy.deepcopy(envelope)
    marker = f"phase2-{next(_SEQ)}"
    old = envelope["snapshot"]["snapshot_id"]
    envelope["snapshot"]["snapshot_id"] = marker
    holders = [envelope["result"]["inputs"], envelope["window_validation"]]
    if isinstance(envelope.get("overlay"), dict):
        holders.append(envelope["overlay"])
    for holder in holders:
        if holder.get("snapshot_id") == old:
            holder["snapshot_id"] = marker
    stub.envelope = envelope
    page.evaluate("livePolling.analysis.now()")
    page.wait_for(
        f"state.analysis?.snapshot?.snapshot_id === {json.dumps(marker)}"
        f" && overlayState().last?.mode === {json.dumps(mode)}",
        timeout=timeout,
    )
    return page.evaluate("overlayState()")


def _of(state, kind):
    return [a for a in state["last"]["annotations"] if a["kind"] == kind]


def _skipped(state, kind):
    return {
        s.get("name", s["kind"]): s["reason"] for s in state["last"]["skipped"] if s["kind"] == kind
    }


# ================================================================ EMA lines
def test_ema_lines_render_at_exact_candles_and_stop_at_the_last_analysed_candle(
    page, served
) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    env = with_payload(rebased("R7", chart), chart)
    state = _show(page, stub, env, "current")
    lines = {line["name"]: line for line in _of(state, "ema_line")}
    assert set(lines) == {"EMA20", "EMA50", "EMA200"}
    last_t = env["result"]["inputs"]["window"]["last_t"]
    for line in lines.values():
        pts = [p for run in line["runs"] for p in run]
        assert pts and max(_ms(p["t"]) for p in pts) == _ms(last_t)  # ends at the analysed candle
        for p in pts[-5:] + pts[:3]:
            assert abs(p["x"] - _x_of(page, p["t"])) < 0.01  # exact candle positions
    assert chart["bars"][-1]["t"] != last_t  # the newer (forming) chart candle has no line point
    ema200 = [p for run in lines["EMA200"]["runs"] for p in run]
    assert all(not p["converged"] for p in ema200[:-1]) and ema200[-1]["converged"]


def test_lines_break_at_null_and_missing_points(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    closed = [b for b in chart["bars"] if b["complete"] is True]
    null_t, missing_t = closed[-10]["t"], closed[-20]["t"]
    env = with_payload(rebased("R7", chart), chart, null={null_t}, drop={missing_t})
    state = _show(page, stub, env, "current")
    (ema20,) = [line for line in _of(state, "ema_line") if line["name"] == "EMA20"]
    times = [[p["t"] for p in run] for run in ema20["runs"]]
    flat = [t for run in times for t in run]
    assert null_t not in flat and missing_t not in flat
    for run in times:  # no run bridges either gap
        ms = [_ms(t) for t in run]
        assert not (min(ms) < _ms(null_t) < max(ms)) and not (min(ms) < _ms(missing_t) < max(ms))
    assert len(times) >= 3


def test_unmappable_ema_points_are_suppressed_and_reported(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    env = with_payload(rebased("R7", chart), chart)
    pts = env["overlay"]["ema"]["EMA50"]["points"]
    pts.append(["2026-11-11T13:30:00Z", 1.1])  # not a candle open time
    pts.append([chart["bars"][-1]["t"], 1.1])  # after the analysis window
    state = _show(page, stub, env, "current")
    flat = [
        p["t"]
        for line in _of(state, "ema_line")
        if line["name"] == "EMA50"
        for run in line["runs"]
        for p in run
    ]
    assert "2026-11-11T13:30:00Z" not in flat and chart["bars"][-1]["t"] not in flat
    assert "2 point(s)" in _skipped(state, "ema_line")["EMA50"]


# ================================================================ swings
def test_confirmed_swings_render_at_their_pivot_candles(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    env = with_payload(rebased("R7", chart), chart)
    state = _show(page, stub, env, "current")
    drawn = {s["id"]: s for s in _of(state, "swing")}
    assert set(drawn) == {s["id"] for s in env["overlay"]["swings"]["confirmed"]}
    for swing in env["overlay"]["swings"]["confirmed"]:
        marker = drawn[swing["id"]]
        assert abs(marker["x"] - _x_of(page, swing["pivot_t"])) < 0.01
        assert (
            marker["type"] == swing["type"]
            and marker["confirmed_at_utc"] == swing["confirmed_at_utc"]
        )


def test_invalid_or_not_yet_confirmed_swings_are_suppressed(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    env = with_payload(rebased("R7", chart), chart)
    s = env["overlay"]["swings"]["confirmed"]
    as_of = env["result"]["as_of_utc"]
    s[0]["confirmed_at_utc"] = _iso(_ms(as_of) + 3600)  # after the analysed close
    s[1]["confirmation_status"] = "pending"
    s[2]["pivot_t"] = "2026-11-11T13:30:00Z"  # no such candle
    s[3]["confirmed_at_utc"] = s[3]["pivot_t"]  # not after the pivot
    state = _show(page, stub, env, "current")
    assert _of(state, "swing") == []
    reasons = _skipped(state, "swing")
    assert reasons[s[0]["id"]] == "not yet confirmed at the analysed close"
    assert reasons[s[1]["id"]] == "not confirmed"
    assert reasons[s[2]["id"]] == "pivot candle outside the coverage or not on the chart"
    assert reasons[s[3]["id"]] == "invalid confirmation time"


def test_decision_pivots_are_not_repeated_as_swing_rings(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    env = rebased("R7", chart)
    pivot = env["result"]["features"]["swings"]["pivots"][0]
    closed = [b["t"] for b in chart["bars"] if b["complete"] is True]
    by = closed[closed.index(pivot["pivot_t"]) + 2]
    same = {
        "id": f"{pivot['type']}@{pivot['pivot_t']}",
        "type": pivot["type"],
        "price": pivot["price"],
        "pivot_t": pivot["pivot_t"],
        "confirmed_by_t": by,
        "confirmed_at_utc": pivot["confirmed_at_utc"],
        "confirmation_status": "confirmed",
    }
    state = _show(page, stub, with_payload(env, chart, swings=[same]), "current")
    assert _of(state, "swing") == [] and _of(state, "pivot")


# ================================================================ payload integrity and states
@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda o: o.update(snapshot_id="other"), "overlay snapshot differs"),
        (lambda o: o.update(symbol="GBPUSD"), "overlay symbol or timeframe differs"),
        (lambda o: o.update(timeframe="H4"), "overlay symbol or timeframe differs"),
        (lambda o: o.update(broker_symbol="OTHERc"), "overlay broker symbol differs"),
        (lambda o: o["window"].update(first_t="2020-01-01T00:00:00Z"), "overlay window differs"),
        (lambda o: o.update(last_analysed_t="2020-01-01T00:00:00Z"), "overlay window differs"),
        (
            lambda o: o.update(validation_verdict="provisional"),
            "overlay validation verdict differs",
        ),
        (lambda o: o.update(as_of_utc="2020-01-01T00:00:00Z"), "overlay as-of time differs"),
        (lambda o: o["coverage"].update(last_t="2020-01-01T00:00:00Z"), "overlay coverage"),
        (lambda o: o.update(status="unavailable", reasons=["x"]), "overlay unavailable: x"),
    ],
)
def test_inconsistent_payload_suppresses_phase2_only(page, served, mutate, reason) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    env = with_payload(rebased("R7", chart), chart)
    mutate(env["overlay"])  # a changed snapshot_id stays different from the re-marked ids
    state = _show(page, stub, env, "current")
    assert _of(state, "ema_line") == [] and _of(state, "swing") == []
    overlay_reasons = [s["reason"] for s in state["last"]["skipped"] if s["kind"] == "overlay"]
    assert len(overlay_reasons) == 1 and reason in overlay_reasons[0]
    assert _of(state, "ema") and _of(state, "zone")  # phase 1 remains


def test_missing_payload_is_reported_not_guessed(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    env = rebased("R7", chart)
    env.pop("overlay", None)
    state = _show(page, stub, env, "current")
    assert _of(state, "ema_line") == [] and _of(state, "swing") == []
    assert _skipped(state, "overlay") == {"overlay": "no overlay payload in this analysis"}


def test_provisional_shows_muted_lines_without_swings(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    state = _show(page, stub, with_payload(rebased("R2", chart), chart), "provisional")
    assert len(_of(state, "ema_line")) == 3 and _of(state, "swing") == []
    panel = page.evaluate("document.querySelector('#overlay-panel').textContent")
    assert panel.count("line:") == 3 and "(PROVISIONAL)" in panel


def test_outdated_and_rejected_show_no_phase2(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    _show(page, stub, with_payload(rebased("R7", chart), chart), "current")
    old = with_payload(rebased("R7", chart, last_offset=2), chart)
    state = _show(page, stub, old, "outdated")
    assert state["last"]["annotations"] == []
    rejected = copy.deepcopy(ENVELOPES["R1_history"])
    rejected["result"]["broker_symbol"] = chart["broker"]
    state = _show(page, stub, rejected, "unavailable")
    assert state["last"]["annotations"] == []


# ================================================================ disabled, interactions
def test_disabled_overlay_runs_no_model_or_drawing(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    page.evaluate(
        "window.__models = 0; const realModel = overlayModel;"
        " overlayModel = (...args) => { window.__models += 1; return realModel(...args); };"
    )
    stub.envelope = with_payload(rebased("R7", chart), chart)
    before = page.evaluate("livePollingState().analysis.requests")
    page.evaluate("livePolling.analysis.now(); livePolling.bars.now()")
    page.wait_for(
        f"livePollingState().analysis.requests > {before}"
        " && !livePollingState().analysis.inFlight && !livePollingState().bars.inFlight"
    )
    page.wait_for("state.analysis !== null")
    assert page.evaluate("window.__models") == 0
    assert page.evaluate("overlayState().draws") == 0


def test_lines_and_swings_follow_zoom_and_pan(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    _show(page, stub, with_payload(rebased("R7", chart), chart), "current")
    box = _box(page)
    x, y = _plot_x(box, 0.6), box["y"] + box["h"] / 2
    _wheel(page, x, y, -120, times=2)
    _drag(page, x, y, x - 150, y)
    page.wait_for("chartViewState().follow === false")
    state = page.evaluate("overlayState()")
    for line in _of(state, "ema_line"):
        for run in line["runs"]:
            for p in run[:2] + run[-2:]:
                assert abs(p["x"] - _x_of(page, p["t"])) < 0.01
    for swing in _of(state, "swing"):
        assert abs(swing["x"] - _x_of(page, swing["t"])) < 0.01
