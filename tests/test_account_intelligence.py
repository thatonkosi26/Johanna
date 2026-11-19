"""Phase 3 account and broker intelligence: validation, provenance, caching (read-only).

Prices and account values are synthetic (FakeMT5), shaped like an Exness Standard Cent account.
"""

from __future__ import annotations

import json
import logging
import math
import threading
from datetime import UTC, datetime, timedelta

import pytest
from fake_mt5 import READ_ONLY_CALLS, FakeMT5

from johanna import account_intelligence as ai
from johanna.account_intelligence import AccountIntelligenceService
from johanna.adapters.mt5 import ACCOUNT_FIELDS, MetaTrader5Adapter
from johanna.live_market import LiveMarketService

SECRET_LOGIN = 12345678
SECRET_NAME = "Private Person"


def cent_symbols() -> dict[str, dict]:
    return {
        "EURUSDc": {"bid": 1.10046, "ask": 1.10054, "digits": 5, "point": 0.00001},
        "GBPUSDc": {"bid": 1.31943, "ask": 1.31953, "digits": 5, "point": 0.00001},
        "USDJPYc": {"bid": 159.119, "ask": 159.129, "digits": 3, "point": 0.001},
    }


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


def make(fake: FakeMT5 | None = None, *, running=lambda: True, clock=None, **kwargs):
    fake = fake or FakeMT5(symbols=cent_symbols())
    live = LiveMarketService(
        adapter_factory=lambda: MetaTrader5Adapter(mt5_module=fake),
        terminal_running=running,
        reconnect_interval=0,
        clock=clock,
    )
    return fake, AccountIntelligenceService(live, clock=clock, **kwargs)


def account(fake=None, **kwargs):
    _, service = make(fake, **kwargs)
    return service.summary()


def instrument(fake, pair="EURUSD"):
    _, service = make(fake)
    return service.instrument(pair)["instrument"]


def json_safe(payload) -> str:
    return json.dumps(payload, allow_nan=False)


# ---------------------------------------------------------------- account (P3.1)
def test_valid_cent_account_is_verified_with_provenance() -> None:
    fake, service = make()
    body = service.summary()
    assert body["status"] == "LIVE" and body["validation"] == "verified"
    fields = body["account"]
    assert fields["currency"]["value"] == "USC" and fields["currency"]["status"] == "verified"
    assert fields["currency"]["unit"] == {
        "kind": "cent",
        "major": "USD",
        "units_per_major": 100,
        "confirmation": "consistent",
    }
    for key in ("balance", "equity", "margin_free"):
        assert fields[key] == {
            "value": 1000.0,
            "status": "verified",
            "source": f"account_info.{key}",
            "reason": None,
            "unit": "USC",
        }
    assert fields["leverage"]["value"] == 2000
    assert fields["trade_allowed"]["value"] is True
    assert fields["trade_mode"]["value"] == "demo" and fields["trade_mode"]["raw"] == 0
    assert body["provenance"]["status"] == "established"
    assert body["provenance"]["server"] == "Exness-MT5Trial9"
    assert body["provenance"]["identity_rechecked_after_read"] is True
    assert body["retrieved_at_utc"] and body["atomic"] is False
    assert {c["check"]: c["status"] for c in body["checks"]} == {
        "equity": "consistent",
        "free_margin": "consistent",
        "margin_level": "not_applicable",
        "currency_unit": "consistent",
    }
    assert set(fake.calls) <= READ_ONLY_CALLS  # nothing but read-only MT5 calls
    json_safe(body)


def test_missing_account_currency_is_unavailable_not_defaulted() -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.account_missing.add("currency")
    body = account(fake)
    assert body["account"]["currency"]["status"] == "unavailable"
    assert body["account"]["currency"]["value"] is None
    assert body["account"]["balance"]["unit"] is None  # no currency is assumed
    assert body["validation"] == "incomplete"


@pytest.mark.parametrize("value", ["", "usd", "US", "USDT", "ZZZ", 840, None])
def test_invalid_account_currency(value) -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.account["currency"] = value
    report = account(fake)["account"]["currency"]
    assert report["status"] == ("unavailable" if value is None else "invalid")


def test_iso_account_currency_reconciles_with_factor_one() -> None:
    fake = FakeMT5(symbols={"EURUSD": {"bid": 1.1, "ask": 1.1001, "digits": 5, "point": 0.00001}})
    fake.account["currency"] = "USD"
    fake.symbols["EURUSD"]["info"] = {
        "trade_tick_value": 0.01,
        "trade_tick_value_profit": 0.01,
        "trade_tick_value_loss": 0.01,
    }
    _, service = make(fake, pairs=("EURUSD",))
    body = service.instruments()
    assert body["account_currency"]["unit"]["kind"] == "iso"
    check = next(c for c in body["instruments"][0]["checks"] if c["check"] == "tick_value")
    assert check["status"] == "consistent" and check["account_units_per_major"] == 1


def test_zero_profit_and_zero_used_margin_are_legitimate() -> None:
    body = account()
    assert body["account"]["profit"]["value"] == 0.0
    assert body["account"]["profit"]["status"] == "verified"
    assert body["account"]["margin"]["value"] == 0.0
    assert body["account"]["margin"]["status"] == "verified"
    level = body["account"]["margin_level"]
    assert level["status"] == "not_applicable" and "no margin in use" in level["reason"]


def test_margin_level_is_checked_when_margin_is_used() -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.account.update(margin=200.0, margin_free=800.0, margin_level=500.0)
    body = account(fake)
    assert body["account"]["margin_level"]["status"] == "verified"
    fake.account["margin_level"] = 450.0
    body = account(fake)
    assert body["account"]["margin_level"]["status"] == "invalid"
    assert body["validation"] == "partial"  # a non-required field failed its check


def test_missing_equity() -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.account_missing.add("equity")
    body = account(fake)
    assert body["account"]["equity"]["status"] == "unavailable"
    assert body["validation"] == "incomplete"
    assert {c["check"]: c["status"] for c in body["checks"]}["equity"] == "not_checkable"


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, "1000", True])
def test_non_finite_or_non_numeric_balance_is_invalid(value) -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.account["balance"] = value
    body = account(fake)
    assert body["account"]["balance"]["status"] == "invalid"
    assert body["account"]["balance"]["value"] is None
    assert body["validation"] == "invalid"
    json_safe(body)  # never emits NaN/Infinity


def test_equity_discrepancy_is_exposed_not_resolved() -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.account["equity"] = 990.0
    body = account(fake)
    check = next(c for c in body["checks"] if c["check"] == "equity")
    assert check["status"] == "discrepancy"
    assert (check["equity"], check["expected"]) == (990.0, 1000.0)
    for key in ("balance", "equity", "profit", "credit"):
        assert body["account"][key]["status"] == "invalid"
    assert body["validation"] == "invalid"


def test_disconnected_mt5_is_unavailable_then_cached_with_original_time() -> None:
    clock = Clock()
    fake, service = make(running=lambda: fake.running, clock=clock)
    fake.running = False
    body = service.summary()
    assert body["status"] == "UNAVAILABLE" and body["account"] is None
    assert body["validation"] == "unavailable" and body["reason"]
    fake.running = True
    first = service.summary()
    assert first["status"] == "LIVE"
    fake.running = False
    clock.now += timedelta(seconds=30)
    later = service.summary()
    assert later["status"] == "CACHED" and later["served_from_cache"] is True
    assert later["retrieved_at_utc"] == first["retrieved_at_utc"]
    assert later["age_seconds"] == 30.0


def test_terminal_not_connected_to_server() -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.terminal_connected = False
    body = account(fake)
    assert body["status"] == "UNAVAILABLE" and body["reason"] == "not_connected_to_server"


def test_sensitive_identifiers_never_leave_the_adapter(caplog) -> None:
    caplog.set_level(logging.DEBUG)
    fake, service = make()
    adapter = MetaTrader5Adapter(mt5_module=fake)
    adapter.initialize()
    raw = adapter.account_snapshot()
    assert "login" not in raw["account"] and "name" not in raw["account"]
    assert set(raw["account"]) <= set(ACCOUNT_FIELDS)
    text = json_safe(service.summary()) + json_safe(service.instruments())
    fake.running = False
    service._fetched_at -= timedelta(minutes=1)
    text += json_safe(service.summary())
    for secret in (str(SECRET_LOGIN), SECRET_NAME):
        assert secret not in text
        assert secret not in caplog.text


# ---------------------------------------------------------------- instruments (P3.2)
def test_three_supported_symbols_resolve_to_broker_symbols() -> None:
    fake, service = make()
    body = service.instruments()
    assert [(i["symbol"], i["broker_symbol"]) for i in body["instruments"]] == [
        ("EURUSD", "EURUSDc"),
        ("GBPUSD", "GBPUSDc"),
        ("USDJPY", "USDJPYc"),
    ]
    for item in body["instruments"]:
        assert item["validation"] == "verified"
        assert all(c["status"] == "consistent" for c in item["checks"])
        assert item["provenance"]["status"] == "established"
        assert item["fields"]["contract_size"]["value"] == 1000.0
        assert item["fields"]["path"]["value"].startswith("Cent\\Forex\\")
        assert item["fields"]["order_types"]["value"] == [
            "market",
            "limit",
            "stop",
            "stop_limit",
            "stop_loss",
            "take_profit",
            "close_by",
        ]
        assert item["fields"]["filling_modes"]["value"] == ["fok", "ioc"]
        assert item["fields"]["volume_limit"]["status"] == "not_applicable"
        assert item["quote"]["freshness"] == "LIVE"
    usdjpy = body["instruments"][2]["fields"]
    assert usdjpy["tick_value"]["unit"] == "USC"
    assert usdjpy["tick_value"]["value"] == pytest.approx(100 / 159.129)
    assert set(fake.calls) <= READ_ONLY_CALLS
    json_safe(body)


@pytest.mark.parametrize(
    ("mt5_field", "field"),
    [
        ("trade_contract_size", "contract_size"),
        ("trade_tick_size", "tick_size"),
        ("trade_tick_value", "tick_value"),
        ("trade_mode", "trade_mode"),
    ],
)
def test_missing_required_specification_is_unavailable(mt5_field, field) -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.symbols["EURUSDc"]["info_missing"] = [mt5_field]
    item = instrument(fake)
    assert item["fields"][field]["status"] == "unavailable"
    assert item["fields"][field]["value"] is None
    assert item["validation"] == "incomplete"


def test_missing_contract_size_makes_tick_value_uncheckable() -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.symbols["EURUSDc"]["info_missing"] = ["trade_contract_size"]
    check = next(c for c in instrument(fake)["checks"] if c["check"] == "tick_value")
    assert check["status"] == "not_checkable"


@pytest.mark.parametrize(
    ("info", "field", "validation"),
    [
        ({"volume_min": 0.0}, "volume_min", "invalid"),
        ({"volume_min": -0.01}, "volume_min", "invalid"),
        ({"volume_max": 0.001}, "volume_max", "invalid"),  # below the minimum
        ({"volume_step": 0.0}, "volume_step", "invalid"),
        ({"volume_step": 0.03}, "volume_step", "invalid"),  # minimum not a whole step
        ({"trade_contract_size": -1000.0}, "contract_size", "invalid"),
        ({"trade_tick_size": 0.0}, "tick_size", "invalid"),
        ({"point": 0.0001}, "point", "invalid"),  # not 10^-digits
        ({"currency_profit": "JPY"}, "currency_profit", "invalid"),  # not EURUSD
        ({"trade_mode": 9}, "trade_mode", "invalid"),  # unknown code
    ],
)
def test_invalid_specifications_fail_closed(info, field, validation) -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.symbols["EURUSDc"]["info"] = info
    item = instrument(fake)
    assert item["fields"][field]["status"] == "invalid"
    assert item["validation"] == validation


def test_unknown_order_flag_bits_are_invalid() -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.symbols["EURUSDc"]["info"] = {"order_mode": 255}
    assert instrument(fake)["fields"]["order_types"]["status"] == "invalid"


def test_trading_permission_is_reported_as_given() -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.symbols["EURUSDc"]["info"] = {"trade_mode": 3}
    fake.account["trade_allowed"] = False
    _, service = make(fake)
    assert service.instrument("EURUSD")["instrument"]["fields"]["trade_mode"]["value"] == (
        "close_only"
    )
    assert service.summary()["account"]["trade_allowed"]["value"] is False


def test_tick_value_in_the_wrong_unit_is_a_discrepancy() -> None:
    fake = FakeMT5(symbols=cent_symbols())
    fake.symbols["EURUSDc"]["info"] = {
        "trade_tick_value": 0.01,  # USD, not USC
        "trade_tick_value_profit": 0.01,
        "trade_tick_value_loss": 0.01,
    }
    _, service = make(fake)
    item = service.instrument("EURUSD")["instrument"]
    check = next(c for c in item["checks"] if c["check"] == "tick_value")
    assert check["status"] == "discrepancy"
    assert item["fields"]["tick_value"]["status"] == "invalid"
    unit = next(c for c in service.summary()["checks"] if c["check"] == "currency_unit")
    assert unit["status"] == "discrepancy"


def test_tick_value_without_conversion_path_is_unverified() -> None:
    fake = FakeMT5(symbols={"EURGBPc": {"bid": 0.85, "ask": 0.8501, "digits": 5, "point": 0.00001}})
    _, service = make(fake, pairs=("EURGBP",))
    item = service.instruments()["instruments"][0]
    assert item["fields"]["tick_value"]["status"] == "unverified"
    assert item["validation"] == "invalid"  # a required value could not be confirmed


def test_mismatched_broker_provenance_invalidates_everything() -> None:
    fake = FakeMT5(symbols=cent_symbols())
    original = fake.account_info
    count = {"n": 0}

    def switching():
        count["n"] += 1
        record = original()
        if count["n"] > 2:  # the identity read after the job sees another server
            record.server = "Other-Broker-Server"
        return record

    fake.account_info = switching
    _, service = make(fake)
    body = service.instruments()
    assert body["provenance"]["status"] == "not_established"
    assert "changed during the read" in body["provenance"]["reason"]
    for item in body["instruments"]:
        assert item["validation"] == "invalid"
        assert item["fields"]["contract_size"]["status"] == "invalid"
    assert service.summary()["validation"] == "invalid"


def test_zero_spread_is_valid_and_distinct_from_unavailable() -> None:
    symbols = cent_symbols()
    symbols["EURUSDc"]["ask"] = symbols["EURUSDc"]["bid"]
    fake = FakeMT5(symbols=symbols)
    quote = instrument(fake)["quote"]
    assert quote["spread_points"]["value"] == 0
    assert quote["spread_points"]["status"] == "verified"
    assert "legitimate" in quote["spread_points"]["reason"]


def test_stale_quote_is_identified() -> None:
    fake = FakeMT5(symbols=cent_symbols(), tick_age_seconds=600)
    quote = instrument(fake)["quote"]
    assert quote["freshness"] == "STALE" and quote["quote_age_seconds"] >= 600


def test_loading_symbol_has_no_quote() -> None:
    symbols = cent_symbols()
    symbols["EURUSDc"]["loading"] = True
    quote = instrument(FakeMT5(symbols=symbols))["quote"]
    assert quote["freshness"] == "UNAVAILABLE"
    assert quote["bid"]["status"] == "unavailable"


def test_symbol_not_offered_by_the_broker() -> None:
    symbols = cent_symbols()
    del symbols["GBPUSDc"]
    _, service = make(FakeMT5(symbols=symbols))
    item = service.instrument("GBPUSD")["instrument"]
    assert item["validation"] == "unavailable" and item["reason"] == "symbol_not_offered"


def test_unsupported_and_mismatched_symbols() -> None:
    fake, service = make()
    assert service.supported_pair("EURGBP") is None
    assert service.instrument("EURGBP") is None
    assert fake.calls == []  # decided without contacting MT5
    assert service.instrument("EURUSDm") is None  # not this broker's symbol for the pair
    assert service.instrument("eurusdc")["instrument"]["broker_symbol"] == "EURUSDc"


def test_one_mt5_read_serves_both_endpoints_within_the_ttl() -> None:
    clock = Clock()
    fake, service = make(clock=clock)
    service.summary()
    service.instruments()
    service.instrument("USDJPY")
    assert service.reads == 1
    clock.now += timedelta(seconds=6)
    again = service.summary()
    assert service.reads == 2 and again["served_from_cache"] is False


def test_concurrent_requests_share_one_read() -> None:
    fake, service = make()
    fake.delay = 0.05
    threads = [threading.Thread(target=service.instruments) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert service.reads == 1


def test_validation_has_no_market_analysis_dependency() -> None:
    source = open(ai.__file__, encoding="utf-8").read()
    assert "johanna.analysis" not in source and "market_analysis" not in source
    for forbidden in ("order_send", "order_check", "positions_get", "order_calc"):
        assert forbidden not in source
