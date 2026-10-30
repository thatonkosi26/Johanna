from __future__ import annotations

import json
from collections import namedtuple
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from johanna.adapters.base import MT5AdapterError
from johanna.adapters.mt5 import MetaTrader5Adapter, metadata_path
from johanna.mt5_smoke import run_smoke

Tick = namedtuple("Tick", "bid ask last volume time time_msc flags volume_real")
Rate = namedtuple("Rate", "time open high low close tick_volume spread real_volume")
Symbol = namedtuple(
    "Symbol",
    "name description path currency_base currency_profit currency_margin digits point "
    "trade_contract_size trade_tick_size trade_tick_value trade_tick_value_profit "
    "trade_tick_value_loss volume_min volume_max volume_step volume_limit filling_mode "
    "trade_exemode trade_mode trade_calc_mode trade_stops_level trade_freeze_level "
    "spread spread_float order_mode swap_mode",
)
SymbolValues = dict(
    name="EURUSD.a",
    description="Synthetic test symbol",
    path="Forex\\Majors",
    currency_base="EUR",
    currency_profit="USD",
    currency_margin="EUR",
    digits=5,
    point=0.00001,
    trade_contract_size=100000.0,
    trade_tick_size=0.00001,
    trade_tick_value=1.0,
    trade_tick_value_profit=1.0,
    trade_tick_value_loss=1.0,
    volume_min=None,
    volume_max=100.0,
    volume_step=0.01,
    volume_limit=0.0,
    filling_mode=3,
    trade_exemode=2,
    trade_mode=4,
    trade_calc_mode=0,
    trade_stops_level=10,
    trade_freeze_level=0,
    spread=12,
    spread_float=True,
    order_mode=127,
    swap_mode=1,
)


class FakeMT5:
    TIMEFRAME_M1 = 1
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_H1 = 60
    SYMBOL_TRADE_EXECUTION_REQUEST = 0
    SYMBOL_TRADE_EXECUTION_INSTANT = 1
    SYMBOL_TRADE_EXECUTION_MARKET = 2
    SYMBOL_TRADE_EXECUTION_EXCHANGE = 3
    SYMBOL_FILLING_FOK = 1
    SYMBOL_FILLING_IOC = 2

    def __init__(self, *, initialize_ok: bool = True) -> None:
        self.initialize_ok = initialize_ok
        self.initialize_calls: list[tuple[Any, ...]] = []
        self.shutdown_calls = 0
        self.selected: list[tuple[str, bool]] = []
        self.password = "synthetic-password-must-not-be-logged"

    def initialize(self, *args: Any) -> bool:
        self.initialize_calls.append(args)
        return self.initialize_ok

    def last_error(self) -> tuple[int, str]:
        return -10004, self.password

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def terminal_info(self) -> SimpleNamespace:
        return SimpleNamespace(connected=True)

    def account_info(self) -> SimpleNamespace:
        return SimpleNamespace(server="Synthetic-Demo", login=123456, password=self.password)

    def symbols_get(self) -> tuple[SimpleNamespace, ...]:
        return (SimpleNamespace(name="EURUSD.a"), SimpleNamespace(name="USDJPY"))

    def symbol_select(self, symbol: str, selected: bool) -> bool:
        self.selected.append((symbol, selected))
        return symbol in {"EURUSD.a", "USDJPY"}

    def symbol_info_tick(self, symbol: str) -> Tick:
        assert symbol == "EURUSD.a"
        return Tick(1.1, 1.1002, 1.1001, 0, 1_735_689_600, 1_735_689_600_125, 1, 0.0)

    def copy_rates_from_pos(self, symbol: str, timeframe: int, start: int, count: int):
        assert (symbol, timeframe, start) == ("EURUSD.a", self.TIMEFRAME_M5, 1)
        return [
            Rate(1_735_689_900, 1.1, 1.2, 1.0, 1.15, 12, 10, 2),
            Rate(1_735_689_600, 1.1, 1.2, 1.0, 1.15, 11, 9, 1),
        ]

    def symbol_info(self, symbol: str) -> Symbol:
        assert symbol == "EURUSD.a"
        return Symbol(**SymbolValues)

    def version(self) -> tuple[int, int, str]:
        return 500, 1234, "synthetic build"


def connected_adapter(fake: FakeMT5 | None = None) -> tuple[MetaTrader5Adapter, FakeMT5]:
    module = fake or FakeMT5()
    adapter = MetaTrader5Adapter(mt5_module=module)
    adapter.initialize()
    return adapter, module


def test_adapter_interface_lists_and_selects_exact_broker_symbol() -> None:
    adapter, fake = connected_adapter()

    assert adapter.is_connected()
    assert adapter.list_symbols() == ["EURUSD.a", "USDJPY"]
    adapter.select_symbol("EURUSD.a")
    assert fake.selected == [("EURUSD.a", True)]
    assert not hasattr(adapter, "order_send")
    adapter.shutdown()
    assert fake.shutdown_calls == 1


def test_tick_timestamp_is_utc_and_has_no_machine_timezone_assumption() -> None:
    adapter, _ = connected_adapter()

    tick = adapter.get_tick("EURUSD.a")

    assert tick["time_utc"].tzinfo is UTC
    assert tick["time_utc"] == datetime.fromtimestamp(1_735_689_600.125, UTC)
    adapter.shutdown()


def test_m5_bars_return_completed_bars_in_utc_bar_open_order() -> None:
    adapter, _ = connected_adapter()

    bars = adapter.get_bars("EURUSD.a", "M5", 2)

    assert [bar["timestamp"] for bar in bars] == sorted(bar["timestamp"] for bar in bars)
    assert all(bar["source_timestamp_convention"] == "bar_open" for bar in bars)
    assert all(bar["tick_volume"] != bar["real_volume"] for bar in bars)
    adapter.shutdown()


def test_symbol_specification_records_unknowns_and_provenance(tmp_path: Path) -> None:
    adapter, _ = connected_adapter()

    metadata = adapter.capture_symbol_metadata("EURUSD.a")
    destination = metadata_path(tmp_path, "EURUSD.a")
    adapter.save_metadata(metadata, destination)
    saved = json.loads(destination.read_text(encoding="utf-8"))

    assert saved["provenance"]["exact_symbol_name"] == "EURUSD.a"
    assert saved["symbol_specification"]["contract_size"] == 100000.0
    assert saved["symbol_specification"]["filling_mode"] == 3
    assert saved["symbol_specification"]["filling_mode_names"] == [
        "SYMBOL_FILLING_FOK",
        "SYMBOL_FILLING_IOC",
    ]
    assert saved["symbol_specification"]["execution_mode_name"] == "SYMBOL_TRADE_EXECUTION_MARKET"
    assert saved["symbol_specification"]["minimum_volume"] is None
    assert saved["server_time"]["status"] == "unknown"
    assert saved["captured_at_utc"].endswith("Z")
    assert "synthetic-password-must-not-be-logged" not in destination.read_text(encoding="utf-8")
    adapter.shutdown()


def test_initialization_failure_is_helpful_and_does_not_expose_error_text() -> None:
    fake = FakeMT5(initialize_ok=False)
    adapter = MetaTrader5Adapter(mt5_module=fake)

    with pytest.raises(MT5AdapterError, match="[Cc]redentials are not displayed") as exc_info:
        adapter.initialize()

    assert fake.password not in str(exc_info.value)
    assert fake.shutdown_calls == 1


def test_missing_mt5_package_has_install_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    import johanna.adapters.mt5 as adapter_module

    def missing_module(name: str) -> Any:
        raise ImportError(name)

    monkeypatch.setattr(adapter_module.importlib, "import_module", missing_module)
    adapter = MetaTrader5Adapter()

    with pytest.raises(MT5AdapterError, match=r'pip install -e "\.\[mt5\]"'):
        adapter.initialize()


def test_smoke_test_reports_connection_samples_and_never_logs_password(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    adapter = MetaTrader5Adapter(mt5_module=FakeMT5())

    result = run_smoke(adapter, symbol="EURUSD.a", metadata_dir=tmp_path)
    output = capsys.readouterr().out

    assert result == 0
    assert "Terminal connected: True" in output
    assert "Account connected: True" in output
    assert "EURUSD.a" in output
    assert "sample_m5_bars" in output
    assert "synthetic-password-must-not-be-logged" not in output
    assert (tmp_path / "EURUSD.a.json").is_file()


def test_smoke_test_failure_exits_nonzero_and_shuts_down(
    capsys: pytest.CaptureFixture[str],
) -> None:
    fake = FakeMT5(initialize_ok=False)
    adapter = MetaTrader5Adapter(mt5_module=fake)

    assert run_smoke(adapter) == 2
    assert fake.shutdown_calls == 1
    assert "terminal is installed" in capsys.readouterr().err.lower()
