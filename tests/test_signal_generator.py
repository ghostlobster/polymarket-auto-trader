"""Tests for the ensemble signal generator."""

from unittest.mock import AsyncMock, patch

import pytest

from agents.signal_generator import SignalGeneratorAgent
from config import Settings
from models import SignalStrength


@pytest.mark.asyncio
async def test_signal_generator_no_edge_returns_hold():
    """When all scenarios match market price, strength should be HOLD."""
    settings = Settings(anthropic_api_key="test")
    agent = SignalGeneratorAgent(settings)

    market = {
        "condition_id": "m1",
        "question": "Will X happen?",
        "yes_token_id": "yes1",
        "no_token_id": "no1",
        "best_bid": 0.50,
        "category": "Other",
    }
    research = {
        "estimated_probability": 0.51,
        "confidence": 0.8,
        "summary": "Roughly 50/50",
        "bull_case": "maybe",
        "bear_case": "maybe",
        "data_quality": "medium",
    }
    # Tight bull/base/bear bracket around the market price → ensemble posterior ≈ 0.50
    mock_response = (
        '{"bull_estimate":0.52,"bull_confidence":0.7,'
        '"base_estimate":0.51,"base_confidence":0.7,'
        '"bear_estimate":0.50,"bear_confidence":0.7,'
        '"side":"YES","rationale":"flat","research_summary":"50/50"}'
    )
    with patch.object(agent, "run", new_callable=AsyncMock, return_value=mock_response):
        signal = await agent.generate(market, research)

    assert signal is not None
    assert signal.strength == SignalStrength.HOLD
    assert not signal.is_actionable


@pytest.mark.asyncio
async def test_signal_generator_strong_buy_with_wide_bracket():
    """All scenarios above market by a wide margin → STRONG_BUY."""
    settings = Settings(anthropic_api_key="test")
    agent = SignalGeneratorAgent(settings)

    market = {
        "condition_id": "m2",
        "question": "Will Y happen?",
        "yes_token_id": "yes2",
        "no_token_id": "no2",
        "best_bid": 0.40,
        "category": "Other",
    }
    research = {
        "estimated_probability": 0.70,
        "confidence": 0.85,
        "summary": "Strong evidence for YES",
        "bull_case": "multiple confirming data points",
        "bear_case": "minor uncertainty",
        "data_quality": "high",
    }
    mock_response = (
        '{"bull_estimate":0.85,"bull_confidence":0.85,'
        '"base_estimate":0.78,"base_confidence":0.85,'
        '"bear_estimate":0.70,"bear_confidence":0.80,'
        '"side":"YES","rationale":"30+ edge","research_summary":"strong"}'
    )
    with patch.object(agent, "run", new_callable=AsyncMock, return_value=mock_response):
        signal = await agent.generate(market, research)

    assert signal is not None
    # Posterior pulls toward market via the market_implied estimator weight.
    # With weights all equal (0.25): posterior = (0.85+0.78+0.70+0.40)/4 ≈ 0.6825
    # → edge ≈ 0.2825, which is >0.15 with confidence ≥0.7 → STRONG_BUY.
    assert signal.side == "YES"
    assert signal.strength == SignalStrength.STRONG_BUY
    assert signal.is_actionable
    assert signal.edge > 0.20
    # Model disagreement is captured
    assert signal.model_disagreement > 0


@pytest.mark.asyncio
async def test_signal_generator_legacy_payload_back_compat():
    """If the LLM returns the legacy single-estimate shape, we fan it out."""
    settings = Settings(anthropic_api_key="test")
    agent = SignalGeneratorAgent(settings)

    market = {
        "condition_id": "m3",
        "question": "Will Z happen?",
        "yes_token_id": "yes3",
        "no_token_id": "no3",
        "best_bid": 0.40,
        "category": "Other",
    }
    research = {
        "estimated_probability": 0.65,
        "confidence": 0.75,
        "summary": "evidence",
        "data_quality": "medium",
    }
    legacy = (
        '{"side":"YES","estimated_probability":0.65,"confidence":0.75,'
        '"rationale":"legacy","research_summary":"legacy"}'
    )
    with patch.object(agent, "run", new_callable=AsyncMock, return_value=legacy):
        signal = await agent.generate(market, research)

    assert signal is not None
    assert signal.side == "YES"
    # estimated_probability is the blended posterior — must be >market
    assert signal.estimated_probability > market["best_bid"]


@pytest.mark.asyncio
async def test_signal_generator_persists_bias_tags_and_disagreement():
    settings = Settings(anthropic_api_key="test")
    agent = SignalGeneratorAgent(settings)
    from research import BiasReport

    bias = BiasReport(tags=["panic_cascade", "smart_money"], directional_hint=0.4)
    market = {
        "condition_id": "m4",
        "question": "Will incumbent re-elect?",
        "yes_token_id": "y",
        "no_token_id": "n",
        "best_bid": 0.45,
        "category": "Politics",
    }
    research = {
        "estimated_probability": 0.70,
        "confidence": 0.7,
        "summary": "x",
        "data_quality": "medium",
    }
    mock_response = (
        '{"bull_estimate":0.78,"bull_confidence":0.7,'
        '"base_estimate":0.70,"base_confidence":0.7,'
        '"bear_estimate":0.60,"bear_confidence":0.7,'
        '"side":"YES","rationale":"","research_summary":""}'
    )
    with patch.object(agent, "run", new_callable=AsyncMock, return_value=mock_response):
        signal = await agent.generate(market, research, bias=bias)

    import json as _json

    assert signal is not None
    assert _json.loads(signal.bias_tags_json) == ["panic_cascade", "smart_money"]
    assert signal.model_disagreement > 0
    assert signal.posterior_p is not None
    # Politics + "incumbent re-elect" → prior matched
    assert signal.prior_p is not None
