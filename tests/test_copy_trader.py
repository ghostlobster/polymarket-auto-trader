"""CopyTraderAgent integration test using stubs for data + execution."""
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from agents.copy_trader import CopyTraderAgent
from config import Settings
from database import init_db
from models import OrderBook, PriceLevel, TrackedTrader


class FakeData:
    def __init__(self, events_by_wallet):
        self._events = events_by_wallet

    async def get_users_activity(self, wallets, cursors):
        return {w: self._events.get(w, []) for w in wallets}


class FakePoly:
    def __init__(self, book):
        self._book = book

    async def get_orderbook(self, token_id):
        return self._book

    async def get_balance_usdc(self):
        return 1000.0


@pytest_asyncio.fixture
async def settings(tmp_path):
    s = Settings(
        anthropic_api_key="test",
        db_path=str(tmp_path / "test.db"),
        copy_enabled=True,
        copy_default_preset="scaled_market",
        copy_paper_starting_usdc=1000,
        max_position_usdc=50,
    )
    return s


@pytest.mark.asyncio
async def test_copy_paper_records_leader_trade_and_order(settings, tmp_path):
    db = await init_db(settings.db_path)
    # Seed a trader in 'paper' status
    trader = TrackedTrader(
        wallet="0xleader",
        status="paper",
        preset="scaled_market",
        score=80.0,
        sample_size=100,
        last_seen_ts=0,
    )
    await db.upsert_tracked_trader(trader)

    book = OrderBook(
        token_id="tok1",
        bids=[PriceLevel(price=0.48, size=200)],
        asks=[PriceLevel(price=0.50, size=200)],
    )
    poly = FakePoly(book)
    ts = int(datetime.now(timezone.utc).timestamp())
    data = FakeData({
        "0xleader": [{
            "tx_hash": "0xtx1",
            "timestamp": ts,
            "type": "TRADE",
            "side": "BUY",
            "outcome": "YES",
            "condition_id": "cond1",
            "token_id": "tok1",
            "size_usdc": 1000.0,
            "price": 0.50,
        }],
    })

    agent = CopyTraderAgent(settings, data, poly, db, risk=None)
    summary = await agent.cycle()

    assert summary["events"] == 1
    assert summary["copied"] == 1

    leader_trades = await db.get_leader_trades("0xleader")
    assert len(leader_trades) == 1
    lt = leader_trades[0]
    assert lt.expected_copy is True
    assert lt.copy_order_id != ""
    assert lt.copy_mode == "paper"

    paper_orders = await db.get_paper_orders(wallet="0xleader")
    assert len(paper_orders) == 1
    assert paper_orders[0].status == "FILLED"

    refreshed = await db.get_tracked_trader("0xleader")
    assert refreshed.last_seen_ts == ts

    await db.close()


@pytest.mark.asyncio
async def test_copy_dedupes_repeated_event(settings):
    db = await init_db(settings.db_path)
    await db.upsert_tracked_trader(TrackedTrader(
        wallet="0xleader", status="paper", preset="scaled_market",
        score=80.0, sample_size=100,
    ))
    book = OrderBook(
        token_id="tok1",
        bids=[PriceLevel(price=0.48, size=200)],
        asks=[PriceLevel(price=0.50, size=200)],
    )
    ts = int(datetime.now(timezone.utc).timestamp())
    ev = {
        "tx_hash": "0xtx-same", "timestamp": ts, "type": "TRADE",
        "side": "BUY", "outcome": "YES", "condition_id": "cond1",
        "token_id": "tok1", "size_usdc": 1000.0, "price": 0.50,
    }
    data = FakeData({"0xleader": [ev]})
    agent = CopyTraderAgent(settings, data, FakePoly(book), db, risk=None)
    await agent.cycle()
    # Re-run — dedupe should make it a no-op
    summary = await agent.cycle()
    assert summary["events"] == 1
    assert summary["copied"] == 0   # second pass: dedupe

    paper_orders = await db.get_paper_orders(wallet="0xleader")
    assert len(paper_orders) == 1
    await db.close()


@pytest.mark.asyncio
async def test_shadow_status_records_but_does_not_execute(settings):
    db = await init_db(settings.db_path)
    await db.upsert_tracked_trader(TrackedTrader(
        wallet="0xleader", status="shadow", preset="shadow",  # shadow preset
        score=80.0, sample_size=100,
    ))
    book = OrderBook(
        token_id="tok1",
        bids=[PriceLevel(price=0.48, size=200)],
        asks=[PriceLevel(price=0.50, size=200)],
    )
    ts = int(datetime.now(timezone.utc).timestamp())
    ev = {
        "tx_hash": "0xtxshadow", "timestamp": ts, "type": "TRADE",
        "side": "BUY", "outcome": "YES", "condition_id": "cond1",
        "token_id": "tok1", "size_usdc": 1000.0, "price": 0.50,
    }
    data = FakeData({"0xleader": [ev]})
    agent = CopyTraderAgent(settings, data, FakePoly(book), db, risk=None)
    await agent.cycle()

    leader_trades = await db.get_leader_trades("0xleader")
    assert len(leader_trades) == 1
    assert leader_trades[0].copy_order_id == ""
    assert leader_trades[0].skip_reason == "shadow_mode"
    paper_orders = await db.get_paper_orders(wallet="0xleader")
    assert paper_orders == []
    await db.close()
