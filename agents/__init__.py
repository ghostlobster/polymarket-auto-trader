from typing import TYPE_CHECKING

from .base import BaseAgent

if TYPE_CHECKING:
    from .calibration_auditor import CalibrationAuditor
    from .copy_audit import CopyAuditAgent
    from .copy_executor import CopyExecutor
    from .copy_trader import CopyTraderAgent
    from .market_scanner import MarketScannerAgent
    from .orchestrator import OrchestratorAgent
    from .order_executor import OrderExecutorAgent
    from .portfolio_monitor import PortfolioMonitorAgent
    from .research_analyst import ResearchAnalystAgent
    from .risk_manager import RiskManagerAgent
    from .signal_generator import SignalGeneratorAgent
    from .trader_discovery import TraderDiscoveryAgent


def __getattr__(name):
    """Lazy-load agents only when accessed, avoiding heavy transitive imports in tests."""
    _map = {
        "MarketScannerAgent": ".market_scanner",
        "ResearchAnalystAgent": ".research_analyst",
        "SignalGeneratorAgent": ".signal_generator",
        "RiskManagerAgent": ".risk_manager",
        "OrderExecutorAgent": ".order_executor",
        "PortfolioMonitorAgent": ".portfolio_monitor",
        "OrchestratorAgent": ".orchestrator",
        "CalibrationAuditor": ".calibration_auditor",
        "TraderDiscoveryAgent": ".trader_discovery",
        "CopyTraderAgent": ".copy_trader",
        "CopyExecutor": ".copy_executor",
        "CopyAuditAgent": ".copy_audit",
    }
    if name in _map:
        import importlib

        module = importlib.import_module(_map[name], package=__name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BaseAgent",
    "MarketScannerAgent",
    "ResearchAnalystAgent",
    "SignalGeneratorAgent",
    "RiskManagerAgent",
    "OrderExecutorAgent",
    "PortfolioMonitorAgent",
    "OrchestratorAgent",
    "CalibrationAuditor",
    "TraderDiscoveryAgent",
    "CopyTraderAgent",
    "CopyExecutor",
    "CopyAuditAgent",
]
