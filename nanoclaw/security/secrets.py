"""Explicit tool-secret capability broker."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from nanoclaw.tools.runtime_context import record_secret_access

POLICY_NAME = "tool_secret_broker"
POLICY_VERSION = "v0"


@dataclass(frozen=True)
class SecretCapabilitySpec:
    """Describe one secret capability exposed to tools."""

    capability: str
    config_attr: str
    config_path: str
    env_names: tuple[str, ...]


_SECRET_CAPABILITIES: dict[str, SecretCapabilitySpec] = {
    "web_search.brave_api_key": SecretCapabilitySpec(
        capability="web_search.brave_api_key",
        config_attr="api_key",
        config_path="tools.webSearch.apiKey",
        env_names=("BRAVE_SEARCH_API_KEY",),
    ),
    "web_search.serper_api_key": SecretCapabilitySpec(
        capability="web_search.serper_api_key",
        config_attr="serper_api_key",
        config_path="tools.webSearch.serperApiKey",
        env_names=("SERPER_API_KEY", "SERP_API_KEY"),
    ),
}

_TOOL_SECRET_ALLOWLIST: dict[str, frozenset[str]] = {
    "web_search": frozenset(_SECRET_CAPABILITIES.keys()),
}


def _get_secret_isolation_config() -> Any:
    """Load secret-isolation config with a safe fallback."""
    from nanoclaw.core.config import Config, get_config

    try:
        return get_config().tools.secret_isolation
    except Exception:
        return Config().tools.secret_isolation


def _record_access(
    *,
    tool_name: str,
    capability: str,
    decision: str,
    source: str,
    reason: str = "",
) -> None:
    """Append one secret-capability decision to the active tool trace."""
    record_secret_access(
        {
            "tool_name": tool_name,
            "capability": capability,
            "decision": decision,
            "source": source,
            "reason": reason,
            "policy_name": POLICY_NAME,
            "policy_version": POLICY_VERSION,
        }
    )


def describe_secret_requirement(capability: str) -> str:
    """Return one stable operator hint for a secret capability."""
    spec = _SECRET_CAPABILITIES.get(capability)
    if spec is None:
        return "Required secret is not configured."
    env_hint = " / ".join(spec.env_names)
    return (
        f"Add `{spec.config_path}` or enable "
        f"`tools.secretIsolation.allowEnvironmentFallback` and set `{env_hint}`."
    )


def resolve_tool_secret(
    capability: str,
    *,
    tool_name: str,
    web_cfg: Any,
    audit_access: bool = True,
) -> str:
    """Resolve one secret only when the requesting tool has explicit capability."""
    spec = _SECRET_CAPABILITIES.get(capability)
    if spec is None:
        if audit_access:
            _record_access(
                tool_name=tool_name,
                capability=capability,
                decision="blocked",
                source="unknown",
                reason="unknown capability",
            )
        return ""

    allowed_capabilities = _TOOL_SECRET_ALLOWLIST.get(tool_name, frozenset())
    if capability not in allowed_capabilities:
        if audit_access:
            _record_access(
                tool_name=tool_name,
                capability=capability,
                decision="blocked",
                source="none",
                reason="tool not allowed",
            )
        return ""

    secret_cfg = _get_secret_isolation_config()
    if bool(getattr(secret_cfg, "allow_environment_fallback", False)):
        for env_name in spec.env_names:
            value = os.environ.get(env_name, "").strip()
            if value:
                if audit_access and bool(getattr(secret_cfg, "audit_access", True)):
                    _record_access(
                        tool_name=tool_name,
                        capability=capability,
                        decision="granted",
                        source=f"env:{env_name}",
                    )
                return value

    value = str(getattr(web_cfg, spec.config_attr, "") or "").strip()
    if value:
        if audit_access and bool(getattr(secret_cfg, "audit_access", True)):
            _record_access(
                tool_name=tool_name,
                capability=capability,
                decision="granted",
                source=f"config:{spec.config_path}",
            )
        return value

    if audit_access and bool(getattr(secret_cfg, "audit_access", True)):
        _record_access(
            tool_name=tool_name,
            capability=capability,
            decision="missing",
            source="none",
        )
    return ""


def has_tool_secret(
    capability: str,
    *,
    tool_name: str,
    web_cfg: Any,
) -> bool:
    """Return whether one tool capability can currently resolve to a secret."""
    return bool(
        resolve_tool_secret(
            capability,
            tool_name=tool_name,
            web_cfg=web_cfg,
            audit_access=False,
        )
    )


def get_tool_secret_capabilities(tool_name: str) -> list[str]:
    """Return sorted secret capabilities exposed to one tool."""
    return sorted(_TOOL_SECRET_ALLOWLIST.get(tool_name, frozenset()))
