"""Lightweight role-plan, handoff, and shared-evidence helpers."""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field


_URL_RE = re.compile(r"https?://[^\s<>\"]+")
_STRUCTURED_WORKFLOWS = {
    "grounded_current_info",
    "feishu_paper_template",
    "wechat_article_flow",
    "scheduled_job_flow",
    "heartbeat_checklist",
}
_EVIDENCE_TOOLS = {
    "web_search",
    "web_fetch",
    "paper_search",
    "daily_digest",
    "hotspot_brief",
    "wechat_article_assist",
}
_DEFAULT_ROLE_RETRY_BUDGETS = {
    "executor": 2,
    "critic": 1,
    "summarizer": 1,
}
_DEFAULT_ROLE_TURN_BUDGETS = {
    "planner": 1,
    "router": 1,
    "executor": 2,
    "critic": 2,
    "summarizer": 2,
}


class RolePlanStep(BaseModel):
    """One lightweight workflow role step."""

    role: str
    summary: str


class SharedEvidenceItem(BaseModel):
    """One deduplicated evidence item collected from tool output."""

    evidence_id: str
    tool_name: str
    url: str
    title: str = ""
    snippet: str = ""


class RoleHandoff(BaseModel):
    """One lightweight contract between two workflow roles."""

    from_role: str
    to_role: str
    contract: dict[str, Any] = Field(default_factory=dict)


class RoleExecutionBrief(BaseModel):
    """One role-specific internal execution brief."""

    role: str
    stage: str
    checkpoint_id: str
    content: str
    contract: dict[str, Any] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)


class RoleRecoveryAction(BaseModel):
    """One lightweight role-level recovery instruction."""

    failed_role: str
    recovery_role: str
    stage: str
    reason: str
    resume_checkpoint_id: str
    recovery_task_key: str
    recovery_path: list[str] = Field(default_factory=list)
    scheduler_policy: str = "recovery_path_v2"
    content: str
    evidence_refs: list[str] = Field(default_factory=list)


class RoleTaskEnvelope(BaseModel):
    """One task-like envelope for a workflow role boundary."""

    role: str
    stage: str
    task_key: str
    status: str
    depends_on: list[str] = Field(default_factory=list)
    depends_on_any: list[str] = Field(default_factory=list)
    checkpoint_id: str = ""
    resume_checkpoint_id: str = ""
    retry_budget: int = 0
    turn_budget: int = 1
    evidence_refs: list[str] = Field(default_factory=list)


class RoleRuntimeTaskSpec(BaseModel):
    """One runtime-consumable role task spec for future task execution."""

    task_key: str
    role: str
    stage: str
    task_type: str
    source: str
    description: str
    priority: int
    timeout_seconds: int
    max_attempts: int
    idempotency_key: str
    payload: dict[str, Any] = Field(default_factory=dict)


class RoleCheckpointState(BaseModel):
    """One persisted-in-memory role checkpoint for the current workflow run."""

    checkpoint_id: str
    role: str
    stage: str
    message_count: int
    evidence_snapshot: dict[str, Any] = Field(default_factory=dict)


class RoleRestoreResult(BaseModel):
    """One compact result for a role checkpoint restore."""

    checkpoint_id: str
    restored_messages: int
    restored_evidence_count: int


class SharedEvidenceStore:
    """Collect and deduplicate evidence URLs across multiple tool calls."""

    def __init__(self) -> None:
        """Initialize one empty evidence store."""
        self._items: dict[str, SharedEvidenceItem] = {}

    @staticmethod
    def _clean_url(url: str) -> str:
        """Normalize one extracted URL token."""
        return url.rstrip(").,]>\"'")

    @staticmethod
    def _clean_line(text: str) -> str:
        """Remove lightweight markup noise from one line."""
        line = re.sub(r"<[^>]+>", "", str(text or ""))
        line = line.replace("**", "").replace("`", "").strip()
        return line[:160]

    def collect_tool_output(self, tool_name: str, output: str) -> dict[str, Any]:
        """Extract evidence URLs from one tool output and return one compact link result."""
        if tool_name not in _EVIDENCE_TOOLS:
            return {"added": 0, "evidence_ids": []}
        lines = [line.strip() for line in str(output or "").splitlines() if line.strip()]
        added = 0
        evidence_ids: list[str] = []
        for index, line in enumerate(lines):
            urls = [self._clean_url(item) for item in _URL_RE.findall(line)]
            if not urls:
                continue
            title = ""
            snippet = ""
            if index > 0:
                candidate = self._clean_line(lines[index - 1])
                if candidate and "http" not in candidate.lower():
                    title = candidate
            if index + 1 < len(lines):
                candidate = self._clean_line(lines[index + 1])
                if candidate and "http" not in candidate.lower():
                    snippet = candidate
            for url in urls:
                if url in self._items:
                    evidence_ids.append(self._items[url].evidence_id)
                    continue
                self._items[url] = SharedEvidenceItem(
                    evidence_id=f"ev_{len(self._items) + 1}",
                    tool_name=tool_name,
                    url=url,
                    title=title,
                    snippet=snippet,
                )
                evidence_ids.append(self._items[url].evidence_id)
                added += 1
        return {"added": added, "evidence_ids": sorted(set(evidence_ids))}

    def add_tool_output(self, tool_name: str, output: str) -> int:
        """Extract evidence URLs from one tool output and return the added count."""
        return int(self.collect_tool_output(tool_name, output).get("added") or 0)

    def snapshot(self, limit: int = 5) -> dict[str, Any]:
        """Return one compact snapshot suitable for workflow telemetry."""
        items = list(self._items.values())
        return {
            "count": len(items),
            "tools": sorted({item.tool_name for item in items}),
            "items": [item.model_dump() for item in items[:limit]],
        }

    def load_snapshot(self, snapshot: dict[str, Any]) -> None:
        """Replace current evidence state with one previous snapshot."""
        self._items = {}
        for item in list(snapshot.get("items") or []):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            evidence_id = str(item.get("evidence_id") or "").strip()
            if not url or not evidence_id:
                continue
            self._items[url] = SharedEvidenceItem(
                evidence_id=evidence_id,
                tool_name=str(item.get("tool_name") or ""),
                url=url,
                title=str(item.get("title") or ""),
                snippet=str(item.get("snippet") or ""),
            )


class RoleRuntimeState:
    """Track lightweight role checkpoints during one agent run."""

    def __init__(self) -> None:
        """Initialize one empty role runtime state."""
        self._checkpoints: dict[str, RoleCheckpointState] = {}

    @staticmethod
    def _copy_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
        """Return one detached copy of a compact evidence snapshot."""
        return {
            "count": int(snapshot.get("count") or 0),
            "tools": list(snapshot.get("tools") or []),
            "items": [
                dict(item)
                for item in list(snapshot.get("items") or [])
                if isinstance(item, dict)
            ],
        }

    def record_checkpoint(
        self,
        *,
        checkpoint_id: str,
        role: str,
        stage: str,
        messages: list[dict[str, Any]],
        evidence_snapshot: dict[str, Any],
    ) -> RoleCheckpointState:
        """Record one stable role checkpoint for later recovery."""
        checkpoint = RoleCheckpointState(
            checkpoint_id=checkpoint_id,
            role=role,
            stage=stage,
            message_count=len(messages),
            evidence_snapshot=self._copy_snapshot(evidence_snapshot),
        )
        self._checkpoints[checkpoint_id] = checkpoint
        return checkpoint

    def restore_checkpoint(
        self,
        checkpoint_id: str,
        *,
        messages: list[dict[str, Any]],
        shared_evidence: SharedEvidenceStore,
    ) -> RoleRestoreResult | None:
        """Restore messages and shared evidence back to one prior checkpoint."""
        checkpoint = self._checkpoints.get(checkpoint_id)
        if checkpoint is None:
            return None
        restored_messages = max(0, len(messages) - checkpoint.message_count)
        del messages[checkpoint.message_count :]
        shared_evidence.load_snapshot(checkpoint.evidence_snapshot)
        return RoleRestoreResult(
            checkpoint_id=checkpoint_id,
            restored_messages=restored_messages,
            restored_evidence_count=int(checkpoint.evidence_snapshot.get("count") or 0),
        )


def build_role_checkpoint_id(role: str, stage: str) -> str:
    """Return one stable checkpoint id for one role-stage pair."""
    return f"{role}@{stage}"


def _get_workflow_role_policy() -> Any | None:
    """Return the configured workflow role policy when available."""
    try:
        from nanoclaw.core.config import get_config

        return getattr(get_config().agent, "workflow_role_policy", None)
    except Exception:
        return None


def get_role_retry_budget(role: str) -> int:
    """Return the configured lightweight retry budget for one failed role."""
    normalized_role = str(role or "").strip()
    default = int(_DEFAULT_ROLE_RETRY_BUDGETS.get(normalized_role, 1))
    policy = _get_workflow_role_policy()
    if policy is None or not hasattr(policy, "get_retry_budget"):
        return default
    return int(policy.get_retry_budget(normalized_role, default))


def get_role_turn_budget(role: str) -> int:
    """Return the configured multi-turn budget for one runtime role task."""
    normalized_role = str(role or "").strip()
    default = int(_DEFAULT_ROLE_TURN_BUDGETS.get(normalized_role, 1))
    policy = _get_workflow_role_policy()
    if policy is None or not hasattr(policy, "get_turn_budget"):
        return default
    return int(policy.get_turn_budget(normalized_role, default))


def workflow_role_graph_fanout_enabled() -> bool:
    """Return whether the runtime should materialize all dependency-ready graph nodes."""
    policy = _get_workflow_role_policy()
    if policy is None:
        return True
    return bool(getattr(policy, "enable_graph_fanout", True))


def build_role_recovery_path(
    failed_role: str,
    recovery_role: str,
) -> list[str]:
    """Return one explicit recovery path for one failed workflow role."""
    key_map = {
        "planner": build_role_checkpoint_id("planner", "pre_llm"),
        "router": build_role_checkpoint_id("router", "pre_llm"),
        "executor": build_role_checkpoint_id("executor", "tool_phase"),
        "critic": build_role_checkpoint_id("critic", "post_tools"),
        "summarizer": build_role_checkpoint_id("summarizer", "post_tools"),
    }
    ordered_roles = ["planner", "router", "executor", "critic", "summarizer"]
    start_index = ordered_roles.index(recovery_role) if recovery_role in ordered_roles else 0
    path = [key_map[role] for role in ordered_roles[start_index:] if role in key_map]
    if failed_role == "executor" and recovery_role == "router":
        return path
    return path


def get_workflow_role_identity(workflow_name: str, role: str) -> dict[str, str]:
    """Return workflow-specific role identity metadata for runtime and replay."""
    generic = {
        "planner": ("planner", "plan checkpoints and execution order"),
        "router": ("router", "choose workflow path and evidence/tools"),
        "executor": ("executor", "run tools and gather evidence"),
        "critic": ("critic", "review evidence quality and gaps"),
        "summarizer": ("summarizer", "prepare the final user-facing output"),
    }
    article = {
        "planner": ("planner", "define angle, reader, and article structure"),
        "router": ("researcher", "collect and route article evidence"),
        "executor": ("drafter", "assemble draft-ready article material"),
        "critic": ("critic", "fact-check claims and publish gates"),
        "summarizer": ("editor", "prepare the publish-ready article bundle"),
    }
    mapping = article if workflow_name == "wechat_article_flow" else generic
    role_label, role_focus = mapping.get(role, (role, ""))
    return {
        "role_label": role_label,
        "role_focus": role_focus,
    }


def build_role_plan(
    workflow_name: str,
    user_message: str,
    tool_names: list[str],
    needs_grounded: bool,
) -> list[RolePlanStep]:
    """Build the lightweight role plan for one workflow execution."""
    if workflow_name == "wechat_article_flow":
        return [
            RolePlanStep(
                role="planner",
                summary=(
                    "Define the article angle, target reader, headline direction, and "
                    "checkpoint-friendly section structure."
                ),
            ),
            RolePlanStep(
                role="router",
                summary=(
                    "Choose the evidence mix and stage path for article writing "
                    "(user links, RSS, papers, or article helper)."
                ),
            ),
            RolePlanStep(
                role="executor",
                summary=(
                    "Collect grounded evidence and assemble draft-ready article material "
                    "without duplicate fetching."
                ),
            ),
            RolePlanStep(
                role="critic",
                summary=(
                    "Verify claims, source reachability, and coverage gaps before the "
                    "article is allowed to ship."
                ),
            ),
            RolePlanStep(
                role="summarizer",
                summary=(
                    "Produce the publish-ready WeChat article package, including editing "
                    "notes and export guidance."
                ),
            ),
        ]
    structured = workflow_name in _STRUCTURED_WORKFLOWS or needs_grounded
    planner_summary = (
        "Break the request into evidence-friendly steps and keep checkpoints stable."
        if structured or len(user_message) > 120
        else "Keep the plan short and avoid unnecessary tool churn."
    )
    router_summary = (
        "Choose the workflow, provider, and tool path that best fits the request."
    )
    if tool_names:
        executor_summary = (
            "Execute the selected tools and collect shared evidence without duplicate fetches."
        )
    else:
        executor_summary = "No tool execution was needed for this run."
    critic_summary = (
        "Review evidence coverage, failure reasons, and fallback quality before answering."
        if structured or tool_names
        else "Review the draft response for obvious gaps before answering."
    )
    summarizer_summary = "Produce the final user-facing answer from the shared evidence."
    return [
        RolePlanStep(role="planner", summary=planner_summary),
        RolePlanStep(role="router", summary=router_summary),
        RolePlanStep(role="executor", summary=executor_summary),
        RolePlanStep(role="critic", summary=critic_summary),
        RolePlanStep(role="summarizer", summary=summarizer_summary),
    ]


def build_role_task_envelopes(
    *,
    workflow_name: str,
    tool_names: list[str],
    needs_grounded: bool,
    evidence_snapshot: dict[str, Any],
    run_status: str,
) -> list[RoleTaskEnvelope]:
    """Build one compact task-like role boundary list for replay and future runtime use."""
    del workflow_name
    evidence_ids = [
        item.get("evidence_id")
        for item in list(evidence_snapshot.get("items") or [])
        if isinstance(item, dict) and item.get("evidence_id")
    ]
    executor_status = "attached" if tool_names else "skipped"
    critic_status = "warning" if needs_grounded and tool_names and not evidence_ids else "attached"
    summarizer_status = "blocked" if run_status not in {"success", "degraded"} else "attached"
    return [
        RoleTaskEnvelope(
            role="planner",
            stage="pre_llm",
            task_key="planner@pre_llm",
            status="attached",
            checkpoint_id="planner@pre_llm",
            turn_budget=get_role_turn_budget("planner"),
        ),
        RoleTaskEnvelope(
            role="router",
            stage="pre_llm",
            task_key="router@pre_llm",
            status="attached",
            depends_on=["planner@pre_llm"],
            checkpoint_id="router@pre_llm",
            turn_budget=get_role_turn_budget("router"),
        ),
        RoleTaskEnvelope(
            role="executor",
            stage="tool_phase",
            task_key="executor@tool_phase",
            status=executor_status,
            depends_on=["router@pre_llm"],
            resume_checkpoint_id="router@pre_llm",
            retry_budget=get_role_retry_budget("executor"),
            turn_budget=get_role_turn_budget("executor"),
            evidence_refs=evidence_ids[:3],
        ),
        RoleTaskEnvelope(
            role="critic",
            stage="post_tools",
            task_key="critic@post_tools",
            status=critic_status,
            depends_on=["executor@tool_phase"],
            checkpoint_id="critic@post_tools",
            resume_checkpoint_id="router@pre_llm",
            retry_budget=get_role_retry_budget("critic"),
            turn_budget=get_role_turn_budget("critic"),
            evidence_refs=evidence_ids[:3],
        ),
        RoleTaskEnvelope(
            role="summarizer",
            stage="post_tools",
            task_key="summarizer@post_tools",
            status=summarizer_status,
            depends_on=["critic@post_tools"],
            checkpoint_id="summarizer@post_tools",
            resume_checkpoint_id="router@pre_llm",
            retry_budget=get_role_retry_budget("summarizer"),
            turn_budget=get_role_turn_budget("summarizer"),
            evidence_refs=evidence_ids[:3],
        ),
    ]


def build_role_runtime_task_specs(
    *,
    session_id: str,
    workflow_name: str,
    workflow_identity: str = "",
    user_message: str,
    role_tasks: list[RoleTaskEnvelope],
    tool_names: list[str],
    needs_grounded: bool,
    evidence_snapshot: dict[str, Any],
    failure_reason: str = "",
    parent_task_id: str = "",
) -> list[RoleRuntimeTaskSpec]:
    """Build runtime-task specs from role envelopes without enqueueing them yet."""
    priority_map = {
        "planner": 820,
        "router": 800,
        "executor": 760,
        "critic": 720,
        "summarizer": 680,
    }
    timeout_map = {
        "planner": 180,
        "router": 180,
        "executor": 600,
        "critic": 240,
        "summarizer": 180,
    }
    items = list(evidence_snapshot.get("items") or [])
    role_plan = build_role_plan(workflow_name, user_message, tool_names, needs_grounded)
    plan_map = {item.role: item.summary for item in role_plan}
    execution_briefs = build_role_execution_briefs(
        workflow_name=workflow_name,
        user_message=user_message,
        tool_names=tool_names,
        needs_grounded=needs_grounded,
        evidence_snapshot=evidence_snapshot,
        failure_reason=failure_reason,
        stage="pre_llm",
    )
    execution_briefs.extend(
        build_role_execution_briefs(
            workflow_name=workflow_name,
            user_message=user_message,
            tool_names=tool_names,
            needs_grounded=needs_grounded,
            evidence_snapshot=evidence_snapshot,
            failure_reason=failure_reason,
            stage="post_tools",
        )
    )
    brief_map = {(item.role, item.stage): item for item in execution_briefs}
    handoff_map = {
        item.from_role: dict(item.contract)
        for item in build_role_handoffs(
            workflow_name=workflow_name,
            tool_names=tool_names,
            needs_grounded=needs_grounded,
            evidence_snapshot=evidence_snapshot,
            failure_reason=failure_reason,
        )
    }
    specs: list[RoleRuntimeTaskSpec] = []
    for task in role_tasks:
        role_identity = get_workflow_role_identity(workflow_name, task.role)
        role_brief = brief_map.get((task.role, task.stage))
        execution_brief = ""
        if role_brief is not None:
            execution_brief = role_brief.content
        elif plan_map.get(task.role):
            execution_brief = (
                f"[Internal: {role_identity['role_label'].title()} phase. "
                f"{plan_map[task.role]}]"
            )
        payload = {
            "session_id": session_id,
            "parent_task_id": parent_task_id,
            "workflow_identity": workflow_identity,
            "workflow_name": workflow_name,
            "user_summary": user_message[:160],
            "tool_names": list(tool_names),
            "needs_grounded": bool(needs_grounded),
            "failure_reason": failure_reason[:160],
            "role": task.role,
            "role_label": role_identity["role_label"],
            "role_focus": role_identity["role_focus"],
            "role_stage_name": role_identity["role_label"],
            "role_tool_enabled": bool(
                workflow_name == "wechat_article_flow" and "wechat_article_assist" in tool_names
            ),
            "execution_brief": execution_brief,
            "handoff_contract": dict(
                role_brief.contract if role_brief is not None else handoff_map.get(task.role, {})
            ),
            "stage": task.stage,
            "task_key": task.task_key,
            "depends_on": list(task.depends_on),
            "depends_on_any": list(task.depends_on_any),
            "checkpoint_id": task.checkpoint_id,
            "resume_checkpoint_id": task.resume_checkpoint_id,
            "retry_budget": int(task.retry_budget),
            "turn_index": 1,
            "turn_budget": max(1, int(task.turn_budget or 1)),
            "turn_reason": "initial",
            "upstream_input_fingerprint": "",
            "turn_history": [],
            "evidence_refs": list(task.evidence_refs),
            "evidence_snapshot": {
                "count": int(evidence_snapshot.get("count") or 0),
                "items": [
                    dict(item)
                    for item in items
                    if isinstance(item, dict)
                    and item.get("evidence_id") in set(task.evidence_refs)
                ],
            },
        }
        specs.append(
            RoleRuntimeTaskSpec(
                task_key=task.task_key,
                role=task.role,
                stage=task.stage,
                task_type="workflow_role",
                source="workflow_role",
                description=f"{workflow_name}:{task.task_key}",
                priority=int(priority_map.get(task.role, 700)),
                timeout_seconds=int(timeout_map.get(task.role, 300)),
                max_attempts=max(1, int(task.retry_budget or 1)),
                idempotency_key=f"{session_id}:{workflow_name}:{task.task_key}"[:120],
                payload=payload,
            )
        )
    return specs


def build_role_handoffs(
    *,
    workflow_name: str,
    tool_names: list[str],
    needs_grounded: bool,
    evidence_snapshot: dict[str, Any],
    failure_reason: str,
) -> list[RoleHandoff]:
    """Build stable lightweight handoff contracts for replay and evaluation."""
    evidence_items = list(evidence_snapshot.get("items") or [])
    evidence_ids = [
        item.get("evidence_id")
        for item in evidence_items
        if isinstance(item, dict) and item.get("evidence_id")
    ]
    evidence_tools = list(evidence_snapshot.get("tools") or [])
    critic_verdict = "ok"
    if failure_reason:
        critic_verdict = "fallback_needed"
    elif needs_grounded and not evidence_ids:
        critic_verdict = "evidence_gap"
    elif evidence_ids:
        critic_verdict = "grounded"
    response_mode = "grounded_answer" if needs_grounded else "direct_answer"
    if workflow_name in _STRUCTURED_WORKFLOWS:
        response_mode = "structured_workflow"
    if workflow_name == "wechat_article_flow":
        return [
            RoleHandoff(
                from_role="planner",
                to_role="router",
                contract={
                    "workflow_name": workflow_name,
                    "article_mode": "wechat_publish",
                    "deliverables": ["angle", "outline", "headlines"],
                },
            ),
            RoleHandoff(
                from_role="router",
                to_role="executor",
                contract={
                    "tool_names": tool_names,
                    "provider_mode": "grounded_article",
                    "evidence_strategy": "user_rss_paper_merge",
                },
            ),
            RoleHandoff(
                from_role="executor",
                to_role="critic",
                contract={
                    "evidence_ids": evidence_ids,
                    "evidence_count": int(evidence_snapshot.get("count") or 0),
                    "evidence_tools": evidence_tools,
                    "publish_gate": "factcheck_required",
                },
            ),
            RoleHandoff(
                from_role="critic",
                to_role="summarizer",
                contract={
                    "verdict": critic_verdict,
                    "evidence_ids": evidence_ids[:3],
                    "failure_reason": failure_reason[:160],
                    "publish_mode": "wechat_article_bundle",
                },
            ),
        ]
    return [
        RoleHandoff(
            from_role="planner",
            to_role="router",
            contract={
                "workflow_name": workflow_name,
                "needs_grounded": needs_grounded,
                "response_mode": response_mode,
            },
        ),
        RoleHandoff(
            from_role="router",
            to_role="executor",
            contract={
                "tool_names": tool_names,
                "provider_mode": "grounded" if needs_grounded else "general",
                "evidence_strategy": "reuse_shared_evidence",
            },
        ),
        RoleHandoff(
            from_role="executor",
            to_role="critic",
            contract={
                "evidence_ids": evidence_ids,
                "evidence_count": int(evidence_snapshot.get("count") or 0),
                "evidence_tools": evidence_tools,
            },
        ),
        RoleHandoff(
            from_role="critic",
            to_role="summarizer",
            contract={
                "verdict": critic_verdict,
                "evidence_ids": evidence_ids[:3],
                "failure_reason": failure_reason[:160],
            },
        ),
    ]


def build_collaboration_events(
    *,
    workflow_name: str,
    user_message: str,
    tool_names: list[str],
    needs_grounded: bool,
    evidence_snapshot: dict[str, Any],
    run_status: str,
    failure_reason: str,
    final_response: str,
) -> list[dict[str, Any]]:
    """Build lightweight role and evidence events for workflow telemetry."""
    role_plan = build_role_plan(workflow_name, user_message, tool_names, needs_grounded)
    role_tasks = build_role_task_envelopes(
        workflow_name=workflow_name,
        tool_names=tool_names,
        needs_grounded=needs_grounded,
        evidence_snapshot=evidence_snapshot,
        run_status=run_status,
    )
    handoffs = build_role_handoffs(
        workflow_name=workflow_name,
        tool_names=tool_names,
        needs_grounded=needs_grounded,
        evidence_snapshot=evidence_snapshot,
        failure_reason=failure_reason,
    )
    handoff_map = {handoff.from_role: handoff for handoff in handoffs}
    events: list[dict[str, Any]] = []
    for step in role_plan:
        role_identity = get_workflow_role_identity(workflow_name, step.role)
        status = "success"
        if step.role == "executor" and not tool_names:
            status = "skipped"
        elif step.role == "critic" and needs_grounded and int(evidence_snapshot.get("count") or 0) == 0:
            status = "warning"
        elif step.role == "summarizer" and run_status not in {"success", "degraded"}:
            status = run_status
        events.append(
            {
                "type": "workflow_role",
                "role": step.role,
                "role_label": role_identity["role_label"],
                "status": status,
                "summary": step.summary,
            }
        )
        if step.role == "executor":
            events.append(
                {
                    "type": "shared_evidence",
                    "status": "captured" if evidence_snapshot.get("count") else "empty",
                    "count": int(evidence_snapshot.get("count") or 0),
                    "tools": list(evidence_snapshot.get("tools") or []),
                    "items": list(evidence_snapshot.get("items") or []),
                }
            )
        handoff = handoff_map.get(step.role)
        if handoff:
            events.append(
                {
                    "type": "workflow_handoff",
                    "from_role": handoff.from_role,
                    "to_role": handoff.to_role,
                    "contract": handoff.contract,
                }
            )
        if step.role == "critic" and failure_reason:
            events[-1]["failure_reason"] = failure_reason
        if step.role == "summarizer":
            events[-1]["response_chars"] = len(final_response or "")
    for task in role_tasks:
        events.append(
            {
                "type": "workflow_role_task",
                "role": task.role,
                "role_label": get_workflow_role_identity(workflow_name, task.role)["role_label"],
                "stage": task.stage,
                "task_key": task.task_key,
                "status": task.status,
                "depends_on": list(task.depends_on),
                "depends_on_any": list(task.depends_on_any),
                "checkpoint_id": task.checkpoint_id,
                "resume_checkpoint_id": task.resume_checkpoint_id,
                "retry_budget": int(task.retry_budget),
                "turn_budget": int(task.turn_budget),
                "evidence_refs": list(task.evidence_refs),
            }
        )
    return events


def build_role_runtime_bridge_events(
    *,
    session_id: str,
    workflow_name: str,
    workflow_identity: str = "",
    user_message: str,
    role_tasks: list[RoleTaskEnvelope],
    tool_names: list[str],
    needs_grounded: bool,
    evidence_snapshot: dict[str, Any],
    failure_reason: str = "",
    parent_task_id: str = "",
) -> list[dict[str, Any]]:
    """Build one compact runtime-bridge view for future role task execution."""
    specs = build_role_runtime_task_specs(
        session_id=session_id,
        workflow_name=workflow_name,
        workflow_identity=workflow_identity,
        user_message=user_message,
        role_tasks=role_tasks,
        tool_names=tool_names,
        needs_grounded=needs_grounded,
        evidence_snapshot=evidence_snapshot,
        failure_reason=failure_reason,
        parent_task_id=parent_task_id,
    )
    return [
        {
            "type": "workflow_role_task_bridge",
            "task_key": spec.task_key,
            "role": spec.role,
            "role_label": str(spec.payload.get("role_label") or spec.role),
            "stage": spec.stage,
            "task_type": spec.task_type,
            "source": spec.source,
            "description": spec.description,
            "priority": int(spec.priority),
            "timeout_seconds": int(spec.timeout_seconds),
            "max_attempts": int(spec.max_attempts),
            "idempotency_key": spec.idempotency_key,
            "payload": dict(spec.payload),
            "depends_on_any": list(spec.payload.get("depends_on_any") or []),
            "evidence_refs": list(spec.payload.get("evidence_refs") or []),
        }
        for spec in specs
    ]


def build_shared_evidence_brief(evidence_snapshot: dict[str, Any], limit: int = 3) -> str:
    """Build one compact internal evidence brief for later execution turns."""
    items = list(evidence_snapshot.get("items") or [])
    if not items:
        return ""
    lines = [
        "[Internal: Shared evidence is available. Reuse these references before "
        "calling more evidence tools. Only fetch again if coverage is still missing.]"
    ]
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        evidence_id = item.get("evidence_id", "")
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        parts = [part for part in [evidence_id, title, url, snippet] if part]
        if parts:
            lines.append("- " + " | ".join(parts))
    return "\n".join(lines)


def _normalize_dependency_outputs(dependency_outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return one compact, stable dependency-output view."""
    items: list[dict[str, Any]] = []
    for item in dependency_outputs:
        if not isinstance(item, dict):
            continue
        items.append(
            {
                "task_key": str(item.get("task_key") or ""),
                "role": str(item.get("role") or ""),
                "role_label": str(item.get("role_label") or item.get("role") or ""),
                "status": str(item.get("status") or ""),
                "action": str(item.get("action") or ""),
                "artifact_preview": str(item.get("artifact_preview") or ""),
                "tool_handler_name": str(item.get("tool_handler_name") or ""),
                "tool_handler_stage": str(item.get("tool_handler_stage") or ""),
                "tool_handler_status": str(item.get("tool_handler_status") or ""),
                "tool_handler_output_preview": str(item.get("tool_handler_output_preview") or ""),
                "result_text": str(item.get("result_text") or "")[:160],
                "attempt_number": int(item.get("attempt_number") or 0),
                "turn_index": int(item.get("turn_index") or 0),
                "evidence_refs": [
                    str(ref).strip()
                    for ref in list(item.get("evidence_refs") or [])
                    if str(ref).strip()
                ],
            }
        )
    return items


def build_role_runtime_execution_result(
    *,
    payload: dict[str, Any],
    dependency_outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build one deterministic execution result for a runtime role task."""
    workflow_name = str(payload.get("workflow_name") or "").strip()
    user_summary = str(payload.get("user_summary") or "").strip()
    role = str(payload.get("role") or "").strip()
    role_label = str(payload.get("role_label") or role).strip()
    role_focus = str(payload.get("role_focus") or "").strip()
    stage = str(payload.get("stage") or "").strip()
    task_key = str(payload.get("task_key") or "").strip()
    tool_names = [str(item).strip() for item in list(payload.get("tool_names") or []) if str(item).strip()]
    needs_grounded = bool(payload.get("needs_grounded"))
    failure_reason = str(payload.get("failure_reason") or "").strip()
    depends_on = [str(item).strip() for item in list(payload.get("depends_on") or []) if str(item).strip()]
    evidence_snapshot = dict(payload.get("evidence_snapshot") or {})
    evidence_count = int(evidence_snapshot.get("count") or 0)
    evidence_refs = [str(item).strip() for item in list(payload.get("evidence_refs") or []) if str(item).strip()]
    payload_execution_brief = str(payload.get("execution_brief") or "").strip()
    payload_handoff_contract = dict(payload.get("handoff_contract") or {})
    turn_index = max(1, int(payload.get("turn_index") or 1))
    turn_budget = max(1, int(payload.get("turn_budget") or 1))
    turn_reason = str(payload.get("turn_reason") or "initial").strip() or "initial"
    upstream_input_fingerprint = str(payload.get("upstream_input_fingerprint") or "").strip()
    depends_on_any = [
        str(item).strip() for item in list(payload.get("depends_on_any") or []) if str(item).strip()
    ]
    turn_history = [
        dict(item)
        for item in list(payload.get("turn_history") or [])
        if isinstance(item, dict)
    ][:3]
    execution_briefs = build_role_execution_briefs(
        workflow_name=workflow_name,
        user_message=user_summary,
        tool_names=tool_names,
        needs_grounded=needs_grounded,
        evidence_snapshot=evidence_snapshot,
        failure_reason=failure_reason,
        stage=stage,
    )
    role_brief = next(
        (item for item in execution_briefs if item.role == role and item.stage == stage),
        None,
    )
    dependency_view = _normalize_dependency_outputs(dependency_outputs)
    upstream_actions = [
        item["action"]
        for item in dependency_view
        if item.get("action")
    ]
    upstream_artifacts = [
        item.get("tool_handler_output_preview") or item["artifact_preview"]
        for item in dependency_view
        if item.get("tool_handler_output_preview") or item.get("artifact_preview")
    ]
    action = "role_runtime_executed"
    role_summary = f"{role or 'role'} runtime executed."
    handler_kind = "structured_snapshot"
    brief_content = ""
    artifact_preview = ""
    contract: dict[str, Any] = {}
    checkpoint_id = str(payload.get("checkpoint_id") or "").strip()
    if payload_execution_brief:
        handler_kind = "execution_brief"
        brief_content = payload_execution_brief
    elif role_brief is not None:
        handler_kind = "execution_brief"
        brief_content = role_brief.content
    if payload_handoff_contract:
        contract = payload_handoff_contract
    elif role_brief is not None:
        contract = dict(role_brief.contract)
    if not checkpoint_id and role_brief is not None:
        checkpoint_id = role_brief.checkpoint_id
    if role == "planner":
        action = "stable_plan_ready"
        role_summary = "Planner kept a checkpoint-friendly execution outline."
    elif role == "router":
        route_mode = (
            "structured"
            if workflow_name in _STRUCTURED_WORKFLOWS
            else "grounded"
            if evidence_count > 0
            else "lean"
        )
        action = f"route_selected:{route_mode}"
        role_summary = f"Router selected a `{route_mode}` path for the workflow."
    elif role == "executor":
        action = "shared_evidence_reused" if evidence_count > 0 else "tool_phase_ready"
        role_summary = (
            "Executor prepared to reuse shared evidence."
            if evidence_count > 0
            else "Executor marked the tool phase ready without shared evidence."
        )
    elif role == "critic":
        verdict = (
            "grounded"
            if evidence_count > 0
            else "evidence_gap"
            if workflow_name in _STRUCTURED_WORKFLOWS
            else "light_review"
        )
        action = f"critic_verdict:{verdict}"
        role_summary = f"Critic produced a `{verdict}` verdict for downstream summarization."
    elif role == "summarizer":
        summary_mode = "evidence_backed" if evidence_count > 0 else "direct_summary"
        action = f"summary_mode:{summary_mode}"
        role_summary = f"Summarizer prepared a `{summary_mode}` answer boundary."
    if workflow_name == "wechat_article_flow":
        if role == "planner":
            action = "article_outline_ready"
            role_summary = "Planner prepared the article angle, headline path, and section plan."
            artifact_preview = (
                "Angle + structure ready: lead with the strongest signal, then unpack "
                "technical change, impact, and action items."
            )
        elif role == "router":
            route_mode = "article_helper" if "wechat_article_assist" in tool_names else "evidence_mix"
            action = f"article_route:{route_mode}"
            role_summary = f"Router selected the `{route_mode}` path for the article workflow."
            seed = upstream_artifacts[0] if upstream_artifacts else ""
            artifact_preview = (
                "Evidence route ready: merge user links, RSS/news signals, and paper evidence "
                "before drafting."
            )
            if seed:
                artifact_preview += f" Outline seed: {seed[:120]}"
        elif role == "executor":
            action = "article_material_ready" if evidence_count > 0 else "article_material_partial"
            role_summary = (
                "Executor assembled grounded article material from shared evidence."
                if evidence_count > 0
                else "Executor prepared a partial article material bundle with limited evidence."
            )
            artifact_preview = (
                f"Draft packet ready from {evidence_count} evidence item(s); write against "
                "verified snippets first."
            )
            if upstream_actions:
                artifact_preview += f" Routed via: {', '.join(upstream_actions[:2])}."
        elif role == "critic":
            verdict = "publish_ready" if evidence_count > 0 and not failure_reason else "needs_review"
            action = f"article_gate:{verdict}"
            role_summary = f"Critic marked the article bundle as `{verdict}`."
            artifact_preview = (
                "Fact-check gate complete: verify source reachability, conflict points, "
                "and over-claim risk before publish."
            )
            if upstream_artifacts:
                artifact_preview += f" Reviewed bundle: {upstream_artifacts[0][:120]}"
        elif role == "summarizer":
            action = "article_bundle_ready"
            role_summary = "Summarizer prepared the publish-ready article bundle."
            artifact_preview = (
                "Publish bundle ready: title candidates, polished draft, fact-check table, "
                "and export guidance."
            )
            if upstream_actions:
                artifact_preview += f" Inputs: {', '.join(upstream_actions[:3])}."
    elif role_focus:
        artifact_preview = role_focus
    result_text = f"{workflow_name}:{task_key} -> {action}" if workflow_name and task_key else action
    scheduler_action = "complete"
    if turn_reason == "upstream_changed":
        scheduler_action = "rearmed_upstream"
    elif turn_reason in {"recovery_reentry", "recovery_refresh"}:
        scheduler_action = "rearmed_recovery"
    return {
        "status": "executed",
        "scheduler_action": scheduler_action,
        "result_text": result_text,
        "workflow_name": workflow_name,
        "role": role,
        "role_label": role_label,
        "role_focus": role_focus,
        "stage": stage,
        "task_key": task_key,
        "action": action,
        "handler_kind": handler_kind,
        "role_summary": role_summary,
        "artifact_preview": artifact_preview,
        "brief_content": brief_content,
        "contract": contract,
        "checkpoint_id": checkpoint_id,
        "user_summary": user_summary[:160],
        "tool_names": tool_names,
        "needs_grounded": needs_grounded,
        "dependency_count": len(depends_on),
        "dependency_any_count": len(depends_on_any),
        "resolved_dependencies": [item["task_key"] for item in dependency_view if item.get("task_key")],
        "upstream_actions": upstream_actions[:5],
        "upstream_artifacts": upstream_artifacts[:3],
        "turn_index": turn_index,
        "turn_budget": turn_budget,
        "turn_reason": turn_reason,
        "turn_history": turn_history,
        "turn_history_count": len(turn_history),
        "upstream_input_fingerprint": upstream_input_fingerprint,
        "dependency_outputs": dependency_view,
        "evidence_count": evidence_count,
        "evidence_refs": evidence_refs[:5],
    }


def build_persistent_resume_brief(resume_state: dict[str, Any], limit: int = 3) -> str:
    """Build one compact internal brief from a persisted role checkpoint."""
    checkpoint_id = str(resume_state.get("resume_checkpoint_id") or "").strip()
    source_run_id = int(resume_state.get("source_workflow_run_id") or 0)
    workflow_status = str(resume_state.get("workflow_status") or "").strip()
    failure_reason = str(resume_state.get("failure_reason") or "").strip()
    snapshot = dict(resume_state.get("evidence_snapshot") or {})
    lines = [
        "[Internal: Resume from persisted role checkpoint. "
        f"Source run: {source_run_id or '-'} "
        f"checkpoint: {checkpoint_id or '-'} "
        f"status: {workflow_status or '-'} "
        f"failure: {failure_reason[:120] or '-'} "
        "Reuse this persisted evidence before fetching again.]"
    ]
    items = list(snapshot.get("items") or [])
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        evidence_id = str(item.get("evidence_id") or "").strip()
        title = str(item.get("title") or "").strip()
        url = str(item.get("url") or "").strip()
        snippet = str(item.get("snippet") or "").strip()
        parts = [part for part in [evidence_id, title, url, snippet] if part]
        if parts:
            lines.append("- " + " | ".join(parts))
    return "\n".join(lines)


def build_role_execution_briefs(
    *,
    workflow_name: str,
    user_message: str,
    tool_names: list[str],
    needs_grounded: bool,
    evidence_snapshot: dict[str, Any],
    failure_reason: str,
    stage: str,
) -> list[RoleExecutionBrief]:
    """Build internal role-execution briefs for one execution stage."""
    role_plan = build_role_plan(workflow_name, user_message, tool_names, needs_grounded)
    plan_map = {item.role: item.summary for item in role_plan}
    handoffs = build_role_handoffs(
        workflow_name=workflow_name,
        tool_names=tool_names,
        needs_grounded=needs_grounded,
        evidence_snapshot=evidence_snapshot,
        failure_reason=failure_reason,
    )
    handoff_map = {item.to_role: item.contract for item in handoffs}
    evidence_ids = [
        item.get("evidence_id")
        for item in list(evidence_snapshot.get("items") or [])
        if isinstance(item, dict) and item.get("evidence_id")
    ]
    if stage == "pre_llm":
        planner_label = get_workflow_role_identity(workflow_name, "planner")["role_label"].title()
        router_label = get_workflow_role_identity(workflow_name, "router")["role_label"].title()
        return [
            RoleExecutionBrief(
                role="planner",
                stage=stage,
                checkpoint_id=build_role_checkpoint_id("planner", stage),
                content=(
                    f"[Internal: {planner_label} phase. "
                    f"{plan_map['planner']} User request: {user_message[:160]}]"
                ),
                contract=handoff_map.get("router", {}),
            ),
            RoleExecutionBrief(
                role="router",
                stage=stage,
                checkpoint_id=build_role_checkpoint_id("router", stage),
                content=(
                    f"[Internal: {router_label} phase. "
                    f"{plan_map['router']} Selected tools should stay lean.]"
                ),
                contract=handoff_map.get("executor", {}),
            ),
        ]
    if stage == "post_tools":
        critic_label = get_workflow_role_identity(workflow_name, "critic")["role_label"].title()
        summarizer_label = get_workflow_role_identity(
            workflow_name,
            "summarizer",
        )["role_label"].title()
        return [
            RoleExecutionBrief(
                role="critic",
                stage=stage,
                checkpoint_id=build_role_checkpoint_id("critic", stage),
                content=(
                    f"[Internal: {critic_label} phase. "
                    f"{plan_map['critic']} Evidence refs: {', '.join(evidence_ids) or 'none'}]"
                ),
                contract=handoff_map.get("summarizer", {}),
                evidence_refs=evidence_ids[:3],
            ),
            RoleExecutionBrief(
                role="summarizer",
                stage=stage,
                checkpoint_id=build_role_checkpoint_id("summarizer", stage),
                content=(
                    f"[Internal: {summarizer_label} phase. "
                    f"{plan_map['summarizer']} Use evidence refs first and avoid duplicate fetches.]"
                ),
                contract=handoff_map.get("summarizer", {}),
                evidence_refs=evidence_ids[:3],
            ),
        ]
    return []


def build_role_recovery_action(
    *,
    workflow_name: str,
    tool_names: list[str],
    needs_grounded: bool,
    evidence_snapshot: dict[str, Any],
    failure_reason: str,
    stage: str,
) -> RoleRecoveryAction | None:
    """Build one minimal role-level recovery action when the current phase degrades."""
    evidence_ids = [
        item.get("evidence_id")
        for item in list(evidence_snapshot.get("items") or [])
        if isinstance(item, dict) and item.get("evidence_id")
    ]
    if stage == "post_tools" and failure_reason:
        return RoleRecoveryAction(
            failed_role="executor",
            recovery_role="router",
            stage=stage,
            reason=failure_reason[:160],
            resume_checkpoint_id=build_role_checkpoint_id("router", "pre_llm"),
            recovery_task_key=build_role_checkpoint_id("router", "pre_llm"),
            recovery_path=build_role_recovery_path("executor", "router"),
            content=(
                "[Internal: Role recovery. Executor failed. "
                "Router must choose a safer provider or query path and reuse any surviving "
                f"evidence refs: {', '.join(evidence_ids) or 'none'}.]"
            ),
            evidence_refs=evidence_ids[:3],
        )
    if stage == "post_tools" and needs_grounded and tool_names and not evidence_ids:
        return RoleRecoveryAction(
            failed_role="critic",
            recovery_role="executor",
            stage=stage,
            reason="evidence_gap",
            resume_checkpoint_id=build_role_checkpoint_id("router", "pre_llm"),
            recovery_task_key=build_role_checkpoint_id("executor", "tool_phase"),
            recovery_path=build_role_recovery_path("critic", "executor"),
            content=(
                "[Internal: Role recovery. Critic found no grounded evidence. "
                f"Executor must gather more grounded evidence for workflow `{workflow_name}` "
                "before summarizing.]"
            ),
            evidence_refs=[],
        )
    if stage == "pre_final" and needs_grounded:
        return RoleRecoveryAction(
            failed_role="summarizer",
            recovery_role="executor",
            stage=stage,
            reason="grounded_search_required",
            resume_checkpoint_id=build_role_checkpoint_id("router", "pre_llm"),
            recovery_task_key=build_role_checkpoint_id("executor", "tool_phase"),
            recovery_path=build_role_recovery_path("summarizer", "executor"),
            content=(
                "[Internal: Role recovery. Summarizer cannot answer yet. "
                "Executor must gather grounded evidence before the next response.]"
            ),
            evidence_refs=evidence_ids[:3],
        )
    return None
