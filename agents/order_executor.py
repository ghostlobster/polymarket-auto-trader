"""
OrderExecutorAgent: places and manages orders on Polymarket.
"""
import asyncio
import json
from datetime import datetime

import structlog

from agents.base import BaseAgent
from config import Settings
from database import Database
from models import Order, OrderSide, OrderStatus, Signal
from polymarket.client import PolymarketClient
from tools import build_market_tools

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are the Order Executor agent for an automated Polymarket trading system.

Your job: given a trading signal and approved position size, execute the order optimally.

Execution strategy:
1. Check the live order book for current best bid/ask
2. Place a limit order 1 cent INSIDE the spread (better price than market)
   - For BUY: place at best_ask - 0.01 (slightly below ask)
   - For SELL: place at best_bid + 0.01 (slightly above bid)
3. If limit order is not filled within 60 seconds, convert to market order
4. Log all order details

Use the available tools to check orderbook and place orders.
Return a JSON summary of what happened:
{
  "action": "limit_order_placed|market_order_placed|order_filled|failed",
  "order_id": "...",
  "token_id": "...",
  "side": "BUY|SELL",
  "size_usdc": 25.0,
  "price": 0.52,
  "status": "OPEN|FILLED|FAILED",
  "notes": "Placed limit buy at 0.52, 1c inside spread"
}"""


class OrderExecutorAgent(BaseAgent):
    def __init__(self, settings: Settings, poly: PolymarketClient, db: Database):
        self._settings = settings
        self._poly = poly
        self._db = db
        tools, handlers = build_market_tools(poly)
        exec_tools = [t for t in tools if t["name"] in (
            "get_orderbook", "place_limit_order", "place_market_order", "cancel_order"
        )]
        exec_handlers = {k: v for k, v in handlers.items() if k in (
            "get_orderbook", "place_limit_order", "place_market_order", "cancel_order"
        )}
        super().__init__(
            name="OrderExecutor",
            model=self.MODEL_SONNET,
            tools=exec_tools,
            handlers=exec_handlers,
            system_prompt=SYSTEM_PROMPT,
            max_tokens=2048,
        )

    async def execute(self, signal: Signal, size_usdc: float) -> Order | None:
        """Execute a trade for the given signal. Returns the Order object."""
        if self._settings.dry_run:
            log.info("DRY RUN — skipping order placement", signal_id=signal.id, size=size_usdc)
            return None

        prompt = (
            f"Execute this trade:\n\n"
            f"**Signal**: {signal.question}\n"
            f"**Side**: {signal.side} (buy {signal.side} shares)\n"
            f"**Token ID**: {signal.token_id}\n"
            f"**Market ID**: {signal.market_id}\n"
            f"**Signal ID**: {signal.id}\n"
            f"**Size**: ${size_usdc:.2f} USDC\n"
            f"**Estimated fair value**: {signal.estimated_probability:.4f}\n\n"
            f"1. First check the live orderbook for token {signal.token_id}\n"
            f"2. Place a limit order 1 cent inside the spread\n"
            f"3. Return the execution summary JSON."
        )

        result = await self.run(prompt)
        try:
            start = result.find("{")
            end = result.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(result[start:end])
                order = Order(
                    id=data.get("order_id", ""),
                    market_id=signal.market_id,
                    signal_id=signal.id,
                    token_id=signal.token_id,
                    side=OrderSide(data.get("side", "BUY")),
                    size_usdc=size_usdc,
                    price=data.get("price", signal.market_price),
                    status=OrderStatus(data.get("status", "PENDING")),
                    placed_at=datetime.utcnow(),
                )
                await self._db.save_order(order)
                return order
        except Exception as exc:
            log.error("OrderExecutor parse error", error=str(exc))
        return None
