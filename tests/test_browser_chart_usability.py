"""Chart usability (post phase 2 live verification): zones containing the price (D1), label
placement and the EMA warm-up legend, expected chart-range differences, and the persistent
Annotations dropdown. Real headless browser against the real backend (FakeMT5 chart data; the
analysis service replaced by a stub returning real I1 envelopes rebased onto the chart).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import browser_harness
import pytest
from browser_harness import Browser
from fake_mt5 import FakeMT5
from test_browser_analysis import BROWSER, ENVELOPES, StubAnalysis, _ready
from test_browser_overlay import PLOT, _chart, _enable, _x_of, rebased
from test_browser_overlay_phase2 import _show, with_payload
from test_browser_smoke import _serve

pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(BROWSER is None, reason="no Chromium-family browser installed"),
]

KEY = "johanna.chart.annotationPreferences.v1"
# an older record (some keys only), an unknown key and a non-boolean value
OLD_RECORD = "{ema20: false, bogus: false, ema50: 'no'}"
CHECKED_MAP = (
    "Object.fromEntries([...document.querySelectorAll('[data-annotation-key]')]"
    ".map(b => [b.dataset.annotationKey, b.checked]))"
)
DENY_STORAGE = (
    "Object.defineProperty(window, 'localStorage', { get() { throw new Error('denied'); } });"
)
ALL_KEYS = [
    "ema20",
    "ema50",
    "ema200",
    "swingHighs",
    "swingLows",
    "decisionPivots",
    "supportZones",
    "resistanceZones",
    "containsZones",
    "pullback",
    "tc3",
    "tc4",
    "tc5",
    "tc4Outline",
    "statusLabel",
    "endpointMarker",
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


def full_envelope(chart, *, trusted_zones=True):
    """Rebased envelope + phase 2 payload + one support and one resistance zone containing the
    price (all three backend positions present)."""
    env = with_payload(rebased("R7", chart, trusted_zones=trusted_zones), chart)
    location = env["result"]["market_condition"]["dimensions"]["location"]
    price = env["result"]["features"]["pullback"]["candles"][-1]["close"]
    base = location["zones"][0]
    for zid, side, half in (("CS", "support", 0.0002), ("CR", "resistance", 0.0004)):
        location["zones"].append(
            {
                **base,
                "id": zid,
                "side": side,
                "position": "contains",
                "contains_price": True,
                "distance_atr": 0.0,
                "zone_low": price - half,
                "zone_high": price + half,
            }
        )
    return env


def _of(state, kind):
    return [a for a in state["last"]["annotations"] if a["kind"] == kind]


def _open_menu(page):
    page.evaluate("document.querySelector('[data-annotation-menu]').click()")
    page.wait_for("!document.querySelector('#annotation-panel').hidden")


def _set(page, key, on):
    page.evaluate(
        f"(() => {{ const box = document.querySelector('[data-annotation-key=\"{key}\"]');"
        f" if (box.checked !== {str(on).lower()}) box.click(); }})()"
    )


def _stored(page):
    return page.evaluate(f"JSON.parse(localStorage.getItem({json.dumps(KEY)}) || 'null')")


def _reload(page, url):
    """A genuine page reload (same URL + hash would only be a same-document navigation): the
    marker set before must be gone afterwards."""
    page.evaluate("window.__beforeReload = true")
    page.open("about:blank")
    _ready(page, url)
    assert page.evaluate("window.__beforeReload === undefined")


# ================================================================ D1: zone positions
def test_above_below_and_contains_zones_all_render(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    env = full_envelope(chart)
    env["result"]["market_condition"]["dimensions"]["location"]["zones"].append(
        {
            **env["result"]["market_condition"]["dimensions"]["location"]["zones"][0],
            "id": "BAD",
            "position": "inside",
        }
    )
    state = _show(page, stub, env, "current")
    zones = {z["id"]: z for z in _of(state, "zone")}
    assert {"above", "below", "contains"} <= {z["position"] for z in zones.values()}
    for zid, side, tone in (("CS", "support", "--green"), ("CR", "resistance", "--red")):
        assert zones[zid]["position"] == "contains" and zones[zid]["side"] == side
        assert zones[zid]["color"] == tone and "price inside zone" in zones[zid]["label"]
    reasons = {s["name"]: s["reason"] for s in state["last"]["skipped"] if s["kind"] == "zone"}
    assert reasons == {"BAD": "invalid position"}  # contains is valid; a genuinely bad one is not
    assert "invalid position" not in json.dumps(
        [s for s in state["last"]["skipped"] if s.get("name") in ("CS", "CR")]
    )


def test_untrusted_contains_zone_is_grey_and_labelled(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    state = _show(page, stub, full_envelope(chart, trusted_zones=False), "current")
    contains = [z for z in _of(state, "zone") if z["position"] == "contains"]
    assert contains and all(
        z["color"] == "--dim" and "price inside zone" in z["label"] and "untrusted" in z["label"]
        for z in contains
    )


# ================================================================ readability
def test_labels_do_not_overlap_and_stay_inside_the_plot(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    state = _show(page, stub, full_envelope(chart), "current")
    plot = page.evaluate(PLOT)
    boxes = [a["labelBox"] for a in state["last"]["annotations"] if a.get("labelBox")]
    status = state["last"]["status"]
    boxes.append({"x": status["x"], "y": status["y"], "w": status["w"], "h": status["h"]})
    assert len(boxes) >= 8
    for box in boxes:
        assert box["x"] >= plot["left"] and box["x"] + box["w"] <= plot["right"], box
        assert box["y"] >= plot["top"] and box["y"] + box["h"] <= plot["bottom"], box
    for i, a in enumerate(boxes):
        for b in boxes[i + 1 :]:
            assert not (
                a["x"] < b["x"] + b["w"]
                and a["x"] + a["w"] > b["x"]
                and a["y"] < b["y"] + b["h"]
                and a["y"] + a["h"] > b["y"]
            ), (a, b)
    checks = [c for c in _of(state, "check") if c["labelBox"]]
    assert checks and all(
        c["labelBox"]["x"] + c["labelBox"]["w"] <= plot["right"] for c in checks
    )  # not clipped
    for hidden in state["last"]["hiddenLabels"]:  # anything hidden is listed, its marker stays
        assert hidden["text"]
    for pivot in _of(state, "pivot"):  # markers keep their exact candle coordinates
        assert abs(pivot["x"] - _x_of(page, pivot["t"])) < 0.01
    for ema in _of(state, "ema"):
        assert abs(ema["x"] - _x_of(page, ema["t"])) < 0.01


def test_ema_legend_explains_the_warm_up(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    state = _show(page, stub, full_envelope(chart), "current")
    (legend,) = _of(state, "legend")
    assert (
        legend["warmup"] is True
        and "warm-up" in legend["text"]
        and "not yet converged" in legend["text"]
    )
    assert set(legend["lines"]) == {"EMA20", "EMA50", "EMA200"}
    env = full_envelope(chart)
    for series in env["overlay"]["ema"].values():  # everything converged: no warm-up wording
        series["converged_from_t"] = series["points"][0][0]
    state = _show(page, stub, env, "current")
    (legend,) = _of(state, "legend")
    assert legend["warmup"] is False and legend["text"] == ""


def test_points_older_than_the_chart_are_reported_as_expected(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    env = full_envelope(chart)
    closed = [b for b in chart["bars"] if b["complete"] is True]
    step = _ms(closed[1]["t"]) - _ms(closed[0]["t"])
    # one candle before the chart's first loaded candle, inside the (widened) analysis window
    older_t = datetime.fromtimestamp(_ms(closed[0]["t"]) - step, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    for holder in (
        env["result"]["inputs"]["window"],
        env["window_validation"]["window"],
        env["overlay"]["window"],
    ):
        holder["first_t"] = older_t
    env["overlay"]["coverage"]["first_t"] = older_t
    for series in env["overlay"]["ema"].values():
        series["points"].insert(0, [older_t, 1.1])
    state = _show(page, stub, env, "current")
    expected = [s for s in state["last"]["skipped"] if s["kind"] == "chart_range"]
    assert {s["name"] for s in expected} == {"EMA20", "EMA50", "EMA200"} and all(
        s["expected"] for s in expected
    )
    assert not [s for s in state["last"]["skipped"] if s["kind"] == "ema_line"]  # not a problem
    panel = page.evaluate("document.querySelector('#overlay-panel').textContent")
    assert "Outside the loaded chart range (expected, not a data problem)" in panel


# ================================================================ dropdown
def test_dropdown_opens_closes_and_has_the_grouped_controls(page, served) -> None:
    url, _, _ = served
    _ready(page, url)
    assert page.evaluate("document.querySelector('#annotation-panel').hidden") is True
    _open_menu(page)
    groups = page.evaluate(
        "[...document.querySelectorAll('.annotation-group legend')].map(l => l.textContent)"
    )
    assert groups == [
        "Trend indicators",
        "Market structure",
        "Price levels",
        "Setup annotations",
        "Analysis context",
    ]
    keys = page.evaluate(
        "[...document.querySelectorAll('[data-annotation-key]')].map(b => b.dataset.annotationKey)"
    )
    assert keys == ALL_KEYS
    page.evaluate("document.querySelector('.chart-hint').click()")  # outside click
    assert page.evaluate("document.querySelector('#annotation-panel').hidden") is True
    _open_menu(page)
    page.send(
        "Input.dispatchKeyEvent",
        type="keyDown",
        key="Escape",
        code="Escape",
        windowsVirtualKeyCode=27,
    )
    page.send(
        "Input.dispatchKeyEvent",
        type="keyUp",
        key="Escape",
        code="Escape",
        windowsVirtualKeyCode=27,
    )
    page.wait_for("document.querySelector('#annotation-panel').hidden")
    _open_menu(page)
    page.evaluate("document.querySelector('[data-annotation-key=\"ema20\"]').click()")
    assert page.evaluate("!document.querySelector('#annotation-panel').hidden")  # stays open
    page.evaluate("document.querySelector('[data-annotation-all=\"0\"]').click()")
    assert all(v is False for v in _stored(page).values()) and len(_stored(page)) == 16
    page.evaluate("document.querySelector('[data-annotation-all=\"1\"]').click()")
    assert all(v is True for v in _stored(page).values())


PRESENT = {
    "ema20": lambda s: any(a["kind"] in ("ema_line", "ema") and a["name"] == "EMA20" for a in s),
    "ema50": lambda s: any(a["kind"] in ("ema_line", "ema") and a["name"] == "EMA50" for a in s),
    "ema200": lambda s: any(a["kind"] in ("ema_line", "ema") and a["name"] == "EMA200" for a in s),
    "swingHighs": lambda s: any(a["kind"] == "swing" and a["type"] == "high" for a in s),
    "swingLows": lambda s: any(a["kind"] == "swing" and a["type"] == "low" for a in s),
    "decisionPivots": lambda s: any(a["kind"] == "pivot" for a in s),
    "supportZones": lambda s: any(
        a["kind"] == "zone" and a["side"] == "support" and a["position"] != "contains" for a in s
    ),
    "resistanceZones": lambda s: any(
        a["kind"] == "zone" and a["side"] == "resistance" and a["position"] != "contains" for a in s
    ),
    "containsZones": lambda s: any(a["kind"] == "zone" and a["position"] == "contains" for a in s),
    "pullback": lambda s: any(a["kind"] == "pullback" for a in s),
    "tc3": lambda s: any(a["kind"] == "check" and a["id"] == "TC3_long_term" for a in s),
    "tc4": lambda s: any(a["kind"] == "check" and a["id"] == "TC4_pullback" for a in s),
    "tc5": lambda s: any(a["kind"] == "check" and a["id"] == "TC5_resumption" for a in s),
    "tc4Outline": lambda s: any(a["kind"] == "check_outline" for a in s),
    "endpointMarker": lambda s: any(a["kind"] == "endpoint" for a in s),
}


def test_each_layer_hides_independently_and_restores(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    state = _show(page, stub, full_envelope(chart), "current")
    assert all(check(state["last"]["annotations"]) for check in PRESENT.values())
    assert state["last"].get("status")
    _open_menu(page)
    for key in ALL_KEYS:
        _set(page, key, False)
        last = page.evaluate("overlayState()")["last"]
        if key == "statusLabel":
            assert "status" not in last
        else:
            assert not PRESENT[key](last["annotations"]), key
        others = [k for k in PRESENT if k != key]
        assert all(PRESENT[k](last["annotations"]) for k in others), key  # nothing else hidden
        _set(page, key, True)
        last = page.evaluate("overlayState()")["last"]
        assert (
            key == "statusLabel"
            and last.get("status")
            or PRESENT.get(key, lambda s: True)(last["annotations"])
        )


def test_master_toggle_preserves_individual_choices(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    _show(page, stub, full_envelope(chart), "current")
    _open_menu(page)
    _set(page, "ema20", False)
    _set(page, "swingLows", False)
    page.evaluate("document.querySelector('[data-overlay-mode=\"clean\"]').click()")
    assert page.evaluate("overlayState().last") is None
    assert _stored(page)["ema20"] is False and _stored(page)["swingLows"] is False
    page.evaluate("document.querySelector('[data-overlay-mode=\"analysis\"]').click()")
    page.wait_for("overlayState().last?.mode === 'current'")
    last = page.evaluate("overlayState()")["last"]
    assert not PRESENT["ema20"](last["annotations"]) and not PRESENT["swingLows"](
        last["annotations"]
    )
    assert PRESENT["ema50"](last["annotations"]) and PRESENT["swingHighs"](last["annotations"])
    assert set(last["hiddenLayers"]) == {"ema20", "swingLows"}


# ================================================================ persistence
def test_preferences_survive_a_reload(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    _open_menu(page)
    _set(page, "ema50", False)
    _set(page, "tc4Outline", False)
    _reload(page, url)
    assert (
        page.evaluate("document.querySelector('[data-annotation-key=\"ema50\"]').checked") is False
    )
    assert (
        page.evaluate("document.querySelector('[data-annotation-key=\"tc4Outline\"]').checked")
        is False
    )
    assert (
        page.evaluate("document.querySelector('[data-annotation-key=\"ema20\"]').checked") is True
    )
    chart = _chart(page)
    _enable(page)
    state = _show(page, stub, full_envelope(chart), "current")
    assert not PRESENT["ema50"](state["last"]["annotations"]) and not PRESENT["tc4Outline"](
        state["last"]["annotations"]
    )


def test_preferences_survive_a_browser_restart(served, tmp_path, monkeypatch) -> None:
    url, _, _ = served
    profile = tmp_path / "profile"
    profile.mkdir()
    monkeypatch.setattr(browser_harness.tempfile, "mkdtemp", lambda prefix=None: str(profile))
    monkeypatch.setattr(browser_harness.shutil, "rmtree", lambda *a, **k: None)
    first = Browser(BROWSER)
    try:
        first.set_viewport(1600, 1000)
        _ready(first, url)
        _open_menu(first)
        _set(first, "swingHighs", False)
        assert _stored(first)["swingHighs"] is False
    finally:
        first.close()  # the browser process exits; its profile (and storage) is kept
    (profile / "DevToolsActivePort").unlink(missing_ok=True)  # removed on a clean exit
    second = Browser(BROWSER)
    try:
        second.set_viewport(1600, 1000)
        _ready(second, url)
        assert (
            second.evaluate(
                "document.querySelector('[data-annotation-key=\"swingHighs\"]').checked"
            )
            is False
        )
        assert (
            second.evaluate("document.querySelector('[data-annotation-key=\"swingLows\"]').checked")
            is True
        )
    finally:
        second.close()


def test_missing_malformed_unknown_and_new_keys(page, served) -> None:
    url, _, _ = served
    _ready(page, url)
    assert page.evaluate(f"localStorage.getItem({json.dumps(KEY)})") is None
    assert page.evaluate(
        "[...document.querySelectorAll('[data-annotation-key]')].every(b => b.checked)"
    )
    for raw in ("{not json", "[1, 2]", "42"):
        page.evaluate(f"localStorage.setItem({json.dumps(KEY)}, {json.dumps(raw)})")
        _reload(page, url)
        assert page.evaluate(
            "[...document.querySelectorAll('[data-annotation-key]')].every(b => b.checked)"
        ), raw
        assert page.evaluate("chartViewState() !== null")
    # an older record (only some keys), an unknown key and a non-boolean value
    page.evaluate(f"localStorage.setItem({json.dumps(KEY)}, JSON.stringify({OLD_RECORD}))")
    _reload(page, url)
    checked = page.evaluate(CHECKED_MAP)
    assert checked["ema20"] is False and checked["ema50"] is True
    assert all(checked[k] for k in ALL_KEYS if k != "ema20")  # new keys default on, old choice kept
    _open_menu(page)
    _set(page, "tc3", False)
    stored = _stored(page)
    assert (
        set(stored) == set(ALL_KEYS) and stored["ema20"] is False and stored["tc3"] is False
    )  # unknown dropped


def test_storage_failure_does_not_break_the_chart(page, served) -> None:
    url, _, stub = served
    script = page.send(
        "Page.addScriptToEvaluateOnNewDocument",
        source=DENY_STORAGE,
    )["identifier"]
    try:
        _ready(page, url)
        assert page.evaluate(
            "[...document.querySelectorAll('[data-annotation-key]')].every(b => b.checked)"
        )
        chart = _chart(page)
        _enable(page)
        _show(page, stub, full_envelope(chart), "current")
        _open_menu(page)
        _set(page, "ema200", False)
        last = page.evaluate("overlayState()")["last"]
        assert not PRESENT["ema200"](last["annotations"]) and PRESENT["ema50"](last["annotations"])
        assert page.evaluate("document.querySelector('#annotation-storage-note').hidden") is False
        assert page.evaluate("chartViewState() !== null")
    finally:
        page.send("Page.removeScriptToEvaluateOnNewDocument", identifier=script)


# ================================================================ regression
def test_toggling_annotations_requests_nothing_and_reuses_the_model(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    chart = _chart(page)
    _enable(page)
    _show(page, stub, full_envelope(chart), "current")
    runs = page.evaluate("state.overlay.modelRuns")
    calls = len(stub.calls)
    draws = page.evaluate("overlayState().draws")
    timers = page.evaluate("livePollingState().timers")
    _open_menu(page)
    for key in ("ema20", "swingHighs", "supportZones", "tc3", "pullback", "endpointMarker"):
        _set(page, key, False)
        _set(page, key, True)
    assert page.evaluate("state.overlay.modelRuns") == runs  # no recomputation
    assert len(stub.calls) == calls  # no analysis request
    assert page.evaluate("overlayState().draws") > draws  # redrawn
    assert page.evaluate("livePollingState().timers") == timers


def test_toggling_with_the_overlay_off_draws_nothing(page, served) -> None:
    url, _, _ = served
    _ready(page, url)
    _open_menu(page)
    for key in ("ema20", "tc4", "statusLabel"):
        _set(page, key, False)
    assert (
        page.evaluate("overlayState().draws") == 0 and page.evaluate("state.overlay.modelRuns") == 0
    )
    assert page.evaluate("document.querySelector('#analysis-overlay').hidden") is True
