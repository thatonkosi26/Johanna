"""P6.2a journal schema v4: the additive v3 -> v4 migration (shadow_intent) and its mandatory
pre-migration backup. Temporary directories only; the production journal is never opened."""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import timedelta

from risk_fixtures import BUY_LEVEL, PRODUCTION_COSTS, envelope, propose
from test_journal_store import raw, started
from test_shadow_evaluation import MIDNIGHT, Analysis, Clock, Risk, no_trade

from johanna import journal_store as js
from johanna.adapters.mt5 import ServerClock
from johanna.decision_journal import DecisionJournal
from johanna.journal_store import DB_NAME, KEY_NAME, migration_statements, validate_pair
from johanna.shadow_evaluation import ShadowEvaluationService

V3 = js.MIGRATIONS[:3]
V4 = js.MIGRATIONS[:4]  # Phase 2: v5 is registered; these tests cover the v3 -> v4 step
TABLES = (
    "meta",
    "decision",
    "decision_observation",
    "risk_assessment",
    "risk_assessment_observation",
    "audit_log",
    "trade",
    "trade_fill",
    "trade_decision_link",
    "shadow_scheduler_setting",
    "evaluation_cycle",
    "gate_outcome",
)


def _v3_journal(tmp_path) -> dict:
    """A v3 journal with decisions, a risk assessment, scheduler state, cycles and gates."""
    clock = Clock(MIDNIGHT - timedelta(minutes=5))
    store = started(tmp_path, migrations=V3, clock=clock)
    journal = DecisionJournal(store, clock=clock)
    env = envelope(level=BUY_LEVEL)
    decision = journal.record_analysis(env, "analysis_request")
    journal.record_risk(propose(env=env, costs=PRODUCTION_COSTS), decision)
    analysis = Analysis(no_trade())
    analysis.clock = clock
    service = ShadowEvaluationService(store, journal, analysis, Risk(), ServerClock(), clock=clock)
    service.set_enabled(True)
    clock.now = MIDNIGHT + timedelta(seconds=30)
    service.run_once()  # nine NO_TRADE windows completed on v3
    service.set_enabled(False)
    store.stop()
    with closing(raw(tmp_path)) as conn:
        tables = {
            t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in TABLES
        }
    assert len(tables["evaluation_cycle"]) == 9 and tables["gate_outcome"]
    return {"tables": tables, "key": (tmp_path / KEY_NAME).read_bytes()}


def _version(tmp_path) -> int:
    with closing(raw(tmp_path)) as conn:
        return conn.execute("PRAGMA user_version").fetchone()[0]


def _pairs(tmp_path):
    return [p for p in tmp_path.iterdir() if p.name.startswith("trading_journal.backup-")]


def test_v3_journal_migrates_to_v4_after_a_verified_backup(tmp_path) -> None:
    before = _v3_journal(tmp_path)
    store = started(tmp_path, migrations=V4)
    assert store.ready and store.status()["schema_version"] == 4
    pairs = store.valid_pairs()
    assert len(pairs) == 1 and "premigration-v3" in pairs[0].name and validate_pair(pairs[0])
    assert json.loads((pairs[0] / "pair.json").read_text())["schema_version"] == 3
    with closing(sqlite3.connect(pairs[0] / DB_NAME)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 3
    assert (tmp_path / KEY_NAME).read_bytes() == before["key"]  # key preserved
    assert (pairs[0] / KEY_NAME).read_bytes() == before["key"]
    with closing(raw(tmp_path)) as conn:
        for table, rows in before["tables"].items():
            current = [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")]
            assert current == rows, table  # every v3 row preserved verbatim
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        assert conn.execute("SELECT count(*) FROM shadow_intent").fetchone()[0] == 0
    assert {
        "shadow_intent",
        "shadow_intent_by_key",
        "shadow_intent_by_eligibility",
        "shadow_intent_links",
        "shadow_intent_no_update",
        "shadow_intent_no_delete",
    } <= names
    assert store.verify_audit_chain()["valid"] is True


def test_migration_keeps_the_scheduler_state(tmp_path) -> None:
    before = _v3_journal(tmp_path)
    started(tmp_path)
    with closing(raw(tmp_path)) as conn:
        rows = [tuple(r) for r in conn.execute("SELECT * FROM shadow_scheduler_setting")]
        latest = conn.execute(
            "SELECT enabled FROM shadow_scheduler_setting ORDER BY rowid DESC LIMIT 1"
        ).fetchone()[0]
    assert rows == before["tables"]["shadow_scheduler_setting"] and latest == 0  # still OFF


def test_missing_key_blocks_the_v4_migration_and_is_never_regenerated(tmp_path) -> None:
    _v3_journal(tmp_path)
    (tmp_path / KEY_NAME).unlink()
    store = started(tmp_path)
    assert store.status()["reason"] == "migration_requires_verified_key"
    assert _version(tmp_path) == 3 and not (tmp_path / KEY_NAME).exists()
    assert _pairs(tmp_path) == []


def test_corrupt_key_blocks_the_v4_migration(tmp_path) -> None:
    _v3_journal(tmp_path)
    (tmp_path / KEY_NAME).write_bytes(b"\x00" * 32)
    store = started(tmp_path)
    assert store.status()["reason"] == "migration_requires_verified_key"
    assert store.status()["key_status"] == "mismatch"
    assert _version(tmp_path) == 3 and (tmp_path / KEY_NAME).read_bytes() == b"\x00" * 32
    assert _pairs(tmp_path) == []


def test_corrupt_journal_is_refused_and_left_untouched(tmp_path) -> None:
    _v3_journal(tmp_path)
    for sidecar in ("-wal", "-shm"):
        (tmp_path / f"{DB_NAME}{sidecar}").unlink(missing_ok=True)
    garbage = b"not a database" * 100
    (tmp_path / DB_NAME).write_bytes(garbage)
    store = started(tmp_path)
    assert store.status()["state"] == "refused"
    assert store.status()["reason"] == "corrupt_database"
    assert (tmp_path / DB_NAME).read_bytes() == garbage  # never recreated or quarantined
    assert _pairs(tmp_path) == []


def test_failed_v4_migration_rolls_back_and_preserves_v3(tmp_path) -> None:
    before = _v3_journal(tmp_path)
    broken = (*V3, (4, js.SCHEMA_V4 + "\nCREATE TABLE broken ("))
    store = started(tmp_path, migrations=broken)
    assert store.status()["reason"] == "migration_failed"
    assert _version(tmp_path) == 3
    with closing(raw(tmp_path)) as conn:
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='shadow_intent'").fetchone()
            is None
        )
        for table, rows in before["tables"].items():
            assert [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")] == rows
    assert len(_pairs(tmp_path)) == 1  # the verified backup remains
    retry = started(tmp_path, migrations=V4)
    assert retry.ready and retry.status()["schema_version"] == 4


def test_a_v4_migration_that_violates_a_foreign_key_is_rolled_back(tmp_path) -> None:
    _v3_journal(tmp_path)
    bad = (
        *V3,
        (4, js.SCHEMA_V4 + "\nINSERT INTO decision_observation VALUES ('NOPE','a','a',1);"),
    )
    store = started(tmp_path, migrations=bad)
    assert store.status()["reason"] == "migration_failed"
    assert _version(tmp_path) == 3
    with closing(raw(tmp_path)) as conn:
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='shadow_intent'").fetchone()
            is None
        )


def test_tampered_audit_chain_blocks_the_v4_migration(tmp_path) -> None:
    _v3_journal(tmp_path)
    with closing(raw(tmp_path)) as conn:
        conn.execute("DROP TRIGGER audit_no_update")
        conn.execute("UPDATE audit_log SET action='tampered' WHERE seq=2")
    store = started(tmp_path, migrations=V4)
    assert store.status()["reason"] == "backup_audit_invalid"
    assert _version(tmp_path) == 3 and _pairs(tmp_path) == []


def test_schema_v4_is_purely_additive() -> None:
    statements = migration_statements(js.SCHEMA_V4)
    for statement in statements:
        head = " ".join(statement.split()[:3]).upper()
        assert head.startswith(
            (
                "CREATE TABLE SHADOW_INTENT",
                "CREATE INDEX SHADOW_INTENT",
                "CREATE TRIGGER SHADOW_INTENT",
            )
        ), head
    # every statement creates a shadow_intent object (above); nothing is dropped or altered
    assert not re.search(r"(DROP|ALTER|RENAME)", js.SCHEMA_V4.upper())
    assert js.MIGRATIONS[3] == (4, js.SCHEMA_V4) and js.MIGRATIONS[:3] == V3


def test_fresh_installation_is_latest_with_the_scheduler_off(tmp_path) -> None:
    store = started(tmp_path)
    assert store.status()["schema_version"] == js.MIGRATIONS[-1][0] == 6  # v6 since P6.2b.5
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM shadow_scheduler_setting").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM shadow_intent").fetchone()[0] == 0
