"""Tests for the deterministic bias detector."""

from datetime import datetime, timedelta, timezone

from research import detect_biases


def _ts(seconds_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()


def _snap(mid: float, t: int, bid_5c: float = 50.0, ask_5c: float = 50.0):
    return {
        "ts": _ts(t),
        "mid": mid,
        "bid_depth_1c": bid_5c / 2,
        "bid_depth_5c": bid_5c,
        "ask_depth_1c": ask_5c / 2,
        "ask_depth_5c": ask_5c,
    }


def test_empty_inputs_produce_no_tags():
    rep = detect_biases(market_price=0.5)
    assert rep.tags == []
    assert rep.confidence_modifier == 1.0


def test_panic_cascade_flags_when_price_drops_with_bid_depth_collapse():
    # Price drops from 0.60 to 0.45 in last few minutes; bids vanish on the way down.
    snaps = [
        _snap(0.45, 60, bid_5c=5),  # newest
        _snap(0.48, 120, bid_5c=10),
        _snap(0.55, 180, bid_5c=40),
        _snap(0.60, 240, bid_5c=50),
        _snap(0.60, 300, bid_5c=50),
        _snap(0.60, 360, bid_5c=50),  # oldest
    ]
    rep = detect_biases(market_price=0.45, snapshots=snaps)
    assert "panic_cascade" in rep.tags
    # Panic-down implies mean-reversion BUY → positive directional hint
    assert rep.directional_hint > 0


def test_cross_market_gap_tagged_when_peers_disagree():
    rep = detect_biases(market_price=0.40, peer_prices={"manifold": 0.62, "metaculus": 0.58})
    assert "cross_market_gap" in rep.tags
    assert rep.features["cross_market_gap"]["gap"] > 0


def test_no_cross_market_tag_when_within_5c():
    rep = detect_biases(market_price=0.40, peer_prices={"manifold": 0.42, "metaculus": 0.43})
    assert "cross_market_gap" not in rep.tags


def test_smart_money_disagreement_tagged():
    leader_trades = [
        {"side": "BUY", "outcome": "YES", "size_usdc": 500, "price": 0.55},
        {"side": "BUY", "outcome": "YES", "size_usdc": 300, "price": 0.56},
        {"side": "BUY", "outcome": "NO", "size_usdc": 100, "price": 0.45},
    ]
    rep = detect_biases(market_price=0.42, leader_trades=leader_trades)
    assert "smart_money" in rep.tags
    assert rep.features["smart_money"]["gap"] > 0


def test_favorite_longshot_uses_calibration_buckets():
    buckets = [
        {
            "source": "thesis",
            "category": "all",
            "band_low": 0.7,
            "band_high": 0.8,
            "n": 30,
            "mean_predicted": 0.75,
            "mean_actual": 0.60,
            "brier": 0.05,
            "log_loss": 0.4,
        },
    ]
    rep = detect_biases(market_price=0.75, calibration_buckets=buckets)
    assert "favorite_longshot" in rep.tags
    assert rep.features["favorite_longshot"]["gap"] < 0


def test_recency_bias_flagged_when_move_without_news():
    snaps = [
        _snap(0.60, 60),
        _snap(0.55, 600),
    ]
    rep = detect_biases(market_price=0.60, snapshots=snaps, news_volume_24h=0)
    assert "recency_bias" in rep.tags


def test_recency_bias_not_flagged_with_news():
    snaps = [
        _snap(0.60, 60),
        _snap(0.55, 600),
    ]
    rep = detect_biases(market_price=0.60, snapshots=snaps, news_volume_24h=20)
    assert "recency_bias" not in rep.tags
