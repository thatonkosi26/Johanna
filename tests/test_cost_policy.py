"""P6.2b.2 cost-evidence and policy-management infrastructure: the staged registry (schema v5,
NOT registered for production), user-only decisions, expiry, invalidation, supersession,
broker-terms change detection and the read-only readiness diagnostics.

Temporary journals only. Nothing here configures production costs or approves a real value."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest
from risk_fixtures import (
    PRODUCTION_COSTS,
    fixture_broker_terms,
    make_fake,
    negative_swap,
    propose,
    synthetic_terms,
)
from test_decision_journal import _app, _call
from test_journal_store import raw, started

from johanna import journal_store as js
from johanna.api import BackendAPI
from johanna.cost_evidence import (
    ACCOUNT_TYPE,
    HOLDING_PROPOSALS,
    CostComponent,
    CostTerms,
    broker_terms_fingerprint,
)
from johanna.cost_policy import CostPolicyRegistry, confirmation_value, cost_readiness
from johanna.journal_store import JournalError
from johanna.risk_engine import COST_TERMS, UNVERIFIED

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
STAGED = js.MIGRATIONS  # Phase 2: v5 is registered
V4 = js.MIGRATIONS[:4]  # a journal still on v4 (before the v5 migration)
CONFIRM = "7 USC/lot RT per 1000 units"  # what the user types back to approve the fixture


class Clock:
    def __init__(self, now=NOW) -> None:
        self.now = now

    def __call__(self):
        return self.now


def evidence(**change) -> CostComponent:
    base = {
        "component": "commission",
        "value": 0.035,
        "currency": "USD",
        "unit": "amount_per_lot",
        "basis": "per_side",
        "instruments": ("EURUSDc",),
        "account_type": ACCOUNT_TYPE,
        "source_reference": "synthetic test document",
        "source_description": "synthetic statement (test only)",
        "evidence_date": NOW - timedelta(hours=1),
        "effective_utc": NOW - timedelta(hours=1),
        "status": "UNVERIFIED",
        "lot_units": 1000.0,
        "broker_terms": {"EURUSDc": fixture_broker_terms()["EURUSDc"]},
    }
    return CostComponent(**{**base, **change})


def registry(tmp_path, clock=None):
    clock = clock or Clock()
    store = started(tmp_path, migrations=STAGED, clock=clock)
    return CostPolicyRegistry(store, clock=clock), store, clock


# ====================================================================== staging
def test_schema_v5_is_registered_exactly_once() -> None:
    assert [v for v, _ in js.MIGRATIONS] == [1, 2, 3, 4, 5, 6]  # v6 registered in P6.2b.5
    assert js.MIGRATIONS[4] == (5, js.SCHEMA_V5)
    assert not hasattr(js, "STAGED_MIGRATIONS")  # nothing staged


def test_registry_reports_not_installed_on_v4(tmp_path) -> None:
    store = started(tmp_path, migrations=V4)  # a journal still on v4
    reg = CostPolicyRegistry(store)
    assert reg.installed() is False
    assert reg.history() == {"status": "not_installed", "records": []}
    with pytest.raises(JournalError) as err:
        reg.record_evidence(evidence())
    assert err.value.code == "not_installed"
    with closing(raw(tmp_path)) as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4


def test_v4_journal_migrates_to_v5_after_a_verified_backup(tmp_path) -> None:
    v4 = started(tmp_path, migrations=V4)
    with v4.write() as conn:
        v4.append_audit(conn, action="test.marker", table="meta", record_id=None, at=NOW)
    v4.stop()
    with closing(raw(tmp_path)) as conn:
        audit = [tuple(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY seq")]
    key = (tmp_path / js.KEY_NAME).read_bytes()
    store = started(tmp_path, migrations=js.MIGRATIONS[:5])  # the v4 -> v5 step
    assert store.ready and store.status()["schema_version"] == 5
    pairs = store.valid_pairs()
    assert len(pairs) == 1 and "premigration-v4" in pairs[0].name
    assert (tmp_path / js.KEY_NAME).read_bytes() == key
    with closing(raw(tmp_path)) as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM audit_log ORDER BY seq")] == audit
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert js.tables_installed(conn, "cost_policy_record", "notification", "broker_observation")
        assert conn.execute("SELECT count(*) FROM shadow_scheduler_setting").fetchone()[0] == 0
    assert store.verify_audit_chain()["valid"] is True


def test_schema_v5_is_purely_additive() -> None:
    for statement in js.migration_statements(js.SCHEMA_V5):
        assert statement.split()[0].upper() == "CREATE", statement[:40]
    assert "DROP" not in js.SCHEMA_V5.upper() and "ALTER" not in js.SCHEMA_V5.upper()


# ====================================================================== registry
def test_evidence_is_recorded_unverified_and_approved_only_by_the_user(tmp_path) -> None:
    reg, store, clock = registry(tmp_path)
    record = reg.record_evidence(evidence())
    assert reg.record_evidence(evidence()) == record  # deduplicated by fingerprint
    item = reg.history()["records"][0]
    assert item["effective_state"] == "RECORDED" and item["recorded_status"] == "UNVERIFIED"
    expected = confirmation_value("commission", item["record"])
    assert expected == CONFIRM  # 0.035 USD per side -> x100 -> x2, per 1000-unit lot
    with pytest.raises(ValueError, match="typed back"):
        reg.decide(record, "APPROVED", "ok", actor="user", confirmed_value="0.035")  # USD slip
    with pytest.raises(ValueError, match="only the user"):
        reg.decide(record, "APPROVED", "ok", actor="johanna", confirmed_value=expected)
    assert reg.history()["records"][0]["effective_state"] == "RECORDED"  # nothing automatic
    reg.decide(record, "APPROVED", "user check", actor="user", confirmed_value=expected)
    assert reg.history()["records"][0]["effective_state"] == "APPROVED"
    terms = reg.proposed_terms()
    assert terms.components[0].status == "VERIFIED"
    assert terms.components[0].approved_at_utc == NOW
    assert store.verify_audit_chain()["valid"] is True


def test_recording_verified_evidence_or_approved_holding_is_refused(tmp_path) -> None:
    reg, *_ = registry(tmp_path)
    with pytest.raises(ValueError):
        reg.record_evidence(evidence(status="VERIFIED", approved_at_utc=NOW))
    approved = HOLDING_PROPOSALS["H1"].__class__(
        **{**HOLDING_PROPOSALS["H1"].__dict__, "status": "APPROVED", "approved_at_utc": NOW}
    )
    with pytest.raises(ValueError):
        reg.record_holding(approved)


def test_database_refuses_automatic_or_non_user_approval(tmp_path) -> None:
    reg, *_ = registry(tmp_path)
    record = reg.record_evidence(evidence())
    with closing(raw(tmp_path)) as conn:
        with pytest.raises(sqlite3.IntegrityError):  # Johanna cannot approve
            conn.execute(
                "INSERT INTO cost_policy_decision VALUES ('D1', ?, 'APPROVED', 'x', '7', "
                "'johanna', 't')",
                (record,),
            )
        with pytest.raises(sqlite3.IntegrityError):  # approval needs the confirmed value
            conn.execute(
                "INSERT INTO cost_policy_decision VALUES ('D2', ?, 'APPROVED', 'x', NULL, "
                "'user', 't')",
                (record,),
            )
        with pytest.raises(sqlite3.IntegrityError):  # never stored as VERIFIED
            conn.execute(
                "INSERT INTO cost_policy_record(id, kind, timeframe, value, currency, unit, basis, "
                "instruments, status, record_json, record_fingerprint, broker_terms, lot_units, "
                "supersedes_id, actor, recorded_utc) VALUES ('R9','commission',NULL,1,'USC',"
                "'amount_per_lot','round_trip','[]','VERIFIED','{}','fp9',NULL,1000,NULL,'user','t')"
            )
        for sql in ("UPDATE cost_policy_record SET value=0", "DELETE FROM cost_policy_record"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(sql)


def test_approval_expires_after_thirty_days(tmp_path) -> None:
    reg, _, clock = registry(tmp_path)
    record = reg.record_evidence(evidence())
    reg.decide(record, "APPROVED", "ok", actor="user", confirmed_value=CONFIRM)
    clock.now = NOW + timedelta(days=31)
    assert reg.history()["records"][0]["effective_state"] == "EXPIRED"
    assert reg.proposed_terms().components == ()
    reg.decide(record, "APPROVED", "renewed", actor="user", confirmed_value=CONFIRM)
    assert reg.history()["records"][0]["effective_state"] == "APPROVED"  # explicit renewal


def test_invalidation_and_rejection_are_terminal(tmp_path) -> None:
    reg, *_ = registry(tmp_path)
    a = reg.record_evidence(evidence())
    reg.decide(a, "INVALIDATED", "broker terms changed", actor="johanna")
    assert reg.history()["records"][0]["effective_state"] == "INVALIDATED"
    with pytest.raises(sqlite3.IntegrityError):
        reg.decide(a, "APPROVED", "ok", actor="user", confirmed_value=CONFIRM)
    b = reg.record_evidence(evidence(value=0.05))
    reg.decide(b, "REJECTED", "not verbatim", actor="user")
    states = {r["id"]: r["effective_state"] for r in reg.history()["records"]}
    assert states[b] == "REJECTED"


def test_supersession_keeps_history(tmp_path) -> None:
    reg, *_ = registry(tmp_path)
    old = reg.record_evidence(evidence())
    new = reg.record_evidence(evidence(value=0.04), supersedes=old)
    states = {r["id"]: r["effective_state"] for r in reg.history()["records"]}
    assert states == {old: "SUPERSEDED", new: "RECORDED"}
    with pytest.raises(ValueError):
        reg.record_holding(HOLDING_PROPOSALS["H1"], supersedes=new)  # different kind


def test_broker_terms_change_is_detected(tmp_path) -> None:
    reg, *_ = registry(tmp_path)
    spec = {
        "contract_size": 1000.0,
        "digits": 5,
        "point": 1e-05,
        "tick_size": 1e-05,
        "currency_base": "EUR",
        "currency_profit": "USD",
        "currency_margin": "EUR",
        "execution_mode": "market",
    }
    fp = broker_terms_fingerprint("EURUSDc", "USC", "cent", spec)
    changed = broker_terms_fingerprint("EURUSDc", "USC", "cent", {**spec, "contract_size": 100000})
    assert fp != changed and fp.startswith("sha256:")
    assert broker_terms_fingerprint("EURUSDc", "USC", None, spec) is None  # unknown -> None
    gbp = broker_terms_fingerprint(
        "GBPUSDc", "USC", "cent", {**spec, "currency_base": "GBP", "currency_margin": "GBP"}
    )
    assert gbp != fp  # the symbol is part of the fingerprint: one never validates another
    record = reg.record_evidence(
        evidence(instruments=("EURUSDc", "GBPUSDc"), broker_terms={"EURUSDc": fp, "GBPUSDc": gbp})
    )
    reg.decide(record, "APPROVED", "ok", actor="user", confirmed_value=CONFIRM)
    both = reg.history({"EURUSDc": fp, "GBPUSDc": gbp})["records"][0]
    assert both["effective_state"] == "APPROVED"
    assert both["instrument_terms"] == {"EURUSDc": "MATCH", "GBPUSDc": "MATCH"}
    one = reg.history({"EURUSDc": changed, "GBPUSDc": gbp})["records"][0]
    assert one["instrument_terms"] == {"EURUSDc": "CHANGED", "GBPUSDc": "MATCH"}
    unknown = reg.history({"GBPUSDc": gbp})["records"][0]
    assert unknown["instrument_terms"] == {"EURUSDc": "UNKNOWN", "GBPUSDc": "MATCH"}


def test_engine_refuses_evidence_recorded_under_other_broker_terms() -> None:
    base = synthetic_terms()
    wrong = {s: "sha256:" + "0" * 64 for s in base.components[0].instruments}
    stale = tuple(CostComponent(**{**c.__dict__, "broker_terms": wrong}) for c in base.components)
    result = propose(costs=CostTerms(components=stale, holding=base.holding))
    assert result["status"] == UNVERIFIED and result["sizing"]["volume"] is None
    assert result["costs"]["commission"]["evidence"]["status"] == "BROKER_TERMS_CHANGED"
    assert (
        result["costs"]["terms"]["live_broker_terms_fingerprint"]
        == (fixture_broker_terms()["EURUSDc"])
    )
    ok = propose(costs=base)  # the fixture records each instrument's own fingerprint
    assert ok["costs"]["commission"]["evidence"]["status"] == "VERIFIED"


def test_holding_proposal_records_and_needs_hours_confirmation(tmp_path) -> None:
    reg, *_ = registry(tmp_path)
    record = reg.record_holding(HOLDING_PROPOSALS["H4"])
    item = reg.history()["records"][0]
    assert item["recorded_status"] == "PROPOSED" and item["timeframe"] == "H4"
    with pytest.raises(ValueError):
        reg.decide(record, "APPROVED", "ok", actor="user", confirmed_value="5 days")
    reg.decide(record, "APPROVED", "ok", actor="user", confirmed_value="120 hours")
    assert reg.proposed_terms().holding["H4"].status == "APPROVED"


def test_production_terms_are_untouched_by_the_registry(tmp_path) -> None:
    reg, *_ = registry(tmp_path)
    record = reg.record_evidence(evidence())
    reg.decide(record, "APPROVED", "ok", actor="user", confirmed_value=CONFIRM)
    assert COST_TERMS.components == () and COST_TERMS.holding == {}


# ====================================================================== readiness
def _payload() -> dict:
    fake = negative_swap(make_fake())  # EURUSDc long swap is a cost in these checks
    from risk_fixtures import StubAnalysis, stack  # noqa: PLC0415

    _, account, _ = stack(fake, StubAnalysis())
    return account.instruments()


def test_readiness_explains_every_blocking_reason() -> None:
    report = cost_readiness(PRODUCTION_COSTS, datetime.now(UTC), _payload())
    assert report["approval_possible_anywhere"] is False
    assert report["production_terms_configured"] is False
    assert report["registry"] == {"status": "not_installed"}
    eur = next(i for i in report["instruments"] if i["broker_symbol"] == "EURUSDc")
    assert eur["commission"]["status"] == "MISSING" and eur["slippage"]["status"] == "MISSING"
    assert eur["swap"]["source"].startswith("live MT5")
    assert eur["swap"]["needs_holding_period"] == {"long": True, "short": False}
    reasons = " | ".join(eur["blocking_reasons"])
    for text in (
        "commission: no commission evidence",
        "slippage: no slippage evidence",
        "holding H1: no holding-period assumption",
    ):
        assert text in reasons
    assert eur["live_broker_terms_fingerprint"].startswith("sha256:")


def test_readiness_with_complete_synthetic_terms_is_possible() -> None:
    report = cost_readiness(synthetic_terms(holding_hours=24.0), datetime.now(UTC), _payload())
    assert all(i["approval_possible"] for i in report["instruments"])


def test_readiness_api_is_read_only_and_safe(tmp_path) -> None:
    app = _app(tmp_path)
    app.start()
    try:
        with closing(raw(tmp_path)) as conn:
            before = conn.execute("SELECT count(*) FROM audit_log").fetchone()[0]
        status, body, headers = _call(BackendAPI(app), "/api/costs/readiness")
        assert status == "200 OK" and headers["Cache-Control"] == "no-store"
        assert body["label"].startswith("COST READINESS — DIAGNOSTIC ONLY")
        assert body["registry"]["status"] == "installed"  # Phase 2: v5; still diagnostic
        assert body["approval_possible_anywhere"] is False
        text = json.dumps(body)
        for secret in ("12345678", '"login"', "Exness-MT5Trial9", '"server"', "ticket"):
            assert secret not in text
        with closing(raw(tmp_path)) as conn:
            assert conn.execute("SELECT count(*) FROM audit_log").fetchone()[0] == before
        response: dict = {}
        from io import BytesIO  # noqa: PLC0415

        BackendAPI(app)(
            {
                "PATH_INFO": "/api/costs/readiness",
                "REQUEST_METHOD": "POST",
                "QUERY_STRING": "",
                "wsgi.input": BytesIO(),
            },
            lambda s, h: response.update(status=s),
        )
        assert response["status"].startswith("405")
    finally:
        app.stop()
