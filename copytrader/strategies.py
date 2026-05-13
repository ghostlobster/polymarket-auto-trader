"""
Strategy presets for copy-trading.

Each preset encodes an explicit tradeoff between fidelity, slippage tolerance,
and risk. Selected per trader at promotion time.

| preset          | order_type | scale  | max_age_secs | max_slippage | resolves_in_min_h |
|-----------------|------------|--------|--------------|--------------|-------------------|
| mirror          | market     | 1.0    | 60           | 0.05         | 0   (any)         |
| scaled_market   | market     | 0.01   | 120          | 0.03         | 24                |
| scaled_limit    | limit      | 0.01   | 120          | 0.01         | 24                |
| conservative    | limit      | 0.005  | 90           | 0.01         | 168 (7 d)         |
| shadow          | none       | 0.0    | -            | -            | -                 |

`apply_preset` returns either a `CopyDecision(skip=False, ...)` ready to send
to the executor, or `CopyDecision(skip=True, reason='...')`.
"""

from dataclasses import dataclass
from datetime import datetime, timezone

from models import OrderBook


@dataclass(frozen=True)
class StrategyPreset:
    name: str
    order_type: str  # 'market' | 'limit' | 'none'
    notional_scale: float
    max_age_secs: int
    max_slippage: float
    resolves_in_min_hours: float
    description: str


PRESETS: dict[str, StrategyPreset] = {
    "mirror": StrategyPreset(
        name="mirror",
        order_type="market",
        notional_scale=1.0,
        max_age_secs=60,
        max_slippage=0.05,
        resolves_in_min_hours=0.0,
        description="Highest fidelity. Same notional as leader (capped). Market orders. Tolerates slippage.",
    ),
    "scaled_market": StrategyPreset(
        name="scaled_market",
        order_type="market",
        notional_scale=0.01,
        max_age_secs=120,
        max_slippage=0.03,
        resolves_in_min_hours=24.0,
        description="Balanced default. 1% of leader notional, market fills, skips imminent resolutions.",
    ),
    "scaled_limit": StrategyPreset(
        name="scaled_limit",
        order_type="limit",
        notional_scale=0.01,
        max_age_secs=120,
        max_slippage=0.01,
        resolves_in_min_hours=24.0,
        description="Best price, may miss fills. Limit at leader's fill price; tight slippage.",
    ),
    "conservative": StrategyPreset(
        name="conservative",
        order_type="limit",
        notional_scale=0.005,
        max_age_secs=90,
        max_slippage=0.01,
        resolves_in_min_hours=168.0,
        description="Low risk. Half-percent of leader notional, only on markets that resolve >7d out.",
    ),
    "shadow": StrategyPreset(
        name="shadow",
        order_type="none",
        notional_scale=0.0,
        max_age_secs=120,
        max_slippage=1.0,
        resolves_in_min_hours=0.0,
        description="Logs only — never executes. For empirical evaluation.",
    ),
}


def get_preset(name: str) -> StrategyPreset:
    """Return preset by name, falling back to scaled_market."""
    return PRESETS.get(name, PRESETS["scaled_market"])


@dataclass
class CopyDecision:
    skip: bool
    reason: str = ""
    order_type: str = ""  # 'market' | 'limit'
    size_usdc: float = 0.0
    limit_price: float | None = None
    expected_copy: bool = False  # whether this should have produced an order


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def apply_preset(
    preset: StrategyPreset,
    leader_event: dict,
    book: OrderBook | None,
    available_usdc: float,
    max_position_usdc: float,
    market_resolves_at: datetime | None = None,
    leader_min_notional_usdc: float = 200.0,
) -> CopyDecision:
    """
    Decide whether and how to copy a single leader event.

    leader_event is the dict produced by `PolymarketDataClient.get_user_activity`.
    """
    leader_notional = float(leader_event.get("size_usdc", 0) or 0)
    if leader_notional < leader_min_notional_usdc:
        return CopyDecision(skip=True, reason="leader_notional_below_min", expected_copy=False)

    # Age check
    ts = int(leader_event.get("timestamp", 0) or 0)
    if ts > 0:
        age = (_now_utc() - datetime.fromtimestamp(ts, tz=timezone.utc)).total_seconds()
        if age > preset.max_age_secs:
            return CopyDecision(
                skip=True,
                reason=f"stale:{int(age)}s>{preset.max_age_secs}s",
                expected_copy=False,
            )

    # Resolution-window filter
    if preset.resolves_in_min_hours > 0 and market_resolves_at is not None:
        delta_h = (market_resolves_at - _now_utc()).total_seconds() / 3600.0
        if delta_h < preset.resolves_in_min_hours:
            return CopyDecision(
                skip=True,
                reason=f"resolves_in_{delta_h:.1f}h<{preset.resolves_in_min_hours}h",
                expected_copy=False,
            )

    # Shadow short-circuit — log as expected_copy=False to keep audit honest.
    if preset.order_type == "none":
        return CopyDecision(
            skip=True,
            reason="shadow_mode",
            expected_copy=False,
        )

    # Sizing
    raw_size = leader_notional * preset.notional_scale
    size_usdc = min(raw_size, max_position_usdc, max(available_usdc, 0.0))
    if size_usdc < 5.0:
        return CopyDecision(
            skip=True,
            reason=f"size<{5.0}_after_caps:{size_usdc:.2f}",
            expected_copy=False,
        )

    # Slippage check (entries only — sells reduce position size and don't suffer adverse selection here)
    leader_price = float(leader_event.get("price", 0) or 0)
    side = (leader_event.get("side") or "BUY").upper()
    if book is not None and leader_price > 0 and side == "BUY":
        ref = book.best_ask or book.mid or leader_price
        if ref > leader_price * (1 + preset.max_slippage):
            return CopyDecision(
                skip=True,
                reason=f"slippage:{ref:.4f}>{leader_price:.4f}*(1+{preset.max_slippage})",
                expected_copy=True,  # we wanted to copy, slippage forced skip — counts as "expected miss"
            )

    if preset.order_type == "market":
        return CopyDecision(
            skip=False,
            order_type="market",
            size_usdc=size_usdc,
            limit_price=None,
            expected_copy=True,
        )
    if preset.order_type == "limit":
        # Limit at leader's fill price (or 1¢ inside spread for conservative side)
        limit_price = leader_price if leader_price > 0 else (book.mid if book else 0.5)
        return CopyDecision(
            skip=False,
            order_type="limit",
            size_usdc=size_usdc,
            limit_price=round(limit_price, 4),
            expected_copy=True,
        )

    return CopyDecision(skip=True, reason="unknown_preset_order_type")
