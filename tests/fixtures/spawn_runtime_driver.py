"""Subprocess driver for spawn runtime recovery tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from nanoclaw.channels.gateway import set_gateway
from nanoclaw.core.agent import set_agent
from nanoclaw.runtime.tasks import get_task_store, set_task_store, TaskStore
import nanoclaw.tools.spawn as spawn_module


class HoldAgent:
    """Agent stub that never completes."""

    async def run(self, user_message: str, session_id: str) -> str:
        """Block until the subprocess is killed."""
        await asyncio.Event().wait()
        return ""


class CompleteAgent:
    """Agent stub that finishes immediately."""

    async def run(self, user_message: str, session_id: str) -> str:
        """Return one deterministic result."""
        return f"completed:{session_id}:{user_message}"


class SilentGateway:
    """No-op proactive notifier."""

    async def send_proactive(self, text: str, channel: str = "telegram") -> None:
        """Ignore proactive notifications inside the subprocess test harness."""
        return None


async def _wait_for_terminal_status(timeout: float = 5.0) -> int:
    """Wait until all persisted spawn tasks are no longer pending or running."""
    deadline = asyncio.get_running_loop().time() + timeout
    store = get_task_store()
    while asyncio.get_running_loop().time() < deadline:
        tasks = await store.list_tasks(limit=20)
        spawn_tasks = [item for item in tasks if item.get("source") == "spawn_task"]
        if spawn_tasks and all(
            item.get("status") in {"succeeded", "failed", "cancelled"}
            for item in spawn_tasks
        ):
            return 0
        await asyncio.sleep(0.05)
    return 2


async def main() -> int:
    """Run one subprocess mode for integration testing."""
    if len(sys.argv) != 3:
        print("usage: spawn_runtime_driver.py <db-path> <hold|recover>", file=sys.stderr)
        return 2

    db_path = Path(sys.argv[1])
    mode = sys.argv[2].strip().lower()
    set_task_store(TaskStore(db_path))
    set_gateway(SilentGateway())

    spawn_module._MAX_BACKGROUND_TASKS = 1
    spawn_module._TASK_LEASE_TIMEOUT_SECONDS = 1
    spawn_module._TASK_HEARTBEAT_INTERVAL_SECONDS = 1

    if mode == "hold":
        set_agent(HoldAgent())
        await spawn_module.start_background_runtime()
        await asyncio.Event().wait()
        return 0

    if mode == "recover":
        set_agent(CompleteAgent())
        await spawn_module.start_background_runtime()
        try:
            return await _wait_for_terminal_status()
        finally:
            await spawn_module.stop_background_runtime()

    print(f"unsupported mode: {mode}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
