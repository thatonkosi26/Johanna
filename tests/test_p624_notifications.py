"""P6.2b.4 M-6 / L-5: claim-based notification delivery, concurrency, observability, audit.

Temporary journals with the staged schema v6 only. Concurrency is controlled with
``threading.Event`` (no timing sleeps): an adapter blocks inside ``deliver`` while a second
worker runs, so the interleaving is exact."""

from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from test_journal_store import raw, started

from johanna import journal_store as js
from johanna import notifications as nt
from johanna.notifications import (
    CLAIM_TIMEOUT,
    MAX_ATTEMPTS,
    InMemoryAdapter,
    NotificationCenter,
    NotificationError,
)

V6 = js.MIGRATIONS  # registered in P6.2b.5
INFO = {"title": "Backup verified", "text": "Local check passed."}


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Blocking:
    """Delivers only after ``release`` is set; signals ``entered`` when inside deliver."""

    def __init__(self, name: str = "memory") -> None:
        self.name = name
        self.entered, self.release = threading.Event(), threading.Event()
        self.delivered: list[dict] = []

    def deliver(self, message) -> None:
        self.entered.set()
        assert self.release.wait(10), "test did not release the adapter"
        self.delivered.append(dict(message))


def centre(tmp_path, *adapters, clock=None, migrations=V6):
    clock = clock or Clock()
    store = started(tmp_path, migrations=migrations, clock=clock)
    return NotificationCenter(store, adapters or (InMemoryAdapter(),), clock=clock), store, clock


def rows(tmp_path, sql, *args):
    with closing(raw(tmp_path)) as conn:
        return [tuple(r) for r in conn.execute(sql, args)]


def audit_actions(tmp_path) -> list[str]:
    return [r[0] for r in rows(tmp_path, "SELECT action FROM audit_log ORDER BY seq")]


# ====================================================================== concurrency
def test_a_second_worker_never_delivers_an_attempt_in_flight(tmp_path) -> None:
    adapter = Blocking()
    a, store, clock = centre(tmp_path, adapter)
    b = NotificationCenter(store, (adapter,), clock=clock)  # a second worker, same journal
    nid = a.notify("information", "k1", INFO)
    results: dict[str, dict] = {}
    worker = threading.Thread(target=lambda: results.setdefault("a", a.deliver_pending()))
    worker.start()
    assert adapter.entered.wait(10)  # A owns attempt 1 and is inside deliver
    results["b"] = b.deliver_pending()  # B runs while A is delivering
    adapter.release.set()
    worker.join(10)
    assert results["b"]["in_progress"] == 1 and results["b"]["delivered"] == 0
    assert results["a"]["delivered"] == 1
    assert len(adapter.delivered) == 1  # exactly one delivery
    assert rows(tmp_path, "SELECT attempt, outcome FROM notification_delivery") == [
        (1, "delivered")
    ]
    assert rows(tmp_path, "SELECT count(*) FROM notification_claim") == [(1,)]
    assert b.deliver_pending()["already_delivered"] == 0  # nothing pending any more
    states = rows(tmp_path, "SELECT state FROM notification_state ORDER BY rowid")
    assert [r[0] for r in states] == [
        "pending",
        "available",
    ]
    assert nid and store.verify_audit_chain()["valid"]


def test_many_concurrent_workers_deliver_once(tmp_path) -> None:
    adapter = InMemoryAdapter()
    first, store, clock = centre(tmp_path, adapter)
    for i in range(5):
        first.notify("information", f"k{i}", INFO)
    workers = [NotificationCenter(store, (adapter,), clock=clock) for _ in range(6)]
    barrier = threading.Barrier(len(workers))
    outcomes: list[dict] = []

    def run(c):
        barrier.wait(10)
        outcomes.append(c.deliver_pending())

    threads = [threading.Thread(target=run, args=(c,)) for c in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert len(adapter.delivered) == 5  # each notification exactly once
    assert sum(o["delivered"] for o in outcomes) == 5
    assert all(o["status"] == "ok" for o in outcomes)
    assert rows(tmp_path, "SELECT count(*) FROM notification_delivery") == [(5,)]


def test_a_lost_claim_race_is_a_conflict_not_an_exception(tmp_path, monkeypatch) -> None:
    adapter = InMemoryAdapter()
    c, store, _ = centre(tmp_path, adapter)
    nid = c.notify("information", "k1", INFO)
    original = c.store.write

    class Racing:
        """Inserts a competing claim for the same attempt immediately before our INSERT,
        i.e. after our read of the existing claims -- the exact race window."""

        def __init__(self, conn) -> None:
            self._conn = conn

        def execute(self, sql, params=()):
            if sql.startswith("INSERT INTO notification_claim"):
                self._conn.execute(
                    "INSERT INTO notification_claim VALUES ('RACE', ?, ?, ?, 'other', ?)",
                    (params[1], params[2], params[3], params[5]),
                )
            return self._conn.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def racing_write():
        ctx = original()

        class Wrapped:
            def __enter__(self):
                return Racing(ctx.__enter__())

            def __exit__(self, *exc):
                return ctx.__exit__(*exc)

        return Wrapped()

    monkeypatch.setattr(c.store, "write", racing_write)
    assert c._claim(nid, "memory") == "conflict"
    monkeypatch.setattr(c.store, "write", original)
    assert adapter.delivered == []  # the loser never called the adapter
    assert rows(tmp_path, "SELECT count(*) FROM notification_claim") == [(0,)]  # rolled back


def test_the_database_refuses_a_second_successful_delivery(tmp_path) -> None:
    c, _, _ = centre(tmp_path)
    nid = c.notify("information", "k1", INFO)
    c.deliver_pending()
    with closing(raw(tmp_path)) as conn:
        conn.execute(
            "INSERT INTO notification_claim VALUES ('C2', ?, 'test_x', 1, 'w', 't')", (nid,)
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO notification_delivery VALUES ('D2', ?, 'test_x', 1, 'delivered', "
                "NULL, 't')",
                (nid,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="claimed attempt"):
            conn.execute(
                "INSERT INTO notification_delivery VALUES ('D3', ?, 'memory', 9, 'failed', "
                "'x', 't')",
                (nid,),
            )


# ====================================================================== failure handling
def test_a_crashed_owner_becomes_a_visible_expired_failure_then_retries(tmp_path) -> None:
    adapter = InMemoryAdapter()
    c, store, clock = centre(tmp_path, adapter)
    nid = c.notify("information", "k1", INFO)
    assert c._claim(nid, "memory") == 1  # claimed, then the owner "crashes"
    listed = c.list()["notifications"][0]
    assert listed["delivery"] == "in_progress"
    assert c.deliver_pending()["in_progress"] == 1 and adapter.delivered == []
    clock.now += CLAIM_TIMEOUT
    assert c.list()["notifications"][0]["delivery"] == "claim_stale"
    assert c.deliver_pending()["delivered"] == 1  # deterministic retry after expiry
    assert rows(
        tmp_path, "SELECT attempt, outcome, error_code FROM notification_delivery ORDER BY rowid"
    ) == [(1, "failed", "claim_expired"), (2, "delivered", None)]
    assert "notification.failed" in audit_actions(tmp_path)
    assert store.verify_audit_chain()["valid"]


def test_failed_retryable_and_exhausted_are_distinguishable(tmp_path) -> None:
    flaky = InMemoryAdapter(fail=1)
    c, _, _ = centre(tmp_path, flaky)
    c.notify("information", "k1", INFO)
    assert c.list()["notifications"][0]["delivery"] == "not_attempted"
    assert c.deliver_pending()["failed"] == 1
    item = c.list()["notifications"][0]
    assert (item["delivery"], item["last_error"]) == ("failed_retryable", "adapter_unavailable")
    assert c.deliver_pending()["delivered"] == 1
    assert c.list()["notifications"][0]["delivery"] == "delivered"

    dead = InMemoryAdapter("test_dead", fail=10**6)
    d, _, _ = centre(tmp_path / "dead", dead)
    d.notify("information", "k2", INFO)
    outcomes = [d.deliver_pending() for _ in range(MAX_ATTEMPTS + 2)]
    assert [o["failed"] for o in outcomes[:MAX_ATTEMPTS]] == [1] * MAX_ATTEMPTS
    assert outcomes[-1]["exhausted"] == 1  # further runs report exhaustion, never retry
    item = d.list()["notifications"][0]
    assert item["delivery"] == "exhausted" and item["state"] == "pending"
    assert audit_actions(tmp_path / "dead").count("notification.exhausted") == 1
    assert audit_actions(tmp_path / "dead").count("notification.failed") == MAX_ATTEMPTS


def test_retry_order_and_attempt_numbers_are_deterministic(tmp_path) -> None:
    flaky = InMemoryAdapter(fail=2)
    c, _, _ = centre(tmp_path, flaky)
    c.notify("information", "k1", INFO)
    for _ in range(3):
        c.deliver_pending()
    assert rows(tmp_path, "SELECT attempt, outcome FROM notification_delivery ORDER BY rowid") == [
        (1, "failed"),
        (2, "failed"),
        (3, "delivered"),
    ]
    assert rows(tmp_path, "SELECT attempt FROM notification_claim ORDER BY rowid") == [
        (1,),
        (2,),
        (3,),
    ]


def test_database_errors_never_escape_deliver_pending(tmp_path, monkeypatch) -> None:
    c, _, _ = centre(tmp_path)
    c.notify("information", "k1", INFO)

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(c, "_attempt", boom)
    result = c.deliver_pending()
    assert result["status"] == "journal_error" and c.last_failure == "OperationalError"


def test_v5_journals_refuse_delivery_fail_closed(tmp_path) -> None:
    adapter = InMemoryAdapter()
    c, _, _ = centre(tmp_path, adapter, migrations=js.MIGRATIONS[:5])
    nid = c.notify("information", "k1", INFO)
    assert nid is not None  # creation still works on v5
    assert c.deliver_pending()["status"] == "claims_not_installed"
    assert adapter.delivered == [] and c.status()["claims_installed"] is False


# ====================================================================== transitions + audit
def test_explicit_transitions_raise_controlled_errors_and_are_audited(tmp_path) -> None:
    c, store, _ = centre(tmp_path)
    nid = c.notify("information", "k1", INFO)
    with pytest.raises(NotificationError) as err:
        c.mark_presented(nid)
    assert err.value.code == "invalid_transition"
    with pytest.raises(NotificationError) as err:
        c.acknowledge("01JNOTANOTIFICATION000000A")
    assert err.value.code == "not_found"
    c.deliver_pending()
    assert c.mark_presented(nid) == {"id": nid, "state": "presented"}
    assert c.acknowledge(nid)["state"] == "acknowledged"
    actions = audit_actions(tmp_path)
    for action in (
        "notification.created",
        "notification.delivered",
        "notification.presented",
        "notification.acknowledged",
    ):
        assert actions.count(action) == 1, action
    who = rows(tmp_path, "SELECT actor FROM audit_log WHERE action='notification.acknowledged'")
    assert who == [("user",)]
    assert store.verify_audit_chain()["valid"]


def test_claims_are_append_only_and_nothing_is_external(tmp_path) -> None:
    c, _, _ = centre(tmp_path)
    nid = c.notify("information", "k1", INFO)
    c.deliver_pending()
    with closing(raw(tmp_path)) as conn:
        for sql in ("UPDATE notification_claim SET worker='x'", "DELETE FROM notification_claim"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)
    status = c.status()
    assert status["automatic_notifications"] is False
    assert status["external_delivery"].startswith("none")
    assert nid and nt.CLAIM_TIMEOUT == timedelta(minutes=5)
