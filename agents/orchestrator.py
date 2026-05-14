"""
OrchestratorAgent: the master coordinator that runs each trading cycle.

The cognitive-arbitrage pipeline runs:
  scan → research → external augmentation → bias detector → ensemble signal
       → deterministic risk → executor.

Every signal carries its prior, posterior, ensemble-disagreement, bias-tag, and
applied shrinkage so the calibration auditor can later score each component.
"""

import asyncio
from datetime import datetime

import httpx
import structlog

from agents.market_scanner import MarketScannerAgent
from agents.order_executor import OrderExecutorAgent
from agents.portfolio_monitor import PortfolioMonitorAgent
from agents.research_analyst import ResearchAnalystAgent
from agents.risk_manager import RiskManagerAgent
from agents.signal_generator import SignalGeneratorAgent
from config import Settings
from database import Database
from models import AgentMessage, PortfolioSnapshot
from polymarket.client import PolymarketClient
from research import default_priors, detect_biases
from tools.external import (
    fetch_news_volume,
    fetch_peer_prices,
    fetch_polling_consensus,
    question_fingerprint,
    score_sentiment,
)

log = structlog.get_logger(__name__)


class OrchestratorAgent:
    """Coordinates all specialist agents. Deterministic — does not use the LLM directly."""

    def __init__(
        self,
        settings: Settings,
        poly: PolymarketClient,
        db: Database,
        http_client: httpx.AsyncClient | None = None,
    ):
        self._settings = settings
        self._db = db
        self._poly = poly
        self._http = http_client  # shared httpx client for external adapters
        self._priors = default_priors()

        self.scanner = MarketScannerAgent(settings, poly)
        self.researcher = ResearchAnalystAgent(settings)
        self.signal_gen = SignalGeneratorAgent(settings, prior_library=self._priors)
        self.risk_mgr = RiskManagerAgent(settings, db=db)
        self.executor = OrderExecutorAgent(settings, poly, db)
        self.monitor = PortfolioMonitorAgent(settings, poly, db)

    async def run_cycle(self) -> dict:
        """Execute one full trading cycle."""
        cycle_start = datetime.utcnow()
        log.info(
            "Trading cycle started",
            timestamp=cycle_start.isoformat(),
            edge_mode=self._settings.edge_mode,
        )
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
            snapshot = PortfolioSnapshot(total_usdc=0, available_usdc=0, snapshot_at=cycle_start)

        available = snapshot.available_usdc
        if available < self._settings.risk_min_trade_usdc * 2:
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

        # Step 3-6: Research, signal, risk, execute
        for opp in opportunities:
            question = opp.get("question", "Unknown")
            try:
                # Research
                log.info("Researching market", question=question[:60])
                research = await self.researcher.analyze(opp)

                # External augmentation (cross-market parity + news volume + polling)
                peer_prices, news, polling = await self._gather_externals(opp)
                peer_dict = dict(peer_prices.by_source) if peer_prices else {}
                if polling is not None:
                    peer_dict["polling"] = polling.yes_probability

                # Bias detector
                bias = await self._compute_bias(opp, peer_dict, news)

                # Persist fingerprint if we got any peers
                if peer_dict and opp.get("condition_id"):
                    await self._db.upsert_market_fingerprint(
                        condition_id=opp["condition_id"],
                        fingerprint=question_fingerprint(question),
                    )

                # Signal generation (ensemble + prior blend + bias-aware)
                signal = await self.signal_gen.generate(
                    opp,
                    research,
                    bias=bias,
                    peer_prices=peer_dict or None,
                )
                if signal is None:
                    log.info("No signal generated", question=question[:60])
                    continue
                summary["signals_generated"] += 1
                await self._db.save_signal(signal)
                await self._db.save_signal_features(
                    signal.id,
                    {
                        "research": research,
                        "peer_prices": peer_dict,
                        "news_volume_24h": news.volume_24h if news else None,
                        "hours_since_news": news.hours_since_latest if news else None,
                        "sentiment_score": score_sentiment(news.headlines) if news else None,
                        "bias_tags": bias.tags if bias else [],
                        "bias_features": bias.features if bias else {},
                        "edge_mode": self._settings.edge_mode,
                    },
                )

                if not signal.is_actionable:
                    log.info(
                        "Signal not actionable",
                        question=question[:60],
                        edge=signal.edge,
                        confidence=signal.confidence,
                    )
                    continue

                # Risk assessment (deterministic)
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
                if size_usdc < self._settings.risk_min_trade_usdc:
                    log.info("Position size too small after risk", size=size_usdc)
                    continue

                # Execution
                log.info(
                    "Executing trade",
                    question=question[:60],
                    side=signal.side,
                    size_usdc=size_usdc,
                    edge=signal.edge,
                    bias_tags=signal.bias_tags_json,
                )
                order = await self.executor.execute(signal, size_usdc)
                if order:
                    summary["trades_executed"] += 1

                # Audit trail
                msg = AgentMessage(
                    from_agent="Orchestrator",
                    to_agent="Database",
                    msg_type="trade_decision",
                    payload={
                        "signal_id": signal.id,
                        "question": question,
                        "side": signal.side,
                        "edge": signal.edge,
                        "posterior_p": signal.posterior_p,
                        "prior_p": signal.prior_p,
                        "model_disagreement": signal.model_disagreement,
                        "bias_tags": signal.bias_tags_json,
                        "applied_shrinkage": signal.applied_shrinkage,
                        "size_usdc": size_usdc,
                        "risk_reason": risk.get("reason", ""),
                        "order_id": order.id if order else None,
                        "dry_run": self._settings.dry_run,
                    },
                )
                await self._db.save_message(msg)

                await asyncio.sleep(2)

            except Exception as exc:
                log.error("Error processing opportunity", question=question[:60], error=str(exc))
                summary["errors"].append(f"{question[:40]}: {exc}")

        summary["completed_at"] = datetime.utcnow().isoformat()
        log.info(
            "Trading cycle complete",
            **{k: v for k, v in summary.items() if k != "errors"},
        )
        return summary

    # ------------------------------------------------------------------ #
    #  External augmentation                                             #
    # ------------------------------------------------------------------ #

    async def _gather_externals(self, opp: dict):
        """Run external adapters in parallel; each respects its own feature flag."""
        question = opp.get("question", "") or ""
        # Run the three external lookups concurrently — each adapter handles its own flags.
        peer_task = fetch_peer_prices(self._settings, question, http=self._http)
        news_task = fetch_news_volume(self._settings, question, http=self._http)
        poll_task = fetch_polling_consensus(self._settings, question, http=self._http)
        try:
            peer, news, polling = await asyncio.gather(
                peer_task,
                news_task,
                poll_task,
                return_exceptions=True,
            )
        except Exception as exc:
            log.debug("External gather failed", error=str(exc))
            return None, None, None
        peer = peer if not isinstance(peer, BaseException) else None
        news = news if not isinstance(news, BaseException) else None
        polling = polling if not isinstance(polling, BaseException) else None
        return peer, news, polling

    async def _compute_bias(self, opp: dict, peer_prices: dict[str, float], news):
        """Pull recent snapshots + leader trades and run the bias detector."""
        token_id = opp.get("yes_token_id") or ""
        market_price = float(opp.get("best_bid", opp.get("last_trade_price", 0.5)) or 0.5)
        try:
            snapshots = await self._db.get_recent_snapshots(token_id, limit=12)
        except Exception:
            snapshots = []
        try:
            condition_id = opp.get("condition_id", "") or ""
            leader_trades = (
                [
                    t.model_dump()
                    for t in await self._db.get_leader_trades_for_condition(
                        condition_id,
                        limit=50,
                    )
                ]
                if condition_id
                else []
            )
        except Exception:
            leader_trades = []
        try:
            calibration_buckets = await self._db.get_calibration_buckets(source="thesis")
        except Exception:
            calibration_buckets = []

        sentiment = score_sentiment(news.headlines) if news else None
        return detect_biases(
            market_price=market_price,
            snapshots=snapshots,
            calibration_buckets=calibration_buckets,
            peer_prices=peer_prices or None,
            leader_trades=leader_trades or None,
            news_volume_24h=news.volume_24h if news else None,
            hours_since_news=news.hours_since_latest if news else None,
            sentiment_score=sentiment,
        )
