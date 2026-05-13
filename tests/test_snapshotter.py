"""Tests for the order-book snapshotter and theta/slippage gates."""

from datetime import datetime, timedelta, timezone

import pytest

from agents.portfolio_monitor import evaluate_position_gates
from config import Settings
from models import OrderBook, Position, PriceLevel
from polymarket.snapshotter import summarize_orderbook


def make_book() -> OrderBook:
    return OrderBook(
        token_id="tok-1",
        bids=[
            PriceLevel(price=0.50, size=200),
            PriceLevel(price=0.49, size=100),
            PriceLevel(price=0.45, size=50),
        ],
        asks=[
            PriceLevel(price=0.52, size=150),
            PriceLevel(price=0.53, size=80),
            PriceLevel(price=0.57, size=40),
        ],
    )


def test_summarize_orderbook_computes_midprice():
    snap = summarize_orderbook(make_book(), "cond-1")
    assert snap is not None
    assert snap["best_bid"] == pytest.approx(0.50)
    assert snap["best_ask"] == pytest.approx(0.52)
    assert snap["mid"] == pytest.approx(0.51)
    # Microprice biased by relative depth
    assert 0.50 <= snap["microprice"] <= 0.52


def test_summarize_orderbook_depth_buckets():
    snap = summarize_orderbook(make_book(), "cond-1")
    assert snap is not None
    # bid_depth_1c covers 0.50 only (0.49 is 1c below best, included)
    assert snap["bid_depth_1c"] == pytest.approx(300.0)
    # bid_depth_5c covers all three bids
    assert snap["bid_depth_5c"] == pytest.approx(350.0)
    # ask_depth_1c covers 0.52 + 0.53
    assert snap["ask_depth_1c"] == pytest.approx(230.0)


def test_summarize_orderbook_empty_book_returns_none():
    book = OrderBook(token_id="t", bids=[], asks=[])
    assert summarize_orderbook(book, "c") is None


def test_position_gate_stop_loss():
    settings = Settings(anthropic_api_key="test", stop_loss_pct=0.30)
    pos = Position(
        market_id="m",
        token_id="t",
        side="YES",
        size=100,
        avg_price=0.50,
        current_price=0.30,
    )
    gate = evaluate_position_gates(pos, settings)
    assert gate["action"] == "exit"
    assert "stop_loss" in gate["reason"]


def test_position_gate_take_profit():
    settings = Settings(anthropic_api_key="test", take_profit_pct=0.50)
    pos = Position(
        market_id="m",
        token_id="t",
        side="YES",
        size=100,
        avg_price=0.50,
        current_price=0.80,
    )
    gate = evaluate_position_gates(pos, settings)
    assert gate["action"] == "exit"
    assert "take_profit" in gate["reason"]


def test_position_gate_theta_force_close():
    settings = Settings(anthropic_api_key="test", theta_force_close_hours=2.0)
    pos = Position(
        market_id="m",
        token_id="t",
        side="YES",
        size=100,
        avg_price=0.50,
        current_price=0.55,
    )
    resolves = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    gate = evaluate_position_gates(pos, settings, market_resolves_at=resolves)
    assert gate["action"] == "exit"
    assert "theta_force_close" in gate["reason"]


def test_position_gate_theta_window_tp():
    settings = Settings(
        anthropic_api_key="test",
        theta_take_profit_pct=0.20,
        theta_window_hours=24.0,
        theta_force_close_hours=2.0,
    )
    pos = Position(
        market_id="m",
        token_id="t",
        side="YES",
        size=100,
        avg_price=0.50,
        current_price=0.65,
    )
    resolves = (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat()
    gate = evaluate_position_gates(pos, settings, market_resolves_at=resolves)
    assert gate["action"] == "exit"
    assert "theta_window_tp" in gate["reason"]


def test_position_gate_holds_when_within_bounds():
    settings = Settings(anthropic_api_key="test")
    pos = Position(
        market_id="m",
        token_id="t",
        side="YES",
        size=100,
        avg_price=0.50,
        current_price=0.55,
    )
    gate = evaluate_position_gates(pos, settings)
    assert gate["action"] == "hold"
