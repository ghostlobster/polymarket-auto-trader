"""
Database query tools for agents that need portfolio/history context.
"""
import json

from database import Database


def build_db_tools(db: Database) -> tuple[list[dict], dict]:
    """Return (tools_schema_list, handler_map) for database read tools."""

    tools = [
        {
            "name": "get_recent_signals",
            "description": "Get recently generated trading signals from the database.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Number of signals to return", "default": 10},
                },
            },
        },
        {
            "name": "get_open_positions_db",
            "description": "Get currently open positions stored in the local database.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_total_pnl",
            "description": "Get total realized P&L across all closed positions.",
            "input_schema": {"type": "object", "properties": {}},
        },
        {
            "name": "get_open_orders_db",
            "description": "Get currently open/pending orders from the local database.",
            "input_schema": {"type": "object", "properties": {}},
        },
    ]

    async def handle_get_recent_signals(inputs: dict) -> str:
        signals = await db.get_recent_signals(limit=inputs.get("limit", 10))
        return json.dumps([s.model_dump() for s in signals], default=str)

    async def handle_get_open_positions(inputs: dict) -> str:
        positions = await db.get_open_positions()
        return json.dumps([p.model_dump() for p in positions], default=str)

    async def handle_get_total_pnl(inputs: dict) -> str:
        pnl = await db.get_total_realized_pnl()
        return json.dumps({"total_realized_pnl_usdc": pnl})

    async def handle_get_open_orders(inputs: dict) -> str:
        orders = await db.get_open_orders()
        return json.dumps([o.model_dump() for o in orders], default=str)

    handlers = {
        "get_recent_signals": handle_get_recent_signals,
        "get_open_positions_db": handle_get_open_positions,
        "get_total_pnl": handle_get_total_pnl,
        "get_open_orders_db": handle_get_open_orders,
    }

    return tools, handlers
