"""
ResearchAnalystAgent: researches a market using web search to assess true probability.
"""

import json

import structlog

from agents.base import BaseAgent
from config import Settings
from tools import build_web_tools

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are the Research Analyst agent for an automated Polymarket trading system.

Your job: given a prediction market question, research what the TRUE probability of the YES outcome is,
using web search and news analysis.

Research process:
1. Search for recent news directly related to the question
2. Search for base rates, historical data, or expert predictions
3. Search for any upcoming events or catalysts that could affect the outcome
4. Synthesize findings into a probability estimate

Be rigorous and skeptical. Consider:
- Source credibility
- Recency of information
- Conflicting signals
- Known biases in prediction markets (overconfidence, recency bias)

Return a structured JSON object:
{
  "question": "...",
  "estimated_probability": 0.65,
  "confidence": 0.75,
  "key_evidence": [
    "Evidence item 1",
    "Evidence item 2"
  ],
  "bull_case": "Why YES is more likely than market thinks",
  "bear_case": "Why NO is more likely than market thinks",
  "data_quality": "high|medium|low",
  "summary": "2-3 sentence research summary"
}

Use 3-5 web searches to form a thorough view. Do not guess — if you can't find good data, set confidence to 0.3."""


class ResearchAnalystAgent(BaseAgent):
    def __init__(self, settings: Settings):
        tools, handlers = build_web_tools()
        super().__init__(
            name="ResearchAnalyst",
            model=self.MODEL_SONNET,
            tools=tools,
            handlers=handlers,
            system_prompt=SYSTEM_PROMPT,
            max_tokens=4096,
        )

    async def analyze(self, market: dict) -> dict:
        """Research a market and return probability assessment."""
        question = market.get("question", "")
        category = market.get("category", "")
        end_date = market.get("end_date_iso", "")
        current_price = market.get("best_bid", market.get("last_trade_price", 0.5))

        prompt = (
            f"Research this prediction market question:\n\n"
            f"**Question**: {question}\n"
            f"**Category**: {category}\n"
            f"**Resolves**: {end_date}\n"
            f"**Current market price (YES probability)**: {current_price:.2f}\n\n"
            f"The market currently prices this at {current_price:.0%}. "
            f"Research whether this is accurate, overpriced, or underpriced. "
            f"Return your analysis as a JSON object."
        )

        result = await self.run(prompt)
        try:
            start = result.find("{")
            end = result.rfind("}") + 1
            if start >= 0 and end > start:
                return json.loads(result[start:end])
        except json.JSONDecodeError:
            log.warning("ResearchAnalyst returned non-JSON", question=question[:50])
        return {
            "question": question,
            "estimated_probability": current_price,
            "confidence": 0.2,
            "summary": "Research failed — could not parse structured output.",
            "data_quality": "low",
        }
