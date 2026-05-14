"""Models for the copy-trading subsystem."""

from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, Field

# Trader status lifecycle:
#   discovered -> shadow -> paper -> live
#                                 \-> disabled
TRADER_STATUSES = ("discovered", "shadow", "paper", "live", "disabled")
COPY_MODES = ("shadow", "paper", "live", "backtest")


class TrackedTrader(BaseModel):
    wallet: str
    status: str = "discovered"
    preset: str = "scaled_market"
    score: float = 0.0
    sample_size: int = 0
    weeks_profitable_frac: float = 0.0
    max_drawdown: float = 0.0
    total_volume_usdc: float = 0.0
    resolution_sniper_frac: float = 0.0
    last_seen_ts: int = 0  # unix seconds — high-water mark for activity polling
    last_evaluated_at: datetime | None = None
    notes: str = ""
    created_at: datetime | None = Field(default_factory=datetime.utcnow)


class LeaderTrade(BaseModel):
    wallet: str
    tx_hash: str
    condition_id: str
    token_id: str
    side: str  # BUY|SELL
    outcome: str  # YES|NO
    size_usdc: float
    price: float
    observed_at: datetime
    expected_copy: bool = False
    copy_order_id: str = ""
    copy_mode: str = ""  # shadow|paper|live (set after dispatch)
    skip_reason: str = ""
    created_at: datetime | None = Field(default_factory=datetime.utcnow)


class PaperOrder(BaseModel):
    id: str = Field(default_factory=lambda: f"paper-{uuid4()}")
    wallet: str  # the leader being followed
    market_id: str
    signal_id: str = ""
    token_id: str
    side: str  # BUY|SELL
    size_usdc: float
    price: float  # simulated VWAP fill price
    order_type: str = "MARKET"
    status: str = "FILLED"
    placed_at: datetime | None = None
    filled_at: datetime | None = None
    fill_price: float | None = None
    error: str = ""
    leader_tx_hash: str = ""


class PaperPosition(BaseModel):
    id: str = Field(default_factory=lambda: f"paperpos-{uuid4()}")
    wallet: str
    market_id: str
    question: str = ""
    token_id: str
    side: str  # YES|NO
    size: float
    avg_price: float
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    opened_at: datetime | None = None
    closed_at: datetime | None = None

    def update_pnl(self) -> None:
        self.unrealized_pnl = round((self.current_price - self.avg_price) * self.size, 4)


class CopyPerformance(BaseModel):
    wallet: str
    mode: str  # shadow|paper|live|backtest
    trades_observed: int = 0
    trades_copied: int = 0
    copy_hit_rate: float = 0.0  # trades_copied / trades_observed
    audit_miss_rate: float = 0.0  # missed-by-bug / expected
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    win_count: int = 0
    loss_count: int = 0
    notes: str = ""
    last_updated: datetime | None = Field(default_factory=datetime.utcnow)

    @property
    def total_pnl(self) -> float:
        return round(self.realized_pnl + self.unrealized_pnl, 4)
