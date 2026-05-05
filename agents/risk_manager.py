"""
RiskManagerAgent: sizes positions using Kelly Criterion and enforces portfolio limits.
"""
import json

import structlog

from agents.base import BaseAgent
from config import Settings
from models import PortfolioSnapshot, Signal

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are the Risk Manager agent for an automated Polymarket trading system.

Your job: given a trading signal and current portfolio state, decide whether to approve the trade
and what position size (in USDC) to use.

Position sizing — fractional Kelly Criterion:
  kelly_fraction_full = edge / (1 - market_price)   [for YES bets]
  kelly_fraction_full = (-edge) / market_price       [for NO bets]
  kelly_usdc = kelly_fraction_full * available_balance * config_kelly_fraction

Hard limits (non-negotiable):
1. Max single position: config_max_position_usdc
2. Max concurrent open positions: config_max_concurrent_positions
3. Minimum trade size: $5 USDC (below this, skip)
4. Never risk more than 10% of total portfolio on one trade
5. If a very similar market is already open, reduce size by 50%

Return ONLY valid JSON:
{
  "approved": true,
  "size_usdc": 25.0,
  "reason": "Edge 12%, quarter-Kelly sizing gives $28, capped at $25 max",
  "kelly_pct": 0.08,
  "risk_score": 3
}
or
{
  "approved": false,
  "size_usdc": 0,
  "reason": "Already at max concurrent positions (5)"
}"""


class RiskManagerAgent(BaseAgent):
    def __init__(self, settings: Settings):
        self._settings = settings
        super().__init__(
            name="RiskManager",
            model=self.MODEL_SONNET,
            tools=[],
            handlers={},
            system_prompt=SYSTEM_PROMPT,
            max_tokens=1024,
        )

    async def assess(self, signal: Signal, portfolio: PortfolioSnapshot) -> dict:
        """Return risk assessment dict with approved bool and size_usdc."""
        open_count = len(portfolio.open_positions)
        available = portfolio.available_usdc

        prompt = (
            f"Assess this trading signal for risk and position sizing.\n\n"
            f"**Signal**:\n"
            f"- Market: {signal.question}\n"
            f"- Side: {signal.side}\n"
            f"- Edge: {signal.edge:+.4f}\n"
            f"- Confidence: {signal.confidence:.2f}\n"
            f"- Estimated probability: {signal.estimated_probability:.4f}\n"
            f"- Market price: {signal.market_price:.4f}\n\n"
            f"**Portfolio**:\n"
            f"- Available USDC: ${available:.2f}\n"
            f"- Total USDC: ${portfolio.total_usdc:.2f}\n"
            f"- Open positions: {open_count}\n\n"
            f"**Config limits**:\n"
            f"- Max position: ${self._settings.max_position_usdc}\n"
            f"- Max concurrent positions: {self._settings.max_concurrent_positions}\n"
            f"- Kelly fraction: {self._settings.kelly_fraction}\n\n"
            f"Apply Kelly sizing, enforce all hard limits, and return the risk assessment JSON."
        )

        result = await self.run(prompt)
        try:
            start = result.find("{")
            end = result.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(result[start:end])
        except Exception as exc:
            log.warning("RiskManager parse error", error=str(exc))

        return {"approved": False, "size_usdc": 0, "reason": "Risk assessment failed"}
