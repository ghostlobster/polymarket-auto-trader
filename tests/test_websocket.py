"""Unit tests for PolymarketWebSocketClient using async mocks."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from polymarket.websocket_client import PolymarketWebSocketClient


@pytest.mark.asyncio
async def test_subscription_management():
    ws_client = PolymarketWebSocketClient()
    await ws_client.subscribe(["tok1", "tok2"])
    assert ws_client._subscribed_assets == {"tok1", "tok2"}

    # Duplicate subscribe doesn't add new assets
    await ws_client.subscribe(["tok1"])
    assert ws_client._subscribed_assets == {"tok1", "tok2"}

    # Unsubscribe removes assets
    await ws_client.unsubscribe(["tok1"])
    assert ws_client._subscribed_assets == {"tok2"}


@pytest.mark.asyncio
async def test_handler_dispatch():
    ws_client = PolymarketWebSocketClient()
    received_sync = []
    received_async = []

    def sync_handler(data):
        received_sync.append(data)

    async def async_handler(data):
        received_async.append(data)

    ws_client.add_handler(sync_handler)
    ws_client.add_handler(async_handler)

    test_event = {"event_type": "trade", "asset_id": "tok1", "price": 0.55}

    # Simulate pushing event to queue and invoking handlers
    await ws_client._queue.put(test_event)
    for handler in ws_client._handlers:
        res = handler(test_event)
        if asyncio.iscoroutine(res) or hasattr(res, "__await__"):
            await res  # type: ignore[misc]

    assert received_sync == [test_event]
    assert received_async == [test_event]


@pytest.mark.asyncio
async def test_start_stop_lifecycle():
    ws_client = PolymarketWebSocketClient()

    # Mock internal _run_loop to avoid real network calls
    mock_run_loop = AsyncMock()
    ws_client._run_loop = mock_run_loop

    await ws_client.start()
    assert ws_client._running is True
    assert ws_client._listen_task is not None

    await ws_client.stop()
    assert ws_client._running is False
    assert ws_client._listen_task is None
