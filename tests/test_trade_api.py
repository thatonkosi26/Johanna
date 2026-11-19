"""P5.2 trade logbook API. Temporary directories only; MT5 is the in-process fake."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from risk_fixtures import BUY_LEVEL, envelope
from test_decision_journal import _app, _call

from johanna.api import BackendAPI

TRADE = "01JZZZZZZZZZZZZZZZZZZZZZZZ"


def _when(minutes: int = 30) -> str:
    return (datetime.now(UTC) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _post(
    api, path, body, *, raw: bytes | None = None, content_type="application/json", headers=None
):
    data = raw if raw is not None else json.dumps(body).encode()
    response: dict = {}
    environ = {
        "PATH_INFO": path,
        "REQUEST_METHOD": "POST",
        "QUERY_STRING": "",
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(data)),
        "wsgi.input": BytesIO(data),
        **(headers or {}),
    }
    out = b"".join(api(environ, lambda s, h: response.update(status=s, headers=dict(h))))
    return response["status"], json.loads(out), response["headers"]


def _method(api, method, path):
    response: dict = {}
    api(
        {"PATH_INFO": path, "REQUEST_METHOD": method, "QUERY_STRING": "", "wsgi.input": BytesIO()},
        lambda s, h: response.update(status=s, headers=dict(h)),
    )
    return response["status"], response["headers"]


def _trade_body(**extra) -> dict:
    return {
        "request_id": str(uuid.uuid4()),
        "symbol": "EURUSDc",
        "direction": "BUY",
        "entry": {"time_utc": _when(60), "price": 1.1, "volume": 0.1},
        **extra,
    }


@pytest.fixture
def api(tmp_path):
    app = _app(tmp_path)
    app.start()
    try:
        yield BackendAPI(app)
    finally:
        app.stop()


def _create(api, **extra) -> str:
    status, body, _ = _post(api, "/api/trades", _trade_body(**extra))
    assert status == "201 Created", body
    return body["trade_id"]


def test_create_list_and_detail(api) -> None:
    trade_id = _create(api)
    status, page, headers = _call(api, "/api/trades")
    assert status == "200 OK" and headers["Cache-Control"] == "no-store"
    assert page["banner"].startswith("Record trades you have already executed in MT5.")
    assert [t["id"] for t in page["trades"]] == [trade_id]
    assert page["journal"]["state"] == "ready"
    status, detail, headers = _call(api, f"/api/trades/{trade_id}")
    assert status == "200 OK" and headers["Cache-Control"] == "no-store"
    trade = detail["trade"]
    assert (
        trade["verification"] == "MANUAL_UNVERIFIED" and trade["protection"]["state"] == "MISSING"
    )


def test_every_write_route(api) -> None:
    app = api.application
    decision = app.decisions.capture_analysis(envelope(level=BUY_LEVEL), "analysis_request")
    trade_id = _create(api, stop_loss=1.09)
    rid = lambda: str(uuid.uuid4())  # noqa: E731
    base = f"/api/trades/{trade_id}"
    s, fill, _ = _post(
        api,
        f"{base}/fills",
        {
            "request_id": rid(),
            "kind": "exit",
            "price": 1.101,
            "volume": 0.05,
            "time_utc": _when(30),
        },
    )
    assert s == "201 Created"
    s, voided, _ = _post(
        api,
        f"{base}/fills/{fill['fill_id']}/void",
        {
            "request_id": rid(),
            "reason": "typo",
            "replacement": {"price": 1.102, "volume": 0.05, "time_utc": _when(30)},
        },
    )
    assert s == "200 OK" and voided["replacement_fill_id"]
    s, _, _ = _post(
        api,
        f"{base}/protection",
        {"request_id": rid(), "stop_loss": 1.095, "take_profit": 1.12, "effective_utc": _when(20)},
    )
    assert s == "201 Created"
    assert _post(api, f"{base}/notes", {"request_id": rid(), "text": "note"})[0] == "201 Created"
    s, link, _ = _post(api, f"{base}/links", {"request_id": rid(), "decision_id": decision})
    assert s == "201 Created"
    _, d, _ = _call(api, f"/api/journal/decisions/{decision}")
    assert d["decision"]["execution_status"] == "MANUALLY_RECORDED"
    s, _, _ = _post(
        api, f"{base}/links/{link['link_id']}/void", {"request_id": rid(), "reason": "wrong"}
    )
    assert s == "200 OK"
    assert _post(api, f"{base}/void", {"request_id": rid(), "reason": "dup"})[0] == "200 OK"
    trade = _call(api, base)[1]["trade"]
    assert trade["status"] == "VOIDED" and trade["protection"]["state"] == "COMPLETE"
    assert {a["actor"] for a in trade["audit"]} == {"user"}


def test_idempotent_replay_and_conflict(api) -> None:
    body = _trade_body()
    first = _post(api, "/api/trades", body)
    again = _post(api, "/api/trades", body)
    assert first[0] == again[0] == "201 Created" and first[1] == again[1]
    changed = {**body, "direction": "SELL"}
    status, problem, _ = _post(api, "/api/trades", changed)
    assert status == "409 Conflict" and problem["error"] == "request_id_conflict"
    assert len(_call(api, "/api/trades")[1]["trades"]) == 1


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (b"not json", "invalid_body"),
        (b"[1, 2]", "invalid_body"),
        (b'{"symbol": "EURUSDc"}', "invalid_request_id"),
        (json.dumps({"request_id": "x"}).encode(), "invalid_request_id"),
        (b"{" + b" " * 16_400 + b"}", "invalid_body"),
    ],
)
def test_invalid_bodies_are_400(api, raw, error) -> None:
    status, body, _ = _post(api, "/api/trades", None, raw=raw)
    assert status == "400 Bad Request" and body["error"] == error


def test_validation_errors_are_400_with_a_reason(api) -> None:
    status, body, _ = _post(api, "/api/trades", _trade_body(symbol="XAUUSD"))
    assert status == "400 Bad Request" and body["error"] == "unsupported_symbol"


def test_json_content_type_is_required(api) -> None:
    status, body, _ = _post(api, "/api/trades", _trade_body(), content_type="text/plain")
    assert status == "415 Unsupported Media Type"


def test_cross_origin_writes_are_refused(api) -> None:
    status, _, _ = _post(
        api,
        "/api/trades",
        _trade_body(),
        headers={"HTTP_HOST": "127.0.0.1:8765", "HTTP_ORIGIN": "http://evil.example"},
    )
    assert status.startswith("421")
    status, _, _ = _post(api, "/api/trades", _trade_body(), headers={"HTTP_HOST": "evil.example"})
    assert status.startswith("421")
    assert _call(api, "/api/trades")[1]["trades"] == []


@pytest.mark.parametrize(
    ("method", "path", "allow"),
    [
        ("DELETE", "/api/trades", "GET, POST"),
        ("PUT", "/api/trades", "GET, POST"),
        ("DELETE", f"/api/trades/{TRADE}", "GET"),
        ("PATCH", f"/api/trades/{TRADE}", "GET"),
        ("GET", f"/api/trades/{TRADE}/fills", "POST"),
        ("DELETE", f"/api/trades/{TRADE}/fills/{TRADE}/void", "POST"),
        ("DELETE", f"/api/trades/{TRADE}/notes", "POST"),
        ("GET", f"/api/trades/{TRADE}/void", "POST"),
        ("DELETE", f"/api/trades/{TRADE}/links/{TRADE}/void", "POST"),
    ],
)
def test_unsupported_methods_are_405_and_no_delete_exists(api, method, path, allow) -> None:
    status, headers = _method(api, method, path)
    assert status == "405 Method Not Allowed" and headers["Allow"] == allow


@pytest.mark.parametrize(
    "path",
    [
        f"/api/trades/{TRADE}",
        "/api/trades/not-an-id",
        "/api/trades/x/fills",
        f"/api/trades/{TRADE}/unknown",
    ],
)
def test_unknown_or_malformed_paths_are_404(api, path) -> None:
    assert _call(api, path)[0] == "404 Not Found"


def test_unknown_trade_write_is_404(api) -> None:
    status, _, _ = _post(
        api, f"/api/trades/{TRADE}/notes", {"request_id": str(uuid.uuid4()), "text": "x"}
    )
    assert status == "404 Not Found"


@pytest.mark.parametrize(
    "query",
    [
        "status=DONE",
        "source=robot",
        "symbol=XAUUSD",
        "limit=0",
        "limit=101",
        "cursor=abc",
        "cursor=0",
    ],
)
def test_invalid_list_filters_are_400(api, query) -> None:
    assert _call(api, "/api/trades", query)[0] == "400 Bad Request"


def test_refused_journal_is_503_for_reads_and_writes(tmp_path) -> None:
    app = _app(tmp_path)
    app.start()
    app.stop()
    (tmp_path / "trading_journal.sqlite3").write_bytes(b"corrupt" * 100)
    app2 = _app(tmp_path)
    app2.start()
    try:
        api = BackendAPI(app2)
        status, body, _ = _call(api, "/api/trades")
        assert status == "503 Service Unavailable" and body["error"] == "journal_unavailable"
        status, body, _ = _post(api, "/api/trades", _trade_body())
        assert status == "503 Service Unavailable" and body["error"] == "journal_unavailable"
        assert app2.health()[0] is True
    finally:
        app2.stop()


def test_responses_contain_no_broker_identifiers_or_key(api, tmp_path) -> None:
    trade_id = _create(api)
    key = (tmp_path / "trading_journal.key").read_bytes().hex()
    texts = [
        json.dumps(_call(api, "/api/trades")[1]),
        json.dumps(_call(api, f"/api/trades/{trade_id}")[1]),
    ]
    for text in texts:
        for secret in (
            key,
            '"login"',
            '"ticket"',
            "12345678",
            "Private Person",
            "key_check",
            "broker_deal_ref",
            "position_id",
        ):
            assert secret not in text


def test_logbook_uses_only_read_only_mt5_calls(tmp_path, monkeypatch) -> None:
    import test_decision_journal
    from risk_fixtures import make_fake

    fakes = []

    def tracking_fake():
        fakes.append(make_fake())
        return fakes[-1]

    monkeypatch.setattr(test_decision_journal, "make_fake", tracking_fake)
    app = _app(tmp_path)
    app.start()
    try:
        api = BackendAPI(app)
        trade_id = _create(api)
        _call(api, f"/api/trades/{trade_id}")
        _call(api, "/api/trades")
    finally:
        app.stop()
    calls = set(fakes[0].calls)
    assert not {c for c in calls if c.startswith(("order", "position"))}


def test_logbook_source_has_no_execution_calls() -> None:
    from pathlib import Path

    import johanna.trade_logbook as module

    text = Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in (
        "order_send",
        "order_check",
        "positions_get",
        "position_close",
        "DELETE FROM",
    ):
        assert forbidden not in text
