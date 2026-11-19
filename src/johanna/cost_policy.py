"""P6.2b.2 cost-evidence and policy-management infrastructure.

**Approval-independent.** This module records, versions and explains cost evidence; it never
approves a value and it is never wired into the risk service: ``RiskService`` keeps using the
production ``COST_TERMS`` (empty), so every proposal stays UNVERIFIED until the user approves
evidence *and* separately approves using the registry for risk.

Registry (staged schema v5 tables ``cost_policy_record`` / ``cost_policy_decision``):

- A record is evidence for one commission or slippage value, or a holding-period proposal for
  one timeframe. It is stored ``UNVERIFIED``, ``PROVISIONAL`` (evidence) or ``PROPOSED``
  (holding) -- never verified on entry. Records are immutable and deduplicated by fingerprint;
  a newer record may name the record it supersedes.
- A decision is a separate append-only row. Only the user can ``APPROVED`` / ``REJECTED``;
  approving needs the converted value typed back (``confirmed_value``: USC per lot round trip
  for commission, points per side for slippage, hours for holding) so a USD figure cannot pass
  as USC unnoticed. Johanna may only ``INVALIDATED`` (for example when the broker terms the
  evidence was recorded under have changed). Nothing follows a rejection or invalidation.
- Effective state, derived read-only: ``RECORDED`` (no decision), ``APPROVED`` (latest
  approval within 30 days), ``EXPIRED``, ``REJECTED``, ``INVALIDATED``, ``SUPERSEDED``,
  ``BROKER_TERMS_CHANGED`` (live terms differ; reported, not written).

``cost_readiness`` explains, per instrument and timeframe, exactly why costs are unverified.
Swap rates are always live broker data; they are never recorded here.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from johanna.cost_evidence import (
    ACCOUNT_TYPE,
    MAX_EVIDENCE_AGE,
    SUPPORTED_INSTRUMENTS,
    TIMEFRAMES,
    CostComponent,
    CostTerms,
    HoldingAssumption,
    assess_component,
    assess_holding,
    broker_terms_fingerprint,
    commission_round_trip_usc,
    slippage_points_per_side,
)
from johanna.journal_store import JournalError, new_ulid, tables_installed, utc_iso

REGISTRY_TABLES = ("cost_policy_record", "cost_policy_decision")
NOT_INSTALLED = "not_installed"


def _iso(value: datetime | None) -> str | None:
    return utc_iso(value) if value else None


def _parse(text: str | None) -> datetime | None:
    return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None


def _fingerprint(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def confirmation_value(kind: str, record: Mapping[str, Any], digits: Any = None) -> str | None:
    """The converted value the user must type back to approve (``None``: not convertible)."""
    if kind == "holding":
        return f"{float(record['value']):g} hours"
    component = _component(record, status="UNVERIFIED")
    if kind == "commission":
        usc = commission_round_trip_usc(component)["usc_per_lot_round_trip"]
        return f"{usc:g} USC/lot RT per {float(record['lot_units']):g} units"
    converted = slippage_points_per_side(component, digits if digits is not None else 5)
    return f"{converted['points_per_side']:g} points/side" if converted else None


def _component(record: Mapping[str, Any], *, status: str, approved: datetime | None = None):
    return CostComponent(
        component=record["kind"],
        value=float(record["value"]),
        currency=record["currency"],
        unit=record["unit"],
        basis=record["basis"],
        instruments=tuple(record["instruments"]),
        account_type=record["account_type"],
        source_reference=record["source_reference"],
        source_description=record["source_description"],
        evidence_date=_parse(record["evidence_date"]),
        effective_utc=_parse(record["effective_utc"]),
        status=status,
        approved_at_utc=approved,
        lot_units=record.get("lot_units"),
        broker_terms=record.get("broker_terms"),
    )


class CostPolicyRegistry:
    """Staged registry. Every method reports ``not_installed`` while schema v5 is absent."""

    def __init__(self, store: Any, clock=None) -> None:
        self.store = store
        self._clock = clock or (lambda: datetime.now(UTC))

    def installed(self) -> bool:
        try:
            with self.store.read() as conn:
                return tables_installed(conn, *REGISTRY_TABLES)
        except JournalError:
            return False

    def _require(self) -> None:
        if not self.installed():
            raise JournalError(NOT_INSTALLED, "cost-policy registry not installed (schema v5)")

    # ------------------------------------------------------------------ writes (explicit)
    def record_evidence(
        self, component: CostComponent, *, supersedes: str | None = None, actor: str = "user"
    ) -> str:
        """Record commission/slippage evidence (never VERIFIED on entry)."""
        self._require()
        if component.status not in ("UNVERIFIED", "PROVISIONAL"):
            raise ValueError("evidence is recorded UNVERIFIED or PROVISIONAL; approval is separate")
        data = {
            "kind": component.component,
            "value": component.value,
            "currency": component.currency,
            "unit": component.unit,
            "basis": component.basis,
            "instruments": list(component.instruments),
            "account_type": component.account_type,
            "source_reference": component.source_reference,
            "source_description": component.source_description,
            "evidence_date": _iso(component.evidence_date),
            "effective_utc": _iso(component.effective_utc),
            "lot_units": component.lot_units,
            "broker_terms": dict(component.broker_terms) if component.broker_terms else None,
        }
        return self._insert(data, None, component.status, supersedes, actor)

    def record_holding(
        self, holding: HoldingAssumption, *, supersedes: str | None = None, actor: str = "user"
    ) -> str:
        """Record a holding-period proposal for one timeframe (always PROPOSED on entry)."""
        self._require()
        if holding.status != "PROPOSED":
            raise ValueError("holding periods are recorded PROPOSED; approval is separate")
        data = {
            "kind": "holding",
            "value": holding.max_hours,
            "currency": None,
            "unit": "hours",
            "basis": "per_position",
            "instruments": [],
            "timeframe": holding.timeframe,
            "rationale": holding.rationale,
            "policy_version": holding.policy_version,
            "effective_utc": _iso(holding.effective_utc),
        }
        return self._insert(data, holding.timeframe, "PROPOSED", supersedes, actor)

    def _insert(self, data, timeframe, status, supersedes, actor) -> str:
        if actor not in ("user", "johanna"):
            raise ValueError("unknown actor")
        fingerprint = _fingerprint(data)
        now = self._clock()
        with self.store.write() as conn:
            existing = conn.execute(
                "SELECT id FROM cost_policy_record WHERE record_fingerprint=?", (fingerprint,)
            ).fetchone()
            if existing is not None:
                return existing["id"]  # the same evidence is never recorded twice
            if supersedes is not None:
                old = conn.execute(
                    "SELECT kind, timeframe FROM cost_policy_record WHERE id=?", (supersedes,)
                ).fetchone()
                if old is None or (old["kind"], old["timeframe"]) != (data["kind"], timeframe):
                    raise ValueError("a record can only supersede one of the same kind")
            record_id = new_ulid(now)
            conn.execute(
                "INSERT INTO cost_policy_record(id, kind, timeframe, value, currency, unit, "
                "basis, instruments, status, record_json, record_fingerprint, "
                "broker_terms, lot_units, supersedes_id, actor, recorded_utc) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id,
                    data["kind"],
                    timeframe,
                    data["value"],
                    data["currency"],
                    data["unit"],
                    data["basis"],
                    json.dumps(data["instruments"]),
                    status,
                    json.dumps(data, sort_keys=True),
                    fingerprint,
                    json.dumps(data["broker_terms"], sort_keys=True)
                    if data.get("broker_terms")
                    else None,
                    data.get("lot_units"),
                    supersedes,
                    actor,
                    utc_iso(now),
                ),
            )
            self.store.append_audit(
                conn,
                action="cost_policy.recorded",
                table="cost_policy_record",
                record_id=record_id,
                new={"kind": data["kind"], "status": status, "fingerprint": fingerprint},
                at=now,
                actor=actor,
            )
        return record_id

    def decide(
        self,
        record_id: str,
        decision: str,
        reason: str,
        *,
        actor: str,
        confirmed_value: str | None = None,
        digits: Any = None,
    ) -> str:
        """Append a decision. APPROVED needs actor 'user' and the exact converted value."""
        self._require()
        if decision not in ("APPROVED", "REJECTED", "INVALIDATED"):
            raise ValueError("unknown decision")
        if decision != "INVALIDATED" and actor != "user":
            raise ValueError("only the user approves or rejects")
        now = self._clock()
        with self.store.write() as conn:
            row = conn.execute(
                "SELECT kind, record_json FROM cost_policy_record WHERE id=?", (record_id,)
            ).fetchone()
            if row is None:
                raise ValueError("unknown record")
            if decision == "APPROVED":
                record = json.loads(row["record_json"])
                if row["kind"] != "holding" and set(record.get("broker_terms") or {}) != set(
                    record.get("instruments") or []
                ):
                    raise ValueError(
                        "evidence needs one broker-terms fingerprint per covered instrument "
                        "before it can be approved"
                    )
                expected = confirmation_value(row["kind"], record, digits)
                if expected is None or confirmed_value != expected:
                    raise ValueError(f"approval needs the converted value typed back: {expected}")
            decision_id = new_ulid(now)
            conn.execute(
                "INSERT INTO cost_policy_decision(id, record_id, decision, reason, "
                "confirmed_value, actor, decided_utc) VALUES (?,?,?,?,?,?,?)",
                (decision_id, record_id, decision, reason, confirmed_value, actor, utc_iso(now)),
            )
            self.store.append_audit(
                conn,
                action=f"cost_policy.{decision.lower()}",
                table="cost_policy_decision",
                record_id=decision_id,
                new={"record_id": record_id, "decision": decision},
                at=now,
                actor=actor,
            )
        return decision_id

    # ------------------------------------------------------------------ reads
    def history(self, live_terms: Mapping[str, str | None] | None = None) -> dict[str, Any]:
        """Every record with its decisions and derived effective state (read-only)."""
        if not self.installed():
            return {"status": NOT_INSTALLED, "records": []}
        now = self._clock()
        with self.store.read() as conn:
            records = conn.execute("SELECT * FROM cost_policy_record ORDER BY rowid").fetchall()
            decisions = conn.execute("SELECT * FROM cost_policy_decision ORDER BY rowid").fetchall()
        by_record: dict[str, list] = {}
        for d in decisions:
            by_record.setdefault(d["record_id"], []).append(dict(d))
        superseded = {r["supersedes_id"] for r in records if r["supersedes_id"]}
        items = []
        for r in records:
            data = json.loads(r["record_json"])
            ds = by_record.get(r["id"], [])
            items.append(
                {
                    "id": r["id"],
                    "kind": r["kind"],
                    "timeframe": r["timeframe"],
                    "recorded_status": r["status"],
                    "record": data,
                    "decisions": ds,
                    "effective_state": _effective_state(ds, r["id"] in superseded, now),
                    "instrument_terms": _instrument_terms(data, live_terms or {}),
                    "recorded_utc": r["recorded_utc"],
                    "actor": r["actor"],
                }
            )
        return {"status": "installed", "records": items}

    def proposed_terms(self, live_terms: Mapping[str, str | None] | None = None) -> CostTerms:
        """The CostTerms the registry *would* supply (APPROVED, current, not superseded).
        For diagnostics only: nothing in Johanna uses it for risk without a separate approval."""
        history = self.history(live_terms)
        components, holding = [], {}
        for item in history["records"]:
            if item["effective_state"] != "APPROVED":
                continue
            approved = _parse(
                next(d for d in reversed(item["decisions"]) if d["decision"] == "APPROVED")[
                    "decided_utc"
                ]
            )
            if item["kind"] == "holding":
                data = item["record"]
                holding[item["timeframe"]] = HoldingAssumption(
                    timeframe=item["timeframe"],
                    max_hours=float(data["value"]),
                    rationale=data["rationale"],
                    status="APPROVED",
                    effective_utc=_parse(data["effective_utc"]),
                    policy_version=data["policy_version"],
                    approved_at_utc=approved,
                )
            else:
                components.append(_component(item["record"], status="VERIFIED", approved=approved))
        return CostTerms(components=tuple(components), holding=holding)


def _instrument_terms(data: Mapping[str, Any], live_terms: Mapping[str, Any]) -> dict:
    """M-1: per covered instrument, its own recorded fingerprint against its own live one.
    MATCH / CHANGED / UNKNOWN (live terms not verified) / NOT_RECORDED (no fingerprint)."""
    recorded = data.get("broker_terms") or {}
    out = {}
    for symbol in data.get("instruments") or []:
        mine = recorded.get(symbol)
        live = live_terms.get(symbol)
        out[symbol] = (
            "NOT_RECORDED"
            if mine is None
            else "UNKNOWN"
            if live is None
            else "MATCH"
            if live == mine
            else "CHANGED"
        )
    return out


def _effective_state(decisions, superseded, now) -> str:
    """Decision-based state; broker-terms changes are per instrument (``instrument_terms``)."""
    if any(d["decision"] == "INVALIDATED" for d in decisions):
        return "INVALIDATED"
    if any(d["decision"] == "REJECTED" for d in decisions):
        return "REJECTED"
    if superseded:
        return "SUPERSEDED"
    approvals = [d for d in decisions if d["decision"] == "APPROVED"]
    if not approvals:
        return "RECORDED"
    if now - _parse(approvals[-1]["decided_utc"]) > MAX_EVIDENCE_AGE:
        return "EXPIRED"
    return "APPROVED"


# ====================================================================== readiness (pure)
def live_broker_terms(instruments_payload: Mapping[str, Any]) -> dict[str, str | None]:
    """Broker-terms fingerprints per symbol from the Phase 3 ``instruments()`` payload."""
    currency = instruments_payload.get("account_currency")
    if isinstance(currency, Mapping):
        account_currency, unit_kind = (
            currency.get("value"),
            (currency.get("unit") or {}).get("kind"),
        )
    else:
        account_currency, unit_kind = currency, None
    out = {}
    for item in instruments_payload.get("instruments") or []:
        fields = item.get("fields") or {}
        spec = {
            k: (fields.get(k) or {}).get("value")
            for k in (
                "contract_size",
                "digits",
                "point",
                "tick_size",
                "currency_base",
                "currency_profit",
                "currency_margin",
                "execution_mode",
            )
        }
        verified = item.get("validation") == "verified"
        out[item.get("broker_symbol")] = (
            broker_terms_fingerprint(item.get("broker_symbol"), account_currency, unit_kind, spec)
            if verified
            else None
        )
    return out


def cost_readiness(
    terms: CostTerms,
    at: datetime,
    instruments_payload: Mapping[str, Any] | None,
    *,
    registry: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Why costs are (un)verified, per instrument and timeframe. Pure; reads nothing."""
    payload = instruments_payload or {}
    live = live_broker_terms(payload) if payload else {}
    specs = {i.get("broker_symbol"): i for i in payload.get("instruments") or []}
    instruments = []
    for symbol in SUPPORTED_INSTRUMENTS:
        item = specs.get(symbol) or {}
        fields = item.get("fields") or {}
        value = lambda k: (fields.get(k) or {}).get("value")  # noqa: E731
        digits = value("digits")
        commission = assess_component(
            terms.component("commission", symbol),
            "commission",
            at,
            symbol,
            live_terms=live.get(symbol),
            contract_size=value("contract_size"),
        )
        slippage = assess_component(
            terms.component("slippage", symbol),
            "slippage",
            at,
            symbol,
            digits,
            live_terms=live.get(symbol),
        )
        swap = {
            "source": "live MT5 symbol specification (never configured)",
            "available": bool(item) and item.get("validation") == "verified",
            "mode": value("swap_mode"),
            "long": value("swap_long"),
            "short": value("swap_short"),
            "triple_day": value("swap_rollover3days"),
        }
        swap["needs_holding_period"] = {
            side: (isinstance(swap[side], int | float) and swap[side] < 0)
            for side in ("long", "short")
        }
        holding = {tf: assess_holding(terms.holding.get(tf), tf, at) for tf in TIMEFRAMES}
        reasons = []
        for name, view in (("commission", commission), ("slippage", slippage)):
            if not view["usable"]:
                reasons.append(f"{name}: {view['reason']}")
        if not swap["available"]:
            reasons.append("swap: live broker specification not verified")
        for tf, view in holding.items():
            if not view["usable"] and any(swap["needs_holding_period"].values()):
                reasons.append(f"holding {tf}: {view['reason']}")
        instruments.append(
            {
                "broker_symbol": symbol,
                "account_currency": "USC",
                "commission": commission,
                "slippage": slippage,
                "swap": swap,
                "holding": holding,
                "live_broker_terms_fingerprint": live.get(symbol),
                "approval_possible": not reasons,
                "blocking_reasons": reasons,
            }
        )
    return {
        "label": "COST READINESS — DIAGNOSTIC ONLY; NOTHING HERE APPROVES A COST",
        "evaluated_at_utc": utc_iso(at),
        "account_type": ACCOUNT_TYPE,
        "currency_note": "All amounts in USC (1 USD = 100 USC); conversions are recorded",
        "production_terms_configured": terms.configured,
        "max_evidence_age_days": MAX_EVIDENCE_AGE.days,
        "instruments": instruments,
        "registry": registry or {"status": NOT_INSTALLED},
        "approval_possible_anywhere": any(i["approval_possible"] for i in instruments),
    }


def holding_proposals_view(proposals: Mapping[str, HoldingAssumption]) -> list[dict[str, Any]]:
    return [
        {**asdict(h), "effective_utc": _iso(h.effective_utc), "approved_at_utc": None}
        for h in proposals.values()
    ]
