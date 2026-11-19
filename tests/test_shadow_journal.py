"""P6.1 journal schema v3: the v2 -> v3 migration and its mandatory pre-migration backup.

Temporary directories only; nothing touches the real ``data/`` directory.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from risk_fixtures import BUY_LEVEL, PRODUCTION_COSTS, envelope, propose
from test_journal_store import Acl, raw, started

from johanna import journal_store as js
from johanna.decision_journal import DecisionJournal
from johanna.journal_store import DB_NAME, KEY_NAME, validate_pair
from johanna.trade_logbook import TradeLogbook

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
V2 = js.MIGRATIONS[:2]
V3 = js.MIGRATIONS[:3]  # these tests cover the P6.1 v2 -> v3 migration specifically


def _v2_journal(tmp_path) -> dict:
    """A v2 journal with decisions, a risk assessment, a trade and a link."""
    store = started(tmp_path, migrations=V2, clock=lambda: NOW)
    journal = DecisionJournal(store, clock=lambda: NOW)
    env = envelope(level=BUY_LEVEL)
    decision = journal.record_analysis(env, "analysis_request")
    journal.record_risk(propose(env=env, costs=PRODUCTION_COSTS), decision)
    other = envelope()
    journal.record_analysis(other, "risk_request")
    book = TradeLogbook(store, None, clock=lambda: NOW)
    book.create_trade(
        {
            "request_id": str(uuid.uuid4()),
            "symbol": "EURUSDc",
            "direction": "BUY",
            "entry": {
                "time_utc": (NOW - timedelta(hours=1)).isoformat(),
                "price": 1.1,
                "volume": 0.1,
            },
            "decision_id": decision,
        }
    )
    store.stop()
    with closing(raw(tmp_path)) as conn:
        tables = {
            t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")]
            for t in (
                "decision",
                "decision_observation",
                "risk_assessment",
                "audit_log",
                "trade",
                "trade_fill",
                "trade_decision_link",
                "meta",
            )
        }
    return {"tables": tables, "key": (tmp_path / KEY_NAME).read_bytes(), "decision": decision}


def _pairs(tmp_path):
    return [p for p in tmp_path.iterdir() if p.name.startswith("trading_journal.backup-")]


def test_v2_journal_migrates_to_v3_after_a_verified_backup(tmp_path) -> None:
    before = _v2_journal(tmp_path)
    store = started(tmp_path, migrations=V3, clock=lambda: NOW)
    assert store.ready and store.status()["schema_version"] == 3
    pairs = store.valid_pairs()
    assert len(pairs) == 1 and "premigration-v2" in pairs[0].name and validate_pair(pairs[0])
    manifest = json.loads((pairs[0] / "pair.json").read_text())
    assert manifest["schema_version"] == 2  # the snapshot's own version
    with closing(sqlite3.connect(pairs[0] / DB_NAME)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='evaluation_cycle'").fetchone()
            is None
        )
    assert (tmp_path / KEY_NAME).read_bytes() == before["key"]
    with closing(raw(tmp_path)) as conn:
        for table, rows in before["tables"].items():
            current = [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            assert current == rows, table  # every existing row preserved verbatim
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert {"evaluation_cycle", "gate_outcome", "shadow_scheduler_setting"} <= names
    assert {"decision_no_update", "decision_no_delete", "decision_key"} <= names
    assert store.verify_audit_chain()["valid"] is True
    journal = DecisionJournal(store, clock=lambda: NOW)
    detail = journal.get(before["decision"])["decision"]
    assert detail["execution_status"] == "MANUALLY_RECORDED"  # trade link still resolves
    assert detail["risk_assessments"][0]["risk_status"] == "UNVERIFIED"


def test_migrated_decision_table_keeps_its_protections(tmp_path) -> None:
    _v2_journal(tmp_path)
    store = started(tmp_path, clock=lambda: NOW)
    assert store.ready
    with closing(raw(tmp_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE decision SET symbol='X'")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM decision")
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO decision_observation VALUES ('01JZZZZZZZZZZZZZZZZZZZZZZZ','a','a',1)"
            )


def test_scheduled_capture_source_is_accepted_and_others_are_rejected(tmp_path) -> None:
    store = started(tmp_path, clock=lambda: NOW)
    journal = DecisionJournal(store, clock=lambda: NOW)
    decision = journal.record_analysis(envelope(level=BUY_LEVEL), "scheduled_evaluation")
    assert journal.get(decision)["decision"]["capture_source"] == "scheduled_evaluation"
    with pytest.raises(ValueError):
        journal.record_analysis(envelope(), "background")
    with closing(raw(tmp_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO decision VALUES ('D9','EURUSD','EURUSDc','H1','t','NO_TRADE','R1',"
                "'NONE','{}','fp9',NULL,NULL,NULL,'bogus','t')"
            )


def test_scheduled_capture_keeps_fingerprint_deduplication(tmp_path) -> None:
    store = started(tmp_path, clock=lambda: NOW)
    journal = DecisionJournal(store, clock=lambda: NOW)
    env = envelope(level=BUY_LEVEL)
    first = journal.record_analysis(env, "scheduled_evaluation")
    second = journal.record_analysis(env, "analysis_request")
    assert first == second
    assert journal.get(first)["decision"]["observations"] == 2


def test_backup_failure_blocks_the_v3_migration(tmp_path) -> None:
    _v2_journal(tmp_path)
    store = js.JournalStore(
        tmp_path,
        acl=Acl(),
        acl_check=Acl(),
        clock=lambda: NOW,
        fault=lambda step: (
            (_ for _ in ()).throw(RuntimeError("x")) if step == "backup_after_snapshot" else None
        ),
    )
    store.start()
    assert store.status()["reason"] == "backup_failed"
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert _pairs(tmp_path) == []


def test_missing_key_blocks_the_v3_migration(tmp_path) -> None:
    _v2_journal(tmp_path)
    (tmp_path / KEY_NAME).unlink()
    store = started(tmp_path, clock=lambda: NOW)
    assert store.status()["reason"] == "migration_requires_verified_key"
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert not (tmp_path / KEY_NAME).exists()  # never regenerated


def test_tampered_audit_chain_blocks_the_backup_and_the_migration(tmp_path) -> None:
    _v2_journal(tmp_path)
    with closing(raw(tmp_path)) as conn:
        conn.execute("DROP TRIGGER audit_no_update")
        conn.execute("UPDATE audit_log SET action='tampered' WHERE seq=2")
    store = started(tmp_path, clock=lambda: NOW)
    assert store.status()["reason"] == "backup_audit_invalid"
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    assert _pairs(tmp_path) == []


def test_failed_v3_migration_preserves_v2(tmp_path) -> None:
    before = _v2_journal(tmp_path)
    broken = (*V2, (3, js.SCHEMA_V3 + "\nCREATE TABLE broken ("))
    store = started(tmp_path, migrations=broken, clock=lambda: NOW)
    assert store.status()["reason"] == "migration_failed"
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='evaluation_cycle'").fetchone()
            is None
        )
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='decision_v3'").fetchone()
            is None
        )
        rows = [tuple(r) for r in conn.execute("SELECT * FROM decision ORDER BY rowid")]
        assert rows == before["tables"]["decision"]
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        assert {"decision_no_update", "decision_no_delete"} <= names
    assert len(_pairs(tmp_path)) == 1  # the verified backup remains
    retry = started(tmp_path, migrations=V3, clock=lambda: NOW)
    assert retry.ready and retry.status()["schema_version"] == 3


def test_a_migration_that_violates_a_foreign_key_is_rolled_back(tmp_path) -> None:
    _v2_journal(tmp_path)
    bad = (*V2, (3, "INSERT INTO decision_observation VALUES ('NOPE','a','a',1);"))
    store = started(tmp_path, migrations=bad, clock=lambda: NOW)
    assert store.status()["reason"] == "migration_failed"
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            conn.execute(
                "SELECT count(*) FROM decision_observation WHERE decision_id='NOPE'"
            ).fetchone()[0]
            == 0
        )


def test_a_migration_may_not_commit_on_its_own(tmp_path) -> None:
    _v2_journal(tmp_path)
    sneaky = (*V2, (3, "CREATE TABLE sneaky (id INTEGER); COMMIT;"))
    store = started(tmp_path, migrations=sneaky, clock=lambda: NOW)
    assert store.status()["reason"] == "migration_failed"


def test_fresh_installation_has_the_scheduler_disabled(tmp_path) -> None:
    store = started(tmp_path, clock=lambda: NOW)
    assert store.status()["schema_version"] == js.MIGRATIONS[-1][0]  # latest (4 since P6.2a)
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM shadow_scheduler_setting").fetchone()[0] == 0


def test_migration_never_enables_the_scheduler(tmp_path) -> None:
    _v2_journal(tmp_path)
    started(tmp_path, clock=lambda: NOW)
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM shadow_scheduler_setting").fetchone()[0] == 0
