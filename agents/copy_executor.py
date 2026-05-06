"""
CopyExecutor: dispatches a copy decision to live (PolymarketClient) or paper
(PaperBroker) execution depending on the trader's status.

Returns (order_id, mode) or ("", reason) on skip / failure.
"""
from datetime import datetime

import structlog

from config import Settings
from copytrader.strategies import CopyDecision
from database import Database
from models import OrderSide, Signal, TrackedTrader
from polymarket.client import PolymarketClient
from polymarket.paper_broker import PaperBroker

log = structlog.get_logger(__name__)


class CopyExecutor:
    def __init__(self, settings: Settings, poly: PolymarketClient, db: Database):
        self._settings = settings
        self._poly = poly
        self._db = db
        # One PaperBroker per leader wallet — keeps PnL bucketed per trader.
        self._brokers: dict[str, PaperBroker] = {}

    def _broker(self, wallet: str) -> PaperBroker:
        if wallet not in self._brokers:
            self._brokers[wallet] = PaperBroker(
                poly=self._poly,
                db=self._db,
                wallet_label=wallet,
                starting_usdc=self._settings.copy_paper_starting_usdc,
            )
        return self._brokers[wallet]

    def broker(self, wallet: str) -> PaperBroker:
        """Public accessor — used by performance.recompute for mark-to-market."""
        return self._broker(wallet)

    async def execute(
        self,
        signal: Signal,
        decision: CopyDecision,
        trader: TrackedTrader,
        leader_tx_hash: str,
    ) -> tuple[str, str]:
        """Returns (order_id, mode). order_id is '' on skip/failure."""
        mode = trader.status   # shadow|paper|live

        if mode == "shadow":
            log.info("Shadow mode — no execution", wallet=trader.wallet, signal_id=signal.id)
            return "", "shadow"

        side = OrderSide.BUY if signal.side == "YES" or signal.side == "BUY" else (
            OrderSide.SELL if signal.side == "SELL" else OrderSide.BUY
        )

        if mode == "paper":
            broker = self._broker(trader.wallet)
            try:
                if decision.order_type == "market":
                    order = await broker.place_market_order(
                        token_id=signal.token_id, side=side,
                        size_usdc=decision.size_usdc, market_id=signal.market_id,
                        signal_id=signal.id, leader_tx_hash=leader_tx_hash,
                    )
                else:
                    order = await broker.place_limit_order(
                        token_id=signal.token_id, side=side,
                        size_usdc=decision.size_usdc, price=decision.limit_price or 0.0,
                        market_id=signal.market_id, signal_id=signal.id,
                        leader_tx_hash=leader_tx_hash,
                    )
            except Exception as exc:
                log.error("Paper order failed", wallet=trader.wallet, error=str(exc))
                return "", "paper"
            if order.status != "FILLED":
                return "", "paper"
            return order.id, "paper"

        if mode == "live":
            if self._settings.dry_run:
                log.info("DRY_RUN — skipping live copy order", signal_id=signal.id)
                return "", "live"
            try:
                if decision.order_type == "market":
                    order = await self._poly.place_market_order(
                        token_id=signal.token_id, side=side,
                        size_usdc=decision.size_usdc, market_id=signal.market_id,
                        signal_id=signal.id,
                    )
                else:
                    order = await self._poly.place_limit_order(
                        token_id=signal.token_id, side=side,
                        size_usdc=decision.size_usdc, price=decision.limit_price or 0.0,
                        market_id=signal.market_id, signal_id=signal.id,
                    )
            except Exception as exc:
                log.error("Live copy order failed", wallet=trader.wallet, error=str(exc))
                return "", "live"
            order.placed_at = order.placed_at or datetime.utcnow()
            await self._db.save_order(order)
            return order.id, "live"

        return "", mode
