"""
SignalGeneratorAgent: synthesizes research into a structured trading signal.
"""
import json

import structlog

from agents.base import BaseAgent
from config import Settings
from models import Signal, SignalStrength

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are the Signal Generator agent for an automated Polymarket trading system.

You receive market data and research analysis, then produce a structured trading signal.

Signal logic:
- **edge** = estimated_probability - market_price (positive = YES underpriced, negative = NO underpriced)
- Only generate actionable signals when |edge| >= 0.05 (5 cents or more)
- Strength mapping:
  - |edge| >= 0.15 and confidence >= 0.7 → STRONG_BUY (if positive) or STRONG_SELL
  - |edge| >= 0.05 and confidence >= 0.6 → BUY or SELL
  - otherwise → HOLD
- "side" = "YES" if edge > 0 (buy YES shares), "NO" if edge < 0 (buy NO shares)

Return ONLY valid JSON, no markdown:
{
  "market_id": "...",
  "question": "...",
  "token_id": "...",
  "side": "YES|NO",
  "strength": "STRONG_BUY|BUY|HOLD|SELL|STRONG_SELL",
  "estimated_probability": 0.72,
  "market_price": 0.55,
  "edge": 0.17,
  "confidence": 0.75,
  "rationale": "One paragraph explaining the trading thesis",
  "research_summary": "Brief summary of key evidence"
}"""


class SignalGeneratorAgent(BaseAgent):
    def __init__(self, settings: Settings):
        # No external tools — pure reasoning
        super().__init__(
            name="SignalGenerator",
            model=self.MODEL_SONNET,
            tools=[],
            handlers={},
            system_prompt=SYSTEM_PROMPT,
            max_tokens=2048,
        )

    async def generate(self, market: dict, research: dict) -> Signal | None:
        """Generate a Signal from market + research data."""
        market_price = market.get("best_bid", market.get("last_trade_price", 0.5))
        estimated_prob = research.get("estimated_probability", market_price)
        confidence = research.get("confidence", 0.3)

        prompt = (
            f"Generate a trading signal for this prediction market.\n\n"
            f"**Market**: {market.get('question', '')}\n"
            f"**Market ID**: {market.get('condition_id', '')}\n"
            f"**YES token ID**: {market.get('yes_token_id', '')}\n"
            f"**NO token ID**: {market.get('no_token_id', '')}\n"
            f"**Current market price (YES)**: {market_price:.4f}\n\n"
            f"**Research findings**:\n"
            f"- Estimated true probability: {estimated_prob:.4f}\n"
            f"- Confidence in estimate: {confidence:.2f}\n"
            f"- Summary: {research.get('summary', 'N/A')}\n"
            f"- Bull case: {research.get('bull_case', 'N/A')}\n"
            f"- Bear case: {research.get('bear_case', 'N/A')}\n"
            f"- Data quality: {research.get('data_quality', 'low')}\n\n"
            f"Compute the edge and generate the signal JSON."
        )

        result = await self.run(prompt)
        try:
            start = result.find("{")
            end = result.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(result[start:end])
                signal = Signal(
                    market_id=data.get("market_id", market.get("condition_id", "")),
                    question=data.get("question", market.get("question", "")),
                    token_id=data.get("token_id", market.get("yes_token_id", "")),
                    side=data.get("side", "YES"),
                    strength=SignalStrength(data.get("strength", "HOLD")),
                    estimated_probability=float(data.get("estimated_probability", estimated_prob)),
                    market_price=float(data.get("market_price", market_price)),
                    edge=float(data.get("edge", estimated_prob - market_price)),
                    confidence=float(data.get("confidence", confidence)),
                    rationale=data.get("rationale", ""),
                    research_summary=data.get("research_summary", ""),
                )
                return signal
        except Exception as exc:
            log.warning("SignalGenerator parse error", error=str(exc), raw=result[:200])
        return None
