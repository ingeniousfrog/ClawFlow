"""Operator-facing summary of the current boundary policy contract."""

from __future__ import annotations

from typing import Any

from nanoclaw.security.boundary import get_outbound_host_policy_summary
from nanoclaw.security.sandbox_backends import (
    DEFAULT_SHELL_BACKEND,
    PRIMARY_CONTAINER_BACKEND,
    get_container_remediation_plan,
    inspect_container_backend_health,
    resolve_shell_backend,
)
from nanoclaw.security.secrets import (
    POLICY_NAME as SECRET_POLICY_NAME,
    POLICY_VERSION as SECRET_POLICY_VERSION,
    get_tool_secret_capabilities,
)

CONTRACT_VERSION = "r1-f.v6"


def build_boundary_policy_contract(config: Any) -> dict[str, Any]:
    """Return one compact summary of the active boundary policy contract."""
    tools_cfg = getattr(config, "tools", None)
    shell_cfg = getattr(tools_cfg, "shell", None)
    secret_cfg = getattr(tools_cfg, "secret_isolation", None)
    host_policy = get_outbound_host_policy_summary(config)
    web_search_capabilities = get_tool_secret_capabilities("web_search")
    container_image = str(getattr(shell_cfg, "container_image", "") or "")
    shell_backend = resolve_shell_backend(
        getattr(shell_cfg, "backend", DEFAULT_SHELL_BACKEND),
        container_image=container_image,
    )
    primary_container = inspect_container_backend_health(
        backend=PRIMARY_CONTAINER_BACKEND,
        container_image=container_image,
    )
    remediation = get_container_remediation_plan(
        primary_container,
        backend=PRIMARY_CONTAINER_BACKEND,
        container_image=container_image,
    )

    return {
        "contract_version": CONTRACT_VERSION,
        "shell": {
            "mode": getattr(shell_cfg, "mode", "subprocess"),
            "backend_requested": str(shell_backend["requested"]),
            "backend_selected": str(shell_backend["selected"]),
            "stronger_backend_available": bool(
                shell_backend["stronger_backend_available"]
            ),
            "available_backends": list(shell_backend["available_backends"]),
            "container_image_configured": bool(
                container_image.strip()
            ),
            "primary_container_target": {
                **primary_container,
                "remediation_steps": list(remediation["steps"]),
                "remediation_commands": list(remediation["commands"]),
                "verify_command": str(remediation["verify_command"]),
                "prepare_command": str(remediation["prepare_command"]),
                "start_runtime_command": str(remediation["start_runtime_command"]),
                "restart_runtime_command": str(remediation["restart_runtime_command"]),
                "runtime_command": str(remediation["runtime_command"]),
                "pull_command": str(remediation["pull_command"]),
            },
            "confirm_dangerous": bool(getattr(shell_cfg, "confirm_dangerous", True)),
            "isolate_home": bool(getattr(shell_cfg, "isolate_home", True)),
            "max_memory_mb": int(getattr(shell_cfg, "max_memory_mb", 512) or 0),
            "max_file_size_kb": int(getattr(shell_cfg, "max_file_size_kb", 8192) or 0),
        },
        "web_hosts": host_policy,
        "secrets": {
            "policy_name": SECRET_POLICY_NAME,
            "policy_version": SECRET_POLICY_VERSION,
            "allow_environment_fallback": bool(
                getattr(secret_cfg, "allow_environment_fallback", False)
            ),
            "audit_access": bool(getattr(secret_cfg, "audit_access", True)),
            "web_search_capabilities": web_search_capabilities,
            "web_search_capability_count": len(web_search_capabilities),
        },
    }
