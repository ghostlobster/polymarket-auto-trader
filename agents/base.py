"""
BaseAgent: wraps Anthropic Claude with tool-use loop and prompt caching.
"""

import json
from typing import Any

import anthropic
import structlog

log = structlog.get_logger(__name__)


class BaseAgent:
    """
    Base class for all trading agents.

    Manages the Claude API tool-use loop with prompt caching on the system prompt.
    Subclasses set self.system_prompt, self.model, self.tools, and self.handlers.
    """

    MODEL_SONNET = "claude-sonnet-4-6"
    MODEL_OPUS = "claude-opus-4-7"

    def __init__(
        self,
        name: str,
        model: str,
        tools: list[dict],
        handlers: dict,
        system_prompt: str,
        max_tokens: int = 4096,
        max_tool_rounds: int = 10,
    ):
        self.name = name
        self.model = model
        self.tools = tools
        self.handlers = handlers
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self.max_tool_rounds = max_tool_rounds
        self._client = anthropic.AsyncAnthropic()

    async def run(self, user_message: str, context: dict | None = None) -> str:
        """
        Run a single agent turn with full tool-use loop.
        Returns the final text response.
        Uses ephemeral prompt caching on the system prompt.
        """
        messages: list[dict] = [{"role": "user", "content": user_message}]
        if context:
            context_block = json.dumps(context, default=str, indent=2)
            messages[0]["content"] = f"<context>\n{context_block}\n</context>\n\n{user_message}"

        system = [
            {
                "type": "text",
                "text": self.system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        log.info("Agent starting", agent=self.name, model=self.model)

        for round_num in range(self.max_tool_rounds):
            response = await self._client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system,
                tools=self.tools or [],
                messages=messages,
            )

            # Accumulate assistant message
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                text = self._extract_text(response.content)
                log.info("Agent finished", agent=self.name, rounds=round_num + 1)
                return text

            if response.stop_reason != "tool_use":
                break

            # Execute tool calls
            tool_results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                handler = self.handlers.get(block.name)
                if handler is None:
                    result = json.dumps({"error": f"Unknown tool: {block.name}"})
                else:
                    try:
                        result = await handler(block.input)
                        log.debug("Tool called", agent=self.name, tool=block.name)
                    except Exception as exc:
                        result = json.dumps({"error": str(exc)})
                        log.warning("Tool error", agent=self.name, tool=block.name, error=str(exc))

                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    }
                )

            messages.append({"role": "user", "content": tool_results})

        # Fallback: extract any text from last response
        return self._extract_text(response.content)

    @staticmethod
    def _extract_text(content: list[Any]) -> str:
        parts = [b.text for b in content if hasattr(b, "text")]
        return "\n".join(parts).strip()
