"""P5.1 journal API (read-only) and the Decisions page. Temporary directories only."""

from __future__ import annotations

import json
import time

import pytest
from browser_harness import Browser, find_browser
from risk_fixtures import BUY_LEVEL, PRODUCTION_COSTS, envelope, make_fake, propose
from test_browser_smoke import _load, _serve
from test_decision_journal import _app, _call

from johanna.api import BackendAPI

BROWSER = find_browser()


def _seeded(tmp_path):
    app = _app(tmp_path)
    app.start()
    env = envelope(level=BUY_LEVEL)
    decision = app.decisions.capture_analysis(env, "risk_request")
    app.decisions.capture_risk(propose(env=env, costs=PRODUCTION_COSTS), decision)
    return app, decision


def test_list_and_detail(tmp_path) -> None:
    app, decision = _seeded(tmp_path)
    try:
        api = BackendAPI(app)
        status, body, headers = _call(api, "/api/journal/decisions")
        assert status == "200 OK" and headers["Cache-Control"] == "no-store"
        assert body["coverage"]["mode"] == "request_time"
        assert body["journal"]["state"] == "ready" and body["journal"]["hmac_available"] is True
        assert body["journal"]["hmac_prerequisite"] == "verified"
        assert body["journal"]["monitoring_and_reconciliation"] == "not_implemented"
        assert "available" not in json.dumps(
            {k: v for k, v in body["journal"].items() if k == "monitoring_and_reconciliation"}
        )
        item = body["decisions"][0]
        assert (
            item["analysis_outcome"],
            item["risk_status"],
            item["trade_eligibility"],
            item["execution_status"],
        ) == ("TRADE_CANDIDATE", "UNVERIFIED", "NOT_APPROVED", "NONE")
        status, detail, _ = _call(api, f"/api/journal/decisions/{decision}")
        assert status == "200 OK" and detail["decision"]["id"] == decision
        assert detail["decision"]["risk_assessments"][0]["risk_status"] == "UNVERIFIED"
    finally:
        app.stop()


@pytest.mark.parametrize(
    "query",
    [
        "outcome=APPROVED",
        "eligibility=YES",
        "execution=DONE",
        "timeframe=H2",
        "symbol=%3Cx%3E",
        "limit=0",
        "limit=101",
        "cursor=abc",
        "cursor=0",
    ],
)
def test_invalid_filters_are_400(tmp_path, query) -> None:
    app, _ = _seeded(tmp_path)
    try:
        status, body, _ = _call(BackendAPI(app), "/api/journal/decisions", query)
        assert status == "400 Bad Request" and body["error"] == "invalid_journal_request"
    finally:
        app.stop()


def test_filters_and_pagination(tmp_path) -> None:
    app, _ = _seeded(tmp_path)
    try:
        api = BackendAPI(app)
        for outcome in ("MARKET_CONDITION", "NO_TRADE"):
            env = envelope(level=BUY_LEVEL)
            env["result"]["decision"].update(
                outcome=outcome, rule="R6" if outcome != "NO_TRADE" else "R1"
            )
            app.decisions.capture_analysis(env)
        _, page1, _ = _call(api, "/api/journal/decisions", "limit=2")
        assert len(page1["decisions"]) == 2 and page1["next_cursor"]
        _, page2, _ = _call(api, "/api/journal/decisions", f"limit=2&cursor={page1['next_cursor']}")
        assert len(page2["decisions"]) == 1 and page2["next_cursor"] is None
        _, only, _ = _call(api, "/api/journal/decisions", "outcome=TRADE_CANDIDATE")
        assert [d["analysis_outcome"] for d in only["decisions"]] == ["TRADE_CANDIDATE"]
        _, none, _ = _call(api, "/api/journal/decisions", "execution=BROKER_VERIFIED")
        assert none["decisions"] == []
    finally:
        app.stop()


def test_unknown_and_malformed_ids_are_404(tmp_path) -> None:
    app, _ = _seeded(tmp_path)
    try:
        api = BackendAPI(app)
        assert _call(api, "/api/journal/decisions/01JZZZZZZZZZZZZZZZZZZZZZZZ")[0] == "404 Not Found"
        assert _call(api, "/api/journal/decisions/not-an-id")[0] == "404 Not Found"
    finally:
        app.stop()


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "path", ["/api/journal/decisions", "/api/journal/decisions/01JZZZZZZZZZZZZZZZZZZZZZZZ"]
)
def test_journal_endpoints_are_get_only(tmp_path, method, path) -> None:
    app, _ = _seeded(tmp_path)
    try:
        response: dict = {}
        from io import BytesIO

        BackendAPI(app)(
            {
                "PATH_INFO": path,
                "REQUEST_METHOD": method,
                "QUERY_STRING": "",
                "wsgi.input": BytesIO(),
            },
            lambda status, headers: response.update(status=status, headers=dict(headers)),
        )
        assert (
            response["status"] == "405 Method Not Allowed" and response["headers"]["Allow"] == "GET"
        )
    finally:
        app.stop()


def test_refused_journal_is_503_without_secrets(tmp_path) -> None:
    app = _app(tmp_path)
    app.start()
    key = (tmp_path / "trading_journal.key").read_bytes()
    app.stop()
    (tmp_path / "trading_journal.sqlite3").write_bytes(b"corrupt" * 100)
    app2 = _app(tmp_path)
    app2.start()
    try:
        status, body, _ = _call(BackendAPI(app2), "/api/journal/decisions")
        assert status == "503 Service Unavailable" and body["reason"] == "corrupt_database"
        assert key.hex() not in json.dumps(body)
        assert app2.health()[0] is True  # the rest of Johanna keeps running
    finally:
        app2.stop()


def test_responses_contain_no_identifiers_or_key(tmp_path) -> None:
    app, decision = _seeded(tmp_path)
    try:
        api = BackendAPI(app)
        key = (tmp_path / "trading_journal.key").read_bytes().hex()
        texts = [
            json.dumps(_call(api, "/api/journal/decisions")[1]),
            json.dumps(_call(api, f"/api/journal/decisions/{decision}")[1]),
        ]
        for text in texts:
            for secret in (key, '"login"', '"ticket"', "12345678", "Private Person", "key_check"):
                assert secret not in text
    finally:
        app.stop()


# ---------------------------------------------------------------- dashboard
browser_only = [
    pytest.mark.browser,
    pytest.mark.skipif(BROWSER is None, reason="no Chromium-family browser installed"),
]


@pytest.fixture(scope="module")
def browser():
    if BROWSER is None:
        pytest.skip("no browser")
    instance = Browser(BROWSER)
    yield instance
    instance.close()


@pytest.mark.browser
@pytest.mark.skipif(BROWSER is None, reason="no Chromium-family browser installed")
def test_decisions_page_shows_separate_dimensions(browser, tmp_path) -> None:
    browser.set_viewport(1500, 1000)
    with _serve(tmp_path, fake=make_fake()) as (url, app):
        env = envelope(level=BUY_LEVEL)
        decision = app.decisions.capture_analysis(env, "risk_request")
        app.decisions.capture_risk(propose(env=env, costs=PRODUCTION_COSTS), decision)
        _load(browser, url, "signals")
        browser.wait_for("document.querySelector('#decisions-table') !== null", 15)
        assert (
            browser.evaluate("document.querySelector('.page-heading h1').innerText") == "Decisions"
        )
        assert "Decisions" in browser.text('#primary-nav [data-page="signals"]')
        row = "#decisions-table tbody tr"
        cell = lambda col: browser.evaluate(  # noqa: E731
            f"document.querySelector('{row} [data-col=\"{col}\"]').textContent.trim()"
        )
        assert cell("outcome") == "TRADE CANDIDATE · R7"
        assert cell("direction") == "BUY"
        assert cell("risk").upper() == "UNVERIFIED"
        assert cell("eligibility").upper() == "NOT APPROVED"
        assert cell("execution").upper() == "NONE"
        assert "not continuous market coverage" in browser.text("#decisions-coverage")
        journal_line = browser.text("#decisions-journal")
        assert "HMAC prerequisite verified" in journal_line
        assert "monitoring and reconciliation not implemented" in journal_line
        assert "available" not in journal_line
        count = browser.evaluate(
            "document.querySelectorAll('main button, main input, main select, main form').length"
        )
        assert count == 0
        assert browser.evaluate("livePollingState().decisions.scheduled") is True
        browser.click('#primary-nav [data-page="markets"]')
        browser.wait_for("!livePollingState().decisions.inFlight")
        assert browser.evaluate("livePollingState().decisions.scheduled") is False
        time.sleep(0.2)
