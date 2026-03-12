"""Built-in workflow registry and default workflow selection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class WorkflowDefinition(BaseModel):
    """User-facing description for one built-in workflow."""

    name: str
    summary: str
    entry_points: list[str] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    notes: str = ""
    default_roles: list[str] = Field(default_factory=list, alias="defaultRoles")
    structured_match_terms: list[str] = Field(
        default_factory=list,
        alias="structuredMatchTerms",
    )

    model_config = {"populate_by_name": True}


_WORKFLOW_DEFAULTS = {
    "chat": "default_chat_loop",
    "grounded": "grounded_current_info",
    "web_model": "web_model_grounding",
    "scheduled": "scheduled_job_flow",
    "heartbeat": "heartbeat_checklist",
    "feishu_paper": "feishu_paper_template",
    "wechat_article": "wechat_article_flow",
}

def get_workflow_catalog_path() -> Path:
    """Return the packaged workflow catalog data path."""
    return Path(__file__).with_name("workflow_catalog.json")


def _load_workflow_catalog_data() -> list[WorkflowDefinition]:
    """Load built-in workflow metadata from packaged JSON."""
    raw = json.loads(get_workflow_catalog_path().read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("workflow catalog must be a list")
    return [WorkflowDefinition(**item) for item in raw if isinstance(item, dict)]


_WORKFLOWS = _load_workflow_catalog_data()

_WORKFLOWS_BY_NAME = {item.name: item for item in _WORKFLOWS}
_STRUCTURED_WORKFLOWS = [
    item for item in _WORKFLOWS if item.structured_match_terms
]


def resolve_workflow_defaults(defaults: Any | None = None) -> dict[str, str]:
    """Resolve workflow defaults from config-like values with safe fallbacks."""
    resolved = dict(_WORKFLOW_DEFAULTS)
    if defaults is None:
        return resolved

    if isinstance(defaults, dict):
        values = defaults
    elif hasattr(defaults, "model_dump"):
        values = defaults.model_dump()  # type: ignore[union-attr]
    elif hasattr(defaults, "dict"):
        values = defaults.dict()  # type: ignore[union-attr]
    else:
        values = {role: getattr(defaults, role, "") for role in _WORKFLOW_DEFAULTS}

    for role, fallback in _WORKFLOW_DEFAULTS.items():
        candidate = str(values.get(role, "") or "").strip()
        if candidate in _WORKFLOWS_BY_NAME:
            resolved[role] = candidate
        else:
            resolved[role] = fallback
    return resolved


def get_workflow_catalog(defaults: Any | None = None) -> list[WorkflowDefinition]:
    """Return workflow definitions annotated with the current default roles."""
    role_map = resolve_workflow_defaults(defaults)
    default_roles: dict[str, list[str]] = {}
    for role, workflow_name in role_map.items():
        default_roles.setdefault(workflow_name, []).append(role)

    catalog: list[WorkflowDefinition] = []
    for item in _WORKFLOWS:
        copied = _model_copy(item)
        copied.default_roles = sorted(default_roles.get(item.name, []))
        catalog.append(copied)
    return catalog


def matches_structured_grounding_workflow(user_message: str) -> bool:
    """Return True when the request clearly matches a built-in structured workflow."""
    text = user_message.lower()
    for item in _STRUCTURED_WORKFLOWS:
        if any(token in text for token in item.structured_match_terms):
            return True
    return False


def build_workflow_tags(
    *,
    session_id: str,
    user_message: str,
    tool_names: list[str],
    needs_grounded: bool,
    direct_web_used: bool,
    defaults: Any | None = None,
) -> list[str]:
    """Build workflow tags from the shared registry and configured defaults."""
    role_map = resolve_workflow_defaults(defaults)
    tags = [role_map["chat"]]
    if session_id.startswith("heartbeat:"):
        tags.append(role_map["heartbeat"])
    if session_id.startswith("cron:"):
        tags.append(role_map["scheduled"])
    if user_message.lstrip().startswith("/paper"):
        tags.append(role_map["feishu_paper"])
    if "wechat_article_assist" in tool_names:
        tags.append(role_map["wechat_article"])
    if direct_web_used:
        tags.append(role_map["web_model"])
    if needs_grounded or any(
        name in {"daily_digest", "hotspot_brief", "paper_search"} for name in tool_names
    ):
        tags.append(role_map["grounded"])
    return list(dict.fromkeys(tags))


def classify_primary_workflow(
    *,
    session_id: str,
    user_message: str,
    tool_names: list[str],
    workflow_tags: list[str],
    direct_web_used: bool,
    defaults: Any | None = None,
) -> str:
    """Choose the primary workflow label from the shared registry defaults."""
    role_map = resolve_workflow_defaults(defaults)
    if session_id.startswith("heartbeat:"):
        return role_map["heartbeat"]
    if session_id.startswith("cron:"):
        return role_map["scheduled"]
    if user_message.lstrip().startswith("/paper"):
        return role_map["feishu_paper"]
    if "wechat_article_assist" in tool_names:
        return role_map["wechat_article"]
    if direct_web_used:
        return role_map["web_model"]
    if role_map["grounded"] in workflow_tags:
        return role_map["grounded"]
    return role_map["chat"]


def _model_copy(item: WorkflowDefinition) -> WorkflowDefinition:
    """Copy a pydantic model across supported versions."""
    if hasattr(item, "model_copy"):
        return item.model_copy(deep=True)
    return item.copy(deep=True)
