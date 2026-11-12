"""Session factories for MT5 worker-process tests (imported inside spawned child processes)."""

from __future__ import annotations

from pathlib import Path

from fake_mt5 import FakeMT5

from johanna.adapters.mt5 import MetaTrader5Adapter
from johanna.live_market import MT5Session


def make_fake_session(spin_seconds: float = 0.0) -> MT5Session:
    fake = FakeMT5()
    fake.spin_seconds = spin_seconds
    return MT5Session(lambda: MetaTrader5Adapter(mt5_module=fake), lambda: True)


def make_failing_session() -> MT5Session:
    raise RuntimeError("synthetic: worker could not be created")


def make_flag_session(flag_path: str) -> MT5Session:
    """Fails to start while ``flag_path`` exists; lets tests break and then heal a worker."""
    if Path(flag_path).exists():
        raise RuntimeError("synthetic: worker start failure while the flag file exists")
    return make_fake_session()


def make_offline_session() -> MT5Session:
    """A healthy worker whose MT5 terminal is not running (normal MT5 unavailability)."""
    fake = FakeMT5()
    return MT5Session(
        lambda: MetaTrader5Adapter(mt5_module=fake), lambda: False, reconnect_interval=0
    )
