"""P6.4 local notification infrastructure: lifecycle, idempotency, bounded delivery, safe
content, failure isolation and read-only listing. Temporary journals with the staged schema v5;
local adapters only -- nothing is sent anywhere."""

from __future__ import annotations

import ast
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path

import pytest
from test_decision_journal import _app, _call
from test_journal_store import raw, started

from johanna import journal_store as js
from johanna.api import BackendAPI
from johanna.notifications import (
    MAX_ATTEMPTS,
    DashboardInboxAdapter,
    InMemoryAdapter,
    NotificationCenter,
    NotificationError,
    compose,
)

# P6.2b.4: claim-based delivery (M-6) needs the staged v6 tables; creation works on v5.
STAGED = js.MIGRATIONS  # v6 registered in P6.2b.5
V4 = js.MIGRATIONS[:4]  # a journal still on v4 (before the v5 migration)
SRC = Path(__file__).resolve().parents[1] / "src" / "johanna"
CANDIDATE = {
    "symbol": "EURUSDc",
    "timeframe": "H1",
    "direction": "BUY",
    "eligibility": "UNVERIFIED",
}


def centre(tmp_path, *adapters):
    store = started(tmp_path, migrations=STAGED)
    return NotificationCenter(store, adapters or (InMemoryAdapter(),)), store


def states(tmp_path, nid) -> list[str]:
    with closing(raw(tmp_path)) as conn:
        return [
            r[0]
            for r in conn.execute(
                "SELECT state FROM notification_state WHERE notification_id=? ORDER BY rowid",
                (nid,),
            )
        ]


# ====================================================================== content
def test_candidate_is_never_described_as_an_approved_trade() -> None:
    title, body = compose("trade_candidate", CANDIDATE)
    assert title == "Market candidate: EURUSDc H1 BUY"
    assert "not an approved trade and not an order" in body
    assert "places no orders" in body


@pytest.mark.parametrize(
    ("category", "facts"),
    [
        ("information", {"title": "Account 12345678", "text": "x"}),  # identifier-like
        ("information", {"title": "Hello", "text": "ticket closed"}),
        ("information", {"title": "Hello", "text": "your password"}),
        ("information", {"title": "Hello", "text": "Buy now on EURUSD"}),  # an instruction
        ("information", {"title": "Hello", "text": "This approved trade is ready"}),
        ("trade_candidate", {**CANDIDATE, "symbol": "XAUUSDc"}),
        ("trade_candidate", {**CANDIDATE, "eligibility": "APPROVED"}),
        (
            "broker_event",
            {"symbol": "EURUSDc", "event": "position_confirmed_closed", "basis": "inferred"},
        ),  # only broker-confirmed facts
        ("risk_rejection", {"symbol": "EURUSDc", "timeframe": "H1", "reason": "user@example"}),
        ("alert", {"text": "x"}),
    ],
)
def test_unsafe_or_invalid_content_is_refused(category, facts) -> None:
    with pytest.raises(ValueError):
        compose(category, facts)


def test_categories_are_distinguished() -> None:
    assert compose(
        "broker_event",
        {"symbol": "USDJPYc", "event": "position_confirmed_closed", "basis": "broker_confirmed"},
    )[0].startswith("Broker-confirmed")
    assert (
        "No order"
        in compose(
            "risk_rejection",
            {"symbol": "GBPUSDc", "timeframe": "H4", "reason": "costs not established"},
        )[1]
    )


# ====================================================================== lifecycle
def test_full_lifecycle_in_order(tmp_path) -> None:
    memory = InMemoryAdapter()
    c, store = centre(tmp_path, memory)
    nid = c.notify("trade_candidate", "cand:EURUSD:H1:2026-11-18T13:00Z", CANDIDATE)
    assert states(tmp_path, nid) == ["pending"]
    assert c.deliver_pending()["delivered"] == 1 and len(memory.delivered) == 1
    assert states(tmp_path, nid) == ["pending", "available"]
    c.mark_presented(nid)
    c.acknowledge(nid)
    assert states(tmp_path, nid) == ["pending", "available", "presented", "acknowledged"]
    assert store.verify_audit_chain()["valid"] is True


def test_states_cannot_be_skipped_repeated_or_acknowledged_by_johanna(tmp_path) -> None:
    c, _ = centre(tmp_path)
    nid = c.notify("trade_candidate", "k1", CANDIDATE)
    with pytest.raises(NotificationError):  # M-6: a controlled error, not a raw DB error
        c.mark_presented(nid)  # available first
    c.deliver_pending()
    with pytest.raises(NotificationError):
        c.acknowledge(nid)  # presented first
    c.mark_presented(nid)
    with pytest.raises(NotificationError):
        c.mark_presented(nid)  # each state once
    with closing(raw(tmp_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO notification_state VALUES ('S9', ?, 'acknowledged', 'johanna', 't')",
                (nid,),
            )
        for sql in (
            "UPDATE notification SET title='x'",
            "DELETE FROM notification_state",
            "UPDATE notification_delivery SET outcome='delivered'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)


def test_idempotent_across_retries_and_restarts(tmp_path) -> None:
    memory = InMemoryAdapter()
    c, store = centre(tmp_path, memory)
    first = c.notify("trade_candidate", "same-key", CANDIDATE)
    assert c.notify("trade_candidate", "same-key", CANDIDATE) == first
    c.deliver_pending()
    c.deliver_pending()  # already available: not delivered again
    restarted = NotificationCenter(store, (memory,))
    assert restarted.notify("trade_candidate", "same-key", CANDIDATE) == first
    restarted.deliver_pending()
    assert len(memory.delivered) == 1
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM notification").fetchone()[0] == 1


def test_failed_deliveries_are_recorded_retried_and_bounded(tmp_path) -> None:
    flaky = InMemoryAdapter(fail=2)
    c, _ = centre(tmp_path, flaky)
    nid = c.notify("information", "info-1", {"title": "Backup verified", "text": "All good."})
    assert c.deliver_pending()["failed"] == 1
    assert c.deliver_pending()["failed"] == 1
    assert c.deliver_pending()["delivered"] == 1
    with closing(raw(tmp_path)) as conn:
        rows = conn.execute(
            "SELECT attempt, outcome, error_code FROM notification_delivery ORDER BY attempt"
        ).fetchall()
    assert [tuple(r) for r in rows] == [
        (1, "failed", "adapter_unavailable"),
        (2, "failed", "adapter_unavailable"),
        (3, "delivered", None),
    ]
    dead = InMemoryAdapter("test_dead", fail=10**6)
    c2, _ = centre(tmp_path / "b", dead)
    nid2 = c2.notify("information", "info-2", {"title": "Hello", "text": "Local only."})
    for _ in range(MAX_ATTEMPTS + 2):
        c2.deliver_pending()
    with closing(raw(tmp_path / "b")) as conn:
        assert conn.execute("SELECT count(*) FROM notification_delivery").fetchone()[0] == (
            MAX_ATTEMPTS
        )
    assert states(tmp_path / "b", nid2) == ["pending"]
    assert states(tmp_path, nid) == ["pending", "available"]


def test_failures_never_raise_into_callers(tmp_path) -> None:
    class Broken:
        name = "test_broken"

        def deliver(self, message):
            raise RuntimeError("adapter bug")

    c, store = centre(tmp_path, Broken())
    assert c.notify("trade_candidate", "k", {**CANDIDATE, "symbol": "BAD"}) is None
    assert c.failures == 1
    nid = c.notify("trade_candidate", "k", CANDIDATE)
    assert c.deliver_pending()["failed"] == 1  # adapter bug = failed attempt, no crash
    store.stop()  # journal refused / stopped
    assert c.notify("trade_candidate", "k2", CANDIDATE) is None
    assert c.deliver_pending()["status"] in ("not_installed", "journal_unavailable")
    assert nid is not None


def test_not_installed_on_a_v4_journal(tmp_path) -> None:
    store = started(tmp_path, migrations=V4)
    c = NotificationCenter(store, (DashboardInboxAdapter(),))
    assert c.installed() is False
    assert c.notify("trade_candidate", "k", CANDIDATE) is None
    assert c.last_failure == "not_installed"
    assert c.list() == {"status": "not_installed", "notifications": []}


def test_only_local_adapters_and_no_network_code() -> None:
    class Whatsapp:
        name = "whatsapp"

        def deliver(self, message):  # pragma: no cover
            raise AssertionError

    with pytest.raises(ValueError):
        NotificationCenter(None, (Whatsapp(),))
    tree = ast.parse((SRC / "notifications.py").read_text(encoding="utf-8"))
    imported = {
        (n.module or "") if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [n])
    }
    for banned in ("smtplib", "urllib", "requests", "http", "socket", "twilio", "email"):
        assert not any(i.split(".")[0] == banned for i in imported), banned


def test_list_is_read_only(tmp_path) -> None:
    c, _ = centre(tmp_path)
    nid = c.notify("trade_candidate", "k", CANDIDATE)
    before = states(tmp_path, nid)
    listed = c.list()
    assert listed["notifications"][0]["state"] == "pending"
    assert c.list(state="pending")["notifications"][0]["id"] == nid
    assert states(tmp_path, nid) == before
    with pytest.raises(ValueError):
        c.list(state="read")


def test_api_lists_without_mutation(tmp_path) -> None:
    app = _app(tmp_path)
    app.start()
    try:
        api = BackendAPI(app)
        status, body, headers = _call(api, "/api/notifications")
        assert status == "200 OK" and headers["Cache-Control"] == "no-store"
        assert body["status"] == "installed" and body["notifications"] == []  # none automatic
        assert body["service"]["external_delivery"].startswith("none")
        assert body["service"]["automatic_notifications"] is False
        assert _call(api, "/api/notifications", "state=bogus")[0] == "400 Bad Request"
        response: dict = {}
        api(
            {
                "PATH_INFO": "/api/notifications",
                "REQUEST_METHOD": "POST",
                "QUERY_STRING": "",
                "wsgi.input": BytesIO(),
            },
            lambda s, h: response.update(status=s),
        )
        assert response["status"].startswith("405")
    finally:
        app.stop()


def test_dashboard_inbox_sends_nothing() -> None:
    assert DashboardInboxAdapter().deliver({"title": "x"}) is None
    assert datetime.now(UTC)  # local adapter, no I/O
