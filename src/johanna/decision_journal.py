"""Phase 5 decision journal (P5.1). See ``docs/phase-5-architecture.md`` §2.2, §3.3.

Records the T6 analyses and Phase 4 risk assessments **that the application was asked for**
(request-time capture). It is not continuous market coverage.

Five dimensions are kept independent:

- analysis outcome and market direction come from T6 (``decision``, immutable evidence);
- risk status comes from Phase 4 (``risk_assessment``, append-only, versioned per decision);
- trade eligibility is derived: ``APPROVED`` only for a ``TRADE_CANDIDATE`` whose latest risk
  assessment is ``APPROVED``;
- execution status is derived from trade links (P5.2): ``MANUALLY_RECORDED`` while an ACTIVE
  link to a non-voided manual trade exists, otherwise ``NONE``. ``BROKER_VERIFIED`` arrives with
  reconciliation and is never reported yet.

A risk failure never changes the T6 outcome. Capture failures are isolated: the capture
methods never raise and never modify the envelope or result they are given.

Analysis fingerprint (decision identity; frozen architecture §3.3). SHA-256 of canonical JSON of:

- ``symbol``, ``timeframe`` — which market;
- ``as_of_utc`` — the closed candle evaluated (a new candle is a new decision);
- ``outcome``, ``rule``, ``direction`` — the T6 decision itself;
- ``reasons`` (sorted) — why T6 decided; their only numbers are window facts (candle times,
  swing and candle counts) that are fixed for a given closed-candle window and change only when
  the analytical input changes (e.g. a candle's closure becoming tick-observed);
- ``verdict`` — the T4 window-validation verdict;
- ``states`` — the market-condition dimension states.

Deliberately excluded (vary between identical evaluations, not decision-significant):
snapshot ids, evaluation/served timestamps, data age and timing, indicator and price values,
the overlay payload and window-validation detail text. These stay out of decision identity; the
first occurrence's provenance (snapshot id, evaluation time) is kept as immutable evidence and
repeats only advance the observation counters.
"""

from __future__ import annotations

import contextvars
import copy
import hashlib
import json
import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any

from johanna.journal_store import JournalError, JournalStore, new_ulid, utc_iso

LOGGER = logging.getLogger("johanna.decision_journal")

COVERAGE = {
    "mode": "request_time",
    "note": (
        "Johanna records the evaluations the application requested (open pages and API "
        "calls) and, while shadow evaluation is enabled, one evaluation per closed H1, H4 and "
        "D1 candle of EURUSDc, GBPUSDc and USDJPYc (capture source scheduled_evaluation). "
        "This is not continuous market coverage."
    ),
    # P6.1: additive; scheduled captures are identified by this capture source.
    "scheduled_capture_source": "scheduled_evaluation",
}
OUTCOMES = ("TRADE_CANDIDATE", "MARKET_CONDITION", "NO_TRADE")
RISK_STATUSES = ("APPROVED", "REJECTED", "UNVERIFIED", "NOT_EVALUATED")
_DIRECTION = {"bullish": "BUY", "bearish": "SELL"}
_PHASE4_STATUS = {
    "APPROVED_FOR_MANUAL_REVIEW": "APPROVED",
    "REJECTED": "REJECTED",
    "UNVERIFIED": "UNVERIFIED",
}
_TIMEFRAME_SECONDS = {
    "M1": 60,
    "M5": 300,
    "M15": 900,
    "M30": 1800,
    "H1": 3600,
    "H4": 14400,
    "D1": 86400,
}
# Capture sources (journal CHECK constraint, schema v3): request-time and P6.1 scheduled.
CAPTURE_SOURCES = ("analysis_request", "risk_request", "scheduled_evaluation")
EXECUTION_STATUSES = ("NONE", "MANUALLY_RECORDED", "BROKER_VERIFIED")
_MANUALLY_RECORDED_SQL = (
    "EXISTS (SELECT 1 FROM trade_decision_link l JOIN trade t ON t.id = l.trade_id "
    "WHERE l.decision_id = d.id AND l.status = 'ACTIVE' AND t.voided_utc IS NULL)"
)

# The decision captured by the analysis call made inside the current risk request.
LAST_CAPTURED_DECISION: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "johanna_last_captured_decision", default=None
)


def _sha256(value: Any) -> str:
    data = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _m(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def analysis_record(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Normalised, JSON-safe decision content from a T6 analysis envelope."""
    result = _m(envelope.get("result"))
    decision = _m(result.get("decision"))
    opportunity = _m(result.get("opportunity"))
    condition = _m(result.get("market_condition"))
    inputs = _m(result.get("inputs"))
    outcome = decision.get("outcome")
    if outcome not in OUTCOMES:
        raise ValueError("analysis envelope has no recognised T6 outcome")
    symbol, timeframe = envelope.get("symbol"), envelope.get("timeframe")
    if not isinstance(symbol, str) or not isinstance(timeframe, str):
        raise ValueError("analysis envelope has no symbol/timeframe")
    states = {
        name: _m(dim).get("state") for name, dim in sorted(_m(condition.get("dimensions")).items())
    }
    reasons = [str(r) for r in decision.get("reasons") or []]
    evidence = {
        "decision": {
            "outcome": outcome,
            "rule": decision.get("rule"),
            "reasons": reasons,
            "actionable": decision.get("actionable"),
        },
        "gate": _m(result.get("gate")).get("passed"),
        "validation_verdict": inputs.get("validation_verdict"),
        "analysis_status": inputs.get("analysis_status"),
        "window": inputs.get("window"),
        "market_condition": {
            "trusted": condition.get("trusted"),
            "understood": condition.get("understood"),
            "states": states,
        },
        "opportunity": {
            "setup": opportunity.get("setup"),
            "direction": opportunity.get("direction"),
            "status": opportunity.get("status"),
            "failed_conditions": list(opportunity.get("failed_conditions") or []),
            "triggered_vetoes": list(opportunity.get("triggered_vetoes") or []),
            "checklist": [
                {"id": c.get("id"), "result": c.get("result")}
                for c in opportunity.get("checklist") or []
                if isinstance(c, Mapping)
            ],
        },
    }
    direction = _DIRECTION.get(opportunity.get("direction"), "NONE")
    as_of = result.get("as_of_utc")
    fingerprint = _sha256(
        {
            "symbol": symbol,
            "timeframe": timeframe,
            "as_of_utc": as_of,
            "outcome": outcome,
            "rule": decision.get("rule"),
            "direction": direction,
            "reasons": sorted(reasons),
            "verdict": inputs.get("validation_verdict"),
            "states": states,
        }
    )
    return {
        "symbol": symbol,
        "broker_symbol": result.get("broker_symbol"),
        "timeframe": timeframe,
        "as_of_utc": as_of,
        "analysis_outcome": outcome,
        "t6_rule": decision.get("rule"),
        "market_direction": direction,
        "t6_evidence": evidence,
        "analysis_fingerprint": fingerprint,
        "snapshot_id": _m(envelope.get("snapshot")).get("snapshot_id") or inputs.get("snapshot_id"),
        "evaluated_at_utc": result.get("evaluated_at_utc"),
    }


def risk_record(result: Mapping[str, Any]) -> dict[str, Any]:
    """Normalised Phase 4 evidence. Free-text details are not persisted: they can contain
    account amounts (balance, equity, budgets). Check ids, outcomes and statuses are kept."""
    setup = _m(result.get("setup"))
    raw = result.get("status")
    if setup.get("t6_outcome") != "TRADE_CANDIDATE":
        status = "NOT_EVALUATED"  # a precondition failure, not a risk assessment
    else:
        status = _PHASE4_STATUS.get(raw)
        if status is None:
            raise ValueError("unrecognised Phase 4 status")
    validations = {
        area: {
            "status": _m(item).get("status"),
            "checks": [
                {"id": c.get("id"), "outcome": c.get("outcome")}
                for c in _m(item).get("checks") or []
                if isinstance(c, Mapping)
            ],
        }
        for area, item in sorted(_m(result.get("validations")).items())
    }
    costs = _m(result.get("costs"))
    sizing = _m(result.get("sizing"))
    policy = _m(result.get("policy"))
    evidence = {
        "phase4_status": raw,
        "validations": validations,
        "rejections": [
            {"area": r.get("area"), "id": r.get("id")}
            for r in result.get("rejection_reasons") or []
            if isinstance(r, Mapping)
        ],
        "unverified": [
            {"area": u.get("area"), "id": u.get("id")}
            for u in result.get("unverified") or []
            if isinstance(u, Mapping)
        ],
        "gross_reward_risk": result.get("gross_reward_risk"),
        "net_reward_risk_status": _m(result.get("net_reward_risk")).get("status"),
        "costs_status": costs.get("status"),
        "slippage_status": _m(costs.get("slippage")).get("status"),
        "sizing_status": sizing.get("status"),
        "sizing_includes_costs": sizing.get("includes_costs"),
        "policy": {
            key: policy.get(key)
            for key in ("max_risk_per_trade", "max_daily_risk", "min_reward_risk")
        },
    }
    versions = _m(result.get("versions"))
    if versions:  # P6.2a M4 (results of earlier engine versions have none: evidence unchanged)
        evidence["versions"] = {
            key: versions.get(key)
            for key in ("risk_engine_version", "policy_fingerprint", "cost_terms_version")
        }
    return {
        "risk_status": status,
        "phase4_status_raw": raw,
        "risk_evidence": evidence,
        "risk_fingerprint": _sha256({"status": status, "evidence": evidence}),
        "evaluated_at_utc": result.get("evaluated_at_utc"),
        "symbol": result.get("symbol"),
        "timeframe": result.get("timeframe"),
    }


class DecisionJournal:
    """Captures request-time decisions and risk assessments into the journal store."""

    name = "decision_journal"

    def __init__(self, store: JournalStore, *, clock: Callable[[], datetime] | None = None):
        self.store = store
        self._clock = clock or store.now
        self.capture_failures = 0
        self.last_failure: str | None = None
        self.captured = 0

    # -- capture (never raises) --------------------------------------------------------
    def capture_analysis(self, envelope: Any, source: str = "analysis_request") -> str | None:
        try:
            return self.record_analysis(copy.deepcopy(envelope), source)
        except Exception as exc:  # isolated: the caller's response is never affected
            self._failed(exc)
            return None

    def capture_risk(self, result: Any, decision_id: str | None) -> str | None:
        if decision_id is None:
            return None
        try:
            return self.record_risk(copy.deepcopy(result), decision_id)
        except Exception as exc:
            self._failed(exc)
            return None

    def _failed(self, exc: Exception) -> None:
        self.capture_failures += 1
        self.last_failure = exc.code if isinstance(exc, JournalError) else type(exc).__name__
        LOGGER.warning("decision journal capture failed (%s)", self.last_failure)

    # -- recording ---------------------------------------------------------------------
    def record_analysis(self, envelope: Mapping[str, Any], source: str) -> str:
        if source not in CAPTURE_SOURCES:
            raise ValueError("unknown capture source")
        record = analysis_record(envelope)
        now = self._clock()
        with self.store.write() as conn:
            latest = conn.execute(
                "SELECT id, analysis_fingerprint FROM decision WHERE symbol=? AND timeframe=? "
                "ORDER BY rowid DESC LIMIT 1",
                (record["symbol"], record["timeframe"]),
            ).fetchone()
            if (
                latest is not None
                and latest["analysis_fingerprint"] == record["analysis_fingerprint"]
            ):
                self._observe(conn, "decision_observation", "decision_id", latest["id"], now)
                decision_id = latest["id"]
            else:
                decision_id = new_ulid(now)
                conn.execute(
                    "INSERT INTO decision(id, symbol, broker_symbol, timeframe, as_of_utc, "
                    "analysis_outcome, t6_rule, market_direction, t6_evidence, "
                    "analysis_fingerprint, snapshot_id, evaluated_at_utc, previous_decision_id, "
                    "capture_source, recorded_utc) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        decision_id,
                        record["symbol"],
                        record["broker_symbol"],
                        record["timeframe"],
                        record["as_of_utc"],
                        record["analysis_outcome"],
                        record["t6_rule"],
                        record["market_direction"],
                        json.dumps(record["t6_evidence"], sort_keys=True, allow_nan=False),
                        record["analysis_fingerprint"],
                        record["snapshot_id"],
                        record["evaluated_at_utc"],
                        latest["id"] if latest else None,
                        source,
                        utc_iso(now),
                    ),
                )
                conn.execute(
                    "INSERT INTO decision_observation(decision_id, first_seen_utc, "
                    "last_seen_utc, observations) VALUES (?,?,?,1)",
                    (decision_id, utc_iso(now), utc_iso(now)),
                )
                self.store.append_audit(
                    conn,
                    action="decision.recorded",
                    table="decision",
                    record_id=decision_id,
                    new={
                        "analysis_outcome": record["analysis_outcome"],
                        "market_direction": record["market_direction"],
                        "previous_decision_id": latest["id"] if latest else None,
                        "capture_source": source,
                    },
                    at=now,
                )
        self.captured += 1
        return decision_id

    def record_risk(self, result: Mapping[str, Any], decision_id: str) -> str:
        record = risk_record(result)
        now = self._clock()
        with self.store.write() as conn:
            decision = conn.execute(
                "SELECT symbol, timeframe FROM decision WHERE id=?", (decision_id,)
            ).fetchone()
            if decision is None:
                raise ValueError("unknown decision")
            if (decision["symbol"], decision["timeframe"]) != (
                record["symbol"],
                record["timeframe"],
            ):
                raise ValueError("risk result does not belong to this decision")
            latest = conn.execute(
                "SELECT id, risk_fingerprint FROM risk_assessment WHERE decision_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (decision_id,),
            ).fetchone()
            if latest is not None and latest["risk_fingerprint"] == record["risk_fingerprint"]:
                self._observe(
                    conn, "risk_assessment_observation", "assessment_id", latest["id"], now
                )
                return latest["id"]
            assessment_id = new_ulid(now)
            conn.execute(
                "INSERT INTO risk_assessment(id, decision_id, risk_status, phase4_status_raw, "
                "risk_evidence, risk_fingerprint, evaluated_at_utc, previous_assessment_id, "
                "recorded_utc) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    assessment_id,
                    decision_id,
                    record["risk_status"],
                    record["phase4_status_raw"],
                    json.dumps(record["risk_evidence"], sort_keys=True, allow_nan=False),
                    record["risk_fingerprint"],
                    record["evaluated_at_utc"],
                    latest["id"] if latest else None,
                    utc_iso(now),
                ),
            )
            conn.execute(
                "INSERT INTO risk_assessment_observation(assessment_id, first_seen_utc, "
                "last_seen_utc, observations) VALUES (?,?,?,1)",
                (assessment_id, utc_iso(now), utc_iso(now)),
            )
            self.store.append_audit(
                conn,
                action="risk_assessment.recorded",
                table="risk_assessment",
                record_id=assessment_id,
                new={
                    "decision_id": decision_id,
                    "risk_status": record["risk_status"],
                    "previous_assessment_id": latest["id"] if latest else None,
                },
                at=now,
            )
        return assessment_id

    def _observe(self, conn, table: str, key: str, record_id: str, now: datetime) -> None:
        row = conn.execute(
            f"SELECT last_seen_utc, observations FROM {table} WHERE {key}=?", (record_id,)
        ).fetchone()
        new_seen = max(row["last_seen_utc"], utc_iso(now))
        conn.execute(
            f"UPDATE {table} SET last_seen_utc=?, observations=observations+1 WHERE {key}=?",
            (new_seen, record_id),
        )
        self.store.append_audit(
            conn,
            action="observation.updated",
            table=table,
            record_id=record_id,
            old={"last_seen_utc": row["last_seen_utc"], "observations": row["observations"]},
            new={"last_seen_utc": new_seen, "observations": row["observations"] + 1},
            at=now,
        )

    # -- queries -----------------------------------------------------------------------
    _LIST_SQL = f"""
        SELECT d.*, o.first_seen_utc, o.last_seen_utc, o.observations,
          (SELECT r.risk_status FROM risk_assessment r WHERE r.decision_id = d.id
             ORDER BY r.rowid DESC LIMIT 1) AS latest_risk,
          (SELECT n.id FROM decision n
             WHERE n.previous_decision_id = d.id LIMIT 1) AS superseded_by,
          {_MANUALLY_RECORDED_SQL} AS manually_recorded,
          d.rowid AS cursor_key
        FROM decision d JOIN decision_observation o ON o.decision_id = d.id
    """

    def _item(self, row, now: datetime) -> dict[str, Any]:
        risk = row["latest_risk"] or "NOT_EVALUATED"
        eligible = row["analysis_outcome"] == "TRADE_CANDIDATE" and risk == "APPROVED"
        state = "CURRENT"
        if row["superseded_by"]:
            state = "SUPERSEDED"
        elif row["as_of_utc"] and row["timeframe"] in _TIMEFRAME_SECONDS:
            as_of = datetime.fromisoformat(row["as_of_utc"].replace("Z", "+00:00"))
            if now >= as_of + timedelta(seconds=_TIMEFRAME_SECONDS[row["timeframe"]]):
                state = "EXPIRED"
        return {
            "id": row["id"],
            "symbol": row["symbol"],
            "broker_symbol": row["broker_symbol"],
            "timeframe": row["timeframe"],
            "as_of_utc": row["as_of_utc"],
            "analysis_outcome": row["analysis_outcome"],
            "t6_rule": row["t6_rule"],
            "market_direction": row["market_direction"],
            "risk_status": risk,
            "trade_eligibility": "APPROVED" if eligible else "NOT_APPROVED",
            "execution_status": "MANUALLY_RECORDED" if row["manually_recorded"] else "NONE",
            "state": state,
            "evaluated_at_utc": row["evaluated_at_utc"],
            "first_seen_utc": row["first_seen_utc"],
            "last_seen_utc": row["last_seen_utc"],
            "observations": row["observations"],
            "capture_source": row["capture_source"],
            "previous_decision_id": row["previous_decision_id"],
            "superseded_by": row["superseded_by"],
        }

    def list(
        self,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
        outcome: str | None = None,
        eligibility: str | None = None,
        execution: str | None = None,
        cursor: int | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        clauses, params = [], []
        for column, value in (
            ("d.symbol", symbol),
            ("d.timeframe", timeframe),
            ("d.analysis_outcome", outcome),
        ):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if eligibility == "APPROVED":
            clauses.append(
                "d.analysis_outcome = 'TRADE_CANDIDATE' AND (SELECT r.risk_status FROM "
                "risk_assessment r WHERE r.decision_id = d.id ORDER BY r.rowid DESC "
                "LIMIT 1) = 'APPROVED'"
            )
        elif eligibility == "NOT_APPROVED":
            clauses.append(
                "NOT (d.analysis_outcome = 'TRADE_CANDIDATE' AND COALESCE((SELECT "
                "r.risk_status FROM risk_assessment r WHERE r.decision_id = d.id "
                "ORDER BY r.rowid DESC LIMIT 1), '') = 'APPROVED')"
            )
        if execution == "MANUALLY_RECORDED":
            clauses.append(_MANUALLY_RECORDED_SQL)
        elif execution == "NONE":
            clauses.append(f"NOT {_MANUALLY_RECORDED_SQL}")
        elif execution is not None:
            clauses.append("0")  # BROKER_VERIFIED needs reconciliation (not implemented)
        if cursor is not None:
            clauses.append("d.rowid < ?")
            params.append(cursor)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        limit = max(1, min(int(limit), 100))
        with self.store.read() as conn:
            rows = conn.execute(
                f"{self._LIST_SQL} {where} ORDER BY d.rowid DESC LIMIT ?", (*params, limit + 1)
            ).fetchall()
        now = self._clock()
        items = [self._item(row, now) for row in rows[:limit]]
        next_cursor = str(rows[limit - 1]["cursor_key"]) if len(rows) > limit else None
        return {"coverage": COVERAGE, "decisions": items, "next_cursor": next_cursor}

    def get(self, decision_id: str) -> dict[str, Any] | None:
        with self.store.read() as conn:
            row = conn.execute(f"{self._LIST_SQL} WHERE d.id = ?", (decision_id,)).fetchone()
            if row is None:
                return None
            assessments = conn.execute(
                "SELECT a.*, o.first_seen_utc, o.last_seen_utc, o.observations "
                "FROM risk_assessment a JOIN risk_assessment_observation o "
                "ON o.assessment_id = a.id WHERE a.decision_id = ? ORDER BY a.rowid",
                (decision_id,),
            ).fetchall()
            chain, previous = [], row["previous_decision_id"]
            while previous and len(chain) < 50:
                chain.append(previous)
                parent = conn.execute(
                    "SELECT previous_decision_id FROM decision WHERE id=?", (previous,)
                ).fetchone()
                previous = parent["previous_decision_id"] if parent else None
        item = self._item(row, self._clock())
        item["t6_evidence"] = json.loads(row["t6_evidence"])
        item["supersession_chain"] = chain
        item["risk_assessments"] = [
            {
                "id": a["id"],
                "risk_status": a["risk_status"],
                "phase4_status_raw": a["phase4_status_raw"],
                "risk_evidence": json.loads(a["risk_evidence"]),
                "evaluated_at_utc": a["evaluated_at_utc"],
                "first_seen_utc": a["first_seen_utc"],
                "last_seen_utc": a["last_seen_utc"],
                "observations": a["observations"],
                "previous_assessment_id": a["previous_assessment_id"],
            }
            for a in assessments
        ]
        return {"coverage": COVERAGE, "decision": item}

    def status(self) -> dict[str, object]:
        return {
            "captured": self.captured,
            "capture_failures": self.capture_failures,
            "last_failure": self.last_failure,
        }


class CapturingAnalysis:
    """Wraps the analysis service handed to ``RiskService`` so the T6 evaluation made inside a
    risk request is journaled and its decision id is available for the risk assessment. The
    envelope is returned unchanged; analysis behaviour is not altered."""

    def __init__(self, inner: Any, journal: DecisionJournal) -> None:
        self._inner = inner
        self._journal = journal

    def analyse(self, symbol: str, timeframe: str) -> dict[str, Any]:
        envelope = self._inner.analyse(symbol, timeframe)
        LAST_CAPTURED_DECISION.set(self._journal.capture_analysis(envelope, "risk_request"))
        return envelope

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)
