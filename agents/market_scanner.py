"""
MarketScannerAgent: scans all Polymarket markets and ranks opportunities.
"""
import json

import structlog

from agents.base import BaseAgent
from config import Settings
from models import Market
from polymarket.client import PolymarketClient
from tools import build_market_tools

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are the Market Scanner agent for an automated Polymarket trading system.

Your role is to scan all active prediction markets and identify the top trading opportunities.

For each opportunity, evaluate:
1. **Volume**: prefer markets with >$10,000 total volume (liquid enough to trade)
2. **Spread**: prefer tighter spreads (<5 cents) — wide spreads eat profit
3. **Time to resolution**: 3–30 days is ideal (long enough to research, short enough to resolve)
4. **Category diversity**: return opportunities across different categories
5. **Price range**: markets priced 10–90 cents are most tradeable (avoid near-certain outcomes)

Use the get_markets tool to fetch markets. For the most promising ones, use get_orderbook to check the live spread.

Return a JSON array of up to 5 market objects with these fields:
{
  "condition_id": "...",
  "question": "...",
  "category": "...",
  "yes_token_id": "...",
  "no_token_id": "...",
  "volume": 12345.0,
  "spread": 0.03,
  "best_bid": 0.48,
  "best_ask": 0.51,
  "end_date_iso": "...",
  "opportunity_score": 7.5,
  "reason": "High volume, tight spread, resolves in 8 days"
}

Be systematic. Fetch enough markets (try limit=100) to find good ones."""


class MarketScannerAgent(BaseAgent):
    def __init__(self, settings: Settings, poly: PolymarketClient):
        tools, handlers = build_market_tools(poly)
        # Scanner only needs read tools
        read_tools = [t for t in tools if t["name"] in ("get_markets", "get_orderbook")]
        read_handlers = {k: v for k, v in handlers.items() if k in ("get_markets", "get_orderbook")}
        super().__init__(
            name="MarketScanner",
            model=self.MODEL_SONNET,
            tools=read_tools,
            handlers=read_handlers,
            system_prompt=SYSTEM_PROMPT,
        )

    async def scan(self) -> list[dict]:
        """Scan markets and return top opportunities as a list of dicts."""
        result = await self.run(
            "Scan all active Polymarket markets. Fetch 100 markets, evaluate them using the orderbook "
            "for the most promising ones, and return the top 5 opportunities as a JSON array."
        )
        try:
            # Find JSON array in response
            start = result.find("[")
            end = result.rfind("]") + 1
            if start >= 0 and end > start:
                return json.loads(result[start:end])
        except json.JSONDecodeError:
            log.warning("MarketScanner returned non-JSON", raw=result[:200])
        return []
