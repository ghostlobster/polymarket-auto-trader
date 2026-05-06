"""
Offline evaluation: replay a wallet's historical trades through PaperBroker
to estimate "what would I have made copying them?" — useful for ranking
candidate leaders before promoting any of them to paper or live.

Usage:
    python -m copy.evaluate --wallet 0xabc... --since 30d --capital 1000 \\
        --preset scaled_market

This uses leader fill prices as the assumed entry price (no historical
order-book replay is available via the public data API). The result is
written to `copy_performance` with mode='backtest' so it shows up alongside
live/paper rows in the web UI.
"""
import argparse
import asyncio
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import structlog

from config import Settings
from copytrader.strategies import apply_preset, get_preset
from database import init_db
from models import LeaderTrade, OrderSide, PaperOrder, PaperPosition
from polymarket.data_client import PolymarketDataClient

log = structlog.get_logger(__name__)


_DUR_RE = re.compile(r"^(\d+)([dhm])$")


def parse_since(s: str) -> int:
    """Parse '30d' / '12h' / '90m' to a unix-seconds cutoff."""
    m = _DUR_RE.match(s)
    if not m:
        raise SystemExit(f"--since: expected NNd|NNh|NNm, got {s!r}")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"d": timedelta(days=n), "h": timedelta(hours=n), "m": timedelta(minutes=n)}[unit]
    return int((datetime.now(timezone.utc) - delta).timestamp())


async def evaluate(wallet: str, since: str, capital: float, preset_name: str) -> None:
    settings = Settings()
    cutoff = parse_since(since)
    db = await init_db(settings.db_path)
    preset = get_preset(preset_name)

    async with PolymarketDataClient(settings.polymarket_data_api) as data:
        events = await data.get_user_activity(wallet, after_ts=cutoff, limit=500)

    log.info("Replaying events", wallet=wallet, count=len(events), preset=preset.name)

    available = capital
    open_pos: dict[str, PaperPosition] = {}    # token_id -> PaperPosition
    realized = 0.0

    for ev in events:
        # Replay-friendly: book is unknown, so skip the slippage check by passing book=None.
        decision = apply_preset(
            preset=preset,
            leader_event=ev,
            book=None,
            available_usdc=available,
            max_position_usdc=settings.max_position_usdc,
        )
        lt = LeaderTrade(
            wallet=wallet,
            tx_hash=ev["tx_hash"],
            condition_id=ev["condition_id"],
            token_id=ev["token_id"],
            side=ev["side"],
            outcome=ev["outcome"],
            size_usdc=ev["size_usdc"],
            price=ev["price"],
            observed_at=datetime.fromtimestamp(ev["timestamp"], tz=timezone.utc),
            expected_copy=decision.expected_copy,
            skip_reason=decision.reason if decision.skip else "",
            copy_mode="backtest",
        )
        await db.record_leader_trade(lt)

        if decision.skip:
            continue

        # Apply at leader's fill price as the assumed entry price (best public proxy).
        side = OrderSide(ev["side"])
        price = ev["price"] or 0.5
        shares = decision.size_usdc / price if price > 0 else 0
        if shares <= 0:
            continue

        order = PaperOrder(
            id=f"bt-{uuid4()}",
            wallet=wallet,
            market_id=ev["condition_id"],
            token_id=ev["token_id"],
            side=ev["side"],
            size_usdc=decision.size_usdc,
            price=price,
            order_type=decision.order_type.upper(),
            status="FILLED",
            placed_at=lt.observed_at,
            filled_at=lt.observed_at,
            fill_price=price,
            leader_tx_hash=ev["tx_hash"],
        )
        await db.save_paper_order(order)
        await db.update_leader_trade_copy(wallet, ev["tx_hash"], order.id, "backtest")

        if side == OrderSide.BUY:
            existing = open_pos.get(ev["token_id"])
            if existing is None:
                existing = PaperPosition(
                    id=f"btpos-{uuid4()}",
                    wallet=wallet,
                    market_id=ev["condition_id"],
                    token_id=ev["token_id"],
                    side=ev["outcome"],
                    size=shares,
                    avg_price=price,
                    current_price=price,
                    opened_at=lt.observed_at,
                )
            else:
                new_size = existing.size + shares
                existing.avg_price = (existing.avg_price * existing.size + price * shares) / new_size
                existing.size = new_size
                existing.current_price = price
            open_pos[ev["token_id"]] = existing
            available -= decision.size_usdc
            await db.save_paper_position(existing)
        else:  # SELL
            existing = open_pos.get(ev["token_id"])
            if existing is None:
                continue
            sell_shares = min(shares, existing.size)
            pnl = (price - existing.avg_price) * sell_shares
            realized += pnl
            existing.realized_pnl = round(existing.realized_pnl + pnl, 6)
            existing.size = round(existing.size - sell_shares, 8)
            existing.current_price = price
            available += decision.size_usdc + pnl
            if existing.size <= 1e-9:
                existing.closed_at = lt.observed_at
                open_pos.pop(ev["token_id"], None)
            await db.save_paper_position(existing)

    # Mark remaining open positions to last leader price (already done above).
    unrealized = sum(
        (p.current_price - p.avg_price) * p.size for p in open_pos.values()
    )

    from copytrader.performance import recompute_for_wallet
    perf = await recompute_for_wallet(db, wallet, mode="backtest", force=True)

    print()
    print(f"=== Backtest: {wallet}  preset={preset.name}  since={since}  capital=${capital:.2f} ===")
    print(f"  events observed       : {perf.trades_observed}")
    print(f"  events copied         : {perf.trades_copied}")
    print(f"  copy hit rate         : {perf.copy_hit_rate:.2%}")
    print(f"  realized PnL          : ${perf.realized_pnl:+.2f}")
    print(f"  unrealized PnL (last) : ${unrealized:+.2f}")
    print(f"  win/loss closed       : {perf.win_count}/{perf.loss_count}")
    print(f"  ending cash           : ${available:+.2f}")
    print(f"  written to copy_performance(mode='backtest')")
    await db.close()


def main() -> None:
    p = argparse.ArgumentParser(description="Backtest copy-trading a single wallet.")
    p.add_argument("--wallet", required=True)
    p.add_argument("--since", default="30d", help="e.g. 30d, 12h, 90m")
    p.add_argument("--capital", type=float, default=1000.0)
    p.add_argument("--preset", default="scaled_market",
                   choices=["mirror", "scaled_market", "scaled_limit", "conservative", "shadow"])
    args = p.parse_args()
    asyncio.run(evaluate(args.wallet, args.since, args.capital, args.preset))


if __name__ == "__main__":
    main()
