from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, Field


class AgentMessage(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    from_agent: str
    to_agent: str
    msg_type: str  # "scan_result", "research_result", "signal", "risk_assessment", "order_result", "portfolio_update"
    payload: dict
    created_at: datetime = Field(default_factory=datetime.utcnow)
    processed: bool = False
    processed_at: datetime | None = None
