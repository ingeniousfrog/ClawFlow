"""News search skill using web search."""

from __future__ import annotations

from nanoclaw.tools.registry import tool
from nanoclaw.tools.web import web_search


@tool(
    name="get_news",
    description="Get latest news on a topic using web search",
    parameters={
        "topic": {
            "type": "string",
            "description": "News topic to search for",
        }
    },
)
async def get_news(topic: str) -> str:
    """Search for recent news on a topic."""
    results = await web_search(f"{topic} news today")
    return f"Latest news about '{topic}':\n\n{results}"
