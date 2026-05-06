"""
Async REST client for the Polymarket data API (data-api.polymarket.com).

This is separate from the CLOB client (`polymarket/client.py`) which handles
order placement. The data API exposes leaderboard, per-wallet positions, and
per-wallet activity feeds — the raw inputs to copy-trading discovery and the
follow loop.

The exact response shapes from data-api.polymarket.com are normalized into
plain dicts here so the rest of the codebase doesn't depend on the upstream
schema changing field names.
"""
import asyncio
from typing import Any

import httpx
import structlog
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

log = structlog.get_logger(__name__)


# Some fields on the Polymarket data-api responses come and go between
# versions; we always read with .get() and accept missing values.


class PolymarketDataClient:
    """Thin async wrapper around the Polymarket data API."""

    def __init__(self, base_url: str, http: httpx.AsyncClient | None = None):
        self._base_url = base_url.rstrip("/")
        self._http = http
        self._owns_client = http is None

    async def __aenter__(self) -> "PolymarketDataClient":
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15.0)
        return self

    async def __aexit__(self, *exc_info) -> None:
        if self._owns_client and self._http is not None:
            await self._http.aclose()
            self._http = None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=15.0)
        return self._http

    async def _get(self, path: str, params: dict | None = None) -> Any:
        url = f"{self._base_url}{path}"
        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(3),
            wait=wait_exponential(multiplier=1, min=1, max=8),
            retry=retry_if_exception_type((httpx.HTTPError, httpx.TransportError)),
            reraise=True,
        ):
            with attempt:
                resp = await self.http.get(url, params=params or {})
                resp.raise_for_status()
                return resp.json()
        return None

    # ------------------------------------------------------------------ #
    #  Leaderboard                                                        #
    # ------------------------------------------------------------------ #

    async def get_leaderboard(
        self,
        window: str = "1m",
        metric: str = "profit",
        limit: int = 200,
    ) -> list[dict]:
        """
        Fetch top traders by profit/volume over a time window.

        window: one of '1d','1w','1m','all'
        metric: 'profit' or 'volume'
        Returns a list of dicts with at least: wallet, pnl, volume.
        """
        try:
            raw = await self._get(
                "/leaderboard",
                params={"window": window, "metric": metric, "limit": limit},
            )
        except Exception as exc:
            log.warning("Leaderboard fetch failed", window=window, error=str(exc))
            return []
        rows = raw if isinstance(raw, list) else (raw.get("data") or raw.get("leaderboard") or [])
        out = []
        for r in rows:
            wallet = r.get("proxyWallet") or r.get("address") or r.get("user") or r.get("wallet")
            if not wallet:
                continue
            out.append({
                "wallet": wallet.lower(),
                "pnl": float(r.get("pnl", r.get("profit", 0)) or 0),
                "volume": float(r.get("volume", 0) or 0),
                "trades": int(r.get("trades", r.get("tradeCount", 0)) or 0),
                "name": r.get("name") or r.get("displayName") or "",
                "raw": r,
            })
        return out

    # ------------------------------------------------------------------ #
    #  Per-wallet positions                                               #
    # ------------------------------------------------------------------ #

    async def get_user_positions(self, wallet: str) -> list[dict]:
        try:
            raw = await self._get("/positions", params={"user": wallet})
        except Exception as exc:
            log.warning("User positions fetch failed", wallet=wallet, error=str(exc))
            return []
        rows = raw if isinstance(raw, list) else (raw.get("data") or [])
        return rows or []

    async def get_user_value(self, wallet: str) -> float:
        try:
            raw = await self._get("/value", params={"user": wallet})
        except Exception as exc:
            log.warning("User value fetch failed", wallet=wallet, error=str(exc))
            return 0.0
        if isinstance(raw, dict):
            return float(raw.get("value", raw.get("totalValue", 0)) or 0)
        if isinstance(raw, list) and raw:
            return float(raw[0].get("value", 0) or 0)
        return 0.0

    # ------------------------------------------------------------------ #
    #  Per-wallet activity feed                                           #
    # ------------------------------------------------------------------ #

    async def get_user_activity(
        self,
        wallet: str,
        after_ts: int = 0,
        types: tuple[str, ...] = ("TRADE",),
        limit: int = 100,
    ) -> list[dict]:
        """
        Fetch recent activity for a wallet. Returns list normalized to:
          {tx_hash, timestamp (unix s), type, side, outcome, condition_id,
           token_id, size_usdc, price, raw}
        Only events with timestamp > after_ts are returned.
        """
        try:
            raw = await self._get(
                "/activity",
                params={
                    "user": wallet,
                    "limit": limit,
                    "type": ",".join(types),
                },
            )
        except Exception as exc:
            log.warning("User activity fetch failed", wallet=wallet, error=str(exc))
            return []
        rows = raw if isinstance(raw, list) else (raw.get("data") or [])
        events = []
        for r in rows:
            ts = int(r.get("timestamp", r.get("ts", 0)) or 0)
            if ts <= after_ts:
                continue
            ev_type = (r.get("type") or "").upper() or "TRADE"
            if ev_type not in types:
                continue
            tx_hash = r.get("transactionHash") or r.get("txHash") or r.get("hash") or ""
            if not tx_hash:
                # Synthesize a stable id when missing — wallet+ts+token uniquely IDs a trade.
                tx_hash = f"{wallet}-{ts}-{r.get('asset', r.get('tokenId', ''))}"
            side = (r.get("side") or "").upper() or "BUY"
            outcome = (r.get("outcome") or "").upper() or "YES"
            size_usdc = float(r.get("usdcSize", r.get("size", 0)) or 0)
            if not size_usdc:
                # fall back to shares × price when only share size is reported
                shares = float(r.get("shares", r.get("amount", 0)) or 0)
                price = float(r.get("price", 0) or 0)
                size_usdc = shares * price
            events.append({
                "tx_hash": tx_hash,
                "timestamp": ts,
                "type": ev_type,
                "side": side,
                "outcome": outcome,
                "condition_id": r.get("conditionId") or r.get("market") or "",
                "token_id": r.get("asset") or r.get("tokenId") or "",
                "size_usdc": size_usdc,
                "price": float(r.get("price", 0) or 0),
                "raw": r,
            })
        # Return chronological so the caller can advance the cursor monotonically.
        events.sort(key=lambda e: e["timestamp"])
        return events

    # ------------------------------------------------------------------ #
    #  Convenience                                                        #
    # ------------------------------------------------------------------ #

    async def get_users_activity(
        self, wallets: list[str], after_ts_per_wallet: dict[str, int]
    ) -> dict[str, list[dict]]:
        """Fetch activity for many wallets in parallel."""
        async def one(w):
            return w, await self.get_user_activity(
                w, after_ts=after_ts_per_wallet.get(w, 0)
            )
        results = await asyncio.gather(*(one(w) for w in wallets), return_exceptions=False)
        return dict(results)
