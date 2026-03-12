"""Cron scheduler runtime integration tests."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanoclaw.channels.gateway import set_gateway
from nanoclaw.cron.scheduler import CRON_TASK_SOURCE, Scheduler
from nanoclaw.runtime.tasks import TaskStore, set_task_store
from nanoclaw.security.audit import AuditLog
from nanoclaw.tools.spawn import start_background_runtime, stop_background_runtime


class FakeGateway:
    """Minimal gateway stub for cron runtime tests."""

    def __init__(
        self,
        response: str = "cron complete",
        *,
        proactive_failures_remaining: int = 0,
        targeted_failures_remaining: int = 0,
    ) -> None:
        """Store deterministic response and capture calls."""
        self.response = response
        self.proactive_failures_remaining = proactive_failures_remaining
        self.targeted_failures_remaining = targeted_failures_remaining
        self.channels = {"feishu": _FakeFeishuChannel(self), "telegram": object()}
        self.handle_calls: list[tuple[str, str, str]] = []
        self.notifications: list[tuple[str, str]] = []
        self.targeted_notifications: list[tuple[str, str]] = []

    async def handle_incoming(
        self,
        channel_id: str,
        user_id: str,
        message: str,
        confirm_callback: object = None,
    ) -> str:
        """Capture the cron request and return the configured response."""
        self.handle_calls.append((channel_id, user_id, message))
        return self.response

    async def send_proactive(self, text: str, channel: str = "telegram") -> None:
        """Capture proactive cron notifications."""
        if self.proactive_failures_remaining > 0:
            self.proactive_failures_remaining -= 1
            raise RuntimeError("proactive send failed")
        self.notifications.append((channel, text))


class _FakeFeishuChannel:
    """Small targeted-send stub attached to the fake gateway."""

    def __init__(self, gateway: FakeGateway) -> None:
        """Hold a reference to the parent gateway."""
        self.gateway = gateway

    async def send_proactive_to(self, chat_id: str, text: str) -> bool:
        """Capture targeted Feishu notifications."""
        if self.gateway.targeted_failures_remaining > 0:
            self.gateway.targeted_failures_remaining -= 1
            return False
        self.gateway.targeted_notifications.append((chat_id, text))
        return True


def _init_cron_jobs_table(db_path: Path) -> None:
    """Create the minimal cron job table used by scheduler tests."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS cron_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            message TEXT NOT NULL,
            cron_expr TEXT,
            interval_seconds INTEGER,
            channel TEXT DEFAULT 'telegram',
            target_id TEXT DEFAULT '',
            quiet_start TEXT DEFAULT '',
            quiet_end TEXT DEFAULT '',
            last_run TEXT,
            enabled INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()


async def _wait_for_task_status(
    store: TaskStore,
    task_id: str,
    expected_status: str,
    *,
    timeout: float = 1.0,
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


async def _wait_for_task_with_source(
    store: TaskStore,
    source: str,
    *,
    expected_status: str | None = None,
    timeout: float = 1.0,
) -> dict[str, object]:
    """Poll until one task with the requested source exists and optionally matches status."""
    deadline = asyncio.get_running_loop().time() + timeout
    last_seen: dict[str, object] | None = None
    while True:
        tasks = await store.list_tasks(limit=20)
        for task in tasks:
            if str(task.get("source") or "") != source:
                continue
            last_seen = task
            if expected_status is None or str(task.get("status")) == expected_status:
                return task
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(
                f"No task with source `{source}` reached `{expected_status}`. Last seen: {last_seen}"
            )
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_scheduler_queues_due_interval_job_into_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Due cron jobs should be persisted into the shared runtime queue."""
    db_path = tmp_path / "nanoclaw.db"
    _init_cron_jobs_table(db_path)
    store = TaskStore(db_path)
    set_task_store(store)
    gateway = FakeGateway()
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.core.config.get_data_path", lambda: tmp_path)
    monkeypatch.setattr("nanoclaw.tools.spawn.wake_background_runtime", lambda: None)

    scheduler = Scheduler(SimpleNamespace(), gateway)
    job_id = await scheduler.add_job(
        "Morning digest",
        "Summarize today's priorities",
        interval_seconds=60,
        channel="feishu",
        target_id="oc_chat_1",
    )

    await scheduler._check_and_run()

    tasks = await store.list_tasks(limit=5)
    jobs = await scheduler.list_jobs()
    assert len(tasks) == 1
    assert tasks[0]["source"] == CRON_TASK_SOURCE
    assert tasks[0]["task_type"] == "cron"
    assert tasks[0]["status"] == "pending"
    assert tasks[0]["payload"]["job_id"] == job_id
    assert tasks[0]["payload"]["job_name"] == "Morning digest"
    assert tasks[0]["payload"]["channel"] == "feishu"
    assert tasks[0]["payload"]["target_id"] == "oc_chat_1"
    assert tasks[0]["payload"]["quiet_start"] == ""
    assert tasks[0]["payload"]["quiet_end"] == ""
    assert jobs[0]["last_run"] is not None


@pytest.mark.asyncio
async def test_scheduler_update_job_persists_new_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Updating a cron job should persist the new schedule payload."""
    db_path = tmp_path / "nanoclaw.db"
    _init_cron_jobs_table(db_path)
    gateway = FakeGateway()
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.core.config.get_data_path", lambda: tmp_path)

    scheduler = Scheduler(SimpleNamespace(), gateway)
    job_id = await scheduler.add_job(
        "Old job",
        "old payload",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await scheduler.update_job(
        job_id,
        name="New job",
        message="new payload",
        cron_expr="15 9 * * 1-5",
        interval_seconds=None,
        channel="feishu",
        target_id="oc_chat_1",
        quiet_start="22:00",
        quiet_end="08:00",
    )

    jobs = await scheduler.list_jobs()
    assert len(jobs) == 1
    assert jobs[0]["name"] == "New job"
    assert jobs[0]["message"] == "new payload"
    assert jobs[0]["cron_expr"] == "15 9 * * 1-5"
    assert jobs[0]["target_id"] == "oc_chat_1"
    assert jobs[0]["quiet_start"] == "22:00"
    assert jobs[0]["quiet_end"] == "08:00"


@pytest.mark.asyncio
async def test_scheduler_runtime_state_includes_recent_schedule_signal_timeline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runtime-state jobs should include a compact recent schedule signal timeline."""
    db_path = tmp_path / "nanoclaw.db"
    _init_cron_jobs_table(db_path)
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    gateway = FakeGateway()
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.core.config.get_data_path", lambda: tmp_path)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    scheduler = Scheduler(SimpleNamespace(), gateway)
    job_id = await scheduler.add_job(
        "Signal timeline",
        "Show recent schedule signals",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await audit.log(
        action_type="schedule_alert",
        tool_name="cron_job",
        input_summary="job_id=1 stage=attention_initial repeat_count=1 reason=latest_execution_failed",
        output_summary="Sent proactive schedule health alert.",
        status="error",
        session_id=f"schedule:{job_id}",
    )
    await audit.log(
        action_type="schedule_recovery",
        tool_name="cron_job",
        input_summary="job_id=1 previous_stage=attention_initial previous_repeat_count=1 health=healthy",
        output_summary="Sent proactive schedule recovery notice.",
        status="success",
        session_id=f"schedule:{job_id}",
    )

    jobs = await scheduler.list_jobs_with_runtime_state()

    assert len(jobs) == 1
    timeline = jobs[0]["runtime"]["signal_timeline"]
    assert timeline[0]["label"] == "recovery"
    assert timeline[0]["detail"] == "healthy after attention_initial"
    assert timeline[1]["label"] == "alert"
    assert timeline[1]["detail"] == "attention_initial x1"


@pytest.mark.asyncio
async def test_background_runtime_executes_persisted_cron_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persisted cron tasks should run through the shared background runtime."""
    await stop_background_runtime()
    db_path = tmp_path / "nanoclaw.db"
    _init_cron_jobs_table(db_path)
    store = TaskStore(db_path)
    set_task_store(store)
    gateway = FakeGateway(response="Cron summary ready.")
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.core.config.get_data_path", lambda: tmp_path)

    scheduler = Scheduler(SimpleNamespace(), gateway)
    await scheduler.add_job(
        "AI roundup",
        "Collect the top AI headlines",
        interval_seconds=60,
        channel="feishu",
    )

    try:
        await start_background_runtime()
        await scheduler._check_and_run()
        tasks = await store.list_tasks(limit=5)
        task_id = str(tasks[0]["task_id"])
        finished = await _wait_for_task_status(store, task_id, "succeeded")
        steps = await store.list_task_steps(task_id)
        step_map = {step["step_id"]: step for step in steps}

        assert finished["source"] == CRON_TASK_SOURCE
        assert gateway.handle_calls == [
            ("cron", "system", "Collect the top AI headlines")
        ]
        assert gateway.notifications == [
            ("feishu", "**AI roundup**\n\nCron summary ready.")
        ]
        assert gateway.targeted_notifications == []
        assert step_map["cron_run"]["status"] == "succeeded"
        assert step_map["cron_notify"]["status"] == "succeeded"
        assert step_map["cron_run"]["output"]["result_text"] == "Cron summary ready."
    finally:
        await stop_background_runtime()


@pytest.mark.asyncio
async def test_background_runtime_recovers_stale_cron_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered stale cron tasks should finish through the shared runtime."""
    await stop_background_runtime()
    db_path = tmp_path / "nanoclaw.db"
    _init_cron_jobs_table(db_path)
    store = TaskStore(db_path)
    set_task_store(store)
    gateway = FakeGateway(response="Recovered cron summary.")
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.core.config.get_data_path", lambda: tmp_path)
    monkeypatch.setattr("nanoclaw.tools.spawn.wake_background_runtime", lambda: None)

    scheduler = Scheduler(SimpleNamespace(), gateway)
    await scheduler.add_job(
        "Recovered cron",
        "Recover the daily cron summary",
        interval_seconds=60,
        channel="feishu",
    )
    await scheduler._check_and_run()
    tasks = await store.list_tasks(limit=5)
    task_id = str(tasks[0]["task_id"])
    await store.claim_next_task(source=CRON_TASK_SOURCE, worker_id="dead-worker")

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE tasks
        SET last_heartbeat_at = '2000-01-01 00:00:00'
        WHERE task_id = ?
        """,
        (task_id,),
    )
    conn.commit()
    conn.close()

    try:
        await start_background_runtime()
        finished = await _wait_for_task_status(store, task_id, "succeeded")

        assert finished["source"] == CRON_TASK_SOURCE
        assert gateway.handle_calls == [
            ("cron", "system", "Recover the daily cron summary")
        ]
        assert gateway.notifications == [
            ("feishu", "**Recovered cron**\n\nRecovered cron summary.")
        ]
        assert gateway.targeted_notifications == []
    finally:
        await stop_background_runtime()


@pytest.mark.asyncio
async def test_background_runtime_targets_original_feishu_chat_for_cron_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cron tasks with a target chat should use targeted Feishu proactive send."""
    await stop_background_runtime()
    db_path = tmp_path / "nanoclaw.db"
    _init_cron_jobs_table(db_path)
    store = TaskStore(db_path)
    set_task_store(store)
    gateway = FakeGateway(response="Targeted cron summary.")
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.core.config.get_data_path", lambda: tmp_path)

    scheduler = Scheduler(SimpleNamespace(), gateway)
    await scheduler.add_job(
        "Targeted cron",
        "Send to the original Feishu chat",
        interval_seconds=60,
        channel="feishu",
        target_id="oc_chat_target",
    )

    try:
        await start_background_runtime()
        await scheduler._check_and_run()
        tasks = await store.list_tasks(limit=5)
        task_id = str(tasks[0]["task_id"])
        finished = await _wait_for_task_status(store, task_id, "succeeded")
        steps = await store.list_task_steps(task_id)
        step_map = {step["step_id"]: step for step in steps}

        assert finished["source"] == CRON_TASK_SOURCE
        assert gateway.notifications == []
        assert gateway.targeted_notifications == [
            ("oc_chat_target", "**Targeted cron**\n\nTargeted cron summary.")
        ]
        assert step_map["cron_notify"]["status"] == "succeeded"
        assert step_map["cron_notify"]["output"]["target_id"] == "oc_chat_target"
    finally:
        await stop_background_runtime()


@pytest.mark.asyncio
async def test_background_runtime_suppresses_cron_notification_inside_quiet_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cron notifications should be skipped when the local time is inside the quiet window."""
    await stop_background_runtime()
    db_path = tmp_path / "nanoclaw.db"
    _init_cron_jobs_table(db_path)
    store = TaskStore(db_path)
    set_task_store(store)
    gateway = FakeGateway(response="Muted cron summary.")
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.core.config.get_data_path", lambda: tmp_path)

    class _QuietDatetime:
        """Deterministic local clock stub for quiet-window checks."""

        @classmethod
        def now(cls) -> datetime:
            """Return a fixed local wall-clock time."""
            return datetime(2026, 3, 8, 23, 30)

    monkeypatch.setattr("nanoclaw.tools.spawn.datetime", _QuietDatetime)

    scheduler = Scheduler(SimpleNamespace(), gateway)
    await scheduler.add_job(
        "Quiet cron",
        "Send inside the mute window",
        interval_seconds=60,
        channel="feishu",
        target_id="oc_chat_target",
        quiet_start="22:00",
        quiet_end="08:00",
    )

    try:
        await start_background_runtime()
        await scheduler._check_and_run()
        tasks = await store.list_tasks(limit=5)
        task_id = str(tasks[0]["task_id"])
        finished = await _wait_for_task_status(store, task_id, "succeeded")
        steps = await store.list_task_steps(task_id)
        step_map = {step["step_id"]: step for step in steps}

        assert finished["source"] == CRON_TASK_SOURCE
        assert gateway.notifications == []
        assert gateway.targeted_notifications == []
        assert step_map["cron_notify"]["status"] == "succeeded"
        assert step_map["cron_notify"]["output"]["kind"] == "cron_suppressed"
        assert step_map["cron_notify"]["output"]["quiet_window"] == "22:00-08:00"
    finally:
        await stop_background_runtime()


@pytest.mark.asyncio
async def test_background_runtime_queues_cron_delivery_retry_after_notify_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A notify failure should schedule a follow-up delivery task instead of rerunning cron."""
    await stop_background_runtime()
    db_path = tmp_path / "nanoclaw.db"
    _init_cron_jobs_table(db_path)
    store = TaskStore(db_path)
    set_task_store(store)
    gateway = FakeGateway(
        response="Retry this cron result.",
        proactive_failures_remaining=1,
    )
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.core.config.get_data_path", lambda: tmp_path)

    scheduler = Scheduler(SimpleNamespace(), gateway)
    await scheduler.add_job(
        "Retry cron",
        "Generate once and retry delivery if notify fails",
        interval_seconds=60,
        channel="feishu",
    )

    try:
        await start_background_runtime()
        await scheduler._check_and_run()
        original = await _wait_for_task_with_source(
            store,
            CRON_TASK_SOURCE,
            expected_status="succeeded",
        )
        follow_up = await _wait_for_task_with_source(
            store,
            "cron_delivery_retry",
            expected_status="succeeded",
        )
        original_steps = await store.list_task_steps(str(original["task_id"]))
        retry_steps = await store.list_task_steps(str(follow_up["task_id"]))
        original_step_map = {step["step_id"]: step for step in original_steps}
        retry_step_map = {step["step_id"]: step for step in retry_steps}

        assert gateway.handle_calls == [
            ("cron", "system", "Generate once and retry delivery if notify fails")
        ]
        assert gateway.notifications == [
            ("feishu", "**Retry cron**\n\nRetry this cron result.")
        ]
        assert original_step_map["cron_run"]["status"] == "succeeded"
        assert original_step_map["cron_notify"]["status"] == "succeeded"
        assert original_step_map["cron_notify"]["output"]["kind"] == "cron_delivery_retry_scheduled"
        assert original_step_map["cron_notify"]["output"]["retry_task_id"] == follow_up["task_id"]
        assert retry_step_map["cron_delivery_notify"]["status"] == "succeeded"
        assert retry_step_map["cron_delivery_notify"]["output"]["kind"] == "cron_retry_success"
        assert follow_up["payload"]["original_task_id"] == original["task_id"]
    finally:
        await stop_background_runtime()
