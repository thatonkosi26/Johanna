"""Analysis Overlay (V1, phase 1): toggle, fail-closed validation, exact candle mapping, analysis
states and chart-interaction preservation, in a real headless browser against the real backend
(FakeMT5 chart data; the analysis service replaced by a stub returning real I1 envelopes).

Envelopes are real I1 envelopes *rebased* onto the chart's own candles (window, EMA, pullback,
pivot, zone and check timestamps and prices) so exact mapping can be exercised (test data only;
the envelope schema is unchanged).
"""

from __future__ import annotations

import copy
import json
import time
from datetime import UTC, datetime

import pytest
from browser_harness import Browser
from fake_mt5 import FakeMT5
from test_browser_analysis import BROWSER, ENVELOPES, StubAnalysis, _ready
from test_browser_smoke import _box, _drag, _listener_count, _mouse, _plot_x, _serve, _wheel

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


# FakeMT5 builds its bars from (t // step) % 7 and % 2: on the default H1 chart the synthetic
# market repeats every 14 candles, so a test whose layout depends on prices pins the phase.
# At phase 0 the label placer (by design) hides one zone label that cannot be placed without
# overlap, which the pinned-label test's per-zone assertion does not expect; phases 1-13 place
# every zone label. Phase 7 makes the test's synthetic market identical on every run.
PATTERN_PHASE = 7


def fixed_phase_now(phase: int):
    """A synthetic market clock at a fixed phase of the fake's 14-hour price cycle (half past
    the candle), running forward in real time and always in the past."""
    real = time.time()
    index = int(real // 3600)
    target = (index - (index - phase) % 14) * 3600 + 1800
    if target > real - 60:
        target -= 14 * 3600
    lag = real - target
    return lambda: time.time() - lag


@pytest.fixture
def served_fixed_clock(tmp_path):
    fake_now = fixed_phase_now(PATTERN_PHASE)
    stub = StubAnalysis(ENVELOPES["R7"])
    with _serve(
        tmp_path,
        fake=FakeMT5(now=fake_now),
        clock=lambda: datetime.fromtimestamp(fake_now(), UTC),
    ) as (url, app):
        app.market_analysis = stub
        yield url, app, stub


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _chart(page) -> dict:
    return page.evaluate(
        "(() => { const d = chartData(); return { symbol: d.symbol, timeframe: d.timeframe,"
        " broker: d.broker_symbol, bars: d.bars.map(b => ({ t: b.t, o: b.o, h: b.h, l: b.l,"
        " c: b.c, complete: b.complete })) }; })()"
    )


def rebased(name: str, chart: dict, *, trusted_zones: bool = True, last_offset: int = 0) -> dict:
    """A real envelope whose analysis references the chart's own closed candles."""
    env = copy.deepcopy(ENVELOPES[name])
    closed = [b for b in chart["bars"] if b["complete"] is True]
    last = closed[-1 - last_offset]
    li = closed.index(last)
    step = datetime.fromisoformat(closed[1]["t"].replace("Z", "+00:00")) - datetime.fromisoformat(
        closed[0]["t"].replace("Z", "+00:00")
    )
    as_of = iso(datetime.fromisoformat(last["t"].replace("Z", "+00:00")) + step)
    window = {"count": li + 1, "first_t": closed[0]["t"], "last_t": last["t"]}
    r, wv = env["result"], env["window_validation"]
    r["inputs"]["window"] = dict(window)
    wv["window"] = dict(window)
    r["as_of_utc"] = as_of
    r["broker_symbol"] = chart["broker"]
    price = last["c"]
    f = r["features"]
    for k, (n, off) in enumerate((("EMA20", 0.0004), ("EMA50", 0.0012), ("EMA200", 0.0025))):
        f["latest"][n].update(value=round(price + off, 5), t=last["t"], status="available")
        f["latest"][n]["usable"] = True
    rows = closed[li - 4 : li + 1]
    f["pullback"] = {
        "lookback": 5,
        "usable": True,
        "reason": None,
        "candles": [
            {
                "t": b["t"],
                "low": b["l"],
                "high": b["h"],
                "close": b["c"],
                "ema20": round(b["c"] + 0.0003, 5),
                "ema50": round(b["c"] + 0.0010, 5),
                "atr": 0.001,
                "d_bullish": 0.5,
                "d_bearish": 0.5,
            }
            for b in rows
        ],
    }
    pick = {"H_prev": li - 30, "H_last": li - 16, "L_prev": li - 24, "L_last": li - 9}
    f["swings"] = {
        "usable": True,
        "reason": None,
        "state": "lower_highs_lower_lows",
        "pending_ignored": 0,
        "pivots": [
            {
                "role": role,
                "type": "high" if role.startswith("H") else "low",
                "price": closed[i]["h"] if role.startswith("H") else closed[i]["l"],
                "pivot_t": closed[i]["t"],
                "confirmed_at_utc": closed[i + 3]["t"],
            }
            for role, i in pick.items()
        ],
    }
    dims = r["market_condition"]["dimensions"]
    dims["swing_structure"]["trusted"] = True if name != "R2" else False
    recent = closed[max(0, li - 60) : li + 1]
    gap = (max(b["h"] for b in recent) - min(b["l"] for b in recent)) / 12 or 0.0002
    zones = []
    for k in range(4):
        zones.append(
            {
                "id": f"S{k + 1}",
                "side": "support",
                "level": price - gap * (k + 1),
                "low": price - gap * (k + 1),
                "high": price - gap * (k + 1) + gap * 0.2,
                "zone_low": price - gap * (k + 1) - gap * 0.2,
                "zone_high": price - gap * (k + 1) + gap * 0.3,
                "strength": 2 + k,
                "latest_confirmed_at_utc": closed[li - 20]["t"],
                "contains_price": False,
                "distance_atr": 2.0 * (k + 1),
                "position": "below",
            }
        )
        zones.append(
            {
                "id": f"R{k + 1}",
                "side": "resistance",
                "level": price + gap * (k + 1),
                "low": price + gap * (k + 1),
                "high": price + gap * (k + 1) + gap * 0.2,
                "zone_low": price + gap * (k + 1) - gap * 0.2,
                "zone_high": price + gap * (k + 1) + gap * 0.3,
                "strength": 2 + k,
                "latest_confirmed_at_utc": closed[li - 20]["t"],
                "contains_price": False,
                "distance_atr": 2.0 * (k + 1),
                "position": "above",
            }
        )
    dims["location"].update(
        trusted=trusted_zones and name != "R2",
        zones=zones,
        nearest_below={"id": "S1", "distance_atr": 2.0},
        nearest_above={"id": "R1", "distance_atr": 2.0},
    )
    checks = {"TC3_long_term": last["t"], "TC4_pullback": rows[1]["t"], "TC5_resumption": last["t"]}
    for check in r["opportunity"].get("checklist") or []:
        if check["id"] in checks:
            check["t"] = checks[check["id"]]
    return env


_SEQ = iter(range(1, 10**6))


def _show(page, stub, envelope, mode, timeout=10) -> dict:
    """Deliver ``envelope`` and wait until the overlay reflects exactly that envelope (marked
    with a unique envelope snapshot id) in the expected mode."""
    envelope = copy.deepcopy(envelope)
    marker = f"overlay-test-{next(_SEQ)}"
    old = envelope["snapshot"]["snapshot_id"]
    envelope["snapshot"]["snapshot_id"] = marker
    for holder in (envelope["result"]["inputs"], envelope["window_validation"]):
        if holder.get("snapshot_id") == old:  # keep deliberate identity mismatches
            holder["snapshot_id"] = marker
    stub.envelope = envelope
    page.evaluate("livePolling.analysis.now()")
    page.wait_for(
        f"state.analysis?.snapshot?.snapshot_id === {json.dumps(marker)}"
        f" && overlayState().last?.mode === {json.dumps(mode)}",
        timeout=timeout,
    )
    return page.evaluate("overlayState()")


def _enable(page) -> None:
    page.evaluate("document.querySelector('[data-overlay-mode=\"analysis\"]').click()")
    page.wait_for("overlayState().enabled === true")


def _kinds(state) -> dict:
    out: dict[str, int] = {}
    for a in (state["last"] or {}).get("annotations", []):
        out[a["kind"]] = out.get(a["kind"], 0) + 1
    return out


def _x_of(page, t: str) -> float:
    """Candle centre x (canvas coordinates) from the chart's own viewport geometry."""
    return page.evaluate(
        f"(() => {{ const g = chartGeometry(), v = state.chartViewport;"
        f" const i = g.bars.findIndex(b => b.t === {json.dumps(t)});"
        f" const plotW = g.rect.width - CHART_PAD.left - CHART_PAD.right;"
        f" const slot = plotW / v.count;"
        f" return CHART_PAD.left + slot * (i - v.start) + slot / 2; }})()"
    )


# ================================================================ toggle and clean chart
def test_overlay_is_off_by_default_and_charts_page_only(page, served) -> None:
    url, _, _ = served
    _ready(page, url, "overview")
    assert page.evaluate("document.querySelector('[data-overlay-mode]')") is None
    assert page.evaluate("document.querySelector('#analysis-overlay')") is None
    _ready(page, url)
    assert page.evaluate("overlayState().enabled") is False
    assert (
        page.evaluate(
            "document.querySelector('[data-overlay-mode=\"clean\"]').getAttribute('aria-pressed')"
        )
        == "true"
    )
    assert page.evaluate("document.querySelector('#analysis-overlay').hidden") is True
    page.evaluate("livePolling.analysis.now()")
    time.sleep(1.5)
    assert page.evaluate("overlayState().draws") == 0  # nothing drawn while disabled


def test_toggle_on_off_and_clean_chart_restored(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    draws_before = page.evaluate("state.chartDraws")
    _enable(page)
    state = _show(page, stub, rebased("R7", chart), "current")
    kinds = _kinds(state)
    for kind in ("zone", "pullback", "ema_segment", "check", "pivot", "endpoint", "ema"):
        assert kinds.get(kind), (kind, kinds)
    assert page.evaluate("document.querySelector('#analysis-overlay').hidden") is False
    assert page.evaluate("state.chartDraws") == draws_before  # the chart was not repainted
    page.evaluate("document.querySelector('[data-overlay-mode=\"clean\"]').click()")
    assert page.evaluate("overlayState()") == {
        "enabled": False,
        "draws": state["draws"],
        "last": None,
    }
    assert page.evaluate("document.querySelector('#analysis-overlay').hidden") is True
    assert page.evaluate("document.querySelector('#overlay-panel').hidden") is True
    page.evaluate("livePolling.analysis.now()")
    time.sleep(1.5)
    assert page.evaluate("overlayState().draws") == state["draws"]  # disabled: no drawing


def test_repeated_toggling_adds_no_requests_timers_or_listeners(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    kinds = ("click", "pointerdown", "pointermove", "pointerup", "wheel", "dblclick")
    before = {k: _listener_count(page, "document", k) for k in kinds}
    window_resize = _listener_count(page, "window", "resize")
    timers = page.evaluate("livePollingState().timers")
    calls = len(stub.calls)
    for _ in range(10):
        page.evaluate("document.querySelector('[data-overlay-mode=\"analysis\"]').click()")
        page.evaluate("document.querySelector('[data-overlay-mode=\"clean\"]').click()")
    assert {k: _listener_count(page, "document", k) for k in kinds} == before
    assert _listener_count(page, "window", "resize") == window_resize
    assert page.evaluate("livePollingState().timers") == timers
    assert len(stub.calls) == calls  # toggling never requests analysis
    assert page.evaluate("document.querySelectorAll('canvas').length") == 2


# ================================================================ alignment and validation
def test_ema_tags_anchor_at_the_last_analysed_candle_not_the_newest(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    env = rebased("R7", chart)
    state = _show(page, stub, env, "current")
    last_t = env["result"]["inputs"]["window"]["last_t"]
    newest_t = chart["bars"][-1]["t"]
    assert newest_t != last_t  # the chart has a newer (forming) candle
    emas = [a for a in state["last"]["annotations"] if a["kind"] == "ema"]
    assert {a["name"] for a in emas} == {"EMA20", "EMA50", "EMA200"}
    expected_x = _x_of(page, last_t)
    assert all(abs(a["x"] - expected_x) < 0.01 and a["t"] == last_t for a in emas)
    assert expected_x < _x_of(page, newest_t)
    (endpoint,) = [a for a in state["last"]["annotations"] if a["kind"] == "endpoint"]
    assert endpoint["t"] == last_t  # the marker is the last analysed candle
    for a in state["last"]["annotations"]:  # nothing extends beyond the analysis window
        if "t" in a:
            assert a["t"] <= last_t
        if a["kind"] == "zone":
            assert a["right"] <= expected_x + 50


def test_exact_candle_mapping_for_pivots_pullback_and_checks(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    env = rebased("R7", chart)
    state = _show(page, stub, env, "current")
    f = env["result"]["features"]
    pivots = {a["role"]: a for a in state["last"]["annotations"] if a["kind"] == "pivot"}
    for p in f["swings"]["pivots"]:
        assert pivots[p["role"]]["t"] == p["pivot_t"]
        assert abs(pivots[p["role"]]["x"] - _x_of(page, p["pivot_t"])) < 0.01
    assert pivots["H_last"]["label"] == "LH" and pivots["L_last"]["label"] == "LL"
    columns = [a["t"] for a in state["last"]["annotations"] if a["kind"] == "pullback"]
    assert columns == [row["t"] for row in f["pullback"]["candles"]]
    checks = {a["id"]: a["t"] for a in state["last"]["annotations"] if a["kind"] == "check"}
    assert checks["TC4_pullback"] == f["pullback"]["candles"][1]["t"]


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda e: e["result"].update(broker_symbol="OTHERc"), "broker symbol"),
        (lambda e: e["result"]["inputs"].update(snapshot_id="other"), "Snapshot identity"),
        (lambda e: e["window_validation"].update(snapshot_id="other"), "Snapshot identity"),
        (
            lambda e: e["window_validation"]["window"].update(last_t="2020-01-01T00:00:00Z"),
            "window",
        ),
        (lambda e: e["result"]["inputs"]["provenance"].update(passed=False), "provenance"),
        (lambda e: e["result"]["features"]["pullback"]["candles"][2].update(low=0.5), "differs"),
    ],
)
def test_inconsistent_envelopes_fail_closed(page, served, mutate, reason) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    env = rebased("R7", chart)
    mutate(env)
    state = _show(page, stub, env, "unavailable")
    assert state["last"]["annotations"] == []
    assert reason in state["last"]["reason"]
    assert reason in page.evaluate("document.querySelector('#overlay-panel').textContent")


def test_symbol_and_timeframe_mismatch_are_rejected_by_the_model(page, served) -> None:
    url, _, _ = served
    _ready(page, url)
    env = rebased("R7", _chart(page))
    for field, value, reason in (
        ("symbol", "GBPUSD", "different symbol"),
        ("timeframe", "H4", "different timeframe"),
    ):
        bad = copy.deepcopy(env)
        bad[field] = value
        bad["result"][field] = value
        model = page.evaluate(
            f"overlayModel({{ state: 'current', envelope: {json.dumps(bad)}, reasons: [] }},"
            " chartData())"
        )
        assert model["layers"] is None and reason in model["reason"]
    good = page.evaluate(
        f"overlayModel({{ state: 'current', envelope: {json.dumps(env)}, reasons: [] }},"
        " chartData()).mode"
    )
    assert good == "current"


def test_annotations_outside_the_window_or_off_chart_are_not_drawn(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    env = rebased("R7", chart)
    newest = chart["bars"][-1]["t"]  # the forming candle: after the analysis window
    env["result"]["features"]["swings"]["pivots"][0]["pivot_t"] = newest
    env["result"]["features"]["swings"]["pivots"][1]["pivot_t"] = "2026-11-11T13:30:00Z"  # inexact
    state = _show(page, stub, env, "current")
    roles = {a["role"] for a in state["last"]["annotations"] if a["kind"] == "pivot"}
    assert roles == {"L_prev", "L_last"}
    assert {s["name"] for s in state["last"]["skipped"] if s["kind"] == "pivot"} == {
        "H_prev",
        "H_last",
    }


# ================================================================ analysis states
def test_provisional_is_labelled_and_restricted(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    state = _show(page, stub, rebased("R2", chart), "provisional")
    kinds = _kinds(state)
    assert not kinds.get("pivot") and not kinds.get("check") and not kinds.get("pullback")
    zones = [a for a in state["last"]["annotations"] if a["kind"] == "zone"]
    assert zones and all(z["trusted"] is False and "untrusted" in z["label"] for z in zones)
    panel = page.evaluate("document.querySelector('#overlay-panel').textContent")
    assert "PROVISIONAL" in panel and "diagnostic only" in panel


def test_rejected_analysis_shows_no_annotations(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    rejected = copy.deepcopy(ENVELOPES["R1_history"])
    rejected["result"]["broker_symbol"] = chart["broker"]
    state = _show(page, stub, rejected, "unavailable")
    assert state["last"]["annotations"] == []
    panel = page.evaluate("document.querySelector('#overlay-panel').textContent")
    assert "ANALYSIS UNAVAILABLE" in panel and "insufficient_history" in panel
    assert page.evaluate("chartViewState() !== null")  # the clean chart remains usable


def test_outdated_and_superseded_analysis_is_cleared(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    _show(page, stub, rebased("R7", chart), "current")
    state = _show(page, stub, rebased("R7", chart, last_offset=2), "outdated")  # newer closed
    assert state["last"]["annotations"] == []
    assert "OUTDATED" in page.evaluate("document.querySelector('#overlay-panel').textContent")
    state = _show(page, stub, rebased("R7", chart), "current")  # a new snapshot supersedes
    assert state["last"]["snapshotId"].startswith("overlay-test-") and state["last"]["annotations"]


def test_selection_change_clears_the_overlay_until_new_analysis(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    _enable(page)
    _show(page, stub, rebased("R7", _chart(page)), "current")
    stub.delay["EURUSD"] = 3.0  # the next analysis answer is slow
    page.select("#timeframe-select", "H4")
    page.wait_for("prefs().selected_chart_timeframe === 'H4'", timeout=10)
    page.wait_for("overlayState().last?.mode !== 'current'", timeout=5)
    assert page.evaluate("overlayState().last?.annotations?.length ?? 0") == 0
    assert page.evaluate("overlayState().enabled") is True  # the toggle survives re-rendering


def test_untrusted_zones_are_muted_and_missing_location_gives_no_zones(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    state = _show(page, stub, rebased("R7", chart, trusted_zones=False), "current")
    zones = [a for a in state["last"]["annotations"] if a["kind"] == "zone"]
    assert zones and all(not z["trusted"] and "untrusted" in z["label"] for z in zones)
    assert "Zones untrusted" in page.evaluate(
        "document.querySelector('#overlay-panel').textContent"
    )
    env = rebased("R7", chart)
    del env["result"]["market_condition"]["dimensions"]["location"]
    state = _show(page, stub, env, "current")
    assert not _kinds(state).get("zone")
    assert any(s["kind"] == "zones" for s in state["last"]["skipped"])


def test_zone_selection_is_deterministic(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    env = rebased("R7", chart)
    ids = []
    for _ in range(2):
        state = _show(page, stub, env, "current")
        ids.append([a["id"] for a in state["last"]["annotations"] if a["kind"] == "zone"])
    assert ids[0] == ids[1] and ids[0][:2] == ["S1", "R1"]  # nearest below, nearest above first
    assert len([i for i in ids[0] if i.startswith("S")]) <= 3


# ================================================================ interactions
def test_chart_interactions_work_with_the_overlay_enabled(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    _enable(page)
    _show(page, stub, rebased("R7", _chart(page)), "current")
    assert (
        page.evaluate("getComputedStyle(document.querySelector('#analysis-overlay')).pointerEvents")
        == "none"
    )
    box = _box(page)
    x, y = _plot_x(box, 0.6), box["y"] + box["h"] / 2
    assert page.evaluate(f"candleAtX({x})") is not None  # candle selection
    count = page.evaluate("chartViewState().count")
    _wheel(page, x, y, -120, times=3)  # zoom in
    assert page.evaluate("chartViewState().count") < count
    start = page.evaluate("chartViewState().start")
    _drag(page, x, y, x - 200, y)  # drag left: pan towards older candles
    assert page.evaluate("chartViewState().start") < start
    assert page.evaluate("chartViewState().follow") is False
    axis_x = box["x"] + box["w"] - 30
    _drag(page, axis_x, y, axis_x, y + 80)  # price scale
    assert page.evaluate("chartViewState().priceScale") != 1
    _mouse(page, "mousePressed", axis_x, y, button="left", buttons=1, clickCount=2)
    _mouse(page, "mouseReleased", axis_x, y, button="left", buttons=0, clickCount=2)
    assert page.evaluate("chartViewState().priceScale") == 1  # double-click reset
    # the overlay followed the chart: annotations re-positioned on the new viewport
    page.wait_for("overlayState().last?.mode === 'current'")
    last = page.evaluate("overlayState()")["last"]
    for a in last["annotations"]:
        if a["kind"] == "pivot":
            assert abs(a["x"] - _x_of(page, a["t"])) < 0.01


def test_overlay_canvas_follows_resize(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    _enable(page)
    _show(page, stub, rebased("R7", _chart(page)), "current")
    size = (
        "(() => { const c = document.querySelector('#price-chart').getBoundingClientRect(),"
        " o = document.querySelector('#analysis-overlay').getBoundingClientRect();"
        " return [c.left, c.top, c.width, c.height, o.left, o.top, o.width, o.height]; })()"
    )
    a = page.evaluate(size)
    assert a[:4] == a[4:]
    page.set_viewport(1100, 900)
    page.wait_for(
        "(() => { const c = document.querySelector('#price-chart').getBoundingClientRect(),"
        " o = document.querySelector('#analysis-overlay').getBoundingClientRect();"
        " return c.width === o.width && c.height === o.height && c.left === o.left; })()",
        timeout=5,
    )
    b = page.evaluate(size)
    assert b[2] != a[2] and b[:4] == b[4:]


# ================================================================ visual clarity corrections
STATUS_PIXEL = """(() => {
  const s = overlayState().last.status, c = document.querySelector('#analysis-overlay');
  const r = window.devicePixelRatio || 1;
  const px = c.getContext('2d')
    .getImageData(Math.round((s.x + 2) * r), Math.round((s.y + 2) * r), 1, 1).data;
  const probe = document.createElement('canvas').getContext('2d');
  probe.fillStyle = getComputedStyle(document.documentElement).getPropertyValue('--panel').trim();
  probe.fillRect(0, 0, 1, 1);
  const want = probe.getImageData(0, 0, 1, 1).data;
  return { px: Array.from(px), want: Array.from(want) };
})()"""
PLOT = (
    "(() => { const r = document.querySelector('#price-chart').getBoundingClientRect();"
    " return { left: CHART_PAD.left, top: CHART_PAD.top, right: r.width - CHART_PAD.right,"
    " bottom: r.height - CHART_PAD.bottom }; })()"
)


def _labels(state, kind):
    return [a["label"] for a in state["last"]["annotations"] if a["kind"] == kind]


def test_accepted_ema_tags_have_no_provisional_suffix(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    _enable(page)
    state = _show(page, stub, rebased("R7", _chart(page)), "current")
    labels = _labels(state, "ema")
    assert len(labels) == 3 and not any("PROVISIONAL" in label for label in labels)
    assert "(PROVISIONAL)" not in page.evaluate(
        "document.querySelector('#overlay-panel').textContent"
    )


def test_provisional_ema_tags_always_carry_the_suffix(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    env = rebased("R2", chart)
    state = _show(page, stub, env, "provisional")
    labels = _labels(state, "ema")
    assert len(labels) == 3 and all(label.endswith(" (PROVISIONAL)") for label in labels)
    latest = env["result"]["features"]["latest"]
    for label in labels:  # the backend value at the chart's precision, unchanged
        name, value = label.split(" ")[:2]
        assert value == f"{latest[name]['value']:.{len(value.split('.')[1])}f}"
    panel = page.evaluate("document.querySelector('#overlay-panel').textContent")
    assert panel.count("(PROVISIONAL)") == 3


@pytest.mark.parametrize(("name", "mode"), [("R7", "current"), ("R2", "provisional")])
def test_status_label_is_drawn_last_opaque_and_inside_the_plot(page, served, name, mode) -> None:
    url, _, stub = served
    _ready(page, url)
    _enable(page)
    state = _show(page, stub, rebased(name, _chart(page)), mode)
    status = state["last"]["status"]
    assert status["text"] == {"current": "CURRENT", "provisional": "PROVISIONAL"}[mode]
    assert status["order"] == len(state["last"]["annotations"])  # after every layer
    plot = page.evaluate(PLOT)
    assert status["x"] >= plot["left"] and status["y"] >= plot["top"]
    assert status["x"] + status["w"] <= plot["right"]
    assert status["y"] + status["h"] <= plot["bottom"]
    sample = page.evaluate(STATUS_PIXEL)
    assert sample["px"] == sample["want"] and sample["px"][3] == 255  # opaque background


def test_outdated_and_unavailable_labels_are_also_drawn_legibly(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    _show(page, stub, rebased("R7", chart), "current")
    for env, mode, text in (
        (rebased("R7", chart, last_offset=2), "outdated", "OUTDATED"),
        (ENVELOPES["R1_history"], "unavailable", "ANALYSIS UNAVAILABLE"),
    ):
        state = _show(page, stub, env, mode)
        assert state["last"]["status"]["text"] == text
        sample = page.evaluate(STATUS_PIXEL)
        assert sample["px"] == sample["want"]


def test_pinned_zone_labels_stay_below_the_status_label(page, served_fixed_clock) -> None:
    url, _, stub = served_fixed_clock
    _ready(page, url)
    _enable(page)
    env = rebased("R7", _chart(page))
    zones = env["result"]["market_condition"]["dimensions"]["location"]["zones"]
    r1 = next(z for z in zones if z["id"] == "R1")
    r1["zone_high"] = r1["zone_low"] + 10.0  # extends far above the visible range: label pinned
    state = _show(page, stub, env, "current")
    status = state["last"]["status"]
    drawn = [a for a in state["last"]["annotations"] if a["kind"] == "zone"]
    pinned = [z for z in drawn if z["id"] == "R1"]
    assert pinned and pinned[0]["top"] < status["y"]  # its band starts above the plot
    for zone in drawn:
        assert zone["labelY"] - 10 >= status["y"] + status["h"], zone  # text top below the box


def test_untrusted_zones_are_neutral_grey_and_trusted_keep_direction(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    trusted = _show(page, stub, rebased("R7", chart), "current")
    for zone in (a for a in trusted["last"]["annotations"] if a["kind"] == "zone"):
        assert zone["color"] == ("--red" if zone["side"] == "resistance" else "--green")
        assert zone["trusted"] is True and "untrusted" not in zone["label"]
    for env, mode in (
        (rebased("R7", chart, trusted_zones=False), "current"),
        (rebased("R2", chart), "provisional"),
    ):
        state = _show(page, stub, env, mode)
        zones = [a for a in state["last"]["annotations"] if a["kind"] == "zone"]
        assert zones and all(z["color"] == "--dim" and "untrusted" in z["label"] for z in zones)


def test_invalid_zones_are_excluded_and_listed_with_reasons(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    _enable(page)
    env = rebased("R7", _chart(page))
    location = env["result"]["market_condition"]["dimensions"]["location"]
    good = location["zones"][0]
    location["zones"] += [
        {**good, "id": ""},
        {**good, "id": "BADP", "zone_low": None},
        {**good, "id": "REV", "zone_low": good["zone_high"] + 1, "zone_high": good["zone_high"]},
        {**good, "id": "POS", "position": "inside"},
        "not a zone",
    ]
    state = _show(page, stub, env, "current")
    drawn = {a["id"] for a in state["last"]["annotations"] if a["kind"] == "zone"}
    assert not drawn & {"", "BADP", "REV", "POS"}
    reasons = {s["name"]: s["reason"] for s in state["last"]["skipped"] if s["kind"] == "zone"}
    n = len(location["zones"])
    assert reasons == {
        f"zone #{n - 4}": "invalid zone identifier",
        "BADP": "invalid price boundary",
        "REV": "reversed price boundaries",
        "POS": "invalid position",
        f"zone #{n}": "invalid zone record",
    }
    panel = page.evaluate("document.querySelector('#overlay-panel').textContent")
    for reason in reasons.values():
        assert reason in panel


def test_disabled_overlay_leaves_an_empty_hidden_canvas_and_the_chart_alone(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart_signature = page.evaluate("document.querySelector('#price-chart').__signature")
    draws = page.evaluate("state.chartDraws")
    _enable(page)
    _show(page, stub, rebased("R7", _chart(page)), "current")
    page.evaluate("document.querySelector('[data-overlay-mode=\"clean\"]').click()")
    empty = page.evaluate(
        "(() => { const c = document.querySelector('#analysis-overlay');"
        " const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;"
        " return c.hidden && d.every(v => v === 0); })()"
    )
    assert empty is True
    assert page.evaluate("state.chartDraws") == draws
    assert page.evaluate("document.querySelector('#price-chart').__signature") == chart_signature
