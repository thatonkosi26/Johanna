"""I2: market analysis on the dashboard (headless browser, real backend, no MT5).

The app is served exactly as in ``test_browser_smoke`` (FakeMT5 behind the real live service).
``app.market_analysis`` is replaced by a stub whose responses are **real envelopes** produced by
the accepted I1 ``MarketAnalysisService`` from the accepted T6 fixtures, so every field the panel
reads has the actual backend shape. Time is controlled by shortening ``POLL`` intervals and by
moving the browser receipt time, never by waiting 30-65 s.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from datetime import timedelta

import pytest
from browser_harness import Browser, find_browser
from fake_mt5 import FakeMT5
from t6_fixtures import trend_bars
from test_analysis_engine import history
from test_browser_smoke import (
    _chart_ready,
    _hide,
    _load,
    _plot_x,
    _serve,
    _text,
    _wheel,
)
from test_market_analysis import RETRIEVED, FakeLive, FixedClock, service
from test_window_validation import candle, snapshot

BROWSER = find_browser()
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


# ---------------------------------------------------------------- real envelopes (accepted I1)
def _envelope(bars_factory, age=0.8):
    live = FakeLive(lambda s, t, c, n: snapshot(bars_factory()))
    return service(live, FixedClock(RETRIEVED + timedelta(seconds=age))).analyse("EURUSD", "H1")


def _provisional():
    bars = trend_bars()
    last = bars[-1]
    bars[-1] = candle(
        last["t"], o=last["o"], h=last["h"], low=last["l"], c=last["c"], closure="clock_only"
    )
    return bars


ENVELOPES = {
    "R7": _envelope(trend_bars),
    "R6": _envelope(lambda: history(661)),
    "R4": _envelope(lambda: history(700)),
    "R1_history": _envelope(lambda: history(660)),
    "R1_stale": _envelope(trend_bars, age=5.001),
    "R2": _envelope(_provisional),
}
assert {k: v["result"]["decision"]["rule"] for k, v in ENVELOPES.items()} == {
    "R7": "R7",
    "R6": "R6",
    "R4": "R4",
    "R1_history": "R1",
    "R1_stale": "R1",
    "R2": "R2",
}


class StubAnalysis:
    """Replaces app.market_analysis: returns a chosen envelope per request, records calls."""

    def __init__(self, envelope) -> None:
        self.envelope = envelope
        self.by_symbol: dict[str, object] = {}
        self.delay: dict[str, float] = {}
        self.calls: list[tuple[float, str, str]] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def analyse(self, symbol, timeframe):
        with self._lock:
            self.calls.append((time.monotonic(), symbol, timeframe))
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if seconds := self.delay.get(symbol):
                time.sleep(seconds)
            chosen = self.by_symbol.get(symbol, self.envelope)
            if isinstance(chosen, Exception):
                raise chosen
            out = copy.deepcopy(chosen)
            # The real backend evaluates the requested pair: echo it in the envelope and in the
            # T6 result's identity fields (test data only; schema unchanged).
            out["symbol"], out["timeframe"] = symbol, timeframe
            out["result"]["symbol"], out["result"]["timeframe"] = symbol, timeframe
            return out
        finally:
            with self._lock:
                self.active -= 1

    def since(self, moment: float) -> int:
        return sum(1 for t, _, _ in self.calls if t >= moment)


def aligned(envelope, closed_t: str):
    """The same real envelope with its window ending at the chart's latest closed candle, so the
    'newer closed candle' condition can be false (test data only; schema unchanged)."""
    out = copy.deepcopy(envelope)
    out["result"]["inputs"]["window"]["last_t"] = closed_t
    return out


BAD_TOKENS = ("undefined", "NaN", "[object Object]")
# Scheduled at (or brought forward to) the 30 s LIVE cadence.
LIVE_CADENCE = (
    "analysisInterval() === 30000 && livePollingState().analysis.delay !== null"
    " && livePollingState().analysis.delay <= 30000"
)


def _panel_text(page, selector="#analysis-full-body") -> str:
    """Visible panel text, excluding the raw JSON copy (where null is a legitimate value)."""
    return page.evaluate(
        f"(() => {{ const el = document.querySelector({json.dumps(selector)});"
        " if (!el) return ''; const copy = el.cloneNode(true);"
        " copy.querySelectorAll('.analysis-json').forEach(n => n.remove());"
        r" return copy.textContent.replace(/\s+/g, ' ').trim(); })()"
    )


def _state(page, selector="#analysis-full-body") -> str:
    return page.evaluate(
        f"document.querySelector({json.dumps(selector + ' [data-analysis-state]')})"
        "?.dataset.analysisState ?? ''"
    )


def _closed_t(page) -> str:
    return page.evaluate("state.bars.bars.filter(b => b.complete === true).at(-1).t")


def _no_bad_tokens(text: str) -> None:
    for token in BAD_TOKENS:
        assert token not in text, token
    assert " null" not in text and "null " not in text


def _ready(page, url, hash_="charts"):
    _load(page, url, hash_)
    page.wait_for("document.querySelector('#chart-status')?.textContent.includes('LIVE')")
    page.wait_for("state.bars?.bars?.length > 0")


def _show(page, stub, envelope, expected_state, selector="#analysis-full-body"):
    stub.envelope = envelope
    page.evaluate("livePolling.analysis.now()")
    page.wait_for(
        f"document.querySelector({json.dumps(selector + ' [data-analysis-state]')})"
        f"?.dataset.analysisState === {json.dumps(expected_state)}",
        timeout=8,
    )


@pytest.fixture
def served(tmp_path):
    stub = StubAnalysis(ENVELOPES["R7"])
    with _serve(tmp_path, fake=FakeMT5()) as (url, app):
        app.market_analysis = stub
        yield url, app, stub


# ---------------------------------------------------------------- A. rendering
def test_trade_candidate_is_current_and_clearly_not_an_instruction(page, served) -> None:
    url, _, stub = served
    _ready(page, url, "overview")
    _show(
        page, stub, aligned(ENVELOPES["R7"], _closed_t(page)), "current", "#analysis-compact-body"
    )
    compact = _panel_text(page, "#analysis-compact-body")
    for expected in ("Current", "EURUSD · H1", "Research candidate · R7", "trending", "bullish"):
        assert expected in compact, expected
    assert "Trend continuation · qualified (bullish)" in compact
    assert "not an instruction or recommendation to trade" in compact
    assert "Analysis & setups" in page.evaluate(_text("main"))
    _no_bad_tokens(compact)
    page.click('#primary-nav [data-page="charts"]')
    page.wait_for("document.querySelector('#analysis-full-body [data-analysis-state]')")
    full = _panel_text(page)
    for expected in (
        "Decision",
        "Rule R7",
        "Market conditions",
        "Trend strength (ADX)",
        "Volatility (ATR percentile)",
        "Momentum (RSI)",
        "Support / resistance location",
        "Swing structure",
        "TC1 · Trend regime is trending",
        "TC6 · Clearance to the opposing level",
        "V1 · Close beyond EMA50 in the pullback window",
        "V2 · Complete opposite swing structure",
        "Window validation (T4)",
        "accepted",
        "none reported",
        "Market data retrieved (MT5)",
        "Analysis covers candles up to",
        "Evaluated",
        "Response served",
        "Received by this browser",
        "latest backend evaluation window",
    ):
        assert expected in full, expected
    rows = page.evaluate(
        "[...document.querySelectorAll('#analysis-full-body [data-check]')]"
        ".map(r => [r.dataset.check, r.children[1].textContent])"
    )
    assert rows == [
        [f"TC{i}_" + n, "Pass"]
        for i, n in zip(
            range(1, 7),
            ["regime", "bias", "long_term", "pullback", "resumption", "clearance"],
            strict=True,
        )
    ]
    # The evaluation time shown is result.evaluated_at_utc, formatted, never served_at_utc.
    evaluated = page.evaluate("stampSeconds(state.analysis.result.evaluated_at_utc)")
    assert f"Evaluated{evaluated}" in full.replace(" ", "") or evaluated in full
    # No action affordances of any kind.
    buttons = page.evaluate(
        "[...document.querySelectorAll('#analysis-full-body button')].map(b => b.textContent)"
    )
    assert buttons == []
    _no_bad_tokens(full)


def test_market_condition_is_not_presented_as_an_opportunity(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    _show(page, stub, aligned(ENVELOPES["R6"], _closed_t(page)), "current")
    full = _panel_text(page)
    assert "Market condition only · R6" in full
    assert "Trend continuation · not qualified" in full
    assert "Research candidate" not in full
    assert (
        page.evaluate(
            "document.querySelector('#analysis-full-body [data-check=\"TC1_regime\"]')"
            ".children[1].textContent"
        )
        == "Fail"
    )
    _no_bad_tokens(full)


def test_r4_no_trade_shows_rule_and_the_t6_reason(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    _show(page, stub, aligned(ENVELOPES["R4"], _closed_t(page)), "current")
    full = _panel_text(page)
    assert "No trade · R4" in full and "directional evidence conflicts" in full


def test_insufficient_history_is_unavailable_with_the_actual_t4_detail(page, served) -> None:
    url, _, stub = served
    _ready(page, url, "overview")
    _show(
        page,
        stub,
        aligned(ENVELOPES["R1_history"], _closed_t(page)),
        "unavailable",
        "#analysis-compact-body",
    )
    compact = _panel_text(page, "#analysis-compact-body")
    assert "Not enough closed candles: 660 closed candle(s) available, 661 required" in compact
    page.click('#primary-nav [data-page="charts"]')
    page.wait_for(
        "document.querySelector('#analysis-full-body [data-analysis-state=\"unavailable\"]')"
    )
    full = _panel_text(page)
    assert "No trade · R1" in full
    assert "insufficient_history" in full  # the actual code, next to the label
    assert "completeness: fail" in full
    assert "Setup not evaluated" in full and "No market conditions" in full
    _no_bad_tokens(full)


def test_stale_snapshot_shows_the_actual_snapshot_rejection(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    _show(page, stub, aligned(ENVELOPES["R1_stale"], _closed_t(page)), "unavailable")
    full = _panel_text(page)
    assert "Market data snapshot refused" in full and "snapshot_refused" in full
    assert "Market data older than the 5 s limit at evaluation" in full
    assert "snapshot_too_old" in full


@pytest.mark.parametrize(("name", "state"), [("R2", "provisional"), ("R1_history", "unavailable")])
def test_r1_and_r2_are_never_current(page, served, name, state) -> None:
    url, _, stub = served
    _ready(page, url)
    # Every condition that would make R3-R7 current holds: fresh, aligned, chart LIVE.
    _show(page, stub, aligned(ENVELOPES[name], _closed_t(page)), state)
    page.evaluate("state.analysisReceivedAt = Date.now(); checkExpiry()")
    assert _state(page) == state
    full = _panel_text(page)
    if name == "R2":
        assert "Provisional" in full and "diagnostic only" in full
        assert "Candle closed by clock only (provisional)" in full and "clock_only_closure" in full
        assert "Checks not evaluated" in full
    assert "Research candidate" not in full


def test_unknown_code_unavailable_validation_missing_values_and_escaping(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    odd = aligned(ENVELOPES["R1_history"], _closed_t(page))
    reason = odd["window_validation"]["verdict"]["reasons"][0]
    reason["code"] = "brand_new_t4_code"
    reason["detail"] = "<img src=x onerror=window.__pwned=1> detail kept"
    odd["window_validation"]["reason_codes"] = ["brand_new_t4_code"]
    _show(page, stub, odd, "unavailable")
    full = _panel_text(page)
    assert "brand_new_t4_code" in full  # unknown code shown, never dropped
    assert "<img src=x onerror=window.__pwned=1> detail kept" in full  # as text
    assert page.evaluate("document.querySelector('#analysis-full-body img') === null")
    assert page.evaluate("window.__pwned === undefined")
    missing = aligned(ENVELOPES["R6"], _closed_t(page))
    missing["window_validation"] = {"available": False, "reason": "T4 did not run: example reason"}
    trend = missing["result"]["market_condition"]["dimensions"]["trend_strength"]
    trend["supporting"][0]["observed"] = None  # a missing technical value
    _show(page, stub, missing, "current")
    full = _panel_text(page)
    assert "T4 validation did not run." in full and "T4 did not run: example reason" in full
    assert "adx: —" in full
    _no_bad_tokens(full)


def test_joined_result_keeps_the_original_evaluation_time(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    joined = aligned(ENVELOPES["R7"], _closed_t(page))
    joined["service"]["joined_in_flight"] = True
    joined["served_at_utc"] = "2030-01-01T00:00:00Z"
    _show(page, stub, joined, "current")
    full = _panel_text(page)
    assert "shared computation (original evaluation time)" in full
    evaluated = page.evaluate("stampSeconds(state.analysis.result.evaluated_at_utc)")
    served_at = page.evaluate("stampSeconds('2030-01-01T00:00:00Z')")
    row = page.evaluate(
        "[...document.querySelectorAll('#analysis-full-body .analysis-kv')]"
        ".find(r => r.children[0].textContent === 'Evaluated').children[1].textContent"
    )
    assert row.startswith(evaluated) and served_at not in row


# ---------------------------------------------------------------- B. status handling
def test_unsupported_timeframe_shows_unsupported_and_sends_nothing(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    page.select("#timeframe-select", "M15")
    page.wait_for(
        "document.querySelector('#analysis-full-body [data-analysis-state]')"
        "?.dataset.analysisState === 'unsupported'",
        timeout=8,
    )
    page.evaluate("POLL.analysisMs = 200; POLL.quietAnalysisMs = 200; livePolling.analysis.now()")
    start = time.monotonic()
    time.sleep(1.5)
    assert stub.since(start) == 0
    assert "Market analysis covers H1, H4, D1" in _panel_text(page)
    assert page.evaluate("livePollingState().analysis.scheduled") is False


def test_loading_then_result(page, served) -> None:
    url, _, stub = served
    stub.delay["EURUSD"] = 2.0
    _load(page, url, "charts")
    page.wait_for(
        "document.querySelector('#analysis-full-body [data-analysis-state=\"loading\"]') !== null"
    )
    page.wait_for(
        "document.querySelector('#analysis-full-body [data-analysis-state]')"
        "?.dataset.analysisState !== 'loading'",
        timeout=8,
    )


def test_outdated_after_the_65_second_display_expiry(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    _show(page, stub, aligned(ENVELOPES["R7"], _closed_t(page)), "current")
    page.evaluate("stopLivePolling(); state.analysisReceivedAt = Date.now() - 64000; checkExpiry()")
    assert _state(page) == "current"
    page.evaluate("state.analysisReceivedAt = Date.now() - 66000; checkExpiry()")
    assert _state(page) == "outdated"
    assert "no new evaluation for over 65 s" in _panel_text(page)
    assert page.evaluate("ANALYSIS_EXPIRE_MS") == 65000


def test_outdated_when_a_newer_candle_has_closed(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    _show(page, stub, ENVELOPES["R7"], "outdated")  # its window ends before the chart's candles
    assert "a newer candle has closed since this evaluation" in _panel_text(page)


def test_outdated_when_the_chart_is_not_live(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    _show(page, stub, aligned(ENVELOPES["R7"], _closed_t(page)), "current")
    page.evaluate(
        "stopLivePolling();"
        " state.barsError = { key: chartKey(), reason: 'synthetic', message: 'x' };"
        " checkExpiry()"
    )
    assert _state(page) == "outdated"
    assert "the chart is not LIVE" in _panel_text(page)


def test_api_error_is_shown_and_other_pollers_continue(page, served) -> None:
    url, app, stub = served
    stub.envelope = RuntimeError("synthetic analysis failure")
    _load(page, url, "charts")
    page.wait_for(
        "document.querySelector('#analysis-full-body [data-analysis-state=\"error\"]') !== null",
        timeout=8,
    )
    assert "internal_error" in _panel_text(page)
    quotes = page.evaluate("livePollingState().quotes.requests")
    bars = page.evaluate("livePollingState().bars.requests")
    page.wait_for(
        f"livePollingState().quotes.requests > {quotes}"
        f" && livePollingState().bars.requests > {bars}",
        timeout=12,
    )
    assert page.evaluate("livePollingState().analysis.failures") >= 1


# ---------------------------------------------------------------- C. polling
def test_interval_is_30_s_when_live_and_60_s_otherwise(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    assert page.evaluate("[POLL.analysisMs, POLL.quietAnalysisMs]") == [30000, 60000]
    page.wait_for(LIVE_CADENCE, timeout=8)
    page.evaluate(
        "livePolling.bars.stop(); livePolling.quotes.stop();"
        " state.barsError = { key: chartKey(), reason: 'synthetic', message: 'x' };"
        " livePolling.analysis.now()"
    )
    page.wait_for("livePollingState().analysis.delay === 60000", timeout=8)


def test_symbol_and_timeframe_changes_refresh_immediately(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    page.wait_for(LIVE_CADENCE, timeout=8)
    start = time.monotonic()
    page.select("#pair-select", "USDJPY")
    deadline = time.monotonic() + 5
    while not any(c[1] == "USDJPY" and c[0] >= start for c in stub.calls):
        assert time.monotonic() < deadline, stub.calls
        time.sleep(0.05)
    # Wait until the follow-up poll is scheduled at a regular cadence. If the analysis
    # answer beats the new pair's candles, it is scheduled at 60 s and then retime()
    # pulls it in to the remainder of 30 s once the chart turns LIVE (e.g. 28483 ms),
    # so the delay is any value in (0, 60000], not exactly 30000 or 60000.
    page.wait_for(
        "(() => { const a = livePollingState().analysis;"
        " return !a.inFlight && a.delay !== null && a.delay > 0 && a.delay <= 60000; })()",
        timeout=8,
    )
    start = time.monotonic()
    page.select("#timeframe-select", "H4")
    deadline = time.monotonic() + 5
    while not any(c[2] == "H4" and c[0] >= start for c in stub.calls):
        assert time.monotonic() < deadline, stub.calls
        time.sleep(0.05)


def test_a_newly_closed_candle_triggers_an_early_refresh(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    page.wait_for(LIVE_CADENCE, timeout=8)
    start = time.monotonic()
    page.evaluate(
        "state.analysisCandle = { key: chartKey(), t: '2000-01-01T00:00:00Z' };"
        " livePolling.bars.now()"
    )
    deadline = time.monotonic() + 5
    while stub.since(start) == 0:
        assert time.monotonic() < deadline, "no early analysis refresh"
        time.sleep(0.05)


def test_hidden_tab_suspends_and_visible_tab_refreshes(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    page.evaluate("POLL.analysisMs = 300; POLL.quietAnalysisMs = 300; livePolling.analysis.now()")
    _hide(page, True)
    hidden_at = time.monotonic() + 0.5  # a running request may still finish
    time.sleep(2.0)
    assert stub.since(hidden_at) == 0
    assert page.evaluate("livePollingState().analysis.scheduled") is False
    shown_at = time.monotonic()
    _hide(page, False)
    deadline = time.monotonic() + 3
    while stub.since(shown_at) == 0:
        assert time.monotonic() < deadline, "no refresh after the tab became visible"
        time.sleep(0.05)


def test_no_analysis_polling_on_pages_without_the_panel(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    page.evaluate("POLL.analysisMs = 200; POLL.quietAnalysisMs = 200")
    page.click('#primary-nav [data-page="markets"]')
    page.wait_for("state.page === 'markets'")
    left_at = time.monotonic() + 0.5
    time.sleep(1.5)
    assert stub.since(left_at) == 0
    assert page.evaluate("livePollingState().analysis.scheduled") is False
    back = time.monotonic()
    page.click('#primary-nav [data-page="overview"]')
    deadline = time.monotonic() + 3
    while stub.since(back) == 0:
        assert time.monotonic() < deadline, "no analysis after returning to the panel"
        time.sleep(0.05)


def test_analysis_requests_never_overlap(page, served) -> None:
    url, _, stub = served
    stub.delay["EURUSD"] = 1.0
    _ready(page, url)
    page.evaluate(
        "POLL.analysisMs = 50; POLL.quietAnalysisMs = 50;"
        " for (let i = 0; i < 10; i++) livePolling.analysis.now()"
    )
    time.sleep(3.5)
    assert stub.max_active == 1
    assert len(stub.calls) >= 2


def test_late_answer_for_an_old_selection_never_overwrites(page, served) -> None:
    url, _, stub = served
    _ready(page, url)
    page.wait_for(LIVE_CADENCE, timeout=8)
    stub.delay["EURUSD"] = 2.5
    page.evaluate("livePolling.analysis.now()")
    page.wait_for("livePolling.analysis.inFlight === true")
    page.select("#pair-select", "USDJPY")
    page.wait_for("state.analysisKey === 'USDJPY|H1'", timeout=8)
    time.sleep(3.0)  # the slow EURUSD answer arrives in the meantime
    assert page.evaluate("state.analysisKey") == "USDJPY|H1"
    assert "USDJPY · H1" in _panel_text(page)


# ---------------------------------------------------------------- D. chart isolation
def test_analysis_updates_never_touch_the_chart(page, served) -> None:
    url, _, stub = served
    box = _chart_ready(page, url)
    _wheel(page, _plot_x(box, 0.3), box["y"] + box["h"] / 2, -120, times=3)  # into history
    page.wait_for("chartViewState().follow === false")
    page.evaluate(
        "stopLivePolling();"  # no bars/quotes refresh can repaint during the check
        " document.querySelector('#price-chart').__marker = 'kept';"
        " window.__draws = 0; const realDraw = drawChart;"
        " drawChart = function () { window.__draws += 1; return realDraw.apply(this, arguments); };"
    )
    before = page.evaluate(
        "JSON.stringify([chartViewState(), state.view, state.chartDraws, state.bars.bars.length,"
        " document.querySelector('#chart-status').textContent])"
    )
    closed = _closed_t(page)
    for name in ("R7", "R1_history", "R2", "R6"):
        stub.envelope = aligned(ENVELOPES[name], closed)
        page.evaluate("refreshAnalysis(livePolling.analysis)")
        page.evaluate("checkExpiry()")
    after = page.evaluate(
        "JSON.stringify([chartViewState(), state.view, state.chartDraws, state.bars.bars.length,"
        " document.querySelector('#chart-status').textContent])"
    )
    assert after == before
    assert page.evaluate("window.__draws") == 0
    assert page.evaluate("document.querySelector('#price-chart').__marker") == "kept"
    assert page.evaluate("state.analysis.result.decision.rule") == "R6"
    assert "You are viewing older candles on the chart" in _panel_text(page)
