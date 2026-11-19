"""P6.2a read-only shadow-intent and funnel API. Temporary directories only; MT5 is the
in-process fake; intents are seeded through the scheduler's own completion path."""

from __future__ import annotations

import copy
import json
from contextlib import closing
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from risk_fixtures import BUY_LEVEL, PRODUCTION_COSTS, VERIFIED_COSTS, envelope, propose
from test_decision_journal import _app, _call
from test_journal_store import raw
from test_shadow_api import _method

from johanna.api import BackendAPI
from johanna.shadow_evaluation import iso_z

WEDNESDAY = datetime(2026, 11, 18, 13, 0, tzinfo=UTC)
ENV = envelope(level=BUY_LEVEL)
UNVERIFIED = propose(env=ENV, costs=PRODUCTION_COSTS)
APPROVED = propose(env=ENV, costs=VERIFIED_COSTS)
TABLES = (
    "decision",
    "risk_assessment",
    "evaluation_cycle",
    "gate_outcome",
    "shadow_intent",
    "shadow_scheduler_setting",
    "audit_log",
    "decision_observation",
    "risk_assessment_observation",
)


def seed(service, result, close, symbol="EURUSD"):
    """One completed scheduled candidate window through ``_create_window`` and ``_complete``."""
    service._create_window(symbol, "H1", close, close + timedelta(seconds=30))
    with service.store.write() as conn:
        cycle_id = conn.execute(
            "SELECT id FROM evaluation_cycle WHERE symbol=? AND window_close_utc=?",
            (symbol, iso_z(close)),
        ).fetchone()[0]
        conn.execute(
            "UPDATE evaluation_cycle SET status='RUNNING', attempts=1 WHERE id=?", (cycle_id,)
        )
    env = copy.deepcopy(ENV)
    env["symbol"] = env["result"]["symbol"] = symbol
    risk = {**copy.deepcopy(result), "symbol": symbol, "broker_symbol": f"{symbol}c"}
    outcome = {
        "envelope": env,
        "risk": risk,
        "closure": "tick_observed",
        "window_close_utc": iso_z(close),
    }
    service._complete(cycle_id, outcome, 0)
    return cycle_id


@pytest.fixture
def app(tmp_path):
    application = _app(tmp_path)
    application.start()
    try:
        seed(application.shadow, UNVERIFIED, WEDNESDAY)
        seed(application.shadow, APPROVED, WEDNESDAY + timedelta(hours=1), "USDJPY")
        seed(application.shadow, UNVERIFIED, WEDNESDAY + timedelta(days=1), "GBPUSD")
        yield application
    finally:
        application.stop()


def counts(tmp_path) -> dict:
    with closing(raw(tmp_path)) as conn:
        return {t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in TABLES}


def test_list_returns_labelled_intents(app) -> None:
    status, body, headers = _call(BackendAPI(app), "/api/shadow/intents")
    assert status == "200 OK" and headers["Cache-Control"] == "no-store"
    assert body["label"] == "HYPOTHETICAL — NOT AN ORDER"
    assert len(body["intents"]) == 3 and body["next_cursor"] is None
    for item in body["intents"]:
        assert item["label"] == "HYPOTHETICAL — NOT AN ORDER"
        assert item["kind"] == "SHADOW_HYPOTHETICAL"
    approved = next(i for i in body["intents"] if i["eligibility"] == "RISK_APPROVED")
    assert approved["symbol"] == "USDJPY" and approved["volume"] == 0.01
    assert approved["cost_evidence"]["status"] == "established"
    unverified = next(i for i in body["intents"] if i["symbol"] == "EURUSD")
    assert unverified["volume"] is None and unverified["risk_amount_usc"] is None
    assert unverified["reasons"]["primary"]["reason"] == "costs.net_reward_risk"


@pytest.mark.parametrize(
    ("query", "symbols"),
    [
        ("symbol=EURUSDc", {"EURUSD"}),
        ("symbol=gbpusd", {"GBPUSD"}),
        ("eligibility=RISK_APPROVED", {"USDJPY"}),
        ("eligibility=unverified", {"EURUSD", "GBPUSD"}),
        ("timeframe=H4", set()),
        ("from=2026-11-19", {"GBPUSD"}),
        ("to=2026-11-18", {"EURUSD", "USDJPY"}),
        ("from=2026-11-18&to=2026-11-18&symbol=USDJPY", {"USDJPY"}),
    ],
)
def test_list_filters(app, query, symbols) -> None:
    status, body, _ = _call(BackendAPI(app), "/api/shadow/intents", query)
    assert status == "200 OK" and {i["symbol"] for i in body["intents"]} == symbols


def test_list_pages_with_a_cursor(app) -> None:
    api = BackendAPI(app)
    first = _call(api, "/api/shadow/intents", "limit=2")[1]
    assert len(first["intents"]) == 2 and first["next_cursor"]
    rest = _call(api, "/api/shadow/intents", f"limit=2&cursor={first['next_cursor']}")[1]
    assert len(rest["intents"]) == 1 and rest["next_cursor"] is None


def test_detail_returns_the_intent_and_its_evidence(app) -> None:
    api = BackendAPI(app)
    listed = _call(api, "/api/shadow/intents", "symbol=USDJPY")[1]["intents"][0]
    status, body, headers = _call(api, f"/api/shadow/intents/{listed['id']}")
    assert status == "200 OK" and headers["Cache-Control"] == "no-store"
    assert body["label"] == "HYPOTHETICAL — NOT AN ORDER"
    assert body["intent"]["id"] == listed["id"]
    assert body["evaluation"]["id"] == listed["evaluation_id"]
    assert body["evaluation"]["status"] == "COMPLETED"
    assert body["risk_assessment"]["id"] == listed["risk_assessment_id"]
    assert body["risk_assessment"]["risk_status"] == "APPROVED"


@pytest.mark.parametrize(
    "path", ["/api/shadow/intents/01JZZZZZZZZZZZZZZZZZZZZZZZ", "/api/shadow/intents/not-an-id"]
)
def test_unknown_intents_are_404(app, path) -> None:
    assert _call(BackendAPI(app), path)[0] == "404 Not Found"


def test_funnel_endpoint(app) -> None:
    status, body, headers = _call(BackendAPI(app), "/api/shadow/funnel")
    assert status == "200 OK" and headers["Cache-Control"] == "no-store"
    stages = {s["id"]: s["count"] for s in body["stages"]}
    assert stages["windows_scheduled"] == 3 and stages["constructed_trade_intents"] == 3
    assert stages["risk_approved_shadow_intents"] == 1
    assert body["execution"] == {**body["execution"], "executed_trades": None, "realised_pnl": None}
    ranged = _call(BackendAPI(app), "/api/shadow/funnel", "from=2026-11-19&to=2026-11-19")[1]
    assert ranged["stages"][0]["count"] == 1 and ranged["range"]["from_utc"].startswith(
        "2026-11-19"
    )


def test_gets_never_write_to_the_journal(app, tmp_path) -> None:
    api = BackendAPI(app)
    before = counts(tmp_path)
    intent_id = _call(api, "/api/shadow/intents")[1]["intents"][0]["id"]
    for path, query in (
        ("/api/shadow/intents", ""),
        ("/api/shadow/intents", "eligibility=REJECTED&from=2026-11-01&to=2026-11-30"),
        (f"/api/shadow/intents/{intent_id}", ""),
        ("/api/shadow/funnel", ""),
        ("/api/shadow/funnel", "from=2026-11-18"),
        ("/api/shadow/diagnostics", ""),
    ):
        assert _call(api, path, query)[0] == "200 OK"
    assert counts(tmp_path) == before


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize(
    "path",
    ["/api/shadow/intents", "/api/shadow/intents/01JZZZZZZZZZZZZZZZZZZZZZZZ", "/api/shadow/funnel"],
)
def test_only_get_is_allowed(app, tmp_path, method, path) -> None:
    before = counts(tmp_path)
    status, headers = _method(BackendAPI(app), method, path)
    assert status == "405 Method Not Allowed" and headers["Allow"] == "GET"
    assert counts(tmp_path) == before


@pytest.mark.parametrize(
    "extra",
    [
        {"HTTP_HOST": "evil.example"},
        {"HTTP_HOST": "127.0.0.1:8765", "HTTP_ORIGIN": "http://evil.example"},
    ],
)
def test_host_and_origin_checks_apply(app, extra) -> None:
    response: dict = {}
    for path in ("/api/shadow/intents", "/api/shadow/funnel"):
        body = b"".join(
            BackendAPI(app)(
                {
                    "PATH_INFO": path,
                    "REQUEST_METHOD": "GET",
                    "QUERY_STRING": "",
                    "wsgi.input": BytesIO(),
                    **extra,
                },
                lambda s, h: response.update(status=s),
            )
        )
        assert response["status"].startswith("421") and b"intents" not in body


@pytest.mark.parametrize(
    ("path", "query"),
    [
        ("/api/shadow/intents", "symbol=XAUUSD"),
        ("/api/shadow/intents", "timeframe=M5"),
        ("/api/shadow/intents", "eligibility=APPROVED"),
        ("/api/shadow/intents", "from=2026-13-01"),
        ("/api/shadow/intents", "from=18-11-2026"),
        ("/api/shadow/intents", "from=2026-11-20&to=2026-11-19"),
        ("/api/shadow/intents", "limit=0"),
        ("/api/shadow/intents", "limit=101"),
        ("/api/shadow/intents", "limit=x"),
        ("/api/shadow/intents", "cursor=0"),
        ("/api/shadow/intents", "status=COMPLETED"),
        ("/api/shadow/funnel", "symbol=EURUSD"),
        ("/api/shadow/funnel", "from=yesterday"),
    ],
)
def test_invalid_filters_are_400(app, tmp_path, path, query) -> None:
    before = counts(tmp_path)
    status, body, _ = _call(BackendAPI(app), path, query)
    assert status == "400 Bad Request" and body["error"] == "invalid_shadow_request"
    assert counts(tmp_path) == before


def test_responses_contain_no_identifiers_or_key(app, tmp_path) -> None:
    api = BackendAPI(app)
    key = (tmp_path / "trading_journal.key").read_bytes().hex()
    intent_id = _call(api, "/api/shadow/intents")[1]["intents"][0]["id"]
    texts = [
        json.dumps(_call(api, path)[1])
        for path in (
            "/api/shadow/intents",
            f"/api/shadow/intents/{intent_id}",
            "/api/shadow/funnel",
        )
    ]
    for text in texts:
        for secret in (
            key,
            '"login"',
            '"ticket"',
            "12345678",
            "Exness-MT5Trial9",
            "Exness Technologies",
            '"server"',
            '"company"',
            "key_check",
        ):
            assert secret not in text, secret


def test_refused_journal_is_503(tmp_path) -> None:
    first = _app(tmp_path)
    first.start()
    first.stop()
    (tmp_path / "trading_journal.sqlite3").write_bytes(b"corrupt" * 100)
    broken = _app(tmp_path)
    broken.start()
    try:
        api = BackendAPI(broken)
        for path in (
            "/api/shadow/intents",
            "/api/shadow/funnel",
            "/api/shadow/intents/01JZZZZZZZZZZZZZZZZZZZZZZZ",
        ):
            assert _call(api, path)[0] == "503 Service Unavailable"
    finally:
        broken.stop()
