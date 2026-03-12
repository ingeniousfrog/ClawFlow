"""Operator-facing channel auth, lifecycle, routing, and diagnostics contract."""

from __future__ import annotations

from typing import Any

from nanoclaw.channels.registry import ChannelRuntimeSpec, get_channel_runtime_registry

CONTRACT_VERSION = "r2-f.v0"
_ROUTE_PURPOSES = (
    "default_proactive",
    "heartbeat",
    "runtime_alert",
    "runtime_alert_escalation",
)


def build_channel_contract(config: Any, gateway: Any | None = None) -> dict[str, Any]:
    """Return one compact operator-facing channel contract."""
    channels = _build_channel_entries(config, gateway)
    summary = _build_summary(channels)
    routing_policy = _build_routing_policy(config, gateway, channels)
    orchestration = _build_orchestration_policy(gateway, channels)
    _apply_route_roles(channels, routing_policy)
    return {
        "contract_version": CONTRACT_VERSION,
        "summary": summary,
        "routing_policy": routing_policy,
        "orchestration": orchestration,
        "channels": channels,
    }


def resolve_channel_route(
    config: Any,
    gateway: Any | None = None,
    *,
    purpose: str,
    preferred_channel: str = "",
    exclude_channels: set[str] | None = None,
    channels: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resolve one operator-facing route decision for a proactive purpose."""
    if channels is None:
        channels = _build_channel_entries(config, gateway)
    registry = get_channel_runtime_registry()
    runtime_available = _get_runtime_available_channels(gateway)
    requested = str(preferred_channel or "").strip().lower()
    explicit_requested = bool(requested and requested not in {"auto", "none"})
    fallback_order = registry.fallback_order(purpose)
    excluded = {item for item in set(exclude_channels or set()) if item}
    candidates = _build_route_candidates(
        requested if explicit_requested else "",
        fallback_order,
        excluded,
    )
    blocked_candidates: list[dict[str, str]] = []
    runtime_candidate = ""
    configured_candidate = ""
    configured_reason = ""

    for candidate in candidates:
        readiness, reason = _get_route_candidate_readiness(
            candidate,
            channels,
            runtime_available,
        )
        if readiness == "runtime":
            runtime_candidate = candidate
            configured_reason = reason
            break
        if readiness == "configured_only" and not configured_candidate:
            configured_candidate = candidate
            configured_reason = reason
        blocked_candidates.append({"channel": candidate, "reason": reason})

    selected = runtime_candidate or configured_candidate
    if runtime_candidate:
        status = "ready"
    elif configured_candidate:
        status = "configured_only"
    else:
        status = "unresolved"
    selection_kind = _resolve_selection_kind(
        selected,
        requested if explicit_requested else "",
        runtime_candidate=bool(runtime_candidate),
    )
    reason = _resolve_route_reason(
        status=status,
        requested=requested if explicit_requested else "",
        selected=selected,
        blocked_candidates=blocked_candidates,
    )
    return {
        "purpose": purpose,
        "requested_channel": requested if explicit_requested else "",
        "selected_channel": selected,
        "status": status,
        "selection_kind": selection_kind,
        "reason": reason,
        "detail": configured_reason,
        "candidate_channels": candidates,
        "blocked_candidates": blocked_candidates[:3],
        "runtime_available_channels": runtime_available,
    }


def _build_channel_entries(
    config: Any,
    gateway: Any | None,
) -> dict[str, dict[str, Any]]:
    """Build per-channel entries from config plus runtime overlay."""
    registry = get_channel_runtime_registry()
    runtime = _get_runtime_overlay(gateway)
    diagnostics = _get_diagnostics_overlay(gateway)
    orchestration = _get_orchestration_overlay(gateway)
    channels: dict[str, dict[str, Any]] = {}
    for spec in registry.all_specs():
        cfg = spec.get_channel_config(config)
        channels[spec.name] = _build_channel_entry(
            spec,
            cfg,
            runtime.get(spec.name),
            diagnostics.get(spec.name),
            orchestration.get(spec.name),
        )
    return channels


def _build_summary(channels: dict[str, dict[str, Any]]) -> dict[str, int]:
    """Build one compact summary across all managed channels."""
    summary = {
        "known_count": len(channels),
        "enabled_count": 0,
        "configured_count": 0,
        "running_count": 0,
        "failed_count": 0,
        "disabled_count": 0,
        "misconfigured_count": 0,
        "allowlist_auth_count": 0,
        "open_auth_count": 0,
        "proactive_ready_count": 0,
        "diagnostic_attention_count": 0,
        "desired_running_count": 0,
        "drifted_count": 0,
        "blocked_count": 0,
        "reconciling_count": 0,
    }
    for entry in channels.values():
        managed = bool(entry.get("supports_operator_control"))
        if entry["enabled"]:
            summary["enabled_count"] += 1
        if entry["configured"]:
            summary["configured_count"] += 1
        if entry["status"] == "running":
            summary["running_count"] += 1
        if entry["status"] == "failed":
            summary["failed_count"] += 1
        if entry["status"] == "disabled":
            summary["disabled_count"] += 1
        if entry["status"] == "misconfigured":
            summary["misconfigured_count"] += 1
        if entry["enabled"] and entry["auth_mode"] == "allowlist":
            summary["allowlist_auth_count"] += 1
        if entry["enabled"] and entry["auth_mode"] == "open":
            summary["open_auth_count"] += 1
        if entry["routing_ready"]:
            summary["proactive_ready_count"] += 1
        if entry["diagnostic_health"] == "attention":
            summary["diagnostic_attention_count"] += 1
        if managed and entry["desired_state"] == "running":
            summary["desired_running_count"] += 1
        if managed and entry["drift_status"] == "drifted":
            summary["drifted_count"] += 1
        if managed and entry["drift_status"] == "blocked":
            summary["blocked_count"] += 1
        if managed and entry["reconcile_status"] == "reconciling":
            summary["reconciling_count"] += 1
    return summary


def _build_channel_entry(
    spec: ChannelRuntimeSpec,
    cfg: Any | None,
    runtime: dict[str, object] | None,
    diagnostics: dict[str, object] | None,
    orchestration: dict[str, object] | None,
) -> dict[str, Any]:
    """Build one channel contract entry from config plus runtime overlay."""
    name = spec.name
    managed = bool(spec.managed)
    runtime_status = str((runtime or {}).get("status") or "")
    enabled = bool(_cfg_value(cfg, "enabled", False))
    allow_from = list(_cfg_value(cfg, "allow_from", []) or [])
    auth_mode = spec.auth_mode or ("allowlist" if allow_from else "open")
    auth_configured = auth_mode == "local" or bool(allow_from)

    if spec.proactive_target_mode == "runtime_only":
        configured = bool(runtime_status)
        enabled = runtime_status in {"starting", "running"}
        default_target_configured = configured
        proactive_target_mode = "interactive_session"
        base_detail = (
            spec.proactive_target_ready_detail
            if configured
            else spec.proactive_target_missing_reason
        )
    else:
        configured = _is_required_fields_set(cfg, spec.required_fields)
        if spec.proactive_target_mode == "broadcast_allowlist":
            default_target_configured = bool(allow_from)
            proactive_target_mode = "broadcast_allowlist"
        elif spec.proactive_target_mode == "default_field":
            default_target_configured = _is_field_set(cfg, spec.proactive_target_field)
            proactive_target_mode = "default_chat"
        else:
            default_target_configured = auth_configured
            proactive_target_mode = spec.proactive_target_mode or "default"
        base_detail = (
            spec.ready_reason
            if configured
            else spec.missing_reason
        )

    if spec.proactive_target_mode == "runtime_only":
        status = "stopped"
        if runtime_status:
            status = runtime_status
            configured = True
            enabled = runtime_status in {"starting", "running"}
    else:
        status = "disabled"
        if enabled:
            status = "configured" if configured else "misconfigured"
        if runtime_status:
            status = runtime_status
            if runtime_status != "disabled":
                enabled = True
            if runtime_status in {"starting", "running", "stopped"}:
                configured = True

    detail = str((runtime or {}).get("detail") or "").strip()
    if not detail:
        if spec.proactive_target_mode == "runtime_only" and status == "stopped":
            detail = spec.proactive_target_missing_reason or "channel runtime inactive"
        elif status == "disabled":
            detail = "disabled in config"
        elif status == "configured":
            detail = "configured"
        else:
            detail = base_detail

    routing_ready, routing_detail = _resolve_routing_target_state(
        spec,
        status=status,
        allow_from=allow_from,
        default_target_configured=default_target_configured,
        proactive_target_mode=proactive_target_mode,
        base_detail=base_detail,
    )
    diagnostic_view = _build_diagnostic_view(status, diagnostics)
    orchestration_view = _build_orchestration_view(
        enabled=enabled,
        status=status,
        detail=detail,
        orchestration=orchestration,
    )
    return {
        "label": spec.label,
        "managed": managed,
        "enabled": enabled,
        "configured": configured,
        "status": status,
        "health": _status_to_health(status),
        "delivery_mode": spec.delivery_mode,
        "auth_mode": auth_mode,
        "auth_configured": auth_configured,
        "auth_detail": _resolve_auth_detail(
            spec=spec,
            status=status,
            auth_mode=auth_mode,
            allowlist_count=len(allow_from),
        ),
        "allowlist_count": len(allow_from),
        "default_target_configured": default_target_configured,
        "supports_incoming": spec.supports_incoming,
        "supports_proactive": spec.supports_proactive,
        "supports_targeted_proactive": spec.supports_targeted_proactive,
        "supports_confirmation": spec.supports_confirmation,
        "proactive_target_mode": proactive_target_mode,
        "routing_ready": routing_ready,
        "routing_detail": routing_detail,
        "route_roles": [],
        "diagnostic_health": diagnostic_view["health"],
        "diagnostic_summary": diagnostic_view["summary"],
        "diagnostics": diagnostic_view["stats"],
        "desired_state": orchestration_view["desired_state"],
        "desired_reason": orchestration_view["desired_reason"],
        "desired_updated_at": orchestration_view["desired_updated_at"],
        "actual_status": orchestration_view["actual_status"],
        "actual_detail": orchestration_view["actual_detail"],
        "drift_status": orchestration_view["drift_status"],
        "drift_summary": orchestration_view["drift_summary"],
        "drift_since": orchestration_view["drift_since"],
        "drift_count": orchestration_view["drift_count"],
        "reconcile_status": orchestration_view["reconcile_status"],
        "reconcile_detail": orchestration_view["reconcile_detail"],
        "last_reconciled_at": orchestration_view["last_reconciled_at"],
        "last_action": orchestration_view["last_action"],
        "last_action_at": orchestration_view["last_action_at"],
        "detail": detail,
        "last_error": str((runtime or {}).get("last_error") or ""),
        "last_transition_at": int((runtime or {}).get("last_transition_at") or 0),
        "supports_operator_control": managed,
        "operator_actions": (
            _resolve_operator_actions(
                status,
                desired_state=orchestration_view["desired_state"],
                drift_status=orchestration_view["drift_status"],
                reconcile_status=orchestration_view["reconcile_status"],
            )
            if managed
            else []
        ),
    }


def _build_routing_policy(
    config: Any,
    gateway: Any | None,
    channels: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build one compact routing-policy summary for operator views."""
    heartbeat_cfg = getattr(config, "heartbeat", None)
    tools_cfg = getattr(config, "tools", None)
    background_cfg = getattr(tools_cfg, "background_tasks", None)
    runtime_alert = resolve_channel_route(
        config,
        gateway,
        purpose="runtime_alert",
        preferred_channel=str(getattr(background_cfg, "alert_channel", "") or ""),
        channels=channels,
    )
    return {
        "policy_version": CONTRACT_VERSION,
        "default_proactive": resolve_channel_route(
            config,
            gateway,
            purpose="default_proactive",
            channels=channels,
        ),
        "heartbeat": resolve_channel_route(
            config,
            gateway,
            purpose="heartbeat",
            preferred_channel=str(getattr(heartbeat_cfg, "notify_channel", "") or ""),
            channels=channels,
        ),
        "runtime_alert": runtime_alert,
        "runtime_alert_escalation": resolve_channel_route(
            config,
            gateway,
            purpose="runtime_alert_escalation",
            preferred_channel=str(
                getattr(background_cfg, "alert_escalation_channel", "") or ""
            ),
            exclude_channels={str(runtime_alert.get("selected_channel") or "")},
            channels=channels,
        ),
    }


def _build_diagnostic_view(
    status: str,
    diagnostics: dict[str, object] | None,
) -> dict[str, object]:
    """Build one compact diagnostics view for one channel."""
    values = dict(diagnostics or {})
    stats = {
        "incoming_total": int(values.get("incoming_total") or 0),
        "incoming_successes": int(values.get("incoming_successes") or 0),
        "incoming_failures": int(values.get("incoming_failures") or 0),
        "outgoing_total": int(values.get("outgoing_total") or 0),
        "outgoing_successes": int(values.get("outgoing_successes") or 0),
        "outgoing_failures": int(values.get("outgoing_failures") or 0),
        "targeted_outgoing_total": int(values.get("targeted_outgoing_total") or 0),
        "targeted_outgoing_successes": int(values.get("targeted_outgoing_successes") or 0),
        "targeted_outgoing_failures": int(values.get("targeted_outgoing_failures") or 0),
        "last_incoming_at": int(values.get("last_incoming_at") or 0),
        "last_incoming_session": str(values.get("last_incoming_session") or ""),
        "last_outgoing_at": int(values.get("last_outgoing_at") or 0),
        "last_outgoing_status": str(values.get("last_outgoing_status") or ""),
        "last_outgoing_kind": str(values.get("last_outgoing_kind") or ""),
        "last_outgoing_target": str(values.get("last_outgoing_target") or ""),
        "last_success_at": int(values.get("last_success_at") or 0),
        "last_failure_at": int(values.get("last_failure_at") or 0),
        "last_failure_kind": str(values.get("last_failure_kind") or ""),
        "last_failure_error": str(values.get("last_failure_error") or ""),
        "last_runtime_status": str(values.get("last_runtime_status") or ""),
        "last_runtime_transition_at": int(values.get("last_runtime_transition_at") or 0),
    }
    has_recent_failure = stats["last_failure_at"] > max(stats["last_success_at"], 0)
    if status in {"failed", "misconfigured"}:
        health = "failed"
    elif status == "disabled":
        health = "disabled"
    elif status == "starting":
        health = "starting"
    elif status == "stopped":
        health = "stopped"
    elif has_recent_failure or stats["last_outgoing_status"] == "error":
        health = "attention"
    elif (
        stats["incoming_total"] > 0
        or stats["outgoing_total"] > 0
        or stats["targeted_outgoing_total"] > 0
    ):
        health = "healthy"
    elif status == "running":
        health = "idle"
    else:
        health = "ready"
    return {
        "health": health,
        "summary": _build_diagnostic_summary(status, health, stats),
        "stats": stats,
    }


def _build_diagnostic_summary(
    status: str,
    health: str,
    stats: dict[str, object],
) -> str:
    """Return one compact human-readable diagnostics summary."""
    if health == "attention":
        kind = str(stats["last_failure_kind"] or "delivery")
        error = str(stats["last_failure_error"] or "recent channel failure")
        return f"{kind} failed: {error}"
    if health == "healthy":
        if int(stats["targeted_outgoing_total"]) > 0:
            return "recent targeted proactive delivery succeeded"
        if int(stats["outgoing_total"]) > 0:
            return "recent proactive delivery succeeded"
        return "recent incoming request handled"
    if health == "idle":
        return "runtime active with no recent channel traffic"
    if health == "ready":
        return "configured but no runtime diagnostics yet"
    if status == "stopped":
        return "runtime stopped"
    if status == "disabled":
        return "channel disabled in config"
    if status == "failed":
        return "runtime reported a channel failure"
    if status == "misconfigured":
        return "channel configuration incomplete"
    return "runtime starting"


def _build_orchestration_view(
    *,
    enabled: bool,
    status: str,
    detail: str,
    orchestration: dict[str, object] | None,
) -> dict[str, object]:
    """Build one compact desired-state orchestration view for a channel."""
    values = dict(orchestration or {})
    desired_state = str(
        values.get("desired_state") or ("running" if enabled else "stopped")
    )
    actual_status = str(values.get("actual_status") or status or "")
    actual_detail = str(values.get("actual_detail") or detail or "")
    drift_status = str(
        values.get("drift_status")
        or _resolve_drift_status(desired_state, actual_status)
    )
    reconcile_status = str(
        values.get("reconcile_status")
        or _default_reconcile_status(drift_status)
    )
    reconcile_detail = str(
        values.get("reconcile_detail")
        or _build_orchestration_summary(
            desired_state=desired_state,
            actual_status=actual_status,
            actual_detail=actual_detail,
            drift_status=drift_status,
            reconcile_status=reconcile_status,
        )
    )
    return {
        "desired_state": desired_state,
        "desired_reason": str(values.get("desired_reason") or "config default"),
        "desired_updated_at": int(values.get("desired_updated_at") or 0),
        "actual_status": actual_status,
        "actual_detail": actual_detail,
        "drift_status": drift_status,
        "drift_summary": _build_orchestration_summary(
            desired_state=desired_state,
            actual_status=actual_status,
            actual_detail=actual_detail,
            drift_status=drift_status,
            reconcile_status=reconcile_status,
        ),
        "drift_since": int(values.get("drift_since") or 0),
        "drift_count": int(values.get("drift_count") or 0),
        "reconcile_status": reconcile_status,
        "reconcile_detail": reconcile_detail,
        "last_reconciled_at": int(values.get("last_reconciled_at") or 0),
        "last_action": str(values.get("last_action") or ""),
        "last_action_at": int(values.get("last_action_at") or 0),
    }


def _build_orchestration_summary(
    *,
    desired_state: str,
    actual_status: str,
    actual_detail: str,
    drift_status: str,
    reconcile_status: str,
) -> str:
    """Return one compact operator-facing orchestration summary."""
    if drift_status == "in_sync":
        return f"desired `{desired_state}` is satisfied"
    if drift_status == "converging":
        return f"reconciling toward desired `{desired_state}`"
    if drift_status == "blocked":
        return actual_detail or f"desired `{desired_state}` is blocked"
    if reconcile_status == "drifted":
        return f"desired `{desired_state}` differs from actual `{actual_status or '-'}`"
    return (
        actual_detail
        or f"actual `{actual_status or '-'}` differs from desired `{desired_state}`"
    )


def _apply_route_roles(
    channels: dict[str, dict[str, Any]],
    routing_policy: dict[str, Any],
) -> None:
    """Attach selected route roles back onto channel entries."""
    for name in channels:
        channels[name]["route_roles"] = []
    for purpose in _ROUTE_PURPOSES:
        route = dict(routing_policy.get(purpose) or {})
        selected = str(route.get("selected_channel") or "")
        if selected in channels:
            channels[selected]["route_roles"].append(purpose)


def _build_orchestration_policy(
    gateway: Any | None,
    channels: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build one compact desired-state orchestration summary."""
    managed_channels = [
        entry for entry in channels.values() if entry.get("supports_operator_control")
    ]
    return {
        "policy_version": CONTRACT_VERSION,
        "reconcile_interval_seconds": int(
            getattr(gateway, "_RECONCILE_INTERVAL_SECONDS", 0) or 0
        ),
        "desired_running_count": sum(
            1 for entry in managed_channels if entry["desired_state"] == "running"
        ),
        "desired_stopped_count": sum(
            1 for entry in managed_channels if entry["desired_state"] == "stopped"
        ),
        "drifted_count": sum(
            1 for entry in managed_channels if entry["drift_status"] == "drifted"
        ),
        "blocked_count": sum(
            1 for entry in managed_channels if entry["drift_status"] == "blocked"
        ),
        "reconciling_count": sum(
            1
            for entry in managed_channels
            if entry["reconcile_status"] == "reconciling"
        ),
    }


def _resolve_auth_detail(
    *,
    spec: ChannelRuntimeSpec,
    status: str,
    auth_mode: str,
    allowlist_count: int,
) -> str:
    """Return one human-readable incoming-auth summary."""
    if auth_mode == "local":
        return spec.auth_detail or "local runtime only"
    if status == "disabled":
        return "channel disabled in config"
    if auth_mode == "allowlist":
        return f"{allowlist_count} sender(s) allowed"
    return "open incoming access"


def _resolve_routing_target_state(
    spec: ChannelRuntimeSpec,
    *,
    status: str,
    allow_from: list[str],
    default_target_configured: bool,
    proactive_target_mode: str,
    base_detail: str,
) -> tuple[bool, str]:
    """Return whether config-level proactive routing is ready for one channel."""
    if proactive_target_mode == "interactive_session":
        if status == "running":
            return True, spec.proactive_target_ready_detail or "runtime active"
        return False, spec.proactive_target_missing_reason or "runtime inactive"
    if status == "disabled":
        return False, "channel disabled in config"
    if status == "misconfigured":
        return False, base_detail
    if proactive_target_mode == "broadcast_allowlist":
        if not allow_from:
            return False, spec.proactive_target_missing_reason
        detail = spec.proactive_target_ready_detail or "broadcast target configured"
        return True, detail.format(count=len(allow_from))
    if not default_target_configured:
        return False, spec.proactive_target_missing_reason
    return True, spec.proactive_target_ready_detail


def _build_route_candidates(
    requested: str,
    fallback_order: list[str],
    excluded: set[str],
) -> list[str]:
    """Build one stable list of route candidates."""
    candidates: list[str] = []
    if requested and requested not in excluded:
        candidates.append(requested)
    for candidate in fallback_order:
        if candidate in excluded or candidate in candidates:
            continue
        candidates.append(candidate)
    return candidates


def _get_route_candidate_readiness(
    name: str,
    channels: dict[str, dict[str, Any]],
    runtime_available: list[str],
) -> tuple[str, str]:
    """Return whether one candidate is runtime-ready, config-ready, or blocked."""
    entry = dict(channels.get(name) or {})
    if not entry:
        return "unavailable", "unknown channel"
    if entry.get("proactive_target_mode") == "interactive_session":
        if name in runtime_available:
            return "runtime", str(entry.get("routing_detail") or "runtime route ready")
        return "unavailable", str(entry.get("routing_detail") or "runtime inactive")
    if not entry.get("enabled"):
        return "unavailable", "channel disabled in config"
    if not entry.get("configured"):
        return "unavailable", "channel config incomplete"
    if not entry.get("routing_ready"):
        return "unavailable", str(entry.get("routing_detail") or "route target not configured")
    if name in runtime_available:
        return "runtime", str(entry.get("routing_detail") or "runtime route ready")
    return "configured_only", "channel configured but runtime inactive"


def _resolve_selection_kind(
    selected: str,
    requested: str,
    *,
    runtime_candidate: bool,
) -> str:
    """Classify how one route decision was selected."""
    if not selected:
        return "unresolved"
    if requested and selected == requested:
        return "preferred"
    if requested and selected != requested:
        return "fallback"
    if runtime_candidate:
        return "auto"
    return "configured_only"


def _resolve_route_reason(
    *,
    status: str,
    requested: str,
    selected: str,
    blocked_candidates: list[dict[str, str]],
) -> str:
    """Return one compact route decision reason."""
    if status == "ready" and requested and selected == requested:
        return "preferred channel ready"
    if status == "ready" and requested and selected != requested:
        return f"preferred unavailable; fell back from {requested}"
    if status == "ready":
        return f"first runtime-ready candidate is {selected}"
    if status == "configured_only" and selected:
        return f"{selected} configured but runtime inactive"
    if blocked_candidates:
        first = blocked_candidates[0]
        return f"{first['channel']}: {first['reason']}"
    return "no candidate channels"


def _is_required_fields_set(cfg: Any | None, fields: list[str]) -> bool:
    """Return whether the required channel config fields are populated."""
    if cfg is None:
        return False
    if not fields:
        return True
    return all(_is_field_set(cfg, field_name) for field_name in fields)


def _is_field_set(cfg: Any | None, field_name: str) -> bool:
    """Return whether one config field is populated."""
    if not field_name:
        return False
    value = _cfg_value(cfg, field_name, "")
    if isinstance(value, list):
        return bool(value)
    return bool(str(value or "").strip())


def _cfg_value(cfg: Any | None, field_name: str, default: object) -> object:
    """Return one config field from either a pydantic object or raw extension dict."""
    if cfg is None:
        return default
    if isinstance(cfg, dict):
        if field_name in cfg:
            return cfg[field_name]
        camel_name = _to_camel_case(field_name)
        if camel_name in cfg:
            return cfg[camel_name]
        return default
    return getattr(cfg, field_name, default)


def _to_camel_case(value: str) -> str:
    """Convert one snake_case name into camelCase."""
    head, *tail = value.split("_")
    return head + "".join(item.capitalize() for item in tail)


def _get_runtime_overlay(gateway: Any | None) -> dict[str, dict[str, object]]:
    """Return runtime channel state from one gateway-like object."""
    if gateway is None:
        return {}
    if hasattr(gateway, "get_channel_runtime_snapshot"):
        snapshot = gateway.get_channel_runtime_snapshot()
        if isinstance(snapshot, dict):
            return {
                str(name): dict(state)
                for name, state in snapshot.items()
                if isinstance(state, dict)
            }
    channels = getattr(gateway, "channels", {})
    if isinstance(channels, dict):
        return {
            str(name): {
                "status": "running",
                "detail": "active in gateway runtime",
                "last_error": "",
                "last_transition_at": 0,
            }
            for name in channels
        }
    return {}


def _get_diagnostics_overlay(gateway: Any | None) -> dict[str, dict[str, object]]:
    """Return runtime diagnostics from one gateway-like object."""
    if gateway is None or not hasattr(gateway, "get_channel_diagnostics_snapshot"):
        return {}
    snapshot = gateway.get_channel_diagnostics_snapshot()
    if not isinstance(snapshot, dict):
        return {}
    return {
        str(name): dict(state)
        for name, state in snapshot.items()
        if isinstance(state, dict)
    }


def _get_orchestration_overlay(gateway: Any | None) -> dict[str, dict[str, object]]:
    """Return desired-state orchestration metadata from one gateway-like object."""
    if gateway is None or not hasattr(gateway, "get_channel_orchestration_snapshot"):
        return {}
    snapshot = gateway.get_channel_orchestration_snapshot()
    if not isinstance(snapshot, dict):
        return {}
    return {
        str(name): dict(state)
        for name, state in snapshot.items()
        if isinstance(state, dict)
    }


def _get_runtime_available_channels(gateway: Any | None) -> list[str]:
    """Return the currently available runtime channels."""
    available: list[str] = []
    runtime = _get_runtime_overlay(gateway)
    for name, state in runtime.items():
        if str(state.get("status") or "") == "running" and name not in available:
            available.append(name)
    channels = getattr(gateway, "channels", {})
    if isinstance(channels, dict):
        for name in channels:
            channel_name = str(name)
            if channel_name not in available:
                available.append(channel_name)
    return available


def _status_to_health(status: str) -> str:
    """Map one runtime status into a compact health label."""
    if status in {"running", "configured"}:
        return "healthy"
    if status == "starting":
        return "starting"
    if status in {"failed", "misconfigured"}:
        return "failed"
    if status == "stopped":
        return "stopped"
    return "disabled"


def _resolve_drift_status(desired_state: str, actual_status: str) -> str:
    """Return one compact desired-vs-actual drift status."""
    if desired_state == "running":
        if actual_status == "running":
            return "in_sync"
        if actual_status == "starting":
            return "converging"
        if actual_status in {"disabled", "misconfigured"}:
            return "blocked"
        return "drifted"
    if actual_status in {"", "configured", "disabled", "stopped", "misconfigured"}:
        return "in_sync"
    if actual_status == "starting":
        return "converging"
    return "drifted"


def _default_reconcile_status(drift_status: str) -> str:
    """Return the default reconcile status for one drift label."""
    if drift_status == "in_sync":
        return "reconciled"
    if drift_status == "converging":
        return "reconciling"
    if drift_status == "blocked":
        return "blocked"
    return "drifted"


def _resolve_operator_actions(
    status: str,
    *,
    desired_state: str,
    drift_status: str,
    reconcile_status: str,
) -> list[str]:
    """Return the allowed operator actions for one channel orchestration state."""
    actions: list[str] = []
    if desired_state == "running":
        if status in {"running", "starting"}:
            actions.extend(["restart", "stop"])
        else:
            actions.extend(["recover", "stop"])
    else:
        if status in {"running", "starting"}:
            actions.append("stop")
        if status != "disabled":
            actions.append("start")
    if drift_status in {"drifted", "blocked"} or reconcile_status == "reconciling":
        actions.append("reconcile")
    if drift_status == "drifted" and desired_state == "running" and "recover" not in actions:
        actions.append("recover")
    return list(dict.fromkeys(actions))
