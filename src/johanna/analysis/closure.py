"""Candle closure evidence (T1 rule, single authority; E1).

Pure: plain values in, a label out, no clock reads and no imports beyond the standard library.
``johanna.live_market`` (T1) labels every bar with this rule, and recency validation (E1)
recomputes the same rule to check that a snapshot's closure labels are ones T1 could have
produced. There is one definition of the grace and one of the rule.

Rule, in T1's precedence order, for a candle with a known close time:

- ``tick_observed``: a server tick at or after the close was seen (``latest_tick >= close``);
- ``clock_only``: otherwise, the clock used by T1 (the bars retrieval time) is at or past
  ``close + COMPLETION_GRACE``;
- ``forming``: neither; the candle may still change.

``unknown`` (a close that cannot be established) is not produced here; T1 assigns it when
the close itself is unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta

# A bar whose close has passed by less than this, with no later tick yet, is still treated as
# forming: the system clock may lead the server by up to the time-base tolerance.
COMPLETION_GRACE = timedelta(seconds=60)

FORMING, TICK_OBSERVED, CLOCK_ONLY = "forming", "tick_observed", "clock_only"


def classify_closure(close: datetime, latest_tick: datetime | None, now: datetime) -> str:
    """T1's closure label for a candle closing at ``close``.

    ``latest_tick``: the server time of the latest real tick (``None`` without one);
    ``now``: the clock T1 judged against (the bars retrieval time).
    """
    if latest_tick is not None and latest_tick >= close:
        return TICK_OBSERVED
    if now >= close + COMPLETION_GRACE:
        return CLOCK_ONLY
    return FORMING


def is_complete(close: datetime, latest_tick: datetime | None, now: datetime) -> bool:
    """Whether a bar closing at ``close`` can no longer change (T1's ``complete`` flag)."""
    return classify_closure(close, latest_tick, now) != FORMING
