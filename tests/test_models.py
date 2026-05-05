"""Tests for Pydantic data models."""
from datetime import datetime

import pytest

from models import (
    AgentMessage,
    Market,
    Order,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Signal,
    SignalStrength,
)
from models.market import OrderBook, PriceLevel


def test_orderbook_derived_fields():
    book = OrderBook(
        token_id="abc",
        bids=[PriceLevel(price=0.48, size=100), PriceLevel(price=0.47, size=50)],
        asks=[PriceLevel(price=0.52, size=80), PriceLevel(price=0.53, size=40)],
    )
    assert book.best_bid == 0.48
    assert book.best_ask == 0.52
    assert book.spread == pytest.approx(0.04)
    assert book.mid == pytest.approx(0.50)


def test_orderbook_empty():
    book = OrderBook(token_id="abc")
    assert book.best_bid == 0.0
    assert book.spread == 0.0


def test_market_days_to_resolution():
    from datetime import timedelta, timezone
    future = (datetime.now(timezone.utc) + timedelta(days=10)).isoformat()
    market = Market(condition_id="x", question="Will X happen?", end_date_iso=future)
    days = market.days_to_resolution
    assert days is not None
    assert 9 < days < 11


def test_signal_edge_clamping():
    signal = Signal(
        market_id="m1",
        question="Test?",
        token_id="t1",
        side="YES",
        strength=SignalStrength.BUY,
        estimated_probability=1.5,   # should be clamped to 1.0
        market_price=-0.1,            # should be clamped to 0.0
        edge=0.1,
        confidence=2.0,              # should be clamped to 1.0
        rationale="test",
    )
    assert signal.estimated_probability == 1.0
    assert signal.market_price == 0.0
    assert signal.confidence == 1.0


def test_signal_is_actionable():
    sig = Signal(
        market_id="m1", question="Q?", token_id="t1", side="YES",
        strength=SignalStrength.BUY,
        estimated_probability=0.65, market_price=0.50, edge=0.15,
        confidence=0.75, rationale="strong signal",
    )
    assert sig.is_actionable is True

    weak = Signal(
        market_id="m1", question="Q?", token_id="t1", side="YES",
        strength=SignalStrength.HOLD,
        estimated_probability=0.52, market_price=0.50, edge=0.02,
        confidence=0.5, rationale="weak",
    )
    assert weak.is_actionable is False


def test_position_pnl():
    pos = Position(
        market_id="m1", token_id="t1", side="YES",
        size=100, avg_price=0.50, current_price=0.65,
    )
    pos.update_pnl()
    assert pos.unrealized_pnl == pytest.approx(15.0)
    assert pos.cost_basis_usdc == pytest.approx(50.0)


def test_order_defaults():
    order = Order(market_id="m1", token_id="t1", side=OrderSide.BUY, size_usdc=25.0, price=0.52)
    assert order.status == OrderStatus.PENDING
    assert order.order_type == OrderType.LIMIT


def test_agent_message_auto_id():
    msg1 = AgentMessage(from_agent="A", to_agent="B", msg_type="test", payload={})
    msg2 = AgentMessage(from_agent="A", to_agent="B", msg_type="test", payload={})
    assert msg1.id != msg2.id
