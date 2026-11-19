"""B7.2 application shell in a real browser: grouped navigation and redirects, the operational
status band (actual backend values), Overview summaries without new MT5 reads, the read-only
System Control Centre, safety corrections and responsive layouts. Temporary data only."""

from __future__ import annotations

import json
import time
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from browser_harness import Browser, find_browser
from risk_fixtures import make_fake
from test_browser_smoke import _load, _serve

from johanna import journal_store as js

BROWSER = find_browser()
pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(BROWSER is None, reason="no Chromium-family browser installed"),
]
SECTIONS = [
    "overview",
    "market",
    "decisions",
    "journal",
    "positions",
    "evidence",
    "control",
    "settings",
]
RECORD_FETCH = (
    "window.__requests = []; const realFetch = window.fetch;"
    " window.fetch = (url, opts = {}) => { window.__requests.push([(opts.method || 'GET'),"
    " String(url), opts.body ?? null]); return realFetch(url, opts); };"
)
READY = (
    "document.querySelector('[data-ops-field=\"scheduler\"]')?.dataset.opsState !== 'NOT CHECKED'"
)


@pytest.fixture(scope="module")
def browser():
    instance = Browser(BROWSER)
    instance.set_viewport(1600, 1000)
    yield instance
    instance.close()


def _text(page, selector: str) -> str:
    return page.evaluate(
        f"document.querySelector({json.dumps(selector)})?.textContent"
        ".replace(/\\s+/g, ' ').trim() ?? ''"
    )


def _chip(page, field: str, scope: str = "main") -> str:
    return page.evaluate(
        f"document.querySelector('{scope} [data-ops-field=\"{field}\"]')?.dataset.opsState ?? null"
    )


# ====================================================================== navigation
def test_eight_sections_with_page_items_and_redirects(browser, tmp_path) -> None:
    with _serve(tmp_path, fake=make_fake()) as (url, _):
        _load(browser, url)
        sections = browser.evaluate(
            "[...document.querySelectorAll('#primary-nav [data-nav-section]')]"
            ".map(e => e.dataset.navSection)"
        )
        assert sections == SECTIONS
        items = browser.evaluate(
            "Object.fromEntries([...document.querySelectorAll('#primary-nav [data-nav-section]')]"
            ".map(s => [s.dataset.navSection,"
            " [...s.querySelectorAll('[data-page]')].map(b => b.dataset.page)]))"
        )
        assert items["market"] == ["charts", "markets", "calendar"]
        assert items["decisions"] == ["risk", "signals", "analytics"]
        assert items["positions"] == ["positions", "account"]
        nav_text = _text(browser, "#primary-nav")
        assert "Trading Rules" not in nav_text and "News" not in nav_text
        # saved links: old ids still work; removed / section ids are redirected
        for raw, target, heading in [
            ("rules", "control", "System Control Centre"),
            ("news", "calendar", "Economic Calendar"),
            ("journal", "history", "Trade Journal"),
            ("decisions", "risk", "Risk Management"),
            ("signals", "signals", "Decisions"),
            ("account", "account", "Account & Broker"),
        ]:
            _load(browser, url, raw)
            browser.wait_for(
                f"document.querySelector('.page-heading h1')?.innerText === {heading!r}"
            )
            assert browser.evaluate("location.hash") == f"#{target}"
            assert (
                browser.evaluate(
                    f"document.querySelector('#primary-nav [data-page=\"{target}\"]')"
                    ".getAttribute('aria-current')"
                )
                == "page"
            )
        assert _text(browser, ".page-heading .eyebrow") == "Positions & Reconciliation"


# ====================================================================== status band
def test_status_band_shows_actual_backend_values(browser, tmp_path) -> None:
    with _serve(tmp_path, fake=make_fake()) as (url, app):
        _load(browser, url)
        browser.wait_for(READY, 15)
        assert _chip(browser, "execution") == "DISABLED"
        assert _chip(browser, "unattended") == "NOT PERMITTED"
        assert _chip(browser, "scheduler") == "OFF"
        assert _chip(browser, "costs") == "NOT CONFIGURED"
        assert _chip(browser, "journal") == "READY"
        assert _chip(browser, "schema") == f"v{js.MIGRATIONS[-1][0]}"
        assert _chip(browser, "application") in ("ONLINE", "DEGRADED")
        assert _chip(browser, "market") in ("CURRENT", "STALE", "CACHED", "UNAVAILABLE", "LOADING")
        assert "readiness checked" in _text(browser, "#status-band-freshness")
        # not hard-coded: a backend change (outside the UI) is reflected after Refresh status
        app.shadow.set_enabled(True)
        browser.click("[data-ops-refresh]")
        browser.wait_for(
            "document.querySelector('[data-ops-field=\"scheduler\"]')?.dataset.opsState === 'ON'",
            15,
        )
        app.shadow.set_enabled(False)
        browser.click("[data-ops-refresh]")
        browser.wait_for(
            "document.querySelector('[data-ops-field=\"scheduler\"]')?.dataset.opsState === 'OFF'",
            15,
        )
        # every cell links to the screen that explains it
        browser.click('[data-status-item="costs"]')
        browser.wait_for("location.hash === '#evidence'")


def test_unreachable_readiness_is_unavailable_never_positive(browser, tmp_path) -> None:
    with _serve(tmp_path, fake=make_fake()) as (url, app):
        app.journal_store._refuse("corrupt_database")  # readiness reports the journal refused
        _load(browser, url, "control")
        browser.wait_for(READY, 15)
        assert _chip(browser, "journal") == "ATTENTION"
        assert _chip(browser, "scheduler") == "UNAVAILABLE"  # checked, but not established
        assert _chip(browser, "unattended") == "NOT PERMITTED"
        assert _chip(browser, "execution") == "DISABLED"
        assert _text(browser, "#readiness-manual").endswith("ATTENTION")


# ====================================================================== overview
def test_overview_summaries_use_existing_data_and_no_new_mt5_reads(browser, tmp_path) -> None:
    fake = make_fake()
    with _serve(tmp_path, fake=fake) as (url, app):
        status, _ = app.trades.create_trade(
            {
                "request_id": str(uuid.uuid4()),
                "symbol": "EURUSDc",
                "direction": "BUY",
                "entry": {
                    "time_utc": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(),
                    "price": 1.1,
                    "volume": 0.1,
                },
            }
        )
        assert status == 201
        reads_before = app.account.reads
        _load(browser, url)
        browser.evaluate(RECORD_FETCH)
        browser.wait_for(READY, 15)
        browser.wait_for("document.querySelector('#overview-decisions-card') !== null", 15)
        assert "NOT LOADED" in _text(
            browser, "#overview-account"
        )  # account read only on its screen
        assert "NOT LOADED" in _text(
            browser, "#overview-journal"
        )  # trade list values via MT5 quotes
        assert "NOT CHECKED" in _text(browser, "#overview-risk")  # no risk evaluation from Overview
        notices = browser.evaluate(
            "[...document.querySelectorAll('[data-notice]')].map(e => e.dataset.notice)"
        )
        for code in (
            "unattended",
            "scheduler_disabled",
            "costs_unverified",
            "monitoring_not_activated",
            "notifications_not_activated",
            "backups_disabled",
        ):
            assert code in notices, code
        time.sleep(2.5)  # readiness and decisions are fetched on open / Refresh only, never polled
        requests = browser.evaluate("window.__requests")
        urls = [u for _, u, _ in requests]
        assert not any(u.startswith(("/api/account", "/api/risk", "/api/trades")) for u in urls)
        assert not any(
            u.startswith(("/api/operations/readiness", "/api/journal/decisions")) for u in urls
        )
        assert [m for m, _, _ in requests if m != "GET"] == []
        assert app.account.reads == reads_before  # Overview performed no MT5 account read
        # once the Trade Journal screen has loaded the list, Overview summarises it
        browser.click('#primary-nav [data-page="history"]')
        browser.wait_for("document.querySelector('#trades-table') !== null", 15)
        browser.click('#primary-nav [data-page="overview"]')
        browser.wait_for("document.querySelector('#overview-open') !== null", 15)
        assert _text(browser, "#overview-open").endswith("1")
        assert _text(browser, "#overview-closed").endswith("0")
        assert "No trade fully closed today" in _text(browser, "#overview-today")


# ====================================================================== control centre
def test_control_centre_is_read_only_with_accurate_states(browser, tmp_path) -> None:
    with _serve(tmp_path, fake=make_fake()) as (url, _):
        _load(browser, url, "control")
        browser.wait_for("document.querySelector('#readiness-checks') !== null", 15)
        for panel in (
            "#readiness-panel",
            "#service-panel",
            "#journal-panel",
            "#scheduler-panel",
            "#notifications-panel",
            "#backups-panel",
            "#restrictions-panel",
        ):
            assert browser.evaluate(f"document.querySelector('{panel}') !== null"), panel
        expected = {
            "scheduler": "OFF",
            "monitoring": "INACTIVE",
            "reconciliation": "INACTIVE",
            "notifications": "INACTIVE",
            "backups": "DISABLED",
            "evidence": "INACTIVE",
            "linking": "INACTIVE",
            "execution": "DISABLED",
            "costs": "NOT CONFIGURED",
        }
        for field, value in expected.items():
            assert _chip(browser, field, "#service-status") == value, field
        vocabulary = set(
            browser.evaluate(
                "[...document.querySelectorAll('#readiness-checks [data-ops-field=\"check\"]')]"
                ".map(e => e.dataset.opsState)"
            )
        )
        assert vocabulary and vocabulary <= {"OK", "ATTENTION", "UNAVAILABLE", "NOT CHECKED"}
        assert "COMPLETED" not in _text(browser, "#readiness-checks")
        assert "FAILED" not in _text(browser, "#readiness-checks")
        assert _text(browser, "#control-backup-api").endswith("NOT AVAILABLE")
        assert "requires separate approval" in _text(browser, "#scheduler-readonly")
        labels = browser.evaluate(
            "[...document.querySelectorAll('main button')].map(b => b.textContent.trim())"
        )
        for word in (
            "Enable",
            "Activate",
            "Approve",
            "Restore",
            "Confirm",
            "Acknowledge",
            "Deliver",
            "Backup now",
        ):
            assert not any(word.lower() in label.lower() for label in labels), word


def test_positions_and_evidence_show_inactive_capabilities(browser, tmp_path) -> None:
    with _serve(tmp_path, fake=make_fake()) as (url, _):
        _load(browser, url, "positions")
        browser.wait_for(
            "document.querySelector('#positions-monitoring [data-ops-state]')?.dataset.opsState"
            " === 'INACTIVE'",
            15,
        )
        assert "no broker observation is taken" in _text(browser, "#positions-monitoring")
        assert _text(browser, "#positions-links").count("INACTIVE") == 1
        _load(browser, url, "evidence")
        browser.wait_for(
            "document.querySelector('#evidence-costs [data-ops-state]')?.dataset.opsState"
            " === 'NOT CONFIGURED'",
            15,
        )
        assert "never treated as zero" in _text(browser, "#evidence-costs")
        assert "NOT AVAILABLE" in _text(browser, "#evidence-registry")


# ====================================================================== safety corrections
def test_settings_cannot_change_execution_or_operating_mode(browser, tmp_path) -> None:
    with _serve(tmp_path, fake=make_fake()) as (url, app):
        # a previously saved preference (through the application, as the old UI could save it)
        app.save_preferences({"autonomous_execution_enabled": True, "operating_mode": "autonomous"})
        _load(browser, url, "settings")
        browser.evaluate(RECORD_FETCH)
        assert browser.evaluate("document.querySelector('#execution-toggle')") is None
        assert browser.evaluate("document.querySelector('#mode-select')") is None
        assert "does not have authorisation to execute trades autonomously" in _text(
            browser, "#settings-autonomous-execution"
        )
        assert "has no effect" in _text(browser, "#settings-operating-mode")
        assert _text(browser, ".execution-status") == "Execution Disabled"  # never "on"
        assert _text(browser, "#mode-label") == "MANUAL"
        browser.select("#theme-select", "light")
        browser.wait_for("document.documentElement.dataset.theme === 'light'")
        bodies = [b or "" for m, _, b in browser.evaluate("window.__requests") if m == "POST"]
        assert bodies and all(
            "autonomous_execution_enabled" not in b and "operating_mode" not in b for b in bodies
        )
        prefs = app.store.get_preferences()
        assert (
            prefs["autonomous_execution_enabled"] is True
        )  # stored value preserved, never written
        assert app.status()["autonomous_execution_active"] is False


# ====================================================================== responsive
@pytest.mark.parametrize(
    ("width", "height", "mobile"), [(1600, 1000, False), (900, 900, False), (400, 900, True)]
)
def test_layouts_fit_without_horizontal_scroll(browser, tmp_path, width, height, mobile) -> None:
    with _serve(tmp_path, fake=make_fake()) as (url, _):
        browser.set_viewport(width, height, mobile=mobile)
        try:
            for page in ("overview", "control", "settings", "history", "risk"):
                _load(browser, url, page)
                if page in ("overview", "control"):
                    browser.wait_for(READY, 15)
                    box = browser.evaluate(
                        "JSON.stringify(document.querySelector('.status-band').getBoundingClientRect())"
                    )
                    assert json.loads(box)["width"] > 0
                overflow = browser.evaluate(
                    "document.documentElement.scrollWidth - window.innerWidth"
                )
                assert overflow <= 1, (page, width, overflow)
            if mobile:
                assert (
                    browser.evaluate(
                        "getComputedStyle(document.querySelector('#mobile-menu')).display"
                    )
                    != "none"
                )
                browser.click("#mobile-menu")
                browser.wait_for("document.querySelector('#sidebar').classList.contains('open')")
                browser.click('#primary-nav [data-page="control"]')
                browser.wait_for("location.hash === '#control'")
            else:
                assert (
                    browser.evaluate(
                        "document.querySelector('#primary-nav [data-page=\"markets\"] span')"
                        ".offsetWidth"
                    )
                    > 0
                )
        finally:
            browser.set_viewport(1600, 1000)
