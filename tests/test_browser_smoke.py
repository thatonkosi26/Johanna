"""Headless-browser smoke tests of the live market workspace against the real backend.

Each scenario serves ``JohannaApplication`` + ``BackendAPI`` in-process over a temporary data
directory. Live market data comes from ``FakeMT5`` behind the real ``MetaTrader5Adapter`` and
``LiveMarketService``; nothing contacts a real MT5 terminal (see also ``conftest.py``),
Forex Factory or Twelve Data. Skipped when no Chromium-family browser is installed; deselect
with ``-m "not browser"``.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from wsgiref.simple_server import WSGIRequestHandler

import pytest
from browser_harness import Browser, find_browser
from fake_mt5 import READ_ONLY_CALLS, FakeMT5

from johanna.__main__ import ExclusiveWSGIServer
from johanna.adapters.economic_calendar import (
    CalendarSnapshot,
    Freshness,
    WeeklySnapshotCache,
    normalize_event,
)
from johanna.adapters.mt5 import MetaTrader5Adapter
from johanna.api import BackendAPI
from johanna.application import JohannaApplication
from johanna.config import AppConfig, StorageConfig
from johanna.data_services import week_start_utc
from johanna.live_market import LiveMarketService
from johanna.market_data import ParquetBarStore

BROWSER = find_browser()
pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(BROWSER is None, reason="no Chromium-family browser installed"),
]
NAV = {
    "overview": "Overview",
    "charts": "Charts",
    "markets": "Markets",
    "calendar": "Economic Calendar",
    "news": "News",
    "signals": "Trading Signals",
    "rules": "Trading Rules",
    "risk": "Risk Management",
    "positions": "Positions",
    "history": "Trade History",
    "analytics": "Analytics",
    "settings": "Settings",
}


def _seed_archive(data_dir) -> None:
    """Synthetic legacy-style archive bars (MT5 source, 'UTC' label); not observations."""
    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    bars = [
        {
            "symbol": "EURUSD",
            "timeframe": "H1",
            "timestamp": now - timedelta(hours=40 - index),
            "open": 1.1,
            "high": 1.2,
            "low": 1.0,
            "close": round(1.1 + index / 1000, 5),
            "tick_volume": 1,
            "real_volume": None,
            "source": "MT5",
            "source_timezone": "UTC",
            "source_timestamp_convention": "bar_open",
        }
        for index in range(40)
    ]
    ParquetBarStore(data_dir / "collectors" / "mt5").ingest(bars, source="MT5")


def _seed_calendar(data_dir, retrieved_at: datetime) -> None:
    event = normalize_event(
        {
            "event_name": "Synthetic Smoke Event",
            "currency": "USD",
            "scheduled_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
            "impact": "high",
        },
        source="synthetic-smoke",
        retrieved_at=retrieved_at,
    )
    WeeklySnapshotCache(data_dir / "collectors" / "calendar" / "snapshots").write(
        CalendarSnapshot(
            week_start=week_start_utc(datetime.now(UTC)),
            retrieved_at_utc=retrieved_at,
            source="synthetic-smoke",
            events=(event,),
            freshness=Freshness.FRESH,
        )
    )


def _running_monitor(stop_event: threading.Event) -> None:
    stop_event.wait()


def _failing_monitor(stop_event: threading.Event) -> None:
    raise ConnectionError("synthetic: collector failed")


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format, *args) -> None:  # noqa: A002
        return


@contextlib.contextmanager
def _serve(tmp_path, *, fake: FakeMT5 | None = None, monitor=None, clock=None):
    """Serve the real app; ``fake=None`` means the MT5 terminal is not running."""
    config = replace(AppConfig.load(environ={}), storage=StorageConfig(tmp_path))
    live = LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake),
        terminal_running=lambda: fake is not None,
        reconnect_interval=0,
        clock=clock,
    )
    app = JohannaApplication(config, market_monitor_task=monitor, live_market=live)
    live._cached_bars = app._archived_bars
    server = ExclusiveWSGIServer(("127.0.0.1", 0), _QuietHandler)
    server.set_app(BackendAPI(app))
    app.start()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/", app
    finally:
        server.shutdown()
        server.server_close()
        app.stop()


@pytest.fixture(scope="module")
def browser():
    instance = Browser(BROWSER)
    yield instance
    instance.close()


@pytest.fixture
def page(browser):
    browser.set_viewport(1600, 1000)
    return browser


def _top_js(label: str) -> str:
    """Top-bar item text, whitespace-normalized (items are flex boxes)."""
    return (
        "[...document.querySelectorAll('#top-status .status-item')]"
        r".map(e => e.textContent.replace(/\s+/g, ' ').trim())"
        f".find(t => t.startsWith('{label}')) ?? ''"
    )


def _top(page, label: str) -> str:
    return page.evaluate(_top_js(label))


def _text(selector: str) -> str:
    return (
        f"(document.querySelector({json.dumps(selector)})?.textContent ?? '')"
        r".replace(/\s+/g, ' ').trim()"
    )


def _eventually(predicate, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("backend state did not converge")
        time.sleep(0.05)


def _load(page, url: str, hash_: str = "overview") -> None:
    page.open(f"{url}#{hash_}")
    page.wait_for("document.querySelector('.page-heading h1')")


def _watch_row(page, symbol: str) -> str:
    return page.evaluate(
        "([...document.querySelectorAll('#watchlist-body tr')]"
        f".find(r => r.querySelector('[data-select-symbol=\"{symbol}\"]'))?.textContent ?? '')"
        r".replace(/\s+/g, ' ').trim()"
    )


def _canvas_colours(page) -> dict:
    return page.evaluate(
        "(() => { const c = document.querySelector('#price-chart');"
        " const d = c.getContext('2d').getImageData(0, 0, c.width, c.height).data;"
        " let green = 0, red = 0;"
        " for (let i = 0; i < d.length; i += 4) {"
        "   if (d[i + 3] < 200) continue;"
        "   if (d[i + 1] > 150 && d[i] < 120) green++;"
        "   if (d[i] > 180 && d[i + 1] < 140) red++; }"
        " return { green, red }; })()"
    )


def test_overview_shows_live_mt5_quotes_and_candles(page, tmp_path) -> None:
    fake = FakeMT5()
    _seed_calendar(tmp_path, retrieved_at=datetime.now(UTC) - timedelta(minutes=10))
    with _serve(tmp_path, fake=fake) as (url, app):
        _load(page, url)
        page.wait_for(f"({_top_js('Market data')}).startsWith('Market data LIVE')")
        assert "Exness-MT5Trial9" in _top(page, "Market data")
        assert _top(page, "Application") == "Application Online"
        strip = page.evaluate(_text(".quote-strip"))
        assert "1.08512" in strip and "1.08519" in strip and "0.7 pips" in strip
        assert "LIVE" in strip
        page.wait_for("document.querySelector('#chart-status')?.textContent.includes('300 bars')")
        assert "LIVE" in page.evaluate(_text("#chart-status"))
        assert page.evaluate("document.querySelector('#chart-empty').hidden") is True
        colours = _canvas_colours(page)
        assert colours["green"] > 0 and colours["red"] > 0, colours  # real candlesticks
        assert "LIVE" in _watch_row(page, "EURUSD")
        assert "UNAVAILABLE" in _watch_row(page, "GBPUSD")  # not offered by the fake broker
        info = page.evaluate(_text("#market-info"))
        assert "Exness-MT5Trial9" in info and "Verified against a live tick" in info
        overview = page.evaluate(_text("main"))
        assert "Synthetic Smoke Event" in overview
        assert "Analysis & setups" in overview
        for placeholder in (
            "trading signals",
            "trading rules",
            "risk management",
            "broker execution",
        ):
            assert placeholder not in overview.lower()
        assert set(fake.calls) <= READ_ONLY_CALLS


def test_live_quotes_refresh_on_demand(page, tmp_path) -> None:
    fake = FakeMT5()
    with _serve(tmp_path, fake=fake) as (url, _):
        _load(page, url)
        page.wait_for(f"{_text('.quote-strip')}.includes('1.08512')")
        fake.symbols["EURUSD"]["bid"] = 1.09001
        page.wait_for(f"{_text('.quote-strip')}.includes('1.09001')", timeout=8)


def test_selecting_pair_and_timeframe_fetches_new_bars(page, tmp_path) -> None:
    fake = FakeMT5()
    with _serve(tmp_path, fake=fake) as (url, app):
        _load(page, url)
        page.wait_for("document.querySelector('#chart-status')?.textContent.includes('bars')")
        page.select("#pair-select", "USDJPY")
        page.wait_for("document.querySelector('.chart-symbol')?.textContent === 'USDJPY'")
        page.select("#timeframe-select", "M15")
        page.wait_for("document.querySelector('.chart-timeframe')?.textContent === 'M15'")
        page.wait_for("document.querySelector('#chart-status')?.textContent.includes('LIVE')")
        page.wait_for(f"{_text('#chart-foot')}.includes('M15')")
        _eventually(lambda: app.store.get_preferences()["selected_currency_pair"] == "USDJPY")
        assert fake.calls.count("copy_rates_from_pos") >= 2


def test_navigation_reaches_every_page(page, tmp_path) -> None:
    with _serve(tmp_path) as (url, _):
        _load(page, url)
        for page_id, title in NAV.items():
            selector = f'#primary-nav [data-page="{page_id}"]'
            page.click(selector)
            page.wait_for(f"document.querySelector('.page-heading h1')?.innerText === {title!r}")
            assert page.evaluate("location.hash") == f"#{page_id}"
            assert (
                page.evaluate(
                    f"document.querySelector({json.dumps(selector)}).getAttribute('aria-current')"
                )
                == "page"
            )
            assert "Page not found" not in page.text("main")
        page.click(".settings-shortcut")
        page.wait_for("location.hash === '#settings'")


def test_settings_persist_through_backend_and_reload(page, tmp_path) -> None:
    with _serve(tmp_path) as (url, app):
        _load(page, url, "settings")
        page.select("#theme-select", "light")
        page.wait_for("document.documentElement.dataset.theme === 'light'")
        page.select("#timezone-select", "Europe/London")
        page.wait_for("document.querySelector('#timezone-select')?.value === 'Europe/London'")
        page.select("#settings-timeframe", "M5")
        _eventually(
            lambda: (
                app.store.get_preferences()["user_timezone"] == "Europe/London"
                and app.store.get_preferences()["selected_chart_timeframe"] == "M5"
                and app.store.get_preferences()["theme"] == "light"
            )
        )
        _load(page, url, "settings")  # full reload: state comes back from the backend
        page.wait_for("document.querySelector('#theme-select')?.value === 'light'")
        assert page.evaluate("document.querySelector('#timezone-select').value") == "Europe/London"
        page.select("#theme-select", "dark")
        page.wait_for("document.documentElement.dataset.theme === 'dark'")


def test_settings_show_real_services_and_optional_collector(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5(), monitor=_running_monitor) as (url, app):
        _load(page, url, "settings")
        page.wait_for(
            f"{_text('.settings-grid')}.includes('Background collector (optional) · Running')"
        )
        health = page.evaluate(_text(".health-list"))
        assert "live market" in health and "persistence" in health
        assert "trading signals" not in health  # placeholders are summarised, not listed
        assert "Planned, not yet implemented" in page.evaluate(_text(".settings-grid"))
        page.click("#monitor-toggle")
        page.wait_for(
            f"{_text('.settings-grid')}.includes('Background collector (optional) · Paused')"
        )
        _eventually(lambda: app.status()["market_monitoring_state"] == "paused")


def test_failed_collector_is_visible(page, tmp_path) -> None:
    with _serve(tmp_path, monitor=_failing_monitor) as (url, _):
        _load(page, url, "settings")
        page.wait_for(f"({_top_js('Application')}) === 'Application Degraded'")
        assert "Failed" in page.evaluate(_text(".health-list"))


def test_closed_terminal_is_unavailable_and_archive_is_marked_cached(page, tmp_path) -> None:
    _seed_archive(tmp_path)
    with _serve(tmp_path, fake=None) as (url, app):
        app.save_preferences({"selected_chart_timeframe": "H1"})
        _load(page, url)
        page.wait_for(f"({_top_js('Market data')}).startsWith('Market data UNAVAILABLE')")
        assert "MT5 terminal is not running" in page.evaluate(_text(".quote-strip"))
        assert "UNAVAILABLE" in _watch_row(page, "EURUSD")
        assert "1.1" not in page.evaluate(_text("#watchlist-body"))  # no prices invented
        page.wait_for("document.querySelector('#chart-status')?.textContent.includes('CACHED')")
        notice = page.evaluate(_text("#chart-notice"))
        assert "not live" in notice and "MT5 server time, not UTC" in notice
        assert "Parquet archive" in page.evaluate(_text("#chart-foot"))
        assert "all candles complete" in page.evaluate(_text("#chart-foot"))


def test_unavailable_without_archive_shows_empty_chart(page, tmp_path) -> None:
    with _serve(tmp_path, fake=None) as (url, _):
        _load(page, url, "charts")
        page.wait_for(f"{_text('#chart-empty')}.includes('No chart data')")
        assert "terminal is not running" in page.evaluate(_text("#chart-empty"))


def test_market_closed_quotes_are_stale(page, tmp_path) -> None:
    fake = FakeMT5(tick_age_seconds=3 * 3600)
    with _serve(tmp_path, fake=fake) as (url, _):
        _load(page, url, "markets")
        page.wait_for(f"{_text('#watchlist-body')}.includes('STALE')")
        assert "3h" in _watch_row(page, "EURUSD")
        assert "Not verifiable now" in page.evaluate(_text("#market-info"))
        _load(page, url, "charts")  # unverified time base is flagged on the chart
        page.wait_for(f"{_text('#chart-notice')}.includes('not yet verified against a live tick')")


def test_server_clock_mismatch_is_shown_not_hidden(page, tmp_path) -> None:
    fake = FakeMT5(server_offset_seconds=7200)
    with _serve(tmp_path, fake=fake) as (url, _):
        _load(page, url)
        page.wait_for(f"{_text('#market-info')}.includes('MISMATCH')")
        assert "server clock does not match UTC" in page.evaluate(_text(".quote-strip"))


def test_markets_and_calendar_pages(page, tmp_path) -> None:
    _seed_calendar(tmp_path, retrieved_at=datetime.now(UTC) - timedelta(minutes=10))
    with _serve(tmp_path, fake=FakeMT5()) as (url, _):
        _load(page, url, "markets")
        page.wait_for(f"{_text('#watchlist-body')}.includes('LIVE')")
        header = page.evaluate(_text(".live-watchlist thead"))
        for column in ("Bid", "Ask", "Spread", "Updated", "Age", "Status"):
            assert column in header
        _load(page, url, "calendar")
        page.wait_for("document.querySelector('.event-row')")
        assert "Synthetic Smoke Event" in page.text("main")
        assert "Fresh" in page.text(".status-strip")


@pytest.mark.parametrize("page_id", ["overview", "markets", "calendar", "settings", "charts"])
def test_phone_width_has_no_horizontal_page_overflow(page, tmp_path, page_id) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, _):
        page.set_viewport(400, 900, mobile=True)
        _load(page, url, page_id)
        time.sleep(0.5)  # let live data render
        assert page.evaluate("innerWidth") == 400
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 0, f"{page_id} overflows by {overflow}px at 400px"


def test_mobile_menu_and_desktop_sidebar(page, tmp_path) -> None:
    with _serve(tmp_path) as (url, _):
        page.set_viewport(400, 900, mobile=True)
        _load(page, url)
        assert (
            page.evaluate("getComputedStyle(document.querySelector('#mobile-menu')).display")
            != "none"
        )
        page.click("#mobile-menu")
        page.wait_for("document.querySelector('#sidebar').classList.contains('open')")
        page.click('#primary-nav [data-page="markets"]')
        page.wait_for("location.hash === '#markets'")
        assert not page.evaluate("document.querySelector('#sidebar').classList.contains('open')")
        page.set_viewport(1600, 1000)
        _load(page, url)
        assert (
            page.evaluate("getComputedStyle(document.querySelector('#mobile-menu')).display")
            == "none"
        )


# ---------------------------------------------------------------- audit remediation (H1/M3/M4/M7)
_NO_LIVE_BADGE = (
    "![...document.querySelectorAll('.status-badge')].some(e => e.textContent.startsWith('LIVE'))"
)


def _fail_quotes(app, monkeypatch) -> None:
    def broken(symbols):
        raise RuntimeError("synthetic quotes failure")

    monkeypatch.setattr(app.live_market, "quotes", broken)


def test_failed_quote_refresh_downgrades_live_to_cached_with_reason(
    page, tmp_path, monkeypatch
) -> None:
    fake = FakeMT5()
    with _serve(tmp_path, fake=fake) as (url, app):
        _load(page, url)
        page.wait_for(f"({_top_js('Market data')}).startsWith('Market data LIVE')")
        page.wait_for(f"{_text('#market-info')}.includes('Verified against a live tick')")
        page.evaluate("POLL.maxBackoffMs = 3000")  # failures back off; keep the test short
        _fail_quotes(app, monkeypatch)
        page.wait_for(f"({_top_js('Market data')}).startsWith('Market data CACHED')", timeout=8)
        assert "Backend error" in _top(page, "Market data")
        row = _watch_row(page, "EURUSD")
        assert "CACHED" in row and "1.08512" in row  # earlier price kept, labelled, aged
        strip = page.evaluate(_text(".quote-strip"))
        assert "CACHED" in strip and "Backend error" in strip and "1.08512" in strip
        info = page.evaluate(_text("#market-info"))
        assert "Not verified now" in info and "last verified" in info
        assert "Verified against a live tick" not in info
        assert page.evaluate(_NO_LIVE_BADGE)
        monkeypatch.undo()  # backend recovers: a fresh, checked response restores LIVE
        page.wait_for(f"({_top_js('Market data')}).startsWith('Market data LIVE')", timeout=8)
        page.wait_for(f"{_text('#market-info')}.includes('Verified against a live tick')")


def test_unanswered_quote_refresh_expires_live(page, tmp_path, monkeypatch) -> None:
    fake = FakeMT5()
    release = threading.Event()
    with _serve(tmp_path, fake=fake) as (url, app):
        _load(page, url)
        page.wait_for(f"({_top_js('Market data')}).startsWith('Market data LIVE')")
        original = app.live_market.quotes

        def hung(symbols):
            release.wait(30)
            return original(symbols)

        monkeypatch.setattr(app.live_market, "quotes", hung)
        page.wait_for("livePolling.quotes.inFlight === true", timeout=8)
        # The browser's clock jumps past the expiry while the request is still unanswered.
        page.evaluate("(() => { const real = Date.now; Date.now = () => real() + 11000; })()")
        try:
            page.wait_for(f"({_top_js('Market data')}).startsWith('Market data CACHED')", timeout=5)
            assert "No recent update" in _top(page, "Market data")
            assert "CACHED" in _watch_row(page, "EURUSD")
            assert page.evaluate(_NO_LIVE_BADGE)
        finally:
            release.set()


def test_quotes_failing_before_any_success_show_unavailable_without_prices(
    page, tmp_path, monkeypatch
) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, app):
        _fail_quotes(app, monkeypatch)
        _load(page, url, "markets")
        page.wait_for(f"({_top_js('Market data')}).startsWith('Market data UNAVAILABLE')")
        row = _watch_row(page, "EURUSD")
        assert "UNAVAILABLE" in row and "1.08" not in row
        assert (
            "Backend error"
            in page.evaluate("document.querySelector('#watchlist-body td[title]')?.title ?? ''")
            or "UNAVAILABLE" in row
        )


def test_failed_chart_refresh_downgrades_live_bars_to_cached(page, tmp_path, monkeypatch) -> None:
    fake = FakeMT5()
    with _serve(tmp_path, fake=fake) as (url, app):
        _load(page, url, "charts")
        page.wait_for("document.querySelector('#chart-status')?.textContent.includes('LIVE')")

        def broken(*args, **kwargs):
            raise RuntimeError("synthetic bars failure")

        monkeypatch.setattr(app.live_market, "bars", broken)
        page.evaluate("loadBars()")
        page.wait_for("document.querySelector('#chart-status')?.textContent.includes('CACHED')")
        assert "LIVE" not in page.evaluate(_text("#chart-status"))
        notice = page.evaluate(_text("#chart-notice"))
        assert "bars fetched" in notice and "not live" in notice and "Backend error" in notice
        assert page.evaluate("document.querySelector('#chart-empty').hidden") is True  # kept
        # A different pair with no earlier bars: honest UNAVAILABLE, nothing drawn.
        page.select("#pair-select", "USDJPY")
        page.wait_for(
            "document.querySelector('#chart-status')?.textContent.includes('UNAVAILABLE')"
        )
        assert "Chart unavailable" in page.evaluate(_text("#chart-empty"))
        monkeypatch.undo()
        page.evaluate("loadBars()")
        page.wait_for("document.querySelector('#chart-status')?.textContent.includes('LIVE')")


def test_unanswered_chart_refresh_expires_live(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, _):
        _load(page, url, "charts")
        page.wait_for("document.querySelector('#chart-status')?.textContent.includes('LIVE')")
        # Stop polling first so no fresh answer can land between the clock jump and the check.
        result = page.evaluate(
            "(() => { stopLivePolling(); const real = Date.now; Date.now = () => real() + 66000;"
            " renderChartArea(); return [document.querySelector('#chart-status').textContent,"
            " document.querySelector('#chart-notice').textContent]; })()"
        )
        assert "CACHED" in result[0]
        assert "No recent update" in result[1]


def test_forming_candle_is_flagged_and_drawn_differently(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, _):
        _load(page, url, "charts")
        page.wait_for("document.querySelector('#chart-status')?.textContent.includes('LIVE')")
        flags = page.evaluate("state.bars.bars.map(b => b.complete)")
        assert flags.count(False) == 1 and all(flags[:-1])
        assert page.evaluate("typeof state.bars.bars.at(-1).close_t") == "string"
        assert "1 forming" in page.evaluate(_text("#chart-status"))
        assert "forming candle (hollow) opened" in page.evaluate(_text("#chart-foot"))


def test_clock_check_reflects_a_lost_connection(page, tmp_path) -> None:
    fake = FakeMT5()
    with _serve(tmp_path, fake=fake) as (url, _):
        _load(page, url, "markets")
        page.wait_for(f"{_text('#market-info')}.includes('Verified against a live tick')")
        fake.running = False  # terminal closes
        page.wait_for(f"{_text('#market-info')}.includes('Not verified now')", timeout=8)
        info = page.evaluate(_text("#market-info"))
        assert "last verified" in info and "Verified against a live tick" not in info


def test_watchlist_limit_and_invalid_additions_are_explained(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, app):
        _load(page, url, "markets")
        page.wait_for(f"{_text('#watchlist-body')}.includes('LIVE')")
        page.evaluate("document.querySelector('#watchlist-input').value = 'BAD SYMBOL'")
        page.click("#add-pair")
        page.wait_for(f"{_text('#save-message')}.includes('invalid instrument symbols')")
        symbols = [f"PAIR{index:03d}" for index in range(200)]
        app.save_preferences({"currency_pair_watchlist": symbols})
        page.evaluate("loadDashboard().then(loadQuotes)")  # pick up the backend-side change
        page.wait_for(f"{_text('.watchlist-add')}.includes('200 / 200')")
        page.wait_for("document.querySelectorAll('#watchlist-body tr').length === 200")
        page.wait_for(f"({_top_js('Market data')}).startsWith('Market data UNAVAILABLE')")
        page.evaluate("document.querySelector('#watchlist-input').value = 'EURUSD'")
        page.click("#add-pair")
        page.wait_for(f"{_text('#save-message')}.includes('limited to 200 pairs')")
        assert app.store.get_watchlist() == symbols  # nothing truncated or lost


# ---------------------------------------------------------------- automatic live refresh
class _Calls:
    """Wraps a LiveMarketService method: counts calls, start times and concurrency."""

    def __init__(self, target, name: str, delay=None) -> None:
        self.original = getattr(target, name)
        self.starts: list[float] = []
        self.active = 0
        self.max_active = 0
        self.delay = delay  # callable(args) -> seconds to wait before answering
        self._lock = threading.Lock()
        setattr(target, name, self)

    def __call__(self, *args, **kwargs):
        with self._lock:
            self.starts.append(time.monotonic())
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            if self.delay and (seconds := self.delay(args)):
                time.sleep(seconds)
            return self.original(*args, **kwargs)
        finally:
            with self._lock:
                self.active -= 1

    def since(self, moment: float) -> int:
        return sum(1 for start in self.starts if start >= moment)


def _hide(page, hidden: bool) -> None:
    page.evaluate(
        "(() => { Object.defineProperty(document, 'hidden', { configurable: true,"
        f" get: () => {'true' if hidden else 'false'} }});"
        " document.dispatchEvent(new Event('visibilitychange')); })()"
    )


def _listener_count(page, target: str, kind: str) -> int:
    handle = page.send("Runtime.evaluate", expression=target)["result"]["objectId"]
    listeners = page.send("DOMDebugger.getEventListeners", objectId=handle)["listeners"]
    return sum(1 for listener in listeners if listener["type"] == kind)


def test_quotes_refresh_automatically_about_every_three_seconds(page, tmp_path) -> None:
    fake = FakeMT5()
    with _serve(tmp_path, fake=fake) as (url, app):
        calls = _Calls(app.live_market, "quotes")
        _load(page, url)
        page.wait_for(f"({_top_js('Market data')}).startsWith('Market data LIVE')")
        start = time.monotonic()
        fake.symbols["EURUSD"]["bid"], fake.symbols["EURUSD"]["ask"] = 1.09001, 1.09011
        page.wait_for(f"{_text('.quote-strip')}.includes('1.09001')", timeout=6)
        assert "1.0 pips" in page.evaluate(_text(".quote-strip"))  # spread follows
        assert "LIVE" in _watch_row(page, "EURUSD")
        time.sleep(7)
        recent = [t for t in calls.starts if t >= start]
        gaps = [b - a for a, b in zip(recent, recent[1:], strict=False)]
        assert 2 <= len(recent) <= 5, recent
        assert all(gap >= 2.5 for gap in gaps), gaps  # ~3 s, never faster


def test_quote_requests_never_overlap_or_pile_up(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, app):
        calls = _Calls(app.live_market, "quotes", delay=lambda args: 4.0)  # slower than 3 s
        _load(page, url)
        time.sleep(10)
        assert calls.max_active == 1
        assert 1 <= len(calls.starts) <= 3, calls.starts


def test_polling_pauses_while_hidden_and_resumes_when_visible(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, app):
        quotes = _Calls(app.live_market, "quotes")
        bars = _Calls(app.live_market, "bars")
        _load(page, url)
        page.wait_for(f"({_top_js('Market data')}).startsWith('Market data LIVE')")
        _hide(page, True)
        hidden_at = time.monotonic() + 0.5  # a request already running may still finish
        time.sleep(7)
        assert quotes.since(hidden_at) == 0 and bars.since(hidden_at) == 0
        assert page.evaluate("livePollingState().timers") <= 1  # only the render-only expiry tick
        shown_at = time.monotonic()
        _hide(page, False)
        _eventually(lambda: quotes.since(shown_at) >= 1 and bars.since(shown_at) >= 1, timeout=2)


def test_forming_candle_updates_and_a_new_candle_is_appended(page, tmp_path) -> None:
    offset = {"seconds": 0.0}
    # Fake market time runs ~2 minutes behind (never "in the future") and starts 5 s into a
    # minute, so no M1 candle rolls over by itself during the test.
    lag = 120 + time.time() % 60 - 5

    def fake_now() -> float:
        return time.time() - lag + offset["seconds"]

    fake = FakeMT5(now=fake_now)
    with _serve(tmp_path, fake=fake, clock=lambda: datetime.fromtimestamp(fake_now(), UTC)) as (
        url,
        app,
    ):
        app.save_preferences({"selected_chart_timeframe": "M1"})
        _load(page, url, "charts")
        page.wait_for("document.querySelector('#chart-status')?.textContent.includes('LIVE')")
        page.wait_for("state.bars?.bars?.length === 300")
        first = page.evaluate(
            "(() => { const b = state.bars.bars; window.__completed = b.at(-2);"
            " document.querySelector('#price-chart').__marker = 'kept';"
            " return { t: b.at(-1).t, c: b.at(-1).c, complete: b.at(-1).complete }; })()"
        )
        assert first["complete"] is False
        # The market moves: the forming candle's close follows; completed candles untouched.
        fake.symbols["EURUSD"]["bid"] = round(first["c"] + 0.0042, 5)
        page.wait_for(f"state.bars.bars.at(-1).c === {fake.symbols['EURUSD']['bid']}", timeout=8)
        assert page.evaluate("state.bars.bars.at(-1).t") == first["t"]
        assert page.evaluate("state.bars.bars.at(-1).h") >= fake.symbols["EURUSD"]["bid"]
        assert page.evaluate("state.bars.bars.at(-2) === window.__completed")  # same object
        # The minute advances: one new forming candle; the previous one is now complete.
        offset["seconds"] += 60
        page.wait_for(f"state.bars.bars.at(-1).t !== {json.dumps(first['t'])}", timeout=8)
        result = page.evaluate(
            "(() => { const b = state.bars.bars; const times = b.map(x => x.t);"
            " return { n: b.length, unique: new Set(times).size, last: b.at(-1).t,"
            " previous: b.find(x => x.t === " + json.dumps(first["t"]) + "),"
            " forming: b.filter(x => !x.complete).length,"
            " evidence: candleEvidence(b.find(x => x.t === " + json.dumps(first["t"]) + "),"
            " state.bars.latest_tick_utc) }; })()"
        )
        opened = datetime.fromisoformat(first["t"].replace("Z", "+00:00"))
        assert result["last"] == (opened + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        # One candle appended, none duplicated; the oldest, now outside the backend's
        # 300-candle window, is kept (it may be under inspection in a panned view).
        assert result["n"] == result["unique"] == 301
        assert result["previous"]["complete"] is True and result["evidence"] == "tick"
        assert result["forming"] == 1
        assert page.evaluate("document.querySelector('#price-chart').__marker") == "kept"
        assert page.evaluate("document.querySelector('#timeframe-select').value") == "M1"


def test_chart_is_not_rebuilt_or_repainted_when_nothing_changed(page, tmp_path) -> None:
    offset = {"seconds": 0.0}
    lag = 120 + time.time() % 60 - 5  # starts 5 s into a minute: no M1 rollover in the test

    def fake_now() -> float:
        return time.time() - lag + offset["seconds"]

    with _serve(
        tmp_path,
        fake=FakeMT5(now=fake_now),
        clock=lambda: datetime.fromtimestamp(fake_now(), UTC),
    ) as (url, app):
        app.save_preferences({"selected_chart_timeframe": "M1"})
        _load(page, url, "charts")
        page.wait_for("document.querySelector('#chart-status')?.textContent.includes('LIVE')")
        page.evaluate("document.querySelector('#price-chart').__marker = 'kept'")
        draws = page.evaluate("state.chartDraws")
        requests = page.evaluate("livePollingState().bars.requests")
        page.wait_for(f"livePollingState().bars.requests >= {requests + 2}", timeout=15)
        page.evaluate("loadDashboard({ background: true })")  # periodic snapshot refresh
        assert page.evaluate("document.querySelector('#price-chart').__marker") == "kept"
        assert page.evaluate("state.chartDraws") == draws
        assert page.evaluate("document.querySelector('.chart-symbol').textContent") == "EURUSD"


def test_late_answer_for_an_old_selection_cannot_overwrite_the_chart(page, tmp_path) -> None:
    slow = {"EURUSD": False}
    with _serve(tmp_path, fake=FakeMT5()) as (url, app):
        _Calls(app.live_market, "bars", delay=lambda args: 3.0 if slow.get(args[0]) else 0)
        _load(page, url, "charts")
        page.wait_for("document.querySelector('#chart-status')?.textContent.includes('LIVE')")
        slow["EURUSD"] = True
        page.evaluate("livePolling.bars.now()")
        page.wait_for("livePolling.bars.inFlight === true")
        page.select("#pair-select", "USDJPY")
        page.wait_for("state.bars?.symbol === 'USDJPY'", timeout=8)
        time.sleep(3.5)  # the old EURUSD answer arrives in the meantime
        assert page.evaluate("state.bars.symbol") == "USDJPY"
        assert page.evaluate("document.querySelector('.chart-symbol').textContent") == "USDJPY"
        assert "LIVE" in page.evaluate(_text("#chart-status"))


def test_clock_only_closed_candles_are_not_presented_as_confirmed(page, tmp_path) -> None:
    fake = FakeMT5(tick_age_seconds=600)  # feed quiet for 10 minutes
    with _serve(tmp_path, fake=fake) as (url, app):
        app.save_preferences({"selected_chart_timeframe": "M1"})
        _load(page, url, "charts")
        page.wait_for("state.bars?.bars?.length === 300")
        kinds = page.evaluate(
            "state.bars.bars.map(b => candleEvidence(b, state.bars.latest_tick_utc))"
        )
        assert kinds[-1] == "forming"
        assert kinds.count("clock") >= 5  # closed by time only since the last tick
        assert "tick" in kinds  # older candles are confirmed by the last tick
        assert "unconfirmed" in page.evaluate(_text("#chart-status"))
        assert "closed by clock only" in page.evaluate(_text("#chart-foot"))


def test_merge_keeps_unchanged_candles_and_never_duplicates(page, tmp_path) -> None:
    with _serve(tmp_path) as (url, _):
        _load(page, url)
        result = page.evaluate(
            "(() => { const bar = (t, c, complete = true) => ({ t, o: 1, h: 2, l: 0, c, v: 1,"
            " complete, close_t: null });"
            " const old = [bar('2026-11-11T10:00:00Z', 1.1),"
            " bar('2026-11-11T10:01:00Z', 1.2, false)];"
            " const incoming = [bar('2026-11-11T10:02:00Z', 1.4, false),"
            " bar('2026-11-11T10:00:00Z', 1.1), bar('2026-11-11T10:01:00Z', 1.3),"
            " bar('2026-11-11T10:01:00Z', 1.3)];"
            " const merged = mergeBars(old, incoming);"
            " return { times: merged.map(b => b.t), closes: merged.map(b => b.c),"
            " kept: merged[0] === old[0] }; })()"
        )
        assert result["times"] == [
            "2026-11-11T10:00:00Z",
            "2026-11-11T10:01:00Z",
            "2026-11-11T10:02:00Z",
        ]
        assert result["closes"] == [1.1, 1.3, 1.4]  # MT5's values; nothing interpolated
        assert result["kept"] is True


def test_failures_back_off_without_a_tight_loop_and_polling_recovers(
    page, tmp_path, monkeypatch
) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, app):
        _load(page, url)
        page.wait_for(f"({_top_js('Market data')}).startsWith('Market data LIVE')")
        calls = _Calls(app.live_market, "quotes")

        def broken(*args, **kwargs):
            raise RuntimeError("synthetic outage")

        calls.original = broken
        start = time.monotonic()
        page.wait_for(f"({_top_js('Market data')}).startsWith('Market data CACHED')", timeout=8)
        time.sleep(10)
        assert calls.since(start) <= 3, calls.starts  # 3 s, then 12 s backoff: no tight loop
        assert page.evaluate("livePollingState().quotes.failures") >= 1
        page.evaluate("POLL.maxBackoffMs = 3000")  # keep the test short; logic unchanged
        calls.original = app.live_market.__class__.quotes.__get__(app.live_market)
        page.wait_for(f"({_top_js('Market data')}).startsWith('Market data LIVE')", timeout=15)
        assert page.evaluate("livePollingState().quotes.failures") == 0


def test_starting_twice_adds_no_timers_or_listeners_and_stop_ends_polling(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, app):
        quotes = _Calls(app.live_market, "quotes")
        bars = _Calls(app.live_market, "bars")
        _load(page, url)
        page.wait_for(f"({_top_js('Market data')}).startsWith('Market data LIVE')")
        page.evaluate("startLivePolling(); startLivePolling(); startLivePolling()")
        assert _listener_count(page, "document", "visibilitychange") == 1
        assert _listener_count(page, "window", "pagehide") == 1
        assert page.evaluate("livePollingState().timers") <= 4  # quotes, bars, snapshot, expiry
        page.evaluate("stopLivePolling()")
        stopped_at = time.monotonic() + 0.5
        state = page.evaluate("livePollingState()")
        assert state["started"] is False and state["timers"] == 0
        assert _listener_count(page, "document", "visibilitychange") == 0
        time.sleep(7)
        assert quotes.since(stopped_at) == 0 and bars.since(stopped_at) == 0
        restarted_at = time.monotonic()
        page.evaluate("startLivePolling()")
        _eventually(lambda: quotes.since(restarted_at) >= 1, timeout=3)


# ---------------------------------------------------------------- chart zoom, pan, Go Live
def _box(page) -> dict:
    return page.evaluate(
        "(() => { const r = document.querySelector('#price-chart').getBoundingClientRect();"
        " return { x: r.left, y: r.top, w: r.width, h: r.height }; })()"
    )


def _plot_x(box: dict, fraction: float) -> float:
    return box["x"] + 6 + (box["w"] - 76) * fraction  # CHART_PAD left 6, right 70


def _mouse(page, kind: str, x: float, y: float, **extra) -> None:
    page.send("Input.dispatchMouseEvent", type=kind, x=x, y=y, **extra)


def _wheel(page, x: float, y: float, delta_y: float, times: int = 1) -> None:
    for _ in range(times):
        _mouse(page, "mouseWheel", x, y, deltaX=0, deltaY=delta_y)


def _drag(page, x0: float, y0: float, x1: float, y1: float, steps: int = 12) -> None:
    _mouse(page, "mousePressed", x0, y0, button="left", buttons=1, clickCount=1)
    for step in range(1, steps + 1):
        fraction = step / steps
        _mouse(
            page,
            "mouseMoved",
            x0 + (x1 - x0) * fraction,
            y0 + (y1 - y0) * fraction,
            button="left",
            buttons=1,
        )
    _mouse(page, "mouseReleased", x1, y1, button="left", buttons=0, clickCount=1)


def _view(page) -> dict:
    return page.evaluate("chartViewState()")


def _chart_ready(page, url: str) -> dict:
    _load(page, url, "charts")
    page.wait_for("document.querySelector('#chart-status')?.textContent.includes('LIVE')")
    page.wait_for("chartViewState() !== null && state.bars.bars.length >= 300")
    return _box(page)


def _zoom_in_at_latest(page, box: dict, times: int = 4) -> dict:
    """Zoom in with the cursor on the newest candle: the chart keeps following it."""
    _wheel(page, _plot_x(box, 0.999), box["y"] + box["h"] / 2, -120, times)
    return _view(page)


def _aligned_fake_clock():
    offset = {"seconds": 0.0}
    lag = 120 + time.time() % 60 - 5  # 2 min behind, 5 s into a minute: no stray rollover

    def fake_now() -> float:
        return time.time() - lag + offset["seconds"]

    return offset, fake_now


def test_wheel_zooms_in_and_out_within_limits(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, _):
        box = _chart_ready(page, url)
        initial = _view(page)
        middle = (_plot_x(box, 0.5), box["y"] + box["h"] / 2)
        _wheel(page, *middle, -120)
        zoomed = _view(page)
        assert zoomed["count"] < initial["count"]  # zoom in: fewer, wider candles
        _wheel(page, *middle, 120, times=2)
        assert _view(page)["count"] > zoomed["count"]  # zoom out: wider range again
        _wheel(page, *middle, -120, times=40)
        assert _view(page)["count"] == _view(page)["minCount"] == 10  # most zoomed in
        _wheel(page, *middle, 120, times=40)
        widest = _view(page)
        assert widest["count"] == widest["maxCount"] <= widest["total"]  # never past history
        assert widest["start"] >= 0 and widest["end"] <= widest["total"] - 1


def test_zoom_keeps_the_candle_under_the_cursor(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, _):
        box = _chart_ready(page, url)
        for fraction in (0.3, 0.7):
            x, y = _plot_x(box, fraction), box["y"] + box["h"] / 2
            before = page.evaluate(f"candleAtX({x})")
            _wheel(page, x, y, -120, times=3)
            after = page.evaluate(f"candleAtX({x})")
            positions = page.evaluate(
                f"[state.bars.bars.findIndex(b => b.t === {json.dumps(before)}),"
                f" state.bars.bars.findIndex(b => b.t === {json.dumps(after)})]"
            )
            assert abs(positions[0] - positions[1]) <= 1, (before, after)
            _wheel(page, x, y, 120, times=3)


def test_dragging_pans_to_older_and_back_to_newer_candles(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, _):
        box = _chart_ready(page, url)
        zoomed = _zoom_in_at_latest(page, box)
        assert zoomed["follow"] is True and zoomed["lastT"] == zoomed["newestT"]
        y = box["y"] + box["h"] / 2
        _drag(page, _plot_x(box, 0.6), y, _plot_x(box, 0.6) - 300, y)  # drag left: older
        older = _view(page)
        assert older["follow"] is False and older["end"] < zoomed["end"]
        assert older["count"] == zoomed["count"]  # panning does not zoom
        assert page.evaluate("document.querySelector('#go-live') !== null")
        assert "Viewing history" in page.evaluate(_text("#chart-view-controls"))
        _drag(page, _plot_x(box, 0.4), y, _plot_x(box, 0.4) + 120, y)  # drag right: newer
        newer = _view(page)
        assert older["end"] < newer["end"] < zoomed["end"]
        _drag(page, _plot_x(box, 0.2), y, _plot_x(box, 0.2) + 600, y)  # all the way back
        back = _view(page)
        assert back["follow"] is True and back["lastT"] == back["newestT"]
        assert page.evaluate("document.querySelector('#go-live')") is None


def test_pan_is_clamped_to_the_available_history(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, _):
        box = _chart_ready(page, url)
        zoomed = _zoom_in_at_latest(page, box)
        y = box["y"] + box["h"] / 2
        _drag(page, _plot_x(box, 0.9), y, _plot_x(box, 0.9) - 20000, y, steps=20)
        oldest = _view(page)
        assert oldest["start"] == 0 and oldest["end"] == zoomed["count"] - 1
        assert oldest["firstT"] == page.evaluate("state.bars.bars[0].t")
        _drag(page, _plot_x(box, 0.1), y, _plot_x(box, 0.1) + 20000, y, steps=20)
        newest = _view(page)
        assert newest["end"] == newest["total"] - 1 and newest["follow"] is True


def test_price_axis_drag_changes_the_price_scale_not_the_prices(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, _):
        box = _chart_ready(page, url)
        ohlc = page.evaluate("JSON.stringify(state.bars.bars.map(b => [b.t, b.o, b.h, b.l, b.c]))")
        before = _view(page)
        axis_x, middle_y = box["x"] + box["w"] - 30, box["y"] + box["h"] / 2
        _drag(page, axis_x, middle_y, axis_x, middle_y + 80)  # down: wider price range
        wider = _view(page)
        assert wider["high"] - wider["low"] > (before["high"] - before["low"]) * 1.5
        _drag(page, axis_x, middle_y, axis_x, middle_y - 160)  # up: narrower than before
        narrower = _view(page)
        assert narrower["high"] - narrower["low"] < before["high"] - before["low"]
        assert narrower["start"] == before["start"] and narrower["end"] == before["end"]
        assert (
            page.evaluate("JSON.stringify(state.bars.bars.map(b => [b.t, b.o, b.h, b.l, b.c]))")
            == ohlc
        )
        _drag(page, axis_x, middle_y, axis_x, middle_y - 5000, steps=20)
        assert _view(page)["priceScale"] == pytest.approx(0.1)  # bounded
        _mouse(page, "mousePressed", axis_x, middle_y, button="left", buttons=1, clickCount=1)
        _mouse(page, "mouseReleased", axis_x, middle_y, button="left", buttons=0, clickCount=1)
        _mouse(page, "mousePressed", axis_x, middle_y, button="left", buttons=1, clickCount=2)
        _mouse(page, "mouseReleased", axis_x, middle_y, button="left", buttons=0, clickCount=2)
        assert _view(page)["priceScale"] == 1  # double-click: automatic fit again


def test_new_candles_do_not_move_a_panned_view(page, tmp_path) -> None:
    offset, fake_now = _aligned_fake_clock()
    fake = FakeMT5(now=fake_now)
    with _serve(tmp_path, fake=fake, clock=lambda: datetime.fromtimestamp(fake_now(), UTC)) as (
        url,
        app,
    ):
        app.save_preferences({"selected_chart_timeframe": "M1"})
        box = _chart_ready(page, url)
        _zoom_in_at_latest(page, box)
        y = box["y"] + box["h"] / 2
        _drag(page, _plot_x(box, 0.6), y, _plot_x(box, 0.6) - 250, y)
        panned = _view(page)
        assert panned["follow"] is False
        offset["seconds"] += 60  # the next M1 candle opens
        page.wait_for(f"state.bars.bars.at(-1).t !== {json.dumps(panned['newestT'])}", timeout=10)
        after = _view(page)
        assert (after["firstT"], after["lastT"], after["count"]) == (
            panned["firstT"],
            panned["lastT"],
            panned["count"],
        )
        assert after["follow"] is False and after["newestT"] != panned["newestT"]
        assert after["total"] == panned["total"] + 1  # appended; nothing lost
        assert page.evaluate("new Set(state.bars.bars.map(b => b.t)).size") == after["total"]
        assert page.evaluate("document.querySelector('#go-live') !== null")


def test_forming_candle_keeps_updating_while_zoomed_in(page, tmp_path) -> None:
    offset, fake_now = _aligned_fake_clock()
    fake = FakeMT5(now=fake_now)
    with _serve(tmp_path, fake=fake, clock=lambda: datetime.fromtimestamp(fake_now(), UTC)) as (
        url,
        app,
    ):
        app.save_preferences({"selected_chart_timeframe": "M1"})
        box = _chart_ready(page, url)
        zoomed = _zoom_in_at_latest(page, box)
        bid = round(page.evaluate("state.bars.bars.at(-1).c") + 0.0031, 5)
        fake.symbols["EURUSD"]["bid"] = bid
        page.wait_for(f"state.bars.bars.at(-1).c === {bid}", timeout=10)
        view = _view(page)
        assert view["count"] == zoomed["count"] and view["follow"] is True
        assert view["lastT"] == view["newestT"]
        offset["seconds"] += 60  # while following, a new candle comes into view
        page.wait_for(f"chartViewState().lastT !== {json.dumps(view['lastT'])}", timeout=10)
        moved = _view(page)
        assert moved["lastT"] == moved["newestT"] and moved["count"] == zoomed["count"]


def test_go_live_returns_to_the_latest_candles_and_follows(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, _):
        box = _chart_ready(page, url)
        zoomed = _zoom_in_at_latest(page, box)
        assert "Following latest" in page.evaluate(_text("#chart-view-controls"))
        y = box["y"] + box["h"] / 2
        _drag(page, _plot_x(box, 0.6), y, _plot_x(box, 0.6) - 300, y)
        assert _view(page)["follow"] is False
        assert "LIVE" in page.evaluate(_text("#chart-status"))  # feed status is separate
        page.click("#go-live")
        live = _view(page)
        assert live["follow"] is True and live["lastT"] == live["newestT"]
        assert live["count"] == zoomed["count"]  # zoom level kept
        assert page.evaluate("document.querySelector('#go-live')") is None
        assert "Following latest" in page.evaluate(_text("#chart-view-controls"))


def _panned_away(page, box: dict) -> dict:
    _zoom_in_at_latest(page, box)
    y = box["y"] + box["h"] / 2
    _drag(page, _plot_x(box, 0.6), y, _plot_x(box, 0.6) - 300, y)
    _drag(page, box["x"] + box["w"] - 30, y, box["x"] + box["w"] - 30, y + 60)
    view = _view(page)
    assert view["follow"] is False and view["priceScale"] != 1
    return view


def test_switching_pair_resets_the_viewport(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, _):
        box = _chart_ready(page, url)
        initial = _view(page)
        _panned_away(page, box)
        page.select("#pair-select", "USDJPY")
        page.wait_for("state.bars?.symbol === 'USDJPY' && chartViewState() !== null", timeout=8)
        page.wait_for("chartViewState().newestT === state.bars.bars.at(-1).t")
        view = _view(page)
        assert view["follow"] is True and view["priceScale"] == 1
        assert view["count"] == initial["count"] and view["lastT"] == view["newestT"]


def test_switching_timeframe_resets_the_viewport(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, _):
        box = _chart_ready(page, url)
        initial = _view(page)
        _panned_away(page, box)
        page.select("#timeframe-select", "M15")
        page.wait_for("state.bars?.timeframe === 'M15' && chartViewState() !== null", timeout=8)
        page.wait_for("chartViewState().newestT === state.bars.bars.at(-1).t")
        view = _view(page)
        assert view["follow"] is True and view["priceScale"] == 1
        assert view["count"] == initial["count"] and view["lastT"] == view["newestT"]


def test_resizing_keeps_the_viewing_position_and_zoom(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, _):
        box = _chart_ready(page, url)
        panned = _panned_away(page, box)
        try:
            page.set_viewport(1300, 900)
            page.wait_for(
                f"document.querySelector('#price-chart').getBoundingClientRect().width < {box['w']}"
            )
            page.evaluate("drawChart()")  # the resize listener does this; make it deterministic
            resized = _view(page)
            assert (resized["lastT"], resized["count"], resized["follow"]) == (
                panned["lastT"],
                panned["count"],
                False,
            )
            assert resized["priceScale"] == panned["priceScale"]
        finally:
            page.set_viewport(1600, 1000)


def test_automatic_refreshes_keep_canvas_and_viewport(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, _):
        box = _chart_ready(page, url)
        panned = _panned_away(page, box)
        page.evaluate("document.querySelector('#price-chart').__marker = 'kept'")
        quotes = page.evaluate("livePollingState().quotes.requests")
        bars = page.evaluate("livePollingState().bars.requests")
        page.wait_for(
            f"livePollingState().bars.requests >= {bars + 2}"
            f" && livePollingState().quotes.requests >= {quotes + 2}",
            timeout=15,
        )
        page.evaluate("loadDashboard({ background: true })")  # snapshot refresh
        assert page.evaluate("document.querySelector('#price-chart').__marker") == "kept"
        view = _view(page)
        for key in ("firstT", "lastT", "count", "follow", "priceScale"):
            assert view[key] == panned[key], key


def test_theme_and_timezone_changes_keep_the_viewport_and_time_labels(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5()) as (url, _):
        box = _chart_ready(page, url)
        panned = _panned_away(page, box)
        try:
            for change in ("{ theme: 'light' }", "{ user_timezone: 'Europe/London' }"):
                page.evaluate("document.querySelector('#price-chart').__marker = 'old'")
                page.evaluate(f"savePreferences({change})")  # full page render, new canvas
                page.wait_for("!document.querySelector('#price-chart').__marker")
                page.wait_for("chartViewState() !== null")
                view = _view(page)
                for key in ("firstT", "lastT", "count", "follow", "priceScale"):
                    assert view[key] == panned[key], (change, key)
            # Time-axis labels are drawn below the plot (regression: they were off-canvas).
            label_pixels = page.evaluate(
                "(() => { const c = document.querySelector('#price-chart');"
                " const r = devicePixelRatio || 1;"
                " const d = c.getContext('2d').getImageData(0, c.height - Math.round(18 * r),"
                " c.width - Math.round(70 * r), Math.round(18 * r)).data;"
                " let n = 0; for (let i = 3; i < d.length; i += 4) if (d[i] > 0) n++;"
                " return n; })()"
            )
            assert label_pixels > 50
        finally:
            page.evaluate(
                "savePreferences({ theme: 'dark', user_timezone: 'Africa/Johannesburg' })"
            )


def test_invalid_and_unplottable_candles_are_reported_not_drawn_or_fatal(page, tmp_path) -> None:
    fake = FakeMT5()

    def anomalies(symbol, rows):
        rows = list(rows)
        rows[-5] = rows[-5]._replace(close=float("nan"))  # cannot be drawn
        rows[-8] = rows[-8]._replace(high=rows[-8].low - 0.001)  # inconsistent
        return rows + [rows[-10]]  # duplicate open time

    fake.rates_hook = anomalies
    with _serve(tmp_path, fake=fake) as (url, _):
        box = _chart_ready(page, url)
        status = page.evaluate(_text("#chart-status"))
        # Flagged: the NaN bar, the inconsistent bar and the kept bar of the duplicated time;
        # plus the rejected duplicate itself.
        assert "LIVE" in status and "4 invalid" in status
        kinds = page.evaluate("state.bars.bars.map(b => b.closure)")
        assert set(kinds) <= {"forming", "tick_observed", "clock_only", "unknown"}
        assert page.evaluate("state.bars.bars.filter(b => b.c === null).length") == 1
        view = _view(page)
        assert view["total"] == page.evaluate("state.bars.bars.length") - 1  # NaN bar not drawn
        colours = _canvas_colours(page)
        assert colours["green"] > 0 and colours["red"] > 0  # the chart still draws
        # Interaction still works on the remaining candles.
        _zoom_in_at_latest(page, box)
        y = box["y"] + box["h"] / 2
        _drag(page, _plot_x(box, 0.6), y, _plot_x(box, 0.6) - 200, y)
        assert _view(page)["follow"] is False
        page.click("#go-live")
        assert _view(page)["follow"] is True
