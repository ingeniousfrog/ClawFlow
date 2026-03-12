"""Cross-process spawn runtime recovery integration tests."""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from nanoclaw.runtime.tasks import TaskStore, set_task_store


ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "tests" / "fixtures" / "spawn_runtime_driver.py"


def _spawn_driver(db_path: Path, mode: str) -> subprocess.Popen[str]:
    """Start one subprocess driver for the spawn runtime integration test."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT)
    return subprocess.Popen(
        [sys.executable, str(DRIVER), str(db_path), mode],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _fetch_task(db_path: Path, task_id: str) -> dict[str, str]:
    """Read one task row directly from SQLite."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
    conn.close()
    if row is None:
        raise AssertionError(f"Task `{task_id}` not found.")
    return dict(row)


def _wait_for_status(db_path: Path, task_id: str, statuses: set[str], timeout: float = 5.0) -> dict[str, str]:
    """Poll the task row until it reaches one of the expected statuses."""
    deadline = time.time() + timeout
    last = _fetch_task(db_path, task_id)
    while time.time() < deadline:
        last = _fetch_task(db_path, task_id)
        if str(last.get("status")) in statuses:
            return last
        time.sleep(0.05)
    raise AssertionError(
        f"Timed out waiting for `{task_id}` to reach {sorted(statuses)}; last={last}"
    )


def test_spawn_runtime_recovers_task_after_forced_process_kill(tmp_path: Path) -> None:
    """A killed worker process should leave a recoverable task for the next worker."""
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    set_task_store(store)

    import asyncio

    created = asyncio.run(
        store.create_task(
            "Recover after forced kill",
            source="spawn_task",
            session_id="telegram:recovery",
        )
    )
    task_id = created["task_id"]

    first = _spawn_driver(db_path, "hold")
    second: subprocess.Popen[str] | None = None
    try:
        running = _wait_for_status(db_path, task_id, {"running"}, timeout=5.0)
        first_owner = str(running.get("claimed_by") or "")
        assert first_owner

        if os.name == "nt":
            first.kill()
        else:
            first.send_signal(signal.SIGKILL)
        first.wait(timeout=5)

        time.sleep(1.2)

        second = _spawn_driver(db_path, "recover")
        recovered = _wait_for_status(db_path, task_id, {"succeeded"}, timeout=5.0)
        second.wait(timeout=5)

        assert second.returncode == 0
        assert recovered["status"] == "succeeded"
        assert int(recovered["attempt_count"]) >= 2
        assert str(recovered.get("claimed_by") or "") == ""
        assert recovered["finished_at"] is not None
        assert recovered["last_heartbeat_at"] is None
        assert str(running.get("claimed_by") or "") == first_owner
    finally:
        for proc in (first, second):
            if proc is None:
                continue
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)
