"""Unit tests for TWAP order slicing engine in OrderExecutorAgent."""

from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from agents.order_executor import OrderExecutorAgent
from config import Settings
from database import init_db
from models import OrderBook, OrderSide, PriceLevel, Signal, SignalStrength


class FakePoly:
    def __init__(self, book: OrderBook):
        self._book = book

    async def get_orderbook(self, token_id: str) -> OrderBook:
        return self._book

    async def place_limit_order(self, **kwargs):
        from models import Order, OrderStatus, OrderType
        return Order(
            id="order-twap-1",
            market_id=kwargs.get("market_id", ""),
            token_id=kwargs.get("token_id", ""),
            side=kwargs.get("side", OrderSide.BUY),
            size_usdc=kwargs.get("size_usdc", 10.0),
            price=kwargs.get("price", 0.50),
            order_type=OrderType.LIMIT,
            status=OrderStatus.FILLED,
        )

    async def place_market_order(self, **kwargs):
        from models import Order, OrderStatus, OrderType
        return Order(
            id="order-twap-m",
            market_id=kwargs.get("market_id", ""),
            token_id=kwargs.get("token_id", ""),
            side=kwargs.get("side", OrderSide.BUY),
            size_usdc=kwargs.get("size_usdc", 10.0),
            price=0.50,
            order_type=OrderType.MARKET,
            status=OrderStatus.FILLED,
        )


@pytest_asyncio.fixture
async def db(tmp_path):
    database = await init_db(str(tmp_path / "test.db"))
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_twap_execution_slicing(db):
    settings = Settings(dry_run=False, thesis_max_slippage=0.10)
    book = OrderBook(
        token_id="tok1",
        bids=[PriceLevel(price=0.49, size=100)],
        asks=[PriceLevel(price=0.51, size=100)],
    )
    poly = FakePoly(book)
    executor = OrderExecutorAgent(settings, poly, db)  # type: ignore[arg-type]

    # Mock run method to simulate LLM decision
    executor.run = AsyncMock(return_value='{"order_type": "limit", "price": 0.50}')

    signal = Signal(
        market_id="m1",
        question="Will TWAP work?",
        token_id="tok1",
        side="YES",
        strength=SignalStrength.BUY,
        estimated_probability=0.60,
        market_price=0.50,
        edge=0.10,
        confidence=0.80,
        rationale="TWAP test",
    )

    orders = await executor.execute_twap(signal, size_usdc=150.0, slices=3, interval_secs=0.01)

    assert len(orders) == 3
    for o in orders:
        assert o.size_usdc == pytest.approx(50.0)
        assert o.status.value == "FILLED"


@pytest.mark.asyncio
async def test_twap_automatic_trigger_on_large_size(db):
    settings = Settings(dry_run=False, thesis_max_slippage=0.10)
    book = OrderBook(
        token_id="tok1",
        bids=[PriceLevel(price=0.49, size=100)],
        asks=[PriceLevel(price=0.51, size=100)],
    )
    poly = FakePoly(book)
    executor = OrderExecutorAgent(settings, poly, db)  # type: ignore[arg-type]
    executor.run = AsyncMock(return_value='{"order_type": "market", "price": 0.50}')

    signal = Signal(
        market_id="m1",
        question="Large order question?",
        token_id="tok1",
        side="YES",
        strength=SignalStrength.BUY,
        estimated_probability=0.70,
        market_price=0.50,
        edge=0.20,
        confidence=0.85,
        rationale="Auto TWAP test",
    )

    # Order >= $100 triggers TWAP slicing automatically
    order = await executor.execute(signal, size_usdc=120.0)
    assert order is not None
    assert order.status.value == "FILLED"
