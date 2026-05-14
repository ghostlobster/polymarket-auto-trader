"""
Per-trader performance rollups.

`recompute_for_wallet` is invoked by:
  - the audit loop on a periodic basis
  - the web layer on every page load (with a throttle to prevent spam-recompute)
  - the evaluate CLI for backtests

It marks paper positions to live mid prices, recounts trades, and upserts a
fresh `copy_performance` row.
"""

from datetime import datetime, timedelta
from typing import Callable

import structlog

from database import Database
from models import CopyPerformance

log = structlog.get_logger(__name__)


# Cache of last successful recompute per (wallet, mode) to enforce throttling.
# Process-local; resets on restart, which is fine for a single-node app.
_last_recompute: dict[tuple[str, str], datetime] = {}


async def recompute_for_wallet(
    db: Database,
    wallet: str,
    mode: str = "paper",
    throttle_secs: int = 30,
    mark_to_market: Callable | None = None,
    force: bool = False,
) -> CopyPerformance:
    """
    Recompute copy_performance for one wallet+mode.

    `mark_to_market` is an async callable that refreshes paper position prices
    against the live order book — passed in to avoid a hard dep on
    PolymarketClient at this layer (and to keep this function unit-testable).

    Throttle: if we recomputed within `throttle_secs`, return the cached row.
    """
    key = (wallet, mode)
    now = datetime.utcnow()

    if not force:
        last = _last_recompute.get(key)
        if last is not None and (now - last) < timedelta(seconds=throttle_secs):
            cached = await db.get_copy_performance(wallet, mode)
            if cached is not None:
                return cached

    if mark_to_market is not None:
        try:
            await mark_to_market(wallet)
        except Exception as exc:
            log.debug("mark_to_market raised; continuing", wallet=wallet, error=str(exc))

    leader_trades = await db.get_leader_trades(wallet, limit=10000)
    trades_observed = len(leader_trades)
    expected = sum(1 for t in leader_trades if t.expected_copy)
    copied = sum(1 for t in leader_trades if t.copy_order_id)
    miss = sum(1 for t in leader_trades if t.expected_copy and not t.copy_order_id)
    audit_miss_rate = (miss / expected) if expected else 0.0
    copy_hit_rate = (copied / expected) if expected else 0.0

    realized_pnl = 0.0
    unrealized_pnl = 0.0
    win_count = 0
    loss_count = 0

    if mode in ("paper", "shadow", "backtest"):
        positions = await db.get_paper_positions(wallet=wallet)
        for p in positions:
            realized_pnl += p.realized_pnl or 0.0
            unrealized_pnl += p.unrealized_pnl or 0.0
            if (p.realized_pnl or 0) > 0:
                win_count += 1
            elif (p.realized_pnl or 0) < 0:
                loss_count += 1
    elif mode == "live":
        # Live PnL would join leader_trades with the real positions table; out of
        # scope until live promotion is exercised. Placeholder zeroes are fine.
        pass

    perf = CopyPerformance(
        wallet=wallet,
        mode=mode,
        trades_observed=trades_observed,
        trades_copied=copied,
        copy_hit_rate=round(copy_hit_rate, 4),
        audit_miss_rate=round(audit_miss_rate, 4),
        realized_pnl=round(realized_pnl, 6),
        unrealized_pnl=round(unrealized_pnl, 6),
        win_count=win_count,
        loss_count=loss_count,
        last_updated=now,
    )
    await db.upsert_copy_performance(perf)
    _last_recompute[key] = now
    return perf


def reset_throttle_cache() -> None:
    """Test helper — clear the throttle cache."""
    _last_recompute.clear()
