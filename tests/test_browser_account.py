"""Phase 3 dashboard: Account & Broker page (read-only). Synthetic FakeMT5 data."""

from __future__ import annotations

import time
from datetime import timedelta

import pytest
from browser_harness import Browser, find_browser
from fake_mt5 import READ_ONLY_CALLS, FakeMT5
from test_account_intelligence import SECRET_LOGIN, SECRET_NAME, cent_symbols
from test_browser_smoke import _load, _serve

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


# Text of an element (whitespace-normalised) and the status of the value it shows.
_READ = (
    " const v = e?.querySelector('[data-field-status]');"
    " return { text: (T ?? '').replace(/\\s+/g, ' ').trim(),"
    " status: v?.dataset.fieldStatus ?? null }; })()"
)


def _field(page, key: str) -> dict:
    find = f'#account-content [data-account-field="{key}"]'
    return page.evaluate(
        f"(() => {{ const e = document.querySelector({find!r});"
        + _READ.replace("T", "e?.querySelector('.stat-value, strong')?.textContent")
    )


def _cell(page, row: str, column: int) -> dict:
    find = f'#instrument-table tr[data-row="{row}"] td'
    return page.evaluate(
        f"(() => {{ const e = document.querySelectorAll({find!r})[{column}];"
        + _READ.replace("T", "e?.textContent")
    )


_BADGE_CACHED = "document.querySelector('#account-heading-badge').textContent.includes('CACHED')"
_SAYS_UNAVAILABLE = "document.querySelector('#account-content').textContent.includes('unavailable')"


def _open(page, url) -> None:
    _load(page, url, "account")
    page.wait_for("document.querySelector('#instrument-table') !== null", timeout=15)


def test_account_overview_and_instrument_comparison_render(page, tmp_path) -> None:
    fake = FakeMT5(symbols=cent_symbols())
    with _serve(tmp_path, fake=fake) as (url, _):
        _open(page, url)
        assert page.evaluate("document.querySelector('.page-heading h1').innerText") == (
            "Account & Broker"
        )
        assert "LIVE" in page.text("#account-heading-badge")
        assert _field(page, "currency") == {"text": "USC", "status": "verified"}
        assert _field(page, "balance")["text"] == "1000 USC"
        assert _field(page, "equity")["text"] == "1000 USC"
        assert _field(page, "leverage")["text"] == "1:2000"
        assert "LIVE" in _field(page, "freshness")["text"]
        note = page.text("#account-currency-note")
        assert "1/100 USD" in note and "confirmed by tick values" in note
        heads = page.evaluate(
            "[...document.querySelectorAll('#instrument-table thead th[data-instrument]')]"
            ".map(th => th.textContent.replace(/\\s+/g, ' ').trim())"
        )
        assert heads == ["EURUSDc verified", "GBPUSDc verified", "USDJPYc verified"]
        assert _cell(page, "contract_size", 1)["text"] == "1000 units"
        assert _cell(page, "tick_value", 1)["text"] == "1 USC"
        assert _cell(page, "volume_min", 3)["text"] == "0.01 lots"
        assert _cell(page, "trade_mode", 2)["text"] == "full"
        assert "LIVE" in _cell(page, "quote_freshness", 1)["text"]
        assert "Exness-MT5Trial9" in page.text("#account-source")
        assert "Established" in page.text("#account-source")
        assert set(fake.calls) <= READ_ONLY_CALLS


def test_zero_values_are_distinct_from_unavailable(page, tmp_path) -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.account_missing.add("equity")
    fake.symbols["EURUSDc"]["info_missing"] = ["trade_tick_value"]
    with _serve(tmp_path, fake=fake) as (url, _):
        _open(page, url)
        used = _field(page, "margin")
        assert used == {"text": "0 USC", "status": "verified"}  # zero, shown as zero
        equity = _field(page, "equity")
        assert equity["status"] == "unavailable" and equity["text"] == "Unavailable"
        level = _field(page, "margin_level")
        assert level == {"text": "N/A", "status": "not_applicable"}  # not "0 %"
        tick = _cell(page, "tick_value", 1)
        assert tick["status"] == "unavailable" and tick["text"] == "Unavailable"
        assert "incomplete" in page.text("#instrument-table thead").lower()
        assert _cell(page, "stops_level", 1)["text"] == "0 points"


def test_invalid_values_are_labelled(page, tmp_path) -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.account["balance"] = float("nan")
    with _serve(tmp_path, fake=fake) as (url, _):
        _open(page, url)
        balance = _field(page, "balance")
        assert balance["status"] == "invalid" and balance["text"] == "Invalid"
        assert "not checkable" in page.text("#account-checks").lower()  # equity check
        assert "invalid" in page.text("#account-source").lower()


def test_stale_and_unavailable_information_is_visible(page, tmp_path) -> None:
    fake = FakeMT5(symbols=cent_symbols(), tick_age_seconds=600)
    with _serve(tmp_path, fake=fake) as (url, app):
        _open(page, url)
        assert "STALE" in _cell(page, "quote_freshness", 1)["text"]
        app.account.ttl = timedelta(0)  # every request reads MT5 again
        fake.running = False  # MT5 goes away: the backend answers from its last snapshot
        page.evaluate("livePolling.account.now()")
        page.wait_for(_BADGE_CACHED, timeout=10)
        assert "Not live" in page.text("#account-source")
        assert "CACHED" in _cell(page, "quote_freshness", 1)["text"]


def test_unavailable_mt5_shows_no_values(page, tmp_path) -> None:
    with _serve(tmp_path, fake=None) as (url, _):
        _load(page, url, "account")
        page.wait_for(_SAYS_UNAVAILABLE, timeout=15)
        assert page.evaluate("document.querySelector('#instrument-table')") is None
        assert "UNAVAILABLE" in page.text("#account-heading-badge")


def test_no_execution_controls_or_identifiers(page, tmp_path) -> None:
    fake = FakeMT5(symbols=cent_symbols())
    with _serve(tmp_path, fake=fake) as (url, _):
        _open(page, url)
        text = page.text("main")
        controls = page.evaluate(
            "[...document.querySelectorAll('main button, main input, main select, main form')]"
            ".map(e => (e.textContent || e.value || e.name || '').trim())"
        )
        assert controls == []
        for word in ("Buy", "Sell", "Place order", "Close position", "Lot size"):
            assert word not in text
        html = page.evaluate("document.documentElement.outerHTML")
        state = page.evaluate("JSON.stringify(state.account)")
        for blob in (html, state):
            assert str(SECRET_LOGIN) not in blob and SECRET_NAME not in blob


def test_account_polling_runs_only_on_its_page(page, tmp_path) -> None:
    fake = FakeMT5(symbols=cent_symbols())
    with _serve(tmp_path, fake=fake) as (url, app):
        _load(page, url, "overview")
        time.sleep(1.5)
        assert page.evaluate("livePollingState().account.requests") == 0
        assert app.account.reads == 0
        page.click('#primary-nav [data-page="account"]')
        page.wait_for("document.querySelector('#instrument-table') !== null", timeout=15)
        assert page.evaluate("livePollingState().account.scheduled") is True
        page.click('#primary-nav [data-page="markets"]')
        page.wait_for("!livePollingState().account.inFlight")
        assert page.evaluate("livePollingState().account.scheduled") is False


def test_phone_width_has_no_page_overflow(page, tmp_path) -> None:
    with _serve(tmp_path, fake=FakeMT5(symbols=cent_symbols())) as (url, _):
        page.set_viewport(400, 900, mobile=True)
        _open(page, url)
        time.sleep(0.3)
        overflow = page.evaluate(
            "document.documentElement.scrollWidth - document.documentElement.clientWidth"
        )
        assert overflow <= 0
