"""Shared structural-target contract (P6.2b.8): one definition of a valid take-profit level.

T6's TC6 check and Phase 4's trade construction both call ``validate_structural_target`` so
that they can never disagree on whether a structural target exists or is valid:

- TC6 calls it on the level it selected, with the last closed candle's close as the reference
  price and the ATR clearance requirement (``clearance_atr``). No target means TC6 fails.
- Phase 4 calls it again, independently, on the level carried in the T6 envelope, with the
  executable entry (ask for a BUY, bid for a SELL) as the reference price and no clearance
  requirement. It never trusts the checklist's ``pass`` on its own. Phase 4 remains the final
  authority: live quotes, tick rounding, the structural stop-loss and the gross and net
  reward:risk checks all follow, unchanged.

A valid structural target is a mapping with finite numeric ``low`` <= ``high`` boundaries and a
finite numeric ``strength`` >= ``min_strength`` (the T6 ``min_level_strength``), lying entirely
beyond the reference price in the trade direction (bullish: ``low`` above it; bearish: ``high``
below it) and, when a clearance requirement is given, at least ``clearance_atr`` ATR away.

TC6 clearance is a distance from the close; it is *not* a reward:risk measure. Passing it does
not establish that a trade reaches the 1:2 net reward:risk minimum -- only Phase 4 decides that.

Pure and deterministic: plain data in, plain data out. Malformed input (``None``, missing keys,
non-mapping values, booleans, strings, NaN, infinity) is never coerced; it produces a coded
failure, never an exception.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from numbers import Real
from typing import Any

STRUCTURAL_TARGET_VALID = "structural_target_valid"
NO_STRUCTURAL_TARGET = "no_structural_target"
TARGET_STRENGTH_BELOW_MINIMUM = "target_strength_below_minimum"
TARGET_WRONG_SIDE = "target_wrong_side"
INVALID_TARGET_EVIDENCE = "invalid_target_evidence"
INVALID_DIRECTION = "invalid_direction"
TARGET_CLEARANCE_INSUFFICIENT = "target_clearance_insufficient"

REASON_CODES = (
    STRUCTURAL_TARGET_VALID,
    NO_STRUCTURAL_TARGET,
    TARGET_STRENGTH_BELOW_MINIMUM,
    TARGET_WRONG_SIDE,
    INVALID_TARGET_EVIDENCE,
    INVALID_DIRECTION,
    TARGET_CLEARANCE_INSUFFICIENT,
)
DIRECTIONS = ("bullish", "bearish")


def _number(value: Any) -> float | None:
    """A finite real number, or None. Booleans and strings are not numbers here."""
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _result(
    reason: str,
    detail: str,
    *,
    target: dict[str, Any] | None = None,
    distance_atr: float | None = None,
) -> dict[str, Any]:
    return {
        "valid": reason == STRUCTURAL_TARGET_VALID,
        "reason": reason,
        "detail": detail,
        "target": target,
        "distance_atr": distance_atr,
    }


def validate_structural_target(
    target: Any,
    *,
    direction: Any,
    reference_price: Any,
    min_strength: Any,
    atr: Any = None,
    clearance_atr: Any = None,
) -> dict[str, Any]:
    """Validate one structural target; returns ``{valid, reason, detail, target, distance_atr}``.

    ``reason`` is one of ``REASON_CODES``. The checks run in a fixed order and the first
    failure wins: direction, existence, evidence structure (type, boundaries, strength,
    reference price, threshold), strength, side, then clearance (only when ``clearance_atr`` is
    given, which then also requires a finite positive ``atr``). ``target`` in the result is a
    copy of ``low``, ``high`` and ``strength`` (the validated values as given) once the evidence is
    well formed.
    """
    if not isinstance(direction, str) or direction not in DIRECTIONS:
        return _result(INVALID_DIRECTION, f"direction {direction!r} is not bullish or bearish")
    if target is None:
        return _result(NO_STRUCTURAL_TARGET, "no qualifying opposing structural level")
    if not isinstance(target, Mapping):
        return _result(
            INVALID_TARGET_EVIDENCE, f"target evidence is a {type(target).__name__}, not a mapping"
        )
    low, high = _number(target.get("low")), _number(target.get("high"))
    if low is None or high is None:
        return _result(INVALID_TARGET_EVIDENCE, "target boundaries missing or not finite numbers")
    if low > high:
        return _result(INVALID_TARGET_EVIDENCE, f"target low {low} is above its high {high}")
    strength = _number(target.get("strength"))
    if strength is None:
        return _result(INVALID_TARGET_EVIDENCE, "target strength missing or not a finite number")
    price = _number(reference_price)
    if price is None or price <= 0:
        return _result(INVALID_TARGET_EVIDENCE, "reference price missing or not a positive number")
    minimum = _number(min_strength)
    if minimum is None:
        return _result(INVALID_TARGET_EVIDENCE, "minimum strength missing or not a finite number")
    # validated values, as given (finite reals; nothing is coerced)
    normalised = {k: target[k] for k in ("low", "high", "strength")}
    if strength < minimum:
        return _result(
            TARGET_STRENGTH_BELOW_MINIMUM,
            f"target strength {strength:g} is below the minimum {minimum:g}",
            target=normalised,
        )
    bullish = direction == "bullish"
    beyond = low > price if bullish else high < price
    if not beyond:
        return _result(
            TARGET_WRONG_SIDE,
            f"target {low}-{high} is not entirely {'above' if bullish else 'below'} the "
            f"reference price {price}",
            target=normalised,
        )
    if clearance_atr is None:
        return _result(
            STRUCTURAL_TARGET_VALID,
            f"structural target {low}-{high}, strength {strength:g}",
            target=normalised,
        )
    threshold, atr_value = _number(clearance_atr), _number(atr)
    if threshold is None or atr_value is None or atr_value <= 0:
        return _result(
            INVALID_TARGET_EVIDENCE,
            "clearance threshold or ATR missing, not finite or not positive",
            target=normalised,
        )
    distance = ((low - price) if bullish else (price - high)) / atr_value
    if distance < threshold:
        return _result(
            TARGET_CLEARANCE_INSUFFICIENT,
            f"target is {distance:.3f} ATR away, below the {threshold:g} ATR clearance",
            target=normalised,
            distance_atr=distance,
        )
    return _result(
        STRUCTURAL_TARGET_VALID,
        f"structural target {low}-{high}, strength {strength:g}, {distance:.3f} ATR away",
        target=normalised,
        distance_atr=distance,
    )
