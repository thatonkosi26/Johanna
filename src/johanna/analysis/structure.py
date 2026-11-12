"""Market structure (T5): swing highs/lows and support/resistance clusters.

Pure and deterministic. Candles are dicts with ``t`` (UTC open, ISO-8601), ``h``, ``l`` and a
known timeframe step; they are the validated window's candles in chronological order.

Swings (symmetric window ``k``, default 2):
- swing high at ``i``: ``high_i`` is **strictly greater** than each of the ``k`` preceding
  highs and **greater than or equal** to each of the ``k`` following highs;
- swing low: mirrored with lows (strictly lower than the preceding, lower-or-equal to the
  following).
Tie rule: on an equal-price plateau only the **first** candle qualifies (later plateau
candles are not strictly above their predecessor), so a plateau yields at most one pivot.

No look-ahead: a pivot at ``i`` is confirmed only when candle ``i + k`` has closed; its
``confirmed_at_utc`` is that candle's close time (open + step), never the pivot's own time.
Candles in the last ``k`` positions can only be *pending* (left side satisfied, available
right side satisfied, not yet confirmed); pending pivots are reported but never used.

Support/resistance: confirmed swing lows (support) and swing highs (resistance) are clustered
separately. Tolerance = ``tolerance_atr`` x the ATR reference (the latest available ATR of the
window). Method: sort pivots by (price, pivot time); walk upwards; a pivot joins the current
cluster while ``price - cluster_lowest_price <= tolerance`` (inclusive), otherwise it starts a
new cluster; every cluster therefore spans at most the tolerance. Level = mean of member
prices; strength = number of contributing confirmed pivots (a count, not a probability).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def find_swings(
    candles: Sequence[Mapping[str, Any]], step_seconds: int, k: int = 2
) -> dict[str, list[dict[str, Any]]]:
    """Confirmed and pending swing highs/lows (see module docstring for the exact rules)."""
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise ValueError("k must be a positive integer")
    step = timedelta(seconds=step_seconds)
    n = len(candles)
    confirmed: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []
    for i in range(k, n):
        for kind, field, better, better_or_equal in (
            ("high", "h", lambda a, b: a > b, lambda a, b: a >= b),
            ("low", "l", lambda a, b: a < b, lambda a, b: a <= b),
        ):
            price = candles[i][field]
            if not all(better(price, candles[j][field]) for j in range(i - k, i)):
                continue
            right = range(i + 1, min(i + k, n - 1) + 1)
            if not all(better_or_equal(price, candles[j][field]) for j in right):
                continue
            record = {
                "type": kind,
                "price": price,
                "pivot_t": candles[i]["t"],
                "index": i,
                "left_t": [candles[j]["t"] for j in range(i - k, i)],
                "right_t": [candles[j]["t"] for j in right],
            }
            if i + k <= n - 1:
                confirming = candles[i + k]
                record["confirmation_status"] = "confirmed"
                record["confirmed_by_t"] = confirming["t"]
                record["confirmed_at_utc"] = _iso(_parse(confirming["t"]) + step)
                confirmed.append(record)
            else:
                record["confirmation_status"] = "pending"
                record["confirmed_by_t"] = None
                record["confirmed_at_utc"] = None
                record["candles_still_needed"] = i + k - (n - 1)
                pending.append(record)
    return {"confirmed": confirmed, "pending": pending}


def cluster_levels(pivots: Sequence[Mapping[str, Any]], tolerance: float) -> list[dict[str, Any]]:
    """Cluster one side's confirmed pivots (see module docstring)."""
    ordered = sorted(pivots, key=lambda p: (p["price"], p["pivot_t"]))
    clusters: list[list[Mapping[str, Any]]] = []
    for pivot in ordered:
        if clusters and pivot["price"] - clusters[-1][0]["price"] <= tolerance:
            clusters[-1].append(pivot)
        else:
            clusters.append([pivot])
    levels = []
    for members in clusters:
        prices = [m["price"] for m in members]
        confirmations = sorted(m["confirmed_at_utc"] for m in members)
        levels.append(
            {
                "level": sum(prices) / len(prices),
                "low": min(prices),
                "high": max(prices),
                "strength": len(members),
                "first_confirmed_at_utc": confirmations[0],
                "latest_confirmed_at_utc": confirmations[-1],
                "pivots": [
                    {
                        "price": m["price"],
                        "pivot_t": m["pivot_t"],
                        "confirmed_at_utc": m["confirmed_at_utc"],
                    }
                    for m in sorted(members, key=lambda m: m["pivot_t"])
                ],
            }
        )
    return levels


def support_resistance(
    swings: Mapping[str, Sequence[Mapping[str, Any]]],
    atr_reference: float | None,
    tolerance_atr: float = 0.25,
) -> dict[str, Any]:
    """Support from confirmed swing lows, resistance from confirmed swing highs."""
    confirmed = swings.get("confirmed", [])
    lows = [p for p in confirmed if p["type"] == "low"]
    highs = [p for p in confirmed if p["type"] == "high"]
    base: dict[str, Any] = {
        "tolerance_atr_multiple": tolerance_atr,
        "atr_reference": atr_reference,
        "tolerance": None,
        "method": "price-sorted greedy clustering; span <= tolerance; level = mean",
    }
    if atr_reference is None or not atr_reference > 0:
        return {
            **base,
            "status": "insufficient_history",
            "reason": "no ATR reference",
            "support": [],
            "resistance": [],
        }
    tolerance = tolerance_atr * atr_reference
    support = cluster_levels(lows, tolerance)
    resistance = cluster_levels(highs, tolerance)
    status = "available" if support or resistance else "insufficient_history"
    result = {
        **base,
        "tolerance": tolerance,
        "status": status,
        "support": support,
        "resistance": resistance,
    }
    if not support:
        result["support_reason"] = "no confirmed swing lows"
    if not resistance:
        result["resistance_reason"] = "no confirmed swing highs"
    return result
