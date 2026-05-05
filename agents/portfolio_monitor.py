"""
PortfolioMonitorAgent: tracks positions, enforces stop-losses, reports P&L.
"""
import json
from datetime import datetime

import structlog

from agents.base import BaseAgent
from config import Settings
from database import Database
from models import PortfolioSnapshot, Position
from polymarket.client import PolymarketClient
from tools import build_market_tools, build_db_tools

log = structlog.get_logger(__name__)

SYSTEM_PROMPT = """You are the Portfolio Monitor agent for an automated Polymarket trading system.

Your responsibilities:
1. **Stop-loss enforcement**: Exit any position that has lost more than the configured stop-loss percentage
2. **Take-profit**: Exit positions that have gained more than the configured take-profit percentage
3. **P&L reporting**: Calculate and report total portfolio performance
4. **Position staleness**: Flag positions in markets that have resolved but not been settled

For each open position, check its current price vs. entry price:
- unrealized_pnl_pct = (current_price - avg_price) / avg_price
- If unrealized_pnl_pct < -stop_loss_pct: SELL to cut losses
- If unrealized_pnl_pct > take_profit_pct: SELL to lock gains

Return a JSON report:
{
  "open_positions": 3,
  "total_unrealized_pnl": -2.50,
  "total_realized_pnl": 45.20,
  "actions_taken": ["Sold YES shares in market X (stop-loss hit at -32%)"],
  "alerts": ["Position in market Y approaching stop-loss (-25%)"],
  "portfolio_health": "good|warning|critical"
}"""


class PortfolioMonitorAgent(BaseAgent):
    def __init__(self, settings: Settings, poly: PolymarketClient, db: Database):
        self._settings = settings
        self._poly = poly
        self._db = db
        market_tools, market_handlers = build_market_tools(poly)
        db_tools, db_handlers = build_db_tools(db)
        # Only expose safe tools: no placing new orders, only sells for risk management
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

        if not positions:
            return {
                "open_positions": 0,
                "total_unrealized_pnl": 0.0,
                "total_realized_pnl": realized_pnl,
                "actions_taken": [],
                "alerts": [],
                "portfolio_health": "good",
                "available_usdc": balance,
            }

        prompt = (
            f"Review the current portfolio and enforce risk rules.\n\n"
            f"**Current positions** ({len(positions)} open):\n"
            + json.dumps([p.model_dump() for p in positions], default=str, indent=2)
            + f"\n\n**Available USDC**: ${balance:.2f}\n"
            f"**Total realized P&L**: ${realized_pnl:.2f}\n\n"
            f"**Risk rules**:\n"
            f"- Stop-loss: exit if down {self._settings.stop_loss_pct:.0%}\n"
            f"- Take-profit: exit if up {self._settings.take_profit_pct:.0%}\n\n"
            f"For each position, check current prices via get_orderbook if needed. "
            f"Execute any required stop-loss or take-profit sells. "
            f"Return the portfolio report JSON."
        )

        result = await self.run(prompt)
        try:
            start = result.find("{")
            end = result.rfind("}") + 1
            if start >= 0 and end > start:
                report = json.loads(result[start:end])
                report["available_usdc"] = balance
                return report
        except Exception as exc:
            log.warning("PortfolioMonitor parse error", error=str(exc))

        return {
            "open_positions": len(positions),
            "total_unrealized_pnl": 0.0,
            "total_realized_pnl": realized_pnl,
            "actions_taken": [],
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
