"""Phase 3 API: GET /api/account/summary, /api/account/instruments[/{symbol}] (read-only)."""

from __future__ import annotations

import json
from dataclasses import replace
from io import BytesIO

import pytest
from fake_mt5 import READ_ONLY_CALLS, FakeMT5
from test_account_intelligence import SECRET_LOGIN, SECRET_NAME, cent_symbols

from johanna.adapters.mt5 import MetaTrader5Adapter
from johanna.api import BackendAPI
from johanna.application import JohannaApplication
from johanna.config import AppConfig, StorageConfig
from johanna.live_market import LiveMarketService


def _api(tmp_path, fake=None):
    fake = fake if fake is not None else FakeMT5(symbols=cent_symbols())
    config = replace(AppConfig.load(environ={}), storage=StorageConfig(tmp_path))
    live = LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake),
        terminal_running=lambda: fake.running,
        reconnect_interval=0,
    )
    return fake, BackendAPI(JohannaApplication(config, live_market=live))


def _call(api, path, method="GET", query=""):
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


def test_summary_response(tmp_path) -> None:
    fake, api = _api(tmp_path)
    status, body, text, headers = _call(api, "/api/account/summary")
    assert status == "200 OK"
    assert headers["Cache-Control"] == "no-store"
    assert body["status"] == "LIVE" and body["validation"] == "verified"
    assert body["account"]["currency"]["value"] == "USC"
    assert body["provenance"]["status"] == "established"
    assert body["retrieved_at_utc"].endswith("Z")
    assert "NaN" not in text and "Infinity" not in text
    assert set(fake.calls) <= READ_ONLY_CALLS


def test_instruments_response(tmp_path) -> None:
    _, api = _api(tmp_path)
    status, body, _, _ = _call(api, "/api/account/instruments")
    assert status == "200 OK"
    assert [i["broker_symbol"] for i in body["instruments"]] == ["EURUSDc", "GBPUSDc", "USDJPYc"]
    assert body["account_currency"]["value"] == "USC"
    for item in body["instruments"]:
        assert item["validation"] == "verified"
        assert item["retrieved_at_utc"].endswith("Z")


@pytest.mark.parametrize("symbol", ["USDJPY", "usdjpy", "USDJPYc"])
def test_single_instrument_response(tmp_path, symbol) -> None:
    _, api = _api(tmp_path)
    status, body, _, _ = _call(api, f"/api/account/instruments/{symbol}")
    assert status == "200 OK"
    assert body["instrument"]["symbol"] == "USDJPY"
    assert body["instrument"]["broker_symbol"] == "USDJPYc"
    assert "instruments" not in body


@pytest.mark.parametrize("symbol", ["EURGBP", "XAUUSD", "EURUSDm", "a", "..", "EUR%2FUSD"])
def test_unsupported_instrument_is_404(tmp_path, symbol) -> None:
    _, api = _api(tmp_path)
    status, body, _, _ = _call(api, f"/api/account/instruments/{symbol}")
    assert status == "404 Not Found" and body["error"] == "unsupported_symbol"


def test_mt5_disconnected_is_503_with_reason(tmp_path) -> None:
    fake, api = _api(tmp_path)
    fake.running = False
    for path in (
        "/api/account/summary",
        "/api/account/instruments",
        "/api/account/instruments/EURUSD",
    ):
        status, body, _, _ = _call(api, path)
        assert status == "503 Service Unavailable"
        assert body["error"] == "account_unavailable"
        assert body["status"] == "UNAVAILABLE" and body["reason"] == "terminal_not_running"


def test_invalid_data_response(tmp_path) -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.account["balance"] = float("nan")
    fake.symbols["GBPUSDc"]["info"] = {"volume_step": 0.0}
    _, api = _api(tmp_path, fake)
    status, body, text, _ = _call(api, "/api/account/summary")
    assert status == "200 OK"
    assert body["validation"] == "invalid"
    assert body["account"]["balance"] == {
        "value": None,
        "status": "invalid",
        "source": "account_info.balance",
        "reason": "not a finite number (nan)",
        "unit": "USC",
    }
    assert "NaN" not in text
    _, body, _, _ = _call(api, "/api/account/instruments/GBPUSD")
    assert body["instrument"]["validation"] == "invalid"


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "path", ["/api/account/summary", "/api/account/instruments", "/api/account/instruments/EURUSD"]
)
def test_account_endpoints_are_get_only(tmp_path, method, path) -> None:
    fake, api = _api(tmp_path)
    status, body, _, headers = _call(api, path, method=method)
    assert status == "405 Method Not Allowed" and headers["Allow"] == "GET"
    assert fake.calls == []


def test_no_sensitive_account_information_in_any_response(tmp_path) -> None:
    fake, api = _api(tmp_path)
    texts = [
        _call(api, path)[2]
        for path in (
            "/api/account/summary",
            "/api/account/instruments",
            "/api/account/instruments/EURUSD",
            "/status",
            "/api/dashboard",
            "/api/market/quotes",
        )
    ]
    for text in texts:
        assert str(SECRET_LOGIN) not in text and SECRET_NAME not in text
        assert '"login"' not in text


def test_existing_market_responses_are_unchanged(tmp_path) -> None:
    fake, api = _api(tmp_path, FakeMT5())
    _, quotes, _, _ = _call(api, "/api/market/quotes", query="symbols=EURUSD")
    assert set(quotes) == {"status", "requested_at_utc", "connection", "quotes"}
    assert set(quotes["quotes"][0]) == {
        "symbol",
        "broker_symbol",
        "status",
        "bid",
        "ask",
        "spread_pips",
        "digits",
        "point",
        "pip_size",
        "quote_time_utc",
        "quote_age_seconds",
        "fetched_at_utc",
        "reason",
    }
    _, bars, _, _ = _call(api, "/api/market/bars", query="symbol=EURUSD&timeframe=H1&count=20")
    assert set(bars) == {
        "symbol",
        "broker_symbol",
        "timeframe",
        "digits",
        "point",
        "pip_size",
        "bars",
        "rejected_bars",
        "quality",
        "latest_tick_utc",
        "tick",
        "retrieved_at_utc",
        "source",
        "time_basis",
        "status",
        "fetched_at_utc",
        "requested_at_utc",
        "served_from_cache",
        "reason",
        "message",
    }
    for text in (json.dumps(quotes), json.dumps(bars)):
        for phase3_key in ('"account_currency"', '"provenance"', '"validation"', '"balance"'):
            assert phase3_key not in text
