"""
Peer prediction-market price lookups (Manifold, Kalshi, Metaculus).

Each adapter returns a YES probability in [0, 1] or None on failure / disabled.
The bias detector's `cross_market_gap` feature consumes these.

Question matching is fuzzy: we generate a stable `fingerprint` from question
text (lowercased, stopwords removed, slugified) and pass it as a search query.
For richer joins, callers can persist explicit peer-IDs in
`market_fingerprints`.
"""

import re
from dataclasses import dataclass

import httpx
import structlog

from config import Settings

log = structlog.get_logger(__name__)


@dataclass
class PeerPrices:
    by_source: dict[str, float]


_STOPWORDS = {
    "the",
    "a",
    "an",
    "of",
    "to",
    "in",
    "on",
    "for",
    "by",
    "with",
    "and",
    "or",
    "be",
    "is",
    "are",
    "will",
    "would",
    "should",
    "could",
    "have",
    "has",
    "had",
    "this",
    "that",
    "these",
    "those",
    "at",
    "as",
    "from",
    "after",
    "before",
}


def question_fingerprint(question: str) -> str:
    """Stable, slugified lossy fingerprint of a question string."""
    tokens = re.findall(r"[a-zA-Z0-9]+", (question or "").lower())
    cleaned = [t for t in tokens if t not in _STOPWORDS]
    return "-".join(cleaned[:12])


async def fetch_peer_prices(
    settings: Settings,
    question: str,
    fingerprint: str | None = None,
    http: httpx.AsyncClient | None = None,
) -> PeerPrices:
    """
    Fetch YES-probability quotes from each enabled peer source. Sources flagged
    OFF are skipped silently. Returns a `PeerPrices` with whatever was found
    (may be empty).
    """
    sources = settings.resolve_sources()
    fp = fingerprint or question_fingerprint(question)
    out: dict[str, float] = {}
    own_http = http is None
    client = http or httpx.AsyncClient(timeout=12.0)
    try:
        if sources.get("manifold"):
            p = await _manifold(settings, fp, client)
            if p is not None:
                out["manifold"] = p
        if sources.get("kalshi"):
            p = await _kalshi(settings, fp, client)
            if p is not None:
                out["kalshi"] = p
        if sources.get("metaculus"):
            p = await _metaculus(settings, fp, client)
            if p is not None:
                out["metaculus"] = p
    finally:
        if own_http:
            await client.aclose()
    return PeerPrices(by_source=out)


async def _manifold(settings: Settings, query: str, http: httpx.AsyncClient) -> float | None:
    try:
        resp = await http.get(
            f"{settings.manifold_api_base}/search-markets",
            params={"term": query, "limit": 1},
        )
        if resp.status_code != 200:
            return None
        items = resp.json() or []
        if not items:
            return None
        top = items[0]
        # Binary markets carry `probability` directly. Multi-outcome markets carry
        # `pool` / `answers` — skipped for now.
        p = top.get("probability")
        if isinstance(p, (int, float)) and 0.0 <= p <= 1.0:
            return float(p)
    except Exception as exc:
        log.debug("Manifold fetch failed", error=str(exc))
    return None


async def _kalshi(settings: Settings, query: str, http: httpx.AsyncClient) -> float | None:
    try:
        resp = await http.get(
            f"{settings.kalshi_api_base}/markets",
            params={"limit": 5, "status": "open", "query": query},
        )
        if resp.status_code != 200:
            return None
        data = resp.json() or {}
        markets = data.get("markets") or []
        if not markets:
            return None
        m = markets[0]
        # Kalshi quotes prices in cents 0-100; "yes_bid"/"yes_ask"/"last_price".
        last = m.get("last_price")
        if last is None:
            last = (m.get("yes_bid", 0) + m.get("yes_ask", 0)) / 2.0
        if last is None:
            return None
        return float(last) / 100.0 if last > 1 else float(last)
    except Exception as exc:
        log.debug("Kalshi fetch failed", error=str(exc))
    return None


async def _metaculus(settings: Settings, query: str, http: httpx.AsyncClient) -> float | None:
    try:
        resp = await http.get(
            f"{settings.metaculus_api_base}/questions/",
            params={"search": query, "limit": 1, "status": "open"},
        )
        if resp.status_code != 200:
            return None
        data = resp.json() or {}
        results = data.get("results") or []
        if not results:
            return None
        q = results[0]
        # Binary questions expose `community_prediction.full.q2` (median).
        cp = (q.get("community_prediction") or {}).get("full") or {}
        median = cp.get("q2")
        if isinstance(median, (int, float)) and 0.0 <= median <= 1.0:
            return float(median)
    except Exception as exc:
        log.debug("Metaculus fetch failed", error=str(exc))
    return None
