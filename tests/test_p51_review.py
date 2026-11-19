"""P5.1 review corrections: fingerprint stability, startup isolation, migration/backup safety
and audit canonicalisation. Temporary directories and synthetic data only."""

from __future__ import annotations

import copy
import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta, timezone

import pytest
from risk_fixtures import BUY_LEVEL, PRODUCTION_COSTS, StubAnalysis, envelope
from t6_fixtures import trend_bars
from test_analysis_engine import history
from test_decision_journal import FixedAnalysis, _app, _call, journal, repolled
from test_journal_store import Acl, raw, started
from test_market_analysis import RETRIEVED, FakeLive, FixedClock, service
from test_window_validation import snapshot

from johanna import journal_store as js
from johanna.api import BackendAPI
from johanna.decision_journal import CapturingAnalysis, analysis_record
from johanna.journal_store import DB_NAME, KEY_NAME, JournalError, canonical_json, validate_pair


def real_envelope(bars, age):
    live = FakeLive(lambda s, t, c, n: snapshot(bars()))
    return service(live, FixedClock(RETRIEVED + timedelta(seconds=age))).analyse("EURUSD", "H1")


# ====================================================================== 2. fingerprint
def test_non_significant_diagnostics_do_not_create_decisions(tmp_path) -> None:
    j = journal(tmp_path)
    base = envelope(level=BUY_LEVEL)
    for i in range(10):
        env = repolled(base)  # new snapshot id and evaluation time
        env["served_at_utc"] = f"2026-11-17T10:00:{i:02d}Z"
        env["service"]["timing_ms"] = {"snapshot": 10.0 + i, "evaluation": 3.0, "total": 13.0 + i}
        env["service"]["data_age_at_evaluation_ms"] = 400.0 + i
        env["overlay"] = {"changed": i}
        env["window_validation"] = {"detail": f"diagnostic {i}"}
        latest = env["result"]["features"]["latest"]
        latest["EMA20"]["value"] += i * 1e-6  # indicator values are not decision identity
        latest["ATR14"]["value"] += i * 1e-7
        j.record_analysis(env, "analysis_request")
    page = j.list()
    assert len(page["decisions"]) == 1 and page["decisions"][0]["observations"] == 10
    detail = j.get(page["decisions"][0]["id"])["decision"]
    assert detail["observations"] == 10  # counters stay accurate


def test_real_r1_at_different_stale_ages_is_one_decision(tmp_path) -> None:
    """Real pipeline: the stale ages live in the window validation, not in T6 reasons."""
    j = journal(tmp_path)
    for age in (5.001, 6.5, 9.4, 30.0):
        env = real_envelope(trend_bars, age)
        assert env["result"]["decision"]["rule"] == "R1"
        j.record_analysis(env, "analysis_request")
    assert [d["observations"] for d in j.list()["decisions"]] == [4]


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r["decision"].update(rule="R6", outcome="MARKET_CONDITION"),
        lambda r: r["opportunity"].update(direction="bearish"),
        lambda r: r["inputs"].update(validation_verdict="provisional"),
        lambda r: r["market_condition"]["dimensions"]["trend_regime"].update(state="ranging"),
        lambda r: r["decision"]["reasons"].append("failed check: TC4_pullback"),
        lambda r: r.update(as_of_utc="2030-01-01T00:00:00Z"),
    ],
)
def test_decision_significant_changes_create_new_decisions(tmp_path, change) -> None:
    j = journal(tmp_path)
    env = envelope(level=BUY_LEVEL)
    first = j.record_analysis(env, "analysis_request")
    changed = copy.deepcopy(env)
    change(changed["result"])
    second = j.record_analysis(changed, "analysis_request")
    assert second != first
    assert j.get(second)["decision"]["previous_decision_id"] == first


def test_reason_order_does_not_change_identity() -> None:
    env = envelope(level=BUY_LEVEL)
    env["result"]["decision"]["reasons"] = ["b reason", "a reason"]
    flipped = copy.deepcopy(env)
    flipped["result"]["decision"]["reasons"] = ["a reason", "b reason"]
    assert (
        analysis_record(env)["analysis_fingerprint"]
        == analysis_record(flipped)["analysis_fingerprint"]
    )


def test_r1_with_different_window_causes_is_the_same_t6_decision(tmp_path) -> None:
    """Documents the specified behaviour: stale data and insufficient history both give the
    same T6 decision (NO_TRADE, R1, window rejected, same reasons), so they share one identity.
    The cause is window-validation detail, which the frozen design keeps out of identity."""
    j = journal(tmp_path)
    j.record_analysis(real_envelope(trend_bars, 9.4), "analysis_request")
    j.record_analysis(real_envelope(lambda: history(660), 0.8), "analysis_request")
    decisions = j.list()["decisions"]
    assert len(decisions) == 1 and decisions[0]["observations"] == 2
    assert decisions[0]["analysis_outcome"] == "NO_TRADE" and decisions[0]["t6_rule"] == "R1"


# ====================================================================== 3. startup isolation
def _prepare(tmp_path, mode):
    """Create a journal, then put it into ``mode`` before a fresh application starts."""
    if mode == "failed_initialisation":
        return
    first = _app(tmp_path)
    first.start()
    first.stop()
    if mode == "missing_key":
        (tmp_path / KEY_NAME).unlink()
    elif mode == "mismatched_key":
        (tmp_path / KEY_NAME).write_bytes(os.urandom(32))
    elif mode == "corrupt_database":
        (tmp_path / DB_NAME).write_bytes(b"not sqlite" * 200)


EXPECTED = {
    "missing_key": ("ready", "missing", None),
    "mismatched_key": ("ready", "mismatch", None),
    "corrupt_database": ("refused", None, "corrupt_database"),
    "failed_initialisation": ("refused", None, "initialisation_failed"),
    "failed_migration": ("refused", None, "migration_failed"),
}


@pytest.mark.parametrize("mode", list(EXPECTED))
def test_journal_failures_never_stop_analysis_or_risk(tmp_path, mode) -> None:
    _prepare(tmp_path, mode)
    app = _app(tmp_path)
    if mode == "failed_initialisation":
        app.journal_store._fault = lambda step: (
            (_ for _ in ()).throw(RuntimeError("x")) if step == "after_meta" else None
        )
    if mode == "failed_migration":
        app.journal_store._migrations = (*js.MIGRATIONS, (NEXT, "CREATE TABLE broken ("))
    app.start()  # never raises because of the journal
    try:
        env = envelope(level=BUY_LEVEL)
        app.market_analysis = FixedAnalysis(env)
        app.risk.analysis = CapturingAnalysis(StubAnalysis(env), app.decisions)
        app.risk.costs = PRODUCTION_COSTS
        api = BackendAPI(app)
        assert app.health()[0] is True
        status, body, _ = _call(api, "/api/market/analysis", "symbol=EURUSD&timeframe=H1")
        assert status == "200 OK" and body["result"]["decision"]["rule"] == "R7"
        status, risk, _ = _call(api, "/api/risk/proposal", "symbol=EURUSD&timeframe=H1")
        assert status == "200 OK" and risk["status"] == "UNVERIFIED"
        state, key_status, reason = EXPECTED[mode]
        journal_status = app.status()["services"]["trading_journal"]
        assert journal_status["state"] == state
        assert journal_status["hmac_available"] is False
        assert journal_status["hmac_prerequisite"] == "refused"
        assert journal_status["monitoring_and_reconciliation"] == "reported_by_monitoring_service"
        status, payload, _ = _call(api, "/api/journal/decisions")
        if state == "ready":  # decisions still recorded; HMAC-dependent features refused
            assert journal_status["key_status"] == key_status
            assert status == "200 OK" and payload["journal"]["hmac_available"] is False
            assert len(payload["decisions"]) == 1
        else:
            assert journal_status["reason"] == reason
            assert status == "503 Service Unavailable" and payload["reason"] == reason
            assert app.decisions.capture_failures >= 2
    finally:
        app.stop()


# ====================================================================== 4. migration/backup
# Version-agnostic: a migration from the shipped latest schema to the next one.
LATEST = js.MIGRATIONS[-1][0]
NEXT = LATEST + 1
V2_BROKEN = (NEXT, "CREATE TABLE broken (")


def _decision_count(tmp_path) -> int:
    with raw(tmp_path) as conn:
        return conn.execute("SELECT count(*) FROM decision").fetchone()[0]


def test_backup_completes_and_validates_before_a_migration_that_fails(tmp_path) -> None:
    j = journal(tmp_path)
    j.record_analysis(envelope(level=BUY_LEVEL), "analysis_request")
    j.store.stop()
    store = started(tmp_path, migrations=(*js.MIGRATIONS, V2_BROKEN))
    assert store.status()["reason"] == "migration_failed"
    pairs = [p for p in tmp_path.iterdir() if p.name.startswith("trading_journal.backup-")]
    assert (
        len(pairs) == 1 and f"premigration-v{LATEST}" in pairs[0].name and validate_pair(pairs[0])
    )
    with sqlite3.connect(pairs[0] / DB_NAME) as conn:
        assert conn.execute("SELECT count(*) FROM decision").fetchone()[0] == 1
    # the original database is intact and still at the previous version
    with raw(tmp_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert _decision_count(tmp_path) == 1


def test_backup_failure_blocks_the_migration(tmp_path) -> None:
    started(tmp_path).stop()
    store = js.JournalStore(
        tmp_path,
        acl=Acl(),
        acl_check=Acl(),
        migrations=(*js.MIGRATIONS, (NEXT, "CREATE TABLE ok (id INTEGER);")),
        fault=lambda step: (
            (_ for _ in ()).throw(RuntimeError("x")) if step == "backup_after_snapshot" else None
        ),
    )
    store.start()
    assert store.status()["state"] == "refused" and store.status()["reason"] == "backup_failed"
    with raw(tmp_path) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == LATEST
        assert conn.execute("SELECT name FROM sqlite_master WHERE name='ok'").fetchone() is None
    assert [p.name for p in tmp_path.iterdir() if p.is_dir()] == []  # nothing published or left


def test_retention_never_deletes_premigration_pairs(tmp_path) -> None:
    store = started(tmp_path)
    keep = store.backup("premigration-v1")
    for _ in range(3):
        store.backup("daily")
    store.prune_daily(keep=1)
    assert validate_pair(keep)
    assert sum("-daily-" in p.name for p in store.valid_pairs()) == 1


def test_only_valid_pairs_are_restore_sources(tmp_path) -> None:
    store = started(tmp_path)
    good = store.backup("daily")
    broken = tmp_path / "trading_journal.backup-daily-20200101T000000000000Z"
    broken.mkdir()
    (broken / DB_NAME).write_bytes(b"x")
    assert store.valid_pairs() == [good]


# ====================================================================== 5. canonicalisation
VALUES = [
    None,
    {"text": "Ünïcødé ✓ 日本語 🚀"},
    {"escaped": 'quote " backslash \\ newline \n tab \t control \x01'},
    {"b": 1, "a": {"d": [1, None, {"z": True, "y": 1.5}], "c": "x"}},
]


def test_insertion_and_verification_use_the_same_canonical_form(tmp_path) -> None:
    store = started(tmp_path)
    at = datetime(2026, 1, 1, 2, 0, 0, 123456, tzinfo=timezone(timedelta(hours=2)))
    for value in VALUES:
        with store.write() as conn:
            store.append_audit(
                conn, action="t", table="meta", record_id=None, old=value, new=value, at=at
            )
    with store.read() as conn:
        rows = conn.execute("SELECT * FROM audit_log WHERE action='t' ORDER BY seq").fetchall()
    for row, value in zip(rows, VALUES, strict=True):
        assert row["recorded_utc"] == "2026-01-01T00:00:00.123456Z"  # converted to UTC
        assert row["new_values"] == canonical_json(value)
        assert row["old_values"] == canonical_json(value)
        fields = [
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
        assert row["row_hash"] == js.audit_row_hash(fields)
    assert rows[0]["new_values"] is None and rows[0]["record_id"] is None
    assert "Ünïcødé ✓ 日本語 🚀" in rows[1]["new_values"]  # UTF-8, not \u-escaped
    assert '\\"' in rows[2]["new_values"] and "\\n" in rows[2]["new_values"]
    assert store.verify_audit_chain()["valid"] is True


def test_canonical_json_is_order_independent_and_deterministic() -> None:
    a = {"b": 1, "a": {"d": [1, None], "c": "x"}}
    b = {"a": {"c": "x", "d": [1, None]}, "b": 1}
    assert canonical_json(a) == canonical_json(b) == '{"a":{"c":"x","d":[1,null]},"b":1}'
    fields = [
        1,
        "2026-01-01T00:00:00.000000Z",
        "j",
        "x",
        "meta",
        None,
        None,
        canonical_json(a),
        "0" * 64,
    ]
    assert js.audit_row_hash(fields) == js.audit_row_hash(list(fields))


def test_non_finite_values_are_rejected_and_nothing_is_written(tmp_path) -> None:
    store = started(tmp_path)
    with pytest.raises(ValueError):
        with store.write() as conn:
            store.append_audit(
                conn,
                action="t",
                table="meta",
                record_id=None,
                new={"x": float("nan")},
                at=datetime.now(UTC),
            )
    with store.read() as conn:
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 1  # genesis only


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("row_hash", "not-a-hash"),
        ("row_hash", "A" * 64),
        ("row_hash", "0" * 63),
        ("prev_hash", "z" * 64),
        ("recorded_utc", "2026-01-01T00:00:00Z"),
    ],
)
def test_invalid_hash_or_field_values_fail_verification(tmp_path, column, value) -> None:
    store = started(tmp_path)
    with store.write() as conn:
        store.append_audit(
            conn, action="t", table="meta", record_id="1", new={"a": 1}, at=datetime.now(UTC)
        )
    conn = raw(tmp_path)
    conn.execute("DROP TRIGGER audit_no_update")
    conn.execute(f"UPDATE audit_log SET {column}=? WHERE seq=2", (value,))
    conn.close()
    assert store.verify_audit_chain()["valid"] is False


def test_journal_error_messages_are_generic() -> None:
    assert str(JournalError("corrupt_database")) == "corrupt database"
    assert json.dumps(JournalError("acl_failed").code) == '"acl_failed"'
