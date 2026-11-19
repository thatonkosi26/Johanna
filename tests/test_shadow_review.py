"""P6.1 final review: scheduler lifecycle and health, Phase 3 account cache interaction, and the
P5.1 migration-runner regressions. Temporary directories only; MT5 is the in-process fake."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from fake_mt5 import FakeMT5
from test_decision_journal import _app
from test_journal_store import raw, started
from test_shadow_evaluation import MIDNIGHT, Risk, cycles, setup
from test_shadow_journal import V2, _v2_journal

from johanna import journal_store as js
from johanna.account_intelligence import AccountIntelligenceService
from johanna.adapters.mt5 import MetaTrader5Adapter, ServerClock
from johanna.journal_store import DB_NAME, KEY_NAME, migration_statements, validate_pair
from johanna.live_market import LiveMarketService, background_reads
from johanna.shadow_evaluation import MAX_CONSECUTIVE_ERRORS, ShadowEvaluationService


# ====================================================================== 2. lifecycle
def _wait(predicate, seconds=5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_healthy_only_while_enabled_running_and_error_free(tmp_path) -> None:
    service, clock, _, _ = setup(tmp_path, tick_seconds=0.01, enable=False)
    assert service.status()["health"] == "disabled"
    service.start()
    service.set_enabled(True)
    try:
        assert _wait(lambda: service.status()["last_pass_utc"] is not None)
        status = service.status()
        assert (status["state"], status["health"], status["health_reason"]) == (
            "running",
            "healthy",
            None,
        )
    finally:
        service.stop()
    stopped = service.status()
    assert stopped["enabled"] is True and stopped["health"] == "unhealthy"  # enabled, not running


def test_a_failed_pass_is_unhealthy_until_a_pass_succeeds(tmp_path, monkeypatch) -> None:
    service, clock, _, _ = setup(tmp_path, tick_seconds=0.01)
    failures = {"left": 2}
    original = service._ensure_windows

    def flaky(now):
        if failures["left"]:
            failures["left"] -= 1
            raise RuntimeError("journal hiccup")
        return original(now)

    monkeypatch.setattr(service, "_ensure_windows", flaky)
    service._stop = threading.Event()
    with pytest.raises(RuntimeError):
        service.run_once()
    service._error, service._consecutive_errors = "RuntimeError", 1  # as the loop records it
    service._state, service._thread = "running", threading.current_thread()
    status = service.status()
    assert status["health"] == "unhealthy" and "RuntimeError" in status["health_reason"]
    service._thread = None
    service.start()
    try:
        assert _wait(lambda: service.status()["health"] == "healthy")
        status = service.status()
        assert status["error"] is None and status["last_error"] == "RuntimeError"
        assert status["last_error_utc"] is not None and status["consecutive_errors"] == 0
    finally:
        service.stop()


def test_five_consecutive_errors_stop_the_scheduler_visibly(tmp_path, monkeypatch) -> None:
    service, clock, _, _ = setup(tmp_path, tick_seconds=0.01)

    def broken(now):
        raise RuntimeError("persistent failure")

    monkeypatch.setattr(service, "_ensure_windows", broken)
    service.start()
    assert _wait(lambda: service.status()["state"] == "error")
    status = service.status()
    assert status["health"] == "unhealthy" and status["enabled"] is True
    assert status["consecutive_errors"] == MAX_CONSECUTIVE_ERRORS
    assert status["error"] == "RuntimeError" and status["health_reason"] == "scheduler error"
    assert not service._thread.is_alive()  # not restarted automatically
    time.sleep(0.1)
    assert service.status()["state"] == "error"  # stays visibly unhealthy
    service.stop()
    assert service.enabled() is True  # the persisted choice is never changed by an error


def test_restart_never_changes_the_persisted_enabled_state(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path)
    for _ in range(2):
        revived = ShadowEvaluationService(
            service.store, service.decisions, analysis, Risk(), ServerClock(), clock=clock
        )
        revived.start()
        revived.stop()
        assert revived.enabled() is True
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM shadow_scheduler_setting").fetchone()[0] == 1


def test_paused_scheduler_is_unhealthy(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, tick_seconds=0.01)
    service.start()
    try:
        service._pause_until = clock.now + timedelta(minutes=5)
        status = service.status()
        assert status["health"] == "unhealthy" and "paused" in status["health_reason"]
    finally:
        service.stop()


def test_no_scheduler_thread_survives_backend_shutdown(tmp_path) -> None:
    app = _app(tmp_path)
    app.start()
    app.shadow.set_enabled(True)
    assert app.shadow._thread is not None and app.shadow._thread.is_alive()
    app.stop()
    assert app.shadow._thread is None
    assert not [t for t in threading.enumerate() if t.name == "johanna-shadow-evaluation"]
    assert app.shadow.status()["state"] in ("stopped", "blocked")  # journal closed too


def test_shadow_stops_before_the_services_it_depends_on(tmp_path) -> None:
    app = _app(tmp_path)
    app.start()
    order = []
    original_shadow, original_services = app.shadow.stop, app.services.stop
    app.shadow.stop = lambda: (order.append("shadow"), original_shadow())[1]
    app.services.stop = lambda: (order.append("services"), original_services())[1]
    app.stop()
    assert order == ["shadow", "services"]


def test_crash_after_capture_never_duplicates_decisions_or_gates(tmp_path, monkeypatch) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(minutes=58))
    clock.now = MIDNIGHT + timedelta(hours=1, seconds=30)
    real_gates = service._gates
    calls = {"n": 0}

    def crash_once(conn, cycle_id, gates, now):
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt  # a process death: nothing in the scheduler catches it
        return real_gates(conn, cycle_id, gates, now)

    monkeypatch.setattr(service, "_gates", crash_once)
    with pytest.raises(KeyboardInterrupt):
        service.run_once()  # the decision was captured; the completion was rolled back
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM decision").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM gate_outcome").fetchone()[0] == 0
        assert (
            conn.execute("SELECT count(*) FROM evaluation_cycle WHERE status='RUNNING'").fetchone()[
                0
            ]
            == 1
        )
    clock.advance(seconds=30)
    revived = ShadowEvaluationService(
        service.store, service.decisions, analysis, Risk(), ServerClock(), clock=clock
    )
    revived._recover_interrupted(clock.now)
    revived.run_once()
    revived.run_once()
    with closing(raw(tmp_path)) as conn:
        decisions = conn.execute("SELECT id FROM decision").fetchall()
        statuses = [r[0] for r in conn.execute("SELECT status FROM evaluation_cycle")]
        per_cycle = conn.execute(
            "SELECT evaluation_id, count(*), count(DISTINCT seq) FROM gate_outcome "
            "GROUP BY evaluation_id"
        ).fetchall()
        completed_audits = conn.execute(
            "SELECT record_id, count(*) FROM audit_log WHERE action='evaluation.completed' "
            "GROUP BY record_id"
        ).fetchall()
    assert len(decisions) == 3  # one per instrument, the crashed one included exactly once
    assert statuses.count("COMPLETED") == 3
    assert all(total == distinct for _, total, distinct in per_cycle)
    assert all(count == 1 for _, count in completed_audits) and len(completed_audits) == 3
    assert {c["status"] for c in cycles(revived)} == {"COMPLETED"}


# ====================================================================== 3. account cache
class ManualClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def _account_stack():
    fake = FakeMT5()
    clock = ManualClock()
    live = LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake),
        terminal_running=lambda: fake.running,
        reconnect_interval=0,
        clock=clock,
    )
    account = AccountIntelligenceService(live, clock=clock)
    return live, account, clock


def test_background_refresh_stores_a_newer_snapshot_with_accurate_age() -> None:
    live, account, clock = _account_stack()
    try:
        first = account.summary()
        assert first["status"] == "LIVE" and first["served_from_cache"] is False
        dashboard_success = live.status()["last_success_utc"]
        fetched_first = account._fetched_at
        clock.now += timedelta(seconds=6)  # beyond the 5 s cache
        with background_reads(quiet_seconds=0):
            refreshed = account.summary()
        assert refreshed["status"] == "LIVE" and refreshed["served_from_cache"] is False
        assert refreshed["age_seconds"] == 0.0 and account._fetched_at > fetched_first
        assert live.status()["last_success_utc"] == dashboard_success  # dashboard state intact
        assert live._quotes == {} and live._bars == {}  # no quote/bar caches written
    finally:
        live.stop()


def test_a_deferred_refresh_never_presents_the_old_snapshot_as_fresh() -> None:
    live, account, clock = _account_stack()
    try:
        account.summary()
        fetched = account._fetched_at
        snapshot = account._snapshot
        clock.now += timedelta(seconds=8)
        live.quotes(["EURUSD"])  # foreground activity: the quiet window is not satisfied
        with background_reads(quiet_seconds=5) as reads:
            result = account.summary()
        assert reads.deferrals == 1
        assert result["status"] == "CACHED" and result["served_from_cache"] is True
        assert result["age_seconds"] == 8.0  # its true age, never zero
        assert account._fetched_at == fetched and account._snapshot is snapshot
    finally:
        live.stop()


def test_concurrent_dashboard_and_background_reads_are_serialised_and_monotonic() -> None:
    live, account, clock = _account_stack()
    seen: list[datetime] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def worker(background: bool) -> None:
        try:
            for _ in range(15):
                with lock:
                    clock.now += timedelta(seconds=2)
                if background:
                    with background_reads(quiet_seconds=0):
                        account.summary()
                else:
                    account.summary()
                with account._lock:
                    seen.append(account._fetched_at)
        except BaseException as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i % 2 == 0,)) for i in range(6)]
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    finally:
        live.stop()
    assert errors == []
    assert seen == sorted(seen)  # a stored snapshot is never replaced by an older one


# ====================================================================== 4. migration runner
def test_embedded_commit_and_begin_cannot_split_a_migration(tmp_path) -> None:
    _v2_journal(tmp_path)
    sneaky = (*V2, (3, "CREATE TABLE first_half (id INTEGER); COMMIT; BEGIN; CREATE TABLE x ("))
    store = started(tmp_path, migrations=sneaky)
    assert store.status()["reason"] == "migration_failed"
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert (
            conn.execute("SELECT name FROM sqlite_master WHERE name='first_half'").fetchone()
            is None
        )


@pytest.mark.parametrize(
    "sql",
    [
        "COMMIT;",
        "CREATE TABLE a (x); END;",
        "SAVEPOINT s; CREATE TABLE a (x);",
        "ROLLBACK;",
        "VACUUM;",
        "CREATE TABLE a (",
    ],
)
def test_migration_statements_refuse_transaction_control_and_incomplete_sql(sql) -> None:
    with pytest.raises(sqlite3.OperationalError):
        migration_statements(sql)


def test_trigger_bodies_and_literals_are_kept_whole() -> None:
    sql = (
        "-- comment; with a semicolon\n"
        "CREATE TABLE t (v TEXT DEFAULT 'a;b');\n"
        "CREATE TRIGGER g BEFORE UPDATE ON t BEGIN SELECT RAISE(ABORT, 'no; never'); END;\n"
    )
    statements = migration_statements(sql)
    assert len(statements) == 2 and statements[1].startswith("CREATE TRIGGER")


def test_failed_v3_migration_keeps_key_audit_and_data_byte_identical(tmp_path) -> None:
    before = _v2_journal(tmp_path)
    broken = (*V2, (3, js.SCHEMA_V3 + "\nCREATE TABLE broken ("))
    store = started(tmp_path, migrations=broken)
    assert store.status()["reason"] == "migration_failed"
    assert (tmp_path / KEY_NAME).read_bytes() == before["key"]
    with closing(raw(tmp_path)) as conn:
        for table, rows in before["tables"].items():
            assert [tuple(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY rowid")] == rows
        assert conn.execute("PRAGMA foreign_keys").fetchone() is not None


def test_v1_journal_migrates_through_v2_and_v3_after_one_v1_backup(tmp_path) -> None:
    v1 = started(tmp_path, migrations=js.MIGRATIONS[:1])
    v1.stop()
    store = started(tmp_path)  # through v2, v3 and (since P6.2a) v4 after one v1 backup
    assert store.ready and store.status()["schema_version"] == js.MIGRATIONS[-1][0]
    pairs = store.valid_pairs()
    assert len(pairs) == 1 and "premigration-v1" in pairs[0].name
    manifest = json.loads((pairs[0] / "pair.json").read_text())
    assert manifest["schema_version"] == 1  # the backup's own (pre-migration) version
    with closing(sqlite3.connect(pairs[0] / DB_NAME)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1


def test_a_pair_with_a_broken_audit_chain_is_not_a_valid_backup(tmp_path) -> None:
    store = started(tmp_path)
    pair = store.backup("daily")
    assert validate_pair(pair)
    with closing(sqlite3.connect(pair / DB_NAME)) as conn:
        conn.execute("DROP TRIGGER audit_no_update")
        conn.execute("UPDATE audit_log SET action='tampered'")
        conn.commit()
    assert validate_pair(pair) is False  # hash and audit checks both catch it


def test_v1_to_v2_tests_still_exercise_the_real_v1_to_v2_path() -> None:
    import test_trade_logbook

    assert test_trade_logbook.V1_TO_V2 == js.MIGRATIONS[:2]
    assert [v for v, _ in test_trade_logbook.V1_TO_V2] == [1, 2]
    assert test_trade_logbook.V1_TO_V2[1][1] is js.SCHEMA_V2


def test_a_failed_completion_never_leaves_a_window_running(tmp_path, monkeypatch) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(minutes=58))
    clock.now = MIDNIGHT + timedelta(hours=1, seconds=30)

    def refuse(*args, **kwargs):
        raise ValueError("risk result does not belong to this decision")

    analysis.env = __import__("test_shadow_evaluation").CANDIDATE_UNVERIFIED
    monkeypatch.setattr(service.decisions, "record_risk", refuse)
    service.run_once()
    assert {(c["status"], c["reason_code"], c["reason_detail"]) for c in cycles(service)} == {
        ("FAILED", "evaluation_error", "ValueError")
    }


def test_a_running_row_past_its_deadline_is_failed_on_the_next_pass(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=MIDNIGHT + timedelta(minutes=58))
    analysis.closure = "clock_only"
    clock.now = MIDNIGHT + timedelta(hours=1, seconds=30)
    service.run_once()
    with closing(raw(tmp_path)) as conn:
        conn.execute("UPDATE evaluation_cycle SET status='RUNNING' WHERE status='PENDING'")
    clock.now = MIDNIGHT + timedelta(hours=1, minutes=11)
    service.run_once()  # same process, no restart
    rows = [c for c in cycles(service) if c["window_close_utc"] == "2026-11-18T01:00:00Z"]
    assert {(c["status"], c["reason_code"]) for c in rows} == {("FAILED", "interrupted")}
