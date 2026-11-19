"""P5.2 Trade History page and the Decisions link action, in a real browser.

The real app is served against a temporary data directory and the in-process fake MT5.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from browser_harness import Browser, find_browser
from risk_fixtures import BUY_LEVEL, envelope, make_fake
from test_browser_smoke import _load, _serve

BROWSER = find_browser()
pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(BROWSER is None, reason="no Chromium-family browser installed"),
]
BANNER = (
    "Record trades you have already executed in MT5. Johanna does not place, modify or close "
    "orders."
)


@pytest.fixture(scope="module")
def browser():
    instance = Browser(BROWSER)
    instance.set_viewport(1500, 1100)
    yield instance
    instance.close()


def _utc_input(minutes: int) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%S")


def _fill(page, form: str, values: dict) -> None:
    page.evaluate(
        f"(() => {{ const f = document.querySelector('[data-trade-form=\"{form}\"]');"
        f" const v = {json.dumps(values)};"
        ' for (const [k, x] of Object.entries(v)) f.querySelector(`[name="${k}"]`).value = x; })()'
    )


def _text(page, selector: str) -> str:
    return page.evaluate(
        f"document.querySelector('{selector}')?.textContent.replace(/\\s+/g, ' ').trim() ?? ''"
    )


def _seed_trade(app, symbol="EURUSDc") -> str:
    _, body = app.trades.create_trade(
        {
            "request_id": str(uuid.uuid4()),
            "symbol": symbol,
            "direction": "BUY",
            "entry": {
                "time_utc": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                "price": 1.1,
                "volume": 0.1,
            },
        }
    )
    return body["trade_id"]


def test_trade_history_records_and_shows_a_manual_trade(browser, tmp_path) -> None:
    with _serve(tmp_path, fake=make_fake()) as (url, app):
        _load(browser, url, "history")
        browser.wait_for("document.querySelector('#trades-table, #trades-content .empty-state')")
        assert _text(browser, "#trades-banner") == BANNER
        assert _text(browser, ".page-heading h1") == "Trade Journal"  # B7.2 name
        labels = browser.evaluate(
            "[...document.querySelectorAll('main button')].map(b => b.textContent.trim())"
        )
        for forbidden in ("Buy", "Sell", "Place", "Close position", "Modify", "Execute", "Send"):
            assert not any(label.startswith(forbidden) for label in labels), labels
        _fill(
            browser,
            "create",
            {
                "symbol": "EURUSDc",
                "direction": "BUY",
                "time_utc": _utc_input(60),
                "price": "1.1",
                "volume": "0.1",
            },
        )
        # a double click must not record twice (one request id, one in-flight write)
        browser.evaluate(
            "(() => { const b = document.querySelector('[data-trade-action=\"create\"]');"
            " b.click(); b.click(); })()"
        )
        browser.wait_for("document.querySelector('#trade-detail') !== null", 15)
        time.sleep(0.3)
        assert len(app.trades.list()["trades"]) == 1
        assert "MISSING" in _text(browser, '#trades-table [data-col="protection"]').upper()
        assert "MANUAL UNVERIFIED" in _text(browser, "#trade-detail").upper()
        assert "No exits" in _text(browser, "#trade-net")
        assert "UNKNOWN on 1 fill" in _text(browser, "#trade-cost-commission")

        _fill(
            browser,
            "fill",
            {
                "kind": "exit",
                "time_utc": _utc_input(30),
                "price": "1.101",
                "volume": "0.04",
                "commission": "-28",
            },
        )
        browser.click('[data-trade-action="fill"]')
        browser.wait_for(
            "document.querySelector('#trades-table [data-col=\"status\"]')"
            "?.textContent.includes('PARTIALLY')",
            15,
        )
        # captured spec: contract 1000 (synthetic cent symbol): 0.001 x 0.04 x 1000 x 100
        assert "4.00 USC" in _text(browser, "#trade-gross-calculated")
        assert "Not calculated" in _text(browser, "#trade-net")
        assert "unknown costs" in _text(browser, "#trade-net")
        assert "UNKNOWN on 1 fill" in _text(browser, "#trade-cost-commission")  # entry unknown
        assert "Not entered" in _text(browser, "#trade-gross-user")

        _fill(browser, "protection", {"stop_loss": "1.095", "effective_utc": _utc_input(20)})
        browser.click('[data-trade-action="protection"]')
        browser.wait_for(
            "document.querySelector('#trade-protection')?.textContent.includes('PARTIAL')", 15
        )

        _fill(browser, "note", {"text": "entered on the H1 pullback"})
        browser.click('[data-trade-action="note"]')
        browser.wait_for(
            "document.querySelector('#trade-notes')?.textContent.includes('pullback')", 15
        )

        _fill(browser, "void-trade", {"reason": "test record"})
        browser.click('[data-trade-action="void-trade"]')
        browser.wait_for("document.querySelector('#trade-voided') !== null", 15)
        assert "VOIDED" in _text(browser, '#trades-table [data-col="status"]').upper()
        assert browser.evaluate("document.querySelector('[data-trade-form=\"fill\"]')") is None


def test_validation_errors_are_shown_and_nothing_is_recorded(browser, tmp_path) -> None:
    with _serve(tmp_path, fake=make_fake()) as (url, app):
        _load(browser, url, "history")
        browser.wait_for("document.querySelector('[data-trade-form=\"create\"]') !== null")
        _fill(browser, "create", {"time_utc": _utc_input(60), "price": "1.1", "volume": "0.015"})
        browser.click('[data-trade-action="create"]')
        browser.wait_for("document.querySelector('#trades-message') !== null", 15)
        assert _text(browser, "#trades-message").startswith("Not recorded:")
        assert app.trades.list()["trades"] == []


def test_decision_can_be_linked_to_an_existing_trade(browser, tmp_path) -> None:
    with _serve(tmp_path, fake=make_fake()) as (url, app):
        decision = app.decisions.capture_analysis(envelope(level=BUY_LEVEL), "analysis_request")
        trade_id = _seed_trade(app)
        _load(browser, url, "signals")
        browser.wait_for("document.querySelector('#decisions-table') !== null", 15)
        assert "NONE" in _text(browser, '#decisions-table [data-col="execution"]').upper()
        browser.click(f'[data-link-decision="{decision}"]')
        browser.wait_for("document.querySelector('#decision-link-panel select') !== null", 15)
        assert (
            browser.evaluate("document.querySelector('#decision-link-panel select').value")
            == trade_id
        )
        browser.click("[data-link-confirm]")
        browser.wait_for(
            "document.querySelector('#decisions-table [data-col=\"execution\"]')"
            "?.textContent.toUpperCase().includes('MANUALLY')",
            15,
        )
        assert len(app.trades.list()["trades"]) == 1  # linking never creates a trade


def test_link_without_a_recorded_trade_explains_and_creates_nothing(browser, tmp_path) -> None:
    with _serve(tmp_path, fake=make_fake()) as (url, app):
        decision = app.decisions.capture_analysis(envelope(level=BUY_LEVEL), "analysis_request")
        _load(browser, url, "signals")
        browser.wait_for("document.querySelector('#decisions-table') !== null", 15)
        browser.click(f'[data-link-decision="{decision}"]')
        browser.wait_for(
            "document.querySelector('#decision-link-panel')?.textContent.includes('never becomes')",
            15,
        )
        assert browser.evaluate("document.querySelector('#decision-link-panel select')") is None
        assert app.trades.list()["trades"] == []
