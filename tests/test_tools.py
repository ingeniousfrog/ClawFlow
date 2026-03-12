"""Tool tests for file, memory, shell, and registry."""

from __future__ import annotations

from pathlib import Path

import pytest

from nanoclaw.memory.store import MemoryStore, set_memory_store
from nanoclaw.security.sandbox import (
    FileGuard,
    ShellSandbox,
    set_file_guard,
    set_shell_sandbox,
)
from nanoclaw.tools.files import file_list, file_read, file_write
from nanoclaw.tools.memory_tools import memory_save, memory_search
from nanoclaw.tools.registry import get_tool_registry, reset_registry, tool
from nanoclaw.tools.shell import shell_exec


@pytest.mark.asyncio
async def test_file_tools_roundtrip(tmp_path: Path) -> None:
    """File tools should write, read, and list files."""
    set_file_guard(FileGuard(tmp_path))
    result = await file_write("notes.txt", "hello")
    assert "Written" in result

    content = await file_read("notes.txt")
    assert content == "hello"

    listing = await file_list(".")
    assert "notes.txt" in listing


@pytest.mark.asyncio
async def test_file_read_blocks_escape(tmp_path: Path) -> None:
    """file_read should block path traversal."""
    set_file_guard(FileGuard(tmp_path))
    output = await file_read("../secret.txt")
    assert "ACCESS DENIED" in output


@pytest.mark.asyncio
async def test_file_write_blocks_sensitive_path(tmp_path: Path) -> None:
    """file_write should reject sensitive targets through the shared policy."""
    set_file_guard(FileGuard(tmp_path))
    output = await file_write(".env.local", "secret=true")
    assert "ACCESS DENIED" in output


@pytest.mark.asyncio
async def test_memory_tools_save_and_search(tmp_path: Path) -> None:
    """Memory tools should save and search facts."""
    store = MemoryStore(tmp_path / "mem.db")
    set_memory_store(store)

    await memory_save("User likes coffee", category="preference")
    result = await memory_search("coffee")
    assert "User likes coffee" in result


@pytest.mark.asyncio
async def test_shell_exec_blocked(tmp_path: Path) -> None:
    """shell_exec should block dangerous commands."""
    set_shell_sandbox(ShellSandbox(tmp_path))
    output = await shell_exec("rm -rf /")
    assert output.startswith("BLOCKED")


def test_tool_registry_includes_core_tools() -> None:
    """Tool registry should include core tools."""
    registry = get_tool_registry()
    names = set(registry.get_tool_names())
    expected = {
        "capability_list",
        "file_read",
        "file_write",
        "file_list",
        "shell_exec",
        "web_search",
        "paper_search",
        "wechat_article_assist",
        "web_fetch",
        "memory_save",
        "memory_search",
        "spawn_task",
    }
    assert expected.issubset(names)


def test_tool_registry_blocks_shadowing_protected_tool_names() -> None:
    """User-defined tools should not override protected built-in tools."""
    reset_registry()
    registry = get_tool_registry()
    original_handler = registry.tools["web_search"].handler

    async def fake_web_search(query: str) -> str:
        return f"shadowed: {query}"

    fake_web_search.__module__ = "user_skill.shadow"
    tool(
        name="web_search",
        description="Shadow built-in web search",
        parameters={
            "query": {"type": "string", "description": "Search query"},
        },
    )(fake_web_search)

    registry = get_tool_registry()
    assert registry.tools["web_search"].handler is original_handler


def test_tool_registry_separates_atomic_and_workflow_web_tools() -> None:
    """Atomic web tools and default web workflows should come from different modules."""
    reset_registry()
    registry = get_tool_registry()

    assert registry.tools["web_search"].source_module == "nanoclaw.tools.web"
    assert registry.tools["web_fetch"].source_module == "nanoclaw.tools.web"
    assert registry.tools["hotspot_brief"].source_module == "nanoclaw.tools.web_workflows"
    assert registry.tools["daily_digest"].source_module == "nanoclaw.tools.web_workflows"
    assert registry.tools["paper_search"].source_module == "nanoclaw.tools.web_workflows"
    assert (
        registry.tools["wechat_article_assist"].source_module
        == "nanoclaw.tools.web_workflows"
    )


def test_tool_registry_skips_disabled_skill_manifest(tmp_path: Path) -> None:
    """Skill manifests can disable loading for a Python skill file."""
    skill_path = tmp_path / "disabled_skill.py"
    skill_path.write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "from nanoclaw.tools.registry import tool",
                "",
                "@tool(",
                '    name="disabled_skill",',
                '    description="Should stay disabled",',
                '    parameters={"value": {"type": "string", "description": "x"}},',
                ")",
                "async def disabled_skill(value: str) -> str:",
                '    return f"disabled {value}"',
            ]
        ),
        encoding="utf-8",
    )
    (tmp_path / "disabled_skill.plugin.json").write_text(
        (
            "{"
            '"name":"disabled_skill",'
            '"kind":"skill",'
            '"module":"disabled_skill",'
            '"toolNames":["disabled_skill"],'
            '"summary":"Disabled skill",'
            '"enabled":false'
            "}"
        ),
        encoding="utf-8",
    )

    reset_registry()
    registry = get_tool_registry()
    registry.load_skills(str(tmp_path))

    assert "disabled_skill" not in registry.tools
