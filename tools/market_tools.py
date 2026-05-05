"""
Tool definitions for Polymarket API interactions.
Returns Anthropic tool dicts + paired async handler functions.
"""
import json

from polymarket.client import PolymarketClient


def build_market_tools(poly: PolymarketClient) -> tuple[list[dict], dict]:
    """Return (tools_schema_list, handler_map) for Polymarket API tools."""

    tools = [
        {
            "name": "get_markets",
            "description": "Fetch active prediction markets from Polymarket. Returns market metadata including question, volume, spread, and token IDs.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of markets to fetch (max 100)", "default": 50},
                    "offset": {"type": "integer", "description": "Pagination offset", "default": 0},
                },
            },
        },
        {
            "name": "get_orderbook",
            "description": "Get the current order book for a specific token (YES or NO side of a market). Returns best bid, best ask, and spread.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "token_id": {"type": "string", "description": "The token ID (yes_token_id or no_token_id from market data)"},
                },
                "required": ["token_id"],
            },
        },
        {
            "name": "get_positions",
            "description": "Get current open positions in my portfolio.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_balance",
            "description": "Get current USDC balance available for trading.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "place_limit_order",
            "description": "Place a limit order to buy or sell shares at a specific price.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "token_id": {"type": "string", "description": "Token ID to trade"},
                    "side": {"type": "string", "enum": ["BUY", "SELL"], "description": "Order side"},
                    "size_usdc": {"type": "number", "description": "Amount in USDC to spend/receive"},
                    "price": {"type": "number", "description": "Limit price between 0.0 and 1.0"},
                    "market_id": {"type": "string", "description": "Associated market condition_id"},
                    "signal_id": {"type": "string", "description": "Signal that triggered this order"},
                },
                "required": ["token_id", "side", "size_usdc", "price"],
            },
        },
        {
            "name": "place_market_order",
            "description": "Place a market order (immediate fill at best available price). Use only when speed matters more than price.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "token_id": {"type": "string"},
                    "side": {"type": "string", "enum": ["BUY", "SELL"]},
                    "size_usdc": {"type": "number"},
                    "market_id": {"type": "string"},
                    "signal_id": {"type": "string"},
                },
                "required": ["token_id", "side", "size_usdc"],
            },
        },
        {
            "name": "cancel_order",
            "description": "Cancel an open order by its order ID.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "order_id": {"type": "string", "description": "The order ID to cancel"},
                },
                "required": ["order_id"],
            },
        },
    ]

    from models import OrderSide

    async def handle_get_markets(inputs: dict) -> str:
        markets = await poly.get_markets(
            limit=inputs.get("limit", 50),
            offset=inputs.get("offset", 0),
        )
        return json.dumps([m.model_dump() for m in markets], default=str)

    async def handle_get_orderbook(inputs: dict) -> str:
        book = await poly.get_orderbook(inputs["token_id"])
        return json.dumps(book.model_dump())

    async def handle_get_positions(inputs: dict) -> str:
        positions = await poly.get_positions()
        return json.dumps([p.model_dump() for p in positions], default=str)

    async def handle_get_balance(inputs: dict) -> str:
        bal = await poly.get_balance_usdc()
        return json.dumps({"balance_usdc": bal})

    async def handle_place_limit_order(inputs: dict) -> str:
        order = await poly.place_limit_order(
            token_id=inputs["token_id"],
            side=OrderSide(inputs["side"]),
            size_usdc=inputs["size_usdc"],
            price=inputs["price"],
            market_id=inputs.get("market_id", ""),
            signal_id=inputs.get("signal_id", ""),
        )
        return json.dumps(order.model_dump(), default=str)

    async def handle_place_market_order(inputs: dict) -> str:
        order = await poly.place_market_order(
            token_id=inputs["token_id"],
            side=OrderSide(inputs["side"]),
            size_usdc=inputs["size_usdc"],
            market_id=inputs.get("market_id", ""),
            signal_id=inputs.get("signal_id", ""),
        )
        return json.dumps(order.model_dump(), default=str)

    async def handle_cancel_order(inputs: dict) -> str:
        success = await poly.cancel_order(inputs["order_id"])
        return json.dumps({"success": success})

    handlers = {
        "get_markets": handle_get_markets,
        "get_orderbook": handle_get_orderbook,
        "get_positions": handle_get_positions,
        "get_balance": handle_get_balance,
        "place_limit_order": handle_place_limit_order,
        "place_market_order": handle_place_market_order,
        "cancel_order": handle_cancel_order,
    }

    return tools, handlers
