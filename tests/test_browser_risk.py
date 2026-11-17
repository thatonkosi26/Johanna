"""Phase 4 dashboard: Risk Management page (proposed parameters only; synthetic data)."""

from __future__ import annotations

import time

import pytest
from browser_harness import Browser, find_browser
from fake_mt5 import READ_ONLY_CALLS
from risk_fixtures import BUY_LEVEL, VERIFIED_COSTS, StubAnalysis, envelope, make_fake
from test_browser_smoke import _load, _serve

BROWSER = find_browser()
pytestmark = [
    pytest.mark.browser,
    pytest.mark.skipif(BROWSER is None, reason="no Chromium-family browser installed"),
]
ROW = (
    "(() => {{ const e = document.querySelector("
    "'#risk-content [data-risk-field=\"{key}\"] strong');"
    " return (e?.textContent ?? '').replace(/\\s+/g, ' ').trim(); }})()"
)


@pytest.fixture(scope="module")
def browser():
    instance = Browser(BROWSER)
    yield instance
    instance.close()


@pytest.fixture
def page(browser):
    browser.set_viewport(1600, 1100)
    return browser


def _row(page, key: str) -> str:
    return page.evaluate(ROW.format(key=key))


def _open(page, url) -> None:
    _load(page, url, "risk")
    page.wait_for("document.querySelector('#risk-status-chip')?.dataset.riskStatus !== 'none'", 20)


def _serve_risk(tmp_path, fake, env, costs=None):
    """``costs=None`` keeps the production default (no verified cost terms)."""
    served = _serve(tmp_path, fake=fake)
    url, app = served.__enter__()
    app.risk.analysis = StubAnalysis(env)
    if costs is not None:
        app.risk.costs = costs  # synthetic verified terms (test-only)
    return served, url, app


def test_approved_proposal_renders_as_proposed_parameters(page, tmp_path) -> None:
    fake = make_fake()
    served, url, app = _serve_risk(tmp_path, fake, envelope(level=BUY_LEVEL), VERIFIED_COSTS)
    try:
        _open(page, url)
        assert page.evaluate("document.querySelector('#risk-status-chip').dataset.riskStatus") == (
            "APPROVED_FOR_MANUAL_REVIEW"
        )
        assert "approved for manual review" in page.text("#risk-heading-badge").lower()
        assert "has not placed and will not place" in page.text("#risk-banner")
        assert "NOT AN ORDER" in page.text("#risk-content").upper()
        assert _row(page, "direction") == "BUY"
        assert _row(page, "entry").startswith("1.23798 · ask side")
        assert _row(page, "stop_loss").startswith("1.23432")
        assert _row(page, "take_profit").startswith("1.246")
        assert _row(page, "gross_reward_risk").startswith("1 : 2.19 · spread included")
        assert "excludes commission, swap, slippage" in _row(page, "gross_reward_risk")
        assert "estimated" in _row(page, "net_reward_risk")
        assert page.evaluate("document.querySelector('#risk-net-unverified')") is None
        assert _row(page, "daily_reference").startswith("1000.00 USC · min(balance, equity)")
        assert _row(page, "volume") == "0.01 lots (includes transaction costs)"
        assert _row(page, "estimated_loss") == "3.66 USC · price 3.66 + costs 0.00"
        assert _row(page, "estimated_reward") == "8.02 / 8.02 USC"
        assert _row(page, "sizing_status") == "verified · includes applicable transaction costs"
        assert _row(page, "daily_limit").startswith("20.00 USC")
        assert _row(page, "daily_remaining") == "20.00 USC"
        assert "passed" in page.text("#risk-validations")
        assert "R7" in page.text("#risk-setup")
        assert set(fake.calls) <= READ_ONLY_CALLS
    finally:
        served.__exit__(None, None, None)


def test_rejected_proposal_shows_reasons(page, tmp_path) -> None:
    served, url, _ = _serve_risk(tmp_path, make_fake(), envelope())  # no TC6 level: no target
    try:
        _open(page, url)
        assert "rejected" in page.text("#risk-heading-badge").lower()
        assert "take-profit" in page.text("#risk-content")
        assert "No proposed parameters" in page.text("#risk-proposal")  # nothing constructed
    finally:
        served.__exit__(None, None, None)


def test_unverified_daily_risk_is_visible(page, tmp_path) -> None:
    fake = make_fake()
    fake.failing_reads.add("history_deals_get")
    served, url, _ = _serve_risk(tmp_path, fake, envelope(level=BUY_LEVEL))
    try:
        _open(page, url)
        assert "unverified" in page.text("#risk-heading-badge").lower()
        assert "BLOCKS APPROVAL" in page.text("#risk-content").upper()
        assert _row(page, "daily_status") == "unverified"
        assert _row(page, "daily_remaining") == "Unverified"  # never shown as 0
    finally:
        served.__exit__(None, None, None)


def test_no_action_controls_and_no_execution_wording(page, tmp_path) -> None:
    served, url, _ = _serve_risk(tmp_path, make_fake(), envelope(level=BUY_LEVEL))
    try:
        _open(page, url)
        count = page.evaluate(
            "document.querySelectorAll('main button, main input, main select, main form').length"
        )
        assert count == 0
        text = page.text("main")
        for word in ("Place order", "Execute", "Submit order", "Order placed", "Executed"):
            assert word not in text
    finally:
        served.__exit__(None, None, None)


def test_stale_proposal_is_marked_not_current(page, tmp_path) -> None:
    served, url, _ = _serve_risk(tmp_path, make_fake(), envelope(level=BUY_LEVEL))
    try:
        _open(page, url)
        page.evaluate("livePolling.risk.stop(); state.risk.receivedAt = 1")
        page.wait_for("document.querySelector('#risk-stale') !== null", 5)
        assert "Do not use these parameters" in page.text("#risk-stale")
    finally:
        served.__exit__(None, None, None)


def test_risk_polling_only_on_its_page(page, tmp_path) -> None:
    served, url, app = _serve_risk(tmp_path, make_fake(), envelope(level=BUY_LEVEL))
    try:
        _load(page, url, "markets")
        time.sleep(1.5)
        assert app.risk.evaluations == 0
        page.click('#primary-nav [data-page="risk"]')
        page.wait_for("document.querySelector('#risk-proposal') !== null", 20)
        assert app.risk.evaluations >= 1
        assert page.evaluate("livePollingState().risk.scheduled") is True
        page.click('#primary-nav [data-page="markets"]')
        page.wait_for("!livePollingState().risk.inFlight")
        assert page.evaluate("livePollingState().risk.scheduled") is False
        before = app.risk.evaluations
        time.sleep(1.5)
        assert app.risk.evaluations == before
    finally:
        served.__exit__(None, None, None)


def test_unsupported_timeframe_requests_nothing(page, tmp_path) -> None:
    served, url, app = _serve_risk(tmp_path, make_fake(), envelope(level=BUY_LEVEL))
    try:
        app.save_preferences({"selected_chart_timeframe": "M15"})
        _load(page, url, "risk")
        page.wait_for("document.querySelector('#risk-content').textContent.includes('M15')", 10)
        time.sleep(1)
        assert app.risk.evaluations == 0
    finally:
        served.__exit__(None, None, None)


def test_unverified_net_costs_are_prominent_and_never_approved(page, tmp_path) -> None:
    served, url, _ = _serve_risk(tmp_path, make_fake(), envelope(level=BUY_LEVEL))  # production
    try:
        _open(page, url)
        assert "unverified" in page.text("#risk-heading-badge").lower()
        banner = page.text("#risk-net-unverified")
        assert "NOT established" in banner and "gross ratio alone" in banner
        assert _row(page, "net_reward_risk") == "Unverified · costs not established"
        assert _row(page, "gross_reward_risk").startswith("1 : 2.19")
        assert _row(page, "cost_commission").startswith("unverified")
        assert _row(page, "cost_slippage").startswith("unmodelled")
        assert "Costs (net 1:2)" in page.text("#risk-validations")
        assert "approved" not in page.text("#risk-heading-badge").lower()
        assert "slippage is unmodelled" in banner and "0.5% limit" in banner
        assert _row(page, "volume").startswith("No actionable volume")
        assert "UNVERIFIED, price risk only" in _row(page, "volume")
        assert _row(page, "sizing_status").startswith("unverified")
        assert _row(page, "estimated_loss") == "Not established (costs unverified)"
        assert _row(page, "costs_per_lot") == "unverified / unverified / unverified"
    finally:
        served.__exit__(None, None, None)
