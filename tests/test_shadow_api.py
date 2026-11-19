"""P6.1 shadow evaluation API. Temporary directories only; MT5 is the in-process fake."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from io import BytesIO

import pytest
from test_decision_journal import _app, _call
from test_journal_store import raw

from johanna.api import BackendAPI

SATURDAY = datetime(2026, 11, 21, 12, 0, tzinfo=UTC)


def _post(api, path, body=None, *, raw_body=None, content_type="application/json"):
    data = raw_body if raw_body is not None else json.dumps(body).encode()
    response: dict = {}
    environ = {
        "PATH_INFO": path,
        "REQUEST_METHOD": "POST",
        "QUERY_STRING": "",
        "CONTENT_TYPE": content_type,
        "CONTENT_LENGTH": str(len(data)),
        "wsgi.input": BytesIO(data),
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


@pytest.fixture
def app(tmp_path):
    application = _app(tmp_path)
    application.start()
    try:
        yield application
    finally:
        application.stop()


def _counts(tmp_path) -> tuple:
    with closing(raw(tmp_path)) as conn:
        return tuple(
            conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("evaluation_cycle", "gate_outcome", "shadow_scheduler_setting", "audit_log")
        )


def test_status_is_disabled_by_default(app) -> None:
    status, body, headers = _call(BackendAPI(app), "/api/shadow/status")
    assert status == "200 OK" and headers["Cache-Control"] == "no-store"
    assert body["enabled"] is False and body["state"] == "disabled"
    assert body["banner"] == "SHADOW MODE — READ-ONLY — NO ORDERS WILL BE PLACED"
    assert body["mode"] == "shadow" and body["execution"] == "none"
    assert body["journal"]["schema_version"] == 6  # P6.2b.5: v6 registered
    assert body["next_scheduled_utc"] is None


def test_enable_and_disable_persist_across_restart(tmp_path) -> None:
    first = _app(tmp_path)
    first.start()
    try:
        status, body, _ = _post(BackendAPI(first), "/api/shadow/scheduler", {"enabled": True})
        assert status == "200 OK" and body["enabled"] is True and body["state"] == "running"
    finally:
        first.stop()
    second = _app(tmp_path)
    second.start()
    try:
        api = BackendAPI(second)
        body = _call(api, "/api/shadow/status")[1]
        assert body["enabled"] is True and body["state"] == "running"  # resumed after restart
        status, body, _ = _post(api, "/api/shadow/scheduler", {"enabled": False})
        assert status == "200 OK" and body["enabled"] is False and body["state"] == "disabled"
    finally:
        second.stop()
    third = _app(tmp_path)
    third.start()
    try:
        assert _call(BackendAPI(third), "/api/shadow/status")[1]["state"] == "disabled"
    finally:
        third.stop()


@pytest.mark.parametrize(
    ("raw_body", "content_type", "expected"),
    [
        (b'{"enabled": "yes"}', "application/json", "400 Bad Request"),
        (b"{}", "application/json", "400 Bad Request"),
        (b'{"enabled": true, "x": 1}', "application/json", "400 Bad Request"),
        (b"[true]", "application/json", "400 Bad Request"),
        (b"not json", "application/json", "400 Bad Request"),
        (b"{" + b" " * 16_400 + b"}", "application/json", "400 Bad Request"),
        (b'{"enabled": true}', "text/plain", "415 Unsupported Media Type"),
    ],
)
def test_invalid_scheduler_requests_change_nothing(app, tmp_path, raw_body, content_type, expected):
    before = _counts(tmp_path)
    status, _, _ = _post(
        BackendAPI(app), "/api/shadow/scheduler", raw_body=raw_body, content_type=content_type
    )
    assert status == expected
    assert _counts(tmp_path) == before and app.shadow.enabled() is False


@pytest.mark.parametrize(
    ("method", "path", "allow"),
    [
        ("GET", "/api/shadow/scheduler", "POST"),
        ("DELETE", "/api/shadow/scheduler", "POST"),
        ("POST", "/api/shadow/status", "GET"),
        ("DELETE", "/api/shadow/evaluations", "GET"),
        ("POST", "/api/shadow/diagnostics", "GET"),
        ("DELETE", "/api/shadow/evaluations/01JZZZZZZZZZZZZZZZZZZZZZZZ", "GET"),
    ],
)
def test_unsupported_methods_are_405(app, method, path, allow) -> None:
    status, headers = _method(BackendAPI(app), method, path)
    assert status == "405 Method Not Allowed" and headers["Allow"] == allow


@pytest.mark.parametrize(
    "path",
    [
        "/api/shadow/unknown",
        "/api/shadow/evaluations/not-an-id",
        "/api/shadow/evaluations/01JZZZZZZZZZZZZZZZZZZZZZZZ",
    ],
)
def test_unknown_paths_are_404(app, path) -> None:
    assert _call(BackendAPI(app), path)[0] == "404 Not Found"


@pytest.mark.parametrize(
    "query", ["symbol=XAUUSD", "timeframe=M5", "status=DONE", "limit=0", "limit=101", "cursor=x"]
)
def test_invalid_filters_are_400(app, query) -> None:
    assert _call(BackendAPI(app), "/api/shadow/evaluations", query)[0] == "400 Bad Request"


def test_reads_never_change_state_and_expose_records(app, tmp_path) -> None:
    # Phase 1 (M-10): a pinned clock. With the real clock, enabling within 10 minutes after an
    # hourly close owes that hour's windows, which disabling then closes as SKIPPED
    # (scheduler_disabled) -- the intermittent failure observed at ~18:00 and ~19:00 UTC.
    # Wednesday 12:30 UTC: every H1/H4/D1 deadline has passed, so no window is owed.
    app.shadow._clock = lambda: datetime(2026, 11, 18, 12, 30, tzinfo=UTC)
    app.shadow.set_enabled(True)
    app.shadow.set_enabled(False)
    app.shadow._create_window("EURUSD", "H1", SATURDAY, SATURDAY)  # a weekend window
    api = BackendAPI(app)
    before = _counts(tmp_path)
    for path, query in (
        ("/api/shadow/status", ""),
        ("/api/shadow/diagnostics", ""),
        ("/api/shadow/evaluations", "symbol=EURUSDc&timeframe=H1"),
        ("/api/shadow/evaluations", "status=SKIPPED"),
    ):
        status, body, headers = _call(api, path, query)
        assert status == "200 OK" and headers["Cache-Control"] == "no-store", path
    assert _counts(tmp_path) == before
    items = _call(api, "/api/shadow/evaluations", "status=SKIPPED")[1]["evaluations"]
    assert [(i["status"], i["reason_code"]) for i in items] == [("SKIPPED", "market_closed")]
    detail = _call(api, f"/api/shadow/evaluations/{items[0]['id']}")[1]["evaluation"]
    assert detail["gates"][0]["gate"] == "session.open"
    diag = _call(api, "/api/shadow/diagnostics")[1]
    assert diag["populations"]["executable_opportunities"] is None
    assert diag["populations"]["actual_trades"] is None
    assert diag["data_and_closure"]["market_closed"] == 1


def test_refused_journal_is_503_and_status_reports_blocked(tmp_path) -> None:
    first = _app(tmp_path)
    first.start()
    first.stop()
    (tmp_path / "trading_journal.sqlite3").write_bytes(b"corrupt" * 100)
    broken = _app(tmp_path)
    broken.start()
    try:
        api = BackendAPI(broken)
        body = _call(api, "/api/shadow/status")[1]
        assert body["state"] == "blocked" and body["enabled"] is None
        for path in ("/api/shadow/diagnostics", "/api/shadow/evaluations"):
            assert _call(api, path)[0] == "503 Service Unavailable"
        status, body, _ = _post(api, "/api/shadow/scheduler", {"enabled": True})
        assert status == "503 Service Unavailable" and body["error"] == "journal_unavailable"
        assert broken.health()[0] is True  # the rest of Johanna keeps running
    finally:
        broken.stop()


def test_responses_contain_no_identifiers_or_key(app, tmp_path) -> None:
    app.shadow._create_window("EURUSD", "H1", SATURDAY, SATURDAY)
    api = BackendAPI(app)
    key = (tmp_path / "trading_journal.key").read_bytes().hex()
    texts = [
        json.dumps(_call(api, path)[1])
        for path in ("/api/shadow/status", "/api/shadow/diagnostics", "/api/shadow/evaluations")
    ]
    for text in texts:
        for secret in (key, '"login"', '"ticket"', "12345678", "Private Person", "key_check"):
            assert secret not in text


def test_shadow_module_has_no_execution_calls() -> None:
    from pathlib import Path

    import johanna.shadow_evaluation as module

    text = Path(module.__file__).read_text(encoding="utf-8")
    for forbidden in ("order_send", "order_check", "positions_get", "submit(", "DELETE FROM"):
        assert forbidden not in text
