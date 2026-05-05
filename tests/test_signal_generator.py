"""Tests for signal generation logic (no API calls)."""
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from models import Signal, SignalStrength


@pytest.mark.asyncio
async def test_signal_generator_no_edge():
    """When research matches market price, should return HOLD."""
    from agents.signal_generator import SignalGeneratorAgent
    from config import Settings

    settings = Settings(anthropic_api_key="test-key")
    agent = SignalGeneratorAgent(settings)

    market = {
        "condition_id": "m1",
        "question": "Will X happen?",
        "yes_token_id": "yes1",
        "no_token_id": "no1",
        "best_bid": 0.50,
    }
    research = {
        "estimated_probability": 0.51,  # tiny edge
        "confidence": 0.8,
        "summary": "Roughly 50/50",
        "bull_case": "maybe",
        "bear_case": "maybe",
        "data_quality": "medium",
    }

    mock_response = '{"market_id":"m1","question":"Will X happen?","token_id":"yes1","side":"YES","strength":"HOLD","estimated_probability":0.51,"market_price":0.50,"edge":0.01,"confidence":0.8,"rationale":"tiny edge","research_summary":"50/50"}'

    with patch.object(agent, 'run', new_callable=AsyncMock, return_value=mock_response):
        signal = await agent.generate(market, research)

    assert signal is not None
    assert signal.strength == SignalStrength.HOLD
    assert not signal.is_actionable


@pytest.mark.asyncio
async def test_signal_generator_strong_buy():
    """Large edge with high confidence should produce STRONG_BUY."""
    from agents.signal_generator import SignalGeneratorAgent
    from config import Settings

    settings = Settings(anthropic_api_key="test-key")
    agent = SignalGeneratorAgent(settings)

    market = {
        "condition_id": "m2",
        "question": "Will Y happen?",
        "yes_token_id": "yes2",
        "no_token_id": "no2",
        "best_bid": 0.40,
    }
    research = {
        "estimated_probability": 0.70,
        "confidence": 0.85,
        "summary": "Strong evidence for YES",
        "bull_case": "multiple confirming data points",
        "bear_case": "minor uncertainty",
        "data_quality": "high",
    }

    mock_response = '{"market_id":"m2","question":"Will Y happen?","token_id":"yes2","side":"YES","strength":"STRONG_BUY","estimated_probability":0.70,"market_price":0.40,"edge":0.30,"confidence":0.85,"rationale":"30% edge","research_summary":"strong"}'

    with patch.object(agent, 'run', new_callable=AsyncMock, return_value=mock_response):
        signal = await agent.generate(market, research)

    assert signal is not None
    assert signal.strength == SignalStrength.STRONG_BUY
    assert signal.is_actionable
    assert signal.edge == pytest.approx(0.30)
