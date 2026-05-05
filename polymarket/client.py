"""
Async wrapper around py-clob-client for Polymarket CLOB API.
All prices are normalised to 0.0–1.0 internally (Polymarket uses 0–1 natively).
"""
import asyncio
from datetime import datetime
from functools import partial

import structlog
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    BookParams,
    MarketOrderArgs,
    OrderArgs,
    OrderType,
)
from py_clob_client.constants import POLYGON
from py_clob_client.order_builder.constants import BUY, SELL

from config import Settings
from models import Market, Order, OrderBook, OrderSide, OrderStatus, OrderType as OT, Position, PriceLevel

log = structlog.get_logger(__name__)


class PolymarketClient:
    """Thread-safe async wrapper around the synchronous py-clob-client."""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._client: ClobClient | None = None

    def _build_client(self) -> ClobClient:
        creds = ApiCreds(
            api_key=self._settings.polymarket_api_key,
            api_secret=self._settings.polymarket_api_secret,
            api_passphrase=self._settings.polymarket_api_passphrase,
        )
        return ClobClient(
            host="https://clob.polymarket.com",
            key=self._settings.polymarket_private_key,
            chain_id=self._settings.polymarket_chain_id,
            creds=creds,
            signature_type=2,   # poly_gnosis_safe
        )

    async def _run(self, fn, *args, **kwargs):
        """Run a blocking SDK call in the thread pool executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, partial(fn, *args, **kwargs))

    @property
    def client(self) -> ClobClient:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    # ------------------------------------------------------------------ #
    #  Market data                                                         #
    # ------------------------------------------------------------------ #

    async def get_markets(self, limit: int = 100, offset: int = 0) -> list[Market]:
        raw = await self._run(self.client.get_markets, next_cursor=str(offset))
        markets = []
        for m in (raw.get("data") or []):
            try:
                tokens = m.get("tokens", [])
                yes_tok = next((t["token_id"] for t in tokens if t.get("outcome") == "Yes"), "")
                no_tok = next((t["token_id"] for t in tokens if t.get("outcome") == "No"), "")
                markets.append(Market(
                    condition_id=m["condition_id"],
                    question=m.get("question", ""),
                    category=m.get("tags", [None])[0] or "Other",
                    description=m.get("description", ""),
                    end_date_iso=m.get("end_date_iso", ""),
                    active=m.get("active", True),
                    closed=m.get("closed", False),
                    volume=float(m.get("volume", 0) or 0),
                    volume_24h=float(m.get("volume_24hr", 0) or 0),
                    liquidity=float(m.get("liquidity", 0) or 0),
                    yes_token_id=yes_tok,
                    no_token_id=no_tok,
                ))
            except Exception as exc:
                log.warning("Failed to parse market", error=str(exc), market_id=m.get("condition_id"))
        return markets

    async def get_orderbook(self, token_id: str) -> OrderBook:
        raw = await self._run(self.client.get_order_book, token_id)
        bids = [PriceLevel(price=float(b["price"]), size=float(b["size"])) for b in (raw.bids or [])]
        asks = [PriceLevel(price=float(a["price"]), size=float(a["size"])) for a in (raw.asks or [])]
        return OrderBook(token_id=token_id, bids=bids, asks=asks)

    async def get_last_trade_price(self, token_id: str) -> float:
        try:
            raw = await self._run(self.client.get_last_trade_price, token_id)
            return float(raw.get("price", 0))
        except Exception:
            return 0.0

    # ------------------------------------------------------------------ #
    #  Account / Positions                                                 #
    # ------------------------------------------------------------------ #

    async def get_positions(self) -> list[Position]:
        try:
            raw = await self._run(self.client.get_positions)
            positions = []
            for p in (raw or []):
                positions.append(Position(
                    id=p.get("id", ""),
                    market_id=p.get("condition_id", ""),
                    token_id=p.get("asset_id", ""),
                    side="YES" if p.get("outcome") == "Yes" else "NO",
                    size=float(p.get("size", 0)),
                    avg_price=float(p.get("avg_price", 0)),
                    current_price=float(p.get("cur_price", 0)),
                ))
            return positions
        except Exception as exc:
            log.warning("Failed to fetch positions", error=str(exc))
            return []

    async def get_balance_usdc(self) -> float:
        try:
            raw = await self._run(self.client.get_balance)
            return float(raw or 0)
        except Exception as exc:
            log.warning("Failed to fetch balance", error=str(exc))
            return 0.0

    # ------------------------------------------------------------------ #
    #  Orders                                                              #
    # ------------------------------------------------------------------ #

    async def place_limit_order(
        self,
        token_id: str,
        side: OrderSide,
        size_usdc: float,
        price: float,
        market_id: str = "",
        signal_id: str = "",
    ) -> Order:
        order = Order(
            market_id=market_id,
            signal_id=signal_id,
            token_id=token_id,
            side=side,
            size_usdc=size_usdc,
            price=price,
            order_type=OT.LIMIT,
            placed_at=datetime.utcnow(),
        )
        try:
            clob_side = BUY if side == OrderSide.BUY else SELL
            args = OrderArgs(
                token_id=token_id,
                price=price,
                size=size_usdc / price,  # shares = USDC / price
                side=clob_side,
            )
            resp = await self._run(self.client.create_and_post_order, args)
            order.id = resp.get("orderID", "")
            order.status = OrderStatus.OPEN
            log.info("Limit order placed", order_id=order.id, token=token_id, side=side.value, price=price)
        except Exception as exc:
            order.status = OrderStatus.FAILED
            order.error = str(exc)
            log.error("Failed to place limit order", error=str(exc))
        return order

    async def place_market_order(
        self,
        token_id: str,
        side: OrderSide,
        size_usdc: float,
        market_id: str = "",
        signal_id: str = "",
    ) -> Order:
        order = Order(
            market_id=market_id,
            signal_id=signal_id,
            token_id=token_id,
            side=side,
            size_usdc=size_usdc,
            price=0.0,
            order_type=OT.MARKET,
            placed_at=datetime.utcnow(),
        )
        try:
            clob_side = BUY if side == OrderSide.BUY else SELL
            args = MarketOrderArgs(token_id=token_id, amount=size_usdc, side=clob_side)
            resp = await self._run(self.client.create_market_order, args)
            order.id = resp.get("orderID", "")
            order.status = OrderStatus.OPEN
            log.info("Market order placed", order_id=order.id, token=token_id, side=side.value)
        except Exception as exc:
            order.status = OrderStatus.FAILED
            order.error = str(exc)
            log.error("Failed to place market order", error=str(exc))
        return order

    async def cancel_order(self, order_id: str) -> bool:
        try:
            await self._run(self.client.cancel, order_id=order_id)
            log.info("Order cancelled", order_id=order_id)
            return True
        except Exception as exc:
            log.warning("Failed to cancel order", order_id=order_id, error=str(exc))
            return False

    async def get_order_status(self, order_id: str) -> OrderStatus:
        try:
            raw = await self._run(self.client.get_order, order_id)
            status_map = {
                "OPEN": OrderStatus.OPEN,
                "MATCHED": OrderStatus.FILLED,
                "CANCELED": OrderStatus.CANCELLED,
                "UNMATCHED": OrderStatus.OPEN,
            }
            return status_map.get(raw.get("status", ""), OrderStatus.PENDING)
        except Exception:
            return OrderStatus.PENDING
