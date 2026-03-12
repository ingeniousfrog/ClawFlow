"""Capability introspection tools."""

from __future__ import annotations

from nanoclaw.core.capabilities import render_capability_json, render_capability_text
from nanoclaw.tools.registry import tool


@tool(
    name="capability_list",
    description=(
        "List built-in tools, skills, and default workflows, including what each workflow uses. "
        "Use when the user asks what nanoClaw can do or wants workflow transparency."
    ),
    parameters={
        "kind": {
            "type": "string",
            "description": "Filter to one section: all, tool, skill, or workflow.",
            "enum": ["all", "tool", "skill", "workflow"],
            "default": "all",
        },
        "format": {
            "type": "string",
            "description": "Response format: text or json.",
            "enum": ["text", "json"],
            "default": "text",
        },
    },
    required=[],
    catalog_summary="List built-in tools, skills, and default workflows.",
    catalog_entry_points=["what can you do", "show capabilities", "show workflows"],
    risk_level="low",
)
async def capability_list(kind: str = "all", format: str = "text") -> str:
    """Return the built-in capability catalog."""

    selected_kind = kind.strip().lower() or "all"
    selected_format = format.strip().lower() or "text"

    if selected_kind not in {"all", "tool", "skill", "workflow"}:
        return "Invalid arguments for capability_list: kind must be all, tool, skill, or workflow."
    if selected_format not in {"text", "json"}:
        return "Invalid arguments for capability_list: format must be text or json."

    if selected_format == "json":
        return render_capability_json(selected_kind)
    return render_capability_text(selected_kind)
