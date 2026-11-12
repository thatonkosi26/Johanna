"""Phase 1 domain contracts: typed Protocols with refusing stubs, no implementations."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

import johanna.domain as domain
from johanna.application import JohannaApplication
from johanna.config import AppConfig, StorageConfig
from johanna.domain import ServiceNotImplementedError
from johanna.domain.execution import (
    DisabledExecutionService,
    ExecutionService,
    ExecutionUnavailableError,
    OrderIntent,
)
from johanna.domain.intelligence import (
    AnalysisRequest,
    IntelligenceService,
    UnimplementedIntelligenceService,
)
from johanna.domain.news import NewsService, UnimplementedNewsService
from johanna.domain.risk import RiskService, UnimplementedRiskService
from johanna.domain.rules import RulesService, UnimplementedRulesService
from johanna.domain.signals import Signal, SignalService, UnimplementedSignalService
from johanna.services import Service

NOW = datetime(2026, 11, 11, tzinfo=UTC)
INTENT = OrderIntent(
    symbol="EURUSD", side="buy", volume=Decimal("0.01"), order_type="market", origin="manual"
)
SIGNAL = Signal("EURUSD", "H1", "flat", NOW, "test", "synthetic")

CASES = [
    (UnimplementedNewsService(), NewsService, lambda s: s.latest(limit=5)),
    (
        UnimplementedIntelligenceService(),
        IntelligenceService,
        lambda s: s.analyze(AnalysisRequest("EURUSD", "H1", "synthetic?", NOW)),
    ),
    (UnimplementedSignalService(), SignalService, lambda s: s.generate("EURUSD", "H1", [])),
    (UnimplementedRulesService(), RulesService, lambda s: s.evaluate(SIGNAL)),
    (UnimplementedRiskService(), RiskService, lambda s: s.assess(INTENT)),
    (DisabledExecutionService(), ExecutionService, lambda s: s.submit(INTENT)),
]


@pytest.mark.parametrize(("stub", "protocol", "call"), CASES, ids=lambda v: getattr(v, "name", ""))
def test_stub_satisfies_contract_and_refuses(stub, protocol, call) -> None:
    assert isinstance(stub, protocol)
    stub.start()
    status = stub.status()
    assert status["implemented"] is False
    assert status["contract"].endswith(protocol.__name__)
    assert status["state"] in {"disabled", "not_implemented"}
    with pytest.raises(ServiceNotImplementedError):
        call(stub)
    stub.stop()


def test_execution_refusal_is_explicit() -> None:
    with pytest.raises(ExecutionUnavailableError, match="no order was sent"):
        DisabledExecutionService().submit(INTENT)


def test_application_registers_contract_stubs(tmp_path) -> None:
    config = replace(AppConfig.load(environ={}), storage=StorageConfig(tmp_path))
    app = JohannaApplication(config)
    app.start()
    services = app.status()["services"]
    for name in (
        "news",
        "gemini_intelligence",
        "trading_signals",
        "trading_rules",
        "risk_management",
        "broker_execution",
    ):
        assert services[name]["implemented"] is False, name
        assert services[name]["contract"].startswith("johanna.domain."), name
    assert services["broker_execution"]["state"] == "disabled"
    assert app.status()["autonomous_execution_active"] is False
    stubs = {service.name: service for service in app.services.services}
    assert isinstance(stubs["broker_execution"], ExecutionService)
    assert all(isinstance(service, Service) for service in stubs.values())
    app.stop()


def test_domain_package_imports_no_broker_or_provider_code() -> None:
    forbidden = ("MetaTrader5", "johanna.adapters", "johanna.collectors", "urllib", "http")
    package = Path(domain.__file__).parent
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith(forbidden), f"{path.name} imports {name}"
