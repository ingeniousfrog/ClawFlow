"""Background task queue tool."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from nanoclaw.core.collaboration import (
    build_persistent_resume_brief,
    build_role_recovery_action,
    build_role_runtime_execution_result,
    get_role_turn_budget,
    workflow_role_graph_fanout_enabled,
)
from nanoclaw.core.logger import get_logger
from nanoclaw.runtime.tasks import get_task_store
from nanoclaw.tools.registry import tool
from nanoclaw.tools.runtime_context import (
    get_tool_runtime_context,
    reset_tool_runtime_context,
    set_tool_runtime_context,
)

logger = get_logger(__name__)

_DEFAULT_BACKGROUND_TASKS = 3
_MAX_BACKGROUND_TASKS = _DEFAULT_BACKGROUND_TASKS
_DEFAULT_STARVATION_THRESHOLD_SECONDS = 300
_STARVATION_THRESHOLD_SECONDS = _DEFAULT_STARVATION_THRESHOLD_SECONDS
_DEFAULT_STALL_THRESHOLD_SECONDS = 120
_TASK_LEASE_TIMEOUT_SECONDS = 45
_TASK_HEARTBEAT_INTERVAL_SECONDS = 10
_active_background_tasks: set[str] = set()
_active_task_handles: dict[str, asyncio.Task[None]] = {}
_task_stop_reasons: dict[str, str] = {}
_runtime_alert_cache: dict[str, dict[str, Any]] = {}
_bg_lock: asyncio.Lock | None = None
_bg_lock_loop: asyncio.AbstractEventLoop | None = None
_drain_task: asyncio.Task[None] | None = None
_heartbeat_task: asyncio.Task[None] | None = None
_worker_id = ""
_runtime_stopping = False
_DEFAULT_ALERT_COOLDOWN_SECONDS = 300
_ALERT_ESCALATE_AFTER = 2
_DEFAULT_SCHEDULE_ALERT_RETRYING_AFTER = 2
_DEFAULT_SCHEDULE_ALERT_ESCALATE_AFTER = 3
_HEARTBEAT_TASK_SOURCE = "heartbeat_checklist"
_CRON_TASK_SOURCE = "cron_job"
_CRON_DELIVERY_TASK_SOURCE = "cron_delivery_retry"
_ROLE_TASK_SOURCE = "workflow_role"
_RUNTIME_TASK_SOURCES = (
    "spawn_task",
    _HEARTBEAT_TASK_SOURCE,
    _CRON_TASK_SOURCE,
    _CRON_DELIVERY_TASK_SOURCE,
    _ROLE_TASK_SOURCE,
)
_CRON_DELIVERY_TASK_PRIORITY = 750
_CRON_DELIVERY_TASK_TIMEOUT_SECONDS = 300
_CRON_DELIVERY_TASK_MAX_ATTEMPTS = 4
_CRON_DELIVERY_TASK_RETRY_BACKOFF_SECONDS = 300
_CRON_RUN_STEP_ID = "cron_run"
_CRON_NOTIFY_STEP_ID = "cron_notify"
_CRON_DELIVERY_NOTIFY_STEP_ID = "cron_delivery_notify"
_HEARTBEAT_RUN_STEP_ID = "heartbeat_run"
_AGENT_RUN_STEP_ID = "agent_run"
_ROLE_RUN_STEP_ID = "role_runtime_ack"
_ROLE_TURN_HISTORY_LIMIT = 3
_NOTIFY_RESULT_STEP_ID = "notify_result"
_NOTIFY_CANCELLED_STEP_ID = "notify_cancelled"
_NOTIFY_FAILURE_STEP_ID = "notify_failure"
_role_runtime_llm_override: Any | None = None


class DeferredBackgroundTask(RuntimeError):
    """Signal that a running task was deferred back to pending."""


class RoleRecoveryRequested(RuntimeError):
    """Signal that a runtime role task requested an explicit recovery role."""

    def __init__(
        self,
        message: str,
        *,
        result: dict[str, Any],
        recovery_task: dict[str, Any],
    ) -> None:
        super().__init__(message)
        self.result = result
        self.recovery_task = recovery_task


def set_role_runtime_llm(llm: Any | None) -> None:
    """Set one optional isolated role-runtime LLM override."""
    global _role_runtime_llm_override
    _role_runtime_llm_override = llm


def _get_role_runtime_llm() -> Any | None:
    """Return one isolated role-runtime LLM client when available."""
    if _role_runtime_llm_override is not None:
        return _role_runtime_llm_override
    try:
        from nanoclaw.core import agent as agent_module
    except Exception:
        return None
    agent = getattr(agent_module, "_agent", None)
    return getattr(agent, "llm", None)


def _parse_role_runtime_json(raw_text: str) -> dict[str, Any]:
    """Parse one compact JSON object from role-runtime LLM output."""
    text = str(raw_text or "").strip()
    if not text:
        return {}
    candidates = [text]
    if text.startswith("```"):
        fenced = text.strip("`").strip()
        if fenced.lower().startswith("json"):
            fenced = fenced[4:].strip()
        if fenced:
            candidates.append(fenced)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _dedupe_role_refs(*groups: list[Any]) -> list[str]:
    """Return one stable deduplicated list of role evidence refs."""
    items: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for item in list(group or []):
            ref = str(item).strip()
            if not ref or ref in seen:
                continue
            seen.add(ref)
            items.append(ref)
    return items


def _merge_role_evidence_snapshots(
    current_snapshot: dict[str, Any],
    resume_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Merge current and resumed evidence snapshots into one compact payload."""
    merged_items: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    seen_urls: set[str] = set()
    for source in (current_snapshot, resume_snapshot):
        for item in list(source.get("items") or []):
            if not isinstance(item, dict):
                continue
            evidence_id = str(item.get("evidence_id") or "").strip()
            url = str(item.get("url") or "").strip()
            identity = evidence_id or url
            if not identity or identity in seen_refs or url in seen_urls:
                continue
            seen_refs.add(identity)
            if url:
                seen_urls.add(url)
            merged_items.append(dict(item))
    return {
        "count": len(merged_items),
        "tools": sorted(
            {
                str(tool).strip()
                for tool in list(current_snapshot.get("tools") or [])
                + list(resume_snapshot.get("tools") or [])
                if str(tool).strip()
            }
        ),
        "items": merged_items[:5],
    }


def _resume_state_applies_to_role_payload(
    role_payload: dict[str, Any],
    resume_state: dict[str, Any],
) -> bool:
    """Return whether one persisted resume checkpoint should seed this role task."""
    checkpoint_id = str(resume_state.get("resume_checkpoint_id") or "").strip()
    if not checkpoint_id:
        return False
    task_key = str(role_payload.get("task_key") or "").strip()
    resume_checkpoint_id = str(role_payload.get("resume_checkpoint_id") or "").strip()
    return task_key == checkpoint_id or resume_checkpoint_id == checkpoint_id


def _apply_role_resume_state(role_payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge one persisted resume state into the role payload when available."""
    payload = dict(role_payload)
    resume_state = dict(payload.get("resume_state") or {})
    if not resume_state:
        return payload, {}
    current_snapshot = dict(payload.get("evidence_snapshot") or {})
    resume_snapshot = dict(resume_state.get("evidence_snapshot") or {})
    payload["evidence_snapshot"] = _merge_role_evidence_snapshots(current_snapshot, resume_snapshot)
    payload["evidence_refs"] = _dedupe_role_refs(
        list(payload.get("evidence_refs") or []),
        list(resume_state.get("evidence_refs") or []),
    )[:5]
    payload["resume_checkpoint_id"] = str(
        payload.get("resume_checkpoint_id")
        or resume_state.get("resume_checkpoint_id")
        or ""
    ).strip()
    resume_brief = str(payload.get("resume_brief") or "").strip()
    if not resume_brief:
        resume_brief = build_persistent_resume_brief(resume_state)
        if resume_brief:
            payload["resume_brief"] = resume_brief
    execution_brief = str(payload.get("execution_brief") or "").strip()
    if resume_brief and resume_brief not in execution_brief:
        payload["execution_brief"] = (
            f"{execution_brief}\n{resume_brief}".strip() if execution_brief else resume_brief
        )
    return payload, resume_state


def _normalize_role_turn_history(items: list[Any]) -> list[dict[str, Any]]:
    """Return one compact chronological turn-history view."""
    history: list[dict[str, Any]] = []
    for item in list(items or []):
        if not isinstance(item, dict):
            continue
        history.append(
            {
                "turn_index": max(1, int(item.get("turn_index") or 1)),
                "turn_reason": str(item.get("turn_reason") or "initial").strip() or "initial",
                "action": str(item.get("action") or "").strip(),
                "evidence_count": int(item.get("evidence_count") or 0),
                "recovery_status": str(item.get("recovery_status") or "").strip(),
                "result_text": str(item.get("result_text") or "")[:160],
                "upstream_input_fingerprint": str(
                    item.get("upstream_input_fingerprint") or ""
                ).strip(),
            }
        )
    return history[-_ROLE_TURN_HISTORY_LIMIT:]


def _build_role_turn_summary(
    role_payload: dict[str, Any],
    role_result: dict[str, Any],
) -> dict[str, Any]:
    """Build one compact summary for a completed role turn."""
    return {
        "turn_index": max(1, int(role_result.get("turn_index") or role_payload.get("turn_index") or 1)),
        "turn_reason": str(
            role_result.get("turn_reason") or role_payload.get("turn_reason") or "initial"
        ).strip()
        or "initial",
        "action": str(role_result.get("action") or "").strip(),
        "evidence_count": int(role_result.get("evidence_count") or 0),
        "recovery_status": str(role_result.get("recovery_status") or "").strip(),
        "result_text": str(role_result.get("result_text") or "")[:160],
        "upstream_input_fingerprint": str(
            role_result.get("upstream_input_fingerprint")
            or role_payload.get("upstream_input_fingerprint")
            or ""
        ).strip(),
    }


def _build_role_upstream_input_fingerprint(
    role_payload: dict[str, Any],
    dependency_outputs: list[dict[str, Any]],
) -> str:
    """Hash one compact upstream input view for role-turn rearm decisions."""
    normalized_dependencies: list[dict[str, Any]] = []
    for item in dependency_outputs:
        if not isinstance(item, dict):
            continue
        normalized_dependencies.append(
            {
                "task_key": str(item.get("task_key") or "").strip(),
                "attempt_number": int(item.get("attempt_number") or 0),
                "turn_index": int(item.get("turn_index") or 0),
                "action": str(item.get("action") or "").strip(),
                "artifact_preview": str(item.get("artifact_preview") or "").strip()[:200],
                "tool_handler_status": str(item.get("tool_handler_status") or "").strip(),
                "tool_handler_output_preview": str(
                    item.get("tool_handler_output_preview") or ""
                ).strip()[:200],
                "evidence_refs": _dedupe_role_refs(list(item.get("evidence_refs") or []))[:5],
            }
        )
    fingerprint_payload = {
        "workflow_identity": str(role_payload.get("workflow_identity") or "").strip(),
        "task_key": str(role_payload.get("task_key") or "").strip(),
        "resume_checkpoint_id": str(role_payload.get("resume_checkpoint_id") or "").strip(),
        "dependencies": normalized_dependencies,
    }
    digest = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return digest[:16]


def _prepare_role_turn_payload(
    role_payload: dict[str, Any],
    dependency_outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Attach normalized turn-state and fingerprint fields to one role payload."""
    payload = dict(role_payload)
    role = str(payload.get("role") or "").strip()
    payload["turn_index"] = max(1, int(payload.get("turn_index") or 1))
    payload["turn_budget"] = max(
        1,
        int(payload.get("turn_budget") or get_role_turn_budget(role)),
    )
    payload["turn_reason"] = str(payload.get("turn_reason") or "initial").strip() or "initial"
    payload["turn_history"] = _normalize_role_turn_history(list(payload.get("turn_history") or []))
    payload["upstream_input_fingerprint"] = _build_role_upstream_input_fingerprint(
        payload,
        dependency_outputs,
    )
    return payload


def _get_role_dependency_keys(payload: dict[str, Any], key: str) -> list[str]:
    """Return one normalized dependency-key list from the role payload."""
    return [
        str(item).strip()
        for item in list(payload.get(key) or [])
        if str(item).strip()
    ]


def _role_dependency_view(
    payload: dict[str, Any],
    dependency_map: dict[str, dict[str, Any]],
) -> tuple[list[str], list[dict[str, Any]]]:
    """Resolve all-of and any-of dependencies for one role payload."""
    required_all = _get_role_dependency_keys(payload, "depends_on")
    required_any = _get_role_dependency_keys(payload, "depends_on_any")
    outputs: list[dict[str, Any]] = []
    missing: list[str] = []
    seen: set[str] = set()

    def _append_dependency(dependency_key: str) -> None:
        if dependency_key in seen:
            return
        item = dependency_map.get(dependency_key)
        if not isinstance(item, dict):
            return
        seen.add(dependency_key)
        outputs.append(item)

    for dependency_key in required_all:
        item = dependency_map.get(dependency_key)
        if not isinstance(item, dict):
            missing.append(dependency_key)
            continue
        _append_dependency(dependency_key)

    if required_any:
        satisfied_any = [dependency_key for dependency_key in required_any if dependency_key in dependency_map]
        if not satisfied_any:
            missing.append("any_of:" + ",".join(required_any))
        else:
            for dependency_key in satisfied_any:
                _append_dependency(dependency_key)

    return missing, outputs


def _build_rearmed_role_payload(
    existing_payload: dict[str, Any],
    next_payload: dict[str, Any],
    prior_output: dict[str, Any],
    *,
    turn_reason: str = "upstream_changed",
) -> dict[str, Any]:
    """Merge new upstream inputs into one existing role payload for the next turn."""
    turn_history = _normalize_role_turn_history(list(existing_payload.get("turn_history") or []))
    if prior_output:
        turn_history.append(_build_role_turn_summary(existing_payload, prior_output))
        turn_history = _normalize_role_turn_history(turn_history)
    rearmed = dict(existing_payload)
    rearmed.pop("deferred_recovery_payload", None)
    rearmed.update(next_payload)
    rearmed["turn_index"] = max(1, int(existing_payload.get("turn_index") or 1)) + 1
    rearmed["turn_budget"] = max(
        1,
        int(existing_payload.get("turn_budget") or next_payload.get("turn_budget") or 1),
    )
    rearmed["turn_reason"] = str(turn_reason or "upstream_changed").strip() or "upstream_changed"
    rearmed["turn_history"] = turn_history
    return rearmed


def _build_refreshed_pending_role_payload(
    existing_payload: dict[str, Any],
    next_payload: dict[str, Any],
    *,
    turn_reason: str = "recovery_refresh",
) -> dict[str, Any]:
    """Refresh one pending role payload in place without advancing the turn counter."""
    refreshed = dict(existing_payload)
    refreshed.pop("deferred_recovery_payload", None)
    refreshed.update(next_payload)
    refreshed["turn_index"] = max(1, int(existing_payload.get("turn_index") or 1))
    refreshed["turn_budget"] = max(
        1,
        int(existing_payload.get("turn_budget") or next_payload.get("turn_budget") or 1),
    )
    refreshed["turn_reason"] = str(turn_reason or "recovery_refresh").strip() or "recovery_refresh"
    refreshed["turn_history"] = _normalize_role_turn_history(
        list(existing_payload.get("turn_history") or [])
    )
    return refreshed


def _build_running_recovery_refresh_payload(
    existing_payload: dict[str, Any],
    recovery_payload: dict[str, Any],
) -> dict[str, Any]:
    """Stage one deferred recovery refresh on a currently running role task."""
    refreshed = dict(existing_payload)
    staged_payload = dict(recovery_payload)
    staged_payload.pop("deferred_recovery_payload", None)
    refreshed["deferred_recovery_payload"] = staged_payload
    return refreshed


async def _maybe_rearm_existing_role_task_for_recovery(
    store: Any,
    *,
    parent_task_id: str,
    target_task_key: str,
    recovery_payload: dict[str, Any],
) -> dict[str, Any]:
    """Reuse one pending or succeeded role task for recovery re-entry when possible."""
    if not parent_task_id or not target_task_key:
        return {}
    try:
        child_tasks = await store.list_child_tasks(parent_task_id, source=_ROLE_TASK_SOURCE)
    except Exception as exc:
        logger.error(
            "Recovery role lookup failed for parent `%s` target `%s`: %s",
            parent_task_id,
            target_task_key,
            exc,
        )
        return {}
    existing = next(
        (
            item
            for item in child_tasks
            if str(item.get("payload", {}).get("task_key") or "").strip() == target_task_key
        ),
        None,
    )
    if not isinstance(existing, dict):
        return {}
    status = str(existing.get("status") or "")
    existing_payload = dict(existing.get("payload") or {})
    if status == "pending":
        refreshed_payload = _build_refreshed_pending_role_payload(
            existing_payload,
            recovery_payload,
            turn_reason="recovery_refresh",
        )
        refreshed_payload = _prepare_role_turn_payload(
            refreshed_payload,
            list(refreshed_payload.get("upstream_dependency_outputs") or []),
        )
        try:
            return await store.refresh_pending_task_payload(
                str(existing["task_id"]),
                payload=refreshed_payload,
            )
        except Exception as exc:
            logger.error(
                "Failed to refresh pending recovery role task `%s` for parent `%s`: %s",
                target_task_key,
                parent_task_id,
                exc,
            )
            return {}
    if status == "running":
        turn_index = max(1, int(existing_payload.get("turn_index") or 1))
        turn_budget = max(
            1,
            int(existing_payload.get("turn_budget") or recovery_payload.get("turn_budget") or 1),
        )
        if turn_index >= turn_budget:
            return existing
        refreshed_payload = _build_running_recovery_refresh_payload(
            existing_payload,
            recovery_payload,
        )
        try:
            return await store.refresh_running_task_payload(
                str(existing["task_id"]),
                payload=refreshed_payload,
            )
        except Exception as exc:
            logger.error(
                "Failed to stage running recovery refresh for role task `%s` under parent `%s`: %s",
                target_task_key,
                parent_task_id,
                exc,
            )
            return {}
    if status != "succeeded":
        return {}
    turn_index = max(1, int(existing_payload.get("turn_index") or 1))
    turn_budget = max(
        1,
        int(existing_payload.get("turn_budget") or recovery_payload.get("turn_budget") or 1),
    )
    if turn_index >= turn_budget:
        return {}
    try:
        prior_step = await store.get_task_step(str(existing["task_id"]), _ROLE_RUN_STEP_ID)
    except Exception as exc:
        logger.error(
            "Recovery role step lookup failed for task `%s` target `%s`: %s",
            existing.get("task_id") or "",
            target_task_key,
            exc,
        )
        return {}
    prior_output = dict(prior_step.get("output") or {}) if prior_step else {}
    rearmed_payload = _build_rearmed_role_payload(
        existing_payload,
        recovery_payload,
        prior_output,
        turn_reason="recovery_reentry",
    )
    rearmed_payload = _prepare_role_turn_payload(
        rearmed_payload,
        list(rearmed_payload.get("upstream_dependency_outputs") or []),
    )
    try:
        return await store.rearm_task(
            str(existing["task_id"]),
            payload=rearmed_payload,
        )
    except Exception as exc:
        logger.error(
            "Failed to rearm recovery role task `%s` for parent `%s`: %s",
            target_task_key,
            parent_task_id,
            exc,
        )
        return {}


async def _maybe_rearm_task_after_running_recovery_refresh(
    store: Any,
    task: dict[str, Any],
) -> dict[str, Any]:
    """Rearm one just-finished role task when a deferred running recovery refresh was staged."""
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        return {}
    payload = dict(task.get("payload") or {})
    deferred_payload = dict(payload.pop("deferred_recovery_payload", {}) or {})
    if not deferred_payload:
        return {}
    turn_index = max(1, int(payload.get("turn_index") or 1))
    turn_budget = max(
        1,
        int(payload.get("turn_budget") or deferred_payload.get("turn_budget") or 1),
    )
    if turn_index >= turn_budget:
        return {}
    try:
        prior_step = await store.get_task_step(task_id, _ROLE_RUN_STEP_ID)
    except Exception as exc:
        logger.error(
            "Deferred running recovery step lookup failed for task `%s`: %s",
            task_id,
            exc,
        )
        return {}
    prior_output = dict(prior_step.get("output") or {}) if prior_step else {}
    rearmed_payload = _build_rearmed_role_payload(
        payload,
        deferred_payload,
        prior_output,
        turn_reason="recovery_reentry",
    )
    rearmed_payload = _prepare_role_turn_payload(
        rearmed_payload,
        list(rearmed_payload.get("upstream_dependency_outputs") or []),
    )
    try:
        return await store.rearm_task(
            task_id,
            payload=rearmed_payload,
        )
    except Exception as exc:
        logger.error(
            "Failed to rearm deferred running recovery task `%s`: %s",
            task_id,
            exc,
        )
        return {}


def _build_runtime_role_recovery_action(
    payload: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Build one runtime-side role recovery action when the current role degrades."""
    workflow_name = str(payload.get("workflow_name") or "").strip()
    tool_names = [str(item).strip() for item in list(payload.get("tool_names") or []) if str(item).strip()]
    needs_grounded = bool(payload.get("needs_grounded"))
    evidence_snapshot = dict(payload.get("evidence_snapshot") or {})
    role = str(payload.get("role") or "").strip()
    failure_reason = ""
    stage = str(payload.get("stage") or "").strip()
    if str(result.get("tool_handler_status") or "").strip() == "error":
        handler_name = str(result.get("tool_handler_name") or role or "role_handler").strip()
        failure_reason = f"{handler_name}:error"
        stage = "post_tools"
    elif role == "critic" and needs_grounded and int(result.get("evidence_count") or 0) == 0:
        stage = "post_tools"
    elif role == "summarizer" and needs_grounded and int(result.get("evidence_count") or 0) == 0:
        stage = "pre_final"
    else:
        return {}
    action = build_role_recovery_action(
        workflow_name=workflow_name,
        tool_names=tool_names,
        needs_grounded=needs_grounded,
        evidence_snapshot=evidence_snapshot,
        failure_reason=failure_reason,
        stage=stage,
    )
    return action.model_dump() if action is not None else {}


def _build_role_runtime_llm_messages(
    payload: dict[str, Any],
    dependency_outputs: list[dict[str, Any]],
    baseline_result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build one isolated single-turn prompt for a role-runtime LLM call."""
    role = str(payload.get("role") or "").strip()
    role_label = str(payload.get("role_label") or role).strip()
    stage = str(payload.get("stage") or "").strip()
    evidence_snapshot = dict(payload.get("evidence_snapshot") or {})
    evidence_items: list[dict[str, str]] = []
    for item in list(evidence_snapshot.get("items") or [])[:3]:
        if not isinstance(item, dict):
            continue
        evidence_items.append(
            {
                "evidence_id": str(item.get("evidence_id") or "").strip(),
                "title": str(item.get("title") or "").strip()[:120],
                "url": str(item.get("url") or "").strip()[:200],
                "snippet": str(item.get("snippet") or "").strip()[:160],
            }
        )
    dependency_view: list[dict[str, str]] = []
    for item in dependency_outputs[:3]:
        if not isinstance(item, dict):
            continue
        dependency_view.append(
            {
                "task_key": str(item.get("task_key") or "").strip(),
                "role": str(item.get("role_label") or item.get("role") or "").strip(),
                "action": str(item.get("action") or "").strip()[:80],
                "artifact_preview": (
                    str(
                        item.get("tool_handler_output_preview")
                        or item.get("artifact_preview")
                        or ""
                    )
                    .strip()[:160]
                ),
            }
        )
    return [
        {
            "role": "system",
            "content": (
                "You execute one isolated workflow role turn for nanoClaw. "
                "Return exactly one JSON object and nothing else. "
                "Do not call tools. Keep the output grounded in the provided context. "
                'JSON keys: "role_summary", "artifact_preview", "result_text".'
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "workflow_name": str(payload.get("workflow_name") or "").strip(),
                    "role": role,
                    "role_label": role_label,
                    "stage": stage,
                    "user_summary": str(payload.get("user_summary") or "").strip()[:160],
                    "role_focus": str(payload.get("role_focus") or "").strip()[:160],
                    "tool_names": list(payload.get("tool_names") or [])[:5],
                    "needs_grounded": bool(payload.get("needs_grounded")),
                    "execution_brief": str(payload.get("execution_brief") or "").strip()[:400],
                    "resume_brief": str(payload.get("resume_brief") or "").strip()[:400],
                    "handoff_contract": dict(payload.get("handoff_contract") or {}),
                    "checkpoint_id": str(baseline_result.get("checkpoint_id") or "").strip(),
                    "resume_checkpoint_id": str(payload.get("resume_checkpoint_id") or "").strip(),
                    "resume_source_workflow_run_id": int(
                        dict(payload.get("resume_state") or {}).get("source_workflow_run_id") or 0
                    ),
                    "deterministic_action": str(baseline_result.get("action") or "").strip(),
                    "dependency_outputs": dependency_view,
                    "shared_evidence": evidence_items,
                    "current_artifact_preview": (
                        str(baseline_result.get("artifact_preview") or "").strip()[:240]
                    ),
                    "tool_handler_output_preview": (
                        str(baseline_result.get("tool_handler_output_preview") or "").strip()[:200]
                    ),
                    "instruction": (
                        "Write a concise role-specific update that preserves the current action "
                        "boundary and makes the handoff clearer."
                    ),
                },
                ensure_ascii=True,
            ),
        },
    ]


async def _run_role_llm_turn(
    payload: dict[str, Any],
    dependency_outputs: list[dict[str, Any]],
    baseline_result: dict[str, Any],
) -> dict[str, Any]:
    """Execute one isolated LLM turn for a runtime role task when available."""
    llm = _get_role_runtime_llm()
    if llm is None:
        return {}
    messages = _build_role_runtime_llm_messages(payload, dependency_outputs, baseline_result)
    task_key = str(payload.get("task_key") or "").strip()
    workflow_name = str(payload.get("workflow_name") or "").strip()
    try:
        response = await llm.chat(messages)
    except Exception as exc:
        logger.error(
            "Role runtime LLM turn failed for `%s` in `%s`: %s",
            task_key,
            workflow_name,
            exc,
        )
        return {
            "llm_status": "error",
            "llm_error": str(exc)[:200],
        }
    raw_output = str(getattr(response, "content", "") or "").strip()
    parsed = _parse_role_runtime_json(raw_output)
    usage = getattr(response, "usage", None)
    output = {
        "llm_status": "success" if parsed else "invalid_json",
        "llm_model": str(getattr(llm, "model", "") or "").strip(),
        "llm_tokens": int(getattr(usage, "total_tokens", 0) or 0),
        "llm_output_preview": raw_output[:240],
    }
    if not parsed:
        return output
    role_summary = str(parsed.get("role_summary") or "").strip()
    artifact_preview = str(parsed.get("artifact_preview") or "").strip()
    result_text = str(parsed.get("result_text") or "").strip()
    if role_summary:
        output["role_summary"] = role_summary[:240]
    if artifact_preview:
        output["artifact_preview"] = artifact_preview[:240]
    if result_text:
        output["result_text"] = result_text[:240]
    output["handler_kind"] = "role_llm_turn"
    return output


def _clock_text_to_minutes(raw_value: str) -> int:
    """Convert one HH:MM string into minutes since midnight."""
    hour_text, minute_text = raw_value.split(":", 1)
    return (int(hour_text) * 60) + int(minute_text)


def _is_in_quiet_window(
    quiet_start: str,
    quiet_end: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Return whether the current local time falls inside the quiet window."""
    if not quiet_start or not quiet_end:
        return False
    current = now or datetime.now()
    current_minutes = (current.hour * 60) + current.minute
    start_minutes = _clock_text_to_minutes(quiet_start)
    end_minutes = _clock_text_to_minutes(quiet_end)
    if start_minutes == end_minutes:
        return False
    if start_minutes < end_minutes:
        return start_minutes <= current_minutes < end_minutes
    return current_minutes >= start_minutes or current_minutes < end_minutes


def _next_quiet_window_exit(
    quiet_start: str,
    quiet_end: str,
    *,
    now: datetime | None = None,
) -> datetime:
    """Return the next local timestamp when the quiet window ends."""
    current = now or datetime.now()
    if not quiet_start or not quiet_end:
        return current
    start_minutes = _clock_text_to_minutes(quiet_start)
    end_minutes = _clock_text_to_minutes(quiet_end)
    exit_at = current.replace(
        hour=end_minutes // 60,
        minute=end_minutes % 60,
        second=0,
        microsecond=0,
    )
    current_minutes = (current.hour * 60) + current.minute
    if start_minutes < end_minutes:
        if current_minutes >= end_minutes:
            exit_at += timedelta(days=1)
        return exit_at
    if current_minutes >= start_minutes:
        return exit_at + timedelta(days=1)
    return exit_at


def _get_background_capacity() -> int:
    """Return configured background worker capacity with safe fallback."""
    if _MAX_BACKGROUND_TASKS != _DEFAULT_BACKGROUND_TASKS:
        configured = _MAX_BACKGROUND_TASKS
        return max(1, min(configured, 32))
    try:
        from nanoclaw.core.config import get_config

        config = get_config()
        configured = int(config.tools.background_tasks.max_concurrency)
    except (AttributeError, FileNotFoundError, TypeError, ValueError):
        configured = _MAX_BACKGROUND_TASKS
    return max(1, min(configured, 32))


def _get_starvation_threshold_seconds() -> int:
    """Return the starvation threshold used by background task claiming."""
    if _STARVATION_THRESHOLD_SECONDS != _DEFAULT_STARVATION_THRESHOLD_SECONDS:
        configured = _STARVATION_THRESHOLD_SECONDS
        return max(0, min(configured, 86400))
    try:
        from nanoclaw.core.config import get_config

        config = get_config()
        configured = int(config.tools.background_tasks.starvation_threshold_seconds)
    except (AttributeError, FileNotFoundError, TypeError, ValueError):
        configured = _STARVATION_THRESHOLD_SECONDS
    return max(0, min(configured, 86400))


def _get_runtime_stall_threshold_seconds() -> int:
    """Return the threshold used for ready-queue stall detection."""
    try:
        from nanoclaw.core.config import get_config

        config = get_config()
        configured = int(config.tools.background_tasks.stall_threshold_seconds)
    except (AttributeError, FileNotFoundError, TypeError, ValueError):
        configured = _DEFAULT_STALL_THRESHOLD_SECONDS
    return max(0, min(configured, 86400))


def _get_runtime_alert_channel() -> str:
    """Return the configured proactive alert channel, or empty for auto."""
    try:
        from nanoclaw.core.config import get_config

        config = get_config()
        return str(config.tools.background_tasks.alert_channel or "").strip()
    except (AttributeError, FileNotFoundError, TypeError, ValueError):
        return ""


def _get_runtime_alert_escalation_channel() -> str:
    """Return the configured escalation alert channel, or empty for auto-secondary."""
    try:
        from nanoclaw.core.config import get_config

        config = get_config()
        return str(config.tools.background_tasks.alert_escalation_channel or "").strip()
    except (AttributeError, FileNotFoundError, TypeError, ValueError):
        return ""


def _get_runtime_alert_cooldown_seconds() -> int:
    """Return the cooldown applied to repeated runtime alerts."""
    try:
        from nanoclaw.core.config import get_config

        config = get_config()
        configured = int(config.tools.background_tasks.alert_cooldown_seconds)
    except (AttributeError, FileNotFoundError, TypeError, ValueError):
        configured = _DEFAULT_ALERT_COOLDOWN_SECONDS
    return max(0, min(configured, 86400))


def _get_schedule_alert_escalate_after() -> int:
    """Return how many repeated identical schedule alerts trigger escalation."""
    try:
        from nanoclaw.core.config import get_config

        config = get_config()
        configured = int(config.tools.background_tasks.schedule_alert_escalate_after)
    except (AttributeError, FileNotFoundError, TypeError, ValueError):
        configured = _DEFAULT_SCHEDULE_ALERT_ESCALATE_AFTER
    return max(2, min(configured, 10))


def _get_schedule_alert_retrying_after() -> int:
    """Return after how many repeated retrying states a schedule alert should fire."""
    try:
        from nanoclaw.core.config import get_config

        config = get_config()
        configured = int(config.tools.background_tasks.schedule_alert_retrying_after)
    except (AttributeError, FileNotFoundError, TypeError, ValueError):
        configured = _DEFAULT_SCHEDULE_ALERT_RETRYING_AFTER
    return max(1, min(configured, 10))


def _get_bg_lock() -> asyncio.Lock:
    """Get the background task lock lazily."""
    global _bg_lock, _bg_lock_loop
    loop = asyncio.get_running_loop()
    if _bg_lock is None or _bg_lock_loop is not loop:
        _bg_lock = asyncio.Lock()
        _bg_lock_loop = loop
    return _bg_lock


def _resolve_channel(task: dict[str, Any]) -> str:
    """Pick the best proactive channel from the parent session."""
    session_id = str(task.get("session_id") or "")
    channel = session_id.split(":", 1)[0] if ":" in session_id else session_id
    return channel or "telegram"


def _ensure_worker_id() -> str:
    """Return the current runtime worker id."""
    global _worker_id
    if not _worker_id:
        _worker_id = f"spawn-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    return _worker_id


def _ensure_heartbeat_loop() -> None:
    """Start the lease heartbeat loop when needed."""
    global _heartbeat_task
    loop = asyncio.get_running_loop()
    if _heartbeat_task is not None:
        if _heartbeat_task.done() or _heartbeat_task.get_loop() is not loop:
            _heartbeat_task = None
        else:
            return
    _heartbeat_task = loop.create_task(_lease_heartbeat_loop())


def _seconds_since(timestamp: str) -> float:
    """Return elapsed seconds since a stored SQLite UTC timestamp."""
    dt = datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return (datetime.now(UTC) - dt).total_seconds()


def _cancel_active_task(task_id: str, reason: str) -> None:
    """Cancel one active task handle with a persisted stop reason."""
    handle = _active_task_handles.get(task_id)
    if handle is None or handle.done():
        return
    _task_stop_reasons[task_id] = reason
    handle.cancel()


async def _log_task_run_trace(
    task: dict[str, Any],
    *,
    status: str,
    execution_ms: int,
    failure_reason: str = "",
    final_output_summary: str = "",
) -> None:
    """Write one structured task-attempt trace without breaking the runtime."""
    try:
        from nanoclaw.security.audit import get_audit_log

        await get_audit_log().log_task_run(
            task_id=str(task["task_id"]),
            session_id=f"task:{task['task_id']}",
            attempt_number=int(task.get("attempt_count") or 0),
            worker_id=_ensure_worker_id(),
            status=status,
            failure_reason=failure_reason,
            final_output_summary=final_output_summary,
            execution_ms=execution_ms,
        )
    except Exception as exc:
        logger.error("Task run trace logging failed for `%s`: %s", task["task_id"], exc)


async def _log_runtime_watchdog(
    task_id: str,
    *,
    event: str,
    input_summary: str,
    output_summary: str,
    status: str = "warning",
    tool_name: str = "spawn_task",
) -> None:
    """Persist one watchdog event to the audit log without breaking the runtime."""
    try:
        from nanoclaw.security.audit import get_audit_log

        await get_audit_log().log(
            action_type="runtime_watchdog",
            tool_name=tool_name,
            input_summary=f"task_id={task_id} event={event} {input_summary}".strip(),
            output_summary=output_summary,
            status=status,
            session_id=f"task:{task_id}",
        )
    except Exception as exc:
        logger.error("Runtime watchdog logging failed for `%s`: %s", task_id, exc)


async def _log_recovered_orphan_events(recovered: list[dict[str, Any]]) -> None:
    """Write one watchdog audit entry per recovered stale background task."""
    for task in recovered:
        task_id = str(task.get("task_id") or "")
        if not task_id:
            continue
        await _log_runtime_watchdog(
            task_id,
            event="orphan_recovered",
            input_summary=(
                f"previous_worker={task.get('recovered_from_worker') or '-'} "
                f"stale_age_seconds={int(task.get('stale_age_seconds') or 0)}"
            ),
            output_summary="Recovered stale running task and moved it back to pending.",
            tool_name=str(task.get("source") or "spawn_task"),
        )


def summarize_runtime_health(queue_metrics: dict[str, Any]) -> dict[str, Any]:
    """Summarize background-runtime health from queue metrics."""
    reasons: list[str] = []
    critical_reasons: list[str] = []
    stale_running = int(queue_metrics.get("stale_running_tasks") or 0)
    dead_letter = int(queue_metrics.get("dead_letter_tasks") or 0)
    cancel_requested = int(queue_metrics.get("cancel_requested_running") or 0)
    ready_backlog = int(queue_metrics.get("ready_backlog") or 0)
    running_tasks = int(queue_metrics.get("running_tasks") or 0)
    oldest_ready_age = int(queue_metrics.get("oldest_ready_age_seconds") or 0)
    stall_threshold = int(queue_metrics.get("stall_threshold_seconds") or 0)
    if (
        stall_threshold > 0
        and ready_backlog > 0
        and running_tasks == 0
        and oldest_ready_age >= stall_threshold
    ):
        critical_reasons.append(f"queue_stall={ready_backlog}")
    if stale_running > 0:
        reasons.append(f"stale_running={stale_running}")
    if dead_letter > 0:
        reasons.append(f"dead_letter={dead_letter}")
    if cancel_requested > 0:
        reasons.append(f"cancel_requested={cancel_requested}")
    reasons = critical_reasons + reasons
    if critical_reasons:
        status = "critical"
    elif reasons:
        status = "degraded"
    else:
        status = "healthy"
    return {
        "status": status,
        "reasons": reasons,
        "summary": ", ".join(reasons) if reasons else "healthy",
        "fingerprint": "|".join(reasons),
        "queue_stalled": bool(critical_reasons),
        "base_alert_severity": (
            "error" if status == "critical" else "warning" if status == "degraded" else "none"
        ),
    }


def _resolve_runtime_alert_channel(gateway: Any) -> str:
    """Pick the best proactive channel for runtime health alerts."""
    if hasattr(gateway, "config"):
        try:
            from nanoclaw.channels.contract import resolve_channel_route

            route = resolve_channel_route(
                gateway.config,
                gateway,
                purpose="runtime_alert",
                preferred_channel=_get_runtime_alert_channel(),
            )
            if route["status"] == "ready":
                return str(route["selected_channel"] or "")
        except Exception:
            pass
    preferred = _get_runtime_alert_channel()
    if preferred:
        return preferred if preferred in getattr(gateway, "channels", {}) else ""
    for name in ("telegram", "feishu", "console"):
        if name in getattr(gateway, "channels", {}):
            return name
    channels = list(getattr(gateway, "channels", {}).keys())
    return channels[0] if channels else ""


def _resolve_runtime_alert_targets(gateway: Any, plan: dict[str, Any]) -> list[str]:
    """Resolve the proactive targets for one runtime alert."""
    if hasattr(gateway, "config"):
        try:
            from nanoclaw.channels.contract import resolve_channel_route

            primary_route = resolve_channel_route(
                gateway.config,
                gateway,
                purpose="runtime_alert",
                preferred_channel=_get_runtime_alert_channel(),
            )
            targets: list[str] = []
            primary = str(primary_route.get("selected_channel") or "")
            if primary_route["status"] == "ready" and primary:
                targets.append(primary)
            if plan["escalated"]:
                escalation_route = resolve_channel_route(
                    gateway.config,
                    gateway,
                    purpose="runtime_alert_escalation",
                    preferred_channel=_get_runtime_alert_escalation_channel(),
                    exclude_channels={primary},
                )
                escalation = str(escalation_route.get("selected_channel") or "")
                if escalation_route["status"] == "ready" and escalation and escalation not in targets:
                    targets.append(escalation)
            return targets
        except Exception:
            pass

    channels = getattr(gateway, "channels", {})
    primary = _resolve_runtime_alert_channel(gateway)
    escalation = _get_runtime_alert_escalation_channel()
    targets: list[str] = []
    if primary:
        targets.append(primary)
    if plan["escalated"]:
        if escalation and escalation in channels and escalation not in targets:
            targets.append(escalation)
        elif not escalation:
            for name in ("telegram", "feishu", "console"):
                if name in channels and name not in targets:
                    targets.append(name)
                    break
    if not targets and escalation and escalation in channels:
        targets.append(escalation)
    return targets


async def _send_schedule_alert_message(
    gateway: Any,
    *,
    channel: str,
    text: str,
    target_id: str = "",
) -> None:
    """Send one schedule-level alert, using targeted Feishu delivery when possible."""
    if (
        channel == "feishu"
        and target_id
        and hasattr(gateway, "send_proactive_targeted")
    ):
        sent = await gateway.send_proactive_targeted(
            channel="feishu",
            text=text,
            target_id=target_id,
        )
        if not sent:
            raise RuntimeError("Feishu proactive send failed for the target chat.")
        return
    channels = getattr(gateway, "channels", {})
    if (
        channel == "feishu"
        and target_id
        and channels.get("feishu") is not None
        and hasattr(channels["feishu"], "send_proactive_to")
    ):
        sent = await channels["feishu"].send_proactive_to(target_id, text)
        if not sent:
            raise RuntimeError("Feishu proactive send failed for the target chat.")
        return
    await gateway.send_proactive(text, channel=channel)


async def _send_schedule_recovery(
    gateway: Any,
    *,
    job: dict[str, Any],
    runtime: dict[str, Any],
    cached: dict[str, Any],
) -> None:
    """Send one schedule recovery notice after a previously alerted issue clears."""
    from nanoclaw.security.audit import get_audit_log

    channel = str(job.get("channel") or "").strip()
    if not channel:
        return
    target_id = str(job.get("target_id") or "")
    targets: list[tuple[str, str]] = [(channel, target_id)]
    escalation_channel = _get_runtime_alert_escalation_channel()
    channels = getattr(gateway, "channels", {})
    previous_stage = str(cached.get("stage") or "")
    if (
        previous_stage.endswith("_escalated")
        and escalation_channel
        and escalation_channel in channels
        and escalation_channel != channel
    ):
        targets.append((escalation_channel, ""))

    job_id = int(job.get("id") or 0)
    message = (
        "[schedule recovered]\n"
        f"job_id={job_id}\n"
        f"name={job.get('name') or ''}\n"
        f"health={runtime.get('health') or 'healthy'}\n"
        f"reason={runtime.get('health_reason') or 'latest execution succeeded'}\n"
        f"previous_stage={previous_stage or '-'}\n"
        f"previous_repeat_count={int(cached.get('repeat_count') or 0)}\n"
        f"notify_mode={runtime.get('notify_kind') or 'unknown'}\n"
        f"last_execution={dict(runtime.get('last_execution') or {}).get('status') or 'never'}\n"
        f"last_delivery_retry={dict(runtime.get('last_delivery_retry') or {}).get('status') or 'none'}"
    )
    for target_channel, target_chat in targets:
        await _send_schedule_alert_message(
            gateway,
            channel=target_channel,
            text=message,
            target_id=target_chat,
        )
    target_summary = ",".join(
        f"{channel}:{chat_id or '-'}" for channel, chat_id in targets
    )
    await get_audit_log().log(
        action_type="schedule_recovery",
        tool_name="cron_job",
        input_summary=(
            f"job_id={job_id} targets="
            f"{target_summary} "
            f"previous_stage={previous_stage or '-'} "
            f"previous_repeat_count={int(cached.get('repeat_count') or 0)} "
            f"health={runtime.get('health') or 'healthy'}"
        ),
        output_summary="Sent proactive schedule recovery notice.",
        status="success",
        session_id=f"schedule:{job_id}",
    )


def _should_emit_runtime_alert(key: str, fingerprint: str, cooldown_seconds: int) -> bool:
    """Return True when one runtime alert should be sent."""
    cached = _runtime_alert_cache.get(key)
    now = asyncio.get_running_loop().time()
    if cached is None:
        return True
    if str(cached.get("fingerprint") or "") != fingerprint:
        return True
    last_sent_at = float(cached.get("last_sent_at") or 0.0)
    return now - last_sent_at >= max(0, cooldown_seconds)


def _plan_runtime_alert(
    key: str,
    health: dict[str, Any],
) -> dict[str, Any]:
    """Build one runtime alert plan with repeat-aware severity escalation."""
    fingerprint = str(health["fingerprint"])
    cached = _runtime_alert_cache.get(key)
    if cached is not None and str(cached.get("fingerprint") or "") == fingerprint:
        repeat_count = int(cached.get("repeat_count") or 1) + 1
    else:
        repeat_count = 1

    status = str(health["status"])
    if status == "critical":
        if repeat_count >= _ALERT_ESCALATE_AFTER:
            severity = "critical"
            stage = "critical_escalated"
        else:
            severity = "error"
            stage = "critical_initial"
    elif status == "degraded":
        if repeat_count >= _ALERT_ESCALATE_AFTER:
            severity = "error"
            stage = "degraded_escalated"
        else:
            severity = "warning"
            stage = "degraded_initial"
    else:
        severity = "none"
        stage = "healthy"

    return {
        "fingerprint": fingerprint,
        "repeat_count": repeat_count,
        "severity": severity,
        "stage": stage,
        "escalated": repeat_count >= _ALERT_ESCALATE_AFTER,
    }


def _plan_schedule_alert(
    key: str,
    runtime: dict[str, Any],
) -> dict[str, Any]:
    """Build one schedule-health alert plan with repeat-aware escalation."""
    retrying_after = _get_schedule_alert_retrying_after()
    escalate_after = _get_schedule_alert_escalate_after()
    fingerprint = (
        f"{runtime.get('health') or ''}|{runtime.get('health_reason') or ''}|"
        f"{runtime.get('notify_kind') or ''}|"
        f"{dict(runtime.get('last_execution') or {}).get('status') or ''}|"
        f"{dict(runtime.get('last_delivery_retry') or {}).get('status') or ''}"
    )
    cached = _runtime_alert_cache.get(key)
    if cached is not None and str(cached.get("fingerprint") or "") == fingerprint:
        repeat_count = int(cached.get("repeat_count") or 1) + 1
    else:
        repeat_count = 1

    health = str(runtime.get("health") or "idle")
    if health == "attention":
        should_send = True
        if repeat_count >= escalate_after:
            severity = "critical"
            stage = "attention_escalated"
        else:
            severity = "error"
            stage = "attention_initial"
    elif health == "retrying":
        if repeat_count < retrying_after:
            severity = "warning"
            stage = "retrying_suppressed"
            should_send = False
        else:
            should_send = True
            if repeat_count >= escalate_after:
                severity = "error"
                stage = "retrying_escalated"
            else:
                severity = "warning"
                stage = "retrying_initial"
    else:
        severity = "none"
        stage = f"{health}_inactive"
        should_send = False

    return {
        "fingerprint": fingerprint,
        "repeat_count": repeat_count,
        "severity": severity,
        "stage": stage,
        "escalated": should_send and repeat_count >= escalate_after,
        "should_send": should_send,
        "retrying_after": retrying_after,
        "escalate_after": escalate_after,
    }


def _should_emit_schedule_alert(
    key: str,
    plan: dict[str, Any],
    cooldown_seconds: int,
) -> bool:
    """Return whether one schedule alert should be emitted right now."""
    if not bool(plan.get("should_send")):
        return False
    cached = _runtime_alert_cache.get(key)
    now = asyncio.get_running_loop().time()
    if cached is None:
        return True
    if str(cached.get("fingerprint") or "") != str(plan["fingerprint"]):
        return True
    if bool(cached.get("suppressed")):
        return True
    last_sent_at = float(cached.get("last_sent_at") or 0.0)
    return now - last_sent_at >= max(0, cooldown_seconds)


def _remember_runtime_alert(
    key: str,
    plan: dict[str, Any],
    *,
    sent: bool = True,
) -> None:
    """Persist one in-memory runtime alert state for cooldown dedupe and escalation."""
    _runtime_alert_cache[key] = {
        "fingerprint": str(plan["fingerprint"]),
        "last_sent_at": asyncio.get_running_loop().time() if sent else 0.0,
        "repeat_count": int(plan["repeat_count"]),
        "severity": str(plan["severity"]),
        "stage": str(plan["stage"]),
        "suppressed": not sent,
    }


async def _maybe_send_runtime_alert(queue_metrics: dict[str, Any]) -> None:
    """Send one proactive runtime alert when queue health is degraded."""
    health = summarize_runtime_health(queue_metrics)
    if health["status"] == "healthy":
        _runtime_alert_cache.pop("runtime_health", None)
        return
    try:
        from nanoclaw.channels.gateway import get_gateway
        from nanoclaw.security.audit import get_audit_log

        gateway = get_gateway()
        if gateway is None:
            return
        cooldown_seconds = _get_runtime_alert_cooldown_seconds()
        if not _should_emit_runtime_alert(
            "runtime_health",
            str(health["fingerprint"]),
            cooldown_seconds,
        ):
            return
        plan = _plan_runtime_alert("runtime_health", health)
        targets = _resolve_runtime_alert_targets(gateway, plan)
        if not targets:
            return
        message = (
            "[runtime alert]\n"
            f"worker={_ensure_worker_id()}\n"
            f"health={health['status']}\n"
            f"severity={plan['severity']}\n"
            f"stage={plan['stage']}\n"
            f"repeat_count={plan['repeat_count']}\n"
            f"reasons={health['summary']}\n"
            f"running={int(queue_metrics.get('running_tasks') or 0)} "
            f"ready={int(queue_metrics.get('ready_backlog') or 0)} "
            f"retry={int(queue_metrics.get('retry_backlog') or 0)} "
            f"dead_letter={int(queue_metrics.get('dead_letter_tasks') or 0)} "
            f"stale={int(queue_metrics.get('stale_running_tasks') or 0)}"
        )
        for target in targets:
            await gateway.send_proactive(message, channel=target)
        _remember_runtime_alert("runtime_health", plan)
        await get_audit_log().log(
            action_type="runtime_alert",
            tool_name="spawn_task",
            input_summary=(
                f"targets={','.join(targets)} cooldown={cooldown_seconds}s "
                f"severity={plan['severity']} stage={plan['stage']} "
                f"repeat_count={plan['repeat_count']} reasons={health['summary']}"
            ),
            output_summary="Sent proactive runtime health alert.",
            status="error" if plan["severity"] in {"error", "critical"} else "warning",
            session_id=f"runtime:{_ensure_worker_id()}",
        )
        if plan["escalated"]:
            await get_audit_log().log(
                action_type="runtime_alert_escalation",
                tool_name="spawn_task",
                input_summary=(
                    f"targets={','.join(targets)} "
                    f"severity={plan['severity']} stage={plan['stage']} "
                    f"repeat_count={plan['repeat_count']} reasons={health['summary']}"
                ),
                output_summary="Escalated runtime alert after repeated identical health issue.",
                status="error",
                session_id=f"runtime:{_ensure_worker_id()}",
            )
    except Exception as exc:
        logger.error("Runtime alert delivery failed: %s", exc)


async def _maybe_send_schedule_health_alerts() -> None:
    """Send per-schedule alerts when one persisted schedule is retrying or unhealthy."""
    try:
        from nanoclaw.channels.gateway import get_gateway
        from nanoclaw.security.audit import get_audit_log

        gateway = get_gateway()
        if gateway is None or getattr(gateway, "scheduler", None) is None:
            return
        scheduler = gateway.scheduler
        if not hasattr(scheduler, "list_jobs_with_runtime_state"):
            return
        jobs = await scheduler.list_jobs_with_runtime_state()
        cooldown_seconds = _get_runtime_alert_cooldown_seconds()
        active_keys: set[str] = set()
        for job in jobs:
            job_id = int(job.get("id") or 0)
            cache_key = f"schedule_health:{job_id}"
            runtime = dict(job.get("runtime") or {})
            health = str(runtime.get("health") or "idle")
            cached = _runtime_alert_cache.get(cache_key)
            if (
                not bool(job.get("enabled"))
                or health not in {"retrying", "attention"}
            ):
                if (
                    bool(job.get("enabled"))
                    and health == "healthy"
                    and cached is not None
                    and not bool(cached.get("suppressed"))
                ):
                    await _send_schedule_recovery(
                        gateway,
                        job=job,
                        runtime=runtime,
                        cached=cached,
                    )
                _runtime_alert_cache.pop(cache_key, None)
                continue

            channel = str(job.get("channel") or "").strip()
            if not channel:
                continue
            target_id = str(job.get("target_id") or "")
            active_keys.add(cache_key)
            fingerprint = (
                f"{health}|{runtime.get('health_reason') or ''}|"
                f"{runtime.get('notify_kind') or ''}|"
                f"{runtime.get('last_execution', {}).get('status') if isinstance(runtime.get('last_execution'), dict) else ''}|"
                f"{runtime.get('last_delivery_retry', {}).get('status') if isinstance(runtime.get('last_delivery_retry'), dict) else ''}"
            )
            plan = _plan_schedule_alert(cache_key, runtime)
            if str(plan["fingerprint"]) != fingerprint:
                continue
            if not _should_emit_schedule_alert(cache_key, plan, cooldown_seconds):
                _remember_runtime_alert(cache_key, plan, sent=False)
                continue
            targets: list[tuple[str, str]] = [(channel, target_id)]
            escalation_channel = _get_runtime_alert_escalation_channel()
            channels = getattr(gateway, "channels", {})
            if (
                plan["escalated"]
                and escalation_channel
                and escalation_channel in channels
                and escalation_channel != channel
            ):
                targets.append((escalation_channel, ""))
            message = (
                "[schedule alert]\n"
                f"job_id={job_id}\n"
                f"name={job.get('name') or ''}\n"
                f"health={health}\n"
                f"reason={runtime.get('health_reason') or 'unknown'}\n"
                f"severity={plan['severity']}\n"
                f"stage={plan['stage']}\n"
                f"repeat_count={plan['repeat_count']}\n"
                f"retrying_after={plan['retrying_after']}\n"
                f"escalate_after={plan['escalate_after']}\n"
                f"notify_mode={runtime.get('notify_kind') or 'unknown'}\n"
                f"last_execution={dict(runtime.get('last_execution') or {}).get('status') or 'never'}\n"
                f"last_delivery_retry={dict(runtime.get('last_delivery_retry') or {}).get('status') or 'none'}"
            )
            for target_channel, target_chat in targets:
                await _send_schedule_alert_message(
                    gateway,
                    channel=target_channel,
                    text=message,
                    target_id=target_chat,
                )
            _remember_runtime_alert(cache_key, plan)
            await get_audit_log().log(
                action_type="schedule_alert",
                tool_name="cron_job",
                input_summary=(
                    f"job_id={job_id} targets="
                    f"{','.join(f'{item[0]}:{item[1] or '-'}' for item in targets)} "
                    f"severity={plan['severity']} stage={plan['stage']} "
                    f"repeat_count={plan['repeat_count']} "
                    f"retrying_after={plan['retrying_after']} "
                    f"escalate_after={plan['escalate_after']} "
                    f"reason={runtime.get('health_reason') or 'unknown'}"
                ),
                output_summary="Sent proactive schedule health alert.",
                status="error" if plan["severity"] in {"error", "critical"} else "warning",
                session_id=f"schedule:{job_id}",
            )
            if plan["escalated"]:
                await get_audit_log().log(
                    action_type="schedule_alert_escalation",
                    tool_name="cron_job",
                    input_summary=(
                        f"job_id={job_id} severity={plan['severity']} stage={plan['stage']} "
                        f"repeat_count={plan['repeat_count']} "
                        f"retrying_after={plan['retrying_after']} "
                        f"escalate_after={plan['escalate_after']} "
                        f"reason={runtime.get('health_reason') or 'unknown'}"
                    ),
                    output_summary="Escalated schedule alert after repeated schedule issue.",
                    status="error",
                    session_id=f"schedule:{job_id}",
                )

        stale_keys = [
            key for key in _runtime_alert_cache
            if key.startswith("schedule_health:") and key not in active_keys
        ]
        for key in stale_keys:
            _runtime_alert_cache.pop(key, None)
    except Exception as exc:
        logger.error("Schedule alert delivery failed: %s", exc)


async def _run_agent_step(agent: Any, store: Any, task: dict[str, Any]) -> str:
    """Execute or reuse the checkpointed agent step for one background task."""
    task_id = str(task["task_id"])
    step = await store.start_task_step(
        task_id,
        _AGENT_RUN_STEP_ID,
        step_name="agent_run",
        input_payload={
            "task_description": str(task["description"]),
            "session_id": f"task:{task_id}",
        },
        is_checkpoint=True,
        idempotent=False,
    )
    output = step.get("output") or {}
    if step["status"] == "succeeded" and "result_text" in output:
        logger.info("Reused agent_run checkpoint for background task `%s`", task_id)
        return str(output["result_text"])

    payload = dict(task.get("payload") or {})
    token = set_tool_runtime_context(
        session_id=f"task:{task_id}",
        task_id=task_id,
        step_id=_AGENT_RUN_STEP_ID,
        task_attempt=int(task.get("attempt_count") or 0),
        workflow_identity=str(payload.get("workflow_identity") or "").strip(),
    )
    try:
        result = await agent.run(
            user_message=str(task["description"]),
            session_id=f"task:{task_id}",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await store.fail_task_step(task_id, _AGENT_RUN_STEP_ID, last_error=str(exc))
        raise
    finally:
        reset_tool_runtime_context(token)

    await store.complete_task_step(
        task_id,
        _AGENT_RUN_STEP_ID,
        output_payload={"result_text": result},
    )
    return result


async def _run_workflow_role_task(
    store: Any,
    task: dict[str, Any],
) -> str:
    """Execute one minimal runtime-enqueued workflow role task."""

    async def _run_role_tool_handler(
        role_payload: dict[str, Any],
        dependency_outputs: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Execute one workflow-specific tool handler for a runtime role task."""
        if not bool(role_payload.get("role_tool_enabled")):
            return {}
        workflow_name = str(role_payload.get("workflow_name") or "").strip()
        role_stage = str(
            role_payload.get("role_stage_name")
            or role_payload.get("role_label")
            or role_payload.get("role")
            or ""
        ).strip()
        topic = str(role_payload.get("user_summary") or "").strip()
        if workflow_name != "wechat_article_flow" or not topic or not role_stage:
            return {}

        evidence_lines: list[str] = []
        snapshot = dict(role_payload.get("evidence_snapshot") or {})
        for item in list(snapshot.get("items") or []):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if url:
                evidence_lines.append(url)
        combined_dependency_outputs = list(role_payload.get("upstream_dependency_outputs") or [])
        combined_dependency_outputs.extend(dependency_outputs)
        for item in combined_dependency_outputs:
            if not isinstance(item, dict):
                continue
            tool_preview = str(item.get("tool_handler_output_preview") or "").strip()
            if tool_preview:
                evidence_lines.append(tool_preview[:160])
            artifact = str(item.get("artifact_preview") or "").strip()
            if artifact:
                evidence_lines.append(artifact[:160])
        evidence_text = "\n".join(evidence_lines[:8])
        try:
            from nanoclaw.tools import web_workflows

            tool_output = await web_workflows.wechat_article_assist(
                topic=topic,
                evidence=evidence_text,
                stage=role_stage,
            )
        except Exception as exc:
            logger.error(
                "Role tool handler failed for `%s` stage `%s`: %s",
                workflow_name,
                role_stage,
                exc,
            )
            return {
                "tool_handler_name": "wechat_article_assist",
                "tool_handler_stage": role_stage,
                "tool_handler_status": "error",
                "tool_handler_error": str(exc)[:200],
            }
        preview = str(tool_output or "").strip().splitlines()
        return {
            "tool_handler_name": "wechat_article_assist",
            "tool_handler_stage": role_stage,
            "tool_handler_status": "success",
            "tool_handler_output": str(tool_output or "")[:4000],
            "tool_handler_output_preview": (preview[0] if preview else "")[:200],
        }

    async def _load_role_bridge_items(parent_task_id: str) -> list[dict[str, Any]]:
        """Load the latest role-runtime bridge specs for one parent task."""
        if not parent_task_id:
            return []
        try:
            from nanoclaw.security.audit import get_audit_log

            loader = getattr(get_audit_log(), "get_latest_role_task_bridges", None)
            if not callable(loader):
                return []
            return list(await loader(f"task:{parent_task_id}"))
        except Exception as exc:
            logger.error(
                "Role runtime bridge lookup failed for `%s`: %s",
                parent_task_id,
                exc,
            )
            return []

    async def _enqueue_role_recovery_task(
        role_payload: dict[str, Any],
        role_result: dict[str, Any],
        recovery_action: dict[str, Any],
    ) -> dict[str, Any]:
        """Enqueue one explicit recovery role task for the current role failure."""
        parent_task_id = str(role_payload.get("parent_task_id") or "").strip()
        if not parent_task_id:
            return {}
        resume_checkpoint_id = str(recovery_action.get("resume_checkpoint_id") or "").strip()
        target_task_key = str(
            recovery_action.get("recovery_task_key")
            or recovery_action.get("resume_checkpoint_id")
            or ""
        ).strip()
        if not target_task_key:
            return {}
        bridge_items = await _load_role_bridge_items(parent_task_id)
        bridge_item = next(
            (
                item
                for item in bridge_items
                if str(item.get("task_key") or item.get("payload", {}).get("task_key") or "").strip()
                == target_task_key
            ),
            None,
        )
        if not isinstance(bridge_item, dict):
            return {}
        recovery_payload = dict(bridge_item.get("payload") or {})
        recovery_payload["parent_task_id"] = parent_task_id
        parent_session_id = str(role_payload.get("parent_session_id") or "").strip()
        if parent_session_id:
            recovery_payload["parent_session_id"] = parent_session_id
        workflow_identity = str(role_payload.get("workflow_identity") or "").strip()
        if workflow_identity:
            recovery_payload["workflow_identity"] = workflow_identity
        merged_snapshot = _merge_role_evidence_snapshots(
            dict(recovery_payload.get("evidence_snapshot") or {}),
            dict(role_payload.get("evidence_snapshot") or {}),
        )
        merged_refs = _dedupe_role_refs(
            list(recovery_payload.get("evidence_refs") or []),
            list(role_payload.get("evidence_refs") or []),
            list(recovery_action.get("evidence_refs") or []),
            list(role_result.get("resume_evidence_refs") or []),
        )[:5]
        recovery_path = [
            str(item).strip()
            for item in list(recovery_action.get("recovery_path") or [])
            if str(item).strip()
        ]
        recovery_payload["evidence_snapshot"] = merged_snapshot
        recovery_payload["evidence_refs"] = merged_refs
        recovery_payload["recovery_path"] = recovery_path
        existing_brief = str(recovery_payload.get("execution_brief") or "").strip()
        recovery_brief = str(recovery_action.get("content") or "").strip()
        if recovery_brief and recovery_brief not in existing_brief:
            recovery_payload["execution_brief"] = (
                f"{existing_brief}\n{recovery_brief}".strip() if existing_brief else recovery_brief
            )
        recovery_payload["resume_checkpoint_id"] = resume_checkpoint_id or target_task_key
        recovery_payload["recovery_task_key"] = target_task_key
        recovery_payload["resume_state"] = {
            "source_workflow_run_id": int(role_result.get("resume_source_workflow_run_id") or 0),
            "workflow_name": str(role_payload.get("workflow_name") or "").strip(),
            "workflow_identity": workflow_identity,
            "workflow_status": "degraded",
            "failure_reason": str(recovery_action.get("reason") or "").strip()[:160],
            "resume_checkpoint_id": resume_checkpoint_id or target_task_key,
            "role": str(recovery_payload.get("role") or recovery_action.get("recovery_role") or ""),
            "stage": str(recovery_payload.get("stage") or "").strip(),
            "evidence_refs": merged_refs,
            "evidence_snapshot": merged_snapshot,
        }
        recovery_payload["recovery_state"] = {
            "failed_role": str(recovery_action.get("failed_role") or "").strip(),
            "recovery_role": str(recovery_action.get("recovery_role") or "").strip(),
            "reason": str(recovery_action.get("reason") or "").strip()[:160],
            "resume_checkpoint_id": resume_checkpoint_id or target_task_key,
            "recovery_task_key": target_task_key,
            "recovery_path": recovery_path,
            "scheduler_policy": str(recovery_action.get("scheduler_policy") or "").strip(),
            "attempt_number": int(role_result.get("attempt_number") or 0),
            "budget_limit": int(role_result.get("budget_limit") or 0),
            "remaining_budget": int(role_result.get("remaining_budget") or 0),
        }
        recovery_payload = _prepare_role_turn_payload(
            recovery_payload,
            list(recovery_payload.get("upstream_dependency_outputs") or []),
        )
        reused = await _maybe_rearm_existing_role_task_for_recovery(
            store,
            parent_task_id=parent_task_id,
            target_task_key=target_task_key,
            recovery_payload=recovery_payload,
        )
        if reused:
            _schedule_queue_drain()
            return reused
        idempotency_key = str(bridge_item.get("idempotency_key") or "").strip()
        attempt_number = max(1, int(role_result.get("attempt_number") or 1))
        recovery_suffix = f":recovery:{attempt_number}"
        created = await store.create_task(
            str(bridge_item.get("description") or target_task_key or "workflow role"),
            task_type=str(bridge_item.get("task_type") or "workflow_role"),
            payload=recovery_payload,
            source=_ROLE_TASK_SOURCE,
            session_id=str(recovery_payload.get("session_id") or ""),
            priority=int(bridge_item.get("priority") or 100),
            timeout_seconds=int(bridge_item.get("timeout_seconds") or 300),
            max_attempts=int(bridge_item.get("max_attempts") or 1),
            idempotency_key=(idempotency_key + recovery_suffix).strip(":"),
        )
        _schedule_queue_drain()
        return created

    task_id = str(task["task_id"])
    payload, resume_state = _apply_role_resume_state(dict(task.get("payload") or {}))
    step = await store.start_task_step(
        task_id,
        _ROLE_RUN_STEP_ID,
        step_name="role_runtime_ack",
        input_payload={
            "workflow_name": str(payload.get("workflow_name") or ""),
            "task_key": str(payload.get("task_key") or ""),
            "role": str(payload.get("role") or ""),
            "role_label": str(payload.get("role_label") or ""),
            "stage": str(payload.get("stage") or ""),
            "parent_task_id": str(payload.get("parent_task_id") or ""),
        },
        is_checkpoint=True,
        idempotent=True,
    )
    output = step.get("output") or {}
    if step["status"] == "succeeded" and output.get("result_text"):
        return str(output["result_text"])

    unmet_dependencies: list[str] = []
    dependency_outputs_by_key: dict[str, dict[str, Any]] = {
        str(item.get("task_key") or ""): dict(item)
        for item in list(payload.get("upstream_dependency_outputs") or [])
        if isinstance(item, dict) and str(item.get("task_key") or "").strip()
    }
    parent_task_id = str(payload.get("parent_task_id") or "")
    depends_on = _get_role_dependency_keys(payload, "depends_on")
    depends_on_any = _get_role_dependency_keys(payload, "depends_on_any")
    if (depends_on or depends_on_any) and not parent_task_id:
        unmet_dependencies.extend(depends_on)
        if depends_on_any:
            unmet_dependencies.append("any_of:" + ",".join(depends_on_any))
    if parent_task_id and (depends_on or depends_on_any):
        child_tasks = await store.list_child_tasks(parent_task_id, source=_ROLE_TASK_SOURCE)
        dependency_map = {
            str(item.get("payload", {}).get("task_key") or ""): item
            for item in child_tasks
            if isinstance(item, dict)
        }
        succeeded_dependency_outputs: dict[str, dict[str, Any]] = {}
        for dependency_key in set(depends_on + depends_on_any):
            sibling = dependency_map.get(dependency_key)
            if sibling is None or str(sibling.get("status") or "") != "succeeded":
                continue
            sibling_step = await store.get_task_step(str(sibling["task_id"]), _ROLE_RUN_STEP_ID)
            sibling_output = dict(sibling_step.get("output") or {}) if sibling_step else {}
            succeeded_dependency_outputs[dependency_key] = {
                "task_key": dependency_key,
                "role": str(sibling.get("payload", {}).get("role") or ""),
                "role_label": str(sibling_output.get("role_label") or sibling.get("payload", {}).get("role_label") or sibling.get("payload", {}).get("role") or ""),
                "status": str(sibling.get("status") or ""),
                "attempt_number": int(sibling.get("attempt_count") or 0),
                "turn_index": int(
                    sibling_output.get("turn_index")
                    or sibling.get("payload", {}).get("turn_index")
                    or 1
                ),
                "action": str(sibling_output.get("action") or ""),
                "artifact_preview": str(sibling_output.get("artifact_preview") or "")[:200],
                "tool_handler_name": str(sibling_output.get("tool_handler_name") or ""),
                "tool_handler_stage": str(sibling_output.get("tool_handler_stage") or ""),
                "tool_handler_status": str(sibling_output.get("tool_handler_status") or ""),
                "tool_handler_output_preview": str(sibling_output.get("tool_handler_output_preview") or "")[:200],
                "result_text": str(sibling_output.get("result_text") or "")[:160],
                "evidence_refs": list(sibling_output.get("evidence_refs") or []),
            }
        new_missing, resolved_outputs = _role_dependency_view(payload, succeeded_dependency_outputs)
        unmet_dependencies.extend(new_missing)
        for item in resolved_outputs:
            dependency_key = str(item.get("task_key") or "").strip()
            if dependency_key:
                dependency_outputs_by_key[dependency_key] = item
    if unmet_dependencies:
        wait_until = datetime.now(UTC) + timedelta(seconds=2)
        reason = "waiting for role dependencies: " + ", ".join(unmet_dependencies)
        await store.fail_task_step(task_id, _ROLE_RUN_STEP_ID, last_error=reason)
        await store.defer_task_attempt(
            task_id,
            next_attempt_at=wait_until.strftime("%Y-%m-%d %H:%M:%S"),
            last_error=reason,
        )
        raise DeferredBackgroundTask(reason)

    token = set_tool_runtime_context(
        session_id=f"task:{task_id}",
        task_id=task_id,
        step_id=_ROLE_RUN_STEP_ID,
        task_attempt=int(task.get("attempt_count") or 0),
        workflow_identity=str(payload.get("workflow_identity") or "").strip(),
    )
    recovery_task: dict[str, Any] = {}
    recovery_action: dict[str, Any] = {}
    try:
        dependency_outputs = list(dependency_outputs_by_key.values())
        attempt_number = int(task.get("attempt_count") or 0)
        budget_limit = max(
            1,
            int(payload.get("retry_budget") or task.get("max_attempts") or 1),
        )
        result = build_role_runtime_execution_result(
            payload=payload,
            dependency_outputs=dependency_outputs,
        )
        result.update(
            {
                "attempt_number": attempt_number,
                "budget_limit": budget_limit,
                "remaining_budget": max(0, budget_limit - attempt_number),
                "resume_checkpoint_id": str(
                    payload.get("resume_checkpoint_id")
                    or resume_state.get("resume_checkpoint_id")
                    or ""
                ).strip(),
                "resume_status": "resumed" if resume_state else "",
                "resume_source_workflow_run_id": int(
                    resume_state.get("source_workflow_run_id") or 0
                ),
                "resume_restored_evidence_count": int(
                    dict(resume_state.get("evidence_snapshot") or {}).get("count") or 0
                ),
                "resume_evidence_refs": list(resume_state.get("evidence_refs") or [])[:5],
            }
        )
        tool_result = await _run_role_tool_handler(payload, dependency_outputs)
        if tool_result:
            result.update(tool_result)
        llm_result = await _run_role_llm_turn(payload, dependency_outputs, result)
        if llm_result:
            result.update(llm_result)
        recovery_action = _build_runtime_role_recovery_action(payload, result)
        if recovery_action and attempt_number <= budget_limit:
            recovery_task = await _enqueue_role_recovery_task(payload, result, recovery_action)
            if recovery_task:
                result.update(
                    {
                        "recovery_status": "requested",
                        "recovery_task_id": str(recovery_task.get("task_id") or ""),
                        "recovery_task_key": str(
                            recovery_action.get("recovery_task_key")
                            or recovery_action.get("resume_checkpoint_id")
                            or ""
                        ).strip(),
                        "recovery_role": str(
                            recovery_action.get("recovery_role") or ""
                        ).strip(),
                        "recovery_reason": str(recovery_action.get("reason") or "").strip()[:160],
                    }
                )
        result_text = str(result.get("result_text") or "").strip()
    finally:
        reset_tool_runtime_context(token)

    await store.complete_task_step(
        task_id,
        _ROLE_RUN_STEP_ID,
        output_payload=result,
    )
    if recovery_task:
        raise RoleRecoveryRequested(
            str(result.get("recovery_reason") or result_text or "role recovery requested"),
            result=result,
            recovery_task=recovery_task,
        )
    return result_text


async def _enqueue_role_runtime_bridge_tasks(
    store: Any,
    task: dict[str, Any],
) -> list[dict[str, Any]]:
    """Materialize only ready role-runtime bridge specs as real runtime tasks."""
    payload = dict(task.get("payload") or {})
    parent_task_id = str(payload.get("parent_task_id") or task.get("task_id") or "").strip()
    current_task_key = str(payload.get("task_key") or "").strip()
    current_source = str(task.get("source") or "").strip()
    if not parent_task_id:
        return []
    parent_session_id = str(payload.get("parent_session_id") or "").strip()
    workflow_identity = str(payload.get("workflow_identity") or "").strip()
    if not parent_session_id and str(task.get("task_id") or "") != parent_task_id:
        try:
            parent_task = await store.get_task(parent_task_id)
        except Exception as exc:
            logger.error("Parent task lookup failed for `%s`: %s", parent_task_id, exc)
            parent_task = None
        if parent_task is not None:
            parent_payload = dict(parent_task.get("payload") or {})
            parent_session_id = str(parent_payload.get("parent_session_id") or "").strip()
            if not workflow_identity:
                workflow_identity = str(parent_payload.get("workflow_identity") or "").strip()
    try:
        from nanoclaw.security.audit import get_audit_log

        audit = get_audit_log()
        loader = getattr(audit, "get_latest_role_task_bridges", None)
        resume_loader = getattr(audit, "get_latest_role_resume_state", None)
        if not callable(loader):
            return []
        bridge_items = await loader(f"task:{parent_task_id}")
    except Exception as exc:
        logger.error("Role runtime bridge lookup failed for `%s`: %s", parent_task_id, exc)
        return []

    existing_children = await store.list_child_tasks(parent_task_id, source=_ROLE_TASK_SOURCE)
    existing_by_key = {
        str(item.get("payload", {}).get("task_key") or ""): item
        for item in existing_children
        if isinstance(item, dict)
    }
    created: list[dict[str, Any]] = []
    should_wake = False
    resume_state_cache: dict[str, dict[str, Any] | None] = {}
    resume_session_ids = _dedupe_role_refs(
        [f"task:{parent_task_id}"],
        [parent_session_id],
    )
    for item in bridge_items:
        if str(item.get("source") or "") != _ROLE_TASK_SOURCE:
            continue
        payload = dict(item.get("payload") or {})
        payload["parent_task_id"] = str(payload.get("parent_task_id") or parent_task_id)
        if parent_session_id:
            payload["parent_session_id"] = parent_session_id
        if workflow_identity:
            payload["workflow_identity"] = workflow_identity
        workflow_name = str(payload.get("workflow_name") or "").strip()
        task_key = str(payload.get("task_key") or item.get("task_key") or "").strip()
        if not task_key:
            continue
        if workflow_name and callable(resume_loader):
            cache_key = f"{workflow_name}:{workflow_identity}"
            if cache_key not in resume_state_cache:
                resume_state: dict[str, Any] | None = None
                if workflow_identity:
                    try:
                        resume_state = await resume_loader(
                            resume_session_ids[0] if resume_session_ids else "",
                            workflow_name,
                            workflow_identity=workflow_identity,
                        )
                    except Exception as exc:
                        logger.error(
                            "Role resume lookup failed for parent `%s` workflow `%s`: %s",
                            parent_task_id,
                            workflow_name,
                            exc,
                        )
                        resume_state = None
                if not resume_state:
                    for resume_session_id in resume_session_ids:
                        try:
                            resume_state = await resume_loader(
                                resume_session_id,
                                workflow_name,
                            )
                        except Exception as exc:
                            logger.error(
                                "Role resume lookup failed for parent `%s` workflow `%s`: %s",
                                parent_task_id,
                                workflow_name,
                                exc,
                            )
                            resume_state = None
                        if resume_state:
                            break
                resume_state_cache[cache_key] = resume_state
            resume_state = dict(resume_state_cache.get(cache_key) or {})
            if resume_state and _resume_state_applies_to_role_payload(payload, resume_state):
                payload["resume_state"] = resume_state
                payload["resume_brief"] = build_persistent_resume_brief(resume_state)
                payload["resume_checkpoint_id"] = str(
                    payload.get("resume_checkpoint_id")
                    or resume_state.get("resume_checkpoint_id")
                    or ""
                ).strip()
                payload["evidence_snapshot"] = _merge_role_evidence_snapshots(
                    dict(payload.get("evidence_snapshot") or {}),
                    dict(resume_state.get("evidence_snapshot") or {}),
                )
                payload["evidence_refs"] = _dedupe_role_refs(
                    list(payload.get("evidence_refs") or []),
                    list(resume_state.get("evidence_refs") or []),
                )[:5]
        depends_on = [
            str(dep).strip() for dep in list(payload.get("depends_on") or []) if str(dep).strip()
        ]
        depends_on_any = [
            str(dep).strip()
            for dep in list(payload.get("depends_on_any") or [])
            if str(dep).strip()
        ]
        if current_source == _ROLE_TASK_SOURCE:
            related_keys = set(depends_on + depends_on_any)
            if (
                not workflow_role_graph_fanout_enabled()
                and related_keys
                and current_task_key
                and current_task_key not in related_keys
            ):
                continue
        elif depends_on or depends_on_any:
            continue
        if any(
            str(existing_by_key.get(dep, {}).get("status") or "") != "succeeded"
            for dep in depends_on
        ):
            continue
        if depends_on_any and not any(
            str(existing_by_key.get(dep, {}).get("status") or "") == "succeeded"
            for dep in depends_on_any
        ):
            continue
        dependency_outputs: list[dict[str, Any]] = []
        for dependency_key in dict.fromkeys(depends_on + depends_on_any):
            sibling = existing_by_key.get(dependency_key)
            if not isinstance(sibling, dict):
                continue
            if str(sibling.get("status") or "") != "succeeded":
                continue
            sibling_step = await store.get_task_step(str(sibling["task_id"]), _ROLE_RUN_STEP_ID)
            sibling_output = dict(sibling_step.get("output") or {}) if sibling_step else {}
            dependency_outputs.append(
                {
                    "task_key": dependency_key,
                    "role": str(sibling.get("payload", {}).get("role") or ""),
                    "role_label": str(sibling_output.get("role_label") or sibling.get("payload", {}).get("role_label") or sibling.get("payload", {}).get("role") or ""),
                    "status": str(sibling.get("status") or ""),
                    "attempt_number": int(sibling.get("attempt_count") or 0),
                    "turn_index": int(
                        sibling_output.get("turn_index")
                        or sibling.get("payload", {}).get("turn_index")
                        or 1
                    ),
                    "action": str(sibling_output.get("action") or ""),
                    "artifact_preview": str(sibling_output.get("artifact_preview") or "")[:200],
                    "tool_handler_name": str(sibling_output.get("tool_handler_name") or ""),
                    "tool_handler_stage": str(sibling_output.get("tool_handler_stage") or ""),
                    "tool_handler_status": str(sibling_output.get("tool_handler_status") or ""),
                    "tool_handler_output_preview": str(sibling_output.get("tool_handler_output_preview") or "")[:200],
                    "result_text": str(sibling_output.get("result_text") or "")[:160],
                    "evidence_refs": list(sibling_output.get("evidence_refs") or []),
                }
            )
        if dependency_outputs:
            payload["upstream_dependency_outputs"] = dependency_outputs
        else:
            payload.pop("upstream_dependency_outputs", None)
        payload = _prepare_role_turn_payload(payload, dependency_outputs)
        existing_child = existing_by_key.get(task_key)
        if isinstance(existing_child, dict):
            existing_status = str(existing_child.get("status") or "")
            existing_payload = dict(existing_child.get("payload") or {})
            existing_fingerprint = str(
                existing_payload.get("upstream_input_fingerprint") or ""
            ).strip()
            target_fingerprint = str(payload.get("upstream_input_fingerprint") or "").strip()
            if existing_status in {"pending", "running"}:
                if existing_fingerprint == target_fingerprint and existing_status == "pending":
                    should_wake = True
                continue
            if existing_status == "succeeded":
                if existing_fingerprint == target_fingerprint:
                    continue
                turn_index = max(1, int(existing_payload.get("turn_index") or 1))
                turn_budget = max(
                    1,
                    int(existing_payload.get("turn_budget") or payload.get("turn_budget") or 1),
                )
                if turn_index >= turn_budget:
                    logger.info(
                        "Skipped role rearm for `%s` turn %s/%s because the upstream fingerprint changed",
                        task_key,
                        turn_index,
                        turn_budget,
                    )
                    continue
                prior_step = await store.get_task_step(
                    str(existing_child["task_id"]),
                    _ROLE_RUN_STEP_ID,
                )
                prior_output = dict(prior_step.get("output") or {}) if prior_step else {}
                rearmed_payload = _build_rearmed_role_payload(
                    existing_payload,
                    payload,
                    prior_output,
                )
                rearmed_payload = _prepare_role_turn_payload(
                    rearmed_payload,
                    dependency_outputs,
                )
                try:
                    rearmed = await store.rearm_task(
                        str(existing_child["task_id"]),
                        payload=rearmed_payload,
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to rearm role runtime task `%s` for parent `%s`: %s",
                        task_key,
                        parent_task_id,
                        exc,
                    )
                    continue
                existing_by_key[task_key] = rearmed
                should_wake = True
                continue
        try:
            child = await store.create_task(
                str(item.get("description") or task_key or "workflow role"),
                task_type=str(item.get("task_type") or "workflow_role"),
                payload=payload,
                source=_ROLE_TASK_SOURCE,
                session_id=str(payload.get("session_id") or ""),
                priority=int(item.get("priority") or 100),
                timeout_seconds=int(item.get("timeout_seconds") or 300),
                max_attempts=int(item.get("max_attempts") or 1),
                idempotency_key=str(item.get("idempotency_key") or ""),
            )
        except Exception as exc:
            logger.error(
                "Failed to enqueue role runtime task `%s` for parent `%s`: %s",
                item.get("task_key") or "-",
                parent_task_id,
                exc,
            )
            continue
        existing_by_key[task_key] = child
        if bool(child.get("reused_existing")):
            if str(child.get("status") or "") in {"pending", "running"}:
                should_wake = True
            continue
        created.append(child)
        should_wake = True
    if created:
        logger.info(
            "Queued %s ready role runtime task(s) from parent `%s`",
            len(created),
            parent_task_id,
        )
    if should_wake:
        _schedule_queue_drain()
    return created


async def _send_task_notification(
    gateway: Any,
    store: Any,
    task: dict[str, Any],
    *,
    step_id: str,
    message: str,
    input_payload: dict[str, Any],
    output_payload: dict[str, Any],
    channel_override: str = "",
) -> None:
    """Send one deduplicated proactive notification for a background task."""
    task_id = str(task["task_id"])
    channel = channel_override.strip() or _resolve_channel(task)
    target_id = str(input_payload.get("target_id") or "")
    step = await store.start_task_step(
        task_id,
        step_id,
        step_name=step_id,
        input_payload={
            "channel": channel,
            "task_id": task_id,
            "target_id": target_id,
            **input_payload,
        },
        is_checkpoint=True,
        idempotent=True,
    )
    if step["status"] == "succeeded":
        logger.info("Skipped duplicate `%s` step for background task `%s`", step_id, task_id)
        return

    try:
        if (
            channel == "feishu"
            and target_id
            and hasattr(gateway, "send_proactive_targeted")
        ):
            sent = await gateway.send_proactive_targeted(
                channel="feishu",
                text=message,
                target_id=target_id,
            )
            if not sent:
                raise RuntimeError("Feishu proactive send failed for the target chat.")
        elif (
            channel == "feishu"
            and target_id
            and getattr(gateway, "channels", {}).get("feishu") is not None
            and hasattr(gateway.channels["feishu"], "send_proactive_to")
        ):
            sent = await gateway.channels["feishu"].send_proactive_to(target_id, message)
            if not sent:
                raise RuntimeError("Feishu proactive send failed for the target chat.")
        else:
            await gateway.send_proactive(message, channel=channel)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await store.fail_task_step(task_id, step_id, last_error=str(exc))
        raise

    await store.complete_task_step(
        task_id,
        step_id,
        output_payload={"channel": channel, "target_id": target_id, **output_payload},
    )


async def _send_result_notification(
    gateway: Any,
    store: Any,
    task: dict[str, Any],
    result: str,
) -> None:
    """Send or reuse the proactive success notification step."""
    task_id = str(task["task_id"])
    await _send_task_notification(
        gateway,
        store,
        task,
        step_id=_NOTIFY_RESULT_STEP_ID,
        message=f"Background task {task_id} complete:\n\n{result}",
        input_payload={"result_text": result},
        output_payload={"kind": "success"},
    )


async def _send_cancelled_notification(
    gateway: Any,
    store: Any,
    task: dict[str, Any],
) -> None:
    """Send or reuse the proactive cancellation notification step."""
    task_id = str(task["task_id"])
    await _send_task_notification(
        gateway,
        store,
        task,
        step_id=_NOTIFY_CANCELLED_STEP_ID,
        message=f"Background task {task_id} cancelled.",
        input_payload={"status": "cancelled"},
        output_payload={"kind": "cancelled"},
    )


async def _send_failure_notification(
    gateway: Any,
    store: Any,
    task: dict[str, Any],
    reason: str,
) -> None:
    """Send or reuse the proactive terminal failure notification step."""
    task_id = str(task["task_id"])
    await _send_task_notification(
        gateway,
        store,
        task,
        step_id=_NOTIFY_FAILURE_STEP_ID,
        message=f"Background task {task_id} failed: {reason}",
        input_payload={"status": "failed", "reason": reason[:300]},
        output_payload={"kind": "failed"},
    )


async def _queue_cron_delivery_retry(
    store: Any,
    task: dict[str, Any],
    *,
    job_name: str,
    result_text: str,
    notify_channel: str,
    target_id: str,
    quiet_start: str,
    quiet_end: str,
    last_error: str,
) -> dict[str, Any]:
    """Persist one follow-up delivery retry task for a cron result."""
    original_task_id = str(task["task_id"])
    retry_task = await store.create_task(
        f"Cron delivery retry: {job_name}",
        task_type="cron_delivery",
        payload={
            "original_task_id": original_task_id,
            "job_name": job_name,
            "result_text": result_text,
            "channel": notify_channel,
            "target_id": target_id,
            "quiet_start": quiet_start,
            "quiet_end": quiet_end,
            "last_error": last_error[:300],
        },
        source=_CRON_DELIVERY_TASK_SOURCE,
        session_id=str(task.get("session_id") or "cron:system"),
        priority=_CRON_DELIVERY_TASK_PRIORITY,
        timeout_seconds=_CRON_DELIVERY_TASK_TIMEOUT_SECONDS,
        max_attempts=_CRON_DELIVERY_TASK_MAX_ATTEMPTS,
        retry_backoff_seconds=_CRON_DELIVERY_TASK_RETRY_BACKOFF_SECONDS,
        idempotency_key=f"cron-delivery:{original_task_id}",
    )
    wake_background_runtime()
    return retry_task


async def _run_heartbeat_task(
    store: Any,
    task: dict[str, Any],
) -> str:
    """Execute one persisted heartbeat task inside the shared runtime."""
    from nanoclaw.channels.gateway import get_gateway
    from nanoclaw.core.config import get_config
    from nanoclaw.cron.heartbeat import HeartbeatRunner

    task_id = str(task["task_id"])
    payload = dict(task.get("payload") or {})
    step = await store.start_task_step(
        task_id,
        _HEARTBEAT_RUN_STEP_ID,
        step_name="heartbeat_run",
        input_payload={
            "checklist_path": str(payload.get("checklist_path") or ""),
            "notify_channel": str(payload.get("notify_channel") or ""),
        },
        is_checkpoint=False,
        idempotent=False,
    )
    output = step.get("output") or {}
    if step["status"] == "succeeded" and output.get("status"):
        return str(output["status"])

    gateway = get_gateway()
    if gateway is None:
        raise RuntimeError("Heartbeat task cannot run without an active gateway.")

    config = get_config().heartbeat
    token = set_tool_runtime_context(
        session_id=f"task:{task_id}",
        task_id=task_id,
        step_id=_HEARTBEAT_RUN_STEP_ID,
        task_attempt=int(task.get("attempt_count") or 0),
    )
    try:
        status = await HeartbeatRunner(config, gateway).run_once()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        await store.fail_task_step(task_id, _HEARTBEAT_RUN_STEP_ID, last_error=str(exc))
        raise
    finally:
        reset_tool_runtime_context(token)

    await store.complete_task_step(
        task_id,
        _HEARTBEAT_RUN_STEP_ID,
        output_payload={"status": status},
    )
    return status


async def _run_cron_task(
    store: Any,
    task: dict[str, Any],
) -> str:
    """Execute one persisted cron task inside the shared runtime."""
    from nanoclaw.channels.gateway import get_gateway
    from nanoclaw.security.prompt_guard import get_prompt_guard

    task_id = str(task["task_id"])
    payload = dict(task.get("payload") or {})
    job_name = str(payload.get("job_name") or "Cron job")
    job_message = str(payload.get("message") or "")
    notify_channel = str(payload.get("channel") or "telegram")
    target_id = str(payload.get("target_id") or "")
    quiet_start = str(payload.get("quiet_start") or "")
    quiet_end = str(payload.get("quiet_end") or "")
    step = await store.start_task_step(
        task_id,
        _CRON_RUN_STEP_ID,
        step_name="cron_run",
        input_payload={
            "job_name": job_name,
            "message": job_message,
            "channel": notify_channel,
            "target_id": target_id,
            "quiet_start": quiet_start,
            "quiet_end": quiet_end,
        },
        is_checkpoint=True,
        idempotent=False,
    )
    output = step.get("output") or {}
    if step["status"] == "succeeded" and "result_text" in output:
        result_text = str(output["result_text"])
    else:
        gateway = get_gateway()
        if gateway is None:
            raise RuntimeError("Cron task cannot run without an active gateway.")
        guard = get_prompt_guard()
        detected, matched = guard.check_injection(job_message)
        if detected:
            logger.warning(
                "Cron job `%s` message contains injection pattern: %s",
                job_name,
                matched,
            )
            result_text = "blocked_by_prompt_guard"
        else:
            token = set_tool_runtime_context(
                session_id=f"task:{task_id}",
                task_id=task_id,
                step_id=_CRON_RUN_STEP_ID,
                task_attempt=int(task.get("attempt_count") or 0),
            )
            try:
                result_text = await gateway.handle_incoming(
                    channel_id="cron",
                    user_id="system",
                    message=job_message,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await store.fail_task_step(task_id, _CRON_RUN_STEP_ID, last_error=str(exc))
                raise
            finally:
                reset_tool_runtime_context(token)
        await store.complete_task_step(
            task_id,
            _CRON_RUN_STEP_ID,
            output_payload={"result_text": result_text},
        )

    if result_text != "blocked_by_prompt_guard":
        if _is_in_quiet_window(quiet_start, quiet_end):
            notify_step = await store.start_task_step(
                task_id,
                _CRON_NOTIFY_STEP_ID,
                step_name="cron_notify",
                input_payload={
                    "job_name": job_name,
                    "channel": notify_channel,
                    "target_id": target_id,
                    "quiet_start": quiet_start,
                    "quiet_end": quiet_end,
                    "result_text": result_text,
                },
                is_checkpoint=True,
                idempotent=True,
            )
            if notify_step["status"] != "succeeded":
                logger.info(
                    "Suppressed cron notification for `%s` inside quiet window %s-%s",
                    job_name,
                    quiet_start,
                    quiet_end,
                )
                await store.complete_task_step(
                    task_id,
                    _CRON_NOTIFY_STEP_ID,
                    output_payload={
                        "kind": "cron_suppressed",
                        "quiet_window": f"{quiet_start}-{quiet_end}",
                    },
                )
            return result_text
        gateway = get_gateway()
        if gateway is None:
            raise RuntimeError("Cron task cannot notify without an active gateway.")
        try:
            await _send_task_notification(
                gateway,
                store,
                task,
                step_id=_CRON_NOTIFY_STEP_ID,
                message=f"**{job_name}**\n\n{result_text}",
                input_payload={
                    "job_name": job_name,
                    "channel": notify_channel,
                    "target_id": target_id,
                    "quiet_start": quiet_start,
                    "quiet_end": quiet_end,
                    "result_text": result_text,
                },
                output_payload={"kind": "cron_success"},
                channel_override=notify_channel,
            )
        except Exception as exc:
            retry_task = await _queue_cron_delivery_retry(
                store,
                task,
                job_name=job_name,
                result_text=result_text,
                notify_channel=notify_channel,
                target_id=target_id,
                quiet_start=quiet_start,
                quiet_end=quiet_end,
                last_error=str(exc),
            )
            logger.warning(
                "Queued cron delivery retry `%s` for `%s` after notify failure: %s",
                retry_task["task_id"],
                job_name,
                exc,
            )
            try:
                await store.complete_task_step(
                    task_id,
                    _CRON_NOTIFY_STEP_ID,
                    output_payload={
                        "kind": "cron_delivery_retry_scheduled",
                        "retry_task_id": retry_task["task_id"],
                        "retry_reused_existing": bool(retry_task.get("reused_existing")),
                        "reason": str(exc)[:200],
                        "target_id": target_id,
                    },
                )
            except Exception as step_exc:
                logger.error(
                    "Failed to persist cron retry scheduling step for `%s`: %s",
                    task_id,
                    step_exc,
                )
    return result_text


async def _run_cron_delivery_retry_task(
    store: Any,
    task: dict[str, Any],
) -> str:
    """Deliver one previously generated cron result without re-running the agent."""
    from nanoclaw.channels.gateway import get_gateway

    task_id = str(task["task_id"])
    payload = dict(task.get("payload") or {})
    job_name = str(payload.get("job_name") or "Cron job")
    result_text = str(payload.get("result_text") or "")
    notify_channel = str(payload.get("channel") or "telegram")
    target_id = str(payload.get("target_id") or "")
    quiet_start = str(payload.get("quiet_start") or "")
    quiet_end = str(payload.get("quiet_end") or "")
    original_task_id = str(payload.get("original_task_id") or "")
    if _is_in_quiet_window(quiet_start, quiet_end):
        next_retry = _next_quiet_window_exit(quiet_start, quiet_end)
        deferred = await store.defer_task_attempt(
            task_id,
            next_attempt_at=next_retry.strftime("%Y-%m-%d %H:%M:%S"),
            last_error=f"deferred by quiet window until {next_retry.strftime('%Y-%m-%d %H:%M:%S')}",
        )
        raise DeferredBackgroundTask(
            "cron delivery deferred until "
            f"{deferred['next_attempt_at']} because the quiet window is still active"
        )

    gateway = get_gateway()
    if gateway is None:
        raise RuntimeError("Cron delivery retry cannot notify without an active gateway.")
    await _send_task_notification(
        gateway,
        store,
        task,
        step_id=_CRON_DELIVERY_NOTIFY_STEP_ID,
        message=f"**{job_name}**\n\n{result_text}",
        input_payload={
            "job_name": job_name,
            "channel": notify_channel,
            "target_id": target_id,
            "quiet_start": quiet_start,
            "quiet_end": quiet_end,
            "result_text": result_text,
            "original_task_id": original_task_id,
        },
        output_payload={
            "kind": "cron_retry_success",
            "original_task_id": original_task_id,
        },
        channel_override=notify_channel,
    )
    return result_text


def get_background_runtime_metrics() -> dict[str, Any]:
    """Return in-process runtime capacity and utilization metrics."""
    capacity = _get_background_capacity()
    active = len(_active_background_tasks)
    return {
        "worker_id": _worker_id,
        "active_tasks": active,
        "capacity": capacity,
        "saturation_pct": int((active / capacity) * 100),
        "starvation_threshold_seconds": _get_starvation_threshold_seconds(),
        "stall_threshold_seconds": _get_runtime_stall_threshold_seconds(),
        "lease_timeout_seconds": _TASK_LEASE_TIMEOUT_SECONDS,
        "heartbeat_interval_seconds": _TASK_HEARTBEAT_INTERVAL_SECONDS,
        "alert_channel": _get_runtime_alert_channel() or "auto",
        "alert_escalation_channel": _get_runtime_alert_escalation_channel() or "auto-secondary",
        "alert_cooldown_seconds": _get_runtime_alert_cooldown_seconds(),
        "alert_escalate_after": _ALERT_ESCALATE_AFTER,
        "schedule_alert_retrying_after": _get_schedule_alert_retrying_after(),
        "schedule_alert_escalate_after": _get_schedule_alert_escalate_after(),
    }


def _schedule_queue_drain() -> None:
    """Start the in-process queue drainer when needed."""
    global _drain_task
    if _runtime_stopping:
        return
    loop = asyncio.get_running_loop()
    _ensure_worker_id()
    _ensure_heartbeat_loop()
    if _drain_task is not None:
        if _drain_task.done() or _drain_task.get_loop() is not loop:
            _drain_task = None
        else:
            return
    if loop.is_closed():
        return
    _drain_task = loop.create_task(_drain_pending_tasks())


def wake_background_runtime() -> None:
    """Wake the local background runtime so newly queued work gets claimed."""
    _schedule_queue_drain()


async def _claim_next_runtime_task(store: Any, capacity: int) -> dict[str, Any] | None:
    """Claim the next ready runtime task across the supported sources."""
    return await store.claim_next_task(
        sources=list(_RUNTIME_TASK_SOURCES),
        worker_id=_ensure_worker_id(),
        starvation_threshold_seconds=_get_starvation_threshold_seconds(),
        max_running_tasks=capacity,
    )


async def _drain_pending_tasks() -> None:
    """Claim pending tasks until the local concurrency limit is reached."""
    global _drain_task
    store = get_task_store()
    bg_lock = _get_bg_lock()
    capacity = _get_background_capacity()
    try:
        async with bg_lock:
            while len(_active_background_tasks) < capacity:
                try:
                    task = await _claim_next_runtime_task(store, capacity)
                except RuntimeError as exc:
                    logger.debug("Background task drain skipped during shutdown: %s", exc)
                    break
                if task is None:
                    break
                task_id = str(task["task_id"])
                _active_background_tasks.add(task_id)
                handle = asyncio.create_task(_run_background_task(task))
                _active_task_handles[task_id] = handle
    finally:
        _drain_task = None


async def _lease_heartbeat_loop() -> None:
    """Refresh local task leases and recover stale orphaned tasks."""
    store = get_task_store()
    try:
        while True:
            await asyncio.sleep(_TASK_HEARTBEAT_INTERVAL_SECONDS)
            task_ids = list(_active_background_tasks)
            for task_id in task_ids:
                try:
                    refreshed = await store.heartbeat_task(
                        task_id,
                        worker_id=_ensure_worker_id(),
                    )
                except RuntimeError as exc:
                    logger.debug("Background lease heartbeat skipped during shutdown: %s", exc)
                    return
                if refreshed is None:
                    async with _get_bg_lock():
                        _active_background_tasks.discard(task_id)
                    _active_task_handles.pop(task_id, None)
                    _task_stop_reasons.pop(task_id, None)
                    continue
                if refreshed.get("cancel_requested"):
                    _cancel_active_task(task_id, "cancelled")
                    continue
                timeout_seconds = int(refreshed.get("timeout_seconds") or 0)
                started_at = str(refreshed.get("started_at") or "")
                if (
                    timeout_seconds > 0
                    and started_at
                    and _seconds_since(started_at) >= timeout_seconds
                ):
                    if _task_stop_reasons.get(task_id) != "timeout":
                        await _log_runtime_watchdog(
                            task_id,
                            event="timeout_cancelled",
                            input_summary=(
                                f"worker={_ensure_worker_id()} "
                                f"timeout_seconds={timeout_seconds}"
                            ),
                            output_summary=(
                                "Cancelled running task after timeout budget "
                                "was exceeded."
                            ),
                            tool_name=str(refreshed.get("source") or "spawn_task"),
                        )
                    _cancel_active_task(task_id, "timeout")
            recovered: list[dict[str, Any]] = []
            try:
                for source in _RUNTIME_TASK_SOURCES:
                    recovered.extend(
                        await store.recover_orphaned_tasks(
                            lease_timeout_seconds=_TASK_LEASE_TIMEOUT_SECONDS,
                            source=source,
                            exclude_worker_id=_ensure_worker_id(),
                        )
                    )
            except RuntimeError as exc:
                logger.debug("Background orphan recovery skipped during shutdown: %s", exc)
                return
            if recovered:
                logger.info(
                    "Recovered %s orphaned background task(s) for worker %s",
                    len(recovered),
                    _ensure_worker_id(),
                )
                await _log_recovered_orphan_events(recovered)
                _schedule_queue_drain()
            try:
                metrics = await store.get_queue_metrics(
                    starvation_threshold_seconds=_get_starvation_threshold_seconds(),
                    lease_timeout_seconds=_TASK_LEASE_TIMEOUT_SECONDS,
                    stall_threshold_seconds=_get_runtime_stall_threshold_seconds(),
                )
            except RuntimeError as exc:
                logger.debug("Background queue metrics skipped during shutdown: %s", exc)
                return
            await _maybe_send_runtime_alert(metrics)
            await _maybe_send_schedule_health_alerts()
            if (
                metrics["ready_backlog"] > 0
                and len(_active_background_tasks) < _get_background_capacity()
                and metrics["running_tasks"] < _get_background_capacity()
            ):
                _schedule_queue_drain()
    except asyncio.CancelledError:
        raise


async def _run_background_task(task: dict[str, Any]) -> None:
    """Execute one claimed background task."""
    store = get_task_store()
    task_id = str(task["task_id"])
    source = str(task.get("source") or "spawn_task")
    gateway = None
    started = asyncio.get_running_loop().time()
    try:
        from nanoclaw.channels.gateway import get_gateway
        from nanoclaw.core.agent import get_agent

        gateway = get_gateway()
        completed_task = task
        if source == _HEARTBEAT_TASK_SOURCE:
            result = await _run_heartbeat_task(store, task)
        elif source == _CRON_TASK_SOURCE:
            result = await _run_cron_task(store, task)
        elif source == _CRON_DELIVERY_TASK_SOURCE:
            result = await _run_cron_delivery_retry_task(store, task)
        elif source == _ROLE_TASK_SOURCE:
            result = await _run_workflow_role_task(store, task)
        else:
            agent = get_agent()
            result = await _run_agent_step(agent, store, task)
        if source == _ROLE_TASK_SOURCE:
            completed_task = await store.transition_task(task_id, "succeeded")
            rearmed_task = await _maybe_rearm_task_after_running_recovery_refresh(
                store,
                completed_task,
            )
            if rearmed_task:
                _schedule_queue_drain()
                await _log_task_run_trace(
                    task,
                    status="rearmed",
                    execution_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                    final_output_summary=result,
                )
                return
        if source in {"spawn_task", _HEARTBEAT_TASK_SOURCE, _CRON_TASK_SOURCE, _ROLE_TASK_SOURCE}:
            await _enqueue_role_runtime_bridge_tasks(store, completed_task)
        if gateway and source == "spawn_task":
            await _send_result_notification(gateway, store, task, result)
        if source != _ROLE_TASK_SOURCE:
            completed_task = await store.transition_task(task_id, "succeeded")
        await _log_task_run_trace(
            completed_task,
            status="success",
            execution_ms=int((asyncio.get_running_loop().time() - started) * 1000),
            final_output_summary=result,
        )
    except RoleRecoveryRequested as exc:
        logger.info(
            "Runtime role task `%s` requested recovery via `%s`",
            task_id,
            str(exc.recovery_task.get("task_id") or ""),
        )
        await store.transition_task(task_id, "succeeded")
        await _log_task_run_trace(
            task,
            status="recovered",
            execution_ms=int((asyncio.get_running_loop().time() - started) * 1000),
            failure_reason=str(exc),
            final_output_summary=(
                str(exc.result.get("result_text") or "")[:160]
                or str(exc.recovery_task.get("task_id") or "")[:160]
            ),
        )
    except DeferredBackgroundTask as exc:
        logger.info("Deferred background task `%s`: %s", task_id, exc)
        await _log_task_run_trace(
            task,
            status="deferred",
            execution_ms=int((asyncio.get_running_loop().time() - started) * 1000),
            failure_reason=str(exc),
        )
    except asyncio.CancelledError:
        current = await store.get_task(task_id)
        if current is not None and str(current.get("status") or "") in {
            "succeeded",
            "failed",
            "cancelled",
        }:
            logger.debug(
                "Ignoring cancellation for terminal task `%s` with status `%s`",
                task_id,
                current.get("status"),
            )
            raise
        reason = _task_stop_reasons.pop(task_id, "")
        if reason == "cancelled":
            await store.transition_task(task_id, "cancelled")
            await _log_task_run_trace(
                task,
                status="cancelled",
                execution_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                failure_reason="cancelled",
            )
            if gateway and source == "spawn_task":
                await _send_cancelled_notification(gateway, store, task)
        elif reason == "shutdown":
            await store.transition_task(task_id, "pending", last_error="worker shutdown")
            logger.info("Requeued background task `%s` after worker shutdown", task_id)
            await _log_task_run_trace(
                task,
                status="requeued",
                execution_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                failure_reason="worker shutdown",
            )
        else:
            error_text = "task timed out" if reason == "timeout" else "worker cancelled"
            updated = await store.fail_task_attempt(task_id, last_error=error_text)
            if updated["status"] == "pending":
                logger.info(
                    "Retry scheduled for background task `%s` in %ss (%s/%s)",
                    task_id,
                    updated.get("retry_backoff_seconds", 0),
                    updated.get("attempt_count", 0),
                    updated.get("max_attempts", 1),
                )
                await _log_task_run_trace(
                    task,
                    status="retry",
                    execution_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                    failure_reason=error_text,
                )
            elif gateway and source == "spawn_task":
                await _log_task_run_trace(
                    task,
                    status="failed",
                    execution_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                    failure_reason=error_text,
                )
                await _send_failure_notification(gateway, store, task, error_text)
            else:
                await _log_task_run_trace(
                    task,
                    status="failed",
                    execution_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                    failure_reason=error_text,
                )
    except Exception as exc:
        logger.error("Background task `%s` failed: %s", task_id, exc)
        updated: dict[str, Any] | None = None
        try:
            updated = await store.fail_task_attempt(task_id, last_error=str(exc))
        except Exception as store_exc:
            logger.error("Failed to persist task failure for `%s`: %s", task_id, store_exc)
        if updated is not None and updated["status"] == "pending":
            logger.info(
                "Retry scheduled for background task `%s` in %ss (%s/%s)",
                task_id,
                updated.get("retry_backoff_seconds", 0),
                updated.get("attempt_count", 0),
                updated.get("max_attempts", 1),
            )
            await _log_task_run_trace(
                task,
                status="retry",
                execution_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                failure_reason=str(exc),
            )
        if gateway and source == "spawn_task":
            try:
                if updated is None or updated["status"] != "pending":
                    await _log_task_run_trace(
                        task,
                        status="failed",
                        execution_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                        failure_reason=str(exc),
                    )
                    await _send_failure_notification(gateway, store, task, str(exc))
            except Exception as send_exc:
                logger.error("Failed to send task failure notification: %s", send_exc)
        elif updated is None or updated["status"] != "pending":
            await _log_task_run_trace(
                task,
                status="failed",
                execution_ms=int((asyncio.get_running_loop().time() - started) * 1000),
                failure_reason=str(exc),
            )
    finally:
        async with _get_bg_lock():
            _active_background_tasks.discard(task_id)
        _active_task_handles.pop(task_id, None)
        _task_stop_reasons.pop(task_id, None)
        _schedule_queue_drain()


async def start_background_runtime() -> None:
    """Start background queue support and recover stale leased tasks."""
    global _runtime_stopping
    _runtime_stopping = False
    worker_id = _ensure_worker_id()
    recovered: list[dict[str, Any]] = []
    for source in _RUNTIME_TASK_SOURCES:
        recovered.extend(
            await get_task_store().recover_orphaned_tasks(
                lease_timeout_seconds=_TASK_LEASE_TIMEOUT_SECONDS,
                source=source,
                exclude_worker_id=worker_id,
            )
        )
    if recovered:
        logger.info(
            "Recovered %s orphaned background task(s) for worker %s",
            len(recovered),
            worker_id,
        )
        await _log_recovered_orphan_events(recovered)
    _ensure_heartbeat_loop()
    await _drain_pending_tasks()


async def stop_background_runtime() -> None:
    """Stop background runtime helper tasks."""
    global _drain_task, _heartbeat_task, _worker_id, _runtime_stopping
    _runtime_stopping = True
    active_handles = list(_active_task_handles.values())
    for task_id in list(_active_task_handles):
        _cancel_active_task(task_id, "shutdown")
    for handle in active_handles:
        if handle.done():
            continue
        handle_loop = handle.get_loop()
        if handle_loop.is_closed():
            continue
        with contextlib.suppress(asyncio.CancelledError):
            await handle
    for task in (_drain_task, _heartbeat_task):
        if task is None or task.done():
            continue
        task_loop = task.get_loop()
        if task_loop.is_closed():
            continue
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    _drain_task = None
    _heartbeat_task = None
    _active_background_tasks.clear()
    _active_task_handles.clear()
    _task_stop_reasons.clear()
    _runtime_alert_cache.clear()
    _worker_id = ""
    _runtime_stopping = False


@tool(
    name="spawn_task",
    description=(
        "Start a long-running task in the background. "
        "Use for research, analysis, or any task taking more than 30 seconds. "
        "The result will be sent to the user when complete."
    ),
    parameters={
        "task_description": {
            "type": "string",
            "description": "Detailed description of what to accomplish",
        },
        "priority": {
            "type": "integer",
            "description": "Task priority. Higher values run first. Default: 100.",
        },
        "timeout_seconds": {
            "type": "integer",
            "description": "Maximum runtime before failure. Use 0 to disable timeout.",
        },
        "max_attempts": {
            "type": "integer",
            "description": "Maximum total attempts before terminal failure. Default: 2.",
        },
        "retry_backoff_seconds": {
            "type": "integer",
            "description": "Delay before retrying a failed attempt. Default: 30 seconds.",
        },
        "rate_limit_key": {
            "type": "string",
            "description": (
                "Optional shared bucket name for provider/tool rate limiting. "
                "Tasks using the same key share one rate-limit window."
            ),
        },
        "rate_limit_window_seconds": {
            "type": "integer",
            "description": (
                "Optional rate-limit window in seconds for the shared bucket. "
                "Use with rate_limit_key and rate_limit_max_claims."
            ),
        },
        "rate_limit_max_claims": {
            "type": "integer",
            "description": (
                "Optional max claims allowed within the rate-limit window "
                "for the shared bucket."
            ),
        },
        "idempotency_key": {
            "type": "string",
            "description": (
                "Optional dedupe key. Reusing the same key returns the existing "
                "pending/running/succeeded task instead of queueing a duplicate."
            ),
        },
    },
    required=["task_description"],
    catalog_summary=(
        "Create a background task for long-running work with retries and "
        "optional shared rate limits."
    ),
    catalog_entry_points=["research in background", "run this later"],
    risk_level="medium",
)
async def spawn_task(
    task_description: str,
    priority: int = 100,
    timeout_seconds: int = 1800,
    max_attempts: int = 2,
    retry_backoff_seconds: int = 30,
    rate_limit_key: str = "",
    rate_limit_window_seconds: int = 0,
    rate_limit_max_claims: int = 0,
    idempotency_key: str = "",
) -> str:
    """Persist and enqueue a background sub-agent task."""
    runtime = get_tool_runtime_context()
    store = get_task_store()
    task = await store.create_task(
        task_description,
        task_type="background",
        payload={
            "task_description": task_description,
            "parent_session_id": runtime.session_id,
            "workflow_identity": runtime.workflow_identity,
        },
        source="spawn_task",
        session_id=runtime.session_id,
        priority=priority,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        retry_backoff_seconds=retry_backoff_seconds,
        rate_limit_key=rate_limit_key,
        rate_limit_window_seconds=rate_limit_window_seconds,
        rate_limit_max_claims=rate_limit_max_claims,
        idempotency_key=idempotency_key,
    )
    if task.get("reused_existing"):
        return (
            f"Task already exists as `{task['task_id']}` "
            f"with status `{task['status']}`. Reusing that task."
        )
    _schedule_queue_drain()
    return (
        f"Task queued in background as `{task['task_id']}`. "
        "I'll message you when it's done."
    )
