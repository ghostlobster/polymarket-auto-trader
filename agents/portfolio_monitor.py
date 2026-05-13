"""
PortfolioMonitorAgent: enforces stop-loss / take-profit / theta gates and
reports P&L.

The deterministic pre-LLM gate evaluates each position against hard rules:
  - −stop_loss_pct  → exit
  - +take_profit_pct → exit
  - <theta_force_close_hours to resolution → exit
  - inside theta_window_hours AND up theta_take_profit_pct → exit

Only positions that survive the deterministic gate are passed to the LLM for
nuanced thesis-invalidation review. This stops the LLM from accidentally
holding through a deterministic stop-loss when it tool-loops out.
"""

import json
from datetime import datetime, timezone

import structlog

from agents.base import BaseAgent
from config import Settings
from database import Database
from models import PortfolioSnapshot, Position
from polymarket.client import PolymarketClient
from tools import build_db_tools, build_market_tools

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are the Portfolio Monitor agent for an automated Polymarket trading system.

Your responsibilities:
1. Review positions flagged for LLM analysis after deterministic gates ran.
2. Decide whether new market information has invalidated the original thesis.
3. P&L reporting and portfolio health snapshot.

Return ONLY a JSON report:
{
  "open_positions": 3,
  "total_unrealized_pnl": -2.50,
  "total_realized_pnl": 45.20,
  "actions_taken": ["..."],
  "alerts": ["..."],
  "portfolio_health": "good|warning|critical"
}"""


def _parse_iso(ts: str) -> datetime | None:
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


def evaluate_position_gates(
    position: Position,
    settings: Settings,
    market_resolves_at: str | None = None,
    now: datetime | None = None,
) -> dict:
    """
    Pure deterministic gate evaluator. Returns:
      {"action": "exit"|"hold", "reason": "...", "pnl_pct": float}

    Used by the monitor and exposed for unit tests.
    """
    now = now or datetime.now(timezone.utc)
    pnl_pct = (
        (position.current_price - position.avg_price) / position.avg_price
        if position.avg_price > 0
        else 0.0
    )

    if pnl_pct <= -abs(settings.stop_loss_pct):
        return {"action": "exit", "reason": f"stop_loss:{pnl_pct:.2%}", "pnl_pct": pnl_pct}
    if pnl_pct >= abs(settings.take_profit_pct):
        return {"action": "exit", "reason": f"take_profit:{pnl_pct:.2%}", "pnl_pct": pnl_pct}

    resolves = _parse_iso(market_resolves_at or "")
    if resolves is not None:
        hours_left = (resolves - now).total_seconds() / 3600.0
        if hours_left <= settings.theta_force_close_hours:
            return {
                "action": "exit",
                "reason": f"theta_force_close:{hours_left:.1f}h",
                "pnl_pct": pnl_pct,
            }
        if hours_left <= settings.theta_window_hours and pnl_pct >= settings.theta_take_profit_pct:
            return {
                "action": "exit",
                "reason": f"theta_window_tp:{pnl_pct:.2%}@{hours_left:.1f}h",
                "pnl_pct": pnl_pct,
            }

    return {"action": "hold", "reason": "within_bounds", "pnl_pct": pnl_pct}


class PortfolioMonitorAgent(BaseAgent):
    def __init__(self, settings: Settings, poly: PolymarketClient, db: Database):
        self._settings = settings
        self._poly = poly
        self._db = db
        market_tools, market_handlers = build_market_tools(poly)
        db_tools, db_handlers = build_db_tools(db)
        allowed_market = {"get_positions", "get_balance", "get_orderbook", "place_market_order"}
        tools = [t for t in market_tools if t["name"] in allowed_market] + db_tools
        handlers = {k: v for k, v in market_handlers.items() if k in allowed_market}
        handlers.update(db_handlers)
        super().__init__(
            name="PortfolioMonitor",
            model=self.MODEL_SONNET,
            tools=tools,
            handlers=handlers,
            system_prompt=SYSTEM_PROMPT,
            max_tokens=3096,
        )

    async def check(self) -> dict:
        """Run portfolio health check. Returns report dict."""
        positions = await self._poly.get_positions()
        balance = await self._poly.get_balance_usdc()
        realized_pnl = await self._db.get_total_realized_pnl()

        actions_taken: list[str] = []
        survivors: list[Position] = []

        for pos in positions:
            resolves_at = await self._resolves_at(pos.market_id)
            gate = evaluate_position_gates(pos, self._settings, resolves_at)
            if gate["action"] == "exit":
                ok = await self._exit_position(pos, reason=gate["reason"])
                if ok:
                    actions_taken.append(
                        f"deterministic_exit market={pos.market_id[:10]} reason={gate['reason']}"
                    )
                    continue
            survivors.append(pos)

        if not survivors:
            return {
                "open_positions": 0,
                "total_unrealized_pnl": 0.0,
                "total_realized_pnl": realized_pnl,
                "actions_taken": actions_taken,
                "alerts": [],
                "portfolio_health": "good",
                "available_usdc": balance,
            }

        prompt = (
            f"Review the surviving portfolio. Deterministic gates already ran; you handle thesis-invalidation.\n\n"
            f"**Positions** ({len(survivors)} open):\n"
            + json.dumps([p.model_dump() for p in survivors], default=str, indent=2)
            + f"\n\n**Available USDC**: ${balance:.2f}\n"
            f"**Total realized P&L**: ${realized_pnl:.2f}\n"
            f"**Deterministic actions this cycle**: {actions_taken or 'none'}\n\n"
            f"Return the portfolio report JSON."
        )

        result = await self.run(prompt)
        try:
            start = result.find("{")
            end = result.rfind("}") + 1
            if start >= 0 and end > start:
                report = json.loads(result[start:end])
                report.setdefault("actions_taken", []).extend(actions_taken)
                report["available_usdc"] = balance
                return report
        except (json.JSONDecodeError, ValueError) as exc:
            log.warning("PortfolioMonitor parse error", error=str(exc))

        return {
            "open_positions": len(survivors),
            "total_unrealized_pnl": 0.0,
            "total_realized_pnl": realized_pnl,
            "actions_taken": actions_taken,
            "alerts": [],
            "portfolio_health": "unknown",
            "available_usdc": balance,
        }

    async def snapshot(self, report: dict) -> PortfolioSnapshot:
        """Build and persist a PortfolioSnapshot from a monitor report."""
        positions = await self._db.get_open_positions()
        snapshot = PortfolioSnapshot(
            total_usdc=report.get("available_usdc", 0) + abs(report.get("total_unrealized_pnl", 0)),
            available_usdc=report.get("available_usdc", 0),
            open_positions=positions,
            realized_pnl=report.get("total_realized_pnl", 0),
            unrealized_pnl=report.get("total_unrealized_pnl", 0),
            snapshot_at=datetime.utcnow(),
        )
        await self._db.save_pnl_snapshot(snapshot)
        return snapshot

    # ------------------------------------------------------------------ #
    #  Helpers                                                           #
    # ------------------------------------------------------------------ #

    async def _resolves_at(self, condition_id: str) -> str | None:
        """Best-effort market resolution timestamp for the theta gate."""
        try:
            raw = await self._poly.get_market(condition_id)
            return raw.get("end_date_iso") if raw else None
        except Exception:
            return None

    async def _exit_position(self, pos: Position, reason: str) -> bool:
        """Place a market-sell of the entire position. Returns True on success."""
        if self._settings.dry_run:
            log.info("DRY RUN — would exit position", market=pos.market_id, reason=reason)
            return True
        from models import OrderSide

        try:
            notional = pos.size * (pos.current_price or pos.avg_price)
            if notional < self._settings.risk_min_trade_usdc:
                return False
            order = await self._poly.place_market_order(
                token_id=pos.token_id,
                side=OrderSide.SELL,
                size_usdc=notional,
                market_id=pos.market_id,
            )
            await self._db.save_order(order)
            await self._db.save_position_postmortem(
                {
                    "position_id": pos.id or pos.market_id,
                    "signal_id": None,
                    "pre_fill_mid": pos.current_price,
                    "fill_vwap": order.price or pos.current_price,
                    "quoted_slippage": None,
                    "drift_5m": None,
                    "drift_30m": None,
                    "drift_120m": None,
                    "exit_reason": reason,
                    "recorded_at": datetime.utcnow().isoformat(),
                }
            )
            log.info("Position exited", market=pos.market_id, reason=reason, size=notional)
            return True
        except Exception as exc:
            log.error("Failed to exit position", market=pos.market_id, error=str(exc))
            return False
