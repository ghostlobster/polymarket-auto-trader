"""
OrchestratorAgent: the master coordinator that runs each trading cycle.
"""
import asyncio
from datetime import datetime

import structlog

from agents.base import BaseAgent
from agents.market_scanner import MarketScannerAgent
from agents.research_analyst import ResearchAnalystAgent
from agents.signal_generator import SignalGeneratorAgent
from agents.risk_manager import RiskManagerAgent
from agents.order_executor import OrderExecutorAgent
from agents.portfolio_monitor import PortfolioMonitorAgent
from config import Settings
from database import Database
from models import AgentMessage, PortfolioSnapshot
from polymarket.client import PolymarketClient

log = structlog.get_logger(__name__)


class OrchestratorAgent:
    """
    Coordinates all specialist agents through a single trading cycle.
    Does not use the LLM directly — it orchestrates deterministically.
    """

    def __init__(self, settings: Settings, poly: PolymarketClient, db: Database):
        self._settings = settings
        self._db = db

        self.scanner = MarketScannerAgent(settings, poly)
        self.researcher = ResearchAnalystAgent(settings)
        self.signal_gen = SignalGeneratorAgent(settings)
        self.risk_mgr = RiskManagerAgent(settings)
        self.executor = OrderExecutorAgent(settings, poly, db)
        self.monitor = PortfolioMonitorAgent(settings, poly, db)

    async def run_cycle(self) -> dict:
        """
        Execute one full trading cycle:
        1. Portfolio health check (stop-losses, take-profits)
        2. Scan for opportunities
        3. Research each opportunity
        4. Generate signals
        5. Risk-assess each signal
        6. Execute approved trades
        Returns a cycle summary dict.
        """
        cycle_start = datetime.utcnow()
        log.info("Trading cycle started", timestamp=cycle_start.isoformat())
        summary = {
            "started_at": cycle_start.isoformat(),
            "opportunities_found": 0,
            "signals_generated": 0,
            "trades_approved": 0,
            "trades_executed": 0,
            "errors": [],
        }

        # Step 1: Portfolio health check
        try:
            report = await self.monitor.check()
            snapshot = await self.monitor.snapshot(report)
            log.info(
                "Portfolio checked",
                open_positions=report.get("open_positions", 0),
                health=report.get("portfolio_health"),
                unrealized_pnl=report.get("total_unrealized_pnl", 0),
                actions=report.get("actions_taken", []),
            )
            if report.get("portfolio_health") == "critical":
                log.warning("Portfolio in critical state — skipping new trades this cycle")
                return summary
        except Exception as exc:
            log.error("Portfolio check failed", error=str(exc))
            summary["errors"].append(f"Portfolio check: {exc}")
            snapshot = PortfolioSnapshot(
                total_usdc=0, available_usdc=0, snapshot_at=cycle_start
            )

        # Stop if no available capital
        available = snapshot.available_usdc
        if available < 10.0:
            log.info("Insufficient capital for new trades", available_usdc=available)
            return summary

        # Step 2: Scan markets
        try:
            opportunities = await self.scanner.scan()
            summary["opportunities_found"] = len(opportunities)
            log.info("Markets scanned", opportunities=len(opportunities))
        except Exception as exc:
            log.error("Market scan failed", error=str(exc))
            summary["errors"].append(f"Market scan: {exc}")
            return summary

        if not opportunities:
            log.info("No opportunities found this cycle")
            return summary

        # Step 3-6: Research, signal, risk, execute for each opportunity
        for opp in opportunities:
            question = opp.get("question", "Unknown")
            market_id = opp.get("condition_id", "")

            try:
                # Research
                log.info("Researching market", question=question[:60])
                research = await self.researcher.analyze(opp)

                # Signal generation
                signal = await self.signal_gen.generate(opp, research)
                if signal is None:
                    log.info("No signal generated", question=question[:60])
                    continue
                summary["signals_generated"] += 1
                await self._db.save_signal(signal)

                # Filter weak signals early
                if not signal.is_actionable:
                    log.info(
                        "Signal not actionable",
                        question=question[:60],
                        edge=signal.edge,
                        confidence=signal.confidence,
                    )
                    continue

                # Risk assessment
                risk = await self.risk_mgr.assess(signal, snapshot)
                if not risk.get("approved"):
                    log.info(
                        "Trade rejected by risk manager",
                        question=question[:60],
                        reason=risk.get("reason"),
                    )
                    continue
                summary["trades_approved"] += 1

                size_usdc = float(risk.get("size_usdc", 0))
                if size_usdc < 5.0:
                    log.info("Position size too small, skipping", size=size_usdc)
                    continue

                # Execution
                log.info(
                    "Executing trade",
                    question=question[:60],
                    side=signal.side,
                    size_usdc=size_usdc,
                    edge=signal.edge,
                )
                order = await self.executor.execute(signal, size_usdc)
                if order:
                    summary["trades_executed"] += 1

                # Log the decision chain
                msg = AgentMessage(
                    from_agent="Orchestrator",
                    to_agent="Database",
                    msg_type="trade_decision",
                    payload={
                        "signal_id": signal.id,
                        "question": question,
                        "side": signal.side,
                        "edge": signal.edge,
                        "size_usdc": size_usdc,
                        "risk_reason": risk.get("reason", ""),
                        "order_id": order.id if order else None,
                        "dry_run": self._settings.dry_run,
                    },
                )
                await self._db.save_message(msg)

                # Small delay between trades to avoid rate limits
                await asyncio.sleep(2)

            except Exception as exc:
                log.error("Error processing opportunity", question=question[:60], error=str(exc))
                summary["errors"].append(f"{question[:40]}: {exc}")

        summary["completed_at"] = datetime.utcnow().isoformat()
        log.info("Trading cycle complete", **{k: v for k, v in summary.items() if k != "errors"})
        return summary
