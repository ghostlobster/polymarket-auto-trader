from datetime import datetime

from pydantic import BaseModel


class Position(BaseModel):
    id: str = ""
    market_id: str
    question: str = ""
    token_id: str
    side: str                  # "YES" or "NO"
    size: float                # number of shares held
    avg_price: float           # average entry price (0-1)
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    opened_at: datetime | None = None
    closed_at: datetime | None = None

    def update_pnl(self) -> None:
        self.unrealized_pnl = round((self.current_price - self.avg_price) * self.size, 4)

    @property
    def cost_basis_usdc(self) -> float:
        return round(self.avg_price * self.size, 4)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None


class PortfolioSnapshot(BaseModel):
    total_usdc: float
    available_usdc: float
    open_positions: list[Position] = []
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    snapshot_at: datetime | None = None

    @property
    def total_pnl(self) -> float:
        return round(self.realized_pnl + self.unrealized_pnl, 4)
