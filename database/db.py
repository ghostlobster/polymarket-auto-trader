import json
from datetime import datetime
from pathlib import Path

import aiosqlite

from models import AgentMessage, Order, PortfolioSnapshot, Position, Signal


class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        schema = Path(__file__).parent / "schema.sql"
        await self._conn.executescript(schema.read_text())
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    # --- Signals ---

    async def save_signal(self, signal: Signal) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO signals
               (id, market_id, question, token_id, side, strength, estimated_probability,
                market_price, edge, rationale, research_summary, confidence, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal.id, signal.market_id, signal.question, signal.token_id,
             signal.side, signal.strength.value, signal.estimated_probability,
             signal.market_price, signal.edge, signal.rationale,
             signal.research_summary, signal.confidence,
             signal.created_at.isoformat()),
        )
        await self._conn.commit()

    async def get_recent_signals(self, limit: int = 20) -> list[Signal]:
        async with self._conn.execute(
            "SELECT * FROM signals ORDER BY created_at DESC LIMIT ?", (limit,)
        ) as cur:
            rows = await cur.fetchall()
        return [Signal(**dict(r)) for r in rows]

    # --- Orders ---

    async def save_order(self, order: Order) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO orders
               (id, market_id, signal_id, token_id, side, size_usdc, price, order_type,
                status, placed_at, filled_at, fill_price, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order.id, order.market_id, order.signal_id, order.token_id,
             order.side.value, order.size_usdc, order.price, order.order_type.value,
             order.status.value,
             order.placed_at.isoformat() if order.placed_at else None,
             order.filled_at.isoformat() if order.filled_at else None,
             order.fill_price, order.error),
        )
        await self._conn.commit()

    async def get_open_orders(self) -> list[Order]:
        async with self._conn.execute(
            "SELECT * FROM orders WHERE status IN ('PENDING','OPEN')"
        ) as cur:
            rows = await cur.fetchall()
        return [Order(**dict(r)) for r in rows]

    # --- Positions ---

    async def save_position(self, position: Position) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO positions
               (id, market_id, question, token_id, side, size, avg_price,
                current_price, unrealized_pnl, realized_pnl, opened_at, closed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (position.id, position.market_id, position.question, position.token_id,
             position.side, position.size, position.avg_price, position.current_price,
             position.unrealized_pnl, position.realized_pnl,
             position.opened_at.isoformat() if position.opened_at else None,
             position.closed_at.isoformat() if position.closed_at else None),
        )
        await self._conn.commit()

    async def get_open_positions(self) -> list[Position]:
        async with self._conn.execute(
            "SELECT * FROM positions WHERE closed_at IS NULL"
        ) as cur:
            rows = await cur.fetchall()
        return [Position(**dict(r)) for r in rows]

    # --- Agent messages ---

    async def save_message(self, msg: AgentMessage) -> None:
        await self._conn.execute(
            """INSERT INTO agent_messages
               (id, from_agent, to_agent, msg_type, payload_json, created_at)
               VALUES (?,?,?,?,?,?)""",
            (msg.id, msg.from_agent, msg.to_agent, msg.msg_type,
             json.dumps(msg.payload), msg.created_at.isoformat()),
        )
        await self._conn.commit()

    # --- P&L snapshots ---

    async def save_pnl_snapshot(self, snapshot: PortfolioSnapshot) -> None:
        await self._conn.execute(
            """INSERT INTO pnl_snapshots
               (total_usdc, available_usdc, open_positions, realized_pnl, unrealized_pnl, snapshot_at)
               VALUES (?,?,?,?,?,?)""",
            (snapshot.total_usdc, snapshot.available_usdc,
             len(snapshot.open_positions), snapshot.realized_pnl,
             snapshot.unrealized_pnl,
             (snapshot.snapshot_at or datetime.utcnow()).isoformat()),
        )
        await self._conn.commit()

    async def get_total_realized_pnl(self) -> float:
        async with self._conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions"
        ) as cur:
            row = await cur.fetchone()
        return float(row[0])


async def init_db(db_path: str) -> Database:
    db = Database(db_path)
    await db.connect()
    return db
