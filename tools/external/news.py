"""
News-volume + headline-sentiment adapters.

Two backends, each independently flagged:
  - NewsAPI (https://newsapi.org)  via `newsapi_enabled` + `NEWSAPI_KEY`
  - GDELT  (https://api.gdeltproject.org) via `gdelt_enabled`, no key needed.

Both return a `NewsResult` with the article count over the last 24h and the
hours-since the most recent article. Sentiment is scored locally with a tiny
lexicon (no extra API spend, no transformers dependency).
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import httpx
import structlog

from config import Settings

log = structlog.get_logger(__name__)


@dataclass
class NewsResult:
    volume_24h: int
    hours_since_latest: float | None
    headlines: list[str]
    source: str  # "newsapi" | "gdelt" | "none"


# Tiny finance/political polarity lexicon. Crude but free, deterministic, and
# avoids dragging in transformers/numpy bloat for a single scalar.
_POS = {
    "win",
    "wins",
    "winning",
    "approve",
    "approved",
    "rises",
    "surge",
    "boost",
    "agree",
    "agreement",
    "ceasefire",
    "deal",
    "passes",
    "passed",
    "rally",
    "strong",
    "beats",
    "exceed",
    "record",
    "growth",
}
_NEG = {
    "lose",
    "loses",
    "losing",
    "reject",
    "rejected",
    "falls",
    "fall",
    "drop",
    "crash",
    "fail",
    "failed",
    "fails",
    "war",
    "attack",
    "violence",
    "loss",
    "missed",
    "miss",
    "decline",
    "weak",
    "scandal",
    "indicted",
    "indictment",
}


def score_sentiment(headlines: list[str]) -> float | None:
    """
    Return a polarity score in [-1, 1], or None when no headlines are present.

    Methodology: per-headline (pos - neg) / (pos + neg + 1), then averaged.
    Negation is detected with a 2-word window for "not", "no", "without".
    """
    if not headlines:
        return None
    scores: list[float] = []
    for h in headlines:
        tokens = [t.strip(".,!?:;\"'()").lower() for t in (h or "").split()]
        if not tokens:
            continue
        pos, neg = 0, 0
        for i, t in enumerate(tokens):
            negated = i >= 1 and tokens[i - 1] in {"not", "no", "without", "never"}
            if t in _POS:
                neg += 1 if negated else 0
                pos += 0 if negated else 1
            elif t in _NEG:
                pos += 1 if negated else 0
                neg += 0 if negated else 1
        denom = pos + neg + 1
        scores.append((pos - neg) / denom)
    if not scores:
        return None
    return round(sum(scores) / len(scores), 4)


async def fetch_news_volume(
    settings: Settings,
    query: str,
    http: httpx.AsyncClient | None = None,
) -> NewsResult | None:
    """
    Fetch 24h news volume and recent headlines for a query. Honors per-source
    flags via `Settings.resolve_sources` — returns None when neither backend
    is enabled.
    """
    sources = settings.resolve_sources()
    own_http = http is None
    client = http or httpx.AsyncClient(timeout=12.0)

    try:
        if sources.get("news") and settings.newsapi_key:
            try:
                return await _fetch_newsapi(settings, query, client)
            except Exception as exc:
                log.debug("NewsAPI failed", error=str(exc))
        if sources.get("gdelt"):
            try:
                return await _fetch_gdelt(settings, query, client)
            except Exception as exc:
                log.debug("GDELT failed", error=str(exc))
        return None
    finally:
        if own_http:
            await client.aclose()


async def _fetch_newsapi(
    settings: Settings, query: str, http: httpx.AsyncClient
) -> NewsResult | None:
    since = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    resp = await http.get(
        f"{settings.newsapi_base}/everything",
        params={
            "q": query,
            "from": since,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": 50,
            "apiKey": settings.newsapi_key,
        },
    )
    if resp.status_code != 200:
        return None
    data = resp.json()
    articles = data.get("articles", []) or []
    headlines = [a.get("title", "") for a in articles[:20]]
    latest_iso = articles[0]["publishedAt"] if articles else None
    hours_since = _hours_since(latest_iso)
    return NewsResult(
        volume_24h=len(articles),
        hours_since_latest=hours_since,
        headlines=headlines,
        source="newsapi",
    )


async def _fetch_gdelt(
    settings: Settings, query: str, http: httpx.AsyncClient
) -> NewsResult | None:
    resp = await http.get(
        settings.gdelt_base,
        params={
            "query": query,
            "mode": "ArtList",
            "format": "json",
            "timespan": "1d",
            "maxrecords": 50,
            "sort": "DateDesc",
        },
    )
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    articles = data.get("articles", []) or []
    headlines = [a.get("title", "") for a in articles[:20]]
    latest_iso = articles[0].get("seendate") if articles else None
    hours_since = _hours_since(latest_iso, fmt="%Y%m%dT%H%M%SZ") if latest_iso else None
    return NewsResult(
        volume_24h=len(articles),
        hours_since_latest=hours_since,
        headlines=headlines,
        source="gdelt",
    )


def _hours_since(ts: str | None, fmt: str | None = None) -> float | None:
    if not ts:
        return None
    try:
        if fmt:
            dt = datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
        else:
            s = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        delta = datetime.now(timezone.utc) - dt
        return round(delta.total_seconds() / 3600.0, 2)
    except (ValueError, TypeError):
        return None
