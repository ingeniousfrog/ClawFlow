"""Memory save and search tools. memory_save、memory_search，配合内存模块实现对用户信息的长期记忆和检索。"""

from __future__ import annotations

from nanoclaw.tools.registry import tool


@tool(
    name="memory_save",
    description=(
        "Save an important fact to long-term memory. "
        "Use when the user shares personal info, preferences, "
        "or asks you to remember something."
    ),
    parameters={
        "fact": {
            "type": "string",
            "description": (
                "The fact to remember "
                "(e.g., 'User prefers Python over JavaScript')"
            ),
        },
        "category": {
            "type": "string",
            "description": (
                "Category: 'personal', 'work', 'preference', 'project', 'other'"
            ),
        },
    },
    required=["fact"],
    catalog_summary="Store a durable user or project fact.",
    catalog_entry_points=["remember this", "save preference"],
    risk_level="low",
)
async def memory_save(fact: str, category: str = "other") -> str:
    """Save a fact to long-term memory."""
    from nanoclaw.memory.store import get_memory_store

    memory = get_memory_store()
    await memory.save_memory(fact, category=category)
    return f"Saved to memory: {fact}"


@tool(
    name="memory_search",
    description="Search your long-term memory for relevant facts about the user.",
    parameters={
        "query": {
            "type": "string",
            "description": "What to search for in memory",
        }
    },
    catalog_summary="Search previously saved long-term memories.",
    catalog_entry_points=["what do you remember", "recall saved facts"],
    risk_level="low",
)
async def memory_search(query: str) -> str:
    """Search memories for relevant facts."""
    from nanoclaw.memory.store import get_memory_store

    memory = get_memory_store()
    results = await memory.search_memories(query, limit=10)

    if not results:
        return "No relevant memories found."

    lines = []
    for r in results:
        lines.append(f"- {r['content']} ({r['category']}, {r['created_at']})")
    return "\n".join(lines)
