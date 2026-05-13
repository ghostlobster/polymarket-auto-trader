"""
Web search and fetch tools for the Research Analyst agent.
"""

import json

import httpx
from duckduckgo_search import DDGS


def build_web_tools() -> tuple[list[dict], dict]:
    """Return (tools_schema_list, handler_map) for web research tools."""

    tools = [
        {
            "name": "web_search",
            "description": "Search the web for recent news and information about a topic. Use to research the likelihood of prediction market outcomes.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {
                        "type": "integer",
                        "description": "Max results to return (default 8)",
                        "default": 8,
                    },
                },
                "required": ["query"],
            },
        },
        {
            "name": "fetch_url",
            "description": "Fetch the text content of a URL (news article, data page, etc.).",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL to fetch"},
                },
                "required": ["url"],
            },
        },
    ]

    async def handle_web_search(inputs: dict) -> str:
        query = inputs["query"]
        max_results = inputs.get("max_results", 8)
        try:
            results = []
            with DDGS() as ddgs:
                for r in ddgs.text(query, max_results=max_results):
                    results.append(
                        {
                            "title": r.get("title", ""),
                            "url": r.get("href", ""),
                            "snippet": r.get("body", ""),
                        }
                    )
            return json.dumps(results)
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    async def handle_fetch_url(inputs: dict) -> str:
        url = inputs["url"]
        try:
            async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
                resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                # Truncate to 8000 chars to fit context
                text = resp.text[:8000]
                return json.dumps({"url": url, "status": resp.status_code, "content": text})
        except Exception as exc:
            return json.dumps({"error": str(exc)})

    handlers = {
        "web_search": handle_web_search,
        "fetch_url": handle_fetch_url,
    }

    return tools, handlers
