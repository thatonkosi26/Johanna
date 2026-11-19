"""Phase 2: schema v5 registered in MIGRATIONS; offline v4 -> v5 verification.

Temporary SQLite journals only (pytest ``tmp_path``); no production file is read, copied or
used as a template. The v4 fixture is built through Johanna's own services (decision journal,
shadow scheduler and intents, trade logbook) so that every one of the 17 v4 tables holds
representative rows, relationships, audit entries and a keyed (HMAC) reference.
"""

from __future__ import annotations

import hashlib
import sqlite3
import uuid
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from risk_fixtures import envelope, propose, synthetic_terms
from test_journal_store import raw
from test_journal_store import started as _started
from test_shadow_evaluation import CANDIDATE_UNVERIFIED, RISK_UNVERIFIED, Analysis, Clock
from test_shadow_intents import H13, KeyedRisk

from johanna import journal_store as js
from johanna.adapters.mt5 import ServerClock
from johanna.decision_journal import DecisionJournal
from johanna.journal_store import DB_NAME, KEY_NAME, JournalStore, validate_pair
from johanna.shadow_evaluation import ShadowEvaluationService
from johanna.trade_logbook import TradeLogbook

V4 = js.MIGRATIONS[:4]
V5 = js.MIGRATIONS[:5]


def started(path, **kwargs):
    """These tests cover the v4 -> v5 step: the sequence as registered in Phase 2 (v1 - v5).
    v6 was registered later (P6.2b.5) and is verified in ``test_p625_schema_v6.py``."""
    kwargs.setdefault("migrations", V5)
    return _started(path, **kwargs)


V4_TABLES = (
    "audit_log",
    "decision",
    "decision_observation",
    "evaluation_cycle",
    "event",
    "gate_outcome",
    "meta",
    "risk_assessment",
    "risk_assessment_observation",
    "shadow_intent",
    "shadow_scheduler_setting",
    "trade",
    "trade_decision_link",
    "trade_fill",
    "trade_note",
    "trade_protection",
    "write_request",
)
V5_TABLES = {
    "cost_policy_record",
    "cost_policy_decision",
    "broker_observation",
    "reconciliation_result",
    "notification",
    "notification_state",
    "notification_delivery",
}
V5_INDEXES = {
    "cost_policy_record_by_kind",
    "cost_policy_decision_by_record",
    "broker_observation_by_time",
    "reconciliation_result_by_trade",
}
V5_TRIGGERS = {
    "cost_policy_record_no_update",
    "cost_policy_record_no_delete",
    "cost_policy_decision_terminal",
    "cost_policy_decision_no_update",
    "cost_policy_decision_no_delete",
    "broker_observation_no_update",
    "broker_observation_no_delete",
    "reconciliation_result_no_update",
    "reconciliation_result_no_delete",
    "notification_no_update",
    "notification_no_delete",
    "notification_state_order",
    "notification_state_no_update",
    "notification_state_no_delete",
    "notification_delivery_no_update",
    "notification_delivery_no_delete",
}
V5_COLUMNS = {
    "cost_policy_record": [
        "id",
        "kind",
        "timeframe",
        "value",
        "currency",
        "unit",
        "basis",
        "instruments",
        "status",
        "record_json",
        "record_fingerprint",
        "broker_terms",
        "lot_units",
        "supersedes_id",
        "actor",
        "recorded_utc",
    ],
    "reconciliation_result": [
        "id",
        "observation_id",
        "subject",
        "subject_key",
        "trade_id",
        "position_ref",
        "state",
        "basis",
        "detail",
        "recorded_utc",
    ],
}
# Released migration texts (v1 - v4 unchanged; v5 registered in Phase 2). Never edited.
RELEASED = {
    1: "SCHEMA_V1",
    2: "SCHEMA_V2",
    3: "SCHEMA_V3",
    4: "SCHEMA_V4",
    5: "SCHEMA_V5",
    6: "SCHEMA_V6",
}


# ====================================================================== fixtures
def snapshot(path: Path) -> dict:
    """Every table's rows, every schema object's SQL and the schema version."""
    with closing(raw(path)) as conn:
        objects = {
            (r["type"], r["name"]): r["sql"]
            for r in conn.execute("SELECT type, name, sql FROM sqlite_master")
        }
        tables = sorted(n for t, n in objects if t == "table" and not n.startswith("sqlite_"))
        rows = {
            t: [tuple(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables
        }
        version = conn.execute("PRAGMA user_version").fetchone()[0]
    return {"objects": objects, "rows": rows, "version": version}


def build_v4(path: Path) -> dict:
    """A representative v4 journal: every v4 table populated through Johanna's services."""
    clock = Clock(H13 - timedelta(minutes=2))
    store = started(path, migrations=V4, clock=clock)
    journal = DecisionJournal(store, clock=clock)
    analysis, risk = Analysis(), KeyedRisk()
    analysis.clock = clock
    analysis.env, risk.result = CANDIDATE_UNVERIFIED, RISK_UNVERIFIED
    shadow = ShadowEvaluationService(store, journal, analysis, risk, ServerClock(), clock=clock)
    shadow.set_enabled(True)  # shadow_scheduler_setting (+ audit)
    clock.now = H13 + timedelta(seconds=30)
    shadow.run_once()  # evaluation_cycle, gate_outcome, shadow_intent, decisions, risk
    shadow.set_enabled(False)  # the scheduler is left OFF, as in production
    env = envelope(evaluated_at=clock.now)
    decision = journal.record_analysis(env, "analysis_request")
    journal.record_risk(propose(env=envelope(), costs=synthetic_terms()), decision)
    book = TradeLogbook(store, None, clock=clock)
    status, created = book.create_trade(
        {
            "request_id": str(uuid.uuid4()),
            "symbol": "EURUSDc",
            "direction": "BUY",
            "entry": {
                "time_utc": (clock.now - timedelta(minutes=30)).isoformat(),
                "price": 1.1,
                "volume": 0.1,
            },
            "decision_id": decision,  # trade_decision_link (foreign keys to decision/trade)
        }
    )
    assert status == 201, created
    trade_id = created["trade_id"]
    protection = {
        "request_id": str(uuid.uuid4()),
        "stop_loss": 1.09,
        "effective_utc": (clock.now - timedelta(minutes=20)).isoformat(),
    }
    assert book.add_protection(trade_id, protection)[0] == 201
    assert book.add_note(trade_id, {"request_id": str(uuid.uuid4()), "text": "fixture"})[0] == 201
    with store.write() as conn:  # event: no service writes it yet; a representative row
        conn.execute(
            "INSERT INTO event(id, type, subject_ref, dedup_key, severity, payload, "
            "occurred_utc, recorded_utc) VALUES ('EV1', 'fixture', NULL, 'fixture-1', 'info', "
            "'{}', ?, ?)",
            (js.utc_iso(clock.now), js.utc_iso(clock.now)),
        )
    keyed = store.broker_ref("position", 555001)  # keyed reference before the migration
    store.stop()
    snap = snapshot(path)
    empty = [t for t in V4_TABLES if not snap["rows"][t]]
    assert snap["version"] == 4 and not empty, empty
    assert sorted(snap["rows"]) == sorted(V4_TABLES)
    return {
        **snap,
        "key": (path / KEY_NAME).read_bytes(),
        "keyed": keyed,
        "clock": clock,
        "trade_id": trade_id,
    }


def pairs(path: Path) -> list[Path]:
    return sorted(p for p in path.iterdir() if p.name.startswith("trading_journal.backup-"))


# ====================================================================== A + B. success
def test_full_v4_journal_migrates_to_v5_and_preserves_everything(tmp_path) -> None:
    before = build_v4(tmp_path)
    store = started(tmp_path, clock=before["clock"])  # the REGISTERED sequence (default)
    assert store.ready and store.status()["schema_version"] == 5
    assert store.status()["key_status"] == "verified" and store.hmac_available
    after = snapshot(tmp_path)
    assert after["version"] == 5
    # B: every pre-existing row preserved, value for value, in the same order
    for table in V4_TABLES:
        assert after["rows"][table] == before["rows"][table], table
    # B: no v4 object (table, index, trigger) altered or removed
    for key, sql in before["objects"].items():
        assert after["objects"].get(key) == sql, key
    # A: exactly the expected v5 objects were added, and v5 tables start empty
    added = {k for k in after["objects"] if k not in before["objects"]}
    auto = {k for k in added if k[1].startswith("sqlite_autoindex_")}
    assert {n for t, n in added - auto if t == "table"} == V5_TABLES
    assert {n for t, n in added - auto if t == "index"} == V5_INDEXES
    assert {n for t, n in added - auto if t == "trigger"} == V5_TRIGGERS
    assert all(not after["rows"][t] for t in V5_TABLES)
    with closing(raw(tmp_path)) as conn:
        for table, columns in V5_COLUMNS.items():
            assert [r[1] for r in conn.execute(f"PRAGMA table_info({table})")] == columns
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    with store.read() as conn:  # foreign keys enforced on the journal's own connections
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    # B: audit history intact and the chain valid; key and key check unchanged
    assert store.verify_audit_chain()["valid"] is True
    assert (tmp_path / KEY_NAME).read_bytes() == before["key"]
    assert store.broker_ref("position", 555001) == before["keyed"]  # same keyed reference
    # the verified pre-migration backup holds the untouched v4 journal and the same key
    (pair,) = pairs(tmp_path)
    assert "premigration-v4" in pair.name and validate_pair(pair)
    assert (pair / KEY_NAME).read_bytes() == before["key"]
    assert snapshot(pair)["rows"] == before["rows"] and snapshot(pair)["version"] == 4


def test_row_counts_before_and_after_are_identical(tmp_path) -> None:
    before = build_v4(tmp_path)
    started(tmp_path, clock=before["clock"]).stop()
    after = snapshot(tmp_path)
    counts = {t: (len(before["rows"][t]), len(after["rows"][t])) for t in V4_TABLES}
    assert all(b == a and b > 0 for b, a in counts.values()), counts


def test_append_only_protections_remain_effective_after_migration(tmp_path) -> None:
    before = build_v4(tmp_path)
    started(tmp_path, clock=before["clock"]).stop()
    with closing(raw(tmp_path)) as conn:
        guards = conn.execute(
            "SELECT name, tbl_name FROM sqlite_master WHERE type='trigger' "
            "AND (name LIKE '%no_update' OR name LIKE '%no_delete')"
        ).fetchall()
        assert len(guards) >= 2 * len(V4_TABLES) - 6  # every v4 table has a delete guard
        for name, table in guards:
            if table in V5_TABLES:
                continue  # v5 tables are empty here; covered by their own feature tests
            first = conn.execute(f"PRAGMA table_info({table})").fetchone()[1]
            statement = (
                f"UPDATE {table} SET {first} = {first}"
                if name.endswith("no_update")
                else f"DELETE FROM {table}"
            )
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(statement)
    assert snapshot(tmp_path)["rows"] == {
        **{t: before["rows"][t] for t in V4_TABLES},
        **{t: [] for t in V5_TABLES},
    }


def test_v5_append_only_guards_work_on_a_migrated_journal(tmp_path) -> None:
    before = build_v4(tmp_path)
    started(tmp_path, clock=before["clock"]).stop()
    with closing(raw(tmp_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO broker_observation(id, observed_utc, status, reason, payload, "
            "payload_fingerprint) VALUES ('O1', 't', 'UNAVAILABLE', 'fixture', '{}', 'fp')"
        )
        conn.execute(
            "INSERT INTO reconciliation_result(id, observation_id, subject, subject_key, "
            "trade_id, position_ref, state, basis, detail, recorded_utc) VALUES "
            "('R1', 'O1', 'trade', ?, ?, NULL, 'UNAVAILABLE', 'unavailable', '{}', 't')",
            (before["trade_id"], before["trade_id"]),
        )
        for statement in (
            "UPDATE broker_observation SET reason = 'x'",
            "DELETE FROM broker_observation",
            "UPDATE reconciliation_result SET detail = '{}'",
            "DELETE FROM reconciliation_result",
        ):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(statement)
        with pytest.raises(sqlite3.IntegrityError):  # M-3 unique logical key holds
            conn.execute(
                "INSERT INTO reconciliation_result(id, observation_id, subject, subject_key, "
                "trade_id, position_ref, state, basis, detail, recorded_utc) VALUES "
                "('R2', 'O1', 'trade', ?, ?, NULL, 'PENDING', 'inferred', '{}', 't')",
                (before["trade_id"], before["trade_id"]),
            )
        with pytest.raises(sqlite3.IntegrityError):  # foreign key to a v4 table enforced
            conn.execute(
                "INSERT INTO reconciliation_result(id, observation_id, subject, subject_key, "
                "trade_id, position_ref, state, basis, detail, recorded_utc) VALUES "
                "('R3', 'O1', 'trade', 'NOPE', 'NOPE', NULL, 'PENDING', 'inferred', '{}', 't')"
            )


# ====================================================================== C. rollback
FAILURES = {
    "syntax_error_after_all_objects": js.SCHEMA_V5 + "\nCREATE TABLE broken (",
    "constraint_failure_after_all_objects": js.SCHEMA_V5
    + "\nINSERT INTO cost_policy_record(id) VALUES ('incomplete');",
    "foreign_key_violation_detected_before_commit": js.SCHEMA_V5
    + "\nINSERT INTO cost_policy_decision(id, record_id, decision, reason, actor, decided_utc) "
    "VALUES ('D1', 'MISSING', 'REJECTED', 'fixture', 'user', 't');",
    "failure_in_the_middle": js.SCHEMA_V5.replace(
        "CREATE TABLE broker_observation (", "CREATE TABLE broker_observation (( ", 1
    ),
}


@pytest.mark.parametrize("failure", sorted(FAILURES))
def test_induced_failure_rolls_back_completely_and_a_retry_succeeds(tmp_path, failure) -> None:
    before = build_v4(tmp_path)
    store = started(tmp_path, migrations=(*V4, (5, FAILURES[failure])), clock=before["clock"])
    assert store.status()["state"] == "refused"
    assert store.status()["reason"] == "migration_failed"
    after = snapshot(tmp_path)
    assert after["version"] == 4
    assert after["objects"] == before["objects"]  # no partially created v5 object remains
    assert after["rows"] == before["rows"]  # every record unchanged
    assert (tmp_path / KEY_NAME).read_bytes() == before["key"]
    assert [validate_pair(p) for p in pairs(tmp_path)] == [True]  # its verified backup
    retry = started(tmp_path, clock=before["clock"])  # the registered sequence
    assert retry.ready and retry.status()["schema_version"] == 5
    assert retry.verify_audit_chain()["valid"] is True
    final = snapshot(tmp_path)
    for table in V4_TABLES:
        assert final["rows"][table] == before["rows"][table], table
    assert all(validate_pair(p) for p in pairs(tmp_path))


def test_a_crash_after_commit_leaves_a_complete_v5_journal(tmp_path) -> None:
    """The fault hook fires after the v5 COMMIT: the journal is fully v5, never half-done."""
    before = build_v4(tmp_path)

    def crash(step: str) -> None:
        if step == "after_migration_5":
            raise RuntimeError("simulated crash")

    store = started(tmp_path, clock=before["clock"], fault=crash)
    assert store.status()["state"] == "refused"
    after = snapshot(tmp_path)
    assert after["version"] == 5 and V5_TABLES <= set(after["rows"])
    restarted = started(tmp_path, clock=before["clock"])
    assert restarted.ready and len(pairs(tmp_path)) == 1  # nothing pending: no new backup


# ====================================================================== D. refusals
def test_missing_key_refuses_and_never_creates_a_key(tmp_path) -> None:
    before = build_v4(tmp_path)
    (tmp_path / KEY_NAME).unlink()
    store = started(tmp_path)
    assert store.status()["reason"] == "migration_requires_verified_key"
    assert not (tmp_path / KEY_NAME).exists()
    assert snapshot(tmp_path) == {k: before[k] for k in ("objects", "rows", "version")}
    assert pairs(tmp_path) == []


@pytest.mark.parametrize("bad_key", [b"\x01" * 32, b"\x01" * 31, b""])
def test_invalid_key_refuses_and_keeps_the_key_file(tmp_path, bad_key) -> None:
    before = build_v4(tmp_path)
    (tmp_path / KEY_NAME).write_bytes(bad_key)
    store = started(tmp_path)
    assert store.status()["reason"] == "migration_requires_verified_key"
    assert (tmp_path / KEY_NAME).read_bytes() == bad_key  # never replaced
    assert snapshot(tmp_path) == {k: before[k] for k in ("objects", "rows", "version")}
    assert pairs(tmp_path) == []


def test_corrupt_journal_is_refused_and_left_byte_identical(tmp_path) -> None:
    build_v4(tmp_path)
    key = (tmp_path / KEY_NAME).read_bytes()
    garbage = b"not a database" * 200
    (tmp_path / DB_NAME).write_bytes(garbage)
    store = started(tmp_path)
    assert store.status()["reason"] == "corrupt_database"
    assert (tmp_path / DB_NAME).read_bytes() == garbage  # never recreated or replaced
    assert (tmp_path / KEY_NAME).read_bytes() == key and pairs(tmp_path) == []


def test_failed_integrity_check_is_refused_before_any_migration(tmp_path) -> None:
    build_v4(tmp_path)
    with closing(raw(tmp_path)) as conn:
        page = conn.execute("PRAGMA page_size").fetchone()[0]
        root = conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE name LIKE 'sqlite_autoindex_event%'"
        ).fetchone()[0]
    data = bytearray((tmp_path / DB_NAME).read_bytes())
    start = (root - 1) * page
    data[start + page - 64 : start + page] = b"\x07" * 64  # the index cell (end of page)
    (tmp_path / DB_NAME).write_bytes(bytes(data))
    with closing(raw(tmp_path)) as conn:  # header readable, integrity not ok
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok"
    store = started(tmp_path)
    assert store.status()["reason"] == "corrupt_database"
    assert (tmp_path / DB_NAME).read_bytes() == bytes(data) and pairs(tmp_path) == []


def test_existing_foreign_key_violation_blocks_the_migration(tmp_path) -> None:
    build_v4(tmp_path)
    with closing(raw(tmp_path)) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO trade_note(id, trade_id, text, created_utc) "
            "VALUES ('ORPHAN', 'NO-SUCH-TRADE', 'x', 't')"
        )
    before = snapshot(tmp_path)
    store = started(tmp_path)
    assert store.status()["reason"] == "migration_failed"  # foreign_key_check before COMMIT
    assert snapshot(tmp_path) == before  # still v4, nothing changed, nothing hidden


def test_tampered_audit_chain_blocks_the_migration(tmp_path) -> None:
    build_v4(tmp_path)
    with closing(raw(tmp_path)) as conn:
        conn.execute("DROP TRIGGER audit_no_update")
        conn.execute("UPDATE audit_log SET action = 'tampered' WHERE seq = 2")
    before = snapshot(tmp_path)
    store = started(tmp_path)
    assert store.status()["reason"] == "backup_audit_invalid"
    assert snapshot(tmp_path) == before and pairs(tmp_path) == []


@pytest.mark.parametrize("version", [0, 6, 99])
def test_unexpected_schema_version_is_refused(tmp_path, version) -> None:
    build_v4(tmp_path)
    with closing(raw(tmp_path)) as conn:
        conn.execute(f"PRAGMA user_version = {version}")
    before = snapshot(tmp_path)
    store = started(tmp_path)
    assert store.status()["reason"] == "schema_unsupported"
    assert snapshot(tmp_path) == before and pairs(tmp_path) == []


def test_missing_journal_with_a_key_is_never_recreated(tmp_path) -> None:
    build_v4(tmp_path)
    key = (tmp_path / KEY_NAME).read_bytes()
    (tmp_path / DB_NAME).unlink()
    for suffix in js.SIDECARS:
        Path(f"{tmp_path / DB_NAME}{suffix}").unlink(missing_ok=True)
    store = started(tmp_path)
    assert store.status()["reason"] == "orphaned_key"
    assert not (tmp_path / DB_NAME).exists() and (tmp_path / KEY_NAME).read_bytes() == key


def test_repeated_migration_attempts_change_nothing(tmp_path) -> None:
    before = build_v4(tmp_path)
    first = started(tmp_path, clock=before["clock"])
    assert first.ready
    first.stop()
    migrated = snapshot(tmp_path)
    for _ in range(3):
        again = started(tmp_path, clock=before["clock"])
        assert again.ready and again.status()["schema_version"] == 5
        again.stop()
    assert snapshot(tmp_path) == migrated
    assert len(pairs(tmp_path)) == 1  # one pre-migration backup only


@pytest.mark.parametrize(
    "sequence",
    [
        (*js.MIGRATIONS, (5, js.SCHEMA_V5)),  # v5 registered twice
        (*V4, (6, js.SCHEMA_V5)),  # gap
        (*js.MIGRATIONS[:3], (5, js.SCHEMA_V5)),  # v4 missing
        ((2, js.SCHEMA_V2),),  # does not start at 1
    ],
)
def test_an_invalid_sequence_is_refused_before_any_backup_or_change(tmp_path, sequence) -> None:
    before = build_v4(tmp_path)
    store = started(tmp_path, migrations=sequence)
    assert store.status()["reason"] == "migration_order"
    assert snapshot(tmp_path) == {k: before[k] for k in ("objects", "rows", "version")}
    assert pairs(tmp_path) == []


# ====================================================================== E. registration
def test_v5_is_registered_exactly_once_after_unchanged_v1_to_v4() -> None:
    versions = [v for v, _ in js.MIGRATIONS]
    assert versions == [1, 2, 3, 4, 5, 6] and versions.count(5) == 1  # v6 since P6.2b.5
    assert js.MIGRATIONS[4] == (5, js.SCHEMA_V5)
    for version, name in RELEASED.items():
        assert js.MIGRATIONS[version - 1] == (version, getattr(js, name))
    assert js.migration_sequence_valid(js.MIGRATIONS)
    assert not hasattr(js, "STAGED_MIGRATIONS")


def test_released_migration_texts_are_pinned() -> None:
    digests = {
        v: hashlib.sha256(getattr(js, n).encode()).hexdigest()[:16] for v, n in RELEASED.items()
    }
    assert digests == PINNED, digests


def test_the_runner_identifies_v5_then_v6_as_pending_for_a_v4_journal(tmp_path) -> None:
    build_v4(tmp_path)
    store = JournalStore(tmp_path)  # the registered sequence (v1 - v6 since P6.2b.5)
    assert store.latest_version == 6
    pending = [v for v, _ in store._migrations if v > 4]
    assert pending == [5, 6]


def test_registration_alone_never_touches_a_journal(tmp_path) -> None:
    """Importing, constructing and asking for status never opens or migrates the journal."""
    before = build_v4(tmp_path)
    data = (tmp_path / DB_NAME).read_bytes()
    store = JournalStore(tmp_path)
    assert store.status()["state"] == "stopped"
    assert (tmp_path / DB_NAME).read_bytes() == data
    assert snapshot(tmp_path)["version"] == 4 and pairs(tmp_path) == []
    assert snapshot(tmp_path)["rows"] == before["rows"]


def test_a_fresh_journal_is_created_directly_at_v5(tmp_path) -> None:
    store = started(tmp_path)
    assert store.ready and store.status()["schema_version"] == 5 and pairs(tmp_path) == []
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 5
        created = conn.execute(
            "SELECT new_values FROM audit_log WHERE action = 'journal.created'"
        ).fetchone()[0]
        assert '"schema_version": 5' in created or '"schema_version":5' in created
        assert conn.execute("SELECT count(*) FROM shadow_scheduler_setting").fetchone()[0] == 0


def test_readiness_reports_v5_installed_and_flags_a_refused_journal(tmp_path) -> None:
    from test_decision_journal import _app  # noqa: PLC0415

    from johanna.operations import readiness  # noqa: PLC0415

    app = _app(tmp_path / "ok")
    app.start()
    try:
        report = readiness(app)
        journal = report["journal"]
        assert journal["installed_schema_version"] == 6 == journal["registered_schema_version"]
        codes = {r["code"] for r in report["unattended_blocking_reasons"]}
        assert "schema_not_current" not in codes
        assert report["services"]["monitoring"]["enabled"] is False
        assert report["services"]["notifications"]["automatic"] is False
        assert report["scheduler"]["enabled"] is False
    finally:
        app.stop()
    app = _app(tmp_path / "refused")
    app.journal_store._refuse("migration_failed")
    report = readiness(app)
    codes = {r["code"] for r in report["unattended_blocking_reasons"]}
    assert "schema_not_current" in codes


PINNED = {  # first 16 hex digits of SHA-256; v5 pinned now that it is registered
    1: "30368f372a6e52a9",
    2: "7c0469c15de91678",
    3: "e23ae860ed51fc9c",
    4: "847e1af8380a40de",
    5: "1e59084258cbea09",
    6: "3242050074b27636",  # pinned on registration (P6.2b.5)
}
