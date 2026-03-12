"""Runtime acceptance tests for recovery, scheduling, and watchdog replay."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from nanoclaw.channels.gateway import set_gateway
from nanoclaw.core.agent import set_agent
from nanoclaw.runtime.tasks import TaskStore, set_task_store
from nanoclaw.security.audit import AuditLog
from nanoclaw.tools.runtime_context import reset_tool_runtime_context, set_tool_runtime_context
from nanoclaw.tools.spawn import start_background_runtime, stop_background_runtime
import nanoclaw.tools.spawn as spawn_module


class CompleteAgent:
    """Agent stub that finishes immediately."""

    async def run(self, user_message: str, session_id: str) -> str:
        """Return one deterministic result."""
        return f"done:{session_id}:{user_message}"


class RecordingCompleteAgent:
    """Agent stub that records calls and finishes immediately."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def run(self, user_message: str, session_id: str) -> str:
        """Record one call and return a deterministic result."""
        self.calls.append((user_message, session_id))
        return f"done:{session_id}:{user_message}"


class BlockingAgent:
    """Agent stub that waits forever until the runtime intervenes."""

    async def run(self, user_message: str, session_id: str) -> str:
        """Block until timeout or cancellation interrupts execution."""
        await asyncio.Event().wait()
        return ""


class FailOnceAgent:
    """Agent stub that fails on the first run and succeeds on the next run."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def run(self, user_message: str, session_id: str) -> str:
        """Fail once so the runtime must requeue and retry the task."""
        self.calls.append((user_message, session_id))
        if len(self.calls) == 1:
            raise RuntimeError("transient agent failure")
        return f"retry-ok:{session_id}:{user_message}"


class FailAlwaysAgent:
    """Agent stub that always fails so the runtime must dead-letter the task."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def run(self, user_message: str, session_id: str) -> str:
        """Always raise a deterministic terminal error."""
        self.calls.append((user_message, session_id))
        raise RuntimeError("terminal agent failure")


class SequencedAgent:
    """Agent stub that blocks the first task so later fairness can be observed."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.release_first = asyncio.Event()

    async def run(self, user_message: str, session_id: str) -> str:
        """Hold the first call until the test releases it, then finish quickly."""
        self.calls.append((user_message, session_id))
        if len(self.calls) == 1:
            await self.release_first.wait()
        return f"seq-ok:{session_id}:{user_message}"


class CaptureGateway:
    """Gateway stub that captures proactive notifications."""

    def __init__(self) -> None:
        """Initialize one in-memory capture channel."""
        self.channels = {"telegram": object(), "feishu": object()}
        self.messages: list[tuple[str, str]] = []

    async def send_proactive(self, text: str, channel: str = "telegram") -> None:
        """Store one proactive message."""
        self.messages.append((channel, text))


async def _wait_for_task_status(
    store: TaskStore,
    task_id: str,
    expected_status: str,
    *,
    timeout: float = 1.5,
) -> dict[str, object]:
    """Poll one task until the expected status appears or timeout expires."""
    deadline = asyncio.get_running_loop().time() + timeout
    last_seen: dict[str, object] | None = None
    while True:
        task = await store.get_task(task_id)
        if task is not None:
            last_seen = task
            if str(task.get("status")) == expected_status:
                return task
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Task `{task_id}` did not reach `{expected_status}`. Last seen: {last_seen}"
            )
        await asyncio.sleep(0.01)


async def _wait_for_call_count(
    agent: Any,
    expected_count: int,
    *,
    timeout: float = 1.0,
) -> None:
    """Poll one test agent until the expected number of calls is recorded."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if len(getattr(agent, "calls", [])) >= expected_count:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Agent did not record {expected_count} calls. Current calls: {agent.calls!r}"
            )
        await asyncio.sleep(0.01)


async def _wait_for_length(
    items: list[Any],
    expected_length: int,
    *,
    timeout: float = 1.0,
) -> None:
    """Poll one mutable list until it reaches the expected length."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if len(items) >= expected_length:
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"List did not reach length {expected_length}. Current items: {items!r}"
            )
        await asyncio.sleep(0.01)


async def _wait_for_message_text(
    messages: list[tuple[str, str]],
    needle: str,
    *,
    timeout: float = 1.0,
) -> None:
    """Poll one message list until a substring appears in any message body."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        if any(needle in text for _, text in messages):
            return
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"Message text `{needle}` not observed. Current messages: {messages!r}"
            )
        await asyncio.sleep(0.01)


async def _wait_for_replay(
    audit: AuditLog,
    task_id: str,
    *,
    timeout: float = 1.0,
    require_audit_events: bool = False,
    required_step_ids: tuple[str, ...] = (),
) -> dict[str, object]:
    """Poll one replay bundle until task runs, and optionally audit events, are available."""
    deadline = asyncio.get_running_loop().time() + timeout
    last_seen: dict[str, object] | None = None
    while True:
        replay = await audit.get_task_replay(task_id)
        if replay is not None:
            last_seen = replay
            step_ids = {
                str(item.get("step_id") or "")
                for item in replay.get("steps") or []
            }
            if replay.get("task_runs") and (
                not require_audit_events or replay.get("audit_events")
            ) and all(step_id in step_ids for step_id in required_step_ids):
                return replay
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Replay for `{task_id}` not ready: {last_seen}")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_runtime_acceptance_duplicate_submission_reuses_one_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Duplicate background submissions should collapse to one persisted task and replay."""
    await stop_background_runtime()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    set_agent(CompleteAgent())
    gateway = CaptureGateway()
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    try:
        await start_background_runtime()
        token = set_tool_runtime_context("telegram:acceptance-dedupe")
        try:
            first = await spawn_module.spawn_task(
                "acceptance dedupe",
                idempotency_key="acceptance-key",
            )
            second = await spawn_module.spawn_task(
                "acceptance dedupe",
                idempotency_key="acceptance-key",
            )
        finally:
            reset_tool_runtime_context(token)

        task_id = first.split("`")[1]
        finished = await _wait_for_task_status(store, task_id, "succeeded")
        replay = await audit.get_task_replay(task_id)
        tasks = await store.list_tasks(limit=10)

        assert finished["status"] == "succeeded"
        assert second.split("`")[1] == task_id
        assert len(tasks) == 1
        assert len(gateway.messages) == 1
        assert replay is not None
        assert replay["task_runs"][0]["status"] == "success"
        assert replay["steps"][0]["step_id"] == "agent_run"
        assert replay["audit_events"] == []
    finally:
        await stop_background_runtime()


@pytest.mark.asyncio
async def test_runtime_acceptance_timeout_surfaces_watchdog_in_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Timeout failures should surface watchdog intervention in task replay."""
    await stop_background_runtime()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    set_agent(BlockingAgent())
    gateway = CaptureGateway()
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    original_interval = spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS
    spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = 0.05
    try:
        await start_background_runtime()
        token = set_tool_runtime_context("telegram:acceptance-timeout")
        try:
            response = await spawn_module.spawn_task(
                "acceptance timeout",
                timeout_seconds=1,
                max_attempts=1,
            )
        finally:
            reset_tool_runtime_context(token)

        task_id = response.split("`")[1]
        await _wait_for_task_status(store, task_id, "running")

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

        finished = await _wait_for_task_status(store, task_id, "failed")
        replay = await _wait_for_replay(audit, task_id, require_audit_events=True)
        await _wait_for_message_text(gateway.messages, "failed: task timed out")

        assert finished["last_error"] == "task timed out"
        assert replay["task_runs"][0]["status"] == "failed"
        assert replay["task_runs"][0]["failure_reason"] == "task timed out"
        assert replay["audit_events"]
        assert replay["audit_events"][0]["action_type"] == "runtime_watchdog"
        assert "timeout_cancelled" in replay["audit_events"][0]["input_summary"]
        assert any(channel == "telegram" for channel, _ in gateway.messages)
        assert any("failed: task timed out" in text for _, text in gateway.messages)
    finally:
        spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = original_interval
        await stop_background_runtime()


@pytest.mark.asyncio
async def test_runtime_acceptance_cancel_surfaces_terminal_state_in_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancellation should persist terminal task state and replay details end to end."""
    await stop_background_runtime()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    set_agent(BlockingAgent())
    gateway = CaptureGateway()
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    original_interval = spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS
    spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = 0.05
    try:
        await start_background_runtime()
        token = set_tool_runtime_context("telegram:acceptance-cancel")
        try:
            response = await spawn_module.spawn_task("acceptance cancel")
        finally:
            reset_tool_runtime_context(token)

        task_id = response.split("`")[1]
        await _wait_for_task_status(store, task_id, "running")
        requested = await store.request_cancel(task_id)
        finished = await _wait_for_task_status(store, task_id, "cancelled")
        replay = await _wait_for_replay(
            audit,
            task_id,
            required_step_ids=("notify_cancelled",),
        )

        assert requested["cancel_requested"] is True
        assert finished["status"] == "cancelled"
        assert replay["task_runs"][0]["status"] == "cancelled"
        assert replay["task_runs"][0]["failure_reason"] == "cancelled"
        step_ids = [item["step_id"] for item in replay["steps"]]
        assert "notify_cancelled" in step_ids
        assert any("cancelled" in text for _, text in gateway.messages)
    finally:
        spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = original_interval
        await stop_background_runtime()


@pytest.mark.asyncio
async def test_runtime_acceptance_orphan_recovery_surfaces_watchdog_in_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale running tasks should be recovered, completed, and visible in replay."""
    await stop_background_runtime()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    set_agent(CompleteAgent())
    gateway = CaptureGateway()
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    created = await store.create_task(
        "acceptance orphan recovery",
        source="spawn_task",
        session_id="telegram:acceptance-orphan",
    )
    await store.claim_next_task(source="spawn_task", worker_id="dead-worker")

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE tasks
        SET started_at = '2000-01-01 00:00:00',
            last_heartbeat_at = '2000-01-01 00:00:00'
        WHERE task_id = ?
        """,
        (created["task_id"],),
    )
    conn.commit()
    conn.close()

    try:
        await start_background_runtime()
        finished = await _wait_for_task_status(store, created["task_id"], "succeeded")
        replay = await _wait_for_replay(
            audit,
            created["task_id"],
            require_audit_events=True,
            required_step_ids=("agent_run", "notify_result"),
        )

        assert int(finished["attempt_count"]) >= 2
        assert finished["claimed_by"] == ""
        assert replay["task_runs"]
        assert replay["task_runs"][0]["status"] == "success"
        assert replay["audit_events"]
        assert replay["audit_events"][0]["action_type"] == "runtime_watchdog"
        assert "orphan_recovered" in replay["audit_events"][0]["input_summary"]
        assert any("complete" in text for _, text in gateway.messages)
    finally:
        await stop_background_runtime()


@pytest.mark.asyncio
async def test_runtime_acceptance_orphan_recovery_retries_then_succeeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered stale tasks should still honor retry/backoff and succeed later."""
    await stop_background_runtime()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    agent = FailOnceAgent()
    set_task_store(store)
    set_agent(agent)
    gateway = CaptureGateway()
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    original_interval = spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS
    spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = 0.05
    created = await store.create_task(
        "acceptance orphan retry",
        source="spawn_task",
        session_id="telegram:acceptance-orphan-retry",
        max_attempts=3,
        retry_backoff_seconds=1,
    )
    await store.claim_next_task(source="spawn_task", worker_id="dead-worker")

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE tasks
        SET started_at = '2000-01-01 00:00:00',
            last_heartbeat_at = '2000-01-01 00:00:00'
        WHERE task_id = ?
        """,
        (created["task_id"],),
    )
    conn.commit()
    conn.close()

    try:
        await start_background_runtime()
        pending = await _wait_for_task_status(store, created["task_id"], "pending", timeout=1.0)
        assert pending["last_error"] == "transient agent failure"
        assert pending["attempt_count"] == 2
        assert pending["next_attempt_at"] is not None

        finished = await _wait_for_task_status(store, created["task_id"], "succeeded", timeout=2.5)
        replay = await _wait_for_replay(
            audit,
            created["task_id"],
            require_audit_events=True,
            required_step_ids=("agent_run", "notify_result"),
        )

        assert finished["attempt_count"] == 3
        assert finished["last_error"] == ""
        assert [item["status"] for item in replay["task_runs"]] == ["retry", "success"]
        assert replay["task_runs"][0]["failure_reason"] == "transient agent failure"
        assert replay["audit_events"]
        assert replay["audit_events"][0]["action_type"] == "runtime_watchdog"
        assert "orphan_recovered" in replay["audit_events"][0]["input_summary"]
        assert agent.calls == [
            ("acceptance orphan retry", f"task:{created['task_id']}"),
            ("acceptance orphan retry", f"task:{created['task_id']}"),
        ]
        assert any("retry-ok:" in text for _, text in gateway.messages)
    finally:
        spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = original_interval
        await stop_background_runtime()


@pytest.mark.asyncio
async def test_runtime_acceptance_orphan_recovery_respects_rate_limit_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered stale tasks should still wait for the shared rate-limit bucket."""
    await stop_background_runtime()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    agent = RecordingCompleteAgent()
    set_task_store(store)
    set_agent(agent)
    gateway = CaptureGateway()
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    original_interval = spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS
    spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = 0.05
    recovered = await store.create_task(
        "acceptance orphan rate-limit",
        source="spawn_task",
        session_id="telegram:acceptance-orphan-rate-limit",
        rate_limit_key="shared-search",
        rate_limit_window_seconds=1,
        rate_limit_max_claims=1,
        priority=200,
    )
    await store.claim_next_task(source="spawn_task", worker_id="dead-worker")

    limiter = await store.create_task(
        "rate-limit owner",
        source="spawn_task",
        session_id="telegram:acceptance-rate-limit-owner",
        rate_limit_key="shared-search",
        rate_limit_window_seconds=1,
        rate_limit_max_claims=1,
        priority=100,
    )

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE tasks
        SET status = 'succeeded',
            attempt_count = 1,
            updated_at = datetime('now'),
            finished_at = datetime('now'),
            last_claimed_at = datetime('now')
        WHERE task_id = ?
        """,
        (limiter["task_id"],),
    )
    conn.execute(
        """
        UPDATE tasks
        SET started_at = '2000-01-01 00:00:00',
            last_heartbeat_at = '2000-01-01 00:00:00'
        WHERE task_id = ?
        """,
        (recovered["task_id"],),
    )
    conn.commit()
    conn.close()

    try:
        await start_background_runtime()
        pending = await _wait_for_task_status(store, recovered["task_id"], "pending", timeout=0.4)
        assert pending["attempt_count"] == 1
        assert pending["next_attempt_at"] is not None
        await asyncio.sleep(0.2)
        assert agent.calls == []

        finished = await _wait_for_task_status(store, recovered["task_id"], "succeeded", timeout=2.5)
        replay = await _wait_for_replay(
            audit,
            recovered["task_id"],
            require_audit_events=True,
            required_step_ids=("agent_run", "notify_result"),
        )

        assert finished["attempt_count"] == 2
        assert agent.calls == [
            ("acceptance orphan rate-limit", f"task:{recovered['task_id']}"),
        ]
        assert replay["task_runs"]
        assert replay["task_runs"][0]["status"] == "success"
        assert replay["audit_events"]
        assert replay["audit_events"][0]["action_type"] == "runtime_watchdog"
        assert "orphan_recovered" in replay["audit_events"][0]["input_summary"]
        assert any("complete" in text for _, text in gateway.messages)
    finally:
        spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = original_interval
        await stop_background_runtime()


@pytest.mark.asyncio
async def test_runtime_acceptance_orphan_recovery_dead_letters_terminal_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered stale tasks should dead-letter once the retry budget is exhausted."""
    await stop_background_runtime()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    agent = FailAlwaysAgent()
    set_task_store(store)
    set_agent(agent)
    gateway = CaptureGateway()
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    created = await store.create_task(
        "acceptance orphan dead-letter",
        source="spawn_task",
        session_id="telegram:acceptance-orphan-dead-letter",
        max_attempts=2,
    )
    await store.claim_next_task(source="spawn_task", worker_id="dead-worker")

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE tasks
        SET started_at = '2000-01-01 00:00:00',
            last_heartbeat_at = '2000-01-01 00:00:00'
        WHERE task_id = ?
        """,
        (created["task_id"],),
    )
    conn.commit()
    conn.close()

    try:
        await start_background_runtime()
        finished = await _wait_for_task_status(store, created["task_id"], "failed", timeout=1.5)
        replay = await _wait_for_replay(
            audit,
            created["task_id"],
            require_audit_events=True,
            required_step_ids=("agent_run", "notify_failure"),
        )

        assert finished["attempt_count"] == 2
        assert finished["dead_lettered"] is True
        assert finished["dead_letter_reason"] == "terminal agent failure"
        assert finished["last_error"] == "terminal agent failure"
        assert replay["task_runs"]
        assert replay["task_runs"][0]["status"] == "failed"
        assert replay["task_runs"][0]["failure_reason"] == "terminal agent failure"
        assert replay["audit_events"]
        assert replay["audit_events"][0]["action_type"] == "runtime_watchdog"
        assert "orphan_recovered" in replay["audit_events"][0]["input_summary"]
        assert agent.calls == [
            ("acceptance orphan dead-letter", f"task:{created['task_id']}"),
        ]
        assert any("failed: terminal agent failure" in text for _, text in gateway.messages)
    finally:
        await stop_background_runtime()


@pytest.mark.asyncio
async def test_runtime_acceptance_starved_low_priority_task_runs_before_newer_high_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long-waiting low-priority task should run before newer high-priority work."""
    await stop_background_runtime()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    agent = SequencedAgent()
    set_task_store(store)
    set_agent(agent)
    gateway = CaptureGateway()
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    original_interval = spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS
    original_starvation = spawn_module._STARVATION_THRESHOLD_SECONDS
    original_capacity = spawn_module._MAX_BACKGROUND_TASKS
    spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = 0.05
    spawn_module._STARVATION_THRESHOLD_SECONDS = 1
    spawn_module._MAX_BACKGROUND_TASKS = 1

    low = await store.create_task(
        "acceptance starved low",
        source="spawn_task",
        session_id="telegram:acceptance-starved-low",
        priority=10,
    )
    high_first = await store.create_task(
        "acceptance first high",
        source="spawn_task",
        session_id="telegram:acceptance-first-high",
        priority=500,
    )

    try:
        await start_background_runtime()
        await _wait_for_call_count(agent, 1)
        assert agent.calls[0] == ("acceptance first high", f"task:{high_first['task_id']}")

        high_second = await store.create_task(
            "acceptance second high",
            source="spawn_task",
            session_id="telegram:acceptance-second-high",
            priority=500,
        )
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            UPDATE tasks
            SET next_attempt_at = '2000-01-01 00:00:00'
            WHERE task_id = ?
            """,
            (low["task_id"],),
        )
        conn.commit()
        conn.close()
        agent.release_first.set()

        low_finished = await _wait_for_task_status(store, low["task_id"], "succeeded", timeout=2.5)
        high_second_finished = await _wait_for_task_status(
            store,
            high_second["task_id"],
            "succeeded",
            timeout=2.5,
        )

        assert low_finished["status"] == "succeeded"
        assert high_second_finished["status"] == "succeeded"
        assert [item[0] for item in agent.calls[:3]] == [
            "acceptance first high",
            "acceptance starved low",
            "acceptance second high",
        ]
    finally:
        spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = original_interval
        spawn_module._STARVATION_THRESHOLD_SECONDS = original_starvation
        spawn_module._MAX_BACKGROUND_TASKS = original_capacity
        await stop_background_runtime()


@pytest.mark.asyncio
async def test_runtime_acceptance_multi_source_tie_break_prefers_other_sources_first(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mixed runtime sources should prefer unsaturated sources before a second spawn task."""
    await stop_background_runtime()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    claim_order: list[str] = []
    execution_order: list[str] = []

    class OrderedSpawnAgent:
        """Agent stub that holds the first spawn task so order can be inspected."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.release_first = asyncio.Event()

        async def run(self, user_message: str, session_id: str) -> str:
            self.calls.append((user_message, session_id))
            execution_order.append(f"spawn:{user_message}")
            if len(self.calls) == 1:
                await self.release_first.wait()
            return f"mixed-ok:{session_id}:{user_message}"

    async def fake_heartbeat(store: TaskStore, task: dict[str, object]) -> str:
        execution_order.append(f"heartbeat:{task['description']}")
        await asyncio.sleep(0)
        return "heartbeat_ok"

    async def fake_cron(store: TaskStore, task: dict[str, object]) -> str:
        execution_order.append(f"cron:{task['description']}")
        await asyncio.sleep(0)
        return "cron_ok"

    agent = OrderedSpawnAgent()
    set_task_store(store)
    set_agent(agent)
    set_gateway(CaptureGateway())
    original_claim_next_task = store.claim_next_task

    async def record_claim(*args: Any, **kwargs: Any) -> dict[str, object] | None:
        task = await original_claim_next_task(*args, **kwargs)
        if task is not None:
            claim_order.append(f"{task['source']}:{task['description']}")
        return task

    monkeypatch.setattr(store, "claim_next_task", record_claim)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    monkeypatch.setattr(spawn_module, "_run_heartbeat_task", fake_heartbeat)
    monkeypatch.setattr(spawn_module, "_run_cron_task", fake_cron)

    original_interval = spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS
    original_capacity = spawn_module._MAX_BACKGROUND_TASKS
    spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = 0.05
    spawn_module._MAX_BACKGROUND_TASKS = 2

    spawn_first = await store.create_task(
        "acceptance mixed spawn first",
        source="spawn_task",
        session_id="telegram:acceptance-mixed-spawn-first",
        priority=200,
    )
    await store.create_task(
        "acceptance mixed heartbeat",
        task_type="heartbeat",
        source="heartbeat_checklist",
        session_id="heartbeat:acceptance-mixed",
        priority=100,
    )
    await store.create_task(
        "acceptance mixed cron",
        task_type="cron",
        source="cron_job",
        session_id="cron:acceptance-mixed",
        priority=100,
    )
    spawn_second = await store.create_task(
        "acceptance mixed spawn second",
        source="spawn_task",
        session_id="telegram:acceptance-mixed-spawn-second",
        priority=100,
    )

    try:
        await start_background_runtime()
        await _wait_for_length(claim_order, 4, timeout=1.5)

        assert claim_order[0] == "spawn_task:acceptance mixed spawn first"
        assert claim_order[3] == "spawn_task:acceptance mixed spawn second"
        assert set(claim_order[1:3]) == {
            "heartbeat_checklist:acceptance mixed heartbeat",
            "cron_job:acceptance mixed cron",
        }

        agent.release_first.set()
        first_done = await _wait_for_task_status(store, spawn_first["task_id"], "succeeded", timeout=2.5)
        second_done = await _wait_for_task_status(
            store,
            spawn_second["task_id"],
            "succeeded",
            timeout=2.5,
        )

        assert first_done["status"] == "succeeded"
        assert second_done["status"] == "succeeded"
    finally:
        spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = original_interval
        spawn_module._MAX_BACKGROUND_TASKS = original_capacity
        await stop_background_runtime()


@pytest.mark.asyncio
async def test_runtime_acceptance_queue_stall_escalates_runtime_alert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled ready queue should emit a critical alert and then escalate."""
    await stop_background_runtime()
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)

    class RuntimeGateway:
        """Gateway stub that captures runtime alerts on one channel."""

        def __init__(self) -> None:
            self.channels = {"telegram": object()}
            self.messages: list[tuple[str, str]] = []

        async def send_proactive(self, text: str, channel: str = "telegram") -> None:
            self.messages.append((channel, text))

    async def never_claim(store: Any, capacity: int) -> dict[str, object] | None:
        del store, capacity
        return None

    set_task_store(store)
    set_agent(CompleteAgent())
    gateway = RuntimeGateway()
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    monkeypatch.setattr(spawn_module, "_claim_next_runtime_task", never_claim)
    monkeypatch.setattr(spawn_module, "_get_runtime_alert_cooldown_seconds", lambda: 0)
    monkeypatch.setattr(spawn_module, "_get_runtime_stall_threshold_seconds", lambda: 1)

    original_interval = spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS
    spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = 0.05

    created = await store.create_task(
        "acceptance queue stall",
        source="spawn_task",
        session_id="telegram:acceptance-queue-stall",
    )
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE tasks
        SET next_attempt_at = '2000-01-01 00:00:00',
            created_at = '2000-01-01 00:00:00',
            updated_at = '2000-01-01 00:00:00'
        WHERE task_id = ?
        """,
        (created["task_id"],),
    )
    conn.commit()
    conn.close()

    try:
        await start_background_runtime()
        await _wait_for_length(gateway.messages, 2, timeout=1.0)

        assert gateway.messages[0][0] == "telegram"
        assert "health=critical" in gateway.messages[0][1]
        assert "stage=critical_initial" in gateway.messages[0][1]
        assert "queue_stall=1" in gateway.messages[0][1]

        assert gateway.messages[1][0] == "telegram"
        assert "health=critical" in gateway.messages[1][1]
        assert "stage=critical_escalated" in gateway.messages[1][1]
        assert "severity=critical" in gateway.messages[1][1]
        assert "queue_stall=1" in gateway.messages[1][1]

        pending = await store.get_task(created["task_id"])
        entries = await audit.get_recent(limit=20)
        runtime_alerts = [item for item in entries if item["action_type"] == "runtime_alert"]
        escalation_entries = [
            item for item in entries if item["action_type"] == "runtime_alert_escalation"
        ]

        assert pending is not None
        assert pending["status"] == "pending"
        assert runtime_alerts
        assert any("critical_initial" in item["input_summary"] for item in runtime_alerts)
        assert any("critical_escalated" in item["input_summary"] for item in runtime_alerts)
        assert escalation_entries
        assert "critical_escalated" in escalation_entries[0]["input_summary"]
        assert "queue_stall=1" in escalation_entries[0]["input_summary"]
    finally:
        spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = original_interval
        await stop_background_runtime()
