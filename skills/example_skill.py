"""Example user skill for nanoClaw."""

from __future__ import annotations

from nanoclaw.tools.registry import tool


@tool(
    name="example_skill",
    description="Example user skill that echoes text in uppercase.",
    parameters={
        "text": {
            "type": "string",
            "description": "Text to convert to uppercase",
        }
    },
)
async def example_skill(text: str) -> str:
    """Return the input text in uppercase."""
    return text.upper()
