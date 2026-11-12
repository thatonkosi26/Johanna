"""Test-wide safety: no test may ever reach a real MetaTrader 5 terminal.

In production the live market service spawns MT5 worker processes (``_default_runner``)
that connect to the running terminal. Here that factory is replaced by a runner that always
reports "terminal not running", and the terminal process check always refuses, so any
application built without an explicitly injected fake reports UNAVAILABLE instead of
attaching to a terminal on the developer's machine. Tests that need MT5 data inject
``tests/fake_mt5.FakeMT5``; ``test_mt5_worker_process.py`` exercises the real process runner
with a fake session.
"""

from __future__ import annotations

import pytest

import johanna.live_market as live_market


class _NoTerminalRunner:
    def run(self, op, args, timeout):
        raise live_market.MarketUnavailable(
            "terminal_not_running", "The MetaTrader 5 terminal is not running (test isolation)."
        )

    def close(self) -> None:
        return None


def _no_terminal(*args, **kwargs):
    raise live_market.MarketUnavailable(
        "terminal_not_running", "The MetaTrader 5 terminal is not running (test isolation)."
    )


@pytest.fixture(autouse=True)
def _never_touch_a_real_mt5_terminal(monkeypatch):
    monkeypatch.setattr(live_market, "find_running_terminal", _no_terminal)
    monkeypatch.setattr(live_market, "_default_runner", lambda *args, **kwargs: _NoTerminalRunner())
