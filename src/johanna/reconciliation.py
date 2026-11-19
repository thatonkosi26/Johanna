"""P6.3 reconciliation of manual logbook trades with read-only broker observations (pure).

Inputs are already sanitised: broker identifiers have been replaced by HMAC references
(``position_ref``, ``deal_ref``, ``order_ref``) and only allowlisted fields remain. Nothing
here reads MT5, writes the journal or changes a trade.

States (per non-voided logbook trade):

- ``CONFIRMED_OPEN``   -- the trade's entry deal is in broker history and its position is open
  (broker-confirmed).
- ``CONFIRMED_CLOSED`` -- the entry deal is known and closing deals for its position, inside
  fully covered history, account for the whole entry volume (broker-confirmed).
- ``PENDING``          -- not established yet: history coverage does not reach the trade, or the
  position is absent but no complete closing evidence exists. **Absence from the open
  positions is never treated as a closure.**
- ``CANDIDATE_MATCH``  -- the trade has no broker reference and exactly one broker entry deal
  fits (symbol, direction, volume, time). Inferred; it is **never linked automatically**.
- ``AMBIGUOUS``        -- several deals fit, or several trades claim one deal. Never resolved
  automatically.
- ``UNMATCHED``        -- fully covered history contains no fitting deal (or a recorded
  reference is not found).
- ``UNAVAILABLE``      -- the observation is missing, failed or stale.

M-9: every trade result carries ``detail.link_status``: ``USER_CONFIRMED`` (an active link
the user confirmed), ``RECORDED_REFERENCE`` (a broker reference recorded on the trade),
``CANDIDATE`` / ``AMBIGUOUS`` (inferred, never a link), ``REJECTED`` (the user rejected the only
fitting position), ``NONE`` or ``UNAVAILABLE`` (no usable evidence; ``reason_code`` says why:
``no_observation``, ``observation_not_live``, ``observation_stale``, ...). Discrepancies
(``DISCREPANCY_CODES``) are evaluated only against broker-confirmed facts of a known position.

Broker positions that no trade reaches are reported as ``UNRECORDED_POSITION`` (reference
only). Every result says whether it is ``broker_confirmed``, ``inferred`` or ``unavailable``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

MATCH_TIME_TOLERANCE = timedelta(minutes=2)
MAX_OBSERVATION_AGE = timedelta(minutes=5)
DEAL_ENTRY_IN, DEAL_ENTRY_OUT, DEAL_ENTRY_INOUT, DEAL_ENTRY_OUT_BY = 0, 1, 2, 3
_DIRECTION_DEAL_TYPE = {"BUY": 0, "SELL": 1}
_VOLUME_EPSILON = 1e-9


def _result(trade_id, state, basis, **detail) -> dict[str, Any]:
    return {
        "subject": "trade",
        "trade_id": trade_id,
        "position_ref": detail.pop("position_ref", None),
        "state": state,
        "basis": basis,
        "detail": detail,
    }


def _covered(observation: Mapping[str, Any], since: datetime) -> bool:
    """History covers the trade: the query started before its first entry (with tolerance)
    and ran up to the observation time."""
    start, end = observation.get("coverage_from_utc"), observation.get("coverage_to_utc")
    observed = observation.get("observed_utc")
    if not all(isinstance(t, datetime) for t in (start, end, observed)):
        return False
    return start <= since - MATCH_TIME_TOLERANCE and end >= observed - MATCH_TIME_TOLERANCE


def _unavailable_reason(observation, now, max_age) -> tuple[str, str] | None:
    observed = (observation or {}).get("observed_utc")
    if observation is None:
        return "no_observation", "no broker observation"
    if observation.get("status") != "LIVE":
        return "observation_not_live", str(observation.get("reason") or "not live")
    if not isinstance(observed, datetime):
        return "observation_time_unknown", "broker observation time is unknown"
    if now - observed > max_age:
        return "observation_stale", f"broker observation older than {max_age}"
    if observed - now > timedelta(seconds=5):
        return "observation_from_future", "broker observation time is in the future"
    return None


def reconcile(
    trades: Sequence[Mapping[str, Any]],
    observation: Mapping[str, Any] | None,
    now: datetime,
    *,
    max_age: timedelta = MAX_OBSERVATION_AGE,
    links: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """``links`` (M-9): user decisions, ``{"confirmed": {trade_id: {"position_ref", "link_id"}},
    "rejected": {(trade_id, position_ref), ...}}``. Only a user-confirmed link (or a
    reference recorded on the trade) ties a trade to a position; a candidate never does."""
    live = [t for t in trades if not t.get("voided")]
    confirmed = dict((links or {}).get("confirmed") or {})
    rejected = set((links or {}).get("rejected") or ())
    unavailable = _unavailable_reason(observation, now, max_age)
    if unavailable is not None:
        code, why = unavailable
        return [
            _result(
                t["id"],
                "UNAVAILABLE",
                "unavailable",
                reason=why,
                reason_code=code,
                link_status="UNAVAILABLE",
            )
            for t in live
        ]

    deals = list(observation.get("deals") or [])
    positions = {p["position_ref"]: p for p in observation.get("positions") or []}
    by_ref = {d["deal_ref"]: d for d in deals}
    linked_positions = {
        link["position_ref"] for tid, link in confirmed.items() if tid in {t["id"] for t in live}
    }
    claims: dict[str, list[str]] = {}
    for t in live:
        for fill in _entries(t):
            if fill.get("broker_deal_ref"):
                claims.setdefault(fill["broker_deal_ref"], []).append(t["id"])
    claimed_deals = set(claims)
    reached_positions: set[str] = set()
    # H-1: candidate relationships are evaluated across ALL unreferenced trades first, so a
    # deal fitting more than one trade is never presented as a unique candidate for any.
    # M-9: deals of a user-linked position are claimed by that link; a pair the user
    # rejected is never offered again as a candidate for that trade.
    fits: dict[str, list[dict]] = {}
    fitted_by: dict[str, set[str]] = {}
    rejected_count: dict[str, int] = {}
    for t in live:
        entries = _entries(t)
        if t["id"] in confirmed or not entries or not _complete(entries):
            continue
        if any(f.get("broker_deal_ref") for f in entries):
            continue
        fitting = [
            d
            for d in deals
            if d["deal_ref"] not in claimed_deals
            and d.get("position_ref") not in linked_positions
            and _fits(t, d, entries)
        ]
        kept = [d for d in fitting if (t["id"], d.get("position_ref")) not in rejected]
        rejected_count[t["id"]] = len(fitting) - len(kept)
        fits[t["id"]] = kept
        for d in kept:
            fitted_by.setdefault(d["deal_ref"], set()).add(t["id"])
    results = []
    for t in live:
        entries = _entries(t)
        if t["id"] in confirmed:
            results.append(
                _with_link(
                    t,
                    confirmed[t["id"]],
                    entries,
                    by_ref,
                    positions,
                    deals,
                    observation,
                    reached_positions,
                )
            )
            continue
        if not entries:
            results.append(
                _result(t["id"], "PENDING", "inferred", reason="no entry fill", link_status="NONE")
            )
            continue
        if not _complete(entries):
            results.append(
                _result(
                    t["id"],
                    "PENDING",
                    "inferred",
                    reason="incomplete logbook evidence (entry time or volume missing)",
                    link_status="NONE",
                )
            )
            continue
        first = min(f["time_utc"] for f in entries)
        covered = _covered(observation, first)
        refs = [f["broker_deal_ref"] for f in entries if f.get("broker_deal_ref")]
        if refs:
            result = _with_reference(
                t, refs, by_ref, claims, positions, deals, covered, reached_positions
            )
            result["detail"]["link_status"] = "RECORDED_REFERENCE"
            results.append(result)
            continue
        candidates = fits.get(t["id"], [])
        shared = sorted(
            other
            for c in candidates
            for other in fitted_by.get(c["deal_ref"], set())
            if other != t["id"]
        )
        if len(candidates) > 1 or shared:
            results.append(
                _result(
                    t["id"],
                    "AMBIGUOUS",
                    "inferred",
                    reason=(
                        f"{len(candidates)} broker deals fit"
                        if len(candidates) > 1
                        else "the fitting broker deal also fits other logbook trades"
                    )
                    + "; never matched automatically",
                    candidates=[c["deal_ref"] for c in candidates],
                    competing_trades=sorted(set(shared)),
                    link_status="AMBIGUOUS",
                )
            )
        elif len(candidates) == 1:
            c = candidates[0]
            results.append(
                _result(
                    t["id"],
                    "CANDIDATE_MATCH",
                    "inferred",
                    reason="exactly one broker deal fits symbol, direction, volume and time, and "
                    "it fits no other trade; it is not linked until the user confirms it",
                    candidate=c["deal_ref"],
                    candidate_position_ref=c.get("position_ref"),
                    link_status="CANDIDATE",
                )
            )
        elif rejected_count.get(t["id"]):
            results.append(
                _result(
                    t["id"],
                    "UNMATCHED",
                    "inferred",
                    reason="the only fitting broker position was rejected by the user",
                    rejected_candidates=rejected_count[t["id"]],
                    link_status="REJECTED",
                )
            )
        elif not covered:
            results.append(
                _result(
                    t["id"],
                    "PENDING",
                    "inferred",
                    reason="history does not cover the trade",
                    link_status="NONE",
                )
            )
        else:
            results.append(
                _result(
                    t["id"],
                    "UNMATCHED",
                    "inferred",
                    reason="no fitting deal in covered history",
                    link_status="NONE",
                )
            )
    for ref, position in positions.items():
        if ref not in reached_positions and not any(
            r["detail"].get("candidate_position_ref") == ref and r["state"] == "CANDIDATE_MATCH"
            for r in results
        ):
            results.append(
                {
                    "subject": "broker_position",
                    "trade_id": None,
                    "position_ref": ref,
                    "state": "UNRECORDED_POSITION",
                    "basis": "broker_confirmed",
                    "detail": {
                        "symbol": position.get("symbol"),
                        "volume": position.get("volume"),
                        "reason": "open broker position with no logbook trade",
                    },
                }
            )
    _attach_discrepancies(results, live, positions, deals)
    return results


DISCREPANCY_CODES = (
    "LOGBOOK_CLOSED_BROKER_OPEN",
    "BROKER_CLOSED_LOGBOOK_OPEN",
    "IDENTIFIER_CONFLICT",
    "SYMBOL_MISMATCH",
    "DIRECTION_MISMATCH",
    "VOLUME_MISMATCH",
    "MULTIPLE_POSSIBLE_MATCHES",
    "LINKED_POSITION_NOT_OBSERVED",
)


def _with_link(t, link, entries, by_ref, positions, deals, observation, reached) -> dict:
    """A user-confirmed link: the position's broker state decides (never inferred)."""
    ref = link["position_ref"]
    reached.add(ref)
    base = {"link_status": "USER_CONFIRMED", "link_id": link.get("link_id")}
    recorded = {
        by_ref[f["broker_deal_ref"]].get("position_ref")
        for f in entries
        if f.get("broker_deal_ref") in by_ref
    }
    if recorded - {ref}:
        base["identifier_conflict"] = "a recorded deal reference belongs to another position"
    if ref in positions:
        return _result(
            t["id"],
            "CONFIRMED_OPEN",
            "broker_confirmed",
            position_ref=ref,
            broker_volume=positions[ref].get("volume"),
            **base,
        )
    linked = [d for d in deals if d.get("position_ref") == ref]
    volume_in = sum(d.get("volume") or 0 for d in linked if d.get("entry") == DEAL_ENTRY_IN)
    volume_out = sum(
        d.get("volume") or 0
        for d in linked
        if d.get("entry") in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY)
    )
    first = min(
        (f["time_utc"] for f in entries if isinstance(f.get("time_utc"), datetime)), default=None
    )
    covered = first is not None and _covered(observation, first)
    if covered and volume_in > 0 and volume_out + _VOLUME_EPSILON >= volume_in:
        return _result(
            t["id"],
            "CONFIRMED_CLOSED",
            "broker_confirmed",
            position_ref=ref,
            volume_in=volume_in,
            volume_out=volume_out,
            **base,
        )
    if not linked:
        base["linked_position_not_observed"] = True
    return _result(
        t["id"],
        "PENDING",
        "inferred",
        position_ref=ref,
        reason="linked position is neither open nor established closed in this observation; "
        "closure is never inferred from absence",
        volume_in=volume_in,
        volume_out=volume_out,
        **base,
    )


def _volumes(trade: Mapping[str, Any]) -> tuple[float, float]:
    recorded = [f for f in trade.get("fills") or [] if f.get("status", "RECORDED") == "RECORDED"]
    entry = sum(f.get("volume") or 0 for f in recorded if f.get("kind") == "entry")
    exit_ = sum(f.get("volume") or 0 for f in recorded if f.get("kind") == "exit")
    return entry, exit_


def _attach_discrepancies(results, trades, positions, deals) -> None:
    """M-9: discrepancies between the logbook and broker-confirmed facts. Only evaluated
    where the position is known through a recorded reference or a user-confirmed link;
    unavailable evidence yields no finding (it is reported as unavailable instead)."""
    by_id = {t["id"]: t for t in trades}
    for r in results:
        if r["subject"] != "trade":
            continue
        found: list[dict[str, Any]] = []
        t, ref, state = by_id[r["trade_id"]], r.get("position_ref"), r["state"]
        detail = r["detail"]
        if state == "AMBIGUOUS":
            found.append({"code": "MULTIPLE_POSSIBLE_MATCHES"})
        if detail.get("identifier_conflict"):
            found.append({"code": "IDENTIFIER_CONFLICT"})
        if detail.get("linked_position_not_observed"):
            found.append({"code": "LINKED_POSITION_NOT_OBSERVED"})
        if state in ("CONFIRMED_OPEN", "CONFIRMED_CLOSED") and ref:
            entry, exit_ = _volumes(t)
            logbook_closed = entry > 0 and exit_ + _VOLUME_EPSILON >= entry
            position = positions.get(ref)
            linked = [d for d in deals if d.get("position_ref") == ref]
            entry_deals = [d for d in linked if d.get("entry") == DEAL_ENTRY_IN]
            symbol = (position or {}).get("symbol") or next((d.get("symbol") for d in linked), None)
            if symbol is not None and symbol != t["broker_symbol"]:
                found.append(
                    {"code": "SYMBOL_MISMATCH", "broker": symbol, "logbook": t["broker_symbol"]}
                )
            side = (position or {}).get("type")
            if side is None and entry_deals:
                side = entry_deals[0].get("type")
            if side is not None and side != _DIRECTION_DEAL_TYPE.get(t["direction"]):
                found.append({"code": "DIRECTION_MISMATCH"})
            if state == "CONFIRMED_OPEN":
                if logbook_closed:
                    found.append({"code": "LOGBOOK_CLOSED_BROKER_OPEN"})
                else:
                    broker = position.get("volume") if position else None
                    if broker is None or abs(broker - (entry - exit_)) > _VOLUME_EPSILON:
                        found.append(
                            {
                                "code": "VOLUME_MISMATCH",
                                "broker_open": broker,
                                "logbook_open": round(entry - exit_, 8),
                            }
                        )
            else:
                if not logbook_closed:
                    found.append({"code": "BROKER_CLOSED_LOGBOOK_OPEN"})
                volume_in = sum(d.get("volume") or 0 for d in entry_deals)
                if abs(volume_in - entry) > _VOLUME_EPSILON:
                    found.append(
                        {
                            "code": "VOLUME_MISMATCH",
                            "broker_entry": volume_in,
                            "logbook_entry": round(entry, 8),
                        }
                    )
        if found:
            detail["discrepancies"] = [f["code"] for f in found]
            r["discrepancies"] = found


def discrepancies(results: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Flat list of discrepancy findings (code, trade, position, detail) from results."""
    out = []
    for r in results:
        for f in r.get("discrepancies") or []:
            out.append(
                {
                    "code": f["code"],
                    "trade_id": r.get("trade_id"),
                    "position_ref": r.get("position_ref"),
                    "detail": {k: v for k, v in f.items() if k != "code"},
                }
            )
    return out


def _complete(entries: Sequence[Mapping[str, Any]]) -> bool:
    """Every entry fill has a timezone-aware time and a positive numeric volume."""
    return all(
        isinstance(f.get("time_utc"), datetime)
        and f["time_utc"].tzinfo is not None
        and isinstance(f.get("volume"), int | float)
        and not isinstance(f.get("volume"), bool)
        and f["volume"] > 0
        for f in entries
    )


def _fits(trade: Mapping[str, Any], deal: Mapping[str, Any], entries) -> bool:
    """An entry deal fits a trade: symbol, direction, volume and time (within tolerance)."""
    return (
        deal.get("entry") == DEAL_ENTRY_IN
        and deal.get("symbol") == trade["broker_symbol"]
        and deal.get("type") == _DIRECTION_DEAL_TYPE.get(trade["direction"])
        and isinstance(deal.get("time_utc"), datetime)
        and any(
            abs((deal.get("volume") or 0) - f["volume"]) < _VOLUME_EPSILON
            and abs(deal["time_utc"] - f["time_utc"]) <= MATCH_TIME_TOLERANCE
            for f in entries
        )
    )


def _entries(trade: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return [
        f
        for f in trade.get("fills") or []
        if f.get("kind") == "entry" and f.get("status", "RECORDED") == "RECORDED"
    ]


def _with_reference(t, refs, by_ref, claims, positions, deals, covered, reached) -> dict:
    if any(len(claims.get(r, [])) > 1 for r in refs):
        return _result(
            t["id"], "AMBIGUOUS", "inferred", reason="a deal is claimed by several trades"
        )
    found = [by_ref[r] for r in refs if r in by_ref]
    if len(found) != len(refs):
        if not covered:
            return _result(
                t["id"], "PENDING", "inferred", reason="entry deal outside the covered history"
            )
        return _result(
            t["id"], "UNMATCHED", "inferred", reason="recorded deal reference not in broker history"
        )
    position_refs = {d.get("position_ref") for d in found}
    if len(position_refs) != 1 or None in position_refs:
        return _result(
            t["id"], "AMBIGUOUS", "inferred", reason="entry deals map to several positions"
        )
    (position_ref,) = position_refs
    reached.add(position_ref)
    if position_ref in positions:
        return _result(
            t["id"],
            "CONFIRMED_OPEN",
            "broker_confirmed",
            position_ref=position_ref,
            broker_volume=positions[position_ref].get("volume"),
        )
    volume_in = sum(
        d.get("volume") or 0
        for d in deals
        if d.get("position_ref") == position_ref and d.get("entry") == DEAL_ENTRY_IN
    )
    volume_out = sum(
        d.get("volume") or 0
        for d in deals
        if d.get("position_ref") == position_ref
        and d.get("entry") in (DEAL_ENTRY_OUT, DEAL_ENTRY_OUT_BY)
    )
    if covered and volume_in > 0 and volume_out + _VOLUME_EPSILON >= volume_in:
        return _result(
            t["id"],
            "CONFIRMED_CLOSED",
            "broker_confirmed",
            position_ref=position_ref,
            volume_in=volume_in,
            volume_out=volume_out,
        )
    return _result(
        t["id"],
        "PENDING",
        "inferred",
        position_ref=position_ref,
        reason="position not open, but closing deals in covered history do not establish the "
        "closure; it is not inferred from absence",
        volume_in=volume_in,
        volume_out=volume_out,
    )
