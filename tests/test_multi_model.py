"""Unit tests for BaseAgent multi-model and fallback support."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from agents.base import BaseAgent


class DummyAgent(BaseAgent):
    def __init__(self, fallback_models=None):
        super().__init__(
            name="DummyAgent",
            model="claude-sonnet-4-6",
            tools=[],
            handlers={},
            system_prompt="Test prompt",
            fallback_models=fallback_models,
        )


@pytest.mark.asyncio
async def test_base_agent_fallback_models_initialization():
    agent = DummyAgent(fallback_models=["claude-opus-4-7", "claude-haiku-3-5"])
    assert agent.model == "claude-sonnet-4-6"
    assert agent.fallback_models == ["claude-opus-4-7", "claude-haiku-3-5"]


@pytest.mark.asyncio
async def test_base_agent_primary_model_success():
    agent = DummyAgent(fallback_models=["claude-opus-4-7"])

    mock_resp = MagicMock()
    mock_resp.stop_reason = "end_turn"
    mock_resp.content = [MagicMock(text="Primary model response")]

    agent._client.messages.create = AsyncMock(return_value=mock_resp)

    res = await agent.run("Hello test")
    assert res == "Primary model response"
    assert agent._client.messages.create.call_count == 1
    assert agent._client.messages.create.call_args.kwargs["model"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_base_agent_fallback_on_primary_failure():
    agent = DummyAgent(fallback_models=["claude-opus-4-7"])

    mock_resp = MagicMock()
    mock_resp.stop_reason = "end_turn"
    mock_resp.content = [MagicMock(text="Fallback model response")]

    # Primary model fails, secondary succeeds
    agent._client.messages.create = AsyncMock(
        side_effect=[RuntimeError("Rate limit exceeded"), mock_resp]
    )

    res = await agent.run("Hello test")
    assert res == "Fallback model response"
    assert agent._client.messages.create.call_count == 2


@pytest.mark.asyncio
async def test_base_agent_raises_when_all_models_fail():
    agent = DummyAgent(fallback_models=["claude-opus-4-7"])

    agent._client.messages.create = AsyncMock(
        side_effect=[RuntimeError("Primary failed"), RuntimeError("Fallback failed")]
    )

    with pytest.raises(RuntimeError, match="Fallback failed"):
        await agent.run("Hello test")
