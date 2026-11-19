"""P6.R unattended-operation readiness and hardening. Temporary data directories only; the
readiness report is diagnostic: it never enables a service, writes the journal or reads MT5."""

from __future__ import annotations

import ast
import json
import re
from contextlib import closing
from pathlib import Path

import pytest
from test_decision_journal import _app, _call
from test_journal_store import raw

from johanna.api import BackendAPI
from johanna.instance_lock import InstanceLockError
from johanna.operations import _REMEDIATION, readiness

ROOT = Path(__file__).resolve().parents[1]
CODES = {
    "scheduler_disabled",
    "costs_unverified",
    "monitoring_not_activated",
    "notifications_not_activated",
    "execution_disabled",
}


@pytest.fixture
def app(tmp_path):
    application = _app(tmp_path)
    application.start()
    try:
        yield application
    finally:
        application.stop()


def test_healthy_manual_operation_and_unattended_never_permitted(app) -> None:
    report = readiness(app)
    assert report["unattended_operation_permitted"] is False
    assert {r["code"] for r in report["unattended_blocking_reasons"]} == CODES
    checks = {c["check"]: c for c in report["checks"]}
    for name in (
        "process",
        "journal_ready",
        "journal_integrity",
        "foreign_keys",
        "audit_chain",
        "journal_key",
        "scheduler_health",
    ):
        assert checks[name]["status"] == "ok", name
    journal = report["journal"]
    assert journal["installed_schema_version"] == 6 == journal["registered_schema_version"]
    assert journal["schema_matches_registration"] is True
    assert journal["staged_schema"].startswith("none staged")  # P6.2b.5: v6 registered
    assert report["scheduler"]["enabled"] is False
    services = report["services"]
    assert services["execution"]["enabled"] is False
    assert services["monitoring"]["enabled"] is False
    assert services["notifications"]["automatic"] is False
    assert services["cost_terms_configured"] is False


def test_readiness_api_is_read_only_and_contains_nothing_sensitive(app, tmp_path) -> None:
    with closing(raw(tmp_path)) as conn:
        before = conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
    status, body, headers = _call(BackendAPI(app), "/api/operations/readiness")
    assert status == "200 OK" and headers["Cache-Control"] == "no-store"
    assert body["label"].startswith("OPERATIONS READINESS — DIAGNOSTIC ONLY")
    text = json.dumps(body)
    key = (tmp_path / "trading_journal.key").read_bytes().hex()
    for secret in (
        key,
        "12345678",
        "Exness-MT5Trial9",
        "Exness Technologies",
        '"login"',
        str(tmp_path).replace("\\", "\\\\"),
        "key_check",
    ):
        assert secret not in text, secret
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == before


def test_readiness_makes_no_mt5_read_or_journal_write() -> None:
    tree = ast.parse((ROOT / "src/johanna/operations.py").read_text(encoding="utf-8"))
    calls = {
        n.func.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    for forbidden in (
        "instruments",
        "observe",
        "_worker_read",
        "analyse",
        "write",
        "backup",
        "set_enabled",
        "notify",
        "deliver_pending",
        "reconcile",
        "proposal",
    ):
        assert forbidden not in calls, forbidden


def test_refused_journal_is_reported_with_remediation(tmp_path) -> None:
    first = _app(tmp_path)
    first.start()
    first.stop()
    (tmp_path / "trading_journal.sqlite3").write_bytes(b"corrupt" * 100)
    broken = _app(tmp_path)
    broken.start()
    try:
        status, body, _ = _call(BackendAPI(broken), "/api/operations/readiness")
        assert status == "200 OK" and body["manual_operation"] == "attention"
        check = next(c for c in body["checks"] if c["check"] == "journal_ready")
        assert check["status"] == "attention"
        assert check["detail"]["reason"] == "corrupt_database"
        assert check["remediation"].endswith("#journal-refused")
        assert body["unattended_operation_permitted"] is False
    finally:
        broken.stop()


def test_tampered_audit_chain_is_reported(app, tmp_path) -> None:
    with closing(raw(tmp_path)) as conn:  # a temporary copy's journal, altered on purpose
        conn.execute("DROP TRIGGER audit_no_update")
        conn.execute("UPDATE audit_log SET action='tampered' WHERE seq=1")
    check = next(c for c in readiness(app)["checks"] if c["check"] == "audit_chain")
    assert check["status"] == "attention" and check["detail"]["first_bad_seq"] == 1
    assert check["remediation"].endswith("#audit-chain-failure")


def test_restart_never_enables_a_disabled_service(tmp_path) -> None:
    for _ in range(2):
        application = _app(tmp_path)
        application.start()
        try:
            assert application.shadow.enabled() is False
            assert application.shadow.status()["state"] == "disabled"
            assert application.monitoring.enabled is False
            assert application.monitoring.status()["last_observation"] is None
            assert application.notifications.status()["automatic_notifications"] is False
            assert application.risk.costs.configured is False
            with closing(raw(tmp_path)) as conn:
                assert (
                    conn.execute(
                        "SELECT count(*) FROM evaluation_cycle "
                        "WHERE status IN ('PENDING','RUNNING')"
                    ).fetchone()[0]
                    == 0
                )
        finally:
            application.stop()


def test_a_second_backend_on_the_same_data_is_refused(app, tmp_path) -> None:
    second = _app(tmp_path)
    with pytest.raises(InstanceLockError):
        second.start()
    assert app.journal_store.ready  # the running backend is unaffected


def test_every_remediation_points_to_a_runbook_section() -> None:
    runbook = (ROOT / "docs/operations-runbook.md").read_text(encoding="utf-8")
    anchors = {
        re.sub(r"[^a-z0-9 -]", "", line[3:].strip().lower()).replace(" ", "-")
        for line in runbook.splitlines()
        if line.startswith("## ")
    }
    for target in _REMEDIATION.values():
        assert target.split("#", 1)[1] in anchors, target


def test_mt5_states_are_reported_exactly(app) -> None:
    """T-1: exact states, not 'any state'. A started backend that has not read MT5 is idle
    (ok); an MT5 failure is reported unavailable with its reason and a runbook pointer."""
    from johanna.live_market import MarketUnavailable  # noqa: PLC0415

    report = readiness(app)
    assert report["mt5"] == {**report["mt5"], "state": "idle", "access": "read_only"}
    check = next(c for c in report["checks"] if c["check"] == "mt5_connection")
    assert check["status"] == "ok"
    app.live_market._unavailable = MarketUnavailable("terminal_not_running", "closed")
    report = readiness(app)
    assert report["mt5"]["state"] == "unavailable"
    assert report["mt5"]["unavailable_reason"] == "terminal_not_running"
    check = next(c for c in report["checks"] if c["check"] == "mt5_connection")
    assert check["status"] == "attention" and check["remediation"].endswith("#mt5-unavailable")
    assert report["manual_operation"] == "attention"
