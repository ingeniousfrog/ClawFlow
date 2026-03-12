"""Memory store tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoclaw.memory.store import MemoryStore


@pytest.mark.asyncio
async def test_memory_add_and_get_history(tmp_path: Path) -> None:
    """MemoryStore should return history in order."""
    store = MemoryStore(tmp_path / "mem.db")
    await store.add_message("s1", "user", "hello")
    await store.add_message("s1", "assistant", "hi")

    history = await store.get_history("s1", limit=10)
    assert [h["role"] for h in history] == ["user", "assistant"]
    assert [h["content"] for h in history] == ["hello", "hi"]


@pytest.mark.asyncio
async def test_memory_save_dedup_and_search(tmp_path: Path) -> None:
    """MemoryStore should deduplicate and search memories."""
    store = MemoryStore(tmp_path / "mem.db")
    await store.save_memory("User likes tea", category="preference")
    await store.save_memory("User likes tea", category="preference")

    memories = await store.get_all_memories()
    assert len(memories) == 1

    results = await store.search_memories("tea", limit=5)
    assert any("tea" in r["content"].lower() for r in results)


@pytest.mark.asyncio
async def test_memory_stats_counts(tmp_path: Path) -> None:
    """MemoryStore should report basic stats."""
    store = MemoryStore(tmp_path / "mem.db")
    await store.add_message("s1", "user", "hello")
    await store.save_memory("User likes coffee", category="preference")

    stats = await store.get_stats()
    assert stats["total_messages"] >= 1
    assert stats["memories"] >= 1
