"""P6.2b.5: schema v6 registered; v5 -> v6 verified offline with the production mechanism.

Temporary journals only. The v5 fixture is built with the exact registered v1 - v5 definitions
(``MIGRATIONS[:5]``) through Johanna's services, then given v5-era rows in every v5 table, so
all 24 v1 - v5 tables hold related, non-empty history. The migration is an ordinary
``JournalStore.start()`` with the registered sequence, as at a backend start."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
from test_journal_store import raw
from test_journal_store import started as _started
from test_p2_schema_v5 import build_v4, pairs, snapshot

from johanna import journal_store as js
from johanna.evidence import EvidenceRegistry
from johanna.journal_store import DB_NAME, KEY_NAME, validate_pair
from johanna.notifications import InMemoryAdapter, NotificationCenter
from johanna.trade_links import TradeLinkService

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
POS = "c" * 64  # an HMAC-style position reference (no raw broker identifier)
T = "2026-11-18T13:00:30.000000Z"


def started(path, **kwargs):
    return _started(path, **kwargs)


def expected_schema(migrations) -> dict:
    conn = sqlite3.connect(":memory:")
    for _, sql in migrations:
        for statement in js.migration_statements(sql):
            conn.execute(statement)
    return {(t, n): s for t, n, s in conn.execute("SELECT type, name, sql FROM sqlite_master")}


def build_v5(path) -> dict:
    """A representative v5 journal: every v1 - v5 table populated, with relationships."""
    v4 = build_v4(path)
    store = started(path, migrations=V5, clock=v4["clock"])
    centre = NotificationCenter(store, (InMemoryAdapter(),), clock=v4["clock"])
    nid = centre.notify("information", "fixture-1", {"title": "Hello", "text": "Local only."})
    with store.write() as conn:
        trade_id = conn.execute("SELECT id FROM trade").fetchone()[0]
        decision_id = conn.execute("SELECT id FROM decision ORDER BY rowid").fetchone()[0]
        # v5-era delivery history (made without claims, as v5 code did): failed, then delivered
        conn.execute(
            "INSERT INTO notification_delivery VALUES ('ND1', ?, 'memory', 1, 'failed', "
            "'adapter_unavailable', ?)",
            (nid, T),
        )
        conn.execute(
            "INSERT INTO notification_delivery VALUES ('ND2', ?, 'memory', 2, 'delivered', "
            "NULL, ?)",
            (nid, T),
        )
        conn.execute(
            "INSERT INTO notification_state VALUES ('NS2', ?, 'available', 'johanna', ?)", (nid, T)
        )
        payload = json.dumps(
            {
                "positions": [{"position_ref": POS, "symbol": "EURUSDc", "type": 0, "volume": 0.1}],
                "orders": [],
                "deals": [],
            },
            sort_keys=True,
        )
        conn.execute(
            "INSERT INTO broker_observation VALUES ('OB1', ?, 'LIVE', NULL, ?, ?, 1, 0, 0, ?, "
            "'sha256:fixture')",
            (T, T, T, payload),
        )
        conn.execute(
            "INSERT INTO reconciliation_result VALUES ('RR1', 'OB1', 'trade', ?, ?, NULL, "
            "'CANDIDATE_MATCH', 'inferred', '{}', ?)",
            (trade_id, trade_id, T),
        )
        conn.execute(
            "INSERT INTO reconciliation_result VALUES ('RR2', 'OB1', 'broker_position', ?, NULL, "
            "?, 'UNRECORDED_POSITION', 'broker_confirmed', '{}', ?)",
            (POS, POS, T),
        )
        conn.execute(
            "INSERT INTO cost_policy_record(id, kind, timeframe, value, currency, unit, basis, "
            "instruments, status, record_json, record_fingerprint, broker_terms, lot_units, "
            "supersedes_id, actor, recorded_utc) VALUES ('CP1', 'holding', 'H1', 24, NULL, "
            "'hours', 'per_position', '[\"EURUSDc\"]', 'PROPOSED', '{}', 'fp-cp1', NULL, NULL, "
            "NULL, 'user', ?)",
            (T,),
        )
        conn.execute(
            "INSERT INTO cost_policy_decision VALUES ('CD1', 'CP1', 'REJECTED', 'fixture', "
            "NULL, 'user', ?)",
            (T,),
        )
        store.append_audit(
            conn, action="fixture.v5", table="meta", record_id=None, at=v4["clock"]()
        )
    keyed = store.broker_ref("position", 555001)
    store.stop()
    snap = snapshot(path)
    v5_tables = sorted(n for t, n in expected_schema(V5) if t == "table")
    empty = [t for t in v5_tables if not snap["rows"][t]]
    assert snap["version"] == 5 and not empty, empty
    return {
        **snap,
        "tables": v5_tables,
        "key": (path / KEY_NAME).read_bytes(),
        "keyed": keyed,
        "clock": v4["clock"],
        "trade_id": trade_id,
        "decision_id": decision_id,
        "notification_id": nid,
    }


def migrate(path, before, **kwargs):
    store = started(path, clock=before["clock"], **kwargs)  # registered sequence (v1 - v6)
    return store


def same_journal(path, before) -> bool:
    return snapshot(path) == {k: before[k] for k in ("objects", "rows", "version")}


# ====================================================================== registration
def test_registered_sequence_is_v1_to_v6_and_valid() -> None:
    assert [v for v, _ in js.MIGRATIONS] == [1, 2, 3, 4, 5, 6]
    assert js.MIGRATIONS[5] == (6, js.SCHEMA_V6)
    assert sum(1 for v, _ in js.MIGRATIONS if v == 6) == 1
    assert js.migration_sequence_valid(js.MIGRATIONS)
    assert not hasattr(js, "STAGED_MIGRATIONS")
    assert js.JournalStore("unused-path-never-opened").latest_version == 6


# ====================================================================== 4.1 schema
def test_v5_to_v6_schema_is_exactly_the_registered_schema(tmp_path) -> None:
    before = build_v5(tmp_path)
    store = migrate(tmp_path, before)
    assert store.ready and store.status()["schema_version"] == 6
    after = snapshot(tmp_path)
    assert after["version"] == 6
    for key, sql in before["objects"].items():  # every v1 - v5 object unchanged
        assert after["objects"].get(key) == sql, key
    added = {k for k in after["objects"] if k not in before["objects"]}
    real = {k for k in added if not k[1].startswith("sqlite_autoindex_")}
    assert {n for t, n in real if t == "table"} == V6_TABLES
    assert {n for t, n in real if t == "index"} == V6_INDEXES
    assert {n for t, n in real if t == "trigger"} == V6_TRIGGERS
    assert after["objects"] == expected_schema(js.MIGRATIONS)  # nothing unexpected anywhere
    assert before["objects"] == expected_schema(V5)


# ====================================================================== 4.2 preservation
def test_every_v5_row_key_and_audit_entry_is_preserved(tmp_path) -> None:
    before = build_v5(tmp_path)
    store = migrate(tmp_path, before)
    after = snapshot(tmp_path)
    for table in before["tables"]:
        assert after["rows"][table] == before["rows"][table], table
    assert all(after["rows"][t] == [] for t in V6_TABLES)
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with store.read() as conn:
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert store.verify_audit_chain()["valid"]
    assert (tmp_path / KEY_NAME).read_bytes() == before["key"]
    assert store.status()["key_status"] == "verified" and store.hmac_available
    assert store.broker_ref("position", 555001) == before["keyed"]
    assert len(store.keyed_digest("evidence.record", b"x")) == 64
    (pair,) = [p for p in pairs(tmp_path) if "premigration-v5" in p.name]
    assert validate_pair(pair) and snapshot(pair)["version"] == 5
    assert snapshot(pair)["rows"] == before["rows"]
    with closing(raw(tmp_path)) as conn:  # the backup's audit chain is an identical prefix
        live = [tuple(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY seq")]
    assert live[: len(before["rows"]["audit_log"])] == before["rows"]["audit_log"]


# ====================================================================== 4.3 v6 constraints
@pytest.fixture
def migrated(tmp_path):
    before = build_v5(tmp_path)
    store = migrate(tmp_path, before)
    conn = raw(tmp_path)
    conn.execute("PRAGMA foreign_keys = ON")
    yield store, conn, before
    conn.close()


def refused(conn, sql, params=()):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, params)


def test_notification_claims_and_delivered_once(migrated) -> None:
    store, conn, before = migrated
    nid = before["notification_id"]  # already delivered in the v5 era
    conn.execute("INSERT INTO notification_claim VALUES ('C1', ?, 'memory', 3, 'w', ?)", (nid, T))
    refused(conn, "INSERT INTO notification_claim VALUES ('C2', ?, 'memory', 3, 'w', ?)", (nid, T))
    refused(conn, "INSERT INTO notification_claim VALUES ('C3', 'NOPE', 'memory', 1, 'w', ?)", (T,))
    refused(conn, "INSERT INTO notification_claim VALUES ('C4', ?, 'memory', 0, 'w', ?)", (nid, T))
    refused(  # a second success for the v5-era delivered notification
        conn,
        "INSERT INTO notification_delivery VALUES ('D3', ?, 'memory', 3, 'delivered', NULL, ?)",
        (nid, T),
    )
    refused(  # an outcome without its claim
        conn,
        "INSERT INTO notification_delivery VALUES ('D4', ?, 'memory', 9, 'failed', 'x', ?)",
        (nid, T),
    )
    conn.execute(
        "INSERT INTO notification_delivery VALUES ('D5', ?, 'memory', 3, 'failed', 'x', ?)",
        (nid, T),
    )
    for sql in ("UPDATE notification_claim SET worker='x'", "DELETE FROM notification_claim"):
        refused(conn, sql)
    # the service works on the migrated journal: a new notification is delivered exactly once
    adapter = InMemoryAdapter()
    centre = NotificationCenter(store, (adapter,), clock=before["clock"])
    new = centre.notify("information", "after-migration", {"title": "Hi", "text": "Local."})
    assert centre.deliver_pending()["delivered"] == 1 and centre.deliver_pending()["delivered"] == 0
    assert len(adapter.delivered) == 1 and new


def test_trade_links_are_exclusive_and_revocable_once(migrated) -> None:
    store, conn, before = migrated
    tid = before["trade_id"]
    row = "INSERT INTO trade_broker_link VALUES (?, ?, ?, 'OB1', ?, ?, 'reason', ?, ?)"
    refused(conn, row, ("L0", "NOPE", POS, "CONFIRMED", None, "user", T))  # FK trade
    refused(conn, row, ("L0", tid, "short", "CONFIRMED", None, "user", T))  # reference length
    refused(conn, row, ("L0", tid, POS, "CONFIRMED", None, "johanna", T))  # user only
    refused(conn, row, ("L0", tid, POS, "REVOKED", None, "user", T))  # revocation needs target
    conn.execute(row, ("L1", tid, POS, "CONFIRMED", None, "user", T))
    refused(conn, row, ("L2", tid, "d" * 64, "CONFIRMED", None, "user", T))  # trade busy
    refused(conn, row, ("L3", "OTHER", POS, "CONFIRMED", None, "user", T))  # position busy/FK
    refused(conn, row, ("L4", tid, "d" * 64, "REVOKED", "L1", "user", T))  # wrong position
    conn.execute(row, ("L5", tid, POS, "REVOKED", "L1", "user", T))
    refused(conn, row, ("L6", tid, POS, "REVOKED", "L1", "user", T))  # revoke once
    conn.execute(row, ("L7", tid, POS, "CONFIRMED", None, "user", T))  # re-confirm after revoke
    for sql in ("UPDATE trade_broker_link SET reason='x'", "DELETE FROM trade_broker_link"):
        refused(conn, sql)
    links = TradeLinkService(store).links()["links"]
    assert [(r["id"], r["active"]) for r in links] == [("L1", False), ("L5", False), ("L7", True)]


def test_discrepancies_need_a_subject_key_and_are_unique(migrated) -> None:
    _, conn, before = migrated
    tid = before["trade_id"]
    row = "INSERT INTO reconciliation_discrepancy VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    conn.execute(row, ("X1", "OB1", "VOLUME_MISMATCH", tid, tid, None, "{}", T))
    refused(conn, row, ("X2", "OB1", "VOLUME_MISMATCH", tid, tid, None, "{}", T))  # unique
    refused(conn, row, ("X3", "OB1", "SYMBOL_MISMATCH", None, tid, None, "{}", T))  # NOT NULL
    refused(conn, row, ("X4", "OB1", "SYMBOL_MISMATCH", "other", tid, None, "{}", T))  # key
    refused(conn, row, ("X5", "OB1", "NOT_A_CODE", tid, tid, None, "{}", T))  # code
    refused(conn, row, ("X6", "OB1", "SYMBOL_MISMATCH", tid, tid, None, "[]", T))  # object
    refused(conn, row, ("X7", "NOPE", "SYMBOL_MISMATCH", tid, tid, None, "{}", T))  # FK obs
    conn.execute(row, ("X8", "OB1", "VOLUME_MISMATCH", POS, None, POS, "{}", T))  # by position
    for sql in (
        "UPDATE reconciliation_discrepancy SET code='X'",
        "DELETE FROM reconciliation_discrepancy",
    ):
        refused(conn, sql)


def test_evidence_is_immutable_correction_linked_and_links_are_checked(migrated) -> None:
    store, conn, before = migrated
    rec = (
        "INSERT INTO evidence_record VALUES (?, 'market_data', 'src', 'EURUSD', 'spread', "
        "'VERIFIED', ?, ?, ?, ?, ?, ?, ?, 'johanna', ?)"
    )
    mac = "f" * 64
    later = "2026-11-18T14:00:00.000000Z"
    conn.execute(rec, ("E1", T, later, "{}", "fp1", mac, None, None, T))
    refused(conn, rec, ("E2", T, later, "{}", "fp1", mac, None, None, T))  # fingerprint unique
    refused(conn, rec, ("E2", later, T, "{}", "fp2", mac, None, None, T))  # validity order
    refused(conn, rec, ("E2", T, None, "[]", "fp2", mac, None, None, T))  # object content
    refused(conn, rec, ("E2", T, None, "{}", "fp2", mac, "E1", None, T))  # correction reason
    refused(conn, rec, ("E2", T, None, "{}", "fp2", mac, "NOPE", "r", T))  # FK supersedes
    conn.execute(rec, ("E3", T, None, "{}", "fp3", mac, "E1", "fixed", T))
    refused(conn, rec, ("E4", T, None, "{}", "fp4", mac, "E1", "again", T))  # once
    for sql in ("UPDATE evidence_record SET status='UNVERIFIED'", "DELETE FROM evidence_record"):
        refused(conn, sql)
    link = "INSERT INTO evidence_link VALUES (?, ?, ?, ?, ?, ?, 'johanna', ?)"
    did = before["decision_id"]
    conn.execute(link, ("K1", "E3", "decision", did, did, None, T))
    refused(conn, link, ("K2", "E3", "decision", did, did, None, T))  # unique
    refused(conn, link, ("K3", "NOPE", "decision", did, did, None, T))  # FK evidence
    refused(conn, link, ("K4", "E3", "decision", "NOPE", "NOPE", None, T))  # FK decision
    refused(conn, link, ("K5", "E3", "risk_assessment", did, None, did, T))  # FK risk
    refused(conn, link, ("K6", "E3", "decision", did, None, did, T))  # subject consistency
    refused(conn, link, ("K7", "E3", "decision", "x", did, None, T))  # subject key
    for sql in ("UPDATE evidence_link SET actor='user'", "DELETE FROM evidence_link"):
        refused(conn, sql)
    # the service works on the migrated journal (keyed MAC verifies)
    registry = EvidenceRegistry(store, clock=lambda: datetime(2026, 11, 19, 9, tzinfo=UTC))
    made = registry.record(
        kind="readiness",
        source="post-migration check",
        topic="schema_version",
        status="VERIFIED",
        observed_utc=datetime(2026, 11, 19, 9, tzinfo=UTC),
        content={"schema_version": 6},
    )
    assert registry.verify(made["id"])["valid"] is True


def test_link_service_on_the_migrated_observation(migrated) -> None:
    store, _, before = migrated
    link_id = TradeLinkService(store).confirm(before["trade_id"], POS, "OB1", "mine")
    assert link_id and store.verify_audit_chain()["valid"]


# ====================================================================== 4.4 rollback
FAILURES = {
    "invalid_sql": js.SCHEMA_V6 + "\nCREATE TABLE broken (",
    "not_null": js.SCHEMA_V6 + "\nINSERT INTO evidence_record(id) VALUES ('x');",
    "check": js.SCHEMA_V6
    + "\nINSERT INTO reconciliation_discrepancy VALUES ('X', 'OB1', 'NOT_A_CODE', 'k', NULL, "
    "'k', '{}', 't');",
    "foreign_key": js.SCHEMA_V6
    + "\nINSERT INTO notification_claim VALUES ('C', 'NOPE', 'a', 1, 'w', 't');",
    "after_partial_schema": js.SCHEMA_V6.replace(
        "CREATE TABLE reconciliation_discrepancy (", "CREATE TABLE reconciliation_discrepancy ((", 1
    ),
}


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_induced_failures_roll_back_completely(tmp_path, failure) -> None:
    assert FAILURES[failure] != js.SCHEMA_V6
    before = build_v5(tmp_path)
    store = started(tmp_path, migrations=(*V5, (6, FAILURES[failure])), clock=before["clock"])
    assert store.status()["reason"] == "migration_failed"
    assert same_journal(tmp_path, before)  # v5, no v6 object, every row unchanged
    retry = migrate(tmp_path, before)
    assert retry.ready and retry.status()["schema_version"] == 6


@pytest.mark.parametrize(
    ("error", "reason"),
    [(sqlite3.OperationalError("disk full"), "migration_failed"), (RuntimeError("crash"), None)],
)
def test_failure_immediately_before_commit_rolls_back(tmp_path, error, reason) -> None:
    before = build_v5(tmp_path)

    def fault(step):
        if step == "before_commit_6":
            raise error

    store = migrate(tmp_path, before, fault=fault)
    assert store.status()["state"] == "refused"
    assert store.status()["reason"] == (reason or "unexpected_error")
    assert same_journal(tmp_path, before)
    retry = migrate(tmp_path, before)
    assert retry.ready and retry.status()["schema_version"] == 6


# ====================================================================== 4.5 refusals
def test_missing_key_is_refused_and_never_recreated(tmp_path) -> None:
    before = build_v5(tmp_path)
    (tmp_path / KEY_NAME).unlink()
    n = len(pairs(tmp_path))
    assert migrate(tmp_path, before).status()["reason"] == "migration_requires_verified_key"
    assert not (tmp_path / KEY_NAME).exists() and same_journal(tmp_path, before)
    assert len(pairs(tmp_path)) == n


def test_incorrect_key_is_refused_and_kept(tmp_path) -> None:
    before = build_v5(tmp_path)
    (tmp_path / KEY_NAME).write_bytes(b"\x09" * 32)
    assert migrate(tmp_path, before).status()["reason"] == "migration_requires_verified_key"
    assert (tmp_path / KEY_NAME).read_bytes() == b"\x09" * 32 and same_journal(tmp_path, before)


def test_corrupt_journal_is_refused_untouched(tmp_path) -> None:
    before = build_v5(tmp_path)
    garbage = b"garbage" * 600
    (tmp_path / DB_NAME).write_bytes(garbage)
    assert migrate(tmp_path, before).status()["reason"] == "corrupt_database"
    assert (tmp_path / DB_NAME).read_bytes() == garbage
    assert (tmp_path / KEY_NAME).read_bytes() == before["key"]


def test_invalid_audit_chain_blocks_the_migration(tmp_path) -> None:
    build_v5(tmp_path)
    with closing(raw(tmp_path)) as conn:
        conn.execute("DROP TRIGGER audit_no_update")
        conn.execute("UPDATE audit_log SET action='tampered' WHERE seq=3")
    tampered = snapshot(tmp_path)
    n = len(pairs(tmp_path))
    assert started(tmp_path).status()["reason"] == "backup_audit_invalid"
    assert snapshot(tmp_path) == tampered and len(pairs(tmp_path)) == n


def test_existing_foreign_key_violation_blocks_the_migration(tmp_path) -> None:
    build_v5(tmp_path)
    with closing(raw(tmp_path)) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("INSERT INTO trade_note VALUES ('ORPHAN', 'NO-TRADE', 'x', 't')")
    broken = snapshot(tmp_path)
    assert started(tmp_path).status()["reason"] == "migration_failed"
    assert snapshot(tmp_path) == broken


def test_unsupported_future_version_is_refused(tmp_path) -> None:
    build_v5(tmp_path)
    with closing(raw(tmp_path)) as conn:
        conn.execute("PRAGMA user_version = 7")
    future = snapshot(tmp_path)
    assert started(tmp_path).status()["reason"] == "schema_unsupported"
    assert snapshot(tmp_path) == future


@pytest.mark.parametrize(
    "sequence",
    [
        (*js.MIGRATIONS, (6, js.SCHEMA_V6)),  # v6 twice
        (*V5, (7, js.SCHEMA_V6)),  # gap
        (*js.MIGRATIONS[:4], (6, js.SCHEMA_V6)),  # v5 missing
    ],
)
def test_invalid_sequences_are_refused_before_any_backup(tmp_path, sequence) -> None:
    before = build_v5(tmp_path)
    n = len(pairs(tmp_path))
    assert started(tmp_path, migrations=sequence).status()["reason"] == "migration_order"
    assert same_journal(tmp_path, before) and len(pairs(tmp_path)) == n


def test_incompatible_notification_history_refuses_the_migration(tmp_path) -> None:
    before = build_v5(tmp_path)
    with closing(raw(tmp_path)) as conn:  # a second v5-era success for the same notification
        conn.execute(
            "INSERT INTO notification_delivery VALUES ('ND3', ?, 'test_other', 1, 'delivered', "
            "NULL, ?)",
            (before["notification_id"], T),
        )
    duplicated = snapshot(tmp_path)
    assert started(tmp_path).status()["reason"] == "migration_failed"
    assert snapshot(tmp_path) == duplicated and duplicated["version"] == 5


# ====================================================================== 4.6 idempotency
def test_migration_happens_once_and_restarts_change_nothing(tmp_path) -> None:
    before = build_v5(tmp_path)
    migrate(tmp_path, before).stop()
    migrated_once = snapshot(tmp_path)
    backups = [p.name for p in pairs(tmp_path)]
    assert sum("premigration-v5" in n for n in backups) == 1
    for _ in range(3):
        again = migrate(tmp_path, before)
        assert again.ready and again.status()["schema_version"] == 6
        again.stop()
    assert snapshot(tmp_path) == migrated_once
    assert [p.name for p in pairs(tmp_path)] == backups  # no new backup on a v6 journal
    assert len(migrated_once["objects"]) == len(expected_schema(js.MIGRATIONS))


def test_a_fresh_journal_is_created_directly_at_v6(tmp_path) -> None:
    store = started(tmp_path)
    assert store.ready and store.status()["schema_version"] == 6 and pairs(tmp_path) == []
    with closing(raw(tmp_path)) as conn:
        assert all(conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0] == 0 for t in V6_TABLES)
