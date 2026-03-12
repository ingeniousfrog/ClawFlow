"""User-facing catalog for installable extension manifests."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from nanoclaw.core.extension_installer import (
    USER_INSTALLABLE_KINDS,
    evaluate_runtime_extension_policy,
)
from nanoclaw.core.extension_runtime import should_isolate_extension_runtime
from nanoclaw.core.plugins import PluginManifest, get_plugin_registry

_VALID_KINDS = {"skill", "channel", "search_provider"}


class ExtensionEntry(BaseModel):
    """Compact operator-facing view for one extension manifest."""

    name: str
    kind: str
    summary: str
    module: str
    provides: list[str] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list, alias="entryPoints")
    dependencies: list[str] = Field(default_factory=list)
    risk_level: str = Field(default="low", alias="riskLevel")
    source_scope: str = Field(default="", alias="sourceScope")
    enabled: bool = True
    manifest_path: str = Field(default="", alias="manifestPath")
    metadata: dict[str, object] = Field(default_factory=dict)
    trust_status: str = Field(default="", alias="trustStatus")
    trust_detail: str = Field(default="", alias="trustDetail")
    permissions: list[str] = Field(default_factory=list)
    sandbox_policy: str = Field(default="", alias="sandboxPolicy")
    distribution_type: str = Field(default="", alias="distributionType")
    version: str = ""
    publisher: str = ""
    key_id: str = Field(default="", alias="keyId")
    registry_source: str = Field(default="", alias="registrySource")
    signature_verified: bool = Field(default=False, alias="signatureVerified")
    runtime_mode: str = Field(default="", alias="runtimeMode")

    model_config = {"populate_by_name": True}


def catalog_to_dict(kind: str = "all") -> dict[str, Any]:
    """Return the extension registry as a filtered dictionary."""
    if kind != "all" and kind not in _VALID_KINDS:
        raise ValueError(f"Unsupported extension kind: {kind}")

    registry = get_plugin_registry()
    data = {
        "skills": _dump_entries(registry.get_enabled_skill_manifests()),
        "channels": _dump_entries(registry.get_enabled_channel_manifests()),
        "search_providers": _dump_entries(
            registry.get_enabled_search_provider_manifests()
        ),
        "summary": registry.get_extension_summary(),
    }
    if kind == "all":
        return data
    if kind == "skill":
        return {"skills": data["skills"], "summary": data["summary"]}
    if kind == "channel":
        return {"channels": data["channels"], "summary": data["summary"]}
    return {"search_providers": data["search_providers"], "summary": data["summary"]}


def render_extension_text(kind: str = "all") -> str:
    """Render the extension registry as concise plain text."""
    data = catalog_to_dict(kind)
    summary = dict(data.get("summary") or {})
    lines = ["nanoClaw Extensions", "=" * 19]
    lines.append(
        "Summary: "
        f"total={summary.get('total', 0)} "
        f"skills={summary.get('skills', 0)} "
        f"channels={summary.get('channels', 0)} "
        f"searchProviders={summary.get('search_providers', 0)}"
    )
    if "skills" in data:
        lines.extend(_render_section("Skills", data["skills"]))
    if "channels" in data:
        lines.extend(_render_section("Channels", data["channels"]))
    if "search_providers" in data:
        lines.extend(_render_section("Search Providers", data["search_providers"]))
    return "\n".join(lines).rstrip()


def render_extension_json(kind: str = "all") -> str:
    """Render the extension registry as formatted JSON."""
    return json.dumps(catalog_to_dict(kind), indent=2, sort_keys=True)


def _dump_entries(manifests: list[PluginManifest]) -> list[dict[str, Any]]:
    """Convert manifests into a stable JSON-friendly list."""
    entries = []
    for item in manifests:
        policy = _runtime_policy(item)
        entries.append(
            ExtensionEntry(
                name=item.primary_name,
                kind=item.kind,
                summary=item.summary,
                module=item.module,
                provides=item.provided_names,
                entryPoints=list(item.entry_points),
                dependencies=list(item.dependencies),
                riskLevel=item.risk_level,
                sourceScope=item.source_scope,
                enabled=item.enabled,
                manifestPath=item.manifest_path,
                metadata=dict(item.metadata or {}),
                trustStatus=str(policy.get("status") or ""),
                trustDetail=str(policy.get("reason") or ""),
                permissions=_manifest_permissions(item.metadata),
                sandboxPolicy=_manifest_sandbox_policy(item.metadata),
                distributionType=str(policy.get("distribution_type") or ""),
                version=str(policy.get("version") or ""),
                publisher=str(policy.get("publisher") or ""),
                keyId=str(policy.get("key_id") or ""),
                registrySource=str(policy.get("registry_source") or ""),
                signatureVerified=bool(policy.get("signature_verified", False)),
                runtimeMode=_runtime_mode(item),
            )
        )
    return [_model_dump(item) for item in entries]


def _render_section(title: str, items: list[dict[str, Any]]) -> list[str]:
    """Render one extension section."""
    lines = ["", f"{title} ({len(items)})"]
    for item in items:
        provides = ", ".join(item.get("provides", [])) or item["name"]
        entry_points = ", ".join(item.get("entry_points", [])) or "none"
        dependencies = ", ".join(item.get("dependencies", [])) or "none"
        lines.append(f"- {item['name']}: {item['summary']}")
        lines.append(f"  Provides: {provides}")
        lines.append(f"  Module: {item['module']} source={item['source_scope']}")
        lines.append(f"  Entry points: {entry_points}")
        lines.append(
            f"  Dependencies: {dependencies} risk={item['risk_level']}"
        )
        trust_status = str(item.get("trust_status") or "").strip()
        if trust_status:
            lines.append(
                "  Security: "
                f"trust={trust_status} "
                f"sandbox={item.get('sandbox_policy') or '-'} "
                f"permissions={', '.join(item.get('permissions') or []) or '-'}"
            )
            distribution = str(item.get("distribution_type") or "").strip()
            publisher = str(item.get("publisher") or "").strip()
            if distribution or publisher:
                lines.append(
                    "  Distribution: "
                    f"type={distribution or '-'} "
                    f"version={item.get('version') or '-'} "
                    f"publisher={publisher or '-'} "
                    f"keyId={item.get('key_id') or '-'} "
                    f"signatureVerified={str(bool(item.get('signature_verified', False))).lower()}"
                )
                registry_source = str(item.get("registry_source") or "").strip()
                if registry_source:
                    lines.append(f"  Registry: {registry_source}")
            trust_detail = str(item.get("trust_detail") or "").strip()
            if trust_detail:
                lines.append(f"  Trust detail: {trust_detail}")
        runtime_summary = _render_runtime_summary(item)
        if runtime_summary:
            lines.append(f"  Runtime: {runtime_summary}")
    return lines


def _render_runtime_summary(item: dict[str, Any]) -> str:
    """Render one compact runtime summary derived from manifest metadata."""
    metadata = dict(item.get("metadata") or {})
    runtime = dict(metadata.get("runtime") or {})
    contract = dict(metadata.get("contract") or {})
    routing = dict(metadata.get("routing") or {})
    if item.get("kind") == "channel":
        parts = [
            f"delivery={contract.get('deliveryMode') or '-'}",
            f"managed={str(contract.get('managed', True)).lower()}",
        ]
        runtime_mode = str(item.get("runtime_mode") or "").strip()
        if runtime_mode:
            parts.append(f"mode={runtime_mode}")
        if runtime.get("requiredFields"):
            parts.append("required=" + ",".join(runtime["requiredFields"]))
        if routing.get("priorities"):
            parts.append("routing=manifest")
        return " ".join(parts)
    if item.get("kind") == "search_provider":
        parts = []
        runtime_mode = str(item.get("runtime_mode") or "").strip()
        if runtime_mode:
            parts.append(f"mode={runtime_mode}")
        aliases = runtime.get("aliases") or []
        if aliases:
            parts.append("aliases=" + ",".join(str(value) for value in aliases))
        if runtime.get("autoPriority"):
            parts.append(f"autoPriority={runtime['autoPriority']}")
        if runtime.get("secretCapability"):
            parts.append(f"secret={runtime['secretCapability']}")
        return " ".join(parts)
    return ""


def _runtime_policy(item: PluginManifest) -> dict[str, Any]:
    """Evaluate runtime trust policy for one manifest when applicable."""
    if item.kind not in USER_INSTALLABLE_KINDS or item.source_scope != "user":
        return {}
    try:
        from pathlib import Path
        from nanoclaw.core.config import get_config

        policy = get_config().extensions
        result = evaluate_runtime_extension_policy(
            manifest_path=Path(item.manifest_path),
            kind=item.kind,
            primary_name=item.primary_name,
            module_name=item.module.strip(),
            metadata=dict(item.metadata or {}),
            risk_level=item.risk_level,
            require_install_receipt=bool(getattr(policy, "require_install_receipt", True)),
            require_signed_bundles=bool(getattr(policy, "require_signed_bundles", False)),
            max_risk_level=str(getattr(policy, "max_risk_level", "medium") or "medium"),
            publisher_policy=policy,
        )
        return _model_dump(result)
    except Exception:
        return {}

def _runtime_mode(item: PluginManifest) -> str:
    """Return the effective runtime execution mode for one extension."""
    if item.kind not in USER_INSTALLABLE_KINDS:
        return "builtin"
    if item.kind == "channel" and _manifest_channel_supports_incoming(item.metadata):
        return "in_process"
    if should_isolate_extension_runtime(kind=item.kind, source_scope=item.source_scope):
        return "subprocess"
    return "in_process"


def _manifest_permissions(metadata: dict[str, object]) -> list[str]:
    """Return declared permissions from manifest security metadata."""
    security = dict((metadata or {}).get("security") or {})
    value = security.get("permissions")
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _manifest_sandbox_policy(metadata: dict[str, object]) -> str:
    """Return the declared sandbox policy from manifest security metadata."""
    security = dict((metadata or {}).get("security") or {})
    return str(security.get("sandboxPolicy") or "").strip()


def _manifest_channel_supports_incoming(metadata: dict[str, object]) -> bool:
    """Return whether one channel manifest declares incoming handling support."""
    contract = dict((metadata or {}).get("contract") or {})
    return bool(contract.get("supportsIncoming", True))


def _model_dump(item: BaseModel) -> dict[str, Any]:
    """Dump a pydantic model across v1 and v2."""
    if hasattr(item, "model_dump"):
        return item.model_dump(mode="json")
    return item.dict()
