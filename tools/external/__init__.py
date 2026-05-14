"""
External-data adapters for the cognitive-arbitrage engine.

Every adapter checks its own feature flag (via `Settings.resolve_sources`) and
returns `None` (or an empty list) when disabled, so callers can degrade
gracefully without try/except spaghetti.

Adapters are intentionally tiny — each is one function that hits one
documented public endpoint and returns a dict. Anything heavier belongs in the
research/ package.
"""

from .news import NewsResult, fetch_news_volume, score_sentiment
from .peer_markets import PeerPrices, fetch_peer_prices, question_fingerprint
from .polling import PollingResult, fetch_polling_consensus

__all__ = [
    "fetch_news_volume",
    "score_sentiment",
    "NewsResult",
    "fetch_peer_prices",
    "question_fingerprint",
    "PeerPrices",
    "fetch_polling_consensus",
    "PollingResult",
]
