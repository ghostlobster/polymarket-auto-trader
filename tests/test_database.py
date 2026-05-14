"""Tests for database layer."""

import pytest
import pytest_asyncio

from database import init_db
from models import Order, OrderSide, OrderStatus, Position, Signal, SignalStrength


@pytest_asyncio.fixture
async def db(tmp_path):
    db_path = str(tmp_path / "test.db")
    database = await init_db(db_path)
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_save_and_retrieve_signal(db):
    signal = Signal(
        market_id="m1",
        question="Test question?",
        token_id="t1",
        side="YES",
        strength=SignalStrength.BUY,
        estimated_probability=0.65,
        market_price=0.50,
        edge=0.15,
        confidence=0.75,
        rationale="Test rationale",
    )
    await db.save_signal(signal)

    signals = await db.get_recent_signals(limit=5)
    assert len(signals) == 1
    assert signals[0].market_id == "m1"
    assert signals[0].edge == pytest.approx(0.15)


@pytest.mark.asyncio
async def test_save_and_retrieve_order(db):
    from datetime import datetime

    order = Order(
        id="order-001",
        market_id="m1",
        token_id="t1",
        side=OrderSide.BUY,
        size_usdc=25.0,
        price=0.52,
        status=OrderStatus.OPEN,
        placed_at=datetime.utcnow(),
    )
    await db.save_order(order)

    orders = await db.get_open_orders()
    assert len(orders) == 1
    assert orders[0].id == "order-001"


@pytest.mark.asyncio
async def test_open_positions(db):
    from datetime import datetime

    pos = Position(
        id="pos-001",
        market_id="m2",
        token_id="t2",
        side="YES",
        size=100,
        avg_price=0.48,
        current_price=0.55,
        opened_at=datetime.utcnow(),
    )
    await db.save_position(pos)

    positions = await db.get_open_positions()
    assert len(positions) == 1
    assert positions[0].id == "pos-001"


@pytest.mark.asyncio
async def test_realized_pnl_sum(db):
    from datetime import datetime

    for i, pnl in enumerate([10.0, -5.0, 20.0]):
        pos = Position(
            id=f"pos-{i}",
            market_id=f"m{i}",
            token_id=f"t{i}",
            side="YES",
            size=100,
            avg_price=0.50,
            realized_pnl=pnl,
            opened_at=datetime.utcnow(),
            closed_at=datetime.utcnow(),
        )
        await db.save_position(pos)

    total = await db.get_total_realized_pnl()
    assert total == pytest.approx(25.0)
