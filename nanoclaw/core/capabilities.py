"""User-facing catalog for built-in tools, skills, and default workflows."""

from __future__ import annotations

import json
from typing import Any, List

from pydantic import BaseModel, Field

from nanoclaw.core.plugins import get_plugin_registry
from nanoclaw.core.workflows import WorkflowDefinition, get_workflow_catalog
from nanoclaw.tools.registry import get_tool_registry


class CapabilityEntry(BaseModel):
    """User-facing description for a built-in tool or skill."""

    name: str
    kind: str
    summary: str
    entry_points: List[str] = Field(default_factory=list)
    risk_level: str = Field(default="", alias="riskLevel")


class CapabilityCatalog(BaseModel):
    """Structured catalog consumed by CLI, tools, and docs."""

    tools: List[CapabilityEntry] = Field(default_factory=list)
    skills: List[CapabilityEntry] = Field(default_factory=list)
    workflows: List[WorkflowDefinition] = Field(default_factory=list)


_VALID_KINDS = {"tool", "skill", "workflow"}


def get_capability_catalog() -> CapabilityCatalog:
    """Return the built-in capability catalog."""

    return CapabilityCatalog(
        tools=_get_tool_entries(),
        skills=_get_skill_entries(),
        workflows=_get_workflow_entries(),
    )


def catalog_to_dict(kind: str = "all") -> dict[str, Any]:
    """Return the catalog as a filtered dictionary."""

    if kind != "all" and kind not in _VALID_KINDS:
        raise ValueError(f"Unsupported capability kind: {kind}")

    catalog = get_capability_catalog()
    data = {
        "tools": [_model_dump(item) for item in catalog.tools],
        "skills": [_model_dump(item) for item in catalog.skills],
        "workflows": [_model_dump(item) for item in catalog.workflows],
    }
    if kind == "all":
        return data
    if kind == "tool":
        return {"tools": data["tools"]}
    if kind == "skill":
        return {"skills": data["skills"]}
    return {"workflows": data["workflows"]}


def render_capability_text(kind: str = "all") -> str:
    """Render the catalog as concise plain text."""

    data = catalog_to_dict(kind)
    lines = ["nanoClaw Capabilities", "=" * 21]

    if "tools" in data:
        lines.extend(_render_capability_section("Tools", data["tools"]))
    if "skills" in data:
        lines.extend(_render_capability_section("Skills", data["skills"]))
    if "workflows" in data:
        lines.extend(_render_workflow_section("Workflows", data["workflows"]))

    return "\n".join(lines).rstrip()


def render_capability_json(kind: str = "all") -> str:
    """Render the catalog as formatted JSON."""

    return json.dumps(catalog_to_dict(kind), indent=2, sort_keys=True)


def _render_capability_section(title: str, items: list[dict[str, Any]]) -> list[str]:
    """Render one capability section."""

    lines = ["", f"{title} ({len(items)})"]
    for item in items:
        entry_points = ", ".join(item.get("entry_points", [])) or "direct tool call"
        lines.append(f"- {item['name']}: {item['summary']}")
        lines.append(f"  Entry points: {entry_points}")
        risk_level = str(item.get("risk_level", "") or item.get("riskLevel", "")).strip()
        if risk_level:
            lines.append(f"  Risk: {risk_level}")
    return lines


def _render_workflow_section(title: str, items: list[dict[str, Any]]) -> list[str]:
    """Render one workflow section."""

    lines = ["", f"{title} ({len(items)})"]
    for item in items:
        entry_points = ", ".join(item.get("entry_points", [])) or "normal chat"
        capabilities = ", ".join(item.get("capabilities", [])) or "none"
        lines.append(f"- {item['name']}: {item['summary']}")
        lines.append(f"  Entry points: {entry_points}")
        lines.append(f"  Uses: {capabilities}")
        default_roles = ", ".join(item.get("default_roles", [])) or ", ".join(
            item.get("defaultRoles", [])
        )
        if default_roles:
            lines.append(f"  Default roles: {default_roles}")
        notes = str(item.get("notes", "")).strip()
        if notes:
            lines.append(f"  Notes: {notes}")
    return lines


def _model_dump(item: BaseModel) -> dict[str, Any]:
    """Dump a pydantic model across v1 and v2."""

    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    return item.dict()


def _get_skill_entries() -> list[CapabilityEntry]:
    """Build skill capability entries from plugin manifests."""
    entries: list[CapabilityEntry] = []
    for manifest in get_plugin_registry().get_enabled_skill_manifests():
        entries.append(
            CapabilityEntry(
                name=manifest.primary_tool_name,
                kind="skill",
                summary=manifest.summary,
                entry_points=list(manifest.entry_points),
                riskLevel=manifest.risk_level,
            )
        )
    return entries


def _get_tool_entries() -> list[CapabilityEntry]:
    """Build built-in tool capability entries from the shared tool registry."""
    entries: list[CapabilityEntry] = []
    registry = get_tool_registry()
    for item in sorted(registry.tools.values(), key=lambda value: value.name):
        if not item.source_module.startswith("nanoclaw.tools."):
            continue
        if item.name.endswith("_internal"):
            continue
        entries.append(
            CapabilityEntry(
                name=item.name,
                kind="tool",
                summary=item.catalog_summary or item.description,
                entry_points=list(item.catalog_entry_points),
                riskLevel=item.risk_level,
            )
        )
    return entries


def _get_workflow_entries() -> list[WorkflowDefinition]:
    """Build workflow entries from the shared workflow registry."""
    try:
        from nanoclaw.core.config import get_config

        defaults = get_config().agent.workflow_defaults
    except Exception:
        defaults = None
    return get_workflow_catalog(defaults)
