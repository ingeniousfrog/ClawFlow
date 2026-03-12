"""Heartbeat runner tests."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from nanoclaw.channels.gateway import set_gateway
from nanoclaw.core.config import HeartbeatConfig
from nanoclaw.cron.heartbeat import HEARTBEAT_TASK_SOURCE, HeartbeatRunner
from nanoclaw.runtime.tasks import TaskStore, set_task_store
from nanoclaw.tools.spawn import start_background_runtime, stop_background_runtime


class FakeGateway:
    """Minimal gateway stub for heartbeat tests."""

    def __init__(self, response: str, channels: dict[str, object] | None = None) -> None:
        """Store deterministic response and capture calls."""
        self.response = response
        self.channels = channels or {}
        self.handle_calls: list[tuple[str, str, str]] = []
        self.notifications: list[tuple[str, str]] = []

    async def handle_incoming(
        self,
        channel_id: str,
        user_id: str,
        message: str,
        confirm_callback: object = None,
    ) -> str:
        """Capture the heartbeat prompt and return the configured response."""
        self.handle_calls.append((channel_id, user_id, message))
        return self.response

    async def send_proactive(self, text: str, channel: str = "telegram") -> None:
        """Capture proactive heartbeat notifications."""
        self.notifications.append((channel, text))


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


def test_heartbeat_skips_when_checklist_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing checklist file should skip silently."""
    gateway = FakeGateway(response="unused", channels={"telegram": object()})
    runner = HeartbeatRunner(HeartbeatConfig(enabled=True), gateway)
    monkeypatch.setattr("nanoclaw.cron.heartbeat.get_workspace_path", lambda: tmp_path)

    async def _run() -> None:
        result = await runner.run_once()
        assert result == "missing"
        assert gateway.handle_calls == []
        assert gateway.notifications == []

    asyncio.run(_run())


def test_heartbeat_returns_ok_without_notification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HEARTBEAT_OK should suppress proactive output."""
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] Check AI feeds\n", encoding="utf-8")
    gateway = FakeGateway(response="HEARTBEAT_OK", channels={"telegram": object()})
    runner = HeartbeatRunner(HeartbeatConfig(enabled=True), gateway)
    monkeypatch.setattr("nanoclaw.cron.heartbeat.get_workspace_path", lambda: tmp_path)

    async def _run() -> None:
        result = await runner.run_once()
        assert result == "ok"
        assert len(gateway.handle_calls) == 1
        assert "Check AI feeds" in gateway.handle_calls[0][2]
        assert gateway.notifications == []

    asyncio.run(_run())


def test_heartbeat_notifies_using_available_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heartbeat findings should be pushed to the preferred available channel."""
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] Review daily AI changes\n", encoding="utf-8")
    gateway = FakeGateway(
        response="Two new items need attention.",
        channels={"feishu": object()},
    )
    config = HeartbeatConfig(
        enabled=True,
        notify_channel="telegram",
        checklist_path="HEARTBEAT.md",
    )
    runner = HeartbeatRunner(config, gateway)
    monkeypatch.setattr("nanoclaw.cron.heartbeat.get_workspace_path", lambda: tmp_path)

    async def _run() -> None:
        result = await runner.run_once()
        assert result == "notified"
        assert gateway.notifications == [
            ("feishu", "**Heartbeat**\n\nTwo new items need attention.")
        ]

    asyncio.run(_run())


def test_heartbeat_blocks_checklist_path_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checklist path must stay inside workspace."""
    gateway = FakeGateway(response="unused", channels={"telegram": object()})
    runner = HeartbeatRunner(
        HeartbeatConfig(enabled=True, checklist_path="../outside.md"),
        gateway,
    )
    monkeypatch.setattr("nanoclaw.cron.heartbeat.get_workspace_path", lambda: tmp_path)

    async def _run() -> None:
        result = await runner.run_once()
        assert result == "blocked"
        assert gateway.handle_calls == []
        assert gateway.notifications == []

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_heartbeat_enqueue_persists_runtime_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heartbeat enqueue should persist one shared-runtime task."""
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)
    gateway = FakeGateway(response="unused", channels={"console": object()})
    runner = HeartbeatRunner(
        HeartbeatConfig(enabled=True, checklist_path="HEARTBEAT.md", notify_channel="console"),
        gateway,
    )
    monkeypatch.setattr("nanoclaw.cron.heartbeat.get_workspace_path", lambda: tmp_path)

    result = await runner.enqueue_once()

    tasks = await store.list_tasks(limit=5)
    assert result == "queued"
    assert len(tasks) == 1
    assert tasks[0]["source"] == HEARTBEAT_TASK_SOURCE
    assert tasks[0]["status"] == "pending"
    assert tasks[0]["session_id"] == "heartbeat:system"


@pytest.mark.asyncio
async def test_background_runtime_executes_heartbeat_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Heartbeat tasks should run through the shared background runtime."""
    await stop_background_runtime()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] Review AI feed changes\n", encoding="utf-8")
    gateway = FakeGateway(response="Two new items need attention.", channels={"console": object()})
    runner = HeartbeatRunner(
        HeartbeatConfig(enabled=True, checklist_path="HEARTBEAT.md", notify_channel="console"),
        gateway,
    )
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.cron.heartbeat.get_workspace_path", lambda: tmp_path)
    monkeypatch.setattr(
        "nanoclaw.core.config.get_config",
        lambda: SimpleNamespace(heartbeat=runner.config),
    )

    try:
        await start_background_runtime()
        enqueue_result = await runner.enqueue_once()
        tasks = await store.list_tasks(limit=5)
        task_id = str(tasks[0]["task_id"])
        finished = await _wait_for_task_status(store, task_id, "succeeded")
        steps = await store.list_task_steps(task_id)

        assert enqueue_result == "queued"
        assert finished["source"] == HEARTBEAT_TASK_SOURCE
        assert len(gateway.handle_calls) == 1
        assert gateway.notifications == [
            ("console", "**Heartbeat**\n\nTwo new items need attention.")
        ]
        assert [step["step_id"] for step in steps] == ["heartbeat_run"]
        assert steps[0]["output"]["status"] == "notified"
    finally:
        await stop_background_runtime()


@pytest.mark.asyncio
async def test_background_runtime_recovers_stale_heartbeat_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Recovered stale heartbeat tasks should finish through the shared runtime."""
    await stop_background_runtime()
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] Review AI feed changes\n", encoding="utf-8")
    gateway = FakeGateway(response="Recovered heartbeat findings.", channels={"console": object()})
    runner = HeartbeatRunner(
        HeartbeatConfig(enabled=True, checklist_path="HEARTBEAT.md", notify_channel="console"),
        gateway,
    )
    set_gateway(gateway)
    monkeypatch.setattr("nanoclaw.cron.heartbeat.get_workspace_path", lambda: tmp_path)
    monkeypatch.setattr(
        "nanoclaw.core.config.get_config",
        lambda: SimpleNamespace(heartbeat=runner.config),
    )
    monkeypatch.setattr("nanoclaw.tools.spawn.wake_background_runtime", lambda: None)

    try:
        enqueue_result = await runner.enqueue_once()
        tasks = await store.list_tasks(limit=5)
        task_id = str(tasks[0]["task_id"])
        await store.claim_next_task(source=HEARTBEAT_TASK_SOURCE, worker_id="dead-worker")

        conn = sqlite3.connect(tmp_path / "tasks.db")
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

        await start_background_runtime()
        finished = await _wait_for_task_status(store, task_id, "succeeded")

        assert enqueue_result == "queued"
        assert finished["source"] == HEARTBEAT_TASK_SOURCE
        assert len(gateway.handle_calls) == 1
        assert gateway.notifications == [
            ("console", "**Heartbeat**\n\nRecovered heartbeat findings.")
        ]
    finally:
        await stop_background_runtime()
