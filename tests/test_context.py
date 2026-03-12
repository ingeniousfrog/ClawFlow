"""Context builder tests."""

from __future__ import annotations

from pathlib import Path

from nanoclaw.core.context import ContextBuilder
from nanoclaw.core.persona import PersonaFragments, PersonaStore


def test_select_tools_adds_hotspot_brief_for_chinese_trigger() -> None:
    """Hotspot keyword should inject hotspot_brief into selected tools."""
    builder = ContextBuilder()
    all_tools = [
        {
            "type": "function",
            "function": {"name": "web_search"},
        },
        {
            "type": "function",
            "function": {"name": "hotspot_brief"},
        },
        {
            "type": "function",
            "function": {"name": "file_read"},
        },
    ]

    selected = builder.select_tools("请给我一份AI热点简报", all_tools)
    names = {item["function"]["name"] for item in selected}
    assert "hotspot_brief" in names


def test_select_tools_adds_capability_list_for_discovery_request() -> None:
    """Capability questions should inject the catalog tool."""
    builder = ContextBuilder()
    all_tools = [
        {
            "type": "function",
            "function": {"name": "web_search"},
        },
        {
            "type": "function",
            "function": {"name": "capability_list"},
        },
    ]

    selected = builder.select_tools("你现在有哪些功能和工作流？", all_tools)
    names = {item["function"]["name"] for item in selected}
    assert "capability_list" in names


def test_select_tools_adds_manifest_skill_for_weather_request() -> None:
    """Manifest-backed skill triggers should inject the matching skill tool."""
    builder = ContextBuilder()
    all_tools = [
        {
            "type": "function",
            "function": {"name": "web_search"},
        },
        {
            "type": "function",
            "function": {"name": "get_weather"},
        },
    ]

    selected = builder.select_tools("What's the weather in Tokyo?", all_tools)
    names = {item["function"]["name"] for item in selected}
    assert "get_weather" in names


def test_build_system_prompt_includes_configured_instructions() -> None:
    """Configured system prompt should be appended to the runtime prompt."""
    builder = ContextBuilder("Always ask one clarifying question for ambiguous tasks.")
    prompt = builder.build_system_prompt([])
    assert "ADDITIONAL CONFIGURED INSTRUCTIONS" in prompt
    assert "clarifying question" in prompt


def test_build_system_prompt_includes_protected_persona_fragments(tmp_path: Path) -> None:
    """Protected persona fragments should render before configured instructions."""
    store = PersonaStore(tmp_path / "persona_fragments.json")
    store.save(
        PersonaFragments(
            identity=["Ground answers in cited evidence when available."],
            style=["Keep responses short unless the user asks for depth."],
        )
    )

    builder = ContextBuilder(
        "Always ask one clarifying question for ambiguous tasks.",
        persona_store=store,
    )
    prompt = builder.build_system_prompt([])

    assert "PROTECTED PERSONA FRAGMENTS" in prompt
    assert "Ground answers in cited evidence when available." in prompt
    assert "Keep responses short unless the user asks for depth." in prompt
    assert "ADDITIONAL CONFIGURED INSTRUCTIONS" in prompt
