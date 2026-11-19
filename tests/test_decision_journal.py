"""P5.1 decision journal: dimensions, deduplication, supersession, risk versions, isolation.

Uses real T6 envelopes (``risk_fixtures``) and real Phase 4 results from FakeMT5 — synthetic
market data only. Every journal lives in a temporary directory.
"""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO

import pytest
from risk_fixtures import (
    BUY_LEVEL,
    PRODUCTION_COSTS,
    REAL_BUY,
    VERIFIED_COSTS,
    StubAnalysis,
    envelope,
    make_fake,
    propose,
)
from test_journal_store import Acl

from johanna.adapters.mt5 import MetaTrader5Adapter
from johanna.api import BackendAPI
from johanna.application import JohannaApplication
from johanna.config import AppConfig, StorageConfig
from johanna.decision_journal import (
    COVERAGE,
    CapturingAnalysis,
    DecisionJournal,
    analysis_record,
)
from johanna.journal_store import JournalStore
from johanna.live_market import LiveMarketService


def journal(tmp_path, clock=None) -> DecisionJournal:
    store = JournalStore(tmp_path, acl=Acl(), acl_check=Acl(), clock=clock)
    store.start()
    assert store.ready
    return DecisionJournal(store, clock=clock)


def repolled(env: dict) -> dict:
    """The same evaluation as a later poll: new snapshot id and evaluation time."""
    again = copy.deepcopy(env)
    again["snapshot"]["snapshot_id"] = uuid.uuid4().hex
    again["result"]["inputs"]["snapshot_id"] = again["snapshot"]["snapshot_id"]
    again["result"]["evaluated_at_utc"] = datetime.now(UTC).isoformat()
    return again


def r6(env: dict) -> dict:
    changed = copy.deepcopy(env)
    changed["result"]["decision"].update(
        outcome="MARKET_CONDITION", rule="R6", reasons=["market understood; not qualified"]
    )
    return changed


# ---------------------------------------------------------------- deduplication
def test_one_hundred_identical_evaluations_give_one_decision(tmp_path) -> None:
    j = journal(tmp_path)
    env = envelope(level=BUY_LEVEL)
    ids = {j.record_analysis(repolled(env), "analysis_request") for _ in range(100)}
    assert len(ids) == 1
    page = j.list()
    assert len(page["decisions"]) == 1
    assert page["decisions"][0]["observations"] == 100
    with j.store.read() as conn:
        assert conn.execute("SELECT count(*) FROM decision").fetchone()[0] == 1


def test_changed_analysis_creates_a_linked_decision(tmp_path) -> None:
    j = journal(tmp_path)
    env = envelope(level=BUY_LEVEL)
    first = j.record_analysis(env, "analysis_request")
    second = j.record_analysis(r6(env), "analysis_request")
    third = j.record_analysis(repolled(env), "analysis_request")  # reverts: a new record
    assert len({first, second, third}) == 3
    items = {d["id"]: d for d in j.list()["decisions"]}
    assert items[second]["previous_decision_id"] == first
    assert items[third]["previous_decision_id"] == second
    assert items[first]["state"] == "SUPERSEDED" and items[first]["superseded_by"] == second
    assert items[third]["state"] == "CURRENT"
    assert j.get(third)["decision"]["supersession_chain"] == [second, first]
    # the original evidence is unchanged
    original = j.get(first)["decision"]
    assert original["analysis_outcome"] == "TRADE_CANDIDATE" and original["t6_rule"] == "R7"


def test_decision_expires_after_the_next_candle_close(tmp_path) -> None:
    now = {"t": datetime.now(UTC)}
    j = journal(tmp_path, clock=lambda: now["t"])
    j.record_analysis(envelope(level=BUY_LEVEL, evaluated_at=now["t"]), "analysis_request")
    assert j.list()["decisions"][0]["state"] == "CURRENT"
    now["t"] += timedelta(hours=2)
    assert j.list()["decisions"][0]["state"] == "EXPIRED"


# ---------------------------------------------------------------- dimensions
def test_candidate_with_unverified_risk_is_not_approved_and_not_executed(tmp_path) -> None:
    j = journal(tmp_path)
    env = envelope(level=BUY_LEVEL)
    decision = j.record_analysis(env, "risk_request")
    result = propose(env=env, costs=PRODUCTION_COSTS)
    assert result["status"] == "UNVERIFIED"
    j.record_risk(result, decision)
    item = j.list()["decisions"][0]
    assert item["analysis_outcome"] == "TRADE_CANDIDATE"  # never altered by risk
    assert item["market_direction"] == "BUY"
    assert item["risk_status"] == "UNVERIFIED"
    assert item["trade_eligibility"] == "NOT_APPROVED"
    assert item["execution_status"] == "NONE"


def test_approved_risk_makes_a_candidate_eligible_but_not_executed(tmp_path) -> None:
    j = journal(tmp_path)
    env = envelope(level=BUY_LEVEL)
    decision = j.record_analysis(env, "risk_request")
    result = propose(env=env, costs=VERIFIED_COSTS)  # synthetic verified cost terms
    assert result["status"] == "APPROVED_FOR_MANUAL_REVIEW"
    j.record_risk(result, decision)
    item = j.list(eligibility="APPROVED")["decisions"][0]
    assert item["trade_eligibility"] == "APPROVED" and item["execution_status"] == "NONE"
    assert j.list(eligibility="NOT_APPROVED")["decisions"] == []


def test_non_candidate_risk_is_not_evaluated(tmp_path) -> None:
    j = journal(tmp_path)
    env = r6(envelope(level=BUY_LEVEL))
    decision = j.record_analysis(env, "risk_request")
    result = propose(env=env)
    assert result["status"] == "REJECTED"  # Phase 4: precondition (no T6 candidate)
    j.record_risk(result, decision)
    detail = j.get(decision)["decision"]
    assert detail["risk_status"] == "NOT_EVALUATED"
    assert detail["risk_assessments"][0]["phase4_status_raw"] == "REJECTED"
    assert detail["trade_eligibility"] == "NOT_APPROVED"


def test_risk_changes_append_assessments_only(tmp_path) -> None:
    j = journal(tmp_path)
    env = envelope(level=BUY_LEVEL)
    decision = j.record_analysis(env, "risk_request")
    unverified = propose(env=env, costs=PRODUCTION_COSTS)
    first = j.record_risk(unverified, decision)
    assert j.record_risk(unverified, decision) == first  # identical: observation only
    approved = propose(env=env, costs=VERIFIED_COSTS)
    second = j.record_risk(approved, decision)
    assessments = j.get(decision)["decision"]["risk_assessments"]
    assert [a["id"] for a in assessments] == [first, second]
    assert [a["risk_status"] for a in assessments] == ["UNVERIFIED", "APPROVED"]
    assert assessments[0]["observations"] == 2 and assessments[1]["previous_assessment_id"] == first
    assert j.list()["decisions"][0]["risk_status"] == "APPROVED"  # latest assessment


def test_t6_and_phase4_evidence_stay_separate_and_redacted(tmp_path) -> None:
    j = journal(tmp_path)
    env = envelope(level=BUY_LEVEL)
    decision = j.record_analysis(env, "risk_request")
    j.record_risk(propose(env=env, costs=PRODUCTION_COSTS), decision)
    detail = j.get(decision)["decision"]
    t6 = json.dumps(detail["t6_evidence"])
    risk = json.dumps(detail["risk_assessments"][0]["risk_evidence"])
    assert "validations" not in t6 and "costs_status" not in t6
    assert "market_condition" not in risk and "checklist" not in risk
    assert detail["t6_evidence"]["decision"]["rule"] == "R7"
    evidence = detail["risk_assessments"][0]["risk_evidence"]
    assert evidence["slippage_status"] == "unmodelled" and evidence["costs_status"] == "unverified"
    assert '"detail"' not in risk and "USC" not in risk  # no free text with account amounts


def test_mismatched_risk_result_is_not_attached(tmp_path) -> None:
    j = journal(tmp_path)
    decision = j.record_analysis(envelope(level=BUY_LEVEL), "risk_request")
    other = propose(env=envelope(level=BUY_LEVEL, symbol="GBPUSD"), symbol="GBPUSD")
    assert j.capture_risk(other, decision) is None and j.capture_failures == 1


def test_coverage_disclosure_is_always_present(tmp_path) -> None:
    j = journal(tmp_path)
    decision = j.record_analysis(envelope(level=BUY_LEVEL), "analysis_request")
    assert j.list()["coverage"] == COVERAGE and j.get(decision)["coverage"] == COVERAGE
    assert COVERAGE["mode"] == "request_time" and "not continuous" in COVERAGE["note"]


def test_fingerprint_ignores_snapshot_and_time_but_not_content() -> None:
    env = envelope(level=BUY_LEVEL)
    a, b = analysis_record(env), analysis_record(repolled(env))
    assert a["analysis_fingerprint"] == b["analysis_fingerprint"]
    assert analysis_record(r6(env))["analysis_fingerprint"] != a["analysis_fingerprint"]


def test_audit_rows_are_written_and_chain_verifies(tmp_path) -> None:
    j = journal(tmp_path)
    env = envelope(level=BUY_LEVEL)
    for _ in range(3):
        j.record_analysis(repolled(env), "analysis_request")
    with j.store.read() as conn:
        actions = [r[0] for r in conn.execute("SELECT action FROM audit_log ORDER BY seq")]
    assert actions == [
        "journal.created",
        "decision.recorded",
        "observation.updated",
        "observation.updated",
    ]
    assert j.store.verify_audit_chain()["valid"]


# ---------------------------------------------------------------- isolation
def test_capture_never_raises_and_never_mutates_inputs(tmp_path) -> None:
    store = JournalStore(tmp_path, acl=Acl(), acl_check=Acl())  # never started: unavailable
    j = DecisionJournal(store)
    env = envelope(level=BUY_LEVEL)
    before = json.dumps(env, sort_keys=True, default=str)
    assert j.capture_analysis(env) is None
    assert j.capture_risk({"status": "x"}, "01JTESTDECISION0000000000A") is None
    assert json.dumps(env, sort_keys=True, default=str) == before
    assert j.capture_failures == 2 and j.last_failure == "journal_unavailable"


def _app(tmp_path):
    fake = make_fake()
    config = replace(AppConfig.load(environ={}), storage=StorageConfig(tmp_path))
    live = LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake),
        terminal_running=lambda: True,
        reconnect_interval=0,
    )
    app = JohannaApplication(config, live_market=live)
    app.journal_store._acl = Acl()  # fast stand-in for icacls in this test
    app.journal_store._acl_check = Acl()
    return app


def _call(api, path, query=""):
    response: dict = {}
    body = b"".join(
        api(
            {
                "PATH_INFO": path,
                "REQUEST_METHOD": "GET",
                "QUERY_STRING": query,
                "wsgi.input": BytesIO(),
            },
            lambda status, headers: response.update(status=status, headers=dict(headers)),
        )
    )
    return response["status"], json.loads(body), response["headers"]


class FixedAnalysis:
    def __init__(self, env):
        self.env = env

    def analyse(self, symbol, timeframe):
        return copy.deepcopy(self.env)


@pytest.mark.parametrize("journal_state", ["working", "broken"])
def test_analysis_and_risk_responses_are_unchanged_by_the_journal(tmp_path, journal_state) -> None:
    app = _app(tmp_path)
    app.start()
    try:
        env = envelope(level=BUY_LEVEL)
        app.market_analysis = FixedAnalysis(env)
        app.risk.analysis = CapturingAnalysis(StubAnalysis(env), app.decisions)
        app.risk.costs = PRODUCTION_COSTS
        if journal_state == "broken":
            app.journal_store._state = "refused"  # journal unavailable mid-run
        api = BackendAPI(app)
        status, body, _ = _call(api, "/api/market/analysis", "symbol=EURUSD&timeframe=H1")
        assert status == "200 OK" and body == json.loads(json.dumps(env, default=str))
        status, risk, _ = _call(api, "/api/risk/proposal", "symbol=EURUSD&timeframe=H1")
        assert status == "200 OK" and risk["status"] == "UNVERIFIED"
        if journal_state == "working":
            decisions = app.decisions.list()["decisions"]
            assert len(decisions) == 1 and decisions[0]["observations"] == 2  # analysis + risk
            assert decisions[0]["risk_status"] == "UNVERIFIED"
        else:
            assert app.decisions.capture_failures >= 2
    finally:
        app.stop()


def test_real_envelope_is_not_modified_by_capture(tmp_path) -> None:
    before = json.dumps(REAL_BUY, sort_keys=True, default=str)
    journal(tmp_path).capture_analysis(REAL_BUY)
    assert json.dumps(REAL_BUY, sort_keys=True, default=str) == before
