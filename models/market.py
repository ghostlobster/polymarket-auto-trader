from datetime import datetime
from enum import Enum
from pydantic import BaseModel, Field


class TokenType(str, Enum):
    YES = "YES"
    NO = "NO"


class PriceLevel(BaseModel):
    price: float
    size: float


class OrderBook(BaseModel):
    token_id: str
    bids: list[PriceLevel] = []
    asks: list[PriceLevel] = []
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    mid: float = 0.0

    def model_post_init(self, __context) -> None:
        if self.bids:
            self.best_bid = max(l.price for l in self.bids)
        if self.asks:
            self.best_ask = min(l.price for l in self.asks)
        if self.best_bid and self.best_ask:
            self.spread = round(self.best_ask - self.best_bid, 4)
            self.mid = round((self.best_bid + self.best_ask) / 2, 4)


class Market(BaseModel):
    condition_id: str
    question: str
    category: str = "Unknown"
    description: str = ""
    end_date_iso: str = ""
    active: bool = True
    closed: bool = False
    volume: float = 0.0
    volume_24h: float = 0.0
    liquidity: float = 0.0
    yes_token_id: str = ""
    no_token_id: str = ""
    # Derived from order book — populated after fetching book
    best_bid: float = 0.0
    best_ask: float = 0.0
    spread: float = 0.0
    last_trade_price: float = 0.0
    # Opportunity score assigned by scanner
    opportunity_score: float = Field(default=0.0, exclude=True)

    @property
    def days_to_resolution(self) -> float | None:
        if not self.end_date_iso:
            return None
        try:
            end = datetime.fromisoformat(self.end_date_iso.replace("Z", "+00:00"))
            delta = end - datetime.now(end.tzinfo)
            return max(delta.total_seconds() / 86400, 0)
        except ValueError:
            return None
