"""
Bias detector — the cognitive-arbitrage centerpiece.

Given a market, its recent order-book snapshots, the LLM research result, and
optional peer-market / news / smart-money inputs, this module derives a
`BiasReport` flagging which crowd biases (if any) the price exhibits and what
direction they imply.

The detector is intentionally deterministic — every feature is pure Python
operating on numerical or string inputs. The signal generator consumes the
`confidence_modifier` and `directional_hint`; the calibration auditor later
scores each tag's realized edge so we can keep the ones that work and prune
the rest.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from statistics import mean, pstdev


@dataclass
class BiasReport:
    tags: list[str] = field(default_factory=list)
    features: dict = field(default_factory=dict)
    directional_hint: float = 0.0  # signed ∈ [-1, 1]; >0 = YES, <0 = NO
    confidence_modifier: float = 1.0  # multiplier ∈ [0.5, 1.25]
    notes: list[str] = field(default_factory=list)

    def add(self, tag: str, note: str, *, hint: float = 0.0, conf_mult: float = 1.0) -> None:
        self.tags.append(tag)
        self.notes.append(note)
        self.directional_hint = max(-1.0, min(1.0, self.directional_hint + hint))
        self.confidence_modifier = max(0.5, min(1.25, self.confidence_modifier * conf_mult))


def _to_dt(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d
    except ValueError:
        return None


def _panic_cascade(snapshots: list[dict]) -> dict | None:
    """
    Detect a sharp move accompanied by ask/bid depth collapse.

    Snapshots are expected to be ordered newest-first. Returns a dict with the
    feature numbers if a panic-cascade is detected, else None.
    """
    if len(snapshots) < 4:
        return None
    mids = [float(s["mid"]) for s in snapshots if s.get("mid") is not None]
    if len(mids) < 4:
        return None
    latest = mids[0]
    baseline = mean(mids[3:])  # ~15 min back
    sigma = pstdev(mids) or 1e-6
    move = latest - baseline
    if abs(move) < max(0.05, 2 * sigma):
        return None
    # Depth collapse: side OPPOSITE to the move should have shed liquidity.
    if move < 0:  # price dropped: ask side should be intact, bid side gone
        depth_recent = float(snapshots[0].get("bid_depth_5c") or 0)
        depth_old = mean(
            float(s.get("bid_depth_5c") or 0) for s in snapshots[3:6] or [snapshots[-1]]
        )
    else:  # price spiked: bid side should be intact, ask side gone
        depth_recent = float(snapshots[0].get("ask_depth_5c") or 0)
        depth_old = mean(
            float(s.get("ask_depth_5c") or 0) for s in snapshots[3:6] or [snapshots[-1]]
        )
    if depth_old <= 0:
        return None
    depth_ratio = depth_recent / depth_old
    if depth_ratio > 0.4:  # less than 60% drop in opposing depth
        return None
    return {
        "move": round(move, 4),
        "sigma": round(sigma, 4),
        "depth_ratio": round(depth_ratio, 4),
    }


def _herd_imbalance(snapshots: list[dict]) -> dict | None:
    """Detect persistent one-sided depth (potential herd flow)."""
    if len(snapshots) < 3:
        return None
    ratios = []
    for s in snapshots[:5]:
        bid = float(s.get("bid_depth_1c") or 0) + 1.0
        ask = float(s.get("ask_depth_1c") or 0) + 1.0
        ratios.append(bid / ask)
    if not ratios:
        return None
    avg = mean(ratios)
    if 0.55 <= avg <= 1.8:
        return None  # roughly balanced
    return {"avg_bid_to_ask": round(avg, 3), "samples": len(ratios)}


def _favorite_longshot(market_price: float, calibration_buckets: list[dict] | None) -> dict | None:
    """
    Compare current market price to historical realized rate within the same band.
    Returns the gap when the band has been mispriced by >5¢.
    """
    if not calibration_buckets:
        return None
    for b in calibration_buckets:
        if float(b["band_low"]) <= market_price < float(b["band_high"]):
            n = int(b.get("n", 0))
            if n < 20:
                return None
            mean_actual = float(b["mean_actual"])
            gap = mean_actual - market_price
            if abs(gap) < 0.05:
                return None
            return {
                "realized": round(mean_actual, 4),
                "implied": round(market_price, 4),
                "gap": round(gap, 4),
                "n": n,
            }
    return None


def _cross_market_gap(market_price: float, peer_prices: dict[str, float] | None) -> dict | None:
    """
    Compute weighted average of peer-market prices and the resulting gap.
    Peer dict keys are source names (manifold|kalshi|metaculus|polling); values
    are YES probabilities in [0, 1]. Returns gap and individual peer values.
    """
    if not peer_prices:
        return None
    valid = {k: float(v) for k, v in peer_prices.items() if v is not None and 0.0 <= v <= 1.0}
    if not valid:
        return None
    consensus = sum(valid.values()) / len(valid)
    gap = consensus - market_price
    if abs(gap) < 0.05:
        return None
    return {"consensus": round(consensus, 4), "gap": round(gap, 4), "peers": valid}


def _smart_money_disagreement(leader_trades: list[dict] | None, market_price: float) -> dict | None:
    """
    leader_trades: dict-shaped rows from `leader_trades` table, each with
        side (BUY|SELL), outcome (YES|NO), price, size_usdc.
    Returns a gap when ≥60% of recent leader volume is on one side AND the
    average leader fill price disagrees with current market price by ≥5¢.
    """
    if not leader_trades:
        return None
    yes_buy_usd = 0.0
    no_buy_usd = 0.0
    px_weighted = 0.0
    px_weights = 0.0
    for t in leader_trades:
        size = float(t.get("size_usdc") or 0)
        if size <= 0:
            continue
        side = (t.get("side") or "").upper()
        outcome = (t.get("outcome") or "").upper()
        if side == "BUY" and outcome == "YES":
            yes_buy_usd += size
        elif side == "BUY" and outcome == "NO":
            no_buy_usd += size
        px = float(t.get("price") or 0)
        if px > 0:
            px_weighted += px * size
            px_weights += size
    total = yes_buy_usd + no_buy_usd
    if total < 200:  # not enough smart money to call it
        return None
    yes_frac = yes_buy_usd / total
    if 0.4 <= yes_frac <= 0.6:
        return None
    avg_px = px_weighted / px_weights if px_weights else market_price
    gap = avg_px - market_price
    return {
        "yes_share": round(yes_frac, 3),
        "leader_avg_price": round(avg_px, 4),
        "gap": round(gap, 4),
        "volume_usdc": round(total, 2),
    }


def _recency_bias(news_volume_24h: float | None, snapshots: list[dict]) -> dict | None:
    """
    Compares snapshot-window price move with news activity. If the price has
    moved a lot but no news volume is present, we likely have a sentiment-only
    move that's prone to reversion.
    """
    if news_volume_24h is None or news_volume_24h < 0:
        return None
    if len(snapshots) < 2:
        return None
    mids = [float(s["mid"]) for s in snapshots if s.get("mid") is not None]
    if len(mids) < 2:
        return None
    move = mids[0] - mids[-1]
    if abs(move) < 0.04:
        return None
    if news_volume_24h >= 5:
        return None  # news-justified
    return {"move": round(move, 4), "news_volume_24h": float(news_volume_24h)}


def _narrative_staleness(snapshots: list[dict], hours_since_news: float | None) -> dict | None:
    """Persistent drift with stale news → sentiment, not info."""
    if hours_since_news is None or hours_since_news < 48:
        return None
    if len(snapshots) < 4:
        return None
    mids = [float(s["mid"]) for s in snapshots if s.get("mid") is not None]
    if len(mids) < 4:
        return None
    drift = mids[0] - mids[-1]
    if abs(drift) < 0.04:
        return None
    return {"drift": round(drift, 4), "hours_since_news": float(hours_since_news)}


def detect_biases(
    *,
    market_price: float,
    snapshots: list[dict] | None = None,
    calibration_buckets: list[dict] | None = None,
    peer_prices: dict[str, float] | None = None,
    leader_trades: list[dict] | None = None,
    news_volume_24h: float | None = None,
    hours_since_news: float | None = None,
    sentiment_score: float | None = None,
) -> BiasReport:
    """
    Compute the full BiasReport for a market. All inputs are optional — missing
    inputs simply mean the corresponding feature is skipped (graceful degrade).

    `directional_hint` is signed (+ toward YES, − toward NO). `confidence_modifier`
    is multiplicative ∈ [0.5, 1.25].
    """
    report = BiasReport()
    snaps = list(snapshots or [])
    # Snapshots should be newest-first; if oldest-first, reverse.
    if len(snaps) >= 2:
        first_ts = _to_dt(snaps[0].get("ts", ""))
        last_ts = _to_dt(snaps[-1].get("ts", ""))
        if first_ts and last_ts and first_ts < last_ts:
            snaps = list(reversed(snaps))

    # 1. Panic cascade
    panic = _panic_cascade(snaps)
    if panic:
        # If price moved down panic-driven, lean YES (mean-reversion buy).
        hint = 0.4 if panic["move"] < 0 else -0.4
        report.add("panic_cascade", f"panic_cascade {panic}", hint=hint, conf_mult=1.05)
        report.features["panic_cascade"] = panic

    # 2. Herd imbalance
    herd = _herd_imbalance(snaps)
    if herd:
        hint = -0.2 if herd["avg_bid_to_ask"] > 1.8 else 0.2
        report.add("herd", f"herd {herd}", hint=hint, conf_mult=1.0)
        report.features["herd"] = herd

    # 3. Favorite-longshot from realized calibration
    fl = _favorite_longshot(market_price, calibration_buckets)
    if fl:
        hint = 0.3 if fl["gap"] > 0 else -0.3
        report.add("favorite_longshot", f"favorite_longshot {fl}", hint=hint, conf_mult=1.1)
        report.features["favorite_longshot"] = fl

    # 4. Cross-market parity
    cm = _cross_market_gap(market_price, peer_prices)
    if cm:
        hint = 0.5 if cm["gap"] > 0 else -0.5
        report.add("cross_market_gap", f"cross_market_gap {cm}", hint=hint, conf_mult=1.15)
        report.features["cross_market_gap"] = cm

    # 5. Smart-money disagreement
    sm = _smart_money_disagreement(leader_trades, market_price)
    if sm:
        hint = 0.4 if sm["gap"] > 0 else -0.4
        report.add("smart_money", f"smart_money {sm}", hint=hint, conf_mult=1.1)
        report.features["smart_money"] = sm

    # 6. Recency bias / sentiment without news
    rb = _recency_bias(news_volume_24h, snaps)
    if rb:
        # Sentiment-only move → mean reversion expected
        hint = -0.3 if rb["move"] > 0 else 0.3
        report.add("recency_bias", f"recency_bias {rb}", hint=hint, conf_mult=0.95)
        report.features["recency_bias"] = rb

    # 7. Narrative staleness
    ns = _narrative_staleness(snaps, hours_since_news)
    if ns:
        hint = -0.2 if ns["drift"] > 0 else 0.2
        report.add("narrative_stale", f"narrative_stale {ns}", hint=hint, conf_mult=0.95)
        report.features["narrative_stale"] = ns

    # 8. Sentiment overlay (cheap signal — small confidence bump only)
    if sentiment_score is not None and abs(sentiment_score) >= 0.3:
        hint = 0.1 if sentiment_score > 0 else -0.1
        report.add(
            f"sentiment:{'pos' if sentiment_score > 0 else 'neg'}",
            f"headline_sentiment={sentiment_score:.2f}",
            hint=hint,
            conf_mult=1.02,
        )
        report.features["sentiment_score"] = sentiment_score

    return report
