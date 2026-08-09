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
        fallback_models: list[str] | None = None,
    ):
        self.name = name
        self.model = model
        self.fallback_models = fallback_models or []
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
        Uses ephemeral prompt caching on the system prompt and supports fallback models.
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

        candidate_models = [self.model] + [m for m in self.fallback_models if m != self.model]
        log.info("Agent starting", agent=self.name, primary_model=self.model, candidates=candidate_models)

        last_error = None
        for round_num in range(self.max_tool_rounds):
            response = None
            for model_name in candidate_models:
                try:
                    response = await self._client.messages.create(
                        model=model_name,
                        max_tokens=self.max_tokens,
                        system=system,  # type: ignore[arg-type]
                        tools=self.tools or [],  # type: ignore[arg-type]
                        messages=messages,  # type: ignore[arg-type]
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    log.warning("Model call failed; trying fallback", agent=self.name, model=model_name, error=str(exc))

            if response is None:
                if last_error:
                    raise last_error
                raise RuntimeError("No model candidates succeeded")

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
