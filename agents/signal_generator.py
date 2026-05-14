"""
SignalGeneratorAgent — ensemble forecasting with Bayesian prior blending.

A single LLM call produces three internal scenarios (bull / base / bear) instead
of one point estimate. These are combined in pure Python with (a) the
reference-class prior and (b) the market-implied probability as a regularizer.

The resulting posterior, model-disagreement, and bias-tag inputs are persisted
on the Signal row so the calibration auditor can later score each component.
"""

import json
from statistics import pstdev

import structlog

from agents.base import BaseAgent
from config import Settings
from models import Signal, SignalStrength
from research import BiasReport, PriorLibrary, default_priors
from research.priors import bayesian_blend

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are the Signal Generator agent for an automated Polymarket trading system.

You receive a prediction-market question, research findings, optional peer-market quotes,
and optional bias-detector hints. Produce THREE scenarios — bullish, base, bearish — and
the model will combine them with a reference-class prior and the market price.

For each scenario you supply:
  - estimate: probability of YES outcome in [0, 1]
  - confidence: your confidence in that scenario in [0, 1]

The three scenarios should bracket your best-case, expected, and worst-case readings.
A wide bull–bear gap signals high model uncertainty; a narrow gap signals conviction.

Return ONLY valid JSON, no markdown:
{
  "bull_estimate": 0.75,
  "bull_confidence": 0.55,
  "base_estimate": 0.65,
  "base_confidence": 0.7,
  "bear_estimate": 0.45,
  "bear_confidence": 0.55,
  "side": "YES|NO",
  "rationale": "Two-sentence thesis.",
  "research_summary": "Brief summary of key evidence.",
  "key_drivers": ["Driver 1", "Driver 2"]
}"""


class SignalGeneratorAgent(BaseAgent):
    def __init__(
        self,
        settings: Settings,
        prior_library: PriorLibrary | None = None,
    ):
        self._settings = settings
        self._priors = prior_library or default_priors()
        super().__init__(
            name="SignalGenerator",
            model=self.MODEL_SONNET,
            tools=[],
            handlers={},
            system_prompt=SYSTEM_PROMPT,
            max_tokens=2048,
        )

    async def generate(
        self,
        market: dict,
        research: dict,
        *,
        bias: BiasReport | None = None,
        peer_prices: dict[str, float] | None = None,
    ) -> Signal | None:
        """Produce a Signal blending LLM scenarios + prior + market + bias."""
        market_price = float(market.get("best_bid", market.get("last_trade_price", 0.5)) or 0.5)
        question = market.get("question", "") or ""
        category = market.get("category", "") or ""

        prior = self._priors.lookup(category, question)

        peer_block = ""
        if peer_prices:
            peer_block = "\n**Peer market quotes (YES probability)**:\n" + "\n".join(
                f"- {k}: {v:.3f}" for k, v in peer_prices.items()
            )
        bias_block = ""
        if bias and bias.tags:
            bias_block = (
                f"\n**Bias detector**: tags={bias.tags}, "
                f"directional_hint={bias.directional_hint:+.2f}\n"
            )
        prior_block = (
            f"\n**Reference-class prior**: {prior.prior_p:.3f} (weight={prior.weight:.1f}, "
            f"rationale={prior.rationale})\n"
            if prior is not None
            else ""
        )

        prompt = (
            f"Question: {question}\n"
            f"Category: {category}\n"
            f"Market YES price: {market_price:.4f}\n\n"
            f"**Research**:\n"
            f"- Estimated true probability: {research.get('estimated_probability', market_price):.4f}\n"
            f"- Confidence: {research.get('confidence', 0.5):.2f}\n"
            f"- Summary: {research.get('summary', 'N/A')}\n"
            f"- Bull case: {research.get('bull_case', 'N/A')}\n"
            f"- Bear case: {research.get('bear_case', 'N/A')}\n"
            f"- Data quality: {research.get('data_quality', 'low')}\n"
            f"{prior_block}{peer_block}{bias_block}\n"
            "Output the three-scenario JSON now."
        )

        raw = await self.run(prompt)
        scenarios = self._parse(raw, market_price)
        if scenarios is None:
            log.warning("SignalGenerator parse error", raw=raw[:200])
            return None

        signal = self._assemble(
            market=market,
            scenarios=scenarios,
            market_price=market_price,
            prior=prior,
            bias=bias,
            research_summary=scenarios.get("research_summary", ""),
            rationale=scenarios.get("rationale", ""),
            side_hint=scenarios.get("side"),
        )
        return signal

    # ------------------------------------------------------------------ #
    #  Parsing + assembly                                                #
    # ------------------------------------------------------------------ #

    def _parse(self, raw: str, market_price: float) -> dict | None:
        try:
            start = raw.find("{")
            end = raw.rfind("}") + 1
            if start < 0 or end <= start:
                return None
            data = json.loads(raw[start:end])
        except (json.JSONDecodeError, ValueError):
            return None

        # Backward-compatible mode: if the LLM returned a legacy-shaped object
        # (single estimated_probability + confidence), fan it out into three
        # scenarios so the rest of the pipeline still works.
        if "base_estimate" not in data and "estimated_probability" in data:
            p = float(data.get("estimated_probability", market_price))
            c = float(data.get("confidence", 0.5))
            spread = 0.05 if c >= 0.7 else 0.10
            data = {
                "bull_estimate": min(1.0, p + spread),
                "bull_confidence": c * 0.9,
                "base_estimate": p,
                "base_confidence": c,
                "bear_estimate": max(0.0, p - spread),
                "bear_confidence": c * 0.9,
                "side": data.get("side", "YES" if p > market_price else "NO"),
                "rationale": data.get("rationale", ""),
                "research_summary": data.get("research_summary", ""),
            }
        return data

    def _assemble(
        self,
        *,
        market: dict,
        scenarios: dict,
        market_price: float,
        prior,
        bias: BiasReport | None,
        research_summary: str,
        rationale: str,
        side_hint: str | None,
    ) -> Signal:
        bull = float(scenarios.get("bull_estimate", market_price))
        base = float(scenarios.get("base_estimate", market_price))
        bear = float(scenarios.get("bear_estimate", market_price))
        bull_c = float(scenarios.get("bull_confidence", 0.5))
        base_c = float(scenarios.get("base_confidence", 0.5))
        bear_c = float(scenarios.get("bear_confidence", 0.5))

        estimators: list[tuple[float, float, str]] = []  # (estimate, weight, label)
        s = self._settings
        if prior is not None:
            estimators.append((float(prior.prior_p), s.ensemble_weight_prior, "prior"))
        estimators.append((bull, s.ensemble_weight_bull, "bull"))
        estimators.append((base, s.ensemble_weight_bull, "base"))  # base uses same weight class
        estimators.append((bear, s.ensemble_weight_bear, "bear"))
        estimators.append((market_price, s.ensemble_weight_market, "market"))

        wsum = sum(w for _, w, _ in estimators) or 1.0
        posterior = sum(e * w for e, w, _ in estimators) / wsum

        # Re-anchor to prior via Bayesian blend on top, if a prior matched
        if prior is not None:
            posterior, _ = bayesian_blend(prior, posterior, llm_weight=8.0)

        # Model disagreement = std across LLM scenarios only
        disagreement = (
            pstdev([bull, base, bear]) if max(bull, base, bear) > min(bull, base, bear) else 0.0
        )
        disagreement = max(disagreement, s.ensemble_disagreement_floor)

        # Confidence = mean scenario confidence × (1 − disagreement)
        scenario_conf = (bull_c + base_c + bear_c) / 3.0
        confidence = max(0.0, min(1.0, scenario_conf * (1.0 - disagreement)))
        if bias is not None:
            confidence = max(0.0, min(1.0, confidence * bias.confidence_modifier))

        # Side decision: prefer LLM hint, else infer from posterior vs market
        side = (side_hint or ("YES" if posterior >= market_price else "NO")).upper()
        # Conventional signed edge (positive = YES underpriced, negative = NO underpriced).
        edge = posterior - market_price
        # Direction-corrected magnitude — how much edge our chosen side actually has.
        directional_edge = edge if side == "YES" else -edge
        strength = self._classify(directional_edge, confidence)

        token_id = (
            market.get("yes_token_id", "") if side == "YES" else market.get("no_token_id", "")
        ) or market.get("yes_token_id", "")

        return Signal(
            market_id=market.get("condition_id", ""),
            question=market.get("question", ""),
            token_id=token_id,
            side=side,
            strength=strength,
            estimated_probability=posterior,
            market_price=market_price,
            edge=edge,
            confidence=confidence,
            rationale=rationale,
            research_summary=research_summary,
            category=market.get("category", "") or "",
            cluster_id=(market.get("category", "") or "other").lower().strip(),
            resolves_at=market.get("end_date_iso", "") or "",
            prior_p=float(prior.prior_p) if prior is not None else None,
            prior_weight=float(prior.weight) if prior is not None else 0.0,
            posterior_p=posterior,
            model_disagreement=disagreement,
            bias_tags_json=json.dumps(bias.tags if bias else []),
        )

    @staticmethod
    def _classify(edge: float, confidence: float) -> SignalStrength:
        # `edge` here is already direction-corrected (positive when our chosen
        # side wins, negative otherwise). A negative edge means the data
        # disagrees with the side we picked → HOLD, don't enter.
        if edge <= 0:
            return SignalStrength.HOLD
        if edge >= 0.15 and confidence >= 0.7:
            return SignalStrength.STRONG_BUY
        if edge >= 0.05 and confidence >= 0.6:
            return SignalStrength.BUY
        return SignalStrength.HOLD
