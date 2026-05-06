"""PaperBroker tests — uses an in-memory DB and a fake PolymarketClient."""
from dataclasses import dataclass

import pytest
import pytest_asyncio

from database import init_db
from models import OrderBook, OrderSide, PriceLevel
from polymarket.paper_broker import PaperBroker


class FakePoly:
    def __init__(self, books: dict[str, OrderBook]):
        self.books = books

    async def get_orderbook(self, token_id: str) -> OrderBook:
        return self.books.get(token_id, OrderBook(token_id=token_id))


@pytest_asyncio.fixture
async def db(tmp_path):
    database = await init_db(str(tmp_path / "test.db"))
    yield database
    await database.close()


def _book(token_id="t1", asks=None, bids=None):
    return OrderBook(
        token_id=token_id,
        bids=[PriceLevel(**b) for b in (bids or [])],
        asks=[PriceLevel(**a) for a in (asks or [])],
    )


@pytest.mark.asyncio
async def test_market_buy_walks_book(db):
    poly = FakePoly({
        "t1": _book(asks=[
            {"price": 0.50, "size": 10},   # 5 USDC capacity
            {"price": 0.52, "size": 100},  # plenty
        ]),
    })
    broker = PaperBroker(poly, db, wallet_label="0xLEAD", starting_usdc=1000)

    order = await broker.place_market_order(
        token_id="t1", side=OrderSide.BUY, size_usdc=20, market_id="m1",
    )
    assert order.status == "FILLED"
    # 5 usdc @ 0.50 -> 10 shares; remaining 15 usdc @ 0.52 -> 28.8462 shares
    # VWAP = 20 / (10 + 28.846) ≈ 0.5148
    assert 0.51 < order.price < 0.518
    positions = await db.get_paper_positions(wallet="0xLEAD")
    assert len(positions) == 1
    assert positions[0].size == pytest.approx(10 + (15 / 0.52), rel=1e-3)


@pytest.mark.asyncio
async def test_limit_buy_skips_above_limit(db):
    poly = FakePoly({
        "t1": _book(asks=[{"price": 0.60, "size": 100}]),
    })
    broker = PaperBroker(poly, db, wallet_label="0xLEAD", starting_usdc=1000)
    order = await broker.place_limit_order(
        token_id="t1", side=OrderSide.BUY, size_usdc=20, price=0.50, market_id="m1",
    )
    assert order.status == "FAILED"
    assert order.error == "no_fill_within_limit"
    assert (await db.get_paper_positions(wallet="0xLEAD")) == []


@pytest.mark.asyncio
async def test_sell_realizes_pnl(db):
    poly = FakePoly({
        "t1": _book(
            asks=[{"price": 0.50, "size": 100}],
            bids=[{"price": 0.65, "size": 100}],
        ),
    })
    broker = PaperBroker(poly, db, wallet_label="0xLEAD", starting_usdc=1000)
    await broker.place_market_order(
        token_id="t1", side=OrderSide.BUY, size_usdc=10, market_id="m1",
    )  # buys 20 shares @ 0.50

    # Now sell against the bid side
    sell = await broker.place_market_order(
        token_id="t1", side=OrderSide.SELL, size_usdc=13, market_id="m1",
    )
    assert sell.status == "FILLED"
    positions = await db.get_paper_positions(wallet="0xLEAD")
    p = positions[0]
    # 13 USDC at 0.65 = 20 shares sold (entire position closed)
    assert p.closed_at is not None
    assert p.realized_pnl == pytest.approx((0.65 - 0.50) * 20, rel=1e-3)


@pytest.mark.asyncio
async def test_balance_tracks_open_cost_basis(db):
    poly = FakePoly({
        "t1": _book(asks=[{"price": 0.40, "size": 100}]),
    })
    broker = PaperBroker(poly, db, wallet_label="0xLEAD", starting_usdc=100)
    await broker.place_market_order(
        token_id="t1", side=OrderSide.BUY, size_usdc=20, market_id="m1",
    )
    bal = await broker.get_balance_usdc()
    assert bal == pytest.approx(80.0, rel=1e-3)
