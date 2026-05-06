"""
PaperBroker — simulates fills against the live Polymarket order book.

Mirrors the subset of `PolymarketClient` used by the executor and copy executor
so it can be swapped in by mode without touching call sites.

Fills are simulated by walking the live OrderBook returned by the wrapped
`PolymarketClient`. State is persisted to the `paper_orders` and
`paper_positions` tables (separate from live `positions` / `orders`).
"""
from datetime import datetime
from uuid import uuid4

import structlog

from database import Database
from models import OrderBook, OrderSide, PaperOrder, PaperPosition
from polymarket.client import PolymarketClient

log = structlog.get_logger(__name__)


class InsufficientLiquidityError(RuntimeError):
    pass


class PaperBroker:
    """A paper-trading broker that uses live order books to simulate fills."""

    def __init__(
        self,
        poly: PolymarketClient,
        db: Database,
        wallet_label: str,
        starting_usdc: float,
    ):
        self._poly = poly
        self._db = db
        self._wallet = wallet_label   # leader wallet being followed (used as bucket key)
        self._starting_usdc = starting_usdc

    # ------------------------------------------------------------------ #
    #  Order placement                                                    #
    # ------------------------------------------------------------------ #

    async def place_market_order(
        self,
        token_id: str,
        side: OrderSide,
        size_usdc: float,
        market_id: str = "",
        signal_id: str = "",
        leader_tx_hash: str = "",
    ) -> PaperOrder:
        book = await self._poly.get_orderbook(token_id)
        return await self._fill_against_book(
            book=book,
            token_id=token_id,
            side=side,
            size_usdc=size_usdc,
            market_id=market_id,
            signal_id=signal_id,
            limit_price=None,
            leader_tx_hash=leader_tx_hash,
            order_type="MARKET",
        )

    async def place_limit_order(
        self,
        token_id: str,
        side: OrderSide,
        size_usdc: float,
        price: float,
        market_id: str = "",
        signal_id: str = "",
        leader_tx_hash: str = "",
    ) -> PaperOrder:
        book = await self._poly.get_orderbook(token_id)
        return await self._fill_against_book(
            book=book,
            token_id=token_id,
            side=side,
            size_usdc=size_usdc,
            market_id=market_id,
            signal_id=signal_id,
            limit_price=price,
            leader_tx_hash=leader_tx_hash,
            order_type="LIMIT",
        )

    async def cancel_order(self, order_id: str) -> bool:
        # Paper orders fill or fail immediately; nothing to cancel.
        return True

    # ------------------------------------------------------------------ #
    #  Account                                                            #
    # ------------------------------------------------------------------ #

    async def get_balance_usdc(self) -> float:
        """Synthetic balance = starting cash − cost basis of open paper positions
        + realized PnL from closed paper positions (this leader's bucket only)."""
        positions = await self._db.get_paper_positions(wallet=self._wallet)
        used = 0.0
        realized = 0.0
        for p in positions:
            if p.closed_at is None:
                used += p.avg_price * p.size
            else:
                realized += p.realized_pnl
        return max(self._starting_usdc - used + realized, 0.0)

    async def get_positions(self) -> list[PaperPosition]:
        return await self._db.get_paper_positions(wallet=self._wallet, open_only=True)

    # ------------------------------------------------------------------ #
    #  Internals                                                          #
    # ------------------------------------------------------------------ #

    async def _fill_against_book(
        self,
        book: OrderBook,
        token_id: str,
        side: OrderSide,
        size_usdc: float,
        market_id: str,
        signal_id: str,
        limit_price: float | None,
        leader_tx_hash: str,
        order_type: str,
    ) -> PaperOrder:
        levels = sorted(book.asks, key=lambda lv: lv.price) if side == OrderSide.BUY \
            else sorted(book.bids, key=lambda lv: lv.price, reverse=True)

        remaining_usdc = size_usdc
        filled_shares = 0.0
        spent_usdc = 0.0
        for lv in levels:
            if limit_price is not None:
                if side == OrderSide.BUY and lv.price > limit_price:
                    break
                if side == OrderSide.SELL and lv.price < limit_price:
                    break
            level_capacity_usdc = lv.price * lv.size
            take_usdc = min(remaining_usdc, level_capacity_usdc)
            if take_usdc <= 0:
                continue
            shares = take_usdc / lv.price
            filled_shares += shares
            spent_usdc += take_usdc
            remaining_usdc -= take_usdc
            if remaining_usdc <= 1e-9:
                break

        order = PaperOrder(
            wallet=self._wallet,
            market_id=market_id,
            signal_id=signal_id,
            token_id=token_id,
            side=side.value,
            size_usdc=spent_usdc,
            price=(spent_usdc / filled_shares) if filled_shares > 0 else 0.0,
            order_type=order_type,
            placed_at=datetime.utcnow(),
            leader_tx_hash=leader_tx_hash,
        )

        if filled_shares <= 0:
            order.status = "FAILED"
            order.error = "no_fill_within_limit" if limit_price is not None else "empty_book"
            await self._db.save_paper_order(order)
            return order

        order.status = "FILLED"
        order.fill_price = order.price
        order.filled_at = datetime.utcnow()
        await self._db.save_paper_order(order)

        await self._update_paper_position(
            market_id=market_id,
            token_id=token_id,
            side=side,
            shares=filled_shares,
            price=order.price,
        )
        return order

    async def _update_paper_position(
        self,
        market_id: str,
        token_id: str,
        side: OrderSide,
        shares: float,
        price: float,
    ) -> None:
        existing = await self._db.get_paper_position_for_market(self._wallet, token_id)

        if side == OrderSide.BUY:
            if existing is None:
                pos = PaperPosition(
                    id=f"paperpos-{uuid4()}",
                    wallet=self._wallet,
                    market_id=market_id,
                    token_id=token_id,
                    side="YES",  # caller picks which token; side at the position level is positional
                    size=shares,
                    avg_price=price,
                    current_price=price,
                    opened_at=datetime.utcnow(),
                )
            else:
                new_size = existing.size + shares
                pos = existing
                pos.avg_price = (existing.avg_price * existing.size + price * shares) / new_size
                pos.size = new_size
                pos.current_price = price
            pos.update_pnl()
            await self._db.save_paper_position(pos)
            return

        # SELL: reduce or close
        if existing is None:
            log.warning(
                "Paper sell with no open position — skipped",
                wallet=self._wallet, token_id=token_id,
            )
            return

        sell_shares = min(shares, existing.size)
        realized = (price - existing.avg_price) * sell_shares
        existing.realized_pnl = round(existing.realized_pnl + realized, 6)
        existing.size = round(existing.size - sell_shares, 8)
        existing.current_price = price
        if existing.size <= 1e-9:
            existing.size = 0.0
            existing.closed_at = datetime.utcnow()
            existing.unrealized_pnl = 0.0
        else:
            existing.update_pnl()
        await self._db.save_paper_position(existing)

    # ------------------------------------------------------------------ #
    #  Mark-to-market                                                     #
    # ------------------------------------------------------------------ #

    async def mark_to_market(self) -> None:
        """Refresh current_price on every open paper position from live mid."""
        positions = await self._db.get_paper_positions(wallet=self._wallet, open_only=True)
        for p in positions:
            try:
                book = await self._poly.get_orderbook(p.token_id)
                mid = book.mid or p.current_price
                p.current_price = mid
                p.update_pnl()
                await self._db.save_paper_position(p)
            except Exception as exc:
                log.debug("MTM failed", token_id=p.token_id, error=str(exc))
