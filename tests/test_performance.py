"""Performance recompute + throttle tests."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio

from copytrader.performance import recompute_for_wallet, reset_throttle_cache
from database import init_db
from models import LeaderTrade, PaperOrder, PaperPosition, TrackedTrader


@pytest_asyncio.fixture
async def db(tmp_path):
    database = await init_db(str(tmp_path / "test.db"))
    reset_throttle_cache()
    yield database
    await database.close()


async def _seed(db, wallet="0xL"):
    await db.upsert_tracked_trader(TrackedTrader(
        wallet=wallet, status="paper", preset="scaled_market",
        score=70.0, sample_size=20,
    ))


@pytest.mark.asyncio
async def test_recompute_counts_trades_and_pnl(db):
    await _seed(db)
    now = datetime.now(timezone.utc)

    # Two leader trades: one copied, one expected miss
    await db.record_leader_trade(LeaderTrade(
        wallet="0xL", tx_hash="tx1", condition_id="c", token_id="t",
        side="BUY", outcome="YES", size_usdc=10, price=0.5,
        observed_at=now, expected_copy=True, copy_order_id="paper-1",
        copy_mode="paper",
    ))
    await db.record_leader_trade(LeaderTrade(
        wallet="0xL", tx_hash="tx2", condition_id="c", token_id="t",
        side="BUY", outcome="YES", size_usdc=10, price=0.5,
        observed_at=now, expected_copy=True, copy_order_id="",
        copy_mode="paper",
    ))

    # A closed paper position with realized profit
    await db.save_paper_position(PaperPosition(
        id=str(uuid4()), wallet="0xL", market_id="c", token_id="t", side="YES",
        size=0.0, avg_price=0.5, current_price=0.6, realized_pnl=2.0,
        opened_at=now, closed_at=now,
    ))

    perf = await recompute_for_wallet(db, "0xL", mode="paper", force=True)
    assert perf.trades_observed == 2
    assert perf.trades_copied == 1
    assert perf.copy_hit_rate == pytest.approx(0.5)
    assert perf.audit_miss_rate == pytest.approx(0.5)
    assert perf.realized_pnl == pytest.approx(2.0)
    assert perf.win_count == 1


@pytest.mark.asyncio
async def test_throttle_returns_cached_until_window_expires(db):
    await _seed(db)
    now = datetime.now(timezone.utc)
    await db.record_leader_trade(LeaderTrade(
        wallet="0xL", tx_hash="tx1", condition_id="c", token_id="t",
        side="BUY", outcome="YES", size_usdc=10, price=0.5,
        observed_at=now, expected_copy=True, copy_order_id="paper-1",
        copy_mode="paper",
    ))

    p1 = await recompute_for_wallet(db, "0xL", mode="paper", throttle_secs=60, force=True)

    # Add another trade after the first force-recompute
    await db.record_leader_trade(LeaderTrade(
        wallet="0xL", tx_hash="tx2", condition_id="c", token_id="t",
        side="BUY", outcome="YES", size_usdc=10, price=0.5,
        observed_at=now, expected_copy=True, copy_order_id="paper-2",
        copy_mode="paper",
    ))

    p2 = await recompute_for_wallet(db, "0xL", mode="paper", throttle_secs=60)
    # Throttled — returns the cached row, still showing 1 trade
    assert p2.trades_observed == p1.trades_observed == 1

    # Force bypass throttle
    p3 = await recompute_for_wallet(db, "0xL", mode="paper", throttle_secs=60, force=True)
    assert p3.trades_observed == 2
