"""Strategy preset tests — pure logic, no I/O."""
from datetime import datetime, timedelta, timezone

import pytest

from copytrader.strategies import PRESETS, apply_preset, get_preset
from models import OrderBook, PriceLevel


def _ev(**overrides):
    base = {
        "tx_hash": "0xabc",
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
        "type": "TRADE",
        "side": "BUY",
        "outcome": "YES",
        "condition_id": "cond1",
        "token_id": "tok1",
        "size_usdc": 1000.0,
        "price": 0.55,
    }
    base.update(overrides)
    return base


def _book(best_ask=0.55):
    return OrderBook(
        token_id="tok1",
        bids=[PriceLevel(price=best_ask - 0.02, size=200)],
        asks=[PriceLevel(price=best_ask, size=200)],
    )


def test_get_preset_fallback():
    assert get_preset("nonexistent").name == "scaled_market"


def test_scaled_market_default():
    d = apply_preset(
        preset=PRESETS["scaled_market"], leader_event=_ev(), book=_book(),
        available_usdc=10_000, max_position_usdc=50.0,
    )
    assert d.skip is False
    assert d.order_type == "market"
    assert d.expected_copy is True
    # 1000 * 0.01 = 10, capped under max_position_usdc=50
    assert d.size_usdc == pytest.approx(10.0)


def test_below_min_notional():
    d = apply_preset(
        preset=PRESETS["scaled_market"],
        leader_event=_ev(size_usdc=50),  # below default min 200
        book=_book(),
        available_usdc=10_000, max_position_usdc=50.0,
    )
    assert d.skip is True
    assert "leader_notional_below_min" in d.reason


def test_stale_event_skipped():
    old = int((datetime.now(timezone.utc) - timedelta(minutes=10)).timestamp())
    d = apply_preset(
        preset=PRESETS["scaled_market"], leader_event=_ev(timestamp=old),
        book=_book(), available_usdc=10_000, max_position_usdc=50.0,
    )
    assert d.skip is True
    assert d.reason.startswith("stale:")


def test_slippage_skip_marks_expected():
    # ask is way above leader fill — slippage triggers, but it's an expected miss
    d = apply_preset(
        preset=PRESETS["scaled_market"],
        leader_event=_ev(price=0.50),
        book=_book(best_ask=0.62),  # 0.62 > 0.50 * 1.03
        available_usdc=10_000, max_position_usdc=50.0,
    )
    assert d.skip is True
    assert d.reason.startswith("slippage:")
    assert d.expected_copy is True


def test_shadow_never_executes():
    d = apply_preset(
        preset=PRESETS["shadow"], leader_event=_ev(),
        book=_book(), available_usdc=10_000, max_position_usdc=50.0,
    )
    assert d.skip is True
    assert d.expected_copy is False
    assert d.reason == "shadow_mode"


def test_scaled_limit_uses_leader_price():
    d = apply_preset(
        preset=PRESETS["scaled_limit"],
        leader_event=_ev(price=0.42),
        book=_book(best_ask=0.42),
        available_usdc=10_000, max_position_usdc=50.0,
    )
    assert d.skip is False
    assert d.order_type == "limit"
    assert d.limit_price == pytest.approx(0.42)


def test_conservative_blocks_imminent_resolution():
    resolves = datetime.now(timezone.utc) + timedelta(hours=24)  # < 7d
    d = apply_preset(
        preset=PRESETS["conservative"], leader_event=_ev(),
        book=_book(), available_usdc=10_000, max_position_usdc=50.0,
        market_resolves_at=resolves,
    )
    assert d.skip is True
    assert "resolves_in_" in d.reason


def test_size_clamped_by_available_usdc():
    d = apply_preset(
        preset=PRESETS["mirror"], leader_event=_ev(size_usdc=1_000_000),
        book=_book(), available_usdc=20.0, max_position_usdc=10_000.0,
    )
    # mirror scale = 1.0, but available is 20 and the size must be ≥5
    assert d.skip is False
    assert d.size_usdc == pytest.approx(20.0)
