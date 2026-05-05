from .base import BaseAgent


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
]
