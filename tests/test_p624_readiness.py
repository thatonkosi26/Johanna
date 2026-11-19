"""P6.2b.4 M-8: readiness evidence reuse is correct, invalidated and never a false ``ok``."""

from __future__ import annotations

import threading
from contextlib import closing
from datetime import UTC, datetime

from test_decision_journal import _app
from test_journal_store import raw, started

from johanna import journal_store as js
from johanna.operations import AUDIT_FULL_MAX_AGE, PAIR_MAX_AGE, ReadinessEvidence, readiness


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 19, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


def journal(tmp_path, entries=20):
    store = started(tmp_path)
    append(store, entries)
    return store


def append(store, n) -> None:
    with store.write() as conn:
        for i in range(n):
            store.append_audit(
                conn, action="fixture", table="event", record_id=str(i), at=store.now()
            )


def tamper(path, sql) -> None:
    with closing(raw(path)) as conn:
        conn.execute("DROP TRIGGER IF EXISTS audit_no_update")
        conn.execute("DROP TRIGGER IF EXISTS audit_no_delete")
        conn.execute(sql)


# ====================================================================== audit evidence
def test_incremental_verification_matches_full_and_checks_only_new_rows(tmp_path) -> None:
    store, clock = journal(tmp_path), Clock()
    ev = ReadinessEvidence(clock)
    first = ev.audit(store)
    assert first["valid"] and first["mode"] == "full"
    again = ev.audit(store)
    assert again["valid"] and again["mode"] == "incremental" and again["verified_new_rows"] == 0
    append(store, 7)
    third = ev.audit(store)
    assert third["valid"] and third["verified_new_rows"] == 7
    assert third["rows"] == store.verify_audit_chain()["rows"]


def test_tampered_new_row_is_detected_incrementally(tmp_path) -> None:
    store, ev = journal(tmp_path), ReadinessEvidence(Clock())
    ev.audit(store)
    append(store, 3)
    top = store.verify_audit_chain()["rows"]
    tamper(tmp_path, f"UPDATE audit_log SET action='tampered' WHERE seq={top}")
    result = ev.audit(store)
    assert result["valid"] is False and result["first_bad_seq"] == top
    assert ev.audit(store)["valid"] is False  # invalid is never cached as valid


def test_changed_anchor_or_deleted_rows_force_a_full_failing_check(tmp_path) -> None:
    store, ev = journal(tmp_path), ReadinessEvidence(Clock())
    top = ev.audit(store)["rows"]
    tamper(tmp_path, f"UPDATE audit_log SET row_hash='{'0' * 64}' WHERE seq={top}")
    result = ev.audit(store)
    assert result["mode"] == "full" and result["valid"] is False

    other = journal(tmp_path / "b")
    ev2 = ReadinessEvidence(Clock())
    ev2.audit(other)
    tamper(tmp_path / "b", "DELETE FROM audit_log WHERE seq=3")
    result = ev2.audit(other)
    assert result["mode"] == "full" and result["valid"] is False


def test_older_row_edits_are_caught_within_the_full_check_bound(tmp_path) -> None:
    """Documented limit: an edit of an *older* row that keeps its stored hash (triggers
    dropped first) is invisible to the incremental check and is caught by the next full
    verification, at most AUDIT_FULL_MAX_AGE later."""
    store, clock = journal(tmp_path), Clock()
    ev = ReadinessEvidence(clock)
    ev.audit(store)
    tamper(tmp_path, "UPDATE audit_log SET action='tampered' WHERE seq=2")
    assert ev.audit(store)["mode"] == "incremental"  # inside the window
    clock.now += AUDIT_FULL_MAX_AGE
    stale = ev.audit(store)
    assert stale["mode"] == "full" and stale["valid"] is False and stale["first_bad_seq"] == 2


def test_stale_evidence_is_reverified_in_full(tmp_path) -> None:
    store, clock = journal(tmp_path), Clock()
    ev = ReadinessEvidence(clock)
    ev.audit(store)
    clock.now += AUDIT_FULL_MAX_AGE
    assert ev.audit(store)["mode"] == "full"
    ev.invalidate()
    assert ev.audit(store)["mode"] == "full"


# ====================================================================== pair evidence
def test_pair_validation_is_reused_only_while_unchanged(tmp_path) -> None:
    store, clock = journal(tmp_path), Clock()
    a = store.backup("daily")
    b = store.backup("daily")
    ev = ReadinessEvidence(clock)
    first = ev.valid_pairs(store)
    assert first == {"valid": [a.name, b.name], "validated_now": 2, "reused": 0}
    assert ev.valid_pairs(store)["reused"] == 2
    (a / js.PAIR_MANIFEST).write_text("{}")  # changed: signature differs -> re-validated
    result = ev.valid_pairs(store)
    assert result["valid"] == [b.name] and result["validated_now"] == 1
    assert ev.valid_pairs(store)["validated_now"] == 1  # invalid is re-checked every time
    clock.now += PAIR_MAX_AGE
    assert ev.valid_pairs(store)["validated_now"] == 2  # max age: full re-validation


def test_missing_and_corrupt_pairs_are_never_counted(tmp_path) -> None:
    store, ev = journal(tmp_path), ReadinessEvidence(Clock())
    a = store.backup("daily")
    b = store.backup("daily")
    assert len(ev.valid_pairs(store)["valid"]) == 2
    (b / js.KEY_NAME).unlink()  # incomplete pair
    db = a / js.DB_NAME
    db.write_bytes(db.read_bytes()[:4096] + b"corrupt")  # corrupt pair database
    assert ev.valid_pairs(store)["valid"] == []


# ====================================================================== readiness integration
def test_an_invalid_journal_is_never_ready_from_cache(tmp_path) -> None:
    app = _app(tmp_path)
    app.start()
    try:
        assert readiness(app)["manual_operation"] == "ok"
        assert readiness(app)["manual_operation"] == "ok"  # second request reuses evidence
        tamper(app.journal_store.data_dir, "UPDATE audit_log SET action='x' WHERE seq=1")
        tamper(
            app.journal_store.data_dir,
            "UPDATE audit_log SET row_hash='" + "1" * 64 + "' WHERE seq=(SELECT max(seq) "
            "FROM audit_log)",
        )
        for _ in range(3):
            report = readiness(app)
            check = next(c for c in report["checks"] if c["check"] == "audit_chain")
            assert check["status"] == "attention" and report["manual_operation"] == "attention"
    finally:
        app.stop()


def test_concurrent_readiness_requests_are_consistent(tmp_path) -> None:
    app = _app(tmp_path)
    app.start()
    try:
        results, errors = [], []
        barrier = threading.Barrier(8)

        def run():
            try:
                barrier.wait(10)
                results.append(readiness(app))
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=run) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        assert errors == [] and len(results) == 8
        assert {r["manual_operation"] for r in results} == {"ok"}
        audit = [next(c for c in r["checks"] if c["check"] == "audit_chain") for r in results]
        assert sum(1 for a in audit if a["detail"]["mode"] == "full") == 1  # one full, rest reuse
    finally:
        app.stop()


def test_readiness_triggers_no_mt5_backup_or_notification(tmp_path) -> None:
    app = _app(tmp_path)
    app.start()
    try:
        runner = app.live_market._quote_runner
        jobs = []
        original = runner.run

        def counting(op, args, timeout):
            jobs.append(op)
            return original(op, args, timeout)

        runner.run = counting
        data = app.journal_store.data_dir
        with closing(raw(data)) as conn:
            before = conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
        for _ in range(3):
            readiness(app)
        assert jobs == []
        assert list(data.glob(f"{js.PAIR_PREFIX}*")) == []
        with closing(raw(data)) as conn:
            assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == before
            assert conn.execute("SELECT count(*) FROM notification").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM broker_observation").fetchone()[0] == 0
    finally:
        app.stop()
