"""Capability catalog tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

from click.testing import CliRunner
import pytest

from nanoclaw.cli.main import cli
from nanoclaw.core.capabilities import catalog_to_dict, render_capability_text
from nanoclaw.core.plugins import reset_plugin_registry


def test_render_capability_text_includes_workflow_mapping() -> None:
    """Text rendering should show workflows and the capabilities they use."""
    output = render_capability_text()
    assert "Workflows" in output
    assert "feishu_paper_template" in output
    assert "heartbeat_checklist" in output
    assert "Uses: paper_search" in output


def test_catalog_to_dict_can_filter_workflows_only() -> None:
    """Filtering should keep only the requested section."""
    data = catalog_to_dict("workflow")
    assert "workflows" in data
    assert "tools" not in data
    assert any(item["name"] == "wechat_article_flow" for item in data["workflows"])


def test_capabilities_command_supports_json_output() -> None:
    """CLI should expose the capability catalog in JSON form."""
    runner = CliRunner()
    result = runner.invoke(cli, ["capabilities", "--kind", "workflow", "--format", "json"])
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert any(item["name"] == "default_chat_loop" for item in data["workflows"])


def test_catalog_to_dict_includes_manifest_backed_skill_metadata() -> None:
    """Skill section should be populated from plugin manifests."""
    reset_plugin_registry()
    data = catalog_to_dict("skill")
    assert any(
        item["name"] == "summarize_url" and item["risk_level"] == "medium"
        for item in data["skills"]
    )


def test_catalog_to_dict_uses_tool_registry_metadata() -> None:
    """Tool section should come from shared registry metadata, not a hardcoded list."""
    data = catalog_to_dict("tool")
    tools = {item["name"]: item for item in data["tools"]}

    assert tools["shell_exec"]["summary"].startswith("Run shell commands inside the sandbox")
    assert "inspect the repo" in tools["shell_exec"]["entry_points"]
    assert tools["shell_exec"]["risk_level"] == "high"


def test_catalog_to_dict_marks_current_default_workflow_roles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workflow catalog should reflect configured default workflow roles."""
    fake_config = SimpleNamespace(
        agent=SimpleNamespace(
            workflow_defaults={"chat": "scheduled_job_flow", "grounded": "web_model_grounding"}
        )
    )
    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: fake_config)

    data = catalog_to_dict("workflow")
    workflows = {item["name"]: item for item in data["workflows"]}

    assert "chat" in workflows["scheduled_job_flow"]["default_roles"]
    assert "grounded" in workflows["web_model_grounding"]["default_roles"]
