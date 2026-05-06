"""CopyAuditAgent tests."""
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio

from agents.copy_audit import CopyAuditAgent
from config import Settings
from copytrader.performance import reset_throttle_cache
from database import init_db
from models import LeaderTrade, TrackedTrader


@pytest_asyncio.fixture
async def db(tmp_path):
    database = await init_db(str(tmp_path / "test.db"))
    reset_throttle_cache()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_audit_logs_alerts_for_expected_misses(db, tmp_path):
    settings = Settings(
        anthropic_api_key="x",
        db_path=str(tmp_path / "x.db"),
        copy_audit_window_secs=1,
        copy_audit_miss_rate_demote=0.5,
    )
    await db.upsert_tracked_trader(TrackedTrader(
        wallet="0xL", status="paper", preset="scaled_market",
        score=70.0, sample_size=10,
    ))
    old = datetime.now(timezone.utc) - timedelta(seconds=10)
    # Five expected_copy with no order — a clear miss pattern
    for i in range(5):
        await db.record_leader_trade(LeaderTrade(
            wallet="0xL", tx_hash=f"tx{i}", condition_id="c", token_id="t",
            side="BUY", outcome="YES", size_usdc=10, price=0.5,
            observed_at=old, expected_copy=True, copy_order_id="",
            copy_mode="paper",
        ))

    audit = CopyAuditAgent(settings, db)
    summary = await audit.cycle()
    assert summary["alerts"] == 5
    alerts = await db.get_audit_alerts("0xL")
    assert len(alerts) == 5
    # Trader should auto-demote because miss rate = 100% > 50%
    demoted = await db.get_tracked_trader("0xL")
    assert demoted.status == "shadow"


@pytest.mark.asyncio
async def test_audit_no_alerts_when_all_copied(db, tmp_path):
    settings = Settings(
        anthropic_api_key="x", db_path=str(tmp_path / "x.db"),
        copy_audit_window_secs=1, copy_audit_miss_rate_demote=0.5,
    )
    await db.upsert_tracked_trader(TrackedTrader(
        wallet="0xL", status="paper", preset="scaled_market",
    ))
    old = datetime.now(timezone.utc) - timedelta(seconds=10)
    await db.record_leader_trade(LeaderTrade(
        wallet="0xL", tx_hash="tx1", condition_id="c", token_id="t",
        side="BUY", outcome="YES", size_usdc=10, price=0.5,
        observed_at=old, expected_copy=True, copy_order_id="paper-1",
        copy_mode="paper",
    ))
    audit = CopyAuditAgent(settings, db)
    summary = await audit.cycle()
    assert summary["alerts"] == 0
    assert summary["demoted"] == 0
    refreshed = await db.get_tracked_trader("0xL")
    assert refreshed.status == "paper"
