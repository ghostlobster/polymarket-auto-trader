"""
PolymarketWebSocketClient — real-time WebSocket listener for orderbook and trade events.

Connects to Polymarket's CLOB WebSocket stream (wss://ws-subscriptions.clob.polymarket.com/ws/market)
and streams real-time market updates, trade fills, and orderbook depth changes.
Includes automatic reconnection with exponential backoff and async message queue dispatch.
"""

import asyncio
import json
from typing import Any, AsyncGenerator, Callable

import structlog
import websockets

log = structlog.get_logger(__name__)


class PolymarketWebSocketClient:
    """Async WebSocket client for streaming Polymarket market events."""

    def __init__(
        self,
        url: str = "wss://ws-subscriptions.clob.polymarket.com/ws/market",
        ping_interval: float = 20.0,
        ping_timeout: float = 10.0,
        reconnect_delay: float = 2.0,
        max_reconnect_delay: float = 60.0,
    ):
        self.url = url
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.reconnect_delay = reconnect_delay
        self.max_reconnect_delay = max_reconnect_delay

        self._ws: websockets.WebSocketClientProtocol | None = None  # type: ignore[name-defined]
        self._queue: asyncio.Queue[dict] = asyncio.Queue()
        self._subscribed_assets: set[str] = set()
        self._running = False
        self._listen_task: asyncio.Task | None = None
        self._handlers: list[Callable[[dict], Any]] = []

    @property
    def is_connected(self) -> bool:
        return self._ws is not None and getattr(self._ws, "open", False)

    def add_handler(self, handler: Callable[[dict], Any]) -> None:
        """Register a sync or async callback handler for incoming WS events."""
        self._handlers.append(handler)

    async def subscribe(self, asset_ids: list[str]) -> None:
        """Subscribe to real-time events for specified token/asset IDs."""
        new_ids = set(asset_ids) - self._subscribed_assets
        if not new_ids:
            return
        self._subscribed_assets.update(new_ids)

        if self.is_connected:
            msg = {"type": "subscribe", "assets_ids": list(new_ids)}
            await self._send(msg)

    async def unsubscribe(self, asset_ids: list[str]) -> None:
        """Unsubscribe from real-time events for specified token/asset IDs."""
        rem_ids = set(asset_ids).intersection(self._subscribed_assets)
        if not rem_ids:
            return
        self._subscribed_assets.difference_update(rem_ids)

        if self.is_connected:
            msg = {"type": "unsubscribe", "assets_ids": list(rem_ids)}
            await self._send(msg)

    async def _send(self, payload: dict) -> None:
        if self._ws and getattr(self._ws, "open", False):
            try:
                await self._ws.send(json.dumps(payload))
            except Exception as exc:
                log.warning("WebSocket send error", error=str(exc))

    async def start(self) -> None:
        """Start the background listening loop."""
        if self._running:
            return
        self._running = True
        self._listen_task = asyncio.create_task(self._run_loop())
        log.info("Polymarket WebSocket client started", url=self.url)

    async def stop(self) -> None:
        """Gracefully shutdown the WebSocket client."""
        self._running = False
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
            self._listen_task = None
        log.info("Polymarket WebSocket client stopped")

    async def _run_loop(self) -> None:
        delay = self.reconnect_delay
        while self._running:
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=self.ping_interval,
                    ping_timeout=self.ping_timeout,
                ) as ws:
                    self._ws = ws
                    delay = self.reconnect_delay  # reset backoff on clean connection
                    log.info("WebSocket connected", url=self.url)

                    # Re-subscribe active assets
                    if self._subscribed_assets:
                        msg = {"type": "subscribe", "assets_ids": list(self._subscribed_assets)}
                        await self._send(msg)

                    async for message in ws:
                        if not self._running:
                            break
                        try:
                            data = json.loads(message)
                            await self._queue.put(data)
                            for handler in self._handlers:
                                try:
                                    res = handler(data)
                                    if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
                                        await res  # type: ignore[misc]
                                except Exception as err:
                                    log.error("Handler error", error=str(err))
                        except json.JSONDecodeError:
                            log.warning("WebSocket received non-JSON payload", raw=message[:100])

            except (websockets.ConnectionClosed, OSError, Exception) as exc:
                if not self._running:
                    break
                log.warning(
                    "WebSocket connection dropped, reconnecting", error=str(exc), delay=delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, self.max_reconnect_delay)

    async def stream_events(self) -> AsyncGenerator[dict, None]:
        """Async generator yielding incoming market event payloads."""
        while self._running or not self._queue.empty():
            try:
                event = await asyncio.wait_for(self._queue.get(), timeout=1.0)
                yield event
            except asyncio.TimeoutError:
                continue
