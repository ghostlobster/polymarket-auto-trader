"""
OrderExecutorAgent — thesis-pipeline order execution with a slippage budget.

Before placing an order this agent fetches the live book, computes the
expected fill price against the quoted mid, and rejects fills that exceed
`settings.thesis_max_slippage`. Successful fills are logged with the pre-fill
mid and fill VWAP into `position_postmortem` so the calibration auditor can
later compute the empirical adverse-selection drift.

The LLM is only consulted for nuanced limit-order placement; the slippage
gate is deterministic.
"""

import json
from datetime import datetime
from uuid import uuid4

import structlog

from agents.base import BaseAgent
from config import Settings
from database import Database
from models import Order, OrderSide, OrderStatus, OrderType, Signal
from polymarket.client import PolymarketClient
from tools import build_market_tools

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are the Order Executor agent for an automated Polymarket trading system.

The deterministic gate has already approved the trade and the slippage budget has been checked.
You decide whether to use a limit or market order based on book depth:

  - If the book has ≥2× the order size at the best price, use limit at best_ask − 1¢ (BUY)
    or best_bid + 1¢ (SELL).
  - If thin, place a market order.

Return a single JSON object describing the order to place:
{
  "order_type": "limit|market",
  "price": 0.52,
  "rationale": "Book is thin; market order chosen."
}"""


class OrderExecutorAgent(BaseAgent):
    def __init__(self, settings: Settings, poly: PolymarketClient, db: Database):
        self._settings = settings
        self._poly = poly
        self._db = db
        tools, handlers = build_market_tools(poly)
        exec_tools = [t for t in tools if t["name"] == "get_orderbook"]
        exec_handlers = {k: v for k, v in handlers.items() if k == "get_orderbook"}
        super().__init__(
            name="OrderExecutor",
            model=self.MODEL_SONNET,
            tools=exec_tools,
            handlers=exec_handlers,
            system_prompt=SYSTEM_PROMPT,
            max_tokens=1024,
        )

    async def execute(self, signal: Signal, size_usdc: float) -> Order | None:
        """Execute a trade for the given signal. Returns the Order object."""
        if self._settings.dry_run:
            log.info("DRY RUN — skipping order placement", signal_id=signal.id, size=size_usdc)
            return None

        # 1. Fetch book + compute slippage budget
        try:
            book = await self._poly.get_orderbook(signal.token_id)
        except Exception as exc:
            log.error("Failed to fetch book; aborting", token=signal.token_id, error=str(exc))
            return None
        if not book.asks or not book.bids:
            log.warning("Empty book; refusing to fill", token=signal.token_id)
            return None

        mid = book.mid or (book.best_bid + book.best_ask) / 2.0
        side = OrderSide.BUY  # thesis pipeline only buys YES/NO shares
        ref = book.best_ask if side == OrderSide.BUY else book.best_bid
        slip = abs(ref - mid)
        if slip > self._settings.thesis_max_slippage:
            log.warning(
                "Slippage budget breached — rejecting fill",
                signal_id=signal.id,
                slippage=slip,
                budget=self._settings.thesis_max_slippage,
            )
            await self._db.save_position_postmortem(
                {
                    "position_id": signal.id,
                    "signal_id": signal.id,
                    "pre_fill_mid": mid,
                    "fill_vwap": None,
                    "quoted_slippage": slip,
                    "exit_reason": "slippage_budget_breach",
                    "recorded_at": datetime.utcnow().isoformat(),
                }
            )
            return None

        # 2. Decide order type via LLM (small prompt, cheap)
        prompt = (
            f"Trade: side={side.value} size_usdc={size_usdc:.2f}\n"
            f"Best bid={book.best_bid:.4f} ask={book.best_ask:.4f} mid={mid:.4f}\n"
            f"Top-bid size={book.bids[0].size:.2f} top-ask size={book.asks[0].size:.2f}\n"
            "Decide limit vs market."
        )
        raw = await self.run(prompt)
        decision = self._parse(raw, default_price=ref)

        order_type = OrderType.LIMIT if decision.get("order_type") == "limit" else OrderType.MARKET
        limit_price = float(decision.get("price") or ref)

        # 3. Place order
        try:
            if order_type == OrderType.LIMIT:
                placed = await self._poly.place_limit_order(
                    token_id=signal.token_id,
                    side=side,
                    size_usdc=size_usdc,
                    price=limit_price,
                    market_id=signal.market_id,
                    signal_id=signal.id,
                )
            else:
                placed = await self._poly.place_market_order(
                    token_id=signal.token_id,
                    side=side,
                    size_usdc=size_usdc,
                    market_id=signal.market_id,
                    signal_id=signal.id,
                )
        except Exception as exc:
            log.error("Order placement failed", error=str(exc))
            return None

        placed.placed_at = placed.placed_at or datetime.utcnow()
        if not placed.id:
            placed.id = str(uuid4())
        if placed.status not in (OrderStatus.OPEN, OrderStatus.FILLED, OrderStatus.PENDING):
            placed.status = OrderStatus.PENDING

        await self._db.save_order(placed)
        # Pre-fill postmortem row; the auditor / monitor fills drift_* later.
        await self._db.save_position_postmortem(
            {
                "position_id": placed.id,
                "signal_id": signal.id,
                "pre_fill_mid": mid,
                "fill_vwap": placed.fill_price or placed.price,
                "quoted_slippage": slip,
                "exit_reason": "",
                "recorded_at": datetime.utcnow().isoformat(),
            }
        )
        return placed

    def _parse(self, raw: str, default_price: float) -> dict:
        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(raw[start:end])
        except (json.JSONDecodeError, ValueError):
            pass
        return {"order_type": "limit", "price": default_price, "rationale": "fallback"}
