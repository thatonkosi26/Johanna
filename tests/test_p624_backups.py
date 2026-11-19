"""P6.2b.4 M-7: backup management infrastructure (temporary journals only; nothing scheduled)."""

from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_journal_store import raw, started

from johanna import backups as bk
from johanna import journal_store as js
from johanna.backups import BackupManager, BackupPolicy, retention_plan

SRC = Path(__file__).resolve().parents[1] / "src" / "johanna"
PLENTY = SimpleNamespace(free=10**12)


def manager(tmp_path, policy=None, free=PLENTY, **kwargs):
    store = started(tmp_path / "journal", **kwargs)
    return BackupManager(store, policy, disk_usage=lambda _p: free), store


def add_rows(store, n=5):
    with store.write() as conn:
        for i in range(n):
            conn.execute(
                "INSERT INTO event(id, type, dedup_key, severity, payload, occurred_utc, "
                "recorded_utc) VALUES (?, 'fixture', ?, 'info', '{}', 't', 't')",
                (
                    f"E{i}-{conn.execute('SELECT count(*) FROM event').fetchone()[0]}",
                    f"k{i}-{conn.execute('SELECT count(*) FROM event').fetchone()[0]}",
                ),
            )


def pairs(store) -> list[Path]:
    return sorted(store.data_dir.glob(f"{js.PAIR_PREFIX}*"))


def digest(path: Path) -> str:
    return hashlib.sha256(b"".join(f.read_bytes() for f in sorted(path.iterdir()))).hexdigest()


# ====================================================================== creation
def test_successful_routine_backup_is_verified_and_wal_consistent(tmp_path) -> None:
    m, store = manager(tmp_path)
    add_rows(store, 20)  # committed rows still in the WAL (not checkpointed)
    blocker = raw(store.data_dir)
    blocker.execute("BEGIN IMMEDIATE")  # an uncommitted write in progress
    blocker.execute(
        "INSERT INTO event(id, type, dedup_key, severity, payload, occurred_utc, recorded_utc) "
        "VALUES ('UNCOMMITTED', 'x', 'u', 'info', '{}', 't', 't')"
    )
    try:
        result = m.create_routine()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert result["status"] == "created"
    (pair,) = pairs(store)
    assert js.validate_pair(pair)
    with closing(sqlite3.connect(pair / js.DB_NAME)) as conn:
        assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 20  # all committed
        assert conn.execute("SELECT count(*) FROM event WHERE id='UNCOMMITTED'").fetchone()[0] == 0
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"  # self-contained
    assert (pair / js.KEY_NAME).read_bytes() == (store.data_dir / js.KEY_NAME).read_bytes()
    assert m.status()["last_result"]["status"] == "created"


def test_key_mismatch_fails_closed_and_publishes_nothing(tmp_path) -> None:
    m, store = manager(tmp_path)
    m.create_routine()
    before = {p.name: digest(p) for p in pairs(store)}
    (store.data_dir / js.KEY_NAME).write_bytes(b"\x02" * 32)  # key on disk no longer matches
    result = m.create_routine()
    assert result == {**result, "status": "failed", "reason": "backup_key_mismatch"}
    assert {p.name: digest(p) for p in pairs(store)} == before  # existing pair untouched
    assert not list(store.data_dir.glob(f"{js.TMP_PREFIX}*"))


def test_corrupt_database_fails_closed(tmp_path) -> None:
    m, store = manager(tmp_path)
    add_rows(store, 30)
    store.stop()
    db = store.data_dir / js.DB_NAME
    with closing(sqlite3.connect(db)) as conn:
        page = conn.execute("PRAGMA page_size").fetchone()[0]
        root = conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE name LIKE 'sqlite_autoindex_event%'"
        ).fetchone()[0]
    data = bytearray(db.read_bytes())
    data[root * page - 64 : root * page] = b"\x07" * 64
    db.write_bytes(bytes(data))
    store._state = "ready"  # simulate damage found while running (the store does not re-open)
    store._key = (store.data_dir / js.KEY_NAME).read_bytes()
    result = m.create_routine()
    assert result["status"] == "failed" and result["reason"] in (
        "corrupt_database",
        "backup_failed",
    )
    assert pairs(store) == []


def test_audit_chain_failure_fails_closed(tmp_path) -> None:
    m, store = manager(tmp_path)
    add_rows(store)
    with store.write() as conn:
        store.append_audit(conn, action="x", table="event", record_id=None, at=store.now())
    with closing(raw(store.data_dir)) as conn:
        conn.execute("DROP TRIGGER audit_no_update")
        conn.execute("UPDATE audit_log SET action='tampered' WHERE seq=1")
    result = m.create_routine()
    assert (result["status"], result["reason"]) == ("failed", "backup_audit_invalid")
    assert pairs(store) == []


@pytest.mark.parametrize(
    "step",
    ["backup_after_snapshot", "backup_after_key", "backup_after_acl", "backup_before_publish"],
)
def test_interrupted_backup_leaves_existing_pairs_and_no_temp(tmp_path, step) -> None:
    armed = {"on": False}

    def fault(name):
        if armed["on"] and name == step:
            raise OSError("interrupted")

    m, store = manager(tmp_path, fault=fault)
    assert m.create_routine()["status"] == "created"
    before = {p.name: digest(p) for p in pairs(store)}
    armed["on"] = True
    result = m.create_routine()
    assert result["status"] == "failed"
    assert {p.name: digest(p) for p in pairs(store)} == before
    assert all(js.validate_pair(p) for p in pairs(store))
    assert not list(store.data_dir.glob(f"{js.TMP_PREFIX}*"))


def test_insufficient_storage_is_refused_before_any_copy(tmp_path) -> None:
    m, store = manager(tmp_path, free=SimpleNamespace(free=1024))
    result = m.create_routine()
    assert (result["status"], result["reason"]) == ("failed", "insufficient_space")
    assert pairs(store) == [] and m.required_space() > 1024


def test_unready_journal_or_unverified_key_is_refused(tmp_path) -> None:
    m, store = manager(tmp_path)
    store.stop()
    assert m.create_routine()["reason"] == "journal_not_ready_or_key_unverified"


def test_repeated_backups_are_distinct_valid_pairs(tmp_path) -> None:
    m, store = manager(tmp_path)
    names = []
    for _ in range(3):
        add_rows(store, 2)
        names.append(m.create_routine()["pair"])
    assert len(set(names)) == 3 and all(js.validate_pair(store.data_dir / n) for n in names)


# ====================================================================== retention
def _p(name, kind, stamp, valid=True):
    return {"name": name, "kind": kind, "stamp": stamp, "valid": valid}


def test_retention_plan_never_touches_premigration_or_invalid_pairs() -> None:
    items = [
        _p("pre", "premigration-v4", "20261101T000000000000Z"),
        _p("d1", "daily", "20261110T000000000000Z"),
        _p("d2", "daily", "20261111T000000000000Z"),
        _p("bad", "daily", "20261112T000000000000Z", valid=False),
        _p("d3", "daily", "20261113T000000000000Z"),
    ]
    plan = retention_plan(items, keep_daily=2)
    assert plan["delete"] == ["d1"]
    assert set(plan["keep"]) == {"pre", "d2", "d3"} and plan["manual_review"] == ["bad"]


def test_retention_can_never_remove_the_only_usable_recovery_point() -> None:
    only = [_p("d1", "daily", "20261110T000000000000Z")]
    assert retention_plan(only, keep_daily=1)["delete"] == []
    broken_newer = [
        _p("d1", "daily", "20261110T000000000000Z"),
        _p("d2", "daily", "20261111T000000000000Z", valid=False),
    ]
    assert retention_plan(broken_newer, keep_daily=1)["delete"] == []
    with pytest.raises(ValueError):
        retention_plan(only, keep_daily=0)
    with pytest.raises(ValueError):
        BackupPolicy(keep_daily=0)


def test_prune_is_a_dry_run_unless_applied(tmp_path) -> None:
    m, store = manager(tmp_path, policy=BackupPolicy(keep_daily=2))
    for _ in range(4):
        m.create_routine()
    dry = m.prune()
    assert dry["status"] == "dry_run" and len(dry["delete"]) == 2
    assert len(pairs(store)) == 4  # nothing removed
    applied = m.prune(apply=True)
    assert applied["status"] == "pruned" and applied["removed"] == dry["delete"]
    assert [p.name for p in pairs(store)] == dry["keep"]


def test_prune_refuses_when_a_kept_pair_no_longer_validates(tmp_path, monkeypatch) -> None:
    m, store = manager(tmp_path, policy=BackupPolicy(keep_daily=1))
    for _ in range(2):
        m.create_routine()
    real = bk.js.validate_pair
    calls = {"n": 0}

    def flaky(path):
        calls["n"] += 1
        return real(path) if calls["n"] <= 2 else False  # valid when planned, not when kept

    monkeypatch.setattr(bk.js, "validate_pair", flaky)
    result = m.prune(apply=True)
    assert (result["status"], result["reason"]) == ("refused", "kept_pair_invalid")
    assert len(pairs(store)) == 2


# ====================================================================== restore rehearsal
def test_restore_rehearsal_into_a_separate_directory(tmp_path) -> None:
    m, store = manager(tmp_path)
    add_rows(store, 7)
    pair = m.create_routine()["pair"]
    add_rows(store, 3)  # after the backup: not in the rehearsal
    target = tmp_path / "rehearsal"
    result = m.restore_rehearsal(pair, target)
    assert result["status"] == "ok" and result["audit_valid"] and result["key_status"] == "verified"
    assert result["schema_version"] == js.MIGRATIONS[-1][0]
    assert result["row_counts"]["event"] == 7
    with closing(raw(store.data_dir)) as conn:  # the live journal is untouched
        assert conn.execute("SELECT count(*) FROM event").fetchone()[0] == 10
    manifest = json.loads((store.data_dir / pair / js.PAIR_MANIFEST).read_text())
    assert manifest["verified"] is True and js.validate_pair(store.data_dir / pair)


def test_restore_rehearsal_refuses_unsafe_targets_and_invalid_pairs(tmp_path) -> None:
    m, store = manager(tmp_path)
    pair = m.create_routine()["pair"]
    inside = store.data_dir / "restore"
    assert m.restore_rehearsal(pair, inside)["reason"] == "target_inside_journal_directory"
    assert m.restore_rehearsal(pair, store.data_dir)["reason"] == "target_inside_journal_directory"
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("x")
    assert m.restore_rehearsal(pair, busy)["reason"] == "target_not_empty"
    (store.data_dir / pair / js.PAIR_MANIFEST).write_text("{}")
    assert m.restore_rehearsal(pair, tmp_path / "t2")["reason"] == "pair_invalid"


# ====================================================================== activation boundary
def test_policy_is_disabled_and_nothing_schedules_backups() -> None:
    assert BackupPolicy().enabled is False
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        calls = {
            n.func.attr
            for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }
        if path.name != "backups.py":
            assert "create_routine" not in calls, path.name
            assert "prune_daily" not in calls, path.name
    tree = ast.parse((SRC / "backups.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "Timer" not in names and "Thread" not in names
