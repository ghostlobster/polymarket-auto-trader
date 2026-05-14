"""
OrderBookSnapshotter — periodic minutely-ish snapshots of the CLOB.

For each market on the watch-list (open positions ∪ recently-scanned markets)
this writes a compact row to `orderbook_snapshots`: best bid/ask, mid,
microprice, and depth at 1¢ and 5¢ from the mid on each side. Downstream the
bias detector consumes these snapshots to compute panic-cascade and herd-flow
features.

Pure async, no LLM. Failure on a single market is logged and skipped — one
flaky token shouldn't kill the whole loop.
"""

from datetime import datetime

import structlog

from config import Settings
from database import Database
from models import OrderBook, PriceLevel
from polymarket.client import PolymarketClient

log = structlog.get_logger(__name__)


def _depth_within(levels: list[PriceLevel], ref: float, band: float, ascending: bool) -> float:
    """
    Sum size within `band` of `ref`. For bids, `band` is how far BELOW ref to
    include; for asks, how far ABOVE. Levels at exactly `ref` are included.
    """
    total = 0.0
    for lvl in levels:
        if ascending:  # asks
            if lvl.price <= ref + band + 1e-9:
                total += lvl.size
        else:  # bids
            if lvl.price >= ref - band - 1e-9:
                total += lvl.size
    return round(total, 4)


def summarize_orderbook(book: OrderBook, condition_id: str) -> dict | None:
    """Compress an OrderBook into a snapshot row. Returns None if book is empty."""
    if not book.bids or not book.asks:
        return None
    best_bid = book.best_bid
    best_ask = book.best_ask
    mid = book.mid or (best_bid + best_ask) / 2.0

    top_bid_size = book.bids[0].size if book.bids else 0.0
    top_ask_size = book.asks[0].size if book.asks else 0.0
    denom = top_bid_size + top_ask_size or 1e-9
    microprice = (best_bid * top_ask_size + best_ask * top_bid_size) / denom

    return {
        "condition_id": condition_id,
        "token_id": book.token_id,
        "ts": datetime.utcnow().isoformat(),
        "best_bid": round(best_bid, 4),
        "best_ask": round(best_ask, 4),
        "mid": round(mid, 4),
        "microprice": round(microprice, 4),
        "bid_depth_1c": _depth_within(book.bids, best_bid, 0.01, ascending=False),
        "bid_depth_5c": _depth_within(book.bids, best_bid, 0.05, ascending=False),
        "ask_depth_1c": _depth_within(book.asks, best_ask, 0.01, ascending=True),
        "ask_depth_5c": _depth_within(book.asks, best_ask, 0.05, ascending=True),
        "top_bid_size": round(top_bid_size, 4),
        "top_ask_size": round(top_ask_size, 4),
    }


class OrderBookSnapshotter:
    """Drives the snapshot loop for a configured watch-list."""

    name = "OrderBookSnapshotter"

    def __init__(self, settings: Settings, poly: PolymarketClient, db: Database):
        self._settings = settings
        self._poly = poly
        self._db = db
        self._watchlist: dict[str, str] = {}  # token_id -> condition_id

    def watch(self, token_id: str, condition_id: str) -> None:
        """Add a market to the snapshot watch-list."""
        if not token_id:
            return
        self._watchlist[token_id] = condition_id

    def unwatch(self, token_id: str) -> None:
        self._watchlist.pop(token_id, None)

    async def run_once(self) -> dict:
        """Snapshot every watched market once. Returns counts."""
        # Auto-include open positions
        try:
            positions = await self._db.get_open_positions()
            for p in positions:
                self.watch(p.token_id, p.market_id)
        except Exception as exc:
            log.debug("Snapshot: failed to enumerate open positions", error=str(exc))

        items = list(self._watchlist.items())[: self._settings.snapshot_market_limit]
        ok, err = 0, 0
        for token_id, condition_id in items:
            try:
                book = await self._poly.get_orderbook(token_id)
                snap = summarize_orderbook(book, condition_id)
                if snap is None:
                    continue
                await self._db.save_orderbook_snapshot(snap)
                ok += 1
            except Exception as exc:
                err += 1
                log.debug("Snapshot failed", token_id=token_id, error=str(exc))
        return {"snapshotted": ok, "errors": err, "watched": len(self._watchlist)}
