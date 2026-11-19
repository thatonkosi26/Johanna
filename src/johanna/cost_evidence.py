"""P6.2b.1 cost evidence: structured cost components, timeframe holding assumptions and the
broker rollover schedule. Pure: no clock, no network, no files beyond the bundled IANA
time-zone database (``tzdata``).

**Nothing here configures a production cost or approves a policy.** Production ``COST_TERMS``
stays empty; every missing or not-yet-approved input keeps the risk result UNVERIFIED.

Cost components (commission, slippage)
-------------------------------------
Each component is one evidence record with its value, **currency, unit and charging basis
stated explicitly** (nothing is inferred), the instruments and account type it applies to, its
source, evidence and effective dates, an approval status and the approval time. Only a
``VERIFIED`` component, approved no more than ``MAX_EVIDENCE_AGE`` (30 days) ago, already
effective, and covering the instrument, is usable. ``UNVERIFIED``, ``PROVISIONAL`` and
``REJECTED`` components, expired ones and missing ones never satisfy a requirement and are
never treated as zero. Each component is assessed independently.

Conversions are explicit and auditable: a USD amount becomes USC (x 100) and a per-side
commission becomes round trip (x 2) only through recorded steps; pips become points (x 10)
only for 5- and 3-digit quotes. Swap is not a configurable component: current swap rates are
read live from MT5 on every evaluation.

Holding assumptions
-------------------
One assumption per timeframe (H1, H4, D1): the maximum holding duration used to *estimate*
swap, with its rationale, status (``PROPOSED`` or ``APPROVED``), effective date and policy
version. Only an ``APPROVED`` assumption (approved within 30 days, already effective) is used;
otherwise a swap that is a cost stays unverified. ``HOLDING_PROPOSALS`` are draft values only.
A holding assumption is not a time stop: nothing here closes, modifies or monitors positions.

Rollover schedule
-----------------
Exness states that swap is applied on weekdays at 21:00 GMT in summer and 22:00 GMT in winter,
tripled on Wednesday for forex (Help Centre, "About swap"; read via search summaries, P6.2b).
That pair of times is exactly 17:00 New York time under US daylight-saving rules -- the
industry rollover convention -- so the primary schedule is 17:00 America/New_York, Monday to
Friday. Exness does not say which daylight-saving calendar "summer" follows; in the weeks
when US and UK clocks differ, the alternative reading (22:00 Europe/London) gives other
instants. Both are computed and the **larger** charge count is used (conservative), with the
ambiguity reported. The triple day comes from the live MT5 symbol (``swap_rollover3days``).
Rollovers at or after the evaluation time and up to the end of the holding window count,
each once. Bank-holiday rescheduling is **not modelled** (no reliable source); this is
reported with every swap result.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

MAX_EVIDENCE_AGE = timedelta(days=30)
SUPPORTED_INSTRUMENTS = ("EURUSDc", "GBPUSDc", "USDJPYc")
ACCOUNT_TYPE = "exness_standard_cent"
USC_PER_USD = 100
POINTS_PER_PIP = 10  # 5-digit (EURUSD, GBPUSD) and 3-digit (USDJPY) quotes only
PIP_DIGITS = (3, 5)
TIMEFRAMES = ("H1", "H4", "D1")
EVIDENCE_STATUSES = ("VERIFIED", "UNVERIFIED", "PROVISIONAL", "REJECTED")  # EXPIRED: derived
HOLDING_STATUSES = ("PROPOSED", "APPROVED", "REJECTED")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_RULES = {
    "commission": {"units": ("amount_per_lot",), "currencies": ("USC", "USD")},
    "slippage": {"units": ("points", "pips"), "currencies": (None,)},
}
_BASES = ("per_side", "round_trip")
ROLLOVER_PRIMARY = ("America/New_York", time(17, 0))
ROLLOVER_ALTERNATIVE = ("Europe/London", time(22, 0))
ROLLOVER_SOURCE = (
    "Exness Help Centre 'About swap': applied at 21:00 GMT (summer) / 22:00 GMT (winter) on "
    "weekdays, triple on Wednesday for forex (secondary: search summary, accessed 2026-11-18)"
)
MAX_HOLDING_HOURS = 24 * 60  # 60 days: a sanity bound, not a policy
_FINGERPRINT = re.compile(r"sha256:[0-9a-f]{64}")


def _aware(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value


def _text(value: Any, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z") if value else None


# ====================================================================== cost components
@dataclass(frozen=True)
class CostComponent:
    """One cost-evidence record. Every field is mandatory unless typed optional; nothing is
    inferred from an omission."""

    component: str  # "commission" | "slippage"
    value: float
    currency: str | None  # commission: "USC" | "USD"; slippage (points/pips): None
    unit: str  # commission: "amount_per_lot" (per 1.0 lot); slippage: "points" | "pips"
    basis: str  # "per_side" | "round_trip"
    instruments: tuple[str, ...]  # exact broker symbols
    account_type: str  # must be ACCOUNT_TYPE
    source_reference: str  # URL or document reference
    source_description: str  # what the source states, verbatim where possible
    evidence_date: datetime  # when the evidence was obtained
    effective_utc: datetime  # from when the value applies
    status: str  # EVIDENCE_STATUSES
    approved_at_utc: datetime | None = None  # required for VERIFIED
    # M-5: base-currency units one "lot" of ``value`` refers to (per-lot commission only).
    # Required, never assumed, and it must equal the instrument's verified contract size.
    lot_units: float | None = None
    # M-1: broker terms per covered instrument, {broker symbol: broker_terms_fingerprint()}.
    # Required for VERIFIED evidence; every instrument is checked against its own fingerprint
    # only, so a fingerprint for one instrument can never validate another.
    broker_terms: Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        rules = _RULES.get(self.component)
        if rules is None:
            raise ValueError(f"unsupported cost component {self.component!r}")
        if (
            isinstance(self.value, bool)
            or not isinstance(self.value, int | float)
            or not math.isfinite(self.value)
            or self.value < 0
        ):
            raise ValueError("a cost value must be a finite, non-negative number")
        if self.unit not in rules["units"]:
            raise ValueError(f"unit {self.unit!r} is not supported for {self.component}")
        if self.currency not in rules["currencies"]:
            raise ValueError(
                f"currency {self.currency!r} is not valid for {self.component} in {self.unit}"
            )
        if self.basis not in _BASES:
            raise ValueError("charging basis must be per_side or round_trip")
        if (
            not isinstance(self.instruments, tuple)
            or not self.instruments
            or len(set(self.instruments)) != len(self.instruments)
            or any(s not in SUPPORTED_INSTRUMENTS for s in self.instruments)
        ):
            raise ValueError(
                f"instrument scope must name supported symbols {SUPPORTED_INSTRUMENTS}"
            )
        if self.account_type != ACCOUNT_TYPE:
            raise ValueError(f"account type must be {ACCOUNT_TYPE}")
        _text(self.source_reference, "source_reference")
        _text(self.source_description, "source_description")
        _aware(self.evidence_date, "evidence_date")
        _aware(self.effective_utc, "effective_utc")
        if self.status not in EVIDENCE_STATUSES:
            raise ValueError(f"status must be one of {EVIDENCE_STATUSES}")
        if self.approved_at_utc is not None:
            _aware(self.approved_at_utc, "approved_at_utc")
            if self.approved_at_utc < self.evidence_date:
                raise ValueError("approval cannot precede the evidence")
        if self.status == "VERIFIED" and self.approved_at_utc is None:
            raise ValueError("a VERIFIED component needs its approval time")
        if self.unit == "amount_per_lot":
            if (
                isinstance(self.lot_units, bool)
                or not isinstance(self.lot_units, int | float)
                or not math.isfinite(self.lot_units)
                or self.lot_units <= 0
            ):
                raise ValueError("per-lot evidence needs a positive lot_units (units per lot)")
        elif self.lot_units is not None:
            raise ValueError("lot_units applies to per-lot amounts only")
        if self.broker_terms is not None:
            if not isinstance(self.broker_terms, Mapping) or set(self.broker_terms) != set(
                self.instruments
            ):
                raise ValueError(
                    "broker_terms needs exactly one fingerprint per covered instrument"
                )
            for value in self.broker_terms.values():
                if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
                    raise ValueError(
                        "broker_terms values must come from broker_terms_fingerprint()"
                    )
        if self.status == "VERIFIED" and self.broker_terms is None:
            raise ValueError("VERIFIED evidence needs per-instrument broker-terms fingerprints")

    @property
    def expires_utc(self) -> datetime | None:
        return self.approved_at_utc + MAX_EVIDENCE_AGE if self.approved_at_utc else None


BROKER_TERM_FIELDS = (
    "contract_size",
    "digits",
    "point",
    "tick_size",
    "currency_base",
    "currency_profit",
    "currency_margin",
    "execution_mode",
)


def broker_terms_fingerprint(
    broker_symbol: str, account_currency: Any, unit_kind: Any, spec: Mapping[str, Any]
) -> str | None:
    """Fingerprint of the broker terms a per-lot cost depends on: account currency and unit,
    and the symbol's contract, precision, currencies and execution mode (quotes, swap rates
    and volume limits excluded: they change without changing what a per-lot cost means).
    ``None`` when any of them is unknown."""
    values = {name: spec.get(name) for name in BROKER_TERM_FIELDS}
    if account_currency is None or unit_kind is None or any(v is None for v in values.values()):
        return None
    data = {
        "broker_symbol": broker_symbol,
        "account_currency": account_currency,
        "account_unit": unit_kind,
        **values,
    }
    text = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _component_status(
    c: CostComponent,
    at: datetime,
    broker_symbol: str,
    live_terms: str | None = None,
    contract_size: Any = None,
) -> tuple[str, str]:
    """Effective status at ``at`` and the reason when it is not usable."""
    if broker_symbol not in c.instruments:
        return "OUT_OF_SCOPE", f"{c.component} evidence does not cover {broker_symbol}"
    if c.status != "VERIFIED":
        return c.status, f"{c.component} evidence is {c.status}"
    expected = (c.broker_terms or {}).get(broker_symbol)
    if expected is None:  # unreachable for validated VERIFIED records; kept fail-closed
        return "BROKER_TERMS_MISSING", f"no broker-terms fingerprint for {broker_symbol}"
    if live_terms is None:
        return "BROKER_TERMS_UNKNOWN", f"live broker terms for {broker_symbol} not verified"
    if live_terms != expected:
        return "BROKER_TERMS_CHANGED", (
            f"broker terms for {broker_symbol} changed since the {c.component} evidence"
        )
    if c.unit == "amount_per_lot":
        if (
            isinstance(contract_size, bool)
            or not isinstance(contract_size, int | float)
            or not math.isfinite(contract_size)
            or contract_size <= 0
        ):
            return "LOT_SIZE_UNKNOWN", f"verified contract size for {broker_symbol} unknown"
        if not math.isclose(float(c.lot_units), float(contract_size), rel_tol=0, abs_tol=1e-9):
            return "LOT_SIZE_MISMATCH", (
                f"{c.component} evidence is per {c.lot_units:g} units but one {broker_symbol} "
                f"lot is {contract_size:g} units"
            )
    if at < c.effective_utc:
        return (
            "NOT_YET_EFFECTIVE",
            f"{c.component} evidence is not effective until {c.effective_utc}",
        )
    if at < c.approved_at_utc:
        return "NOT_YET_APPROVED", f"{c.component} evidence was approved after the evaluation"
    if at > c.expires_utc:
        return "EXPIRED", (
            f"{c.component} evidence expired (approved more than {MAX_EVIDENCE_AGE.days} days ago)"
        )
    return "VERIFIED", ""


def commission_round_trip_usc(c: CostComponent) -> dict[str, Any]:
    """Round-trip USC per 1.0 lot, with every conversion step recorded."""
    value = float(c.value)
    steps = [f"{c.value:g} {c.currency} per lot of {c.lot_units:g} units, {c.basis}"]
    if c.currency == "USD":
        value *= USC_PER_USD
        steps.append(f"x {USC_PER_USD} USC per USD = {value:g} USC per lot")
    if c.basis == "per_side":
        value *= 2
        steps.append(f"x 2 sides = {value:g} USC per lot round trip")
    return {
        "usc_per_lot_round_trip": value,
        "usc_per_0_01_lot_round_trip": value / 100,
        "steps": steps,
    }


def slippage_points_per_side(c: CostComponent, digits: Any) -> dict[str, Any] | None:
    """Points per execution side, with every step recorded; None when pips cannot be
    converted for this quote precision."""
    value, steps = float(c.value), [f"{c.value:g} {c.unit}, {c.basis}"]
    if c.unit == "pips":
        if digits not in PIP_DIGITS:
            return None
        value *= POINTS_PER_PIP
        steps.append(f"x {POINTS_PER_PIP} points per pip ({digits}-digit quote) = {value:g} points")
    if c.basis == "round_trip":
        value /= 2
        steps.append(f"/ 2 sides = {value:g} points per side")
    return {"points_per_side": value, "steps": steps}


def assess_component(
    c: CostComponent | None,
    kind: str,
    at: datetime,
    broker_symbol: str,
    digits: Any = None,
    live_terms: str | None = None,
    contract_size: Any = None,
) -> dict[str, Any]:
    """Independent assessment of one component. ``usable`` only for current VERIFIED evidence
    that covers the instrument and converts; a missing record is MISSING, never zero."""
    if c is None:
        return {
            "component": kind,
            "status": "MISSING",
            "usable": False,
            "reason": f"no {kind} evidence",
            "record": None,
            "conversion": None,
        }
    status, reason = _component_status(c, at, broker_symbol, live_terms, contract_size)
    conversion: dict[str, Any] | None = None
    if status == "VERIFIED":
        conversion = (
            commission_round_trip_usc(c)
            if kind == "commission"
            else slippage_points_per_side(c, digits)
        )
        if conversion is None:
            status, reason = "UNCONVERTIBLE", f"pips cannot be converted for {digits}-digit quotes"
    return {
        "component": kind,
        "status": status,
        "usable": status == "VERIFIED",
        "reason": reason or None,
        "record": {
            "value": c.value,
            "currency": c.currency,
            "unit": c.unit,
            "basis": c.basis,
            "instruments": list(c.instruments),
            "account_type": c.account_type,
            "source_reference": c.source_reference,
            "source_description": c.source_description,
            "evidence_date": _iso(c.evidence_date),
            "effective_utc": _iso(c.effective_utc),
            "status": c.status,
            "approved_at_utc": _iso(c.approved_at_utc),
            "expires_utc": _iso(c.expires_utc),
            "lot_units": c.lot_units,
            "broker_terms": dict(c.broker_terms) if c.broker_terms else None,
        },
        "conversion": conversion,
    }


# ====================================================================== holding assumptions
@dataclass(frozen=True)
class HoldingAssumption:
    """Maximum holding duration assumed for swap estimation on one timeframe. Not a time stop."""

    timeframe: str
    max_hours: float
    rationale: str
    status: str  # HOLDING_STATUSES
    effective_utc: datetime
    policy_version: str
    approved_at_utc: datetime | None = None

    def __post_init__(self) -> None:
        if self.timeframe not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {TIMEFRAMES}")
        if (
            isinstance(self.max_hours, bool)
            or not isinstance(self.max_hours, int | float)
            or not math.isfinite(self.max_hours)
            or not 0 < self.max_hours <= MAX_HOLDING_HOURS
        ):
            raise ValueError(f"max_hours must be in (0, {MAX_HOLDING_HOURS}]")
        _text(self.rationale, "rationale")
        _text(self.policy_version, "policy_version")
        _aware(self.effective_utc, "effective_utc")
        if self.status not in HOLDING_STATUSES:
            raise ValueError(f"status must be one of {HOLDING_STATUSES}")
        if self.approved_at_utc is not None:
            _aware(self.approved_at_utc, "approved_at_utc")
        if self.status == "APPROVED" and self.approved_at_utc is None:
            raise ValueError("an APPROVED holding assumption needs its approval time")


def assess_holding(h: HoldingAssumption | None, timeframe: str, at: datetime) -> dict[str, Any]:
    if h is None:
        return {
            "timeframe": timeframe,
            "status": "MISSING",
            "usable": False,
            "reason": f"no holding-period assumption for {timeframe}",
        }
    view = {
        "timeframe": h.timeframe,
        "max_hours": h.max_hours,
        "rationale": h.rationale,
        "policy_version": h.policy_version,
        "effective_utc": _iso(h.effective_utc),
        "approved_at_utc": _iso(h.approved_at_utc),
    }
    if h.status != "APPROVED":
        return {
            **view,
            "status": h.status,
            "usable": False,
            "reason": f"holding period for {timeframe} is {h.status}, not approved",
        }
    if at < h.effective_utc or at < h.approved_at_utc:
        return {
            **view,
            "status": "NOT_YET_EFFECTIVE",
            "usable": False,
            "reason": f"holding period for {timeframe} is not yet effective",
        }
    if at > h.approved_at_utc + MAX_EVIDENCE_AGE:
        return {
            **view,
            "status": "EXPIRED",
            "usable": False,
            "reason": f"holding period for {timeframe} approval expired",
        }
    return {**view, "status": "APPROVED", "usable": True, "reason": None}


_PROPOSED = datetime(2026, 11, 18, tzinfo=UTC)
# Draft design proposals only (P6.2b.1). NOT approved policy and never used by COST_TERMS.
HOLDING_PROPOSALS: Mapping[str, HoldingAssumption] = {
    tf: HoldingAssumption(
        timeframe=tf,
        max_hours=hours,
        rationale="P6.2b.1 provisional design proposal; not approved as production policy",
        status="PROPOSED",
        effective_utc=_PROPOSED,
        policy_version="holding-proposal-2026-11-18",
    )
    for tf, hours in (("H1", 24.0), ("H4", 5 * 24.0), ("D1", 20 * 24.0))
}


# ====================================================================== cost terms
@dataclass(frozen=True)
class CostTerms:
    """Every configured cost input: evidence components and per-timeframe holding assumptions.
    Empty in production (``COST_TERMS``): nothing is verified, so nothing can be approved."""

    components: tuple[CostComponent, ...] = ()
    holding: Mapping[str, HoldingAssumption] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.components, tuple) or any(
            not isinstance(c, CostComponent) for c in self.components
        ):
            raise ValueError("components must be a tuple of CostComponent records")
        covered: set[tuple[str, str]] = set()
        for c in self.components:
            for symbol in c.instruments:
                if (c.component, symbol) in covered:  # never two values for one cost
                    raise ValueError(f"two {c.component} records cover {symbol}")
                covered.add((c.component, symbol))
        for timeframe, h in self.holding.items():
            if not isinstance(h, HoldingAssumption) or h.timeframe != timeframe:
                raise ValueError("holding assumptions are keyed by their own timeframe")

    @property
    def configured(self) -> bool:
        return bool(self.components or self.holding)

    def component(self, kind: str, broker_symbol: str) -> CostComponent | None:
        return next(
            (c for c in self.components if c.component == kind and broker_symbol in c.instruments),
            None,
        )


# ====================================================================== rollover schedule
def _events(start: datetime, end: datetime, zone: str, local: time, triple: int) -> list[dict]:
    tz = ZoneInfo(zone)
    day = start.astimezone(tz).date() - timedelta(days=1)
    last = end.astimezone(tz).date() + timedelta(days=1)
    events = []
    while day <= last:
        if day.weekday() < 5:  # Monday-Friday only: none on Saturday or Sunday
            instant = datetime.combine(day, local, tzinfo=tz).astimezone(UTC)
            if start <= instant <= end:  # at or after the evaluation, up to the window end
                events.append(
                    {
                        "at_utc": _iso(instant),
                        "local_date": day.isoformat(),
                        "weekday": WEEKDAYS[day.weekday()],
                        "units": 3 if day.weekday() == triple else 1,
                    }
                )
        day += timedelta(days=1)
    return events


def holding_window(
    evaluated_at: datetime, entry_valid_until: datetime, max_hours: float
) -> tuple[datetime, datetime]:
    """H-2: the conservative swap window ``[evaluated_at, max(entry_valid_until,
    evaluated_at) + max_hours]``. A position the proposal permits may be entered at any time
    until the proposal expires and held up to ``max_hours``; every such position's rollovers
    fall inside this window. Raises ValueError for naive times or an invalid duration."""
    _aware(evaluated_at, "evaluated_at")
    _aware(entry_valid_until, "entry_valid_until")
    if (
        isinstance(max_hours, bool)
        or not isinstance(max_hours, int | float)
        or not math.isfinite(max_hours)
        or not 0 < max_hours <= MAX_HOLDING_HOURS
    ):
        raise ValueError("invalid holding duration")
    return evaluated_at, max(entry_valid_until, evaluated_at) + timedelta(hours=max_hours)


def rollover_charges(start: datetime, end: datetime, triple_weekday: int) -> dict[str, Any]:
    """Swap charge units for rollovers in ``[start, end]`` (each rollover once; triple day
    x 3). Raises ValueError for naive times, an inverted or excessive window or an invalid
    triple day -- the caller then treats the swap as unverified."""
    _aware(start, "start")
    _aware(end, "end")
    if end < start or end - start > timedelta(hours=MAX_HOLDING_HOURS):
        raise ValueError("invalid holding window")
    if isinstance(triple_weekday, bool) or triple_weekday not in range(5):
        raise ValueError("triple-swap day must be Monday-Friday")
    primary = _events(start, end, *ROLLOVER_PRIMARY, triple_weekday)
    alternative = _events(start, end, *ROLLOVER_ALTERNATIVE, triple_weekday)
    units_primary = sum(e["units"] for e in primary)
    units_alternative = sum(e["units"] for e in alternative)
    return {
        "units": max(units_primary, units_alternative),
        "units_primary": units_primary,
        "units_alternative": units_alternative,
        "events": primary,
        "seasonal_ambiguity": units_primary != units_alternative,
        "convention": "17:00 America/New_York (21:00 / 22:00 GMT); alternative reading "
        "22:00 Europe/London; the larger count is used",
        "holiday_adjustments": "not_modelled",
        "source": ROLLOVER_SOURCE,
        "window_utc": [_iso(start), _iso(end)],
    }
