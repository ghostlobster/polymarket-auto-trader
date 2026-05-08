import json
from datetime import datetime
from pathlib import Path

import aiosqlite

from models import AgentMessage, Order, PortfolioSnapshot, Position, Signal

# Idempotent ALTER TABLE migrations for users with pre-existing DBs.
# Each tuple is (table, column, ddl_fragment).
_MIGRATIONS = [
    ("signals", "source", "TEXT DEFAULT 'thesis'"),
    ("signals", "leader_wallet", "TEXT DEFAULT ''"),
    ("signals", "preset", "TEXT DEFAULT ''"),
]


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
        await self._apply_migrations()
        await self._conn.commit()

    async def _apply_migrations(self) -> None:
        for table, column, ddl in _MIGRATIONS:
            async with self._conn.execute(f"PRAGMA table_info({table})") as cur:
                cols = {r[1] for r in await cur.fetchall()}
            if column not in cols:
                await self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"
                )

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    # --- Signals ---

    async def save_signal(self, signal: Signal) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO signals
               (id, market_id, question, token_id, side, strength, estimated_probability,
                market_price, edge, rationale, research_summary, confidence, created_at,
                source, leader_wallet, preset)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal.id, signal.market_id, signal.question, signal.token_id,
             signal.side, signal.strength.value, signal.estimated_probability,
             signal.market_price, signal.edge, signal.rationale,
             signal.research_summary, signal.confidence,
             signal.created_at.isoformat(),
             signal.source, signal.leader_wallet, signal.preset),
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

    # ------------------------------------------------------------------ #
    #  Copy-trading subsystem                                             #
    # ------------------------------------------------------------------ #

    # --- Tracked traders ---

    async def upsert_tracked_trader(self, trader) -> None:
        """Insert or update a TrackedTrader row. Preserves status/preset on update."""
        await self._conn.execute(
            """INSERT INTO tracked_traders
                 (wallet, status, preset, score, sample_size, weeks_profitable_frac,
                  max_drawdown, total_volume_usdc, resolution_sniper_frac,
                  last_seen_ts, last_evaluated_at, notes, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(wallet) DO UPDATE SET
                 score = excluded.score,
                 sample_size = excluded.sample_size,
                 weeks_profitable_frac = excluded.weeks_profitable_frac,
                 max_drawdown = excluded.max_drawdown,
                 total_volume_usdc = excluded.total_volume_usdc,
                 resolution_sniper_frac = excluded.resolution_sniper_frac,
                 last_evaluated_at = excluded.last_evaluated_at,
                 notes = excluded.notes
            """,
            (trader.wallet, trader.status, trader.preset, trader.score,
             trader.sample_size, trader.weeks_profitable_frac,
             trader.max_drawdown, trader.total_volume_usdc,
             trader.resolution_sniper_frac,
             trader.last_seen_ts,
             (trader.last_evaluated_at or datetime.utcnow()).isoformat(),
             trader.notes,
             (trader.created_at or datetime.utcnow()).isoformat()),
        )
        await self._conn.commit()

    async def set_trader_status(self, wallet: str, status: str) -> None:
        await self._conn.execute(
            "UPDATE tracked_traders SET status=? WHERE wallet=?", (status, wallet)
        )
        await self._conn.commit()

    async def set_trader_preset(self, wallet: str, preset: str) -> None:
        await self._conn.execute(
            "UPDATE tracked_traders SET preset=? WHERE wallet=?", (preset, wallet)
        )
        await self._conn.commit()

    async def set_trader_last_seen(self, wallet: str, ts: int) -> None:
        await self._conn.execute(
            "UPDATE tracked_traders SET last_seen_ts=? WHERE wallet=?", (ts, wallet)
        )
        await self._conn.commit()

    async def get_tracked_trader(self, wallet: str):
        from models import TrackedTrader
        async with self._conn.execute(
            "SELECT * FROM tracked_traders WHERE wallet=?", (wallet,)
        ) as cur:
            row = await cur.fetchone()
        return TrackedTrader(**dict(row)) if row else None

    async def get_tracked_traders(self, statuses: tuple[str, ...] | None = None):
        from models import TrackedTrader
        if statuses:
            placeholders = ",".join("?" * len(statuses))
            sql = f"SELECT * FROM tracked_traders WHERE status IN ({placeholders}) ORDER BY score DESC"
            args = statuses
        else:
            sql = "SELECT * FROM tracked_traders ORDER BY score DESC"
            args = ()
        async with self._conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [TrackedTrader(**dict(r)) for r in rows]

    async def get_active_tracked_traders(self):
        """Traders eligible for the copy loop (shadow/paper/live)."""
        return await self.get_tracked_traders(("shadow", "paper", "live"))

    # --- Leader trades ---

    async def record_leader_trade(self, lt) -> bool:
        """Insert a LeaderTrade. Returns True if inserted, False if duplicate."""
        try:
            await self._conn.execute(
                """INSERT INTO leader_trades
                     (wallet, tx_hash, condition_id, token_id, side, outcome,
                      size_usdc, price, observed_at, expected_copy, copy_order_id,
                      copy_mode, skip_reason, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (lt.wallet, lt.tx_hash, lt.condition_id, lt.token_id, lt.side,
                 lt.outcome, lt.size_usdc, lt.price,
                 lt.observed_at.isoformat(), int(lt.expected_copy),
                 lt.copy_order_id, lt.copy_mode, lt.skip_reason,
                 (lt.created_at or datetime.utcnow()).isoformat()),
            )
            await self._conn.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def update_leader_trade_copy(
        self, wallet: str, tx_hash: str, copy_order_id: str, copy_mode: str
    ) -> None:
        await self._conn.execute(
            """UPDATE leader_trades
                 SET copy_order_id=?, copy_mode=?
               WHERE wallet=? AND tx_hash=?""",
            (copy_order_id, copy_mode, wallet, tx_hash),
        )
        await self._conn.commit()

    async def get_leader_trades(self, wallet: str, limit: int = 50):
        from models import LeaderTrade
        async with self._conn.execute(
            "SELECT * FROM leader_trades WHERE wallet=? ORDER BY observed_at DESC LIMIT ?",
            (wallet, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [LeaderTrade(**dict(r)) for r in rows]

    async def get_unaudited_expected_trades(self, wallet: str, before_ts: str):
        """LeaderTrades that were expected to copy but have no copy_order_id, observed before cutoff."""
        from models import LeaderTrade
        async with self._conn.execute(
            """SELECT * FROM leader_trades
               WHERE wallet=? AND expected_copy=1 AND copy_order_id='' AND observed_at < ?""",
            (wallet, before_ts),
        ) as cur:
            rows = await cur.fetchall()
        return [LeaderTrade(**dict(r)) for r in rows]

    # --- Paper orders / positions ---

    async def save_paper_order(self, order) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO paper_orders
                 (id, wallet, market_id, signal_id, token_id, side, size_usdc, price,
                  order_type, status, placed_at, filled_at, fill_price, error, leader_tx_hash)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (order.id, order.wallet, order.market_id, order.signal_id, order.token_id,
             order.side, order.size_usdc, order.price, order.order_type, order.status,
             order.placed_at.isoformat() if order.placed_at else None,
             order.filled_at.isoformat() if order.filled_at else None,
             order.fill_price, order.error, order.leader_tx_hash),
        )
        await self._conn.commit()

    async def save_paper_position(self, pos) -> None:
        await self._conn.execute(
            """INSERT OR REPLACE INTO paper_positions
                 (id, wallet, market_id, question, token_id, side, size, avg_price,
                  current_price, unrealized_pnl, realized_pnl, opened_at, closed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (pos.id, pos.wallet, pos.market_id, pos.question, pos.token_id, pos.side,
             pos.size, pos.avg_price, pos.current_price, pos.unrealized_pnl,
             pos.realized_pnl,
             pos.opened_at.isoformat() if pos.opened_at else None,
             pos.closed_at.isoformat() if pos.closed_at else None),
        )
        await self._conn.commit()

    async def get_paper_positions(self, wallet: str | None = None, open_only: bool = False):
        from models import PaperPosition
        clauses, args = [], []
        if wallet is not None:
            clauses.append("wallet=?")
            args.append(wallet)
        if open_only:
            clauses.append("closed_at IS NULL")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        async with self._conn.execute(
            f"SELECT * FROM paper_positions{where}", args
        ) as cur:
            rows = await cur.fetchall()
        return [PaperPosition(**dict(r)) for r in rows]

    async def get_paper_position_for_market(self, wallet: str, token_id: str):
        from models import PaperPosition
        async with self._conn.execute(
            """SELECT * FROM paper_positions
               WHERE wallet=? AND token_id=? AND closed_at IS NULL
               ORDER BY opened_at DESC LIMIT 1""",
            (wallet, token_id),
        ) as cur:
            row = await cur.fetchone()
        return PaperPosition(**dict(row)) if row else None

    async def get_paper_orders(self, wallet: str | None = None, limit: int = 100):
        from models import PaperOrder
        if wallet:
            sql = "SELECT * FROM paper_orders WHERE wallet=? ORDER BY placed_at DESC LIMIT ?"
            args = (wallet, limit)
        else:
            sql = "SELECT * FROM paper_orders ORDER BY placed_at DESC LIMIT ?"
            args = (limit,)
        async with self._conn.execute(sql, args) as cur:
            rows = await cur.fetchall()
        return [PaperOrder(**dict(r)) for r in rows]

    # --- Copy performance ---

    async def upsert_copy_performance(self, perf) -> None:
        await self._conn.execute(
            """INSERT INTO copy_performance
                 (wallet, mode, trades_observed, trades_copied, copy_hit_rate,
                  audit_miss_rate, realized_pnl, unrealized_pnl, win_count, loss_count,
                  notes, last_updated)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(wallet, mode) DO UPDATE SET
                 trades_observed = excluded.trades_observed,
                 trades_copied   = excluded.trades_copied,
                 copy_hit_rate   = excluded.copy_hit_rate,
                 audit_miss_rate = excluded.audit_miss_rate,
                 realized_pnl    = excluded.realized_pnl,
                 unrealized_pnl  = excluded.unrealized_pnl,
                 win_count       = excluded.win_count,
                 loss_count      = excluded.loss_count,
                 notes           = excluded.notes,
                 last_updated    = excluded.last_updated
            """,
            (perf.wallet, perf.mode, perf.trades_observed, perf.trades_copied,
             perf.copy_hit_rate, perf.audit_miss_rate, perf.realized_pnl,
             perf.unrealized_pnl, perf.win_count, perf.loss_count, perf.notes,
             (perf.last_updated or datetime.utcnow()).isoformat()),
        )
        await self._conn.commit()

    async def get_copy_performance(self, wallet: str, mode: str):
        from models import CopyPerformance
        async with self._conn.execute(
            "SELECT * FROM copy_performance WHERE wallet=? AND mode=?",
            (wallet, mode),
        ) as cur:
            row = await cur.fetchone()
        return CopyPerformance(**dict(row)) if row else None

    async def get_all_copy_performance(self):
        from models import CopyPerformance
        async with self._conn.execute(
            "SELECT * FROM copy_performance ORDER BY realized_pnl DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [CopyPerformance(**dict(r)) for r in rows]

    # --- Audit alerts ---

    async def record_audit_alert(self, wallet: str, leader_tx_hash: str, reason: str) -> None:
        await self._conn.execute(
            """INSERT INTO audit_alerts (wallet, leader_tx_hash, reason, created_at)
               VALUES (?,?,?,?)""",
            (wallet, leader_tx_hash, reason, datetime.utcnow().isoformat()),
        )
        await self._conn.commit()

    async def get_audit_alerts(self, wallet: str, limit: int = 50):
        async with self._conn.execute(
            """SELECT wallet, leader_tx_hash, reason, created_at
               FROM audit_alerts WHERE wallet=? ORDER BY created_at DESC LIMIT ?""",
            (wallet, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # --- Web UI users ---

    async def upsert_web_user(
        self,
        provider: str,
        provider_id: str,
        email: str | None,
        name: str | None,
        avatar_url: str | None,
        is_allowed: int,
    ) -> tuple[int, int]:
        now = datetime.utcnow().isoformat()
        async with self._conn.execute(
            """INSERT INTO web_users
                 (provider, provider_id, email, name, avatar_url, is_allowed, created_at, last_login)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(provider, provider_id) DO UPDATE SET
                 email=excluded.email,
                 name=excluded.name,
                 avatar_url=excluded.avatar_url,
                 last_login=excluded.last_login,
                 is_allowed=MAX(is_allowed, excluded.is_allowed)
               RETURNING id, is_allowed""",
            (provider, provider_id, email, name, avatar_url, is_allowed, now, now),
        ) as cur:
            row = await cur.fetchone()
        await self._conn.commit()
        return row[0], row[1]

    async def get_web_user_by_id(self, user_id: int) -> dict | None:
        async with self._conn.execute(
            "SELECT * FROM web_users WHERE id=?", (user_id,)
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None


async def init_db(db_path: str) -> Database:
    db = Database(db_path)
    await db.connect()
    return db
