"""Background task queue tests."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from nanoclaw.channels.gateway import set_gateway
from nanoclaw.core.agent import set_agent
from nanoclaw.runtime.tasks import TaskStore, set_task_store
from nanoclaw.security.audit import AuditLog
from nanoclaw.tools.runtime_context import (
    reset_tool_runtime_context,
    set_tool_runtime_context,
)
import nanoclaw.tools.spawn as spawn_module


class FakeAgent:
    """Agent stub used by background task tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self._release = asyncio.Event()

    async def run(self, user_message: str, session_id: str) -> str:
        """Record the background task and wait until the test releases it."""
        self.calls.append((user_message, session_id))
        await self._release.wait()
        return "background complete"


class FakeGateway:
    """Gateway stub used by background task tests."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_proactive(self, text: str, channel: str = "telegram") -> None:
        """Capture proactive notifications."""
        self.messages.append((channel, text))


class FakeRoleLLM:
    """LLM stub used by isolated role-runtime tests."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.calls: list[list[dict[str, Any]]] = []
        self.model = "fake-role-llm"

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: Any = None,
        model: str | None = None,
        extra_payload: dict[str, Any] | None = None,
    ) -> SimpleNamespace:
        """Record the isolated role turn and return one fixed JSON payload."""
        self.calls.append(list(messages))
        return SimpleNamespace(
            content=self.content,
            usage=SimpleNamespace(prompt_tokens=11, completion_tokens=17, total_tokens=28),
        )


class FailOnceGateway(FakeGateway):
    """Gateway stub that fails once before succeeding."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def send_proactive(self, text: str, channel: str = "telegram") -> None:
        """Fail the first delivery attempt to exercise checkpoint reuse."""
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary send failure")
        await super().send_proactive(text, channel=channel)


class _ScheduleGateway:
    """Gateway stub that captures schedule-health alerts."""

    def __init__(self, jobs: list[dict[str, object]]) -> None:
        """Initialize one fake scheduler and alert channels."""
        self.scheduler = _ScheduleScheduler(jobs)
        self.feishu = _ScheduleFeishuChannel()
        self.channels = {"feishu": self.feishu, "console": object()}
        self.messages: list[tuple[str, str]] = []

    async def send_proactive(self, text: str, channel: str = "console") -> None:
        """Capture non-targeted schedule alerts."""
        self.messages.append((channel, text))


class _ScheduleScheduler:
    """Scheduler stub returning fixed runtime-state jobs."""

    def __init__(self, jobs: list[dict[str, object]]) -> None:
        """Store the runtime-state schedule rows for one test."""
        self.jobs = jobs

    async def list_jobs_with_runtime_state(self) -> list[dict[str, object]]:
        """Return the configured schedule rows."""
        return list(self.jobs)


class _ScheduleFeishuChannel:
    """Small targeted-send stub for schedule alert tests."""

    def __init__(self) -> None:
        """Initialize captured targeted deliveries."""
        self.targeted_messages: list[tuple[str, str]] = []

    async def send_proactive_to(self, chat_id: str, text: str) -> bool:
        """Capture the targeted Feishu alert."""
        self.targeted_messages.append((chat_id, text))
        return True


async def _reset_spawn_runtime_state() -> None:
    """Reset module-level queue state between tests."""
    spawn_module._runtime_stopping = True
    for task_id, handle in list(spawn_module._active_task_handles.items()):
        if not handle.done():
            task_loop = handle.get_loop()
            if not task_loop.is_closed():
                spawn_module._cancel_active_task(task_id, "shutdown")
                with contextlib.suppress(asyncio.CancelledError):
                    await handle
    for task in (spawn_module._drain_task, spawn_module._heartbeat_task):
        if task is not None and not task.done():
            task_loop = task.get_loop()
            if not task_loop.is_closed():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
    spawn_module._active_background_tasks.clear()
    spawn_module._bg_lock = None
    spawn_module._bg_lock_loop = None
    spawn_module._drain_task = None
    spawn_module._heartbeat_task = None
    spawn_module._active_task_handles.clear()
    spawn_module._task_stop_reasons.clear()
    spawn_module._runtime_alert_cache.clear()
    spawn_module._worker_id = ""
    spawn_module._runtime_stopping = False
    spawn_module.set_role_runtime_llm(None)
    spawn_module._STARVATION_THRESHOLD_SECONDS = (
        spawn_module._DEFAULT_STARVATION_THRESHOLD_SECONDS
    )


async def _wait_for_status(
    store: TaskStore,
    expected: dict[str, str],
    *,
    timeout: float = 0.3,
) -> dict[str, str]:
    """Poll task statuses until all expected values match or timeout expires."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        rows = await store.list_tasks(limit=20)
        statuses = {item["description"]: item["status"] for item in rows}
        if all(statuses.get(name) == status for name, status in expected.items()):
            return statuses
        if asyncio.get_running_loop().time() >= deadline:
            return statuses
        await asyncio.sleep(0.01)


def _build_role_bridge_item(
    *,
    session_id: str,
    parent_task_id: str,
    task_key: str,
    role: str,
    stage: str,
    priority: int,
    timeout_seconds: int,
    max_attempts: int,
    workflow_name: str = "default_chat_loop",
    tool_names: list[str] | None = None,
    needs_grounded: bool = True,
    depends_on: list[str] | None = None,
    depends_on_any: list[str] | None = None,
    checkpoint_id: str = "",
    resume_checkpoint_id: str = "",
    workflow_identity: str = "",
) -> dict[str, Any]:
    """Build one minimal workflow-role bridge item for runtime tests."""
    tool_list = list(tool_names or ["web_search"])
    dependency_keys = list(depends_on or [])
    dependency_any_keys = list(depends_on_any or [])
    payload: dict[str, Any] = {
        "session_id": session_id,
        "parent_task_id": parent_task_id,
        "workflow_name": workflow_name,
        "tool_names": tool_list,
        "needs_grounded": needs_grounded,
        "role": role,
        "role_label": role,
        "stage": stage,
        "task_key": task_key,
        "depends_on": dependency_keys,
        "depends_on_any": dependency_any_keys,
        "retry_budget": max_attempts,
        "evidence_refs": [],
        "evidence_snapshot": {"count": 0, "items": []},
    }
    if checkpoint_id:
        payload["checkpoint_id"] = checkpoint_id
    if resume_checkpoint_id:
        payload["resume_checkpoint_id"] = resume_checkpoint_id
    if workflow_identity:
        payload["workflow_identity"] = workflow_identity
    return {
        "type": "workflow_role_task_bridge",
        "task_key": task_key,
        "role": role,
        "stage": stage,
        "task_type": "workflow_role",
        "source": "workflow_role",
        "description": f"{workflow_name}:{task_key}",
        "priority": priority,
        "timeout_seconds": timeout_seconds,
        "max_attempts": max_attempts,
        "idempotency_key": f"{session_id}:{workflow_name}:{task_key}",
        "payload": payload,
        "evidence_refs": [],
    }


async def _log_role_bridge_run(
    audit: AuditLog,
    *,
    session_id: str,
    call_chain: list[dict[str, Any]],
    workflow_name: str = "default_chat_loop",
    user_summary: str = "runtime recovery path",
) -> None:
    """Store one workflow run with bridge items for runtime enqueue tests."""
    await audit.log_workflow_run(
        session_id=session_id,
        workflow_name=workflow_name,
        workflow_tags=[workflow_name],
        user_summary=user_summary,
        status="success",
        failure_reason="",
        total_tokens=21,
        execution_ms=75,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5.2",
        call_chain=call_chain,
    )


async def _create_succeeded_role_task(
    store: TaskStore,
    *,
    session_id: str,
    parent_task_id: str,
    task_key: str,
    payload: dict[str, Any],
    output: dict[str, Any],
    description: str,
    workflow_name: str = "default_chat_loop",
    priority: int = 700,
    timeout_seconds: int = 300,
    max_attempts: int = 1,
) -> dict[str, Any]:
    """Create one succeeded workflow-role task with a persisted step output."""
    created = await store.create_task(
        description,
        task_type="workflow_role",
        payload=payload,
        source="workflow_role",
        session_id=session_id,
        priority=priority,
        timeout_seconds=timeout_seconds,
        max_attempts=max_attempts,
        idempotency_key=f"{session_id}:{workflow_name}:{task_key}",
    )
    running = await store.transition_task(created["task_id"], "running")
    await store.start_task_step(
        running["task_id"],
        spawn_module._ROLE_RUN_STEP_ID,
        step_name="role_runtime_ack",
        input_payload={"task_key": task_key, "turn_index": int(payload.get("turn_index") or 1)},
        is_checkpoint=True,
        idempotent=True,
    )
    await store.complete_task_step(
        running["task_id"],
        spawn_module._ROLE_RUN_STEP_ID,
        output_payload=output,
    )
    return await store.transition_task(running["task_id"], "succeeded")


@pytest.mark.asyncio
async def test_background_runtime_metrics_use_configured_capacity(monkeypatch) -> None:
    """Runtime metrics should respect configured background worker capacity."""
    monkeypatch.setattr(
        "nanoclaw.core.config.get_config",
        lambda: SimpleNamespace(
            tools=SimpleNamespace(
                background_tasks=SimpleNamespace(
                    max_concurrency=7,
                    starvation_threshold_seconds=45,
                    stall_threshold_seconds=90,
                    alert_channel="feishu",
                    alert_escalation_channel="console",
                    alert_cooldown_seconds=120,
                    schedule_alert_retrying_after=3,
                    schedule_alert_escalate_after=4,
                ),
            )
        ),
    )
    metrics = spawn_module.get_background_runtime_metrics()
    assert metrics["capacity"] == 7
    assert metrics["starvation_threshold_seconds"] == 45
    assert metrics["stall_threshold_seconds"] == 90
    assert metrics["lease_timeout_seconds"] == 45
    assert metrics["heartbeat_interval_seconds"] == 10
    assert metrics["alert_channel"] == "feishu"
    assert metrics["alert_escalation_channel"] == "console"
    assert metrics["alert_cooldown_seconds"] == 120
    assert metrics["alert_escalate_after"] == 2
    assert metrics["schedule_alert_retrying_after"] == 3
    assert metrics["schedule_alert_escalate_after"] == 4


def test_summarize_runtime_health_marks_queue_stall_critical() -> None:
    """A stalled ready queue should escalate runtime health to critical."""
    health = spawn_module.summarize_runtime_health(
        {
            "ready_backlog": 2,
            "running_tasks": 0,
            "oldest_ready_age_seconds": 180,
            "stall_threshold_seconds": 120,
            "stale_running_tasks": 0,
            "dead_letter_tasks": 0,
            "cancel_requested_running": 0,
        }
    )

    assert health["status"] == "critical"
    assert health["queue_stalled"] is True
    assert health["base_alert_severity"] == "error"
    assert "queue_stall=2" in health["reasons"]


@pytest.mark.asyncio
async def test_background_task_retries_then_succeeds(tmp_path: Path) -> None:
    """A transient failure should requeue the task and succeed on the next attempt."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)

    class FlakyAgent:
        """Agent stub that fails once before succeeding."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def run(self, user_message: str, session_id: str) -> str:
            self.calls.append((user_message, session_id))
            if len(self.calls) == 1:
                raise RuntimeError("temporary upstream error")
            return "retried successfully"

    fake_agent = FlakyAgent()
    fake_gateway = FakeGateway()
    set_agent(fake_agent)
    set_gateway(fake_gateway)

    try:
        token = set_tool_runtime_context("telegram:42")
        try:
            response = await spawn_module.spawn_task(
                "retry this",
                max_attempts=2,
                retry_backoff_seconds=0,
            )
        finally:
            reset_tool_runtime_context(token)

        task_id = response.split("`")[1]
        statuses = await _wait_for_status(store, {"retry this": "succeeded"}, timeout=1.0)
        assert statuses["retry this"] == "succeeded"

        refreshed = await store.get_task(task_id)
        assert refreshed is not None
        assert refreshed["attempt_count"] == 2
        assert refreshed["last_error"] == ""
        assert fake_agent.calls == [
            ("retry this", f"task:{task_id}"),
            ("retry this", f"task:{task_id}"),
        ]
        assert fake_gateway.messages
        assert "retried successfully" in fake_gateway.messages[-1][1]
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_background_task_reuses_agent_checkpoint_after_notify_retry(
    tmp_path: Path,
) -> None:
    """A notify retry should reuse the completed agent step instead of rerunning it."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)

    class ImmediateAgent:
        """Agent stub that returns immediately and records calls."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def run(self, user_message: str, session_id: str) -> str:
            self.calls.append((user_message, session_id))
            return "checkpointed result"

    fake_agent = ImmediateAgent()
    fake_gateway = FailOnceGateway()
    set_agent(fake_agent)
    set_gateway(fake_gateway)

    try:
        token = set_tool_runtime_context("telegram:checkpoint")
        try:
            response = await spawn_module.spawn_task(
                "checkpoint retry",
                max_attempts=2,
                retry_backoff_seconds=0,
            )
        finally:
            reset_tool_runtime_context(token)

        task_id = response.split("`")[1]
        statuses = await _wait_for_status(store, {"checkpoint retry": "succeeded"}, timeout=1.0)
        assert statuses["checkpoint retry"] == "succeeded"
        assert fake_agent.calls == [("checkpoint retry", f"task:{task_id}")]
        assert fake_gateway.calls == 2

        steps = await store.list_task_steps(task_id)
        step_map = {step["step_id"]: step for step in steps}
        assert step_map["agent_run"]["status"] == "succeeded"
        assert step_map["agent_run"]["attempt_count"] == 1
        assert step_map["notify_result"]["status"] == "succeeded"
        assert step_map["notify_result"]["attempt_count"] == 2
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_terminal_notifications_are_deduplicated_by_step(tmp_path: Path) -> None:
    """Successful terminal notifications should not be sent twice for the same task."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)
    task = await store.create_task("dedupe terminal notify", source="spawn_task")
    gateway = FakeGateway()

    try:
        await spawn_module._send_cancelled_notification(gateway, store, task)
        await spawn_module._send_cancelled_notification(gateway, store, task)

        assert len(gateway.messages) == 1
        steps = await store.list_task_steps(task["task_id"])
        step_map = {step["step_id"]: step for step in steps}
        assert step_map["notify_cancelled"]["status"] == "succeeded"
        assert step_map["notify_cancelled"]["attempt_count"] == 1
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_failure_notification_can_retry_without_duplicate_delivery(tmp_path: Path) -> None:
    """A failed notification attempt should be retryable and still dedupe successful delivery."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)
    task = await store.create_task("retry failure notify", source="spawn_task")
    gateway = FailOnceGateway()

    try:
        with pytest.raises(RuntimeError, match="temporary send failure"):
            await spawn_module._send_failure_notification(gateway, store, task, "fatal")

        await spawn_module._send_failure_notification(gateway, store, task, "fatal")
        await spawn_module._send_failure_notification(gateway, store, task, "fatal")

        assert len(gateway.messages) == 1
        assert "fatal" in gateway.messages[0][1]
        steps = await store.list_task_steps(task["task_id"])
        step_map = {step["step_id"]: step for step in steps}
        assert step_map["notify_failure"]["status"] == "succeeded"
        assert step_map["notify_failure"]["attempt_count"] == 2
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_spawn_task_persists_and_completes(tmp_path: Path) -> None:
    """spawn_task should persist a task row before the background job completes."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)

    fake_agent = FakeAgent()
    fake_gateway = FakeGateway()
    set_agent(fake_agent)
    set_gateway(fake_gateway)

    try:
        token = set_tool_runtime_context("feishu:user-1", workflow_identity="workflow_spawn_1")
        try:
            response = await spawn_module.spawn_task("Research competitors")
        finally:
            reset_tool_runtime_context(token)

        assert "Task queued in background as `task_" in response

        tasks = await store.list_tasks(limit=5)
        assert len(tasks) == 1
        assert tasks[0]["description"] == "Research competitors"
        assert tasks[0]["source"] == "spawn_task"
        assert tasks[0]["session_id"] == "feishu:user-1"
        assert tasks[0]["payload"]["workflow_identity"] == "workflow_spawn_1"
        assert tasks[0]["status"] in {"pending", "running"}

        fake_agent._release.set()
        await asyncio.sleep(0.05)

        refreshed = await store.get_task(tasks[0]["task_id"])
        assert refreshed is not None
        assert refreshed["status"] == "succeeded"
        assert fake_agent.calls == [("Research competitors", f"task:{tasks[0]['task_id']}")]
        assert fake_gateway.messages
        channel, text = fake_gateway.messages[0]
        assert channel == "feishu"
        assert tasks[0]["task_id"] in text
        assert "background complete" in text
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_role_runtime_bridge_helper_creates_child_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Role runtime bridge specs should enqueue roots first, then downstream roles."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    parent = await store.create_task("parent workflow task", source="spawn_task")

    await audit.log_workflow_run(
        session_id=f"task:{parent['task_id']}",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="bridge child roles",
        status="success",
        failure_reason="",
        total_tokens=42,
        execution_ms=120,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5.2",
        call_chain=[
            {
                "type": "workflow_role_task_bridge",
                "task_key": "planner@pre_llm",
                "role": "planner",
                "stage": "pre_llm",
                "task_type": "workflow_role",
                "source": "workflow_role",
                "description": "default_chat_loop:planner@pre_llm",
                "priority": 820,
                "timeout_seconds": 180,
                "max_attempts": 2,
                "idempotency_key": f"task:{parent['task_id']}:default_chat_loop:planner@pre_llm",
                "payload": {
                    "session_id": f"task:{parent['task_id']}",
                    "parent_task_id": parent["task_id"],
                    "workflow_name": "default_chat_loop",
                    "tool_names": ["web_search"],
                    "needs_grounded": True,
                    "role": "planner",
                    "stage": "pre_llm",
                    "task_key": "planner@pre_llm",
                    "depends_on": [],
                    "checkpoint_id": "planner@pre_llm",
                    "retry_budget": 2,
                    "evidence_refs": [],
                    "evidence_snapshot": {"count": 0, "items": []},
                },
                "evidence_refs": [],
            },
            {
                "type": "workflow_role_task_bridge",
                "task_key": "router@pre_llm",
                "role": "router",
                "stage": "pre_llm",
                "task_type": "workflow_role",
                "source": "workflow_role",
                "description": "default_chat_loop:router@pre_llm",
                "priority": 800,
                "timeout_seconds": 180,
                "max_attempts": 2,
                "idempotency_key": f"task:{parent['task_id']}:default_chat_loop:router@pre_llm",
                "payload": {
                    "session_id": f"task:{parent['task_id']}",
                    "parent_task_id": parent["task_id"],
                    "workflow_name": "default_chat_loop",
                    "tool_names": ["web_search"],
                    "needs_grounded": True,
                    "role": "router",
                    "stage": "pre_llm",
                    "task_key": "router@pre_llm",
                    "depends_on": ["planner@pre_llm"],
                    "checkpoint_id": "router@pre_llm",
                    "retry_budget": 2,
                    "evidence_refs": [],
                    "evidence_snapshot": {"count": 0, "items": []},
                },
                "evidence_refs": [],
            },
        ],
    )

    created = await spawn_module._enqueue_role_runtime_bridge_tasks(store, parent)

    assert len(created) == 1
    assert created[0]["source"] == "workflow_role"
    assert created[0]["task_type"] == "workflow_role"
    assert created[0]["payload"]["parent_task_id"] == parent["task_id"]
    assert created[0]["payload"]["task_key"] == "planner@pre_llm"

    planner = await store.transition_task(created[0]["task_id"], "running")
    await store.start_task_step(
        planner["task_id"],
        spawn_module._ROLE_RUN_STEP_ID,
        step_name="role_runtime_ack",
        input_payload={"task_key": "planner@pre_llm"},
        is_checkpoint=True,
        idempotent=True,
    )
    await store.complete_task_step(
        planner["task_id"],
        spawn_module._ROLE_RUN_STEP_ID,
        output_payload={
            "task_key": "planner@pre_llm",
            "role": "planner",
            "role_label": "planner",
            "action": "stable_plan_ready",
            "artifact_preview": "Angle + structure ready.",
            "tool_handler_output_preview": "## planner",
            "evidence_refs": [],
        },
    )
    planner = await store.transition_task(planner["task_id"], "succeeded")
    downstream = await spawn_module._enqueue_role_runtime_bridge_tasks(store, planner)
    child_tasks = await store.list_child_tasks(parent["task_id"], source="workflow_role")
    router_task = next(
        (
            item
            for item in (downstream or child_tasks)
            if str(item.get("payload", {}).get("task_key") or "") == "router@pre_llm"
        ),
        None,
    )
    assert router_task is not None
    assert router_task["payload"]["upstream_dependency_outputs"][0]["task_key"] == "planner@pre_llm"
    assert (
        router_task["payload"]["upstream_dependency_outputs"][0]["artifact_preview"]
        == "Angle + structure ready."
    )
    assert (
        router_task["payload"]["upstream_dependency_outputs"][0]["tool_handler_output_preview"]
        == "## planner"
    )


@pytest.mark.asyncio
async def test_role_runtime_bridge_helper_supports_any_dependency_edges(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge enqueue should materialize one node when any configured upstream dependency succeeds."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    parent = await store.create_task("parent workflow task", source="spawn_task")
    session_id = f"task:{parent['task_id']}"

    await _log_role_bridge_run(
        audit,
        session_id=session_id,
        call_chain=[
            _build_role_bridge_item(
                session_id=session_id,
                parent_task_id=parent["task_id"],
                task_key="executor@tool_phase",
                role="executor",
                stage="tool_phase",
                priority=760,
                timeout_seconds=600,
                max_attempts=2,
                depends_on_any=["router@pre_llm", "planner@pre_llm"],
            ),
        ],
    )

    router = await _create_succeeded_role_task(
        store,
        session_id=session_id,
        parent_task_id=parent["task_id"],
        task_key="router@pre_llm",
        workflow_name="default_chat_loop",
        priority=800,
        timeout_seconds=180,
        max_attempts=2,
        description="default_chat_loop:router@pre_llm",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "role": "router",
            "role_label": "router",
            "stage": "pre_llm",
            "task_key": "router@pre_llm",
            "turn_index": 1,
            "turn_budget": 1,
        },
        output={
            "status": "executed",
            "role": "router",
            "role_label": "router",
            "action": "route_selected:grounded",
            "artifact_preview": "Router chose the grounded path.",
            "turn_index": 1,
        },
    )

    created = await spawn_module._enqueue_role_runtime_bridge_tasks(store, router)

    assert len(created) == 1
    assert created[0]["payload"]["task_key"] == "executor@tool_phase"
    assert created[0]["payload"]["depends_on_any"] == ["router@pre_llm", "planner@pre_llm"]
    assert created[0]["payload"]["upstream_dependency_outputs"][0]["task_key"] == "router@pre_llm"
    await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_role_runtime_bridge_helper_applies_persistent_resume_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge enqueue should seed matching role tasks across parent-task sessions."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    parent = await store.create_task(
        "parent workflow task",
        source="spawn_task",
        payload={"parent_session_id": "telegram:42"},
    )
    session_id = f"task:{parent['task_id']}"

    await audit.log_workflow_run(
        session_id="task:old_parent",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="resume prior run",
        status="degraded",
        failure_reason="web_search:error",
        total_tokens=18,
        execution_ms=90,
        llm_calls=1,
        tool_calls=1,
        final_model="gpt-5.2",
        call_chain=[
            {
                "type": "workflow_context",
                "name": "parent_session_id",
                "status": "attached",
                "value": "telegram:42",
            },
            {
                "type": "workflow_role_checkpoint",
                "checkpoint_id": "router@pre_llm",
                "role": "router",
                "stage": "pre_llm",
                "message_count": 2,
                "evidence_count": 1,
                "evidence_refs": ["ev_resume"],
                "evidence_items": [
                    {
                        "evidence_id": "ev_resume",
                        "tool_name": "web_search",
                        "url": "https://example.com/resume",
                        "title": "Resume evidence",
                    }
                ],
            },
            {
                "type": "workflow_role_recovery",
                "failed_role": "executor",
                "recovery_role": "router",
                "stage": "post_tools",
                "reason": "web_search:error",
                "resume_checkpoint_id": "router@pre_llm",
                "attempt_number": 1,
                "budget_limit": 2,
                "remaining_budget": 1,
                "restored_messages": 2,
                "restored_evidence_count": 1,
                "status": "resumed",
                "evidence_refs": ["ev_resume"],
            },
        ],
    )
    await audit.log_workflow_run(
        session_id=session_id,
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="resume new run",
        status="success",
        failure_reason="",
        total_tokens=20,
        execution_ms=100,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5.2",
        call_chain=[
            {
                "type": "workflow_role_task_bridge",
                "task_key": "router@pre_llm",
                "role": "router",
                "stage": "pre_llm",
                "task_type": "workflow_role",
                "source": "workflow_role",
                "description": "default_chat_loop:router@pre_llm",
                "priority": 800,
                "timeout_seconds": 180,
                "max_attempts": 2,
                "idempotency_key": f"{session_id}:default_chat_loop:router@pre_llm",
                "payload": {
                    "session_id": session_id,
                    "parent_task_id": parent["task_id"],
                    "workflow_name": "default_chat_loop",
                    "tool_names": ["web_search"],
                    "needs_grounded": True,
                    "role": "router",
                    "stage": "pre_llm",
                    "task_key": "router@pre_llm",
                    "checkpoint_id": "router@pre_llm",
                    "depends_on": [],
                    "evidence_refs": [],
                    "evidence_snapshot": {"count": 0, "items": []},
                },
                "evidence_refs": [],
            }
        ],
    )

    created = await spawn_module._enqueue_role_runtime_bridge_tasks(store, parent)

    assert len(created) == 1
    payload = created[0]["payload"]
    assert payload["task_key"] == "router@pre_llm"
    assert payload["resume_state"]["source_workflow_run_id"] > 0
    assert payload["resume_checkpoint_id"] == "router@pre_llm"
    assert payload["resume_state"]["workflow_identity"] == ""
    assert payload["evidence_snapshot"]["count"] == 1
    assert payload["evidence_refs"] == ["ev_resume"]
    assert "Resume from persisted role checkpoint" in payload["resume_brief"]


@pytest.mark.asyncio
async def test_role_runtime_bridge_helper_prefers_workflow_identity_resume_lookup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Bridge enqueue should match degraded resume state by workflow identity across parents."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    parent = await store.create_task(
        "parent workflow task",
        source="spawn_task",
        payload={"workflow_identity": "workflow_chain_1"},
    )
    session_id = f"task:{parent['task_id']}"

    await audit.log_workflow_run(
        session_id="task:old_parent",
        workflow_name="default_chat_loop",
        workflow_identity="workflow_chain_1",
        workflow_tags=["default_chat_loop"],
        user_summary="resume prior chain",
        status="degraded",
        failure_reason="web_search:error",
        total_tokens=18,
        execution_ms=90,
        llm_calls=1,
        tool_calls=1,
        final_model="gpt-5.2",
        call_chain=[
            {
                "type": "workflow_role_checkpoint",
                "checkpoint_id": "router@pre_llm",
                "role": "router",
                "stage": "pre_llm",
                "message_count": 2,
                "evidence_count": 1,
                "evidence_refs": ["ev_resume"],
                "evidence_items": [
                    {
                        "evidence_id": "ev_resume",
                        "tool_name": "web_search",
                        "url": "https://example.com/resume",
                    }
                ],
            },
            {
                "type": "workflow_role_recovery",
                "failed_role": "executor",
                "recovery_role": "router",
                "stage": "post_tools",
                "reason": "web_search:error",
                "resume_checkpoint_id": "router@pre_llm",
                "attempt_number": 1,
                "budget_limit": 2,
                "remaining_budget": 1,
                "restored_messages": 2,
                "restored_evidence_count": 1,
                "status": "resumed",
                "evidence_refs": ["ev_resume"],
            },
        ],
    )
    await audit.log_workflow_run(
        session_id=session_id,
        workflow_name="default_chat_loop",
        workflow_identity="workflow_chain_1",
        workflow_tags=["default_chat_loop"],
        user_summary="resume new run",
        status="success",
        failure_reason="",
        total_tokens=20,
        execution_ms=100,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5.2",
        call_chain=[
            _build_role_bridge_item(
                session_id=session_id,
                parent_task_id=parent["task_id"],
                task_key="router@pre_llm",
                role="router",
                stage="pre_llm",
                priority=800,
                timeout_seconds=180,
                max_attempts=2,
                checkpoint_id="router@pre_llm",
                workflow_identity="workflow_chain_1",
            )
        ],
    )

    created = await spawn_module._enqueue_role_runtime_bridge_tasks(store, parent)

    assert len(created) == 1
    payload = created[0]["payload"]
    assert payload["workflow_identity"] == "workflow_chain_1"
    assert payload["resume_state"]["workflow_identity"] == "workflow_chain_1"
    assert payload["resume_state"]["evidence_refs"] == ["ev_resume"]


@pytest.mark.asyncio
async def test_role_runtime_bridge_rearms_succeeded_critic_when_executor_inputs_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Changed executor outputs should rearm the same critic task for one second turn."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    set_gateway(FakeGateway())
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    parent = await store.create_task("parent workflow task", source="test_parent")
    session_id = f"task:{parent['task_id']}"
    workflow_identity = "workflow_turn_chain_critic"

    await _log_role_bridge_run(
        audit,
        session_id=session_id,
        call_chain=[
            _build_role_bridge_item(
                session_id=session_id,
                parent_task_id=parent["task_id"],
                task_key="critic@post_tools",
                role="critic",
                stage="post_tools",
                priority=720,
                timeout_seconds=240,
                max_attempts=1,
                depends_on=["executor@tool_phase"],
                checkpoint_id="critic@post_tools",
                resume_checkpoint_id="router@pre_llm",
                workflow_identity=workflow_identity,
            ),
        ],
    )

    executor = await _create_succeeded_role_task(
        store,
        session_id=session_id,
        parent_task_id=parent["task_id"],
        task_key="executor@tool_phase",
        workflow_name="default_chat_loop",
        priority=760,
        timeout_seconds=600,
        max_attempts=2,
        description="default_chat_loop:executor@tool_phase",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "executor",
            "role_label": "executor",
            "stage": "tool_phase",
            "task_key": "executor@tool_phase",
            "turn_index": 1,
            "turn_budget": 1,
            "turn_reason": "initial",
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "evidence_refs": ["ev_new"],
            "evidence_snapshot": {
                "count": 1,
                "items": [
                    {
                        "evidence_id": "ev_new",
                        "tool_name": "web_search",
                        "url": "https://example.com/new",
                    }
                ],
            },
        },
        output={
            "status": "executed",
            "result_text": "default_chat_loop:executor@tool_phase -> shared_evidence_reused",
            "role": "executor",
            "role_label": "executor",
            "action": "shared_evidence_reused",
            "artifact_preview": "Executor reused new evidence.",
            "evidence_count": 1,
            "evidence_refs": ["ev_new"],
            "turn_index": 1,
            "turn_reason": "initial",
            "upstream_input_fingerprint": "executor_turn_1",
        },
    )
    critic = await _create_succeeded_role_task(
        store,
        session_id=session_id,
        parent_task_id=parent["task_id"],
        task_key="critic@post_tools",
        workflow_name="default_chat_loop",
        priority=720,
        timeout_seconds=240,
        max_attempts=1,
        description="default_chat_loop:critic@post_tools",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "critic",
            "role_label": "critic",
            "stage": "post_tools",
            "task_key": "critic@post_tools",
            "depends_on": ["executor@tool_phase"],
            "checkpoint_id": "critic@post_tools",
            "resume_checkpoint_id": "router@pre_llm",
            "turn_index": 1,
            "turn_budget": 2,
            "turn_reason": "initial",
            "upstream_input_fingerprint": "stale_fingerprint",
            "turn_history": [],
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "evidence_refs": [],
            "evidence_snapshot": {"count": 0, "items": []},
        },
        output={
            "status": "executed",
            "scheduler_action": "complete",
            "result_text": "default_chat_loop:critic@post_tools -> critic_verdict:light_review",
            "role": "critic",
            "role_label": "critic",
            "action": "critic_verdict:light_review",
            "evidence_count": 0,
            "turn_index": 1,
            "turn_reason": "initial",
            "upstream_input_fingerprint": "stale_fingerprint",
        },
    )

    created = await spawn_module._enqueue_role_runtime_bridge_tasks(store, executor)

    assert created == []
    rearmed = await store.get_task(critic["task_id"])
    assert rearmed is not None
    assert rearmed["task_id"] == critic["task_id"]
    assert rearmed["status"] == "pending"
    assert rearmed["payload"]["turn_index"] == 2
    assert rearmed["payload"]["turn_reason"] == "upstream_changed"
    assert rearmed["payload"]["turn_history"][0]["action"] == "critic_verdict:light_review"
    assert rearmed["payload"]["upstream_input_fingerprint"] != "stale_fingerprint"

    try:
        await spawn_module.start_background_runtime()
        statuses = await _wait_for_status(
            store,
            {"default_chat_loop:critic@post_tools": "succeeded"},
            timeout=1.0,
        )
        assert statuses["default_chat_loop:critic@post_tools"] == "succeeded"
        step = await store.get_task_step(critic["task_id"], spawn_module._ROLE_RUN_STEP_ID)
        assert step is not None
        output = step["output"]
        assert output["scheduler_action"] == "rearmed_upstream"
        assert output["turn_index"] == 2
        assert output["turn_budget"] == 2
        assert output["turn_reason"] == "upstream_changed"
        assert output["turn_history"][0]["turn_index"] == 1
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_role_runtime_bridge_keeps_succeeded_critic_when_fingerprint_is_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unchanged upstream fingerprints should not rearm an already-succeeded critic task."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    parent = await store.create_task("parent workflow task", source="test_parent")
    session_id = f"task:{parent['task_id']}"
    workflow_identity = "workflow_turn_chain_same"

    bridge_payload = _build_role_bridge_item(
        session_id=session_id,
        parent_task_id=parent["task_id"],
        task_key="critic@post_tools",
        role="critic",
        stage="post_tools",
        priority=720,
        timeout_seconds=240,
        max_attempts=1,
        depends_on=["executor@tool_phase"],
        checkpoint_id="critic@post_tools",
        resume_checkpoint_id="router@pre_llm",
        workflow_identity=workflow_identity,
    )["payload"]
    dependency_outputs = [
        {
            "task_key": "executor@tool_phase",
            "role": "executor",
            "role_label": "executor",
            "status": "succeeded",
            "attempt_number": 1,
            "turn_index": 1,
            "action": "shared_evidence_reused",
            "artifact_preview": "Executor reused new evidence.",
            "tool_handler_name": "",
            "tool_handler_stage": "",
            "tool_handler_status": "",
            "tool_handler_output_preview": "",
            "result_text": "default_chat_loop:executor@tool_phase -> shared_evidence_reused",
            "evidence_refs": ["ev_new"],
        }
    ]
    prepared_payload = spawn_module._prepare_role_turn_payload(bridge_payload, dependency_outputs)

    await _log_role_bridge_run(
        audit,
        session_id=session_id,
        call_chain=[
            _build_role_bridge_item(
                session_id=session_id,
                parent_task_id=parent["task_id"],
                task_key="critic@post_tools",
                role="critic",
                stage="post_tools",
                priority=720,
                timeout_seconds=240,
                max_attempts=1,
                depends_on=["executor@tool_phase"],
                checkpoint_id="critic@post_tools",
                resume_checkpoint_id="router@pre_llm",
                workflow_identity=workflow_identity,
            ),
        ],
    )
    executor = await _create_succeeded_role_task(
        store,
        session_id=session_id,
        parent_task_id=parent["task_id"],
        task_key="executor@tool_phase",
        workflow_name="default_chat_loop",
        priority=760,
        timeout_seconds=600,
        max_attempts=2,
        description="default_chat_loop:executor@tool_phase",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "executor",
            "role_label": "executor",
            "stage": "tool_phase",
            "task_key": "executor@tool_phase",
            "turn_index": 1,
            "turn_budget": 1,
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "evidence_refs": ["ev_new"],
            "evidence_snapshot": {"count": 1, "items": [{"evidence_id": "ev_new"}]},
        },
        output=dict(dependency_outputs[0]),
    )
    critic = await _create_succeeded_role_task(
        store,
        session_id=session_id,
        parent_task_id=parent["task_id"],
        task_key="critic@post_tools",
        workflow_name="default_chat_loop",
        priority=720,
        timeout_seconds=240,
        max_attempts=1,
        description="default_chat_loop:critic@post_tools",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "critic",
            "role_label": "critic",
            "stage": "post_tools",
            "task_key": "critic@post_tools",
            "depends_on": ["executor@tool_phase"],
            "checkpoint_id": "critic@post_tools",
            "resume_checkpoint_id": "router@pre_llm",
            "turn_index": 1,
            "turn_budget": 2,
            "turn_reason": "initial",
            "upstream_input_fingerprint": prepared_payload["upstream_input_fingerprint"],
            "turn_history": [],
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "evidence_refs": [],
            "evidence_snapshot": {"count": 0, "items": []},
        },
        output={
            "status": "executed",
            "scheduler_action": "complete",
            "result_text": "default_chat_loop:critic@post_tools -> critic_verdict:light_review",
            "role": "critic",
            "role_label": "critic",
            "action": "critic_verdict:light_review",
            "evidence_count": 0,
            "turn_index": 1,
            "turn_reason": "initial",
            "upstream_input_fingerprint": prepared_payload["upstream_input_fingerprint"],
        },
    )

    created = await spawn_module._enqueue_role_runtime_bridge_tasks(store, executor)

    assert created == []
    unchanged = await store.get_task(critic["task_id"])
    assert unchanged is not None
    assert unchanged["status"] == "succeeded"
    assert unchanged["payload"]["turn_index"] == 1
    assert unchanged["payload"]["upstream_input_fingerprint"] == prepared_payload[
        "upstream_input_fingerprint"
    ]
    await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_role_runtime_bridge_stops_rearm_after_turn_budget_is_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A succeeded downstream role should not rearm after reaching its turn budget."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    parent = await store.create_task("parent workflow task", source="test_parent")
    session_id = f"task:{parent['task_id']}"
    workflow_identity = "workflow_turn_chain_budget"

    await _log_role_bridge_run(
        audit,
        session_id=session_id,
        call_chain=[
            _build_role_bridge_item(
                session_id=session_id,
                parent_task_id=parent["task_id"],
                task_key="critic@post_tools",
                role="critic",
                stage="post_tools",
                priority=720,
                timeout_seconds=240,
                max_attempts=1,
                depends_on=["executor@tool_phase"],
                checkpoint_id="critic@post_tools",
                resume_checkpoint_id="router@pre_llm",
                workflow_identity=workflow_identity,
            ),
        ],
    )
    executor = await _create_succeeded_role_task(
        store,
        session_id=session_id,
        parent_task_id=parent["task_id"],
        task_key="executor@tool_phase",
        workflow_name="default_chat_loop",
        priority=760,
        timeout_seconds=600,
        max_attempts=2,
        description="default_chat_loop:executor@tool_phase",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "executor",
            "role_label": "executor",
            "stage": "tool_phase",
            "task_key": "executor@tool_phase",
            "turn_index": 1,
            "turn_budget": 1,
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "evidence_refs": ["ev_new"],
            "evidence_snapshot": {"count": 1, "items": [{"evidence_id": "ev_new"}]},
        },
        output={
            "status": "executed",
            "result_text": "default_chat_loop:executor@tool_phase -> shared_evidence_reused",
            "role": "executor",
            "role_label": "executor",
            "action": "shared_evidence_reused",
            "artifact_preview": "Executor reused new evidence.",
            "evidence_count": 1,
            "evidence_refs": ["ev_new"],
            "turn_index": 1,
            "turn_reason": "initial",
            "upstream_input_fingerprint": "executor_turn_1",
        },
    )
    critic = await _create_succeeded_role_task(
        store,
        session_id=session_id,
        parent_task_id=parent["task_id"],
        task_key="critic@post_tools",
        workflow_name="default_chat_loop",
        priority=720,
        timeout_seconds=240,
        max_attempts=1,
        description="default_chat_loop:critic@post_tools",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "critic",
            "role_label": "critic",
            "stage": "post_tools",
            "task_key": "critic@post_tools",
            "depends_on": ["executor@tool_phase"],
            "checkpoint_id": "critic@post_tools",
            "resume_checkpoint_id": "router@pre_llm",
            "turn_index": 2,
            "turn_budget": 2,
            "turn_reason": "upstream_changed",
            "upstream_input_fingerprint": "older_fingerprint",
            "turn_history": [{"turn_index": 1, "turn_reason": "initial"}],
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "evidence_refs": [],
            "evidence_snapshot": {"count": 0, "items": []},
        },
        output={
            "status": "executed",
            "scheduler_action": "rearmed_upstream",
            "result_text": "default_chat_loop:critic@post_tools -> critic_verdict:light_review",
            "role": "critic",
            "role_label": "critic",
            "action": "critic_verdict:light_review",
            "evidence_count": 0,
            "turn_index": 2,
            "turn_reason": "upstream_changed",
            "upstream_input_fingerprint": "older_fingerprint",
        },
    )

    created = await spawn_module._enqueue_role_runtime_bridge_tasks(store, executor)

    assert created == []
    unchanged = await store.get_task(critic["task_id"])
    assert unchanged is not None
    assert unchanged["status"] == "succeeded"
    assert unchanged["payload"]["turn_index"] == 2
    await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_article_role_runtime_bridge_rearms_succeeded_editor_on_changed_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Article flow should use the same same-task rearm scheduler through shared task keys."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    set_gateway(FakeGateway())
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    parent = await store.create_task("parent workflow task", source="test_parent")
    session_id = f"task:{parent['task_id']}"
    workflow_identity = "workflow_turn_chain_article"
    article_editor_bridge = _build_role_bridge_item(
        session_id=session_id,
        parent_task_id=parent["task_id"],
        task_key="summarizer@post_tools",
        role="summarizer",
        stage="post_tools",
        priority=680,
        timeout_seconds=180,
        max_attempts=1,
        workflow_name="wechat_article_flow",
        depends_on=["critic@post_tools"],
        checkpoint_id="summarizer@post_tools",
        resume_checkpoint_id="router@pre_llm",
        workflow_identity=workflow_identity,
    )
    article_editor_bridge["payload"]["role_label"] = "editor"
    article_editor_bridge["payload"]["role_stage_name"] = "editor"

    await _log_role_bridge_run(
        audit,
        session_id=session_id,
        workflow_name="wechat_article_flow",
        call_chain=[
            article_editor_bridge,
        ],
    )
    critic = await _create_succeeded_role_task(
        store,
        session_id=session_id,
        parent_task_id=parent["task_id"],
        task_key="critic@post_tools",
        workflow_name="wechat_article_flow",
        priority=720,
        timeout_seconds=240,
        max_attempts=1,
        description="wechat_article_flow:critic@post_tools",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "wechat_article_flow",
            "workflow_identity": workflow_identity,
            "role": "critic",
            "role_label": "critic",
            "stage": "post_tools",
            "task_key": "critic@post_tools",
            "turn_index": 2,
            "turn_budget": 2,
            "turn_reason": "upstream_changed",
            "evidence_refs": ["ev_article"],
            "evidence_snapshot": {"count": 1, "items": [{"evidence_id": "ev_article"}]},
        },
        output={
            "status": "executed",
            "result_text": "wechat_article_flow:critic@post_tools -> article_gate:publish_ready",
            "role": "critic",
            "role_label": "critic",
            "action": "article_gate:publish_ready",
            "artifact_preview": "Fact-check gate complete.",
            "evidence_count": 1,
            "evidence_refs": ["ev_article"],
            "turn_index": 2,
            "turn_reason": "upstream_changed",
            "upstream_input_fingerprint": "critic_article_turn_2",
        },
    )
    editor = await _create_succeeded_role_task(
        store,
        session_id=session_id,
        parent_task_id=parent["task_id"],
        task_key="summarizer@post_tools",
        workflow_name="wechat_article_flow",
        priority=680,
        timeout_seconds=180,
        max_attempts=1,
        description="wechat_article_flow:summarizer@post_tools",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "wechat_article_flow",
            "workflow_identity": workflow_identity,
            "role": "summarizer",
            "role_label": "editor",
            "stage": "post_tools",
            "task_key": "summarizer@post_tools",
            "depends_on": ["critic@post_tools"],
            "checkpoint_id": "summarizer@post_tools",
            "resume_checkpoint_id": "router@pre_llm",
            "turn_index": 1,
            "turn_budget": 2,
            "turn_reason": "initial",
            "upstream_input_fingerprint": "stale_article_fingerprint",
            "turn_history": [],
            "tool_names": ["wechat_article_assist"],
            "needs_grounded": True,
            "evidence_refs": ["ev_article"],
            "evidence_snapshot": {"count": 1, "items": [{"evidence_id": "ev_article"}]},
        },
        output={
            "status": "executed",
            "scheduler_action": "complete",
            "result_text": "wechat_article_flow:summarizer@post_tools -> article_bundle_ready",
            "role": "summarizer",
            "role_label": "editor",
            "action": "article_bundle_ready",
            "evidence_count": 1,
            "turn_index": 1,
            "turn_reason": "initial",
            "upstream_input_fingerprint": "stale_article_fingerprint",
        },
    )

    created = await spawn_module._enqueue_role_runtime_bridge_tasks(store, critic)

    assert created == []
    rearmed = await store.get_task(editor["task_id"])
    assert rearmed is not None
    assert rearmed["task_id"] == editor["task_id"]
    assert rearmed["status"] == "pending"
    assert rearmed["payload"]["turn_index"] == 2
    assert rearmed["payload"]["role_label"] == "editor"

    try:
        await spawn_module.start_background_runtime()
        statuses = await _wait_for_status(
            store,
            {"wechat_article_flow:summarizer@post_tools": "succeeded"},
            timeout=1.0,
        )
        assert statuses["wechat_article_flow:summarizer@post_tools"] == "succeeded"
        step = await store.get_task_step(editor["task_id"], spawn_module._ROLE_RUN_STEP_ID)
        assert step is not None
        assert step["output"]["scheduler_action"] == "rearmed_upstream"
        assert step["output"]["turn_index"] == 2
        assert step["output"]["role_label"] == "editor"
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_background_role_task_requests_runtime_recovery_without_enqueuing_failed_branch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A degraded role should enqueue its recovery role without advancing the failed branch."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    set_gateway(FakeGateway())
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    monkeypatch.setattr(spawn_module, "_schedule_queue_drain", lambda: None)
    parent = await store.create_task("parent workflow task", source="spawn_task")
    session_id = f"task:{parent['task_id']}"
    workflow_identity = "workflow_recovery_critic"

    await audit.log_workflow_run(
        session_id=session_id,
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="runtime recovery path",
        status="success",
        failure_reason="",
        total_tokens=21,
        execution_ms=75,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5.2",
        call_chain=[
            {
                "type": "workflow_role_task_bridge",
                "task_key": "router@pre_llm",
                "role": "router",
                "stage": "pre_llm",
                "task_type": "workflow_role",
                "source": "workflow_role",
                "description": "default_chat_loop:router@pre_llm",
                "priority": 800,
                "timeout_seconds": 180,
                "max_attempts": 2,
                "idempotency_key": f"{session_id}:default_chat_loop:router@pre_llm",
                "payload": {
                    "session_id": session_id,
                    "parent_task_id": parent["task_id"],
                    "workflow_name": "default_chat_loop",
                    "workflow_identity": workflow_identity,
                    "tool_names": ["web_search"],
                    "needs_grounded": True,
                    "role": "router",
                    "role_label": "router",
                    "stage": "pre_llm",
                    "task_key": "router@pre_llm",
                    "checkpoint_id": "router@pre_llm",
                    "depends_on": [],
                    "retry_budget": 2,
                    "evidence_refs": [],
                    "evidence_snapshot": {"count": 0, "items": []},
                },
                "evidence_refs": [],
            },
            {
                "type": "workflow_role_task_bridge",
                "task_key": "executor@tool_phase",
                "role": "executor",
                "stage": "tool_phase",
                "task_type": "workflow_role",
                "source": "workflow_role",
                "description": "default_chat_loop:executor@tool_phase",
                "priority": 760,
                "timeout_seconds": 600,
                "max_attempts": 2,
                "idempotency_key": f"{session_id}:default_chat_loop:executor@tool_phase",
                "payload": {
                    "session_id": session_id,
                    "parent_task_id": parent["task_id"],
                    "workflow_name": "default_chat_loop",
                    "workflow_identity": workflow_identity,
                    "tool_names": ["web_search"],
                    "needs_grounded": True,
                    "role": "executor",
                    "role_label": "executor",
                    "stage": "tool_phase",
                    "task_key": "executor@tool_phase",
                    "resume_checkpoint_id": "router@pre_llm",
                    "depends_on": ["router@pre_llm"],
                    "retry_budget": 2,
                    "evidence_refs": [],
                    "evidence_snapshot": {"count": 0, "items": []},
                },
                "evidence_refs": [],
            },
            {
                "type": "workflow_role_task_bridge",
                "task_key": "summarizer@post_tools",
                "role": "summarizer",
                "stage": "post_tools",
                "task_type": "workflow_role",
                "source": "workflow_role",
                "description": "default_chat_loop:summarizer@post_tools",
                "priority": 680,
                "timeout_seconds": 180,
                "max_attempts": 1,
                "idempotency_key": f"{session_id}:default_chat_loop:summarizer@post_tools",
                "payload": {
                    "session_id": session_id,
                    "parent_task_id": parent["task_id"],
                    "workflow_name": "default_chat_loop",
                    "workflow_identity": workflow_identity,
                    "tool_names": ["web_search"],
                    "needs_grounded": True,
                    "role": "summarizer",
                    "role_label": "summarizer",
                    "stage": "post_tools",
                    "task_key": "summarizer@post_tools",
                    "checkpoint_id": "summarizer@post_tools",
                    "resume_checkpoint_id": "router@pre_llm",
                    "depends_on": ["critic@post_tools"],
                    "retry_budget": 1,
                    "evidence_refs": [],
                    "evidence_snapshot": {"count": 0, "items": []},
                },
                "evidence_refs": [],
            },
        ],
    )

    critic = await store.create_task(
        "default_chat_loop:critic@post_tools",
        task_type="workflow_role",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "critic",
            "role_label": "critic",
            "stage": "post_tools",
            "task_key": "critic@post_tools",
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "retry_budget": 1,
            "user_summary": "review the grounded answer",
            "evidence_refs": [],
            "evidence_snapshot": {"count": 0, "items": []},
        },
        source="workflow_role",
        session_id=session_id,
        priority=720,
        timeout_seconds=240,
        max_attempts=1,
        idempotency_key=f"{session_id}:default_chat_loop:critic@post_tools:live",
    )
    critic = await store.transition_task(critic["task_id"], "running")

    try:
        await spawn_module._run_background_task(critic)

        refreshed = await store.get_task(critic["task_id"])
        assert refreshed is not None
        assert refreshed["status"] == "succeeded"

        step = await store.get_task_step(critic["task_id"], spawn_module._ROLE_RUN_STEP_ID)
        assert step is not None
        output = step["output"]
        assert output["action"] == "critic_verdict:light_review"
        assert output["recovery_status"] == "requested"
        assert output["recovery_role"] == "executor"
        assert output["recovery_reason"] == "evidence_gap"
        assert output["recovery_task_id"]
        assert output["recovery_task_key"] == "executor@tool_phase"

        child_tasks = await store.list_child_tasks(parent["task_id"], source="workflow_role")
        executor_task = next(
            (
                item
                for item in child_tasks
                if str(item.get("payload", {}).get("task_key") or "") == "executor@tool_phase"
            ),
            None,
        )
        assert executor_task is not None
        assert executor_task["status"] == "pending"
        assert executor_task["idempotency_key"].endswith(":recovery:1")
        assert executor_task["payload"]["workflow_identity"] == workflow_identity
        assert executor_task["payload"]["resume_checkpoint_id"] == "router@pre_llm"
        assert executor_task["payload"]["recovery_task_key"] == "executor@tool_phase"
        assert executor_task["payload"]["resume_state"]["workflow_identity"] == workflow_identity
        assert executor_task["payload"]["recovery_state"]["failed_role"] == "critic"
        assert executor_task["payload"]["recovery_state"]["recovery_role"] == "executor"
        assert executor_task["payload"]["recovery_state"]["reason"] == "evidence_gap"
        assert (
            executor_task["payload"]["recovery_state"]["resume_checkpoint_id"]
            == "router@pre_llm"
        )
        assert (
            executor_task["payload"]["recovery_state"]["recovery_task_key"]
            == "executor@tool_phase"
        )
        assert "Role recovery. Critic found no grounded evidence." in (
            executor_task["payload"]["execution_brief"]
        )

        assert not any(
            str(item.get("payload", {}).get("task_key") or "") == "summarizer@post_tools"
            for item in child_tasks
        )
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_background_role_task_defers_running_executor_recovery_until_current_turn_finishes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A running executor should stage recovery refresh and rearm after the current turn ends."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    set_gateway(FakeGateway())
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    monkeypatch.setattr(spawn_module, "_schedule_queue_drain", lambda: None)
    parent = await store.create_task("parent workflow task", source="spawn_task")
    session_id = f"task:{parent['task_id']}"
    workflow_identity = "workflow_recovery_executor_running"

    await _log_role_bridge_run(
        audit,
        session_id=session_id,
        call_chain=[
            _build_role_bridge_item(
                session_id=session_id,
                parent_task_id=parent["task_id"],
                task_key="executor@tool_phase",
                role="executor",
                stage="tool_phase",
                priority=760,
                timeout_seconds=600,
                max_attempts=2,
                resume_checkpoint_id="router@pre_llm",
                depends_on=["router@pre_llm"],
                workflow_identity=workflow_identity,
            ),
        ],
        user_summary="defer running executor recovery path",
    )

    await _create_succeeded_role_task(
        store,
        session_id=session_id,
        parent_task_id=parent["task_id"],
        task_key="router@pre_llm",
        workflow_name="default_chat_loop",
        priority=800,
        timeout_seconds=180,
        max_attempts=2,
        description="default_chat_loop:router@pre_llm",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "router",
            "role_label": "router",
            "stage": "pre_llm",
            "task_key": "router@pre_llm",
            "checkpoint_id": "router@pre_llm",
            "turn_index": 1,
            "turn_budget": 1,
            "turn_reason": "initial",
            "turn_history": [],
            "upstream_input_fingerprint": "router_running_refresh",
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "evidence_refs": ["ev_router"],
            "evidence_snapshot": {
                "count": 1,
                "items": [
                    {
                        "evidence_id": "ev_router",
                        "tool_name": "web_search",
                        "url": "https://example.com/router",
                    }
                ],
            },
        },
        output={
            "status": "executed",
            "scheduler_action": "complete",
            "result_text": "default_chat_loop:router@pre_llm -> route_selected:grounded",
            "role": "router",
            "role_label": "router",
            "action": "route_selected:grounded",
            "evidence_count": 1,
            "evidence_refs": ["ev_router"],
            "turn_index": 1,
            "turn_budget": 1,
            "turn_reason": "initial",
            "upstream_input_fingerprint": "router_running_refresh",
        },
    )

    executor = await store.create_task(
        "default_chat_loop:executor@tool_phase",
        task_type="workflow_role",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "executor",
            "role_label": "executor",
            "stage": "tool_phase",
            "task_key": "executor@tool_phase",
            "resume_checkpoint_id": "router@pre_llm",
            "depends_on": ["router@pre_llm"],
            "turn_index": 1,
            "turn_budget": 2,
            "turn_reason": "initial",
            "turn_history": [],
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "evidence_refs": [],
            "evidence_snapshot": {"count": 0, "items": []},
        },
        source="workflow_role",
        session_id=session_id,
        priority=760,
        timeout_seconds=600,
        max_attempts=2,
        idempotency_key=f"{session_id}:default_chat_loop:executor@tool_phase:running",
    )
    executor = await store.transition_task(executor["task_id"], "running")

    critic = await store.create_task(
        "default_chat_loop:critic@post_tools",
        task_type="workflow_role",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "critic",
            "role_label": "critic",
            "stage": "post_tools",
            "task_key": "critic@post_tools",
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "retry_budget": 1,
            "user_summary": "review the grounded answer",
            "evidence_refs": [],
            "evidence_snapshot": {"count": 0, "items": []},
        },
        source="workflow_role",
        session_id=session_id,
        priority=720,
        timeout_seconds=240,
        max_attempts=1,
        idempotency_key=f"{session_id}:default_chat_loop:critic@post_tools:live-running",
    )
    critic = await store.transition_task(critic["task_id"], "running")

    try:
        await spawn_module._run_background_task(critic)

        refreshed_running = await store.get_task(executor["task_id"])
        assert refreshed_running is not None
        assert refreshed_running["status"] == "running"
        deferred = refreshed_running["payload"]["deferred_recovery_payload"]
        assert deferred["recovery_task_key"] == "executor@tool_phase"
        assert deferred["recovery_state"]["failed_role"] == "critic"
        assert deferred["recovery_state"]["reason"] == "evidence_gap"

        await spawn_module._run_background_task(refreshed_running)

        rearmed = await store.get_task(executor["task_id"])
        assert rearmed is not None
        assert rearmed["status"] == "pending"
        assert rearmed["payload"]["turn_index"] == 2
        assert rearmed["payload"]["turn_budget"] == 2
        assert rearmed["payload"]["turn_reason"] == "recovery_reentry"
        assert rearmed["payload"]["turn_history"][0]["turn_index"] == 1
        assert "deferred_recovery_payload" not in rearmed["payload"]

        child_tasks = await store.list_child_tasks(parent["task_id"], source="workflow_role")
        matching = [
            item
            for item in child_tasks
            if str(item.get("payload", {}).get("task_key") or "") == "executor@tool_phase"
        ]
        assert len(matching) == 1
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_background_role_task_refreshes_pending_executor_recovery_in_place(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery should refresh one pending executor task payload instead of spawning a duplicate."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    set_gateway(FakeGateway())
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    monkeypatch.setattr(spawn_module, "_schedule_queue_drain", lambda: None)
    parent = await store.create_task("parent workflow task", source="spawn_task")
    session_id = f"task:{parent['task_id']}"
    workflow_identity = "workflow_recovery_executor_pending"

    await _log_role_bridge_run(
        audit,
        session_id=session_id,
        call_chain=[
            _build_role_bridge_item(
                session_id=session_id,
                parent_task_id=parent["task_id"],
                task_key="executor@tool_phase",
                role="executor",
                stage="tool_phase",
                priority=760,
                timeout_seconds=600,
                max_attempts=2,
                resume_checkpoint_id="router@pre_llm",
                depends_on=["router@pre_llm"],
                workflow_identity=workflow_identity,
            ),
        ],
        user_summary="refresh pending executor recovery path",
    )

    await _create_succeeded_role_task(
        store,
        session_id=session_id,
        parent_task_id=parent["task_id"],
        task_key="router@pre_llm",
        workflow_name="default_chat_loop",
        priority=800,
        timeout_seconds=180,
        max_attempts=2,
        description="default_chat_loop:router@pre_llm",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "router",
            "role_label": "router",
            "stage": "pre_llm",
            "task_key": "router@pre_llm",
            "checkpoint_id": "router@pre_llm",
            "turn_index": 1,
            "turn_budget": 1,
            "turn_reason": "initial",
            "turn_history": [],
            "upstream_input_fingerprint": "router_pending_refresh",
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "evidence_refs": ["ev_router"],
            "evidence_snapshot": {
                "count": 1,
                "items": [
                    {
                        "evidence_id": "ev_router",
                        "tool_name": "web_search",
                        "url": "https://example.com/router",
                    }
                ],
            },
        },
        output={
            "status": "executed",
            "scheduler_action": "complete",
            "result_text": "default_chat_loop:router@pre_llm -> route_selected:grounded",
            "role": "router",
            "role_label": "router",
            "action": "route_selected:grounded",
            "evidence_count": 1,
            "evidence_refs": ["ev_router"],
            "turn_index": 1,
            "turn_budget": 1,
            "turn_reason": "initial",
            "upstream_input_fingerprint": "router_pending_refresh",
        },
    )

    executor = await store.create_task(
        "default_chat_loop:executor@tool_phase",
        task_type="workflow_role",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "executor",
            "role_label": "executor",
            "stage": "tool_phase",
            "task_key": "executor@tool_phase",
            "resume_checkpoint_id": "router@pre_llm",
            "depends_on": ["router@pre_llm"],
            "turn_index": 1,
            "turn_budget": 2,
            "turn_reason": "initial",
            "turn_history": [],
            "upstream_input_fingerprint": "executor_pending_initial",
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "execution_brief": "Executor pending initial brief.",
            "evidence_refs": [],
            "evidence_snapshot": {"count": 0, "items": []},
        },
        source="workflow_role",
        session_id=session_id,
        priority=760,
        timeout_seconds=600,
        max_attempts=2,
        idempotency_key=f"{session_id}:default_chat_loop:executor@tool_phase",
    )

    critic = await store.create_task(
        "default_chat_loop:critic@post_tools",
        task_type="workflow_role",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "critic",
            "role_label": "critic",
            "stage": "post_tools",
            "task_key": "critic@post_tools",
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "retry_budget": 1,
            "user_summary": "review the grounded answer",
            "evidence_refs": [],
            "evidence_snapshot": {"count": 0, "items": []},
        },
        source="workflow_role",
        session_id=session_id,
        priority=720,
        timeout_seconds=240,
        max_attempts=1,
        idempotency_key=f"{session_id}:default_chat_loop:critic@post_tools:live",
    )
    critic = await store.transition_task(critic["task_id"], "running")

    try:
        await spawn_module._run_background_task(critic)

        critic_step = await store.get_task_step(critic["task_id"], spawn_module._ROLE_RUN_STEP_ID)
        assert critic_step is not None
        critic_output = critic_step["output"]
        assert critic_output["recovery_status"] == "requested"
        assert critic_output["recovery_task_id"] == executor["task_id"]
        assert critic_output["recovery_task_key"] == "executor@tool_phase"

        child_tasks = await store.list_child_tasks(parent["task_id"], source="workflow_role")
        matching = [
            item
            for item in child_tasks
            if str(item.get("payload", {}).get("task_key") or "") == "executor@tool_phase"
        ]
        assert len(matching) == 1
        refreshed = matching[0]
        assert refreshed["task_id"] == executor["task_id"]
        assert refreshed["status"] == "pending"
        assert not refreshed["idempotency_key"].endswith(":recovery:1")
        assert refreshed["payload"]["turn_index"] == 1
        assert refreshed["payload"]["turn_budget"] == 2
        assert refreshed["payload"]["turn_reason"] == "recovery_refresh"
        assert refreshed["payload"]["recovery_task_key"] == "executor@tool_phase"
        assert refreshed["payload"]["recovery_state"]["failed_role"] == "critic"
        assert refreshed["payload"]["recovery_state"]["recovery_role"] == "executor"
        assert refreshed["payload"]["recovery_state"]["reason"] == "evidence_gap"
        assert "Role recovery. Critic found no grounded evidence." in (
            refreshed["payload"]["execution_brief"]
        )

        refreshed = await store.transition_task(refreshed["task_id"], "running")
        await spawn_module._run_background_task(refreshed)
        step = await store.get_task_step(executor["task_id"], spawn_module._ROLE_RUN_STEP_ID)
        assert step is not None
        output = step["output"]
        assert output["scheduler_action"] == "rearmed_recovery"
        assert output["turn_index"] == 1
        assert output["turn_budget"] == 2
        assert output["turn_reason"] == "recovery_refresh"
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_background_role_task_rearms_succeeded_executor_for_recovery_reentry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovery should rearm one succeeded executor task instead of creating a duplicate row."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    set_gateway(FakeGateway())
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    monkeypatch.setattr(spawn_module, "_schedule_queue_drain", lambda: None)
    parent = await store.create_task("parent workflow task", source="spawn_task")
    session_id = f"task:{parent['task_id']}"
    workflow_identity = "workflow_recovery_executor_rearm"

    await _log_role_bridge_run(
        audit,
        session_id=session_id,
        call_chain=[
            _build_role_bridge_item(
                session_id=session_id,
                parent_task_id=parent["task_id"],
                task_key="executor@tool_phase",
                role="executor",
                stage="tool_phase",
                priority=760,
                timeout_seconds=600,
                max_attempts=2,
                resume_checkpoint_id="router@pre_llm",
                depends_on=["router@pre_llm"],
                workflow_identity=workflow_identity,
            ),
        ],
        user_summary="rearm executor recovery path",
    )

    await _create_succeeded_role_task(
        store,
        session_id=session_id,
        parent_task_id=parent["task_id"],
        task_key="router@pre_llm",
        workflow_name="default_chat_loop",
        priority=800,
        timeout_seconds=180,
        max_attempts=2,
        description="default_chat_loop:router@pre_llm",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "router",
            "role_label": "router",
            "stage": "pre_llm",
            "task_key": "router@pre_llm",
            "checkpoint_id": "router@pre_llm",
            "turn_index": 1,
            "turn_budget": 1,
            "turn_reason": "initial",
            "turn_history": [],
            "upstream_input_fingerprint": "router_first_turn",
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "evidence_refs": ["ev_router"],
            "evidence_snapshot": {
                "count": 1,
                "items": [
                    {
                        "evidence_id": "ev_router",
                        "tool_name": "web_search",
                        "url": "https://example.com/router",
                    }
                ],
            },
        },
        output={
            "status": "executed",
            "scheduler_action": "complete",
            "result_text": "default_chat_loop:router@pre_llm -> route_selected:grounded",
            "role": "router",
            "role_label": "router",
            "action": "route_selected:grounded",
            "evidence_count": 1,
            "evidence_refs": ["ev_router"],
            "turn_index": 1,
            "turn_budget": 1,
            "turn_reason": "initial",
            "upstream_input_fingerprint": "router_first_turn",
        },
    )

    executor = await _create_succeeded_role_task(
        store,
        session_id=session_id,
        parent_task_id=parent["task_id"],
        task_key="executor@tool_phase",
        workflow_name="default_chat_loop",
        priority=760,
        timeout_seconds=600,
        max_attempts=2,
        description="default_chat_loop:executor@tool_phase",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "executor",
            "role_label": "executor",
            "stage": "tool_phase",
            "task_key": "executor@tool_phase",
            "resume_checkpoint_id": "router@pre_llm",
            "depends_on": ["router@pre_llm"],
            "turn_index": 1,
            "turn_budget": 2,
            "turn_reason": "initial",
            "turn_history": [],
            "upstream_input_fingerprint": "executor_first_turn",
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "evidence_refs": ["ev_executor"],
            "evidence_snapshot": {
                "count": 1,
                "items": [
                    {
                        "evidence_id": "ev_executor",
                        "tool_name": "web_search",
                        "url": "https://example.com/executor",
                    }
                ],
            },
        },
        output={
            "status": "executed",
            "scheduler_action": "complete",
            "result_text": "default_chat_loop:executor@tool_phase -> shared_evidence_reused",
            "role": "executor",
            "role_label": "executor",
            "action": "shared_evidence_reused",
            "evidence_count": 1,
            "evidence_refs": ["ev_executor"],
            "turn_index": 1,
            "turn_budget": 2,
            "turn_reason": "initial",
            "upstream_input_fingerprint": "executor_first_turn",
        },
    )

    critic = await store.create_task(
        "default_chat_loop:critic@post_tools",
        task_type="workflow_role",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "workflow_identity": workflow_identity,
            "role": "critic",
            "role_label": "critic",
            "stage": "post_tools",
            "task_key": "critic@post_tools",
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "retry_budget": 1,
            "user_summary": "review the grounded answer",
            "evidence_refs": [],
            "evidence_snapshot": {"count": 0, "items": []},
        },
        source="workflow_role",
        session_id=session_id,
        priority=720,
        timeout_seconds=240,
        max_attempts=1,
        idempotency_key=f"{session_id}:default_chat_loop:critic@post_tools:live",
    )
    critic = await store.transition_task(critic["task_id"], "running")

    try:
        await spawn_module._run_background_task(critic)

        critic_step = await store.get_task_step(critic["task_id"], spawn_module._ROLE_RUN_STEP_ID)
        assert critic_step is not None
        critic_output = critic_step["output"]
        assert critic_output["recovery_status"] == "requested"
        assert critic_output["recovery_task_id"] == executor["task_id"]
        assert critic_output["recovery_task_key"] == "executor@tool_phase"

        child_tasks = await store.list_child_tasks(parent["task_id"], source="workflow_role")
        matching = [
            item
            for item in child_tasks
            if str(item.get("payload", {}).get("task_key") or "") == "executor@tool_phase"
        ]
        assert len(matching) == 1
        rearmed = matching[0]
        assert rearmed["task_id"] == executor["task_id"]
        assert rearmed["status"] == "pending"
        assert not rearmed["idempotency_key"].endswith(":recovery:1")
        assert rearmed["payload"]["turn_index"] == 2
        assert rearmed["payload"]["turn_budget"] == 2
        assert rearmed["payload"]["turn_reason"] == "recovery_reentry"
        assert rearmed["payload"]["turn_history"][0]["action"] == "shared_evidence_reused"
        assert rearmed["payload"]["recovery_task_key"] == "executor@tool_phase"
        assert rearmed["payload"]["recovery_state"]["failed_role"] == "critic"
        assert rearmed["payload"]["recovery_state"]["recovery_role"] == "executor"
        assert rearmed["payload"]["recovery_state"]["recovery_path"] == [
            "executor@tool_phase",
            "critic@post_tools",
            "summarizer@post_tools",
        ]

        rearmed = await store.transition_task(rearmed["task_id"], "running")
        await spawn_module._run_background_task(rearmed)
        refreshed = await store.get_task(executor["task_id"])
        assert refreshed is not None
        assert refreshed["status"] == "succeeded"
        step = await store.get_task_step(executor["task_id"], spawn_module._ROLE_RUN_STEP_ID)
        assert step is not None
        output = step["output"]
        assert output["scheduler_action"] == "rearmed_recovery"
        assert output["turn_index"] == 2
        assert output["turn_budget"] == 2
        assert output["turn_reason"] == "recovery_reentry"
        assert output["turn_history"][0]["turn_index"] == 1
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_background_role_task_routes_summarizer_recovery_to_executor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grounded summarizer gap should re-enter runtime at the executor role."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    set_gateway(FakeGateway())
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    monkeypatch.setattr(spawn_module, "_schedule_queue_drain", lambda: None)
    parent = await store.create_task("parent workflow task", source="spawn_task")
    session_id = f"task:{parent['task_id']}"

    await _log_role_bridge_run(
        audit,
        session_id=session_id,
        call_chain=[
            _build_role_bridge_item(
                session_id=session_id,
                parent_task_id=parent["task_id"],
                task_key="executor@tool_phase",
                role="executor",
                stage="tool_phase",
                priority=760,
                timeout_seconds=600,
                max_attempts=2,
                resume_checkpoint_id="router@pre_llm",
                depends_on=["router@pre_llm"],
            ),
        ],
        user_summary="summarizer runtime recovery path",
    )

    summarizer = await store.create_task(
        "default_chat_loop:summarizer@post_tools",
        task_type="workflow_role",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "role": "summarizer",
            "role_label": "summarizer",
            "stage": "post_tools",
            "task_key": "summarizer@post_tools",
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "retry_budget": 1,
            "user_summary": "prepare the grounded final answer",
            "evidence_refs": [],
            "evidence_snapshot": {"count": 0, "items": []},
        },
        source="workflow_role",
        session_id=session_id,
        priority=680,
        timeout_seconds=180,
        max_attempts=1,
        idempotency_key=f"{session_id}:default_chat_loop:summarizer@post_tools:live",
    )
    summarizer = await store.transition_task(summarizer["task_id"], "running")

    try:
        await spawn_module._run_background_task(summarizer)

        step = await store.get_task_step(summarizer["task_id"], spawn_module._ROLE_RUN_STEP_ID)
        assert step is not None
        output = step["output"]
        assert output["recovery_status"] == "requested"
        assert output["recovery_role"] == "executor"
        assert output["recovery_reason"] == "grounded_search_required"
        assert output["recovery_task_key"] == "executor@tool_phase"
        assert output["resume_checkpoint_id"] == ""

        child_tasks = await store.list_child_tasks(parent["task_id"], source="workflow_role")
        executor_task = next(
            (
                item
                for item in child_tasks
                if str(item.get("payload", {}).get("task_key") or "") == "executor@tool_phase"
            ),
            None,
        )
        assert executor_task is not None
        assert executor_task["status"] == "pending"
        assert executor_task["payload"]["resume_checkpoint_id"] == "router@pre_llm"
        assert executor_task["payload"]["recovery_task_key"] == "executor@tool_phase"
        assert executor_task["payload"]["recovery_state"]["failed_role"] == "summarizer"
        assert executor_task["payload"]["recovery_state"]["recovery_role"] == "executor"
        assert executor_task["payload"]["recovery_state"]["reason"] == "grounded_search_required"
        assert (
            executor_task["payload"]["recovery_state"]["resume_checkpoint_id"]
            == "router@pre_llm"
        )
        assert (
            executor_task["payload"]["recovery_state"]["recovery_task_key"]
            == "executor@tool_phase"
        )
        assert not any(
            str(item.get("payload", {}).get("task_key") or "") == "router@pre_llm"
            for item in child_tasks
        )
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_background_role_task_routes_tool_handler_failures_back_to_router(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow role tool-handler error should recover through the router role."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    set_gateway(FakeGateway())
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    monkeypatch.setattr(spawn_module, "_schedule_queue_drain", lambda: None)
    parent = await store.create_task("parent workflow task", source="spawn_task")
    session_id = f"task:{parent['task_id']}"
    workflow_identity = "workflow_recovery_router"

    async def _broken_article_assist(**kwargs: Any) -> str:
        raise RuntimeError("handler boom")

    monkeypatch.setattr(
        "nanoclaw.tools.web_workflows.wechat_article_assist",
        _broken_article_assist,
    )
    await _log_role_bridge_run(
        audit,
        session_id=session_id,
        workflow_name="wechat_article_flow",
        user_summary="article handler recovery path",
        call_chain=[
            _build_role_bridge_item(
                session_id=session_id,
                parent_task_id=parent["task_id"],
                task_key="router@pre_llm",
                role="router",
                stage="pre_llm",
                priority=800,
                timeout_seconds=180,
                max_attempts=2,
                workflow_name="wechat_article_flow",
                tool_names=["wechat_article_assist"],
                checkpoint_id="router@pre_llm",
                workflow_identity=workflow_identity,
            ),
        ],
    )

    summarizer = await store.create_task(
        "wechat_article_flow:summarizer@post_tools",
        task_type="workflow_role",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "wechat_article_flow",
            "workflow_identity": workflow_identity,
            "role": "summarizer",
            "role_label": "editor",
            "role_stage_name": "editor",
            "role_tool_enabled": True,
            "stage": "post_tools",
            "task_key": "summarizer@post_tools",
            "tool_names": ["wechat_article_assist"],
            "needs_grounded": True,
            "retry_budget": 1,
            "user_summary": "write the publish-ready article bundle",
            "evidence_refs": [],
            "evidence_snapshot": {"count": 0, "items": []},
        },
        source="workflow_role",
        session_id=session_id,
        priority=680,
        timeout_seconds=180,
        max_attempts=1,
        idempotency_key=f"{session_id}:wechat_article_flow:summarizer@post_tools:live",
    )
    summarizer = await store.transition_task(summarizer["task_id"], "running")

    try:
        await spawn_module._run_background_task(summarizer)

        step = await store.get_task_step(summarizer["task_id"], spawn_module._ROLE_RUN_STEP_ID)
        assert step is not None
        output = step["output"]
        assert output["tool_handler_status"] == "error"
        assert output["recovery_status"] == "requested"
        assert output["recovery_role"] == "router"
        assert output["recovery_reason"] == "wechat_article_assist:error"
        assert output["recovery_task_key"] == "router@pre_llm"

        child_tasks = await store.list_child_tasks(parent["task_id"], source="workflow_role")
        router_task = next(
            (
                item
                for item in child_tasks
                if str(item.get("payload", {}).get("task_key") or "") == "router@pre_llm"
            ),
            None,
        )
        assert router_task is not None
        assert router_task["status"] == "pending"
        assert router_task["payload"]["workflow_identity"] == workflow_identity
        assert router_task["payload"]["resume_checkpoint_id"] == "router@pre_llm"
        assert router_task["payload"]["recovery_task_key"] == "router@pre_llm"
        assert router_task["payload"]["resume_state"]["workflow_identity"] == workflow_identity
        assert router_task["payload"]["recovery_state"]["failed_role"] == "executor"
        assert router_task["payload"]["recovery_state"]["recovery_role"] == "router"
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_background_role_task_uses_legacy_resume_checkpoint_when_task_key_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Legacy recovery payloads without `recovery_task_key` should still route by checkpoint."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    set_gateway(FakeGateway())
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    monkeypatch.setattr(spawn_module, "_schedule_queue_drain", lambda: None)
    parent = await store.create_task("parent workflow task", source="spawn_task")
    session_id = f"task:{parent['task_id']}"

    monkeypatch.setattr(
        spawn_module,
        "_build_runtime_role_recovery_action",
        lambda payload, result: {
            "failed_role": "critic",
            "recovery_role": "router",
            "stage": "post_tools",
            "reason": "legacy_evidence_gap",
            "resume_checkpoint_id": "router@pre_llm",
            "content": "[Internal: Legacy role recovery.]",
            "evidence_refs": [],
        },
    )
    await _log_role_bridge_run(
        audit,
        session_id=session_id,
        call_chain=[
            _build_role_bridge_item(
                session_id=session_id,
                parent_task_id=parent["task_id"],
                task_key="router@pre_llm",
                role="router",
                stage="pre_llm",
                priority=800,
                timeout_seconds=180,
                max_attempts=2,
                checkpoint_id="router@pre_llm",
            ),
        ],
        user_summary="legacy recovery path",
    )

    critic = await store.create_task(
        "default_chat_loop:critic@post_tools",
        task_type="workflow_role",
        payload={
            "session_id": session_id,
            "parent_task_id": parent["task_id"],
            "workflow_name": "default_chat_loop",
            "role": "critic",
            "role_label": "critic",
            "stage": "post_tools",
            "task_key": "critic@post_tools",
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "retry_budget": 1,
            "user_summary": "legacy recovery request",
            "evidence_refs": [],
            "evidence_snapshot": {"count": 0, "items": []},
        },
        source="workflow_role",
        session_id=session_id,
        priority=720,
        timeout_seconds=240,
        max_attempts=1,
        idempotency_key=f"{session_id}:default_chat_loop:critic@post_tools:legacy",
    )
    critic = await store.transition_task(critic["task_id"], "running")

    try:
        await spawn_module._run_background_task(critic)

        step = await store.get_task_step(critic["task_id"], spawn_module._ROLE_RUN_STEP_ID)
        assert step is not None
        output = step["output"]
        assert output["recovery_status"] == "requested"
        assert output["recovery_task_key"] == "router@pre_llm"

        child_tasks = await store.list_child_tasks(parent["task_id"], source="workflow_role")
        router_task = next(
            (
                item
                for item in child_tasks
                if str(item.get("payload", {}).get("task_key") or "") == "router@pre_llm"
            ),
            None,
        )
        assert router_task is not None
        assert router_task["status"] == "pending"
        assert router_task["payload"]["resume_checkpoint_id"] == "router@pre_llm"
        assert router_task["payload"]["recovery_task_key"] == "router@pre_llm"
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_background_runtime_executes_workflow_role_task(tmp_path: Path) -> None:
    """Shared runtime should claim and complete workflow_role tasks."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)
    set_gateway(FakeGateway())

    created = await store.create_task(
        "default_chat_loop:planner@pre_llm",
        task_type="workflow_role",
        payload={
            "session_id": "task:parent_1",
            "parent_task_id": "task_parent_1",
            "workflow_name": "default_chat_loop",
            "role": "planner",
            "stage": "pre_llm",
            "task_key": "planner@pre_llm",
            "user_summary": "bridge planner",
        },
        source="workflow_role",
        session_id="task:parent_1",
        priority=820,
        timeout_seconds=600,
        max_attempts=2,
        idempotency_key="task:parent_1:default_chat_loop:planner@pre_llm",
    )

    try:
        await spawn_module.start_background_runtime()
        statuses = await _wait_for_status(
            store,
            {"default_chat_loop:planner@pre_llm": "succeeded"},
            timeout=1.0,
        )
        assert statuses["default_chat_loop:planner@pre_llm"] == "succeeded"
        steps = await store.list_task_steps(created["task_id"])
        assert steps[0]["step_id"] == spawn_module._ROLE_RUN_STEP_ID
        assert steps[0]["output"]["status"] == "executed"
        assert steps[0]["output"]["task_key"] == "planner@pre_llm"
        assert steps[0]["output"]["action"] == "stable_plan_ready"
        assert steps[0]["output"]["handler_kind"] == "execution_brief"
        assert "Planner phase" in steps[0]["output"]["brief_content"]
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_background_runtime_role_task_reports_resume_budget(tmp_path: Path) -> None:
    """workflow_role steps should expose role budget and resumed evidence metadata."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)
    set_gateway(FakeGateway())

    created = await store.create_task(
        "default_chat_loop:router@pre_llm",
        task_type="workflow_role",
        payload={
            "session_id": "task:parent_resume",
            "parent_task_id": "task_parent_resume",
            "workflow_name": "default_chat_loop",
            "role": "router",
            "role_label": "router",
            "stage": "pre_llm",
            "task_key": "router@pre_llm",
            "user_summary": "resume router",
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "retry_budget": 2,
            "execution_brief": "[Internal: Router phase. Reuse grounded evidence first.]",
            "resume_state": {
                "source_workflow_run_id": 42,
                "workflow_name": "default_chat_loop",
                "workflow_status": "degraded",
                "failure_reason": "web_search:error",
                "resume_checkpoint_id": "router@pre_llm",
                "role": "router",
                "stage": "pre_llm",
                "evidence_refs": ["ev_resume"],
                "evidence_snapshot": {
                    "count": 1,
                    "tools": ["web_search"],
                    "items": [
                        {
                            "evidence_id": "ev_resume",
                            "tool_name": "web_search",
                            "url": "https://example.com/resume",
                            "title": "Resume evidence",
                        }
                    ],
                },
            },
        },
        source="workflow_role",
        session_id="task:parent_resume",
        priority=800,
        timeout_seconds=600,
        max_attempts=2,
        idempotency_key="task:parent_resume:default_chat_loop:router@pre_llm",
    )

    try:
        await spawn_module.start_background_runtime()
        statuses = await _wait_for_status(
            store,
            {"default_chat_loop:router@pre_llm": "succeeded"},
            timeout=1.0,
        )
        assert statuses["default_chat_loop:router@pre_llm"] == "succeeded"
        steps = await store.list_task_steps(created["task_id"])
        output = steps[0]["output"]
        assert output["attempt_number"] == 1
        assert output["budget_limit"] == 2
        assert output["remaining_budget"] == 1
        assert output["resume_status"] == "resumed"
        assert output["resume_checkpoint_id"] == "router@pre_llm"
        assert output["resume_source_workflow_run_id"] == 42
        assert output["resume_restored_evidence_count"] == 1
        assert output["resume_evidence_refs"] == ["ev_resume"]
        assert output["evidence_count"] == 1
        assert output["action"] == "route_selected:grounded"
        assert "Resume from persisted role checkpoint" in output["brief_content"]
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_background_runtime_executes_isolated_role_llm_turn(tmp_path: Path) -> None:
    """workflow_role tasks should support one isolated role LLM turn."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)
    set_gateway(FakeGateway())
    fake_llm = FakeRoleLLM(
        (
            '{"role_summary":"Planner locked a grounded execution outline.",'
            '"artifact_preview":"Plan ready: verify the claim, reuse evidence, then hand off.",'
            '"result_text":"default_chat_loop:planner@pre_llm -> role_llm_turn"}'
        )
    )
    spawn_module.set_role_runtime_llm(fake_llm)

    created = await store.create_task(
        "default_chat_loop:planner@pre_llm",
        task_type="workflow_role",
        payload={
            "session_id": "task:parent_llm",
            "parent_task_id": "task_parent_llm",
            "workflow_name": "default_chat_loop",
            "role": "planner",
            "role_label": "planner",
            "stage": "pre_llm",
            "task_key": "planner@pre_llm",
            "user_summary": "bridge planner",
            "tool_names": ["web_search"],
            "needs_grounded": True,
            "execution_brief": "[Internal: Planner phase. Build a grounded plan first.]",
            "handoff_contract": {
                "workflow_name": "default_chat_loop",
                "needs_grounded": True,
                "response_mode": "grounded_answer",
            },
        },
        source="workflow_role",
        session_id="task:parent_llm",
        priority=820,
        timeout_seconds=600,
        max_attempts=2,
        idempotency_key="task:parent_llm:default_chat_loop:planner@pre_llm",
    )

    try:
        await spawn_module.start_background_runtime()
        statuses = await _wait_for_status(
            store,
            {"default_chat_loop:planner@pre_llm": "succeeded"},
            timeout=1.0,
        )
        assert statuses["default_chat_loop:planner@pre_llm"] == "succeeded"
        steps = await store.list_task_steps(created["task_id"])
        assert steps[0]["output"]["action"] == "stable_plan_ready"
        assert steps[0]["output"]["handler_kind"] == "role_llm_turn"
        assert steps[0]["output"]["role_summary"] == "Planner locked a grounded execution outline."
        assert steps[0]["output"]["artifact_preview"].startswith("Plan ready:")
        assert steps[0]["output"]["result_text"].endswith("role_llm_turn")
        assert steps[0]["output"]["llm_status"] == "success"
        assert steps[0]["output"]["llm_model"] == "fake-role-llm"
        assert steps[0]["output"]["llm_tokens"] == 28
        assert fake_llm.calls
        assert fake_llm.calls[0][0]["role"] == "system"
        assert fake_llm.calls[0][1]["role"] == "user"
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_background_runtime_executes_article_workflow_role_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Article workflow role tasks should expose article-facing runtime identity."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)
    set_gateway(FakeGateway())
    captured_kwargs: list[dict[str, Any]] = []

    async def _fake_wechat_article_assist(**kwargs: Any) -> str:
        captured_kwargs.append(dict(kwargs))
        return f"## {kwargs['stage']}\narticle role output"

    monkeypatch.setattr(
        "nanoclaw.tools.web_workflows.wechat_article_assist",
        _fake_wechat_article_assist,
    )

    upstream = await store.create_task(
        "wechat_article_flow:critic@post_tools",
        task_type="workflow_role",
        payload={
            "parent_task_id": "task_article_parent",
            "task_key": "critic@post_tools",
        },
        source="workflow_role",
        session_id="task:article_parent",
    )
    await store.transition_task(upstream["task_id"], "running")
    await store.transition_task(upstream["task_id"], "succeeded")
    await store.start_task_step(
        upstream["task_id"],
        spawn_module._ROLE_RUN_STEP_ID,
        step_name="role_runtime_ack",
        input_payload={"task_key": "critic@post_tools"},
        is_checkpoint=True,
        idempotent=True,
    )
    await store.complete_task_step(
        upstream["task_id"],
        spawn_module._ROLE_RUN_STEP_ID,
        output_payload={
            "task_key": "critic@post_tools",
            "action": "article_gate:publish_ready",
            "tool_handler_output_preview": "## critic",
        },
    )
    created = await store.create_task(
        "wechat_article_flow:summarizer@post_tools",
        task_type="workflow_role",
        payload={
            "session_id": "task:article_parent",
            "parent_task_id": "task_article_parent",
            "workflow_name": "wechat_article_flow",
            "role": "summarizer",
            "role_label": "editor",
            "role_focus": "prepare the publish-ready article bundle",
            "role_stage_name": "editor",
            "role_tool_enabled": True,
            "stage": "post_tools",
            "task_key": "summarizer@post_tools",
            "depends_on": ["critic@post_tools"],
            "user_summary": "写一篇视频生成模型加速周报",
            "tool_names": ["wechat_article_assist"],
            "needs_grounded": True,
            "evidence_refs": ["ev_1"],
            "evidence_snapshot": {
                "count": 1,
                "items": [{"evidence_id": "ev_1"}],
            },
        },
        source="workflow_role",
        session_id="task:article_parent",
        priority=680,
        timeout_seconds=600,
        max_attempts=2,
        idempotency_key="task:article_parent:wechat_article_flow:summarizer@post_tools",
    )

    try:
        await spawn_module.start_background_runtime()
        statuses = await _wait_for_status(
            store,
            {"wechat_article_flow:summarizer@post_tools": "succeeded"},
            timeout=1.0,
        )
        assert statuses["wechat_article_flow:summarizer@post_tools"] == "succeeded"
        steps = await store.list_task_steps(created["task_id"])
        assert steps[0]["output"]["action"] == "article_bundle_ready"
        assert steps[0]["output"]["role_label"] == "editor"
        assert steps[0]["output"]["resolved_dependencies"] == ["critic@post_tools"]
        assert steps[0]["output"]["upstream_actions"] == ["article_gate:publish_ready"]
        assert "Publish bundle ready" in steps[0]["output"]["artifact_preview"]
        assert "Inputs: article_gate:publish_ready." in steps[0]["output"]["artifact_preview"]
        assert "Editor phase" in steps[0]["output"]["brief_content"]
        assert steps[0]["output"]["tool_handler_name"] == "wechat_article_assist"
        assert steps[0]["output"]["tool_handler_stage"] == "editor"
        assert steps[0]["output"]["tool_handler_status"] == "success"
        assert steps[0]["output"]["tool_handler_output_preview"] == "## editor"
        assert steps[0]["output"]["upstream_artifacts"] == ["## critic"]
        assert "## critic" in captured_kwargs[0]["evidence"]
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_workflow_role_task_defers_until_dependencies_finish(tmp_path: Path) -> None:
    """workflow_role tasks should defer when required sibling roles are not ready yet."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)
    set_gateway(FakeGateway())

    created = await store.create_task(
        "default_chat_loop:critic@post_tools",
        task_type="workflow_role",
        payload={
            "session_id": "task:parent_2",
            "parent_task_id": "task_parent_2",
            "workflow_name": "default_chat_loop",
            "role": "critic",
            "stage": "post_tools",
            "task_key": "critic@post_tools",
            "depends_on": ["executor@tool_phase"],
            "evidence_refs": ["ev_1"],
            "evidence_snapshot": {
                "count": 1,
                "items": [
                    {
                        "evidence_id": "ev_1",
                        "tool_name": "web_search",
                        "url": "https://example.com/a",
                    }
                ],
            },
        },
        source="workflow_role",
        session_id="task:parent_2",
        priority=720,
        timeout_seconds=600,
        max_attempts=2,
        idempotency_key="task:parent_2:default_chat_loop:critic@post_tools",
    )

    try:
        await spawn_module.start_background_runtime()
        deadline = asyncio.get_running_loop().time() + 1.0
        updated: dict[str, Any] | None = None
        while asyncio.get_running_loop().time() < deadline:
            updated = await store.get_task(created["task_id"])
            if updated is not None and updated["status"] == "pending":
                break
            await asyncio.sleep(0.01)
        assert updated is not None
        assert updated["status"] == "pending"
        assert "waiting for role dependencies" in str(updated["last_error"] or "")
        step = await store.get_task_step(created["task_id"], spawn_module._ROLE_RUN_STEP_ID)
        assert step is not None
        assert step["status"] == "failed"
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_spawn_task_auto_enqueues_role_runtime_children(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Successful parent background runs should auto-enqueue role-runtime child tasks."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    set_task_store(store)
    set_gateway(FakeGateway())

    class ImmediateAgent:
        async def run(self, user_message: str, session_id: str) -> str:
            return f"done:{session_id}:{user_message}"

    class BridgeAudit:
        def __init__(self) -> None:
            self.task_runs: list[dict[str, object]] = []

        async def get_latest_role_task_bridges(self, session_id: str) -> list[dict[str, object]]:
            task_id = session_id.split("task:", 1)[-1]
            return [
                {
                    "task_key": "planner@pre_llm",
                    "role": "planner",
                    "stage": "pre_llm",
                    "task_type": "workflow_role",
                    "source": "workflow_role",
                    "description": "default_chat_loop:planner@pre_llm",
                    "priority": 820,
                    "timeout_seconds": 180,
                    "max_attempts": 2,
                    "idempotency_key": f"{session_id}:default_chat_loop:planner@pre_llm",
                    "payload": {
                        "session_id": session_id,
                        "parent_task_id": task_id,
                        "workflow_name": "default_chat_loop",
                        "tool_names": ["web_search"],
                        "needs_grounded": True,
                        "role": "planner",
                        "stage": "pre_llm",
                        "task_key": "planner@pre_llm",
                        "user_summary": "bridge parent",
                        "depends_on": [],
                        "checkpoint_id": "planner@pre_llm",
                    },
                    "evidence_refs": [],
                },
                {
                    "task_key": "router@pre_llm",
                    "role": "router",
                    "stage": "pre_llm",
                    "task_type": "workflow_role",
                    "source": "workflow_role",
                    "description": "default_chat_loop:router@pre_llm",
                    "priority": 800,
                    "timeout_seconds": 180,
                    "max_attempts": 2,
                    "idempotency_key": f"{session_id}:default_chat_loop:router@pre_llm",
                    "payload": {
                        "session_id": session_id,
                        "parent_task_id": task_id,
                        "workflow_name": "default_chat_loop",
                        "tool_names": ["web_search"],
                        "needs_grounded": True,
                        "role": "router",
                        "stage": "pre_llm",
                        "task_key": "router@pre_llm",
                        "depends_on": ["planner@pre_llm"],
                        "checkpoint_id": "router@pre_llm",
                    },
                    "evidence_refs": [],
                },
                {
                    "task_key": "executor@tool_phase",
                    "role": "executor",
                    "stage": "tool_phase",
                    "task_type": "workflow_role",
                    "source": "workflow_role",
                    "description": "default_chat_loop:executor@tool_phase",
                    "priority": 760,
                    "timeout_seconds": 600,
                    "max_attempts": 2,
                    "idempotency_key": f"{session_id}:default_chat_loop:executor@tool_phase",
                    "payload": {
                        "session_id": session_id,
                        "parent_task_id": task_id,
                        "workflow_name": "default_chat_loop",
                        "tool_names": ["web_search"],
                        "needs_grounded": True,
                        "role": "executor",
                        "stage": "tool_phase",
                        "task_key": "executor@tool_phase",
                        "depends_on": ["router@pre_llm"],
                        "resume_checkpoint_id": "router@pre_llm",
                        "retry_budget": 2,
                        "evidence_refs": ["ev_1"],
                        "evidence_snapshot": {
                            "count": 1,
                            "items": [
                                {
                                    "evidence_id": "ev_1",
                                    "tool_name": "web_search",
                                    "url": "https://example.com/article",
                                }
                            ],
                        },
                    },
                    "evidence_refs": ["ev_1"],
                },
                {
                    "task_key": "critic@post_tools",
                    "role": "critic",
                    "stage": "post_tools",
                    "task_type": "workflow_role",
                    "source": "workflow_role",
                    "description": "default_chat_loop:critic@post_tools",
                    "priority": 720,
                    "timeout_seconds": 240,
                    "max_attempts": 1,
                    "idempotency_key": f"{session_id}:default_chat_loop:critic@post_tools",
                    "payload": {
                        "session_id": session_id,
                        "parent_task_id": task_id,
                        "workflow_name": "default_chat_loop",
                        "tool_names": ["web_search"],
                        "needs_grounded": True,
                        "role": "critic",
                        "stage": "post_tools",
                        "task_key": "critic@post_tools",
                        "depends_on": ["executor@tool_phase"],
                        "checkpoint_id": "critic@post_tools",
                        "resume_checkpoint_id": "router@pre_llm",
                        "retry_budget": 1,
                        "evidence_refs": ["ev_1"],
                        "evidence_snapshot": {
                            "count": 1,
                            "items": [
                                {
                                    "evidence_id": "ev_1",
                                    "tool_name": "web_search",
                                    "url": "https://example.com/article",
                                }
                            ],
                        },
                    },
                    "evidence_refs": ["ev_1"],
                },
                {
                    "task_key": "summarizer@post_tools",
                    "role": "summarizer",
                    "stage": "post_tools",
                    "task_type": "workflow_role",
                    "source": "workflow_role",
                    "description": "default_chat_loop:summarizer@post_tools",
                    "priority": 680,
                    "timeout_seconds": 180,
                    "max_attempts": 1,
                    "idempotency_key": f"{session_id}:default_chat_loop:summarizer@post_tools",
                    "payload": {
                        "session_id": session_id,
                        "parent_task_id": task_id,
                        "workflow_name": "default_chat_loop",
                        "tool_names": ["web_search"],
                        "needs_grounded": True,
                        "role": "summarizer",
                        "stage": "post_tools",
                        "task_key": "summarizer@post_tools",
                        "depends_on": ["critic@post_tools"],
                        "checkpoint_id": "summarizer@post_tools",
                        "resume_checkpoint_id": "router@pre_llm",
                        "retry_budget": 1,
                        "evidence_refs": ["ev_1"],
                        "evidence_snapshot": {
                            "count": 1,
                            "items": [
                                {
                                    "evidence_id": "ev_1",
                                    "tool_name": "web_search",
                                    "url": "https://example.com/article",
                                }
                            ],
                        },
                    },
                    "evidence_refs": ["ev_1"],
                },
            ]

        async def log_task_run(self, **kwargs: object) -> None:
            self.task_runs.append(dict(kwargs))

        async def log(self, **kwargs: object) -> None:
            return None

    set_agent(ImmediateAgent())
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: BridgeAudit())

    try:
        token = set_tool_runtime_context("telegram:42")
        try:
            response = await spawn_module.spawn_task("bridge parent")
        finally:
            reset_tool_runtime_context(token)

        parent_task_id = response.split("`")[1]
        statuses = await _wait_for_status(store, {"bridge parent": "succeeded"}, timeout=1.0)
        assert statuses["bridge parent"] == "succeeded"

        deadline = asyncio.get_running_loop().time() + 2.0
        child_rows: list[dict[str, object]] = []
        while asyncio.get_running_loop().time() < deadline:
            rows = await store.list_tasks(limit=10)
            child_rows = [
                item
                for item in rows
                if item["source"] == "workflow_role"
                and item["payload"].get("parent_task_id") == parent_task_id
            ]
            if len(child_rows) == 5 and all(
                str(item.get("status") or "") == "succeeded" for item in child_rows
            ):
                break
            await asyncio.sleep(0.01)
        assert len(child_rows) == 5
        assert {item["payload"]["task_key"] for item in child_rows} == {
            "planner@pre_llm",
            "router@pre_llm",
            "executor@tool_phase",
            "critic@post_tools",
            "summarizer@post_tools",
        }
        assert all(str(item.get("status") or "") == "succeeded" for item in child_rows)
        step_map = {}
        for item in child_rows:
            step = await store.get_task_step(str(item["task_id"]), spawn_module._ROLE_RUN_STEP_ID)
            assert step is not None
            step_map[str(item["payload"]["task_key"])] = step["output"]
        assert step_map["router@pre_llm"]["resolved_dependencies"] == ["planner@pre_llm"]
        assert step_map["executor@tool_phase"]["resolved_dependencies"] == ["router@pre_llm"]
        assert step_map["critic@post_tools"]["resolved_dependencies"] == ["executor@tool_phase"]
        assert step_map["summarizer@post_tools"]["resolved_dependencies"] == ["critic@post_tools"]
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_spawn_task_reuses_existing_task_for_same_idempotency_key(tmp_path: Path) -> None:
    """Repeated spawn requests with the same idempotency key should reuse one task row."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)

    fake_agent = FakeAgent()
    fake_gateway = FakeGateway()
    set_agent(fake_agent)
    set_gateway(fake_gateway)

    try:
        token = set_tool_runtime_context("telegram:dedupe")
        try:
            first = await spawn_module.spawn_task(
                "dedupe request",
                idempotency_key="same-task",
            )
            second = await spawn_module.spawn_task(
                "dedupe request",
                idempotency_key="same-task",
            )
        finally:
            reset_tool_runtime_context(token)

        first_task_id = first.split("`")[1]
        second_task_id = second.split("`")[1]
        tasks = await store.list_tasks(limit=10)

        assert first_task_id == second_task_id
        assert "already exists" in second
        assert len(tasks) == 1
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_spawn_task_logs_task_run_trace(tmp_path: Path, monkeypatch) -> None:
    """Successful background completion should emit one task_run trace row."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    fake_agent = FakeAgent()
    fake_gateway = FakeGateway()
    set_agent(fake_agent)
    set_gateway(fake_gateway)

    try:
        token = set_tool_runtime_context("telegram:trace")
        try:
            response = await spawn_module.spawn_task("trace me")
        finally:
            reset_tool_runtime_context(token)

        task_id = response.split("`")[1]
        fake_agent._release.set()
        statuses = await _wait_for_status(store, {"trace me": "succeeded"}, timeout=1.0)
        assert statuses["trace me"] == "succeeded"

        replay = await audit.get_task_replay(task_id)
        assert replay is not None
        assert replay["task_runs"][0]["status"] == "success"
        assert replay["task_runs"][0]["attempt_number"] == 1
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_spawn_task_drains_pending_queue(tmp_path: Path) -> None:
    """Queued tasks should run later when an active slot is released."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)

    fake_agent = FakeAgent()
    fake_gateway = FakeGateway()
    set_agent(fake_agent)
    set_gateway(fake_gateway)

    original_limit = spawn_module._MAX_BACKGROUND_TASKS
    spawn_module._MAX_BACKGROUND_TASKS = 1
    try:
        token = set_tool_runtime_context("telegram:42")
        try:
            await spawn_module.spawn_task("first task", priority=200)
            await spawn_module.spawn_task("second task", priority=100)
        finally:
            reset_tool_runtime_context(token)

        statuses = await _wait_for_status(
            store,
            {"first task": "running", "second task": "pending"},
        )
        assert statuses["first task"] == "running"
        assert statuses["second task"] == "pending"

        fake_agent._release.set()
        statuses = await _wait_for_status(
            store,
            {"first task": "succeeded", "second task": "succeeded"},
        )
        assert statuses["first task"] == "succeeded"
        assert statuses["second task"] == "succeeded"
    finally:
        spawn_module._MAX_BACKGROUND_TASKS = original_limit
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_background_runtime_recovers_orphaned_task_on_start(tmp_path: Path) -> None:
    """Runtime startup should recover stale running tasks and execute them again."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)

    fake_agent = FakeAgent()
    fake_gateway = FakeGateway()
    set_agent(fake_agent)
    set_gateway(fake_gateway)

    try:
        queued = await store.create_task(
            "recovered task",
            task_type="background",
            source="spawn_task",
            session_id="telegram:99",
        )
        claimed = await store.claim_next_task(
            source="spawn_task",
            worker_id="dead-worker",
        )
        assert claimed is not None

        conn = sqlite3.connect(tmp_path / "tasks.db")
        conn.execute(
            """
            UPDATE tasks
            SET last_heartbeat_at = '2000-01-01 00:00:00'
            WHERE task_id = ?
            """,
            (queued["task_id"],),
        )
        conn.commit()
        conn.close()

        await spawn_module.start_background_runtime()
        statuses = await _wait_for_status(store, {"recovered task": "running"})
        assert statuses["recovered task"] == "running"

        refreshed = await store.get_task(queued["task_id"])
        assert refreshed is not None
        assert refreshed["claimed_by"] != ""
        assert refreshed["claimed_by"] != "dead-worker"

        fake_agent._release.set()
        statuses = await _wait_for_status(store, {"recovered task": "succeeded"})
        assert statuses["recovered task"] == "succeeded"
        assert fake_agent.calls == [("recovered task", f"task:{queued['task_id']}")]
    finally:
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_background_runtime_respects_global_running_limit(tmp_path: Path) -> None:
    """Local runtime should not over-claim when another worker already fills the pool."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)

    fake_agent = FakeAgent()
    fake_gateway = FakeGateway()
    set_agent(fake_agent)
    set_gateway(fake_gateway)

    original_limit = spawn_module._MAX_BACKGROUND_TASKS
    spawn_module._MAX_BACKGROUND_TASKS = 1
    try:
        remote = await store.create_task("remote running", source="spawn_task")
        await store.claim_next_task(source="spawn_task", worker_id="worker-remote")
        local = await store.create_task("local pending", source="spawn_task")

        await spawn_module.start_background_runtime()
        statuses = await _wait_for_status(
            store,
            {"remote running": "running", "local pending": "pending"},
            timeout=0.3,
        )
        assert statuses["remote running"] == "running"
        assert statuses["local pending"] == "pending"

        await store.transition_task(remote["task_id"], "succeeded")
        spawn_module.wake_background_runtime()

        statuses = await _wait_for_status(store, {"local pending": "running"}, timeout=0.5)
        assert statuses["local pending"] == "running"
        refreshed = await store.get_task(local["task_id"])
        assert refreshed is not None
        assert refreshed["claimed_by"] != ""
        assert refreshed["claimed_by"] != "worker-remote"

        fake_agent._release.set()
        statuses = await _wait_for_status(store, {"local pending": "succeeded"}, timeout=1.0)
        assert statuses["local pending"] == "succeeded"
    finally:
        spawn_module._MAX_BACKGROUND_TASKS = original_limit
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_background_runtime_respects_rate_limit_bucket(tmp_path: Path) -> None:
    """Tasks sharing a rate-limit bucket should be staggered by the runtime."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)

    fake_agent = FakeAgent()
    fake_gateway = FakeGateway()
    set_agent(fake_agent)
    set_gateway(fake_gateway)

    original_limit = spawn_module._MAX_BACKGROUND_TASKS
    spawn_module._MAX_BACKGROUND_TASKS = 2
    try:
        token = set_tool_runtime_context("telegram:42")
        try:
            await spawn_module.spawn_task(
                "bucket first",
                priority=200,
                rate_limit_key="search-api",
                rate_limit_window_seconds=60,
                rate_limit_max_claims=1,
            )
            await spawn_module.spawn_task(
                "bucket second",
                priority=100,
                rate_limit_key="search-api",
                rate_limit_window_seconds=60,
                rate_limit_max_claims=1,
            )
        finally:
            reset_tool_runtime_context(token)

        statuses = await _wait_for_status(
            store,
            {"bucket first": "running", "bucket second": "pending"},
            timeout=0.5,
        )
        assert statuses["bucket first"] == "running"
        assert statuses["bucket second"] == "pending"
        deadline = asyncio.get_running_loop().time() + 0.5
        metrics = await store.get_queue_metrics(source="spawn_task")
        while (
            metrics["rate_limited_backlog"] != 1
            and asyncio.get_running_loop().time() < deadline
        ):
            await asyncio.sleep(0.01)
            metrics = await store.get_queue_metrics(source="spawn_task")
        assert metrics["rate_limited_backlog"] == 1

        fake_agent._release.set()
        statuses = await _wait_for_status(
            store, {"bucket first": "succeeded"}, timeout=1.0
        )
        assert statuses["bucket first"] == "succeeded"
    finally:
        spawn_module._MAX_BACKGROUND_TASKS = original_limit
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_running_background_task_can_be_cancelled(tmp_path: Path) -> None:
    """Running tasks should react to cancel requests from the store."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)

    fake_agent = FakeAgent()
    fake_gateway = FakeGateway()
    set_agent(fake_agent)
    set_gateway(fake_gateway)

    original_interval = spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS
    spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = 0.05
    try:
        token = set_tool_runtime_context("telegram:42")
        try:
            response = await spawn_module.spawn_task("cancel me")
        finally:
            reset_tool_runtime_context(token)

        task_id = response.split("`")[1]
        statuses = await _wait_for_status(store, {"cancel me": "running"})
        assert statuses["cancel me"] == "running"

        requested = await store.request_cancel(task_id)
        assert requested["cancel_requested"] is True

        statuses = await _wait_for_status(store, {"cancel me": "cancelled"}, timeout=1.0)
        assert statuses["cancel me"] == "cancelled"
        refreshed = await store.get_task(task_id)
        assert refreshed is not None
        assert refreshed["status"] == "cancelled"
    finally:
        spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = original_interval
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_running_background_task_times_out(tmp_path: Path) -> None:
    """Running tasks should fail once their timeout budget expires."""
    await _reset_spawn_runtime_state()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)

    fake_agent = FakeAgent()
    fake_gateway = FakeGateway()
    set_agent(fake_agent)
    set_gateway(fake_gateway)

    original_interval = spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS
    spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = 0.05
    try:
        token = set_tool_runtime_context("telegram:42")
        try:
            response = await spawn_module.spawn_task(
                "timeout me",
                timeout_seconds=1,
                max_attempts=1,
            )
        finally:
            reset_tool_runtime_context(token)

        task_id = response.split("`")[1]
        statuses = await _wait_for_status(store, {"timeout me": "running"})
        assert statuses["timeout me"] == "running"

        conn = sqlite3.connect(tmp_path / "tasks.db")
        conn.execute(
            """
            UPDATE tasks
            SET started_at = '2000-01-01 00:00:00'
            WHERE task_id = ?
            """,
            (task_id,),
        )
        conn.commit()
        conn.close()

        statuses = await _wait_for_status(store, {"timeout me": "failed"}, timeout=1.0)
        assert statuses["timeout me"] == "failed"
        refreshed = await store.get_task(task_id)
        assert refreshed is not None
        assert refreshed["last_error"] == "task timed out"
    finally:
        spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = original_interval
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_timeout_watchdog_event_is_audited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout-driven task cancellation should emit a runtime watchdog audit event."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    fake_agent = FakeAgent()
    fake_gateway = FakeGateway()
    set_agent(fake_agent)
    set_gateway(fake_gateway)

    original_interval = spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS
    spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = 0.05
    try:
        token = set_tool_runtime_context("telegram:42")
        try:
            response = await spawn_module.spawn_task(
                "audit my timeout",
                timeout_seconds=1,
                max_attempts=1,
            )
        finally:
            reset_tool_runtime_context(token)

        task_id = response.split("`")[1]
        statuses = await _wait_for_status(store, {"audit my timeout": "running"})
        assert statuses["audit my timeout"] == "running"

        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            UPDATE tasks
            SET started_at = '2000-01-01 00:00:00'
            WHERE task_id = ?
            """,
            (task_id,),
        )
        conn.commit()
        conn.close()

        statuses = await _wait_for_status(
            store,
            {"audit my timeout": "failed"},
            timeout=1.0,
        )
        assert statuses["audit my timeout"] == "failed"

        entries = await audit.get_recent(limit=10)
        watchdog_entries = [
            item
            for item in entries
            if item["action_type"] == "runtime_watchdog"
            and item["session_id"] == f"task:{task_id}"
        ]
        assert watchdog_entries
        assert watchdog_entries[0]["tool_name"] == "spawn_task"
        assert "timeout_cancelled" in watchdog_entries[0]["input_summary"]
    finally:
        spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = original_interval
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_runtime_health_alert_is_sent_once_for_dead_letter_queue(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Degraded runtime health should emit one deduplicated proactive alert."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    class RuntimeGateway:
        """Gateway stub that captures runtime health alerts."""

        def __init__(self) -> None:
            self.channels = {"telegram": object()}
            self.messages: list[tuple[str, str]] = []

        async def send_proactive(self, text: str, channel: str = "telegram") -> None:
            self.messages.append((channel, text))

    gateway = RuntimeGateway()
    monkeypatch.setattr("nanoclaw.channels.gateway.get_gateway", lambda: gateway)

    created = await store.create_task("dead-letter alert", source="spawn_task", max_attempts=1)
    await store.claim_next_task(source="spawn_task", worker_id="worker-a")
    await store.fail_task_attempt(created["task_id"], last_error="fatal")

    original_interval = spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS
    spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = 0.05
    try:
        await spawn_module.start_background_runtime()
        deadline = asyncio.get_running_loop().time() + 0.4
        while not gateway.messages and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert len(gateway.messages) == 1
        assert gateway.messages[0][0] == "telegram"
        assert "[runtime alert]" in gateway.messages[0][1]
        assert "severity=warning" in gateway.messages[0][1]
        assert "stage=degraded_initial" in gateway.messages[0][1]
        assert "dead_letter=1" in gateway.messages[0][1]

        await asyncio.sleep(0.12)
        assert len(gateway.messages) == 1

        entries = await audit.get_recent(limit=10)
        runtime_alerts = [item for item in entries if item["action_type"] == "runtime_alert"]
        assert runtime_alerts
        assert "dead_letter=1" in runtime_alerts[0]["input_summary"]
    finally:
        spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = original_interval
        await _reset_spawn_runtime_state()


@pytest.mark.asyncio
async def test_runtime_health_alert_marks_queue_stall_as_critical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Queue stall alerts should be emitted as critical runtime alerts."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    audit = AuditLog(db_path)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    class RuntimeGateway:
        """Gateway stub that captures critical runtime alerts."""

        def __init__(self) -> None:
            self.channels = {"telegram": object()}
            self.messages: list[tuple[str, str]] = []

        async def send_proactive(self, text: str, channel: str = "telegram") -> None:
            self.messages.append((channel, text))

    gateway = RuntimeGateway()
    monkeypatch.setattr("nanoclaw.channels.gateway.get_gateway", lambda: gateway)

    await spawn_module._maybe_send_runtime_alert(
        {
            "ready_backlog": 3,
            "retry_backlog": 0,
            "running_tasks": 0,
            "dead_letter_tasks": 0,
            "stale_running_tasks": 0,
            "cancel_requested_running": 0,
            "oldest_ready_age_seconds": 300,
            "stall_threshold_seconds": 120,
        }
    )

    assert len(gateway.messages) == 1
    assert "health=critical" in gateway.messages[0][1]
    assert "severity=error" in gateway.messages[0][1]
    assert "stage=critical_initial" in gateway.messages[0][1]
    assert "queue_stall=3" in gateway.messages[0][1]

    entries = await audit.get_recent(limit=10)
    runtime_alerts = [item for item in entries if item["action_type"] == "runtime_alert"]
    assert runtime_alerts
    assert runtime_alerts[0]["status"] == "error"
    assert "queue_stall=3" in runtime_alerts[0]["input_summary"]


@pytest.mark.asyncio
async def test_repeated_runtime_alert_escalates_after_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated identical alerts should move into the escalated stage."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    audit = AuditLog(db_path)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    monkeypatch.setattr(
        spawn_module,
        "_get_runtime_alert_cooldown_seconds",
        lambda: 0,
    )

    class RuntimeGateway:
        """Gateway stub that captures repeated runtime alerts."""

        def __init__(self) -> None:
            self.channels = {"telegram": object()}
            self.messages: list[tuple[str, str]] = []

        async def send_proactive(self, text: str, channel: str = "telegram") -> None:
            self.messages.append((channel, text))

    gateway = RuntimeGateway()
    monkeypatch.setattr("nanoclaw.channels.gateway.get_gateway", lambda: gateway)

    degraded_metrics = {
        "ready_backlog": 0,
        "retry_backlog": 0,
        "running_tasks": 0,
        "dead_letter_tasks": 1,
        "stale_running_tasks": 0,
        "cancel_requested_running": 0,
        "oldest_ready_age_seconds": 0,
        "stall_threshold_seconds": 120,
    }

    await spawn_module._maybe_send_runtime_alert(degraded_metrics)
    await spawn_module._maybe_send_runtime_alert(degraded_metrics)

    assert len(gateway.messages) == 2
    assert "stage=degraded_initial" in gateway.messages[0][1]
    assert "repeat_count=1" in gateway.messages[0][1]
    assert "stage=degraded_escalated" in gateway.messages[1][1]
    assert "repeat_count=2" in gateway.messages[1][1]
    assert "severity=error" in gateway.messages[1][1]

    entries = await audit.get_recent(limit=10)
    escalation_entries = [
        item
        for item in entries
        if item["action_type"] == "runtime_alert_escalation"
    ]
    assert escalation_entries
    assert "degraded_escalated" in escalation_entries[0]["input_summary"]


@pytest.mark.asyncio
async def test_escalated_runtime_alert_routes_to_escalation_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Escalated alerts should fan out to the configured escalation channel."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    audit = AuditLog(db_path)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    monkeypatch.setattr(
        spawn_module,
        "_get_runtime_alert_cooldown_seconds",
        lambda: 0,
    )
    monkeypatch.setattr(
        spawn_module,
        "_get_runtime_alert_channel",
        lambda: "telegram",
    )
    monkeypatch.setattr(
        spawn_module,
        "_get_runtime_alert_escalation_channel",
        lambda: "feishu",
    )

    class RuntimeGateway:
        """Gateway stub that captures escalation-channel routing."""

        def __init__(self) -> None:
            self.channels = {"telegram": object(), "feishu": object()}
            self.messages: list[tuple[str, str]] = []

        async def send_proactive(self, text: str, channel: str = "telegram") -> None:
            self.messages.append((channel, text))

    gateway = RuntimeGateway()
    monkeypatch.setattr("nanoclaw.channels.gateway.get_gateway", lambda: gateway)

    degraded_metrics = {
        "ready_backlog": 0,
        "retry_backlog": 0,
        "running_tasks": 0,
        "dead_letter_tasks": 1,
        "stale_running_tasks": 0,
        "cancel_requested_running": 0,
        "oldest_ready_age_seconds": 0,
        "stall_threshold_seconds": 120,
    }

    await spawn_module._maybe_send_runtime_alert(degraded_metrics)
    await spawn_module._maybe_send_runtime_alert(degraded_metrics)

    assert [channel for channel, _ in gateway.messages] == [
        "telegram",
        "telegram",
        "feishu",
    ]

    entries = await audit.get_recent(limit=10)
    escalation_entries = [
        item
        for item in entries
        if item["action_type"] == "runtime_alert_escalation"
    ]
    assert escalation_entries
    assert "targets=telegram,feishu" in escalation_entries[0]["input_summary"]


@pytest.mark.asyncio
async def test_schedule_health_alert_targets_the_schedule_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Schedule health alerts should go back to the owning schedule chat."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    audit = AuditLog(db_path)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    gateway = _ScheduleGateway(
        [
            {
                "id": 7,
                "name": "AI Daily",
                "enabled": 1,
                "channel": "feishu",
                "target_id": "oc_chat_1",
                "runtime": {
                    "health": "attention",
                    "health_reason": "delivery retry failed",
                    "notify_kind": "cron_delivery_retry_scheduled",
                    "last_execution": {"status": "succeeded"},
                    "last_delivery_retry": {"status": "failed"},
                },
            }
        ]
    )
    monkeypatch.setattr("nanoclaw.channels.gateway.get_gateway", lambda: gateway)

    await spawn_module._maybe_send_schedule_health_alerts()

    assert not gateway.messages
    assert len(gateway.feishu.targeted_messages) == 1
    chat_id, text = gateway.feishu.targeted_messages[0]
    assert chat_id == "oc_chat_1"
    assert "[schedule alert]" in text
    assert "job_id=7" in text
    assert "health=attention" in text
    assert "severity=error" in text
    assert "stage=attention_initial" in text
    assert "notify_mode=cron_delivery_retry_scheduled" in text

    entries = await audit.get_recent(limit=10)
    schedule_alerts = [item for item in entries if item["action_type"] == "schedule_alert"]
    assert schedule_alerts
    assert schedule_alerts[0]["session_id"] == "schedule:7"
    assert "targets=feishu:oc_chat_1" in schedule_alerts[0]["input_summary"]
    assert schedule_alerts[0]["status"] == "error"


@pytest.mark.asyncio
async def test_schedule_retrying_alert_is_suppressed_until_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single retrying state should be tracked but not alerted yet."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    audit = AuditLog(db_path)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    gateway = _ScheduleGateway(
        [
            {
                "id": 8,
                "name": "Muted Retry",
                "enabled": 1,
                "channel": "feishu",
                "target_id": "oc_chat_8",
                "runtime": {
                    "health": "retrying",
                    "health_reason": "delivery retry scheduled",
                    "notify_kind": "cron_delivery_retry_scheduled",
                    "last_execution": {"status": "succeeded"},
                    "last_delivery_retry": {"status": "pending"},
                },
            }
        ]
    )
    monkeypatch.setattr("nanoclaw.channels.gateway.get_gateway", lambda: gateway)
    monkeypatch.setattr(
        spawn_module,
        "_get_schedule_alert_retrying_after",
        lambda: 2,
    )

    await spawn_module._maybe_send_schedule_health_alerts()

    assert not gateway.messages
    assert not gateway.feishu.targeted_messages
    cached = spawn_module._runtime_alert_cache["schedule_health:8"]
    assert cached["repeat_count"] == 1
    assert cached["suppressed"] is True
    assert cached["stage"] == "retrying_suppressed"

    entries = await audit.get_recent(limit=10)
    schedule_alerts = [item for item in entries if item["action_type"] == "schedule_alert"]
    assert not schedule_alerts


@pytest.mark.asyncio
async def test_schedule_retrying_alert_fires_after_threshold(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A repeated retrying state should emit the first schedule warning."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    audit = AuditLog(db_path)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    monkeypatch.setattr(spawn_module, "_get_runtime_alert_cooldown_seconds", lambda: 0)
    monkeypatch.setattr(
        spawn_module,
        "_get_schedule_alert_retrying_after",
        lambda: 2,
    )

    gateway = _ScheduleGateway(
        [
            {
                "id": 10,
                "name": "Paper Retry",
                "enabled": 1,
                "channel": "feishu",
                "target_id": "oc_chat_10",
                "runtime": {
                    "health": "retrying",
                    "health_reason": "delivery retry scheduled",
                    "notify_kind": "cron_delivery_retry_scheduled",
                    "last_execution": {"status": "succeeded"},
                    "last_delivery_retry": {"status": "pending"},
                },
            }
        ]
    )
    monkeypatch.setattr("nanoclaw.channels.gateway.get_gateway", lambda: gateway)

    await spawn_module._maybe_send_schedule_health_alerts()
    await spawn_module._maybe_send_schedule_health_alerts()

    assert len(gateway.feishu.targeted_messages) == 1
    chat_id, text = gateway.feishu.targeted_messages[0]
    assert chat_id == "oc_chat_10"
    assert "health=retrying" in text
    assert "stage=retrying_initial" in text
    assert "retrying_after=2" in text
    assert "repeat_count=2" in text
    assert "severity=warning" in text

    entries = await audit.get_recent(limit=10)
    schedule_alerts = [item for item in entries if item["action_type"] == "schedule_alert"]
    assert schedule_alerts
    assert "retrying_after=2" in schedule_alerts[0]["input_summary"]
    assert schedule_alerts[0]["status"] == "warning"


@pytest.mark.asyncio
async def test_repeated_schedule_health_alert_escalates_to_secondary_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated identical schedule alerts should fan out to the escalation channel."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    audit = AuditLog(db_path)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    monkeypatch.setattr(spawn_module, "_get_runtime_alert_cooldown_seconds", lambda: 0)
    monkeypatch.setattr(
        spawn_module,
        "_get_runtime_alert_escalation_channel",
        lambda: "console",
    )
    monkeypatch.setattr(
        spawn_module,
        "_get_schedule_alert_escalate_after",
        lambda: 2,
    )

    gateway = _ScheduleGateway(
        [
            {
                "id": 9,
                "name": "Robotics Hotspot",
                "enabled": 1,
                "channel": "feishu",
                "target_id": "oc_chat_9",
                "runtime": {
                    "health": "attention",
                    "health_reason": "delivery retry failed",
                    "notify_kind": "cron_delivery_retry_scheduled",
                    "last_execution": {"status": "succeeded"},
                    "last_delivery_retry": {"status": "failed"},
                },
            }
        ]
    )
    monkeypatch.setattr("nanoclaw.channels.gateway.get_gateway", lambda: gateway)

    await spawn_module._maybe_send_schedule_health_alerts()
    await spawn_module._maybe_send_schedule_health_alerts()

    assert [chat_id for chat_id, _ in gateway.feishu.targeted_messages] == [
        "oc_chat_9",
        "oc_chat_9",
    ]
    assert len(gateway.messages) == 1
    assert gateway.messages[0][0] == "console"
    assert "stage=attention_escalated" in gateway.messages[0][1]
    assert "repeat_count=2" in gateway.messages[0][1]
    assert "severity=critical" in gateway.messages[0][1]
    assert "escalate_after=2" in gateway.messages[0][1]

    entries = await audit.get_recent(limit=10)
    escalation_entries = [
        item
        for item in entries
        if item["action_type"] == "schedule_alert_escalation"
    ]
    assert escalation_entries
    assert escalation_entries[0]["session_id"] == "schedule:9"
    assert "attention_escalated" in escalation_entries[0]["input_summary"]


@pytest.mark.asyncio
async def test_schedule_recovery_notice_is_sent_after_alerted_issue_clears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A healthy schedule should emit one recovery notice after a real alert."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    audit = AuditLog(db_path)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    jobs = [
        {
            "id": 11,
            "name": "Recovered Daily",
            "enabled": 1,
            "channel": "feishu",
            "target_id": "oc_chat_11",
            "runtime": {
                "health": "attention",
                "health_reason": "latest execution failed",
                "notify_kind": "cron_notify_failed",
                "last_execution": {"status": "failed"},
                "last_delivery_retry": None,
            },
        }
    ]
    gateway = _ScheduleGateway(jobs)
    monkeypatch.setattr("nanoclaw.channels.gateway.get_gateway", lambda: gateway)

    await spawn_module._maybe_send_schedule_health_alerts()
    jobs[0]["runtime"] = {
        "health": "healthy",
        "health_reason": "latest execution succeeded",
        "notify_kind": "cron_notify_sent",
        "last_execution": {"status": "succeeded"},
        "last_delivery_retry": None,
    }
    await spawn_module._maybe_send_schedule_health_alerts()

    assert len(gateway.feishu.targeted_messages) == 2
    recovery_text = gateway.feishu.targeted_messages[-1][1]
    assert "[schedule recovered]" in recovery_text
    assert "job_id=11" in recovery_text
    assert "previous_stage=attention_initial" in recovery_text
    assert "health=healthy" in recovery_text
    assert "last_execution=succeeded" in recovery_text
    assert "schedule_health:11" not in spawn_module._runtime_alert_cache

    entries = await audit.get_recent(limit=10)
    recovery_entries = [item for item in entries if item["action_type"] == "schedule_recovery"]
    assert recovery_entries
    assert recovery_entries[0]["session_id"] == "schedule:11"
    assert "previous_stage=attention_initial" in recovery_entries[0]["input_summary"]


@pytest.mark.asyncio
async def test_suppressed_retrying_schedule_does_not_emit_recovery_notice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A suppressed retrying state should not produce a later recovery notice."""
    await _reset_spawn_runtime_state()
    db_path = tmp_path / "tasks.db"
    audit = AuditLog(db_path)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    monkeypatch.setattr(
        spawn_module,
        "_get_schedule_alert_retrying_after",
        lambda: 2,
    )

    jobs = [
        {
            "id": 12,
            "name": "Quiet Retry",
            "enabled": 1,
            "channel": "feishu",
            "target_id": "oc_chat_12",
            "runtime": {
                "health": "retrying",
                "health_reason": "delivery retry scheduled",
                "notify_kind": "cron_delivery_retry_scheduled",
                "last_execution": {"status": "succeeded"},
                "last_delivery_retry": {"status": "pending"},
            },
        }
    ]
    gateway = _ScheduleGateway(jobs)
    monkeypatch.setattr("nanoclaw.channels.gateway.get_gateway", lambda: gateway)

    await spawn_module._maybe_send_schedule_health_alerts()
    jobs[0]["runtime"] = {
        "health": "healthy",
        "health_reason": "latest execution succeeded",
        "notify_kind": "cron_notify_sent",
        "last_execution": {"status": "succeeded"},
        "last_delivery_retry": None,
    }
    await spawn_module._maybe_send_schedule_health_alerts()

    assert not gateway.feishu.targeted_messages
    assert not gateway.messages
    assert "schedule_health:12" not in spawn_module._runtime_alert_cache

    entries = await audit.get_recent(limit=10)
    recovery_entries = [item for item in entries if item["action_type"] == "schedule_recovery"]
    assert not recovery_entries
