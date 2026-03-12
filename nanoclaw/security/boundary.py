"""Shared boundary policy for file and outbound web operations."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from nanoclaw.core.config import Config, get_config
from nanoclaw.security.sandbox import SecurityError, get_file_guard
from nanoclaw.tools.runtime_context import record_boundary_decision

POLICY_NAME = "shared_tool_boundary"
POLICY_VERSION = "v0"


def _get_web_search_config() -> Any:
    """Load web-search config, falling back to defaults when config is unavailable."""
    try:
        return get_config().tools.web_search
    except Exception:
        return Config().tools.web_search


async def _run_blocking(func: Any, *args: Any) -> Any:
    """Run a blocking helper in a thread."""
    if hasattr(asyncio, "to_thread"):
        return await asyncio.to_thread(func, *args)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, func, *args)


def _normalize_host_rule(value: str) -> str:
    """Normalize one host policy rule or hostname."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text.startswith("*."):
        text = text[2:]
    parsed = urlparse(text if "://" in text else f"//{text}")
    hostname = (parsed.hostname or text).strip().lower().rstrip(".")
    return hostname


def _host_matches_rule(hostname: str, rule: str) -> bool:
    """Return True when a hostname matches a rule or one of its subdomains."""
    normalized_host = _normalize_host_rule(hostname)
    normalized_rule = _normalize_host_rule(rule)
    if not normalized_host or not normalized_rule:
        return False
    return normalized_host == normalized_rule or normalized_host.endswith(
        f".{normalized_rule}"
    )


def _extract_host_rules(config: Any, attr_name: str, alias_name: str) -> list[str]:
    """Extract one normalized host-rule list from config-like objects."""
    raw_rules = getattr(config, attr_name, None)
    if raw_rules is None:
        raw_rules = getattr(config, alias_name, None)
    if not isinstance(raw_rules, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_rule in raw_rules:
        rule = _normalize_host_rule(str(raw_rule))
        if not rule or rule in seen:
            continue
        seen.add(rule)
        normalized.append(rule)
    return normalized


def _is_private_ip(hostname: str) -> bool:
    """Check if a literal IP is private, loopback, or link-local."""
    try:
        addr = ipaddress.ip_address(hostname)
        return addr.is_private or addr.is_loopback or addr.is_link_local
    except ValueError:
        return False


async def _is_private_host(hostname: str) -> bool:
    """Check whether a hostname resolves to private or loopback IP space."""
    if _is_private_ip(hostname):
        return True

    def _resolve() -> bool:
        try:
            for info in socket.getaddrinfo(hostname, None):
                addr = ipaddress.ip_address(info[4][0])
                if addr.is_private or addr.is_loopback or addr.is_link_local:
                    return True
        except (socket.gaierror, ValueError):
            return False
        return False

    return await _run_blocking(_resolve)


class ToolBoundaryPolicy:
    """Shared boundary policy for tool-facing file and web operations."""

    def _record_decision(
        self,
        *,
        operation: str,
        boundary_kind: str,
        action: str,
        target: str,
        decision: str,
        reason: str = "",
        policy_name: str = POLICY_NAME,
    ) -> None:
        """Record one boundary decision into the active tool trace."""
        if not operation:
            return
        record_boundary_decision(
            {
                "operation": operation,
                "boundary_kind": boundary_kind,
                "action": action,
                "target": target,
                "decision": decision,
                "reason": reason,
                "policy_name": policy_name,
                "policy_version": POLICY_VERSION,
            }
        )

    def validate_workspace_path(
        self,
        path: str,
        *,
        operation: str = "",
    ) -> tuple[bool, str, Optional[Path]]:
        """Resolve one workspace-relative path."""
        guard = get_file_guard()
        try:
            safe_path = guard.validate_path(path)
        except SecurityError as exc:
            self._record_decision(
                operation=operation,
                boundary_kind="workspace_path",
                action="resolve",
                target=path,
                decision="blocked",
                reason=str(exc),
            )
            return False, str(exc), None
        self._record_decision(
            operation=operation,
            boundary_kind="workspace_path",
            action="resolve",
            target=path,
            decision="allowed",
        )
        return True, "", safe_path

    def validate_file_read(
        self,
        path: str,
        *,
        operation: str = "",
    ) -> tuple[bool, str, Optional[Path]]:
        """Validate one tool-facing file-read path."""
        guard = get_file_guard()
        try:
            safe_path = guard.validate_path(path)
        except SecurityError as exc:
            self._record_decision(
                operation=operation,
                boundary_kind="file_path",
                action="read",
                target=path,
                decision="blocked",
                reason=str(exc),
            )
            return False, str(exc), None
        if not guard.is_safe_to_read(safe_path):
            reason = f"ACCESS DENIED: cannot read sensitive file: {path}"
            self._record_decision(
                operation=operation,
                boundary_kind="file_path",
                action="read",
                target=path,
                decision="blocked",
                reason=reason,
            )
            return False, reason, None
        self._record_decision(
            operation=operation,
            boundary_kind="file_path",
            action="read",
            target=path,
            decision="allowed",
        )
        return True, "", safe_path

    def validate_file_write(
        self,
        path: str,
        *,
        operation: str = "",
    ) -> tuple[bool, str, Optional[Path]]:
        """Validate one tool-facing file-write path."""
        guard = get_file_guard()
        try:
            safe_path = guard.validate_path(path)
        except SecurityError as exc:
            self._record_decision(
                operation=operation,
                boundary_kind="file_path",
                action="write",
                target=path,
                decision="blocked",
                reason=str(exc),
            )
            return False, str(exc), None
        if not guard.is_safe_to_write(safe_path):
            reason = f"ACCESS DENIED: cannot write to sensitive path: {path}"
            self._record_decision(
                operation=operation,
                boundary_kind="file_path",
                action="write",
                target=path,
                decision="blocked",
                reason=reason,
            )
            return False, reason, None
        self._record_decision(
            operation=operation,
            boundary_kind="file_path",
            action="write",
            target=path,
            decision="allowed",
        )
        return True, "", safe_path

    async def validate_outbound_url(
        self,
        url: str,
        web_cfg: Any | None = None,
        *,
        operation: str = "",
    ) -> tuple[bool, str, str]:
        """Validate one outbound URL against SSRF and configured host policy."""
        config = web_cfg or _get_web_search_config()
        try:
            parsed = urlparse(url)
        except Exception:
            self._record_decision(
                operation=operation,
                boundary_kind="outbound_url",
                action="fetch",
                target=url,
                decision="blocked",
                reason="Invalid URL: parse error",
            )
            return False, "", "Invalid URL: parse error"

        hostname = _normalize_host_rule(parsed.hostname or "")
        if parsed.scheme not in {"http", "https"} or not hostname:
            reason = "Invalid URL: unsupported scheme or missing hostname"
            self._record_decision(
                operation=operation,
                boundary_kind="outbound_url",
                action="fetch",
                target=url,
                decision="blocked",
                reason=reason,
            )
            return False, "", "Invalid URL: unsupported scheme or missing hostname"

        try:
            if await _is_private_host(hostname):
                reason = (
                    f"BLOCKED: outbound host `{hostname}` "
                    "resolves to private/internal address"
                )
                self._record_decision(
                    operation=operation,
                    boundary_kind="outbound_url",
                    action="fetch",
                    target=url,
                    decision="blocked",
                    reason=reason,
                )
                return (
                    False,
                    hostname,
                    reason,
                )
        except Exception:
            reason = f"BLOCKED: failed to validate outbound host `{hostname}`"
            self._record_decision(
                operation=operation,
                boundary_kind="outbound_url",
                action="fetch",
                target=url,
                decision="blocked",
                reason=reason,
            )
            return (
                False,
                hostname,
                reason,
            )

        blocked_rules = _extract_host_rules(config, "blocked_hosts", "blockedHosts")
        if any(_host_matches_rule(hostname, rule) for rule in blocked_rules):
            reason = (
                "BLOCKED: outbound host "
                f"`{hostname}` is denied by `tools.webSearch.blockedHosts`"
            )
            self._record_decision(
                operation=operation,
                boundary_kind="outbound_url",
                action="fetch",
                target=url,
                decision="blocked",
                reason=reason,
            )
            return (
                False,
                hostname,
                reason,
            )

        allowed_rules = _extract_host_rules(config, "allowed_hosts", "allowedHosts")
        if allowed_rules and not any(
            _host_matches_rule(hostname, rule) for rule in allowed_rules
        ):
            reason = (
                "BLOCKED: outbound host "
                f"`{hostname}` is not allowed by `tools.webSearch.allowedHosts`"
            )
            self._record_decision(
                operation=operation,
                boundary_kind="outbound_url",
                action="fetch",
                target=url,
                decision="blocked",
                reason=reason,
            )
            return (
                False,
                hostname,
                reason,
            )

        self._record_decision(
            operation=operation,
            boundary_kind="outbound_url",
            action="fetch",
            target=url,
            decision="allowed",
        )
        return True, hostname, ""


_tool_boundary_policy: Optional[ToolBoundaryPolicy] = None


def get_tool_boundary_policy() -> ToolBoundaryPolicy:
    """Get the shared boundary policy instance."""
    global _tool_boundary_policy
    if _tool_boundary_policy is None:
        _tool_boundary_policy = ToolBoundaryPolicy()
    return _tool_boundary_policy


def set_tool_boundary_policy(policy: ToolBoundaryPolicy) -> None:
    """Override the shared boundary policy instance."""
    global _tool_boundary_policy
    _tool_boundary_policy = policy


def get_outbound_host_policy_summary(config: Any) -> dict[str, Any]:
    """Return one compact summary of the configured outbound host policy."""
    web_cfg = getattr(getattr(config, "tools", None), "web_search", None)
    if web_cfg is None:
        web_cfg = _get_web_search_config()
    allowed_hosts = _extract_host_rules(web_cfg, "allowed_hosts", "allowedHosts")
    blocked_hosts = _extract_host_rules(web_cfg, "blocked_hosts", "blockedHosts")
    return {
        "policy_name": POLICY_NAME,
        "policy_version": POLICY_VERSION,
        "allowed_hosts_count": len(allowed_hosts),
        "blocked_hosts_count": len(blocked_hosts),
        "host_policy_enabled": bool(allowed_hosts or blocked_hosts),
    }
