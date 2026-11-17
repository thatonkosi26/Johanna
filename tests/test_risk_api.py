"""Phase 4 API: GET /api/risk/proposal (read-only, synthetic FakeMT5 data)."""

from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO

import pytest
from fake_mt5 import READ_ONLY_CALLS
from risk_fixtures import BUY_LEVEL, VERIFIED_COSTS, StubAnalysis, envelope, make_fake

from johanna.adapters.mt5 import MetaTrader5Adapter
from johanna.api import BackendAPI
from johanna.application import JohannaApplication
from johanna.config import AppConfig, StorageConfig
from johanna.live_market import LiveMarketService
from johanna.risk_engine import APPROVED, RISK_POLICY, UNVERIFIED


def _api(tmp_path, fake=None, env=None, costs=VERIFIED_COSTS):
    fake = fake if fake is not None else make_fake()
    config = replace(AppConfig.load(environ={}), storage=StorageConfig(tmp_path))
    live = LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake),
        terminal_running=lambda: fake.running,
        reconnect_interval=0,
    )
    app = JohannaApplication(config, live_market=live)
    stub = StubAnalysis(env if env is not None else envelope(level=BUY_LEVEL))
    app.risk.analysis = stub
    app.risk.costs = costs  # synthetic verified terms unless a test passes others
    return fake, stub, app, BackendAPI(app)


def _call(api, path, query="", method="GET"):
    response: dict = {}
    raw = b"".join(
        api(
            {
                "PATH_INFO": path,
                "REQUEST_METHOD": method,
                "QUERY_STRING": query,
                "wsgi.input": BytesIO(),
            },
            lambda status, headers: response.update(status=status, headers=dict(headers)),
        )
    )
    text = raw.decode("utf-8")
    return response["status"], json.loads(text), text, response["headers"]


def test_proposal_response(tmp_path) -> None:
    fake, stub, app, api = _api(tmp_path)
    status, body, text, headers = _call(api, "/api/risk/proposal", "symbol=eurusd&timeframe=h1")
    assert status == "200 OK" and headers["Cache-Control"] == "no-store"
    assert body["status"] == APPROVED and body["symbol"] == "EURUSD"
    assert body["proposal"]["label"] == "Proposed trade parameters (not an order)"
    assert stub.calls == [("EURUSD", "H1")]  # exactly one T6 evaluation
    assert "NaN" not in text and "Infinity" not in text
    assert set(fake.calls) <= READ_ONLY_CALLS
    assert app.risk.evaluations == 1


@pytest.mark.parametrize(
    "query",
    [
        "symbol=EURUSD&timeframe=M15",
        "symbol=EURGBP&timeframe=H1",
        "symbol=%3Cx%3E&timeframe=H1",
        "symbol=&timeframe=H1",
        "symbol=EURGBPc&timeframe=H1",
        "symbol=XAUUSD&timeframe=H1",
    ],
)
def test_invalid_requests_are_400_without_mt5(tmp_path, query) -> None:
    fake, stub, _, api = _api(tmp_path)
    status, body, _, _ = _call(api, "/api/risk/proposal", query)
    assert status == "400 Bad Request" and body["error"] == "invalid_risk_request"
    assert fake.calls == [] and stub.calls == []


def test_risk_policy_cannot_be_set_through_the_api(tmp_path) -> None:
    _, _, _, api = _api(tmp_path)
    query = (
        "symbol=EURUSD&timeframe=H1&max_risk_per_trade=0.05&max_daily_risk=0.5&min_reward_risk=1"
    )
    _, body, _, _ = _call(api, "/api/risk/proposal", query)
    assert body["policy"]["max_risk_per_trade"] == RISK_POLICY.max_risk_per_trade == 0.005
    assert body["policy"]["max_daily_risk"] == 0.02 and body["policy"]["min_reward_risk"] == 2.0


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_proposal_endpoint_is_get_only(tmp_path, method) -> None:
    fake, stub, _, api = _api(tmp_path)
    status, _, _, headers = _call(api, "/api/risk/proposal", "symbol=EURUSD&timeframe=H1", method)
    assert status == "405 Method Not Allowed" and headers["Allow"] == "GET"
    assert fake.calls == [] and stub.calls == []


@pytest.mark.parametrize(
    "path", ["/api/risk/order", "/api/risk/execute", "/api/order", "/api/trade"]
)
def test_no_execution_endpoints_exist(tmp_path, path) -> None:
    _, _, _, api = _api(tmp_path)
    for method in ("GET", "POST"):
        assert _call(api, path, method=method)[0] == "404 Not Found"


def test_mt5_disconnected_is_unverified(tmp_path) -> None:
    fake, _, _, api = _api(tmp_path)
    fake.running = False
    status, body, _, _ = _call(api, "/api/risk/proposal", "symbol=EURUSD&timeframe=H1")
    assert status == "200 OK" and body["status"] == UNVERIFIED
    assert body["proposal"] is None and body["daily_risk"]["status"] == "unverified"


def test_no_sensitive_information_in_the_response(tmp_path) -> None:
    fake = make_fake()
    fake.positions = [
        {
            "symbol": "EURUSDc",
            "type": 0,
            "volume": 0.01,
            "price_open": 1.24,
            "sl": 1.235,
            "tp": 0.0,
            "price_current": 1.2379,
            "profit": 0.0,
            "swap": 0.0,
            "time": 0,
        }
    ]
    _, _, _, api = _api(tmp_path, fake)
    _, _, text, _ = _call(api, "/api/risk/proposal", "symbol=EURUSD&timeframe=H1")
    for secret in ("12345678", "Private Person", "900001", "private", '"login"', '"ticket"'):
        assert secret not in text


def test_existing_responses_are_unchanged(tmp_path) -> None:
    _, _, _, api = _api(tmp_path)
    _, summary, _, _ = _call(api, "/api/account/summary")
    assert set(summary) == {
        "status",
        "requested_at_utc",
        "retrieved_at_utc",
        "age_seconds",
        "served_from_cache",
        "atomic",
        "reason",
        "message",
        "provenance",
        "validation",
        "account",
        "checks",
    }
    _, quotes, _, _ = _call(api, "/api/market/quotes", "symbols=EURUSD")
    assert set(quotes) == {"status", "requested_at_utc", "connection", "quotes"}
