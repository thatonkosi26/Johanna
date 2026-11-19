"""P6.2a Analytics page: shadow trade intents and the opportunity funnel in a real browser.
Intents are seeded through the scheduler's own completion path in a temporary journal."""

from __future__ import annotations

from datetime import timedelta

import pytest
from browser_harness import Browser, find_browser
from risk_fixtures import make_fake
from test_browser_smoke import _load, _serve
from test_shadow_intents_api import APPROVED, UNVERIFIED, WEDNESDAY, seed

BROWSER = find_browser()
pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(BROWSER is None, reason="no Chromium-family browser installed"),
]
LABEL = "HYPOTHETICAL — NOT AN ORDER"


@pytest.fixture(scope="module")
def browser():
    instance = Browser(BROWSER)
    instance.set_viewport(1500, 1100)
    yield instance
    instance.close()


def _text(page, selector: str) -> str:
    return page.evaluate(
        f"document.querySelector('{selector}')?.textContent.replace(/\\s+/g, ' ').trim() ?? ''"
    )


def _rows(page) -> int:
    return page.evaluate("document.querySelectorAll('#shadow-intents tbody tr').length")


def _buttons(page) -> list[str]:
    return sorted(
        page.evaluate(
            "[...document.querySelectorAll('main button')].map(b => b.textContent.trim())"
        )
    )


def test_empty_state_warning_and_funnel(browser, tmp_path) -> None:
    with _serve(tmp_path, fake=make_fake()) as (url, _app):
        _load(browser, url, "analytics")
        browser.wait_for(
            "document.querySelector('#shadow-intents-panel')?.textContent"
            ".includes('No shadow trade intents yet')",
            15,
        )
        panel = _text(browser, "#shadow-intents-panel")
        assert LABEL in _text(browser, "#shadow-intent-warning")
        assert "No shadow trade intents yet" in panel
        assert "not an instruction to trade" in panel
        browser.wait_for("document.querySelector('#shadow-funnel') !== null", 15)
        assert _text(browser, '[data-funnel-stage="windows_scheduled"] td:nth-child(2)') == "0"
        assert "Not available" in _text(browser, "#shadow-funnel-execution")
        assert _buttons(browser) == ["Refresh"]  # no intent actions, no scheduler control (B7.2)
        # P6.R operations readiness (moved to the System Control Centre in B7.2): diagnostic
        # only, unattended operation never permitted
        _load(browser, url, "control")
        browser.wait_for("document.querySelector('#readiness-checks') !== null", 15)
        assert _text(browser, "#readiness-unattended").endswith("NOT PERMITTED")
        blockers = browser.evaluate(
            "[...document.querySelectorAll('[data-readiness-blocker]')]"
            ".map(e => e.dataset.readinessBlocker)"
        )
        assert {"scheduler_disabled", "costs_unverified", "execution_disabled"} <= set(blockers)


def test_intents_table_filters_detail_and_no_zero_values(browser, tmp_path) -> None:
    with _serve(tmp_path, fake=make_fake()) as (url, app):
        seed(app.shadow, UNVERIFIED, WEDNESDAY)
        seed(app.shadow, APPROVED, WEDNESDAY + timedelta(hours=1), "USDJPY")
        _load(browser, url, "analytics")
        browser.wait_for("document.querySelectorAll('#shadow-intents tbody tr').length === 2", 15)
        unverified = "#shadow-intents tbody tr:nth-child(2)"  # newest first: USDJPY on top
        approved = "#shadow-intents tbody tr:nth-child(1)"
        assert "USDJPYc" in _text(browser, approved)
        missing = "Not available — costs not established"
        for cell in ("volume", "risk", "risk-pct", "net"):
            assert _text(browser, f'{unverified} [data-intent-cell="{cell}"]') == missing
        assert _text(browser, f'{unverified} [data-intent-chip="eligibility"]') == "UNVERIFIED"
        assert _text(browser, f'{unverified} [data-intent-chip="costs"]') == "unverified"
        assert "Costs not established: costs.net_reward_risk" in _text(
            browser, f'{unverified} [data-intent-cell="reason"]'
        )
        assert _text(browser, f'{approved} [data-intent-cell="volume"]') == "0.01"
        assert _text(browser, f'{approved} [data-intent-chip="eligibility"]') == "RISK APPROVED"
        assert _text(browser, f'{approved} [data-intent-chip="costs"]') == "established"
        # missing values never render as zero
        assert not any(
            _text(browser, f'{unverified} [data-intent-cell="{c}"]') in ("0", "0.00", "0.000%")
            for c in ("volume", "risk", "risk-pct")
        )
        # funnel
        assert _text(browser, '[data-funnel-stage="windows_scheduled"] td:nth-child(2)') == "2"
        assert (
            _text(browser, '[data-funnel-stage="risk_approved_shadow_intents"] td:nth-child(2)')
            == "1"
        )
        assert "costs not established 1" in _text(browser, "#shadow-funnel-categories")
        # filter by eligibility, then clear
        browser.select('[data-intent-filter="eligibility"]', "RISK_APPROVED")
        browser.wait_for("document.querySelectorAll('#shadow-intents tbody tr').length === 1", 15)
        assert "USDJPYc" in _text(browser, "#shadow-intents tbody tr")
        browser.select('[data-intent-filter="eligibility"]', "")
        browser.wait_for("document.querySelectorAll('#shadow-intents tbody tr').length === 2", 15)
        browser.select('[data-intent-filter="symbol"]', "GBPUSD")
        browser.wait_for(
            "document.querySelector('#shadow-intents-panel')?.textContent"
            ".includes('No intents match these filters')",
            15,
        )
        browser.select('[data-intent-filter="symbol"]', "")
        browser.wait_for("document.querySelectorAll('#shadow-intents tbody tr').length === 2", 15)
        # detail view of the unverified intent
        browser.click(unverified)
        browser.wait_for("document.querySelector('#shadow-intent-d-eligibility') !== null", 15)
        detail = _text(browser, "#shadow-intent-detail")
        assert LABEL in detail and "EURUSDc" in detail
        assert _text(browser, "#shadow-intent-d-volume").endswith(missing)
        assert "Costs not established" in _text(browser, "#shadow-intent-d-reasons")
        browser.click(unverified)  # the same row closes the detail view
        browser.wait_for("document.querySelector('#shadow-intent-detail') === null", 15)
        # read-only: no control that could execute, submit, approve, modify or delete
        assert _buttons(browser) == ["Refresh"]  # B7.2: no scheduler control
        words = browser.evaluate(
            "document.querySelector('#shadow-intents-panel').innerText.toLowerCase()"
        )
        for verb in ("execute", "submit", "place order", "delete", "approve now"):
            assert verb not in words
        assert app.shadow.enabled() is False  # the page never touches the scheduler
