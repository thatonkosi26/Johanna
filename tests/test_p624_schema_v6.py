"""P6.2b.4 staged schema v6: unregistered; v5 -> v6 verified on temporary journals only."""

from __future__ import annotations

import sqlite3
from contextlib import closing

import pytest
from test_journal_store import raw, started
from test_p2_schema_v5 import build_v4, pairs, snapshot

from johanna import journal_store as js
from johanna.journal_store import DB_NAME, KEY_NAME, migration_statements, validate_pair
from johanna.notifications import InMemoryAdapter, NotificationCenter

V6 = js.MIGRATIONS  # registered in P6.2b.5
V5 = js.MIGRATIONS[:5]
V6_TABLES = {
    "notification_claim",
    "trade_broker_link",
    "reconciliation_discrepancy",
    "evidence_record",
    "evidence_link",
}
V6_INDEXES = {
    "notification_claim_by_notification",
    "notification_delivered_once",
    "trade_broker_link_by_trade",
    "trade_broker_link_by_position",
    "reconciliation_discrepancy_by_trade",
    "evidence_record_by_topic",
    "evidence_link_by_subject",
}
V6_TRIGGERS = {
    "notification_claim_no_update",
    "notification_claim_no_delete",
    "notification_delivery_needs_claim",
    "trade_broker_link_exclusive",
    "trade_broker_link_revocation",
    "trade_broker_link_no_update",
    "trade_broker_link_no_delete",
    "reconciliation_discrepancy_no_update",
    "reconciliation_discrepancy_no_delete",
    "evidence_record_no_update",
    "evidence_record_no_delete",
    "evidence_link_no_update",
    "evidence_link_no_delete",
}


def build_v5(path) -> dict:
    """The Phase 2 representative journal, migrated to v5 and given v5 rows."""
    before = build_v4(path)
    store = started(path, migrations=V5, clock=before["clock"])  # the v5 journal
    c = NotificationCenter(store, (InMemoryAdapter(),), clock=before["clock"])
    assert c.notify("information", "n1", {"title": "Hello", "text": "Local only."})
    with store.write() as conn:
        conn.execute(
            "INSERT INTO broker_observation(id, observed_utc, status, reason, payload, "
            "payload_fingerprint) VALUES ('O1', 't', 'UNAVAILABLE', 'fixture', '{}', 'fp')"
        )
    store.stop()
    snap = snapshot(path)
    assert snap["version"] == 5
    return {**snap, "key": (path / KEY_NAME).read_bytes(), "clock": before["clock"]}


def test_v6_is_registered_once_after_v5() -> None:
    assert [v for v, _ in js.MIGRATIONS] == [1, 2, 3, 4, 5, 6]  # P6.2b.5
    assert js.MIGRATIONS[5] == (6, js.SCHEMA_V6)
    assert not hasattr(js, "STAGED_MIGRATIONS")


def test_v6_is_purely_additive() -> None:
    for statement in migration_statements(js.SCHEMA_V6):
        head = " ".join(statement.split()[:2]).upper()
        assert head in ("CREATE TABLE", "CREATE INDEX", "CREATE TRIGGER", "CREATE UNIQUE"), head
    upper = js.SCHEMA_V6.upper()
    assert "DROP " not in upper and "ALTER " not in upper and "RENAME" not in upper


def test_v5_to_v6_preserves_everything_and_adds_exactly_the_v6_objects(tmp_path) -> None:
    before = build_v5(tmp_path)
    store = started(tmp_path, migrations=V6, clock=before["clock"])
    assert store.ready and store.status()["schema_version"] == 6
    after = snapshot(tmp_path)
    for table, rows in before["rows"].items():
        assert after["rows"][table] == rows, table
    for key, sql in before["objects"].items():
        assert after["objects"].get(key) == sql, key
    added = {k for k in after["objects"] if k not in before["objects"]}
    real = {k for k in added if not k[1].startswith("sqlite_autoindex_")}
    assert {n for t, n in real if t == "table"} == V6_TABLES
    assert {n for t, n in real if t == "index"} == V6_INDEXES
    assert {n for t, n in real if t == "trigger"} == V6_TRIGGERS
    assert all(after["rows"][t] == [] for t in V6_TABLES)
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert store.verify_audit_chain()["valid"]
    assert (tmp_path / KEY_NAME).read_bytes() == before["key"]
    v5_pair = [p for p in pairs(tmp_path) if "premigration-v5" in p.name]
    assert len(v5_pair) == 1 and validate_pair(v5_pair[0])
    assert snapshot(v5_pair[0])["rows"] == before["rows"]


FAILURES = {
    "syntax_at_end": js.SCHEMA_V6 + "\nCREATE TABLE broken (",
    "constraint_at_end": js.SCHEMA_V6 + "\nINSERT INTO evidence_record(id) VALUES ('x');",
    "fk_violation": js.SCHEMA_V6
    + "\nINSERT INTO notification_claim VALUES ('C', 'NOPE', 'a', 1, 'w', 't');",
    "middle": js.SCHEMA_V6.replace(
        "CREATE TABLE evidence_record (", "CREATE TABLE evidence_record ((", 1
    ),
}


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_failed_v6_migration_rolls_back_and_a_retry_succeeds(tmp_path, failure) -> None:
    before = build_v5(tmp_path)
    store = started(tmp_path, migrations=(*V5, (6, FAILURES[failure])))
    assert store.status()["reason"] == "migration_failed"
    after = snapshot(tmp_path)
    assert after == {k: before[k] for k in ("objects", "rows", "version")}
    retry = started(tmp_path, migrations=V6)
    assert retry.ready and retry.status()["schema_version"] == 6


def test_existing_duplicate_deliveries_make_the_migration_refuse(tmp_path) -> None:
    """A journal holding two successful deliveries of one notification (possible before v6)
    cannot get the delivered-once index: the migration refuses and changes nothing."""
    before = build_v5(tmp_path)
    with closing(raw(tmp_path)) as conn:
        nid = conn.execute("SELECT id FROM notification").fetchone()[0]
        for i in (1, 2):
            conn.execute(
                "INSERT INTO notification_delivery VALUES (?, ?, 'memory', ?, 'delivered', "
                "NULL, 't')",
                (f"D{i}", nid, i),
            )
    duplicated = snapshot(tmp_path)
    store = started(tmp_path, migrations=V6)
    assert store.status()["reason"] == "migration_failed"
    assert snapshot(tmp_path) == duplicated and duplicated["version"] == before["version"]


def test_v6_idempotency_and_refusals(tmp_path) -> None:
    build_v5(tmp_path)
    started(tmp_path, migrations=V6).stop()
    migrated = snapshot(tmp_path)
    n_pairs = len(pairs(tmp_path))
    for _ in range(2):
        again = started(tmp_path, migrations=V6)
        assert again.ready
        again.stop()
    assert snapshot(tmp_path) == migrated and len(pairs(tmp_path)) == n_pairs
    # code that registers only v1 - v5 refuses a v6 journal instead of using it
    assert started(tmp_path, migrations=V5).status()["reason"] == "schema_unsupported"


@pytest.mark.parametrize("case", ["missing_key", "wrong_key", "corrupt", "future_version"])
def test_v6_refusal_cases_change_nothing(tmp_path, case) -> None:
    build_v5(tmp_path)
    if case == "missing_key":
        (tmp_path / KEY_NAME).unlink()
        reason = "migration_requires_verified_key"
    elif case == "wrong_key":
        (tmp_path / KEY_NAME).write_bytes(b"\x05" * 32)
        reason = "migration_requires_verified_key"
    elif case == "corrupt":
        (tmp_path / DB_NAME).write_bytes(b"x" * 4096)
        reason = "corrupt_database"
    else:
        with closing(raw(tmp_path)) as conn:
            conn.execute("PRAGMA user_version = 7")
        reason = "schema_unsupported"
    data = (tmp_path / DB_NAME).read_bytes()
    n_pairs = len(pairs(tmp_path))
    store = started(tmp_path, migrations=V6)
    assert store.status()["reason"] == reason
    assert (tmp_path / DB_NAME).read_bytes() == data and len(pairs(tmp_path)) == n_pairs


def test_readiness_reports_v6_as_registered_and_installed(tmp_path) -> None:
    from test_decision_journal import _app  # noqa: PLC0415

    from johanna.operations import readiness  # noqa: PLC0415

    app = _app(tmp_path)
    app.start()
    try:
        journal = readiness(app)["journal"]
        assert journal["installed_schema_version"] == 6 == journal["registered_schema_version"]
        assert journal["staged_schema"].startswith("none staged")
    finally:
        app.stop()


def test_v6_tables_are_append_only(tmp_path) -> None:
    started(tmp_path, migrations=V6).stop()
    with closing(raw(tmp_path)) as conn:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND (name LIKE '%no_update' "
                "OR name LIKE '%no_delete')"
            )
        ]
    for table in V6_TABLES:
        assert f"{table}_no_update" in names and f"{table}_no_delete" in names, table
    with closing(raw(tmp_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO reconciliation_discrepancy VALUES ('X', 'NOPE', 'NOT_A_CODE', 'k', "
                "NULL, 'k', '{}', 't')"
            )
