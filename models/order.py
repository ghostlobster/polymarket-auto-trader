from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(str, Enum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class OrderStatus(str, Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class Order(BaseModel):
    id: str = ""
    market_id: str
    signal_id: str = ""
    token_id: str
    side: OrderSide
    size_usdc: float
    price: float  # 0.0–1.0 (Polymarket uses cents / 100)
    order_type: OrderType = OrderType.LIMIT
    status: OrderStatus = OrderStatus.PENDING
    placed_at: datetime | None = None
    filled_at: datetime | None = None
    fill_price: float | None = None
    error: str = ""
