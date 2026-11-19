"""P6.2a shadow trade intents (HYPOTHETICAL -- NOT AN ORDER): persistence, immutability,
execution-domain isolation and the opportunity funnel.

Temporary journals only; the scheduler is driven by a test clock with real T6 envelopes and
real Phase 4 results (synthetic cost terms where an approval is needed)."""

from __future__ import annotations

import ast
import copy
import json
import sqlite3
from contextlib import closing
from datetime import timedelta
from pathlib import Path

import pytest
from risk_fixtures import BUY_LEVEL, PRODUCTION_COSTS, VERIFIED_COSTS, envelope, make_fake, propose
from test_journal_store import raw
from test_shadow_evaluation import (
    CANDIDATE_NO_TARGET,
    CANDIDATE_UNVERIFIED,
    MIDNIGHT,
    RISK_NO_TARGET,
    RISK_UNVERIFIED,
    Risk,
    cycles,
    no_trade,
    setup,
)

from johanna import shadow_evaluation, shadow_intents
from johanna.decision_journal import DecisionJournal
from johanna.domain.execution import (
    DisabledExecutionService,
    ExecutionUnavailableError,
    OrderIntent,
)
from johanna.risk_engine import RISK_ENGINE_VERSION
from johanna.shadow_evaluation import PAIRS, ShadowEvaluationService, iso_z
from johanna.shadow_intents import LABEL, ShadowTradeIntent, build_intent, insert_intent

SRC = Path(__file__).resolve().parents[1] / "src" / "johanna"
RISK_APPROVED = propose(env=CANDIDATE_UNVERIFIED, costs=VERIFIED_COSTS)
RISK_STALE = propose(make_fake(tick_age_seconds=30), env=CANDIDATE_UNVERIFIED)
H13 = MIDNIGHT + timedelta(hours=13)  # Wednesday 13:00 UTC: an H1 close only


class KeyedRisk(Risk):
    """The stub risk service, keyed to the request's broker symbol too; optional per symbol."""

    def __init__(self) -> None:
        super().__init__()
        self.per_symbol: dict[str, dict] = {}

    def assess(self, symbol, timeframe, env):
        self.result = self.per_symbol.get(symbol, self.result)
        result = super().assess(symbol, timeframe, env)
        result["broker_symbol"] = PAIRS[symbol]
        return result


def run_candidates(tmp_path, result=RISK_UNVERIFIED, env=CANDIDATE_UNVERIFIED, **kwargs):
    service, clock, analysis, _ = setup(tmp_path, now=H13 - timedelta(minutes=2), **kwargs)
    risk = KeyedRisk()
    risk.result = result
    service.risk = risk
    analysis.env = env
    clock.now = H13 + timedelta(seconds=30)
    service.run_once()
    return service, clock, analysis, risk


def intent_rows(tmp_path) -> list[sqlite3.Row]:
    with closing(raw(tmp_path)) as conn:
        return conn.execute("SELECT * FROM shadow_intent ORDER BY rowid").fetchall()


# ====================================================================== persistence
def test_one_intent_per_constructed_candidate_linked_to_its_evidence(tmp_path) -> None:
    service, *_ = run_candidates(tmp_path)
    done = cycles(service, status="COMPLETED")
    assert len(done) == 3 and {c["risk_status"] for c in done} == {"UNVERIFIED"}
    rows = intent_rows(tmp_path)
    assert len(rows) == 3
    by_eval = {r["evaluation_id"]: r for r in rows}
    for cycle in done:
        row = by_eval[cycle["id"]]
        assert row["decision_id"] == cycle["decision_id"]
        assert row["risk_assessment_id"] == cycle["risk_assessment_id"]
        assert (row["symbol"], row["timeframe"]) == (cycle["symbol"], cycle["timeframe"])
        assert row["window_close_utc"] == cycle["window_close_utc"] == iso_z(H13)
        assert row["broker_symbol"] == PAIRS[cycle["symbol"]]
        assert row["kind"] == "SHADOW_HYPOTHETICAL" and row["target_method"] == "structural_tc6"
        assert row["eligibility"] == "UNVERIFIED" and row["direction"] == "BUY"
        assert row["entry_side"] == "ask" and row["stop_loss"] < row["entry_price"]
        assert row["volume"] is None and row["risk_amount_usc"] is None  # never invented
        assert row["risk_percent_equity"] is None and row["margin_estimate_usc"] is None
        assert row["net_reward_risk"] is None and row["gross_reward_risk"] > 2
        assert row["cost_status"] == "unverified" and row["cost_terms_version"] == "unavailable"
        assert row["risk_engine_version"] == RISK_ENGINE_VERSION  # 3 since P6.2b.1
        assert row["policy_fingerprint"].startswith("sha256:")
        reasons = json.loads(row["reasons"])
        assert reasons["primary"] == {
            "kind": "unverified",
            "reason": "costs.net_reward_risk",
            "category": "cost_unverified",
        }
        gates = {g["gate"]: g for g in service.cycle(cycle["id"])["evaluation"]["gates"]}
        assert gates["shadow_intent"]["result"] == "PASS"
        assert gates["shadow_intent"]["reason_code"] == "UNVERIFIED"


def test_candidate_without_a_constructed_trade_gets_no_intent(tmp_path) -> None:
    service, *_ = run_candidates(tmp_path, RISK_NO_TARGET, CANDIDATE_NO_TARGET)
    done = cycles(service, status="COMPLETED")
    assert {c["risk_status"] for c in done} == {"REJECTED"}
    assert intent_rows(tmp_path) == []
    gates = {g["gate"]: g for g in service.cycle(done[0]["id"])["evaluation"]["gates"]}
    assert gates["shadow_intent"]["result"] == "SKIPPED"
    assert gates["shadow_intent"]["reason_code"] == "not_constructed"


@pytest.mark.parametrize("outcome", ["NO_TRADE", "MARKET_CONDITION"])
def test_no_trade_and_market_conditions_get_no_intent(tmp_path, outcome) -> None:
    env = no_trade()
    env["result"]["decision"].update(outcome=outcome, rule="R2" if outcome == "NO_TRADE" else "R6")
    service, _, _, risk = run_candidates(tmp_path, env=env)
    assert risk.calls == 0 and intent_rows(tmp_path) == []
    assert {c["t6_outcome"] for c in cycles(service, status="COMPLETED")} == {outcome}


def test_risk_approved_intent_has_a_verified_volume_and_amounts(tmp_path) -> None:
    run_candidates(tmp_path, RISK_APPROVED)
    rows = intent_rows(tmp_path)
    assert len(rows) == 3
    for row in rows:
        assert row["eligibility"] == "RISK_APPROVED"
        assert row["volume"] == RISK_APPROVED["sizing"]["volume"] == 0.01
        assert row["risk_amount_usc"] == RISK_APPROVED["sizing"]["final_loss"]
        assert row["risk_percent_equity"] == RISK_APPROVED["sizing"]["final_loss_percent_of_equity"]
        assert row["margin_estimate_usc"] == RISK_APPROVED["margin"]["estimated"]
        assert row["net_reward_risk"] >= 2 and row["cost_status"] == "established"
        assert row["margin_status"] == "passed"
        assert row["cost_terms_version"] == RISK_APPROVED["versions"]["cost_terms_version"]
        assert json.loads(row["reasons"])["primary"] is None


def test_data_quality_result_is_recorded_as_unverified_intent(tmp_path) -> None:
    assert RISK_STALE["status"] == "UNVERIFIED" and RISK_STALE["gross_reward_risk"] is None
    run_candidates(tmp_path, RISK_STALE)  # stale quote: nothing constructed -> no intent
    assert intent_rows(tmp_path) == []


# ====================================================================== duplicates
def test_repeated_passes_and_a_restart_never_duplicate_an_intent(tmp_path) -> None:
    service, clock, analysis, risk = run_candidates(tmp_path)
    service.run_once()
    clock.advance(minutes=2)
    service.run_once()
    restarted = ShadowEvaluationService(
        service.store,
        DecisionJournal(service.store, clock=clock),
        analysis,
        risk,
        service.server_clock,
        clock=clock,
    )
    restarted.run_once()
    assert len(intent_rows(tmp_path)) == 3 and risk.calls == 3


def test_a_retried_window_records_one_intent(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=H13 - timedelta(minutes=2))
    risk = KeyedRisk()
    risk.defer = True  # the first attempt's risk inputs are contended -> retried
    service.risk = risk
    analysis.env = CANDIDATE_UNVERIFIED
    clock.now = H13 + timedelta(seconds=30)
    service.run_once()
    assert intent_rows(tmp_path) == [] and {c["status"] for c in cycles(service)} == {"PENDING"}
    risk.defer = False
    clock.advance(seconds=6)
    service.run_once()
    rows = intent_rows(tmp_path)
    assert len(rows) == 3
    assert {c["attempts"] for c in cycles(service, status="COMPLETED")} == {2}


def test_a_second_intent_for_the_same_window_is_refused(tmp_path) -> None:
    service, *_ = run_candidates(tmp_path)
    cycle = cycles(service, status="COMPLETED")[0]
    intent = build_intent(
        {**copy.deepcopy(RISK_UNVERIFIED), "symbol": cycle["symbol"]},
        evaluation_id=cycle["id"],
        decision_id=cycle["decision_id"],
        risk_assessment_id=cycle["risk_assessment_id"],
        window_close_utc=cycle["window_close_utc"],
    )
    with pytest.raises(sqlite3.IntegrityError):
        with service.store.write() as conn:
            insert_intent(service.store, conn, intent, service._clock())
    assert len(intent_rows(tmp_path)) == 3
    assert service.store.verify_audit_chain()["valid"] is True


# ====================================================================== transaction
def test_a_failed_intent_write_rolls_back_the_whole_completion(tmp_path, monkeypatch) -> None:
    def failing(store, conn, intent, now):
        shadow_intents.insert_intent(store, conn, intent, now)  # the row is written...
        raise RuntimeError("disk full")  # ...then the transaction fails

    monkeypatch.setattr(shadow_evaluation, "insert_intent", failing)
    service, *_ = run_candidates(tmp_path)
    assert intent_rows(tmp_path) == []  # no partial record
    rows = cycles(service)
    assert {(c["status"], c["reason_code"]) for c in rows} == {("FAILED", "evaluation_error")}
    with closing(raw(tmp_path)) as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM audit_log WHERE action='shadow_intent.recorded'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute("SELECT count(*) FROM gate_outcome WHERE gate='shadow_intent'").fetchone()[
                0
            ]
            == 0
        )
    assert service.store.verify_audit_chain()["valid"] is True


def test_an_inconsistent_risk_result_fails_visibly(tmp_path) -> None:
    broken = copy.deepcopy(RISK_APPROVED)
    broken["sizing"]["volume"] = None  # approved without a verified volume
    service, *_ = run_candidates(tmp_path, broken)
    assert intent_rows(tmp_path) == []
    assert {c["status"] for c in cycles(service)} == {"FAILED"}


# ====================================================================== immutability
def test_intents_cannot_be_updated_or_deleted(tmp_path) -> None:
    run_candidates(tmp_path)
    with closing(raw(tmp_path)) as conn:
        for sql in (
            "UPDATE shadow_intent SET volume = 1.0",
            "UPDATE shadow_intent SET eligibility = 'RISK_APPROVED'",
            "DELETE FROM shadow_intent",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                conn.execute(sql)
        assert conn.execute("SELECT count(*) FROM shadow_intent").fetchone()[0] == 3


def _insert_variant(tmp_path, service, **changes):
    cycle = cycles(service, status="COMPLETED")[0]
    with closing(raw(tmp_path)) as conn:
        row = dict(
            conn.execute(
                "SELECT * FROM shadow_intent WHERE evaluation_id=?", (cycle["id"],)
            ).fetchone()
        )
    row.update(id="01JZZZZZZZZZZZZZZZZZZZZZZZ", **changes)
    with closing(raw(tmp_path)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            f"INSERT INTO shadow_intent({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
            tuple(row.values()),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"evaluation_id": "OTHER"},  # unique per evaluation / must match an evaluation
        {"volume": 0.01},  # rejected/unverified intents never carry a volume
        {"eligibility": "RISK_APPROVED"},  # approved needs volume and amounts
        {"target_method": "atr_multiple"},  # no fallback targets
        {"kind": "ORDER"},
        {"stop_loss": 9.0},  # stop-loss on the wrong side of a BUY entry
        {"risk_engine_version": 1},
    ],
)
def test_database_constraints_refuse_invalid_intents(tmp_path, changes) -> None:
    service, *_ = run_candidates(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_variant(tmp_path, service, **changes)


def test_an_intent_needs_a_completed_candidate_evaluation(tmp_path) -> None:
    env = no_trade()
    service, *_ = run_candidates(tmp_path, env=env)
    cycle = cycles(service, status="COMPLETED")[0]
    intent = build_intent(
        {**copy.deepcopy(RISK_UNVERIFIED), "symbol": cycle["symbol"]},
        evaluation_id=cycle["id"],
        decision_id=cycle["decision_id"],
        risk_assessment_id=cycle["decision_id"],
        window_close_utc=cycle["window_close_utc"],
    )
    with pytest.raises(sqlite3.IntegrityError, match="completed candidate evaluation"):
        with service.store.write() as conn:
            insert_intent(service.store, conn, intent, service._clock())


def test_audit_chain_records_every_intent(tmp_path) -> None:
    service, *_ = run_candidates(tmp_path)
    assert service.store.verify_audit_chain()["valid"] is True
    with closing(raw(tmp_path)) as conn:
        audits = conn.execute(
            "SELECT record_id, new_values FROM audit_log WHERE action='shadow_intent.recorded'"
        ).fetchall()
        ids = {r["id"] for r in conn.execute("SELECT id FROM shadow_intent")}
    assert {a["record_id"] for a in audits} == ids
    assert all(json.loads(a["new_values"])["eligibility"] == "UNVERIFIED" for a in audits)


def test_evidence_fingerprint_is_deterministic(tmp_path) -> None:
    kwargs = dict(
        evaluation_id="E",
        decision_id="D",
        risk_assessment_id="R",
        window_close_utc="2026-11-18T13:00:00Z",
    )
    first = build_intent(RISK_UNVERIFIED, **kwargs)
    assert (
        first.evidence_fingerprint()
        == build_intent(RISK_UNVERIFIED, **kwargs).evidence_fingerprint()
    )
    other = build_intent(RISK_APPROVED, **kwargs)
    assert first.evidence_fingerprint() != other.evidence_fingerprint()


def test_intents_store_no_account_or_broker_identifiers(tmp_path) -> None:
    run_candidates(tmp_path, RISK_APPROVED)
    with closing(raw(tmp_path)) as conn:
        columns = [r[1] for r in conn.execute("PRAGMA table_info(shadow_intent)")]
        text = json.dumps([dict(r) for r in conn.execute("SELECT * FROM shadow_intent")])
    for word in ("login", "server", "company", "ticket", "position", "order", "account_id"):
        assert not any(word in c for c in columns), word
    for secret in (
        "12345678",
        "Exness-MT5Trial9",
        "Exness Technologies",
        '"server"',
        '"company"',
        '"login"',
        "ticket",
    ):
        assert secret not in text, secret


def test_shadow_trade_intent_rules() -> None:
    kwargs = dict(
        evaluation_id="E",
        decision_id="D",
        risk_assessment_id="R",
        window_close_utc="2026-11-18T13:00:00Z",
    )
    assert build_intent(RISK_NO_TARGET, **kwargs) is None  # nothing constructed
    intent = build_intent(RISK_UNVERIFIED, **kwargs)
    with pytest.raises(ValueError):
        ShadowTradeIntent(**{**intent.__dict__, "volume": 0.01})
    with pytest.raises(ValueError):
        ShadowTradeIntent(**{**intent.__dict__, "target_method": "measured_move"})
    with pytest.raises(AttributeError):
        intent.volume = 1.0  # frozen


# ====================================================================== isolation
def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


def test_shadow_intents_module_cannot_reach_execution_or_the_broker() -> None:
    imported = _imports(SRC / "shadow_intents.py")
    assert {i for i in imported if i.startswith("johanna")} == {"johanna.journal_store"}
    text = (SRC / "shadow_intents.py").read_text(encoding="utf-8")
    for name in (
        "OrderIntent",
        "ExecutionService",
        "order_send",
        "submit",
        "MetaTrader5",
        "live_market",
        "trade_logbook",
        "risk_service",
    ):
        assert name not in text, name


def test_no_module_combines_shadow_intents_with_the_execution_domain() -> None:
    for path in SRC.rglob("*.py"):
        imported = _imports(path)
        if "johanna.shadow_intents" in imported:
            assert not {i for i in imported if i.startswith("johanna.domain")}, path.name
            assert "johanna.trade_logbook" not in imported, path.name
    assert "shadow_intent" not in (SRC / "trade_logbook.py").read_text(encoding="utf-8")
    assert "shadow_intent" not in (SRC / "domain" / "execution.py").read_text(encoding="utf-8")


def test_a_shadow_intent_is_not_an_order_and_execution_refuses_everything() -> None:
    intent = build_intent(
        RISK_APPROVED,
        evaluation_id="E",
        decision_id="D",
        risk_assessment_id="R",
        window_close_utc="2026-11-18T13:00:00Z",
    )
    assert not isinstance(intent, OrderIntent) and not issubclass(ShadowTradeIntent, OrderIntent)
    public = {n for n in dir(intent) if not n.startswith("_")}
    assert not {n for n in public if "order" in n or "submit" in n or "execute" in n}
    with pytest.raises(ExecutionUnavailableError):
        DisabledExecutionService().submit(intent)  # nothing can be submitted, intents included


# ====================================================================== funnel
def test_funnel_counts_scheduled_windows_and_never_treats_gaps_as_no_trade(tmp_path) -> None:
    service, clock, analysis, risk = run_candidates(tmp_path)
    saturday = MIDNIGHT + timedelta(days=3, hours=12)
    service._create_window("EURUSD", "H4", saturday, saturday)  # SKIPPED market_closed
    late = MIDNIGHT + timedelta(hours=10)
    service._create_window("GBPUSD", "H4", late, late + timedelta(hours=1))  # MISSED
    analysis.error = RuntimeError("boom")
    clock.now = H13 + timedelta(hours=1, seconds=30)
    service.run_once()  # 14:00 H1 windows fail (evaluation_error)
    funnel = service.funnel()
    stages = {s["id"]: s for s in funnel["stages"]}
    assert stages["windows_scheduled"]["count"] == 3 + 1 + 1 + 3
    assert stages["completed_evaluations"]["count"] == 3
    assert stages["trade_candidates"]["count"] == 3
    assert stages["risk_assessed"]["count"] == 3
    assert stages["constructed_trade_intents"]["count"] == 3
    assert stages["risk_approved_shadow_intents"]["count"] == 0
    assert stages["completed_evaluations"]["of_previous"] == round(3 / 8, 4)
    assert funnel["outcomes"]["NO_TRADE"]["count"] == 0  # gaps are not NO_TRADE
    ne = funnel["not_evaluated"]
    assert (ne["SKIPPED"], ne["MISSED"], ne["FAILED"], ne["total"]) == (1, 1, 3, 5)
    assert funnel["risk_outcomes"]["UNVERIFIED"] == {"count": 3, "of_assessed": 1.0}
    assert funnel["execution"]["executed_trades"] is None
    assert funnel["execution"]["realised_pnl"] is None


def test_funnel_counts_a_retried_window_once(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=H13 - timedelta(minutes=2))
    risk = KeyedRisk()
    risk.defer = True
    service.risk = risk
    analysis.env = CANDIDATE_UNVERIFIED
    clock.now = H13 + timedelta(seconds=30)
    service.run_once()
    risk.defer = False
    clock.advance(seconds=6)
    service.run_once()
    funnel = service.funnel()
    assert funnel["stages"][0]["count"] == 3 and funnel["stages"][1]["count"] == 3


def test_funnel_excludes_dashboard_requested_analyses(tmp_path) -> None:
    service, *_ = run_candidates(tmp_path)
    before = service.funnel()
    journal = DecisionJournal(service.store, clock=service._clock)
    journal.record_analysis(envelope(level=BUY_LEVEL, symbol="EURUSD"), "analysis_request")
    journal.record_analysis(envelope(level=BUY_LEVEL, symbol="USDJPY"), "risk_request")
    assert service.funnel() == before


def test_primary_reasons_count_once_and_separate_data_from_policy(tmp_path) -> None:
    service, clock, analysis, _ = setup(tmp_path, now=H13 - timedelta(minutes=2))
    risk = KeyedRisk()
    risk.result = RISK_UNVERIFIED
    risk.per_symbol = {"GBPUSD": RISK_NO_TARGET, "USDJPY": RISK_STALE}
    service.risk = risk
    analysis.env = CANDIDATE_UNVERIFIED
    clock.now = H13 + timedelta(seconds=30)
    service.run_once()
    funnel = service.funnel()
    primary = {r["reason"]: r for r in funnel["primary_reasons"]["items"]}
    assert primary["costs.net_reward_risk"]["category"] == "cost_unverified"
    assert primary["construction.target_structure"]["category"] == "policy_rejection"
    assert primary["data.quote_fresh"]["category"] == "data_unverified"
    assert sum(r["count"] for r in funnel["primary_reasons"]["items"]) == 3  # once each
    cats = funnel["reason_categories"]
    assert (cats["policy_rejection"], cats["data_unverified"], cats["cost_unverified"]) == (1, 1, 1)
    assert sum(r["count"] for r in funnel["all_reasons"]["items"]) > 3  # several per window
    assert "several" in funnel["all_reasons"]["note"]
    by_status = {r["value"]: r for r in funnel["breakdowns"]["risk_status"]}
    assert by_status["REJECTED"]["windows"] == 1 and by_status["UNVERIFIED"]["windows"] == 2
    assert {r["value"] for r in funnel["breakdowns"]["symbol"]} == {"EURUSD", "GBPUSD", "USDJPY"}
    assert {r["value"] for r in funnel["breakdowns"]["direction"]} == {"BUY"}
    assert funnel["stages"][4]["count"] == 1  # only the EURUSD trade was constructed


def test_funnel_date_range(tmp_path) -> None:
    service, *_ = run_candidates(tmp_path)
    assert service.funnel("2026-11-18T00:00:00Z", "2026-11-19T00:00:00Z")["stages"][0]["count"] == 3
    assert service.funnel("2026-11-19T00:00:00Z", None)["stages"][0]["count"] == 0
    assert service.funnel(None, "2026-11-18T13:00:00Z")["stages"][0]["count"] == 0


def test_historical_t4_defects_stay_identifiable_and_are_not_failures(tmp_path) -> None:
    service, *_ = run_candidates(tmp_path, env=no_trade())
    cycle = cycles(service, status="COMPLETED")[0]
    with closing(raw(tmp_path)) as conn:  # the P6.1 defect signature, reproduced in a copy
        conn.execute("DROP TRIGGER gate_outcome_no_update")
        conn.execute(
            "UPDATE gate_outcome SET reason_code=NULL WHERE evaluation_id=? "
            "AND gate='T4.window_validation'",
            (cycle["id"],),
        )
        conn.execute("DROP TRIGGER evaluation_cycle_transitions")
        conn.execute("UPDATE evaluation_cycle SET data_quality=NULL WHERE id=?", (cycle["id"],))
    funnel = service.funnel()
    assert funnel["historical_recording_defects"]["t4_gate_empty_reason_code"] == 1
    assert funnel["stages"][1]["count"] == 3 and funnel["not_evaluated"]["FAILED"] == 0
    diag = service.diagnostics()
    assert diag["historical_recording_defects"]["t4_gate_empty_reason_code"]["count"] == 1
    assert diag["populations"]["shadow_intents"] == 0


def test_diagnostics_report_intents_separately_from_execution(tmp_path) -> None:
    service, *_ = run_candidates(tmp_path, RISK_APPROVED)
    pops = service.diagnostics()["populations"]
    assert pops["shadow_intents"] == 3 and pops["risk_approved_shadow_intents"] == 3
    assert pops["executable_opportunities"] is None and pops["actual_trades"] is None


def test_label_is_explicit() -> None:
    assert LABEL == "HYPOTHETICAL — NOT AN ORDER"
    assert PRODUCTION_COSTS.configured is False
