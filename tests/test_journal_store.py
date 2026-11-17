"""P5.1 journal store: initialisation, key security, migrations, triggers, audit, backups.

Every test uses a temporary directory; nothing touches the real ``data/`` directory.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from johanna import journal_store as js
from johanna.journal_store import (
    DB_NAME,
    GENESIS_HASH,
    KEY_NAME,
    JournalError,
    JournalStore,
    key_check_value,
    validate_pair,
    verify_audit_rows,
)


class Acl:
    """Records ACL requests (fast stand-in for icacls/chmod); can be told to fail."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[str, bool]] = []
        self.fail = fail

    def __call__(self, path: Path, directory: bool) -> None:
        self.calls.append((Path(path).name, directory))
        if self.fail:
            raise JournalError("acl_failed")


def make(tmp_path: Path, **kwargs) -> JournalStore:
    kwargs.setdefault("acl", Acl())
    kwargs.setdefault("acl_check", Acl())
    return JournalStore(tmp_path, **kwargs)


def started(tmp_path: Path, **kwargs) -> JournalStore:
    store = make(tmp_path, **kwargs)
    store.start()
    return store


def names(path: Path) -> list[str]:
    return sorted(p.name for p in path.iterdir())


def raw(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(tmp_path / DB_NAME, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------- first creation
def test_genuine_first_creation(tmp_path) -> None:
    acl = Acl()
    store = started(tmp_path, acl=acl)
    status = store.status()
    assert status["state"] == "ready" and status["key_status"] == "verified"
    assert status["hmac_available"] is True and status["schema_version"] == 1
    # a verified key is only a prerequisite; the capabilities are not implemented (P5.3/P5.4)
    assert status["hmac_prerequisite"] == "verified"
    assert status["monitoring_and_reconciliation"] == "not_implemented"
    assert names(tmp_path) == [KEY_NAME, DB_NAME]  # no sidecars left, nothing else created
    key = (tmp_path / KEY_NAME).read_bytes()
    assert len(key) == 32
    with raw(tmp_path) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
        check = conn.execute("SELECT value FROM meta WHERE key='key_check'").fetchone()[0]
    assert check == key_check_value(key)
    assert acl.calls == [(KEY_NAME, False)]
    assert store.verify_audit_chain()["valid"] is True


def test_key_is_created_exclusively_and_never_overwritten(tmp_path) -> None:
    (tmp_path / KEY_NAME).write_bytes(b"existing-key-material-that-stays")
    store = started(tmp_path)
    assert store.status()["state"] == "refused" and store.status()["reason"] == "orphaned_key"
    assert (tmp_path / KEY_NAME).read_bytes() == b"existing-key-material-that-stays"
    assert not (tmp_path / DB_NAME).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows icacls")
def test_real_windows_acl_is_user_only(tmp_path) -> None:
    store = JournalStore(tmp_path)  # real icacls with explicit arguments, shell=False
    store.start()
    assert store.status()["key_status"] == "verified"
    js.verify_user_only(tmp_path / KEY_NAME, directory=False)
    pair = store.backup("daily")
    js.verify_user_only(pair / KEY_NAME, directory=False)
    js.verify_user_only(pair, directory=True)


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod")
def test_real_posix_permissions_are_0600(tmp_path) -> None:
    store = JournalStore(tmp_path)
    store.start()
    assert (tmp_path / KEY_NAME).stat().st_mode & 0o777 == 0o600


# ---------------------------------------------------------------- refusal matrix
def test_existing_journal_reopens_with_verified_key(tmp_path) -> None:
    started(tmp_path).stop()
    again = started(tmp_path)
    assert again.status()["state"] == "ready" and again.hmac_available


def test_missing_key_refuses_hmac_and_never_regenerates(tmp_path) -> None:
    started(tmp_path).stop()
    (tmp_path / KEY_NAME).unlink()
    store = started(tmp_path)
    status = store.status()
    assert status["state"] == "ready"  # decisions remain readable/writable
    assert status["key_status"] == "missing" and status["hmac_available"] is False
    assert status["hmac_prerequisite"] == "refused"
    assert status["monitoring_and_reconciliation"] == "not_implemented"
    assert not (tmp_path / KEY_NAME).exists()
    with pytest.raises(JournalError):
        store.broker_ref("position", 123)


def test_mismatched_key_refuses_hmac(tmp_path) -> None:
    started(tmp_path).stop()
    (tmp_path / KEY_NAME).write_bytes(os.urandom(32))
    store = started(tmp_path)
    assert store.status()["key_status"] == "mismatch" and not store.hmac_available
    assert store.status()["hmac_prerequisite"] == "refused"
    assert store.status()["monitoring_and_reconciliation"] == "not_implemented"


def test_key_permissions_not_user_only_refuse_hmac(tmp_path) -> None:
    started(tmp_path).stop()
    store = started(tmp_path, acl_check=Acl(fail=True))
    assert store.status()["key_status"] == "permissions_not_user_only"
    assert not store.hmac_available
    assert store.status()["hmac_prerequisite"] == "refused"


def test_orphaned_key_refuses(tmp_path) -> None:
    started(tmp_path).stop()
    (tmp_path / DB_NAME).unlink()
    store = started(tmp_path)
    assert store.status()["reason"] == "orphaned_key"
    assert (tmp_path / KEY_NAME).exists()  # untouched


@pytest.mark.parametrize(
    "dirname",
    [
        "trading_journal.backup-daily-20260101T000000000000Z",
        ".trading_journal.backup-tmp-20260101T000000000000Z-ab12",
    ],
)
def test_existing_backup_directory_requires_restore(tmp_path, dirname) -> None:
    (tmp_path / dirname).mkdir()
    store = started(tmp_path)
    assert store.status()["reason"] == "restore_required"
    assert names(tmp_path) == [dirname]


def test_stale_sidecar_refuses_initialisation(tmp_path) -> None:
    (tmp_path / f"{DB_NAME}-wal").write_bytes(b"x")
    store = started(tmp_path)
    assert store.status()["reason"] == "stale_sidecars"
    assert names(tmp_path) == [f"{DB_NAME}-wal"]


def test_corrupt_database_is_refused_never_replaced(tmp_path) -> None:
    started(tmp_path).stop()
    garbage = b"this is not a sqlite database" * 100
    (tmp_path / DB_NAME).write_bytes(garbage)
    store = started(tmp_path)
    assert store.status()["state"] == "refused"
    assert store.status()["reason"] == "corrupt_database"
    assert (tmp_path / DB_NAME).read_bytes() == garbage
    assert names(tmp_path) == [KEY_NAME, DB_NAME]  # no quarantine, no recreation


def test_newer_schema_is_refused(tmp_path) -> None:
    started(tmp_path).stop()
    with raw(tmp_path) as conn:
        conn.execute("PRAGMA user_version = 99")
    assert started(tmp_path).status()["reason"] == "schema_unsupported"


# ---------------------------------------------------------------- failed initialisation
@pytest.mark.parametrize("step", ["after_key", "after_acl", "after_db_file", "after_meta"])
def test_failed_initialisation_removes_only_its_own_files(tmp_path, step) -> None:
    (tmp_path / "johanna.sqlite3").write_bytes(b"pre-existing application database")
    (tmp_path / "unrelated.txt").write_text("keep me")

    def fault(name: str) -> None:
        if name == step:
            raise RuntimeError("injected")

    store = started(tmp_path, fault=fault)
    assert store.status()["reason"] == "initialisation_failed"
    assert names(tmp_path) == ["johanna.sqlite3", "unrelated.txt"]  # incl. -wal/-shm removed
    assert (tmp_path / "johanna.sqlite3").read_bytes() == b"pre-existing application database"
    store.start()  # the same process refuses again
    assert store.status()["reason"] == "initialisation_failed"
    assert not (tmp_path / KEY_NAME).exists()
    assert started(tmp_path).status()["state"] == "ready"  # a later start may create afresh


def test_key_acl_failure_aborts_initialisation(tmp_path) -> None:
    store = started(tmp_path, acl=Acl(fail=True))
    assert store.status()["reason"] == "initialisation_failed"
    assert names(tmp_path) == []


def test_cleanup_failure_fails_closed_on_next_start(tmp_path, monkeypatch) -> None:
    real_unlink = Path.unlink

    def stubborn(self, missing_ok=False):
        if self.name == KEY_NAME:
            raise PermissionError("locked")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", stubborn)
    store = started(
        tmp_path,
        fault=lambda s: (_ for _ in ()).throw(RuntimeError("x")) if s == "after_meta" else None,
    )
    assert store.status()["reason"] == "initialisation_failed"
    monkeypatch.setattr(Path, "unlink", real_unlink)
    assert (tmp_path / KEY_NAME).exists() and not (tmp_path / DB_NAME).exists()
    again = started(tmp_path)
    assert again.status()["reason"] == "orphaned_key"  # never a replacement key


# ---------------------------------------------------------------- migrations
V2 = (2, "CREATE TABLE p52_placeholder (id INTEGER PRIMARY KEY);")


def test_migration_creates_verified_premigration_backup_first(tmp_path) -> None:
    started(tmp_path).stop()
    store = started(tmp_path, migrations=(*js.MIGRATIONS, V2))
    assert store.status()["state"] == "ready" and store.latest_version == 2
    with raw(tmp_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
    pairs = store.valid_pairs()
    assert [p.name.split("-")[1] for p in pairs] == ["premigration"]
    assert "premigration-v1" in pairs[0].name
    with sqlite3.connect(pairs[0] / DB_NAME) as conn:  # the backup holds the v1 schema
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_failing_migration_refuses_and_keeps_schema(tmp_path) -> None:
    started(tmp_path).stop()
    store = started(tmp_path, migrations=(*js.MIGRATIONS, (2, "CREATE TABLE broken (")))
    assert store.status()["reason"] == "migration_failed"
    with raw(tmp_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_migrations_must_be_contiguous(tmp_path) -> None:
    started(tmp_path).stop()
    store = started(tmp_path, migrations=(*js.MIGRATIONS, (3, "SELECT 1;")))
    assert store.status()["reason"] == "migration_order"


def test_migration_requires_a_verified_key(tmp_path) -> None:
    started(tmp_path).stop()
    (tmp_path / KEY_NAME).unlink()
    store = started(tmp_path, migrations=(*js.MIGRATIONS, V2))
    assert store.status()["reason"] == "migration_requires_verified_key"


# ---------------------------------------------------------------- integrity rules
def _seed_decision(conn, decision_id="01JTESTDECISION0000000000A") -> None:
    conn.execute(
        "INSERT INTO decision(id, symbol, timeframe, analysis_outcome, market_direction, "
        "t6_evidence, analysis_fingerprint, capture_source, recorded_utc) "
        "VALUES (?, 'EURUSD', 'H1', 'NO_TRADE', 'NONE', '{}', 'fp', 'analysis_request', 't')",
        (decision_id,),
    )
    conn.execute(
        "INSERT INTO decision_observation VALUES (?, '2026-01-01', '2026-01-01', 1)", (decision_id,)
    )


def test_foreign_keys_are_enforced(tmp_path) -> None:
    store = started(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        with store.write() as conn:
            conn.execute("INSERT INTO decision_observation VALUES ('nope', 'a', 'a', 1)")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE decision SET symbol='GBPUSD'",
        "DELETE FROM decision",
        "UPDATE meta SET value='x'",
        "DELETE FROM meta",
        "UPDATE audit_log SET actor='x'",
        "DELETE FROM audit_log",
        "UPDATE event SET severity='x'",
        "DELETE FROM event",
        "UPDATE risk_assessment SET risk_status='APPROVED'",
        "DELETE FROM risk_assessment",
        "DELETE FROM decision_observation",
        "UPDATE decision_observation SET observations = 0",
        "UPDATE decision_observation SET first_seen_utc = '1999'",
    ],
)
def test_immutability_triggers_abort(tmp_path, statement) -> None:
    store = started(tmp_path)
    with store.write() as conn:
        _seed_decision(conn)
        conn.execute(
            "INSERT INTO risk_assessment(id, decision_id, risk_status, risk_evidence, "
            "risk_fingerprint, recorded_utc) VALUES ('r1', ?, 'UNVERIFIED', '{}', 'f', 't')",
            ("01JTESTDECISION0000000000A",),
        )
        conn.execute(
            "INSERT INTO event(id, type, dedup_key, severity, payload, occurred_utc, "
            "recorded_utc) VALUES ('e1', 'decision.created', 'k1', 'info', '{}', 't', 't')"
        )
    with pytest.raises(
        sqlite3.IntegrityError, match="immutable|append-only|insert-only|may only|cannot"
    ):
        with store.write() as conn:
            conn.execute(statement)


def test_observation_metadata_may_advance(tmp_path) -> None:
    store = started(tmp_path)
    with store.write() as conn:
        _seed_decision(conn)
        conn.execute(
            "UPDATE decision_observation SET last_seen_utc='2026-02-01', "
            "observations=observations+1"
        )
    with store.read() as conn:
        row = conn.execute("SELECT * FROM decision_observation").fetchone()
    assert (row["observations"], row["last_seen_utc"]) == (2, "2026-02-01")


def test_event_deduplication_is_enforced(tmp_path) -> None:
    store = started(tmp_path)
    sql = (
        "INSERT INTO event(id, type, dedup_key, severity, payload, occurred_utc, recorded_utc)"
        " VALUES (?, 't', 'same-key', 'info', '{}', 't', 't')"
    )
    with store.write() as conn:
        conn.execute(sql, ("e1",))
    with pytest.raises(sqlite3.IntegrityError):
        with store.write() as conn:
            conn.execute(sql, ("e2",))


# ---------------------------------------------------------------- audit chain
def _audit_rows(store: JournalStore, n: int = 4) -> None:
    for i in range(n):
        with store.write() as conn:
            store.append_audit(
                conn,
                action="test",
                table="meta",
                record_id=str(i),
                new={"i": i, "text": "é"},
                at=datetime(2026, 1, 1, tzinfo=UTC),
            )


def _drop_audit_triggers(tmp_path: Path) -> sqlite3.Connection:
    """Simulates someone editing the file directly (outside Johanna)."""
    conn = raw(tmp_path)
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute("DROP TRIGGER audit_no_delete")
    return conn


def test_audit_chain_specification(tmp_path) -> None:
    store = started(tmp_path)
    _audit_rows(store)
    with store.read() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
    assert rows[0]["prev_hash"] == GENESIS_HASH
    for previous, row in zip(rows, rows[1:]):
        assert row["prev_hash"] == previous["row_hash"]
    first = rows[1]
    expected = js.audit_row_hash(
        [
            first["seq"],
            first["recorded_utc"],
            first["actor"],
            first["action"],
            first["table_name"],
            first["record_id"],
            first["old_values"],
            first["new_values"],
            first["prev_hash"],
        ]
    )
    assert first["row_hash"] == expected
    assert first["recorded_utc"] == "2026-01-01T00:00:00.000000Z"
    assert first["new_values"] == '{"i":0,"text":"é"}'  # canonical JSON, UTF-8, sorted keys
    result = store.verify_audit_chain()
    assert result["valid"] and result["rows"] == 5
    assert result["guarantee"] == "tamper-evident only; no external anchor"


@pytest.mark.parametrize(
    ("tamper", "problem"),
    [
        ("UPDATE audit_log SET new_values='{\"i\":99}' WHERE seq=3", "row hash mismatch"),
        ("DELETE FROM audit_log WHERE seq=3", "sequence gap or reordering"),
        ("UPDATE audit_log SET prev_hash='" + "f" * 64 + "' WHERE seq=4", "previous hash mismatch"),
    ],
)
def test_audit_chain_detects_tampering(tmp_path, tamper, problem) -> None:
    store = started(tmp_path)
    _audit_rows(store)
    with _drop_audit_triggers(tmp_path) as conn:
        conn.execute(tamper)
    result = store.verify_audit_chain()
    assert result["valid"] is False and result["problem"] == problem


def test_audit_chain_detects_reordering(tmp_path) -> None:
    store = started(tmp_path)
    _audit_rows(store)
    with store.read() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY seq")]
    rows[2], rows[3] = rows[3], rows[2]
    assert verify_audit_rows(rows)["valid"] is False


def test_recomputed_chain_is_not_detected_limitation(tmp_path) -> None:
    """Documents the limit: someone who rewrites rows AND recomputes the chain passes."""
    store = started(tmp_path)
    _audit_rows(store)
    with _drop_audit_triggers(tmp_path) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY seq")]
        rows[2]["new_values"] = '{"forged":true}'
        previous = GENESIS_HASH
        for row in rows:
            row["prev_hash"] = previous
            row["row_hash"] = js.audit_row_hash(
                [
                    row[k]
                    for k in (
                        "seq",
                        "recorded_utc",
                        "actor",
                        "action",
                        "table_name",
                        "record_id",
                        "old_values",
                        "new_values",
                        "prev_hash",
                    )
                ]
            )
            conn.execute(
                "UPDATE audit_log SET new_values=?, prev_hash=?, row_hash=? WHERE seq=?",
                (row["new_values"], row["prev_hash"], row["row_hash"], row["seq"]),
            )
            previous = row["row_hash"]
    assert store.verify_audit_chain()["valid"] is True  # tamper-evident, not tamper-proof


# ---------------------------------------------------------------- HMAC references
def test_broker_references_are_keyed_and_typed(tmp_path) -> None:
    store = started(tmp_path)
    ref = store.broker_ref("position", 123456)
    assert ref == store.broker_ref("position", 123456) and len(ref) == 64
    assert ref != store.broker_ref("deal", 123456)
    other = started(tmp_path / "other")
    assert other.broker_ref("position", 123456) != ref  # a different key, a different domain


# ---------------------------------------------------------------- backups
def test_backup_pair_is_valid_and_wal_safe(tmp_path) -> None:
    store = started(tmp_path)
    blocker = raw(tmp_path)  # an open reader keeps committed rows in the WAL
    blocker.execute("BEGIN")
    blocker.execute("SELECT count(*) FROM audit_log").fetchone()
    _audit_rows(store, 3)
    assert (tmp_path / f"{DB_NAME}-wal").stat().st_size > 0  # uncheckpointed WAL content
    pair = store.backup("daily")
    blocker.execute("COMMIT")
    blocker.close()
    assert names(pair) == ["pair.json", KEY_NAME, DB_NAME]
    assert validate_pair(pair)
    with sqlite3.connect(pair / DB_NAME) as conn:
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 4  # WAL included
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    manifest = json.loads((pair / "pair.json").read_text())
    assert manifest["verified"] is True and manifest["kind"] == "daily"
    assert manifest["key_check"] == key_check_value((tmp_path / KEY_NAME).read_bytes())
    assert (pair / KEY_NAME).read_bytes() == (tmp_path / KEY_NAME).read_bytes()


def test_backup_applies_acl_to_key_and_directory(tmp_path) -> None:
    acl = Acl()
    store = started(tmp_path, acl=acl)
    acl.calls.clear()
    store.backup("daily")
    assert acl.calls == [(KEY_NAME, False), (acl.calls[1][0], True)]
    assert acl.calls[1][0].startswith(".trading_journal.backup-tmp-")  # before publication


@pytest.mark.parametrize(
    "step",
    ["backup_after_snapshot", "backup_after_key", "backup_after_acl", "backup_before_publish"],
)
def test_failed_backup_cleans_only_its_own_temporary_directory(tmp_path, step) -> None:
    store = started(tmp_path)
    first = store.backup("daily")
    stale = tmp_path / ".trading_journal.backup-tmp-20250101T000000000000Z-dead"
    stale.mkdir()
    store._fault = lambda name: (
        (_ for _ in ()).throw(RuntimeError("boom")) if name == step else None
    )
    with pytest.raises(JournalError):
        store.backup("daily")
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_dir()) == sorted(
        [first.name, stale.name]
    )
    assert validate_pair(first) and stale.exists()  # existing pair and crash leftover untouched
    assert store.status()["stale_backup_temp_dirs"] == 1


def test_backup_with_mismatched_key_is_rejected(tmp_path) -> None:
    store = started(tmp_path)
    store._fault = lambda name: (
        (tmp_path / KEY_NAME).write_bytes(os.urandom(32))
        if name == "backup_after_snapshot"
        else None
    )
    with pytest.raises(JournalError, match="backup key mismatch"):
        store.backup("daily")
    assert store.valid_pairs() == [] and store.status()["stale_backup_temp_dirs"] == 0


def test_backup_acl_failure_publishes_nothing(tmp_path) -> None:
    acl = Acl()
    store = started(tmp_path, acl=acl)
    acl.fail = True
    with pytest.raises(JournalError):
        store.backup("daily")
    assert [p for p in tmp_path.iterdir() if p.is_dir()] == []


def test_existing_backup_is_never_overwritten(tmp_path) -> None:
    store = started(tmp_path)
    collisions = []

    def collide(name: str) -> None:
        if name == "backup_before_publish":
            target = next(tmp_path.glob(".trading_journal.backup-tmp-*")).name.split("-")[2]
            final = tmp_path / f"trading_journal.backup-daily-{target}"
            final.mkdir()
            (final / "marker").write_text("pre-existing")
            collisions.append(final)

    store._fault = collide
    with pytest.raises(JournalError):
        store.backup("daily")
    assert (collisions[0] / "marker").read_text() == "pre-existing"
    assert names(collisions[0]) == ["marker"]


def test_incomplete_or_tampered_pairs_are_never_valid(tmp_path) -> None:
    store = started(tmp_path)
    good = store.backup("daily")
    assert validate_pair(good)
    cases = {}
    for label in ("no_manifest", "db_changed", "key_changed", "not_verified", "bad_name"):
        copy = tmp_path / f"trading_journal.backup-daily-2026010{len(cases)}T000000000000Z"
        shutil.copytree(good, copy)
        cases[label] = copy
    (cases["no_manifest"] / "pair.json").unlink()
    with open(cases["db_changed"] / DB_NAME, "ab") as stream:
        stream.write(b"\0")
    (cases["key_changed"] / KEY_NAME).write_bytes(os.urandom(32))
    manifest = json.loads((cases["not_verified"] / "pair.json").read_text())
    manifest["verified"] = False
    (cases["not_verified"] / "pair.json").write_text(json.dumps(manifest))
    renamed = tmp_path / "trading_journal.backup-weekly-20260101T000000000000Z"
    cases["bad_name"].rename(renamed)
    for label, path in {**cases, "bad_name": renamed}.items():
        assert validate_pair(path) is False, label
    temp = tmp_path / ".trading_journal.backup-tmp-20260101T000000000000Z-0000"
    shutil.copytree(good, temp)
    assert validate_pair(temp) is False  # a temporary directory is never a backup


def test_retention_keeps_seven_valid_daily_pairs_and_never_premigration_or_invalid(
    tmp_path,
) -> None:
    store = started(tmp_path)
    source = store.backup("daily")
    premigration = store.backup("premigration-v1")
    pairs = [source]
    for i in range(8):  # nine valid daily pairs in total, distinct timestamps
        copy = tmp_path / f"trading_journal.backup-daily-2026010{i + 1}T000000000000Z"
        shutil.copytree(source, copy)
        pairs.append(copy)
    invalid = tmp_path / "trading_journal.backup-daily-20250101T000000000000Z"
    invalid.mkdir()  # oldest name, but not a valid pair
    removed = store.prune_daily()
    assert len(removed) == 2
    remaining_daily = [p for p in store.valid_pairs() if "-daily-" in p.name]
    assert len(remaining_daily) == 7
    assert validate_pair(premigration) and invalid.exists()
    assert all(not p.exists() for p in removed)


# ---------------------------------------------------------------- secrecy
def test_key_never_appears_in_logs_status_or_errors(tmp_path, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    store = started(tmp_path)
    key = (tmp_path / KEY_NAME).read_bytes()
    texts = [json.dumps(store.status())]
    store.stop()
    (tmp_path / KEY_NAME).write_bytes(os.urandom(32))
    texts.append(json.dumps(started(tmp_path).status()))
    (tmp_path / KEY_NAME).write_bytes(key)
    (tmp_path / DB_NAME).write_bytes(b"corrupt" * 50)
    texts.append(json.dumps(started(tmp_path).status()))
    for secret in (key.hex(), key.hex().upper(), repr(key)):
        assert all(secret not in text for text in texts)
        assert secret not in caplog.text
