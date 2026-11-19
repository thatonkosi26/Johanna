"""P6.1 / B7.2 Shadow Research page: read-only scheduler state and diagnostics in a real browser."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from browser_harness import Browser, find_browser
from risk_fixtures import make_fake
from test_browser_smoke import _load, _serve

BROWSER = find_browser()
pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(BROWSER is None, reason="no Chromium-family browser installed"),
]
BANNER = "SHADOW MODE — READ-ONLY — NO ORDERS WILL BE PLACED"
SATURDAY = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


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


# The page's requests are recorded (fetch is a page global used by every module) to prove the
# scheduler POST endpoint is never called from the browser.
RECORD_FETCH = (
    "window.__requests = []; const realFetch = window.fetch;"
    " window.fetch = (url, opts = {}) => { window.__requests.push([(opts.method || 'GET'),"
    " String(url)]); return realFetch(url, opts); };"
)


def test_shadow_research_scheduler_is_read_only_and_follows_the_backend(browser, tmp_path):
    """B7.2: the one-click enable control was removed; the page shows the backend's state only.
    Enabling happens outside the UI (here directly on the temporary journal, as a separate
    approval would)."""
    with _serve(tmp_path, fake=make_fake()) as (url, app):
        app.shadow._create_window("EURUSD", "H1", SATURDAY, SATURDAY)
        _load(browser, url, "analytics")
        browser.evaluate(RECORD_FETCH)
        browser.wait_for("document.querySelector('#shadow-state') !== null", 15)
        assert _text(browser, ".page-heading h1") == "Shadow Research"
        assert _text(browser, "#shadow-banner") == BANNER
        assert "Disabled" in _text(browser, "#shadow-state")
        assert browser.evaluate("document.querySelector('#shadow-toggle')") is None
        assert "requires separate approval" in _text(browser, "#shadow-scheduler-readonly")
        assert _text(browser, "#shadow-executable").endswith("Not available")
        assert _text(browser, "#shadow-trades").endswith("Not available")
        sync = _text(browser, "#shadow-history-sync")  # P6.1.1 history-sync diagnostics
        assert sync.startswith("MT5 history sync: 0 retries in 0 window(s) · recovered 0")
        assert sync.endswith("not an extra opportunity.")
        labels = browser.evaluate(
            "[...document.querySelectorAll('main button')].map(b => b.textContent.trim())"
        )
        assert sorted(labels) == ["Refresh"]  # no scheduler and no execution controls
        app.shadow.set_enabled(True)  # outside the UI
        browser.click("#shadow-refresh")
        browser.wait_for(
            "document.querySelector('#shadow-state')?.textContent.includes('Enabled')", 15
        )
        assert "running" in _text(browser, "#shadow-state")
        assert browser.evaluate("document.querySelector('#shadow-toggle')") is None
        # leave the page: the backend scheduler keeps running without it
        browser.click('#primary-nav [data-page="markets"]')
        browser.wait_for("location.hash === '#markets'")
        assert app.shadow.status()["state"] == "running"
        posts = browser.evaluate("window.__requests.filter(([m, u]) => m !== 'GET')")
        assert posts == []  # in particular no POST /api/shadow/scheduler
        _load(browser, url, "analytics")  # full reload: state comes back from the journal
        browser.wait_for(
            "document.querySelector('#shadow-state')?.textContent.includes('Enabled')", 15
        )
        assert "SKIPPED" in _text(browser, "#shadow-cycles").upper()
        app.shadow.set_enabled(False)
        assert app.shadow.enabled() is False


def test_analytics_page_explains_a_refused_journal(browser, tmp_path) -> None:
    (tmp_path / "trading_journal.sqlite3").write_bytes(b"corrupt" * 100)
    (tmp_path / "trading_journal.key").write_bytes(b"x" * 32)
    with _serve(tmp_path, fake=make_fake()) as (url, app):
        _load(browser, url, "analytics")
        browser.wait_for("document.querySelector('#shadow-state') !== null", 15)
        assert "blocked" in _text(browser, "#shadow-state")
        assert browser.evaluate("document.querySelector('#shadow-toggle')") is None
