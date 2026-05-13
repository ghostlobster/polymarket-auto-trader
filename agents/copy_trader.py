"""
CopyTraderAgent: polls each tracked trader's activity and emits copy signals.

Pure async — no LLM in the hot path so latency stays low. Per cycle:
  1. Pull all active tracked traders.
  2. For each, fetch new TRADE events via PolymarketDataClient since last_seen_ts.
  3. For each event:
       - Insert a leader_trades row (dedupe by (wallet, tx_hash)).
       - Apply the trader's strategy preset (size, slippage, age, resolution gate).
       - On skip: write skip_reason.
       - On accept: build a Signal(source='copy') and dispatch to CopyExecutor.
  4. Advance last_seen_ts.

The orchestrator's RiskManagerAgent is reused to enforce portfolio-level
hard limits (max concurrent, available cash) on live copies.
"""

from datetime import datetime, timezone

import structlog

from agents.copy_executor import CopyExecutor
from agents.risk_manager import RiskManagerAgent
from config import Settings
from copytrader.strategies import CopyDecision, apply_preset, get_preset
from database import Database
from models import (
    LeaderTrade,
    PortfolioSnapshot,
    Signal,
    SignalStrength,
    TrackedTrader,
)
from polymarket.client import PolymarketClient
from polymarket.data_client import PolymarketDataClient

log = structlog.get_logger(__name__)


class CopyTraderAgent:
    def __init__(
        self,
        settings: Settings,
        data: PolymarketDataClient,
        poly: PolymarketClient,
        db: Database,
        risk: RiskManagerAgent | None = None,
    ):
        self._settings = settings
        self._data = data
        self._poly = poly
        self._db = db
        self._risk = risk
        self._executor = CopyExecutor(settings, poly, db)

    @property
    def executor(self) -> CopyExecutor:
        return self._executor

    async def cycle(self) -> dict:
        """Run one polling cycle across all active tracked traders."""
        traders = await self._db.get_active_tracked_traders()
        summary = {"traders": len(traders), "events": 0, "copied": 0, "skipped": 0}
        if not traders:
            return summary

        wallets = [t.wallet for t in traders]
        cursors = {t.wallet: t.last_seen_ts for t in traders}
        try:
            results = await self._data.get_users_activity(wallets, cursors)
        except Exception as exc:
            log.warning("Activity fetch failed", error=str(exc))
            return summary

        for trader in traders:
            events = results.get(trader.wallet, [])
            if not events:
                continue
            for ev in events:
                summary["events"] += 1
                handled = await self._handle_event(trader, ev)
                if handled:
                    summary["copied"] += 1
                else:
                    summary["skipped"] += 1
            new_ts = max(ev["timestamp"] for ev in events)
            if new_ts > trader.last_seen_ts:
                await self._db.set_trader_last_seen(trader.wallet, new_ts)

        return summary

    async def _handle_event(self, trader: TrackedTrader, ev: dict) -> bool:
        preset = get_preset(trader.preset)

        # Try to fetch the live order book — required for the slippage check
        # on BUY. If it fails (rate limit / network), we still proceed but
        # without slippage protection (the data client retries internally).
        book = None
        token_id = ev.get("token_id") or ""
        if token_id and (ev.get("side") == "BUY"):
            try:
                book = await self._poly.get_orderbook(token_id)
            except Exception as exc:
                log.debug("Orderbook fetch failed", token_id=token_id, error=str(exc))

        decision: CopyDecision = apply_preset(
            preset=preset,
            leader_event=ev,
            book=book,
            available_usdc=self._settings.copy_paper_starting_usdc,  # broker-bucketed
            max_position_usdc=self._settings.max_position_usdc,
            market_resolves_at=None,
        )

        observed_at = datetime.fromtimestamp(ev["timestamp"], tz=timezone.utc)
        lt = LeaderTrade(
            wallet=trader.wallet,
            tx_hash=ev["tx_hash"],
            condition_id=ev.get("condition_id", ""),
            token_id=token_id,
            side=ev.get("side", "BUY"),
            outcome=ev.get("outcome", "YES"),
            size_usdc=ev.get("size_usdc", 0.0),
            price=ev.get("price", 0.0),
            observed_at=observed_at,
            expected_copy=decision.expected_copy,
            skip_reason=decision.reason if decision.skip else "",
            copy_mode=trader.status,
        )
        inserted = await self._db.record_leader_trade(lt)
        if not inserted:
            return False  # already seen

        if decision.skip:
            log.info(
                "Copy skipped",
                wallet=trader.wallet,
                tx=ev["tx_hash"][:12],
                reason=decision.reason,
            )
            return False

        signal = self._build_signal(trader, ev, decision)
        await self._db.save_signal(signal)

        # Live mode: defer to RiskManager for portfolio-level gating.
        if trader.status == "live" and self._risk is not None:
            balance = await self._poly.get_balance_usdc()
            open_positions = await self._db.get_open_positions()
            snapshot = PortfolioSnapshot(
                total_usdc=balance,
                available_usdc=balance,
                open_positions=open_positions,
            )
            try:
                risk = await self._risk.assess(signal, snapshot)
                if not risk.get("approved"):
                    await self._db.update_leader_trade_copy(
                        trader.wallet, ev["tx_hash"], "", trader.status
                    )
                    log.info(
                        "Live copy rejected by risk",
                        wallet=trader.wallet,
                        reason=risk.get("reason"),
                    )
                    return False
                # Honor risk's smaller size if it shrinks ours
                rsize = float(risk.get("size_usdc", decision.size_usdc))
                decision.size_usdc = min(decision.size_usdc, rsize)
            except Exception as exc:
                log.warning("Risk assessment errored on copy", error=str(exc))

        order_id, mode = await self._executor.execute(
            signal=signal,
            decision=decision,
            trader=trader,
            leader_tx_hash=ev["tx_hash"],
        )
        if order_id:
            await self._db.update_leader_trade_copy(trader.wallet, ev["tx_hash"], order_id, mode)
            log.info(
                "Copy executed",
                wallet=trader.wallet,
                mode=mode,
                size=decision.size_usdc,
                order=order_id,
            )
            return True

        # Did not produce an order (shadow / dry_run / executor failure)
        await self._db.update_leader_trade_copy(trader.wallet, ev["tx_hash"], "", mode)
        return False

    def _build_signal(self, trader: TrackedTrader, ev: dict, decision: CopyDecision) -> Signal:
        outcome = ev.get("outcome", "YES")
        # Edge is unknown for copy signals; we encode the leader's score-derived
        # confidence and the leader's fill price as market_price for traceability.
        confidence = max(0.3, min(0.95, trader.score / 100.0)) if trader.score else 0.5
        return Signal(
            market_id=ev.get("condition_id", ""),
            question=f"copy:{trader.wallet[:10]}",
            token_id=ev.get("token_id", ""),
            side=outcome if ev.get("side") == "BUY" else "SELL",
            strength=SignalStrength.BUY if ev.get("side") == "BUY" else SignalStrength.SELL,
            estimated_probability=ev.get("price", 0.5),
            market_price=ev.get("price", 0.5),
            edge=0.0,
            rationale=f"Copy of {trader.wallet} preset={trader.preset}",
            confidence=confidence,
            source="copy",
            leader_wallet=trader.wallet,
            preset=trader.preset,
        )
