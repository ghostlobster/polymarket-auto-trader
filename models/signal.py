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
    side: str                           # "YES" or "NO"
    strength: SignalStrength
    estimated_probability: float        # our assessed true probability (0-1)
    market_price: float                 # current market price (0-1)
    edge: float                         # estimated_probability - market_price
    rationale: str
    confidence: float                   # 0-1
    research_summary: str = ""
    created_at: datetime = Field(default_factory=datetime.utcnow)

    @field_validator("estimated_probability", "market_price", "confidence")
    @classmethod
    def clamp_01(cls, v: float) -> float:
        return max(0.0, min(1.0, v))

    @property
    def is_actionable(self) -> bool:
        return abs(self.edge) >= 0.05 and self.confidence >= 0.6
