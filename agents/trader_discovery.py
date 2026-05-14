"""
TraderDiscoveryAgent: finds Polymarket wallets with consistent positive performance.

Two-stage filter:
  1. Deterministic — pull leaderboards over multiple windows, intersect by
     consistency (positive in ≥2 windows, min trades, min volume, drawdown cap,
     resolution-sniper guard).
  2. LLM "edge vs luck" — Sonnet looks at the top N candidates and assigns a
     subjective score, returning the top K.

Surviving wallets are upserted into `tracked_traders` with status='discovered'
on first sighting; existing rows have their score/sample_size/etc. refreshed
without disturbing user-set status or preset.
"""

import json
from datetime import datetime

import structlog

from agents.base import BaseAgent
from config import Settings
from database import Database
from models import TrackedTrader
from polymarket.data_client import PolymarketDataClient

log = structlog.get_logger(__name__)


SYSTEM_PROMPT = """You are the Trader Discovery agent for a Polymarket copy-trading system.

You will receive a JSON list of candidate wallets, each pre-filtered for basic
consistency (positive PnL over multiple windows, sufficient trade count, max
drawdown bound). Your job is to assign each wallet a score from 0 to 100
estimating how likely their performance reflects skill rather than luck, and to
flag wallets that look like resolution-snipers, market makers, or wash traders.

Heuristics:
- Higher trade count + lower drawdown + positive across windows -> higher score.
- High % of volume in markets resolving <24h -> resolution-sniper, lower score.
- Single huge winner dominating PnL -> luck, lower score.
- Diversified across categories -> higher score.

Return ONLY valid JSON, an array sorted by score descending:
[
  {"wallet": "0x...", "score": 78, "verdict": "edge|luck|resolution_sniper|maker|wash",
   "reason": "one sentence"}
]
"""


def _consistency_filter(
    by_window: dict[str, list[dict]],
    settings: Settings,
) -> list[dict]:
    """Combine 1w / 1m / all leaderboards into per-wallet rows, applying hard rules."""
    by_wallet: dict[str, dict] = {}
    for window, rows in by_window.items():
        for r in rows:
            w = r["wallet"]
            entry = by_wallet.setdefault(w, {"wallet": w, "windows": {}, "raw": r})
            entry["windows"][window] = {
                "pnl": r.get("pnl", 0.0),
                "volume": r.get("volume", 0.0),
                "trades": r.get("trades", 0),
            }

    survivors = []
    for w, entry in by_wallet.items():
        windows = entry["windows"]
        positive_windows = sum(1 for v in windows.values() if v["pnl"] > 0)
        if positive_windows < 2:
            continue
        # use the longest available window for the volume / trade gates
        ref = windows.get("all") or windows.get("1m") or windows.get("1w") or {}
        if ref.get("volume", 0) < settings.leader_min_wallet_volume:
            continue
        if ref.get("trades", 0) < settings.leader_min_trades:
            continue
        # crude weeks-profitable proxy: positive in 1w *and* 1m AND 'all'
        weeks_frac = positive_windows / max(len(windows), 1)
        if weeks_frac < settings.leader_min_weeks_profit_frac:
            continue
        survivors.append(
            {
                "wallet": w,
                "windows": windows,
                "weeks_profitable_frac": weeks_frac,
                "total_volume": ref.get("volume", 0),
                "trades": ref.get("trades", 0),
                "pnl_all": (windows.get("all") or {}).get("pnl", 0),
                "pnl_1m": (windows.get("1m") or {}).get("pnl", 0),
                "pnl_1w": (windows.get("1w") or {}).get("pnl", 0),
            }
        )
    survivors.sort(key=lambda r: r["pnl_all"] + r["pnl_1m"], reverse=True)
    return survivors


class TraderDiscoveryAgent(BaseAgent):
    def __init__(self, settings: Settings, data: PolymarketDataClient, db: Database):
        self._settings = settings
        self._data = data
        self._db = db
        super().__init__(
            name="TraderDiscovery",
            model=self.MODEL_SONNET,
            tools=[],
            handlers={},
            system_prompt=SYSTEM_PROMPT,
            max_tokens=2048,
        )

    async def discover(self) -> list[TrackedTrader]:
        """Run a discovery pass and persist surviving traders."""
        log.info("Discovery starting")
        by_window = {
            "1w": await self._data.get_leaderboard(window="1w", metric="profit", limit=300),
            "1m": await self._data.get_leaderboard(window="1m", metric="profit", limit=300),
            "all": await self._data.get_leaderboard(window="all", metric="profit", limit=300),
        }

        candidates = _consistency_filter(by_window, self._settings)
        if not candidates:
            log.info("Discovery: no candidates passed deterministic filter")
            return []

        # Cap candidates we send to the LLM
        head = candidates[: self._settings.leaderboard_top_n_for_llm]

        # LLM ranking
        try:
            prompt = (
                "Rank these candidate Polymarket wallets for copy-trading. "
                "Only return the top "
                f"{self._settings.leaderboard_keep_n} by score.\n\n"
                f"{json.dumps(head, indent=2)}"
            )
            raw = await self.run(prompt)
            ranking = self._parse_array(raw)
        except Exception as exc:
            log.warning("LLM ranking failed; using deterministic order", error=str(exc))
            ranking = [
                {"wallet": c["wallet"], "score": 50, "verdict": "edge", "reason": "fallback"}
                for c in head[: self._settings.leaderboard_keep_n]
            ]

        # Persist
        persisted: list[TrackedTrader] = []
        for r in ranking[: self._settings.leaderboard_keep_n]:
            wallet = (r.get("wallet") or "").lower()
            cand = next((c for c in candidates if c["wallet"] == wallet), None)
            if cand is None:
                continue
            existing = await self._db.get_tracked_trader(wallet)
            tt = TrackedTrader(
                wallet=wallet,
                status=existing.status if existing else "discovered",
                preset=existing.preset if existing else self._settings.copy_default_preset,
                score=float(r.get("score", 0)),
                sample_size=cand["trades"],
                weeks_profitable_frac=cand["weeks_profitable_frac"],
                max_drawdown=0.0,  # not exposed by leaderboard payload
                total_volume_usdc=cand["total_volume"],
                resolution_sniper_frac=0.0,  # populated by deeper analysis later
                last_seen_ts=existing.last_seen_ts if existing else 0,
                last_evaluated_at=datetime.utcnow(),
                notes=f"{r.get('verdict', '')}: {r.get('reason', '')}",
                created_at=existing.created_at if existing else datetime.utcnow(),
            )
            await self._db.upsert_tracked_trader(tt)
            persisted.append(tt)

        log.info("Discovery complete", kept=len(persisted))
        return persisted

    @staticmethod
    def _parse_array(raw: str) -> list[dict]:
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start < 0 or end <= start:
            return []
        try:
            return json.loads(raw[start:end])
        except json.JSONDecodeError:
            return []
