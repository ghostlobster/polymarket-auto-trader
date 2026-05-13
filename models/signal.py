from datetime import datetime
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


class SignalStrength(str, Enum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    HOLD = "HOLD"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"


class Signal(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    market_id: str
    question: str
    token_id: str
    side: str  # "YES" or "NO"
    strength: SignalStrength
    estimated_probability: float  # our final posterior probability (0-1)
    market_price: float  # current market price (0-1)
    edge: float  # estimated_probability - market_price
    rationale: str
    confidence: float  # 0-1
    research_summary: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # Provenance — 'thesis' for the original research pipeline,
    # 'copy' for signals produced by following another trader.
    source: str = "thesis"
    leader_wallet: str = ""  # populated when source == 'copy'
    preset: str = ""  # strategy preset name, when source == 'copy'

    # --- Cognitive-arbitrage additions ---
    category: str = ""
    cluster_id: str = ""  # for correlation-cap enforcement
    resolves_at: str = ""  # ISO 8601 — for theta + resolution-window gating
    prior_p: float | None = None  # reference-class prior probability
    prior_weight: float = 0.0  # pseudo-count weight on the prior
    posterior_p: float | None = None  # blended posterior (same as estimated_probability normally)
    model_disagreement: float = 0.0  # std of ensemble estimators
    bias_tags_json: str = "[]"  # JSON-encoded list of bias tags from detector
    applied_shrinkage: float = 1.0  # shrinkage factor applied by risk manager
    resolved_outcome: str = ""  # YES|NO|INVALID once known
    resolved_at: str = ""  # ISO timestamp when resolution was recorded
    was_correct: int | None = None  # 1/0 — set by calibration auditor
    realized_brier: float | None = None
    realized_log_loss: float | None = None

    @field_validator("estimated_probability", "market_price", "confidence")
    @classmethod
    def clamp_01(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @property
    def is_actionable(self) -> bool:
        return abs(self.edge) >= 0.05 and self.confidence >= 0.6
