"""P6.2a shadow trade intents and the opportunity funnel.

**HYPOTHETICAL — NOT AN ORDER.** A ``ShadowTradeIntent`` records the trade the Phase 4 risk
engine constructed for a *scheduled* T6 candidate, with its risk eligibility, so that shadow
evaluation can be studied. It is not an order, an order request, a manual trade or an
instruction to trade -- even when ``RISK_APPROVED``.

Isolation (enforced by tests): this module does not import the execution domain, any broker
adapter or the live-market service; ``ShadowTradeIntent`` is unrelated to the execution
domain's order type and nothing converts one into the other. Intents are written only by the
scheduler, inside its evaluation-completion transaction, and are read-only everywhere else.

Rules:

- One intent per completed scheduled candidate whose trade was *constructed* (entry, stop-loss
  and take-profit on the correct sides; ``gross_reward_risk`` present) and risk-assessed. A
  candidate that failed earlier (no structural target, stale data, ...) has no intent; its
  reasons stay in the risk assessment and the gate outcomes.
- The take-profit is always the structural TC6 level (``target_method`` = ``structural_tc6``);
  there is no fallback target.
- ``volume``, the risk amount, its share of equity and the margin estimate are stored only for
  a ``RISK_APPROVED`` intent (database constraints); otherwise they are NULL -- never invented.
- No account login or number, broker server or company name, credential, key, or broker order /
  position / ticket identifier is stored.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from johanna.journal_store import new_ulid, utc_iso

KIND = "SHADOW_HYPOTHETICAL"
LABEL = "HYPOTHETICAL — NOT AN ORDER"
TARGET_METHOD = "structural_tc6"
ELIGIBILITIES = ("RISK_APPROVED", "REJECTED", "UNVERIFIED")
_ELIGIBILITY = {
    "APPROVED_FOR_MANUAL_REVIEW": "RISK_APPROVED",
    "REJECTED": "REJECTED",
    "UNVERIFIED": "UNVERIFIED",
}
NOTE = (
    "Hypothetical trade parameters recorded by shadow evaluation for research only. Johanna has "
    "not placed and will not place this trade. RISK_APPROVED means every implemented risk check "
    "passed; it is not an instruction to trade."
)
# Unverified checks that only fail because transaction costs are not established.
_COST_CHECKS = frozenset(
    {
        ("costs", "net_reward_risk"),
        ("per_trade_risk", "limit"),
        ("daily_risk", "limit"),
        ("margin", "margin_volume"),
    }
)
_ACCOUNT_KEYS = ("retrieved_at_utc", "age_seconds")  # never company, server or account type


@dataclass(frozen=True)
class ShadowTradeIntent:
    """A hypothetical, immutable record. It has no behaviour and no conversion to an order."""

    evaluation_id: str
    decision_id: str
    risk_assessment_id: str
    symbol: str
    broker_symbol: str
    timeframe: str
    window_close_utc: str
    direction: str
    eligibility: str
    entry_price: float
    entry_side: str
    entry_reference_utc: str
    valid_until_utc: str | None
    stop_loss: float
    take_profit: float
    gross_reward_risk: float
    net_reward_risk: float | None
    volume: float | None
    risk_amount_usc: float | None
    risk_percent_equity: float | None
    margin_estimate_usc: float | None
    cost_status: str
    margin_status: str
    account_evidence: Mapping[str, Any]
    cost_evidence: Mapping[str, Any]
    policy_fingerprint: str
    risk_engine_version: int
    cost_terms_version: str
    reasons: Mapping[str, Any]
    kind: str = KIND
    target_method: str = TARGET_METHOD

    def __post_init__(self) -> None:
        if self.kind != KIND or self.target_method != TARGET_METHOD:
            raise ValueError("a shadow intent is hypothetical with a structural target")
        if self.eligibility not in ELIGIBILITIES:
            raise ValueError("unknown eligibility")
        if (self.eligibility == "RISK_APPROVED") != (self.volume is not None):
            raise ValueError("a volume exists exactly when the intent is risk-approved")

    def evidence_fingerprint(self) -> str:
        text = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"), allow_nan=False)
        return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


# ====================================================================== reasons (pure)
def reason_category(kind: str, area: str | None, check_id: str | None) -> str:
    """``policy_rejection`` (a verified input violates a rule), ``cost_unverified`` (blocked only
    because transaction costs are not established) or ``data_unverified`` (a required input or
    calculation could not be established)."""
    if kind == "rejection":
        return "policy_rejection"
    if area == "costs" or (area, check_id) in _COST_CHECKS:
        return "cost_unverified"
    return "data_unverified"


def reason_list(evidence: Mapping[str, Any]) -> list[dict[str, str]]:
    """Every rejection then every unverified check of a Phase 4 result or journal evidence."""
    items = []
    for kind, key in (("rejection", "rejection_reasons"), ("rejection", "rejections")):
        for item in evidence.get(key) or []:
            items.append(_reason(kind, item))
    for item in evidence.get("unverified") or []:
        items.append(_reason("unverified", item))
    return items


def _reason(kind: str, item: Mapping[str, Any]) -> dict[str, str]:
    area, check = item.get("area"), item.get("id")
    return {
        "kind": kind,
        "reason": f"{area}.{check}",
        "category": reason_category(kind, area, check),
    }


def primary_reason(evidence: Mapping[str, Any]) -> dict[str, str] | None:
    """The first rejection, otherwise the first unverified check (each evaluation once)."""
    items = reason_list(evidence)
    return items[0] if items else None


# ====================================================================== build (pure)
def build_intent(
    result: Mapping[str, Any],
    *,
    evaluation_id: str,
    decision_id: str,
    risk_assessment_id: str,
    window_close_utc: str,
) -> ShadowTradeIntent | None:
    """The intent for a constructed candidate trade, ``None`` when no trade was constructed.
    An inconsistent result raises (the completion transaction then records nothing)."""
    setup = result.get("setup") or {}
    if setup.get("t6_outcome") != "TRADE_CANDIDATE" or result.get("gross_reward_risk") is None:
        return None
    proposal = result.get("proposal") or {}
    eligibility = _ELIGIBILITY.get(result.get("status"))
    if eligibility is None:
        raise ValueError("unrecognised Phase 4 status")
    approved = eligibility == "RISK_APPROVED"
    sizing = result.get("sizing") or {}
    margin = result.get("margin") or {}
    costs = result.get("costs") or {}
    net = result.get("net_reward_risk") or {}
    versions = result.get("versions") or {}
    provenance = result.get("provenance") or {}
    validations = result.get("validations") or {}
    daily = result.get("daily_risk") or {}
    volume = sizing.get("volume") if approved else None
    risk_amount = sizing.get("final_loss") if approved else None
    if approved and (volume is None or risk_amount is None):
        raise ValueError("a risk-approved result without a verified volume and final loss")
    account = provenance.get("account") or {}
    instrument = provenance.get("instrument") or {}
    reasons = reason_list(result)
    return ShadowTradeIntent(
        evaluation_id=evaluation_id,
        decision_id=decision_id,
        risk_assessment_id=risk_assessment_id,
        symbol=result["symbol"],
        broker_symbol=result.get("broker_symbol") or instrument.get("broker_symbol"),
        timeframe=result["timeframe"],
        window_close_utc=window_close_utc,
        direction=result["direction"],
        eligibility=eligibility,
        entry_price=proposal["entry_price"],
        entry_side=proposal["entry_side"],
        entry_reference_utc=instrument.get("quote_time_utc") or result["evaluated_at_utc"],
        valid_until_utc=proposal.get("valid_until_utc"),
        stop_loss=proposal["stop_loss"],
        take_profit=proposal["take_profit"],
        gross_reward_risk=result["gross_reward_risk"],
        net_reward_risk=net.get("value") if net.get("status") == "estimated" else None,
        volume=volume,
        risk_amount_usc=risk_amount,
        risk_percent_equity=sizing.get("final_loss_percent_of_equity") if approved else None,
        margin_estimate_usc=margin.get("estimated") if approved else None,
        cost_status="established" if costs.get("status") == "established" else "unverified",
        margin_status=(validations.get("margin") or {}).get("status") or "not_evaluated",
        account_evidence={
            "currency": sizing.get("currency"),
            "equity": sizing.get("equity"),
            "daily_reference_capital": daily.get("reference_capital"),
            "free_margin_before": margin.get("free_margin_before"),
            **{key: account.get(key) for key in _ACCOUNT_KEYS},
        },
        cost_evidence={
            "status": costs.get("status") or "unverified",
            "cost_terms_version": versions.get("cost_terms_version"),
            "terms": costs.get("terms"),
            "per_lot_total": costs.get("per_lot_total"),
            "components": {
                name: costs.get(name) for name in ("spread", "commission", "swap", "slippage")
            },
        },
        policy_fingerprint=versions["policy_fingerprint"],
        risk_engine_version=versions["risk_engine_version"],
        cost_terms_version=versions["cost_terms_version"],
        reasons={
            "primary": reasons[0] if reasons else None,
            "items": reasons,
        },
    )


# ====================================================================== storage
_COLUMNS = (
    "id",
    "evaluation_id",
    "decision_id",
    "risk_assessment_id",
    "kind",
    "symbol",
    "broker_symbol",
    "timeframe",
    "window_close_utc",
    "direction",
    "eligibility",
    "entry_price",
    "entry_side",
    "entry_reference_utc",
    "valid_until_utc",
    "stop_loss",
    "take_profit",
    "target_method",
    "volume",
    "gross_reward_risk",
    "net_reward_risk",
    "risk_amount_usc",
    "risk_percent_equity",
    "margin_estimate_usc",
    "cost_status",
    "margin_status",
    "account_evidence",
    "cost_evidence",
    "policy_fingerprint",
    "risk_engine_version",
    "cost_terms_version",
    "reasons",
    "evidence_fingerprint",
    "recorded_utc",
)
_JSON_COLUMNS = ("account_evidence", "cost_evidence", "reasons")


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def insert_intent(store: Any, conn: Any, intent: ShadowTradeIntent, now: datetime) -> str:
    """Append one intent and its audit entry on ``conn`` (the caller's transaction)."""
    intent_id = new_ulid(now)
    data = asdict(intent)
    fingerprint = intent.evidence_fingerprint()
    row = {
        **data,
        **{name: _json(data[name]) for name in _JSON_COLUMNS},
        "id": intent_id,
        "evidence_fingerprint": fingerprint,
        "recorded_utc": utc_iso(now),
    }
    conn.execute(
        f"INSERT INTO shadow_intent({', '.join(_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(_COLUMNS))})",
        tuple(row[name] for name in _COLUMNS),
    )
    store.append_audit(
        conn,
        action="shadow_intent.recorded",
        table="shadow_intent",
        record_id=intent_id,
        new={
            "evaluation_id": intent.evaluation_id,
            "eligibility": intent.eligibility,
            "evidence_fingerprint": fingerprint,
        },
        at=now,
    )
    return intent_id


def intent_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """API form of a stored intent: JSON evidence decoded and the hypothetical label first."""
    view: dict[str, Any] = {"label": LABEL}
    for name in _COLUMNS:
        value = row[name]
        view[name] = json.loads(value) if name in _JSON_COLUMNS else value
    return view


def list_intents(
    conn: Any,
    *,
    symbol: str | None = None,
    timeframe: str | None = None,
    eligibility: str | None = None,
    start: str | None = None,
    end: str | None = None,
    cursor: int | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    clauses, params = [], []
    for column, value in (
        ("symbol", symbol),
        ("timeframe", timeframe),
        ("eligibility", eligibility),
    ):
        if value is not None:
            clauses.append(f"{column} = ?")
            params.append(value)
    if start is not None:
        clauses.append("window_close_utc >= ?")
        params.append(start)
    if end is not None:
        clauses.append("window_close_utc < ?")
        params.append(end)
    if cursor is not None:
        clauses.append("rowid < ?")
        params.append(cursor)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    limit = max(1, min(int(limit), 100))
    rows = conn.execute(
        f"SELECT {', '.join(_COLUMNS)}, rowid AS cursor_key FROM shadow_intent {where} "
        "ORDER BY rowid DESC LIMIT ?",
        (*params, limit + 1),
    ).fetchall()
    items = [intent_view(r) for r in rows[:limit]]
    next_cursor = str(rows[limit - 1]["cursor_key"]) if len(rows) > limit else None
    return {"label": LABEL, "note": NOTE, "intents": items, "next_cursor": next_cursor}


def get_intent(conn: Any, intent_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        f"SELECT {', '.join(_COLUMNS)} FROM shadow_intent WHERE id=?", (intent_id,)
    ).fetchone()
    if row is None:
        return None
    evaluation = conn.execute(
        "SELECT id, status, t6_outcome, t6_rule, market_direction, risk_status, data_quality, "
        "attempts, completed_utc FROM evaluation_cycle WHERE id=?",
        (row["evaluation_id"],),
    ).fetchone()
    assessment = conn.execute(
        "SELECT id, risk_status, phase4_status_raw, evaluated_at_utc, recorded_utc "
        "FROM risk_assessment WHERE id=?",
        (row["risk_assessment_id"],),
    ).fetchone()
    return {
        "label": LABEL,
        "note": NOTE,
        "intent": intent_view(row),
        "evaluation": dict(evaluation) if evaluation else None,
        "risk_assessment": dict(assessment) if assessment else None,
    }


# ====================================================================== funnel (pure)
# A window is (symbol, timeframe, candle close). Should a candle ever have more than one row
# (e.g. across engine versions), the most advanced row represents it, so it counts once.
_PRIORITY = {"COMPLETED": 5, "FAILED": 4, "SKIPPED": 3, "MISSED": 2, "RUNNING": 1, "PENDING": 0}
_RISK = ("APPROVED", "REJECTED", "UNVERIFIED")
_OUTCOMES = ("MARKET_CONDITION", "NO_TRADE", "TRADE_CANDIDATE")


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _windows(rows: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    chosen: dict[tuple, Mapping[str, Any]] = {}
    for row in rows:
        key = (row["symbol"], row["timeframe"], row["window_close_utc"])
        rank = (_PRIORITY.get(row["status"], -1), row["rid"])
        best = chosen.get(key)
        if best is None or rank > (_PRIORITY.get(best["status"], -1), best["rid"]):
            chosen[key] = row
    return list(chosen.values())


def _flags(w: Mapping[str, Any]) -> dict[str, bool]:
    completed = w["status"] == "COMPLETED"
    candidate = completed and w["t6_outcome"] == "TRADE_CANDIDATE"
    assessed = candidate and w["risk_status"] in _RISK
    return {
        "completed": completed,
        "not_evaluated": w["status"] in ("SKIPPED", "MISSED", "FAILED"),
        "in_progress": w["status"] in ("PENDING", "RUNNING"),
        "market_condition": completed and w["t6_outcome"] == "MARKET_CONDITION",
        "no_trade": completed and w["t6_outcome"] == "NO_TRADE",
        "candidate": candidate,
        "risk_assessed": assessed,
        "approved": assessed and w["risk_status"] == "APPROVED",
        "rejected": assessed and w["risk_status"] == "REJECTED",
        "unverified": assessed and w["risk_status"] == "UNVERIFIED",
        "intent": w["intent_eligibility"] is not None,
        "approved_intent": w["intent_eligibility"] == "RISK_APPROVED",
    }


def _breakdown(windows: Sequence[Mapping[str, Any]], key) -> list[dict[str, Any]]:
    groups: dict[str, Counter] = {}
    for w in windows:
        name = key(w)
        if name is None:
            continue
        counter = groups.setdefault(name, Counter())
        counter["windows"] += 1
        for flag, on in _flags(w).items():
            counter[flag] += int(on)
    return [
        {
            "value": name,
            **{
                k: c[k]
                for k in (
                    "windows",
                    "completed",
                    "not_evaluated",
                    "in_progress",
                    "market_condition",
                    "no_trade",
                    "candidate",
                    "risk_assessed",
                    "approved",
                    "rejected",
                    "unverified",
                    "intent",
                    "approved_intent",
                )
            },
            "candidate_rate_of_completed": _rate(c["candidate"], c["completed"]),
        }
        for name, c in sorted(groups.items())
    ]


def build_funnel(
    rows: Iterable[Mapping[str, Any]],
    *,
    defect_ids: set[str],
    start: str | None,
    end: str | None,
) -> dict[str, Any]:
    """Opportunity funnel over scheduled evaluation windows (pure; rows from the journal)."""
    windows = _windows(rows)
    flags = [(w, _flags(w)) for w in windows]

    def count(flag: str) -> int:
        return sum(1 for _, f in flags if f[flag])

    total = len(windows)
    completed = count("completed")
    chain = [
        ("windows_scheduled", total),
        ("completed_evaluations", completed),
        ("trade_candidates", count("candidate")),
        ("risk_assessed", count("risk_assessed")),
        ("constructed_trade_intents", count("intent")),
        ("risk_approved_shadow_intents", count("approved_intent")),
    ]
    stages = []
    previous = None
    for name, value in chain:
        stages.append(
            {
                "id": name,
                "count": value,
                "of_previous": _rate(value, previous) if previous is not None else None,
                "of_completed": _rate(value, completed) if name != "windows_scheduled" else None,
            }
        )
        previous = value
    not_evaluated = Counter(w["status"] for w, f in flags if f["not_evaluated"])
    reasons_ne = Counter(
        (w["status"], w["reason_code"] or "unknown") for w, f in flags if f["not_evaluated"]
    )
    primary: Counter = Counter()
    categories: Counter = Counter()
    every: Counter = Counter()
    for w, f in flags:
        if not f["risk_assessed"]:
            continue
        evidence = json.loads(w["risk_evidence"]) if w["risk_evidence"] else {}
        first = primary_reason(evidence)
        if first is not None:
            primary[(first["reason"], first["category"], first["kind"])] += 1
            categories[first["category"]] += 1
        for item in {(i["reason"], i["category"], i["kind"]) for i in reason_list(evidence)}:
            every[item] += 1

    def reason_rows(counter: Counter) -> list[dict[str, Any]]:
        return [
            {"reason": r, "category": c, "kind": k, "count": n}
            for (r, c, k), n in sorted(counter.items(), key=lambda x: (-x[1], x[0]))
        ]

    defects_in_range = sum(1 for w in windows if w["id"] in defect_ids)
    return {
        "label": "SHADOW MODE — HYPOTHETICAL — NO ORDERS",
        "unit": "one scheduled evaluation window = (instrument, timeframe, candle close); "
        "retries and repeated observations of the same candle count once; dashboard-requested "
        "analyses are not scheduled evaluations and are excluded",
        "range": {"from_utc": start, "to_utc": end},
        "stages": stages,
        "outcomes": {
            k: {"count": count(f), "of_completed": _rate(count(f), completed)}
            for k, f in zip(_OUTCOMES, ("market_condition", "no_trade", "candidate"), strict=True)
        },
        "risk_outcomes": {
            k: {
                "count": count(k.lower()),
                "of_assessed": _rate(count(k.lower()), count("risk_assessed")),
            }
            for k in ("REJECTED", "UNVERIFIED", "APPROVED")
        },
        "not_evaluated": {
            "note": "skipped, missed and failed windows were not evaluated; they are never "
            "counted as NO_TRADE",
            "total": sum(not_evaluated.values()),
            **{s: not_evaluated.get(s, 0) for s in ("SKIPPED", "MISSED", "FAILED")},
            "reasons": [
                {"status": s, "reason_code": r, "count": n}
                for (s, r), n in sorted(reasons_ne.items(), key=lambda x: (-x[1], x[0]))
            ],
        },
        "in_progress": count("in_progress"),
        "breakdowns": {
            "symbol": _breakdown(windows, lambda w: w["symbol"]),
            "timeframe": _breakdown(windows, lambda w: w["timeframe"]),
            "direction": _breakdown(
                windows,
                lambda w: (w["market_direction"] or "NONE") if w["status"] == "COMPLETED" else None,
            ),
            "risk_status": _breakdown(
                windows,
                lambda w: (
                    w["risk_status"]
                    if w["status"] == "COMPLETED" and w["t6_outcome"] == "TRADE_CANDIDATE"
                    else None
                ),
            ),
        },
        "primary_reasons": {
            "note": "one primary reason per risk-assessed evaluation (the first rejection, "
            "otherwise the first unverified check)",
            "items": reason_rows(primary),
        },
        "reason_categories": {
            "note": "policy_rejection: a verified input violates a rule; data_unverified: a "
            "required input or calculation could not be established; cost_unverified: "
            "transaction costs are not established",
            **{
                c: categories.get(c, 0)
                for c in ("policy_rejection", "data_unverified", "cost_unverified")
            },
        },
        "all_reasons": {
            "note": "every reason of every risk-assessed evaluation: one evaluation may "
            "contribute several counts",
            "items": reason_rows(every),
        },
        "historical_recording_defects": {
            "t4_gate_empty_reason_code": defects_in_range,
            "treatment": "kept unchanged; counted as the completed evaluations they were, "
            "never as new failures",
        },
        "execution": {
            "executed_trades": None,
            "realised_pnl": None,
            "note": "Not available: shadow intents are hypothetical. Johanna places no orders, "
            "so no intent was executed and no result was realised.",
        },
    }
