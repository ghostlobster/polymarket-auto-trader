"""Tests for risk management logic."""
import pytest
from unittest.mock import AsyncMock, patch

from models import Signal, SignalStrength, PortfolioSnapshot


def make_signal(edge=0.12, confidence=0.75, market_price=0.50):
    return Signal(
        market_id="m1",
        question="Test market?",
        token_id="t1",
        side="YES",
        strength=SignalStrength.BUY,
        estimated_probability=market_price + edge,
        market_price=market_price,
        edge=edge,
        confidence=confidence,
        rationale="test",
    )


def make_portfolio(available=200.0, total=500.0, open_count=0):
    return PortfolioSnapshot(
        total_usdc=total,
        available_usdc=available,
        open_positions=[],
        realized_pnl=10.0,
        unrealized_pnl=-2.0,
    )


@pytest.mark.asyncio
async def test_risk_manager_approves_valid_trade():
    from agents.risk_manager import RiskManagerAgent
    from config import Settings

    settings = Settings(anthropic_api_key="test-key")
    agent = RiskManagerAgent(settings)

    signal = make_signal(edge=0.12)
    portfolio = make_portfolio(available=200.0, open_count=2)

    mock_response = '{"approved": true, "size_usdc": 24.0, "reason": "Quarter-Kelly sizing", "kelly_pct": 0.12, "risk_score": 3}'

    with patch.object(agent, 'run', new_callable=AsyncMock, return_value=mock_response):
        result = await agent.assess(signal, portfolio)

    assert result["approved"] is True
    assert result["size_usdc"] == 24.0


@pytest.mark.asyncio
async def test_risk_manager_rejects_at_max_positions():
    from agents.risk_manager import RiskManagerAgent
    from config import Settings

    settings = Settings(anthropic_api_key="test-key", max_concurrent_positions=5)
    agent = RiskManagerAgent(settings)

    signal = make_signal()
    # Simulate 5 open positions
    from models import Position
    positions = [
        Position(market_id=f"m{i}", token_id=f"t{i}", side="YES", size=10, avg_price=0.5)
        for i in range(5)
    ]
    portfolio = PortfolioSnapshot(total_usdc=500, available_usdc=100, open_positions=positions)

    mock_response = '{"approved": false, "size_usdc": 0, "reason": "Already at max concurrent positions (5)"}'

    with patch.object(agent, 'run', new_callable=AsyncMock, return_value=mock_response):
        result = await agent.assess(signal, portfolio)

    assert result["approved"] is False
