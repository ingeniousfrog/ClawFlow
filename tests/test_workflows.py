"""Workflow registry tests."""

from __future__ import annotations

from nanoclaw.core.workflows import (
    get_workflow_catalog,
    get_workflow_catalog_path,
    matches_structured_grounding_workflow,
    resolve_workflow_defaults,
)


def test_resolve_workflow_defaults_ignores_unknown_names() -> None:
    """Unknown workflow overrides should fall back to built-in defaults."""
    resolved = resolve_workflow_defaults(
        {"chat": "missing_workflow", "grounded": "web_model_grounding"}
    )

    assert resolved["chat"] == "default_chat_loop"
    assert resolved["grounded"] == "web_model_grounding"


def test_get_workflow_catalog_marks_overridden_default_roles() -> None:
    """Catalog entries should surface the current default workflow roles."""
    catalog = {
        item.name: item
        for item in get_workflow_catalog(
            {"chat": "scheduled_job_flow", "grounded": "web_model_grounding"}
        )
    }

    assert "chat" in catalog["scheduled_job_flow"].default_roles
    assert "grounded" in catalog["web_model_grounding"].default_roles
    assert "chat" not in catalog["default_chat_loop"].default_roles


def test_matches_structured_grounding_workflow_uses_registry_terms() -> None:
    """Structured grounding detection should come from the shared workflow registry."""
    assert matches_structured_grounding_workflow("Please search recent arXiv papers on agents")
    assert matches_structured_grounding_workflow("请帮我做一篇公众号文章大纲")
    assert not matches_structured_grounding_workflow("What is two plus two?")


def test_workflow_catalog_loads_from_packaged_json() -> None:
    """Workflow descriptions should come from the packaged catalog data file."""
    assert get_workflow_catalog_path().name == "workflow_catalog.json"
    catalog = {item.name: item for item in get_workflow_catalog()}
    assert catalog["default_chat_loop"].summary.startswith("ReAct-style")
