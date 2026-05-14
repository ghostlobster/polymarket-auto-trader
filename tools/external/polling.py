"""
Polling-aggregator adapter (538 / Silver Bulletin style).

Flagged off by default; intended for political markets only. The free
endpoints don't expose a clean JSON API for every race, so this adapter is
best-effort and returns None for any unmatched question.

When enabled it returns a polling-consensus YES probability that the bias
detector treats like any other peer-market price.
"""

from dataclasses import dataclass

import httpx
import structlog

from config import Settings

log = structlog.get_logger(__name__)


@dataclass
class PollingResult:
    yes_probability: float
    source: str
    sample_size: int = 0


async def fetch_polling_consensus(
    settings: Settings,
    question: str,
    http: httpx.AsyncClient | None = None,
) -> PollingResult | None:
    """
    Attempt to pull a polling-consensus probability for the question.

    Implementation note: 538 retired and Silver Bulletin requires authenticated
    access for most series. This adapter checks one public JSON file that the
    aggregator historically exposed for the headline presidential race; for any
    other question we return None and let the caller fall back to peer markets.
    """
    sources = settings.resolve_sources()
    if not sources.get("polling"):
        return None

    own_http = http is None
    client = http or httpx.AsyncClient(timeout=12.0)
    try:
        # We support one well-known public-facing path. Customize per deployment.
        if "president" in (question or "").lower() and "win" in (question or "").lower():
            try:
                resp = await client.get(
                    f"{settings.polling_aggregator_base}/election-forecast/summary.json"
                )
                if resp.status_code != 200:
                    return None
                payload = resp.json()
                p = payload.get("median_probability")
                if isinstance(p, (int, float)) and 0.0 <= p <= 1.0:
                    return PollingResult(
                        yes_probability=float(p),
                        source="polling_aggregator",
                        sample_size=int(payload.get("n_polls", 0) or 0),
                    )
            except Exception as exc:
                log.debug("Polling fetch failed", error=str(exc))
        return None
    finally:
        if own_http:
            await client.aclose()
