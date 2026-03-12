"""Task store tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nanoclaw.runtime.tasks import TaskStore


@pytest.mark.asyncio
async def test_task_store_create_and_persist(tmp_path: Path) -> None:
    """Created tasks should persist and be readable from a new store instance."""
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)

    created = await store.create_task(
        "Research latest AI papers",
        task_type="background",
        payload={"topic": "ai"},
        source="spawn_task",
        session_id="cli:user",
    )

    assert created["status"] == "pending"
    assert created["task_type"] == "background"
    assert created["payload"] == {"topic": "ai"}
    assert created["priority"] == 100
    assert created["timeout_seconds"] == 1800
    assert created["max_attempts"] == 2
    assert created["retry_backoff_seconds"] == 30
    assert created["rate_limit_key"] == ""
    assert created["rate_limit_window_seconds"] == 0
    assert created["rate_limit_max_claims"] == 0
    assert created["next_attempt_at"] is not None
    assert created["last_claimed_at"] is None
    assert created["cancel_requested"] is False
    assert created["dead_lettered"] is False
    assert created["dead_letter_reason"] == ""
    assert created["idempotency_key"] == ""

    reloaded = TaskStore(db_path)
    fetched = await reloaded.get_task(created["task_id"])
    assert fetched is not None
    assert fetched["task_id"] == created["task_id"]
    assert fetched["source"] == "spawn_task"
    assert fetched["session_id"] == "cli:user"


@pytest.mark.asyncio
async def test_task_store_migrates_legacy_db_before_creating_new_indexes(
    tmp_path: Path,
) -> None:
    """Legacy databases should gain new columns before index creation runs."""
    db_path = tmp_path / "tasks.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE tasks (
            task_id TEXT PRIMARY KEY,
            task_type TEXT NOT NULL DEFAULT 'background',
            status TEXT NOT NULL DEFAULT 'pending',
            description TEXT NOT NULL,
            payload TEXT NOT NULL DEFAULT '{}',
            source TEXT NOT NULL DEFAULT '',
            session_id TEXT NOT NULL DEFAULT '',
            attempt_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            started_at TEXT,
            finished_at TEXT
        );
        """
    )
    conn.commit()
    conn.close()

    store = TaskStore(db_path)
    created = await store.create_task(
        "dedupe me",
        source="spawn_task",
        idempotency_key="same-key",
    )

    conn = sqlite3.connect(db_path)
    columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
    }
    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(tasks)").fetchall()
    }
    conn.close()

    assert "idempotency_key" in columns
    assert "idx_tasks_idempotency" in indexes
    assert created["idempotency_key"] == "same-key"


@pytest.mark.asyncio
async def test_task_store_reuses_existing_task_for_same_idempotency_key(tmp_path: Path) -> None:
    """The same idempotency key should return the existing active task instead of inserting."""
    store = TaskStore(tmp_path / "tasks.db")

    first = await store.create_task(
        "dedupe me",
        source="spawn_task",
        idempotency_key="same-key",
    )
    second = await store.create_task(
        "dedupe me again",
        source="spawn_task",
        idempotency_key="same-key",
    )
    tasks = await store.list_tasks(limit=10)

    assert first["reused_existing"] is False
    assert second["reused_existing"] is True
    assert second["task_id"] == first["task_id"]
    assert len(tasks) == 1


@pytest.mark.asyncio
async def test_task_store_allows_new_task_after_failed_idempotent_attempt(tmp_path: Path) -> None:
    """A failed or cancelled idempotent task should not block a new task with the same key."""
    store = TaskStore(tmp_path / "tasks.db")
    first = await store.create_task(
        "rerun same key",
        source="spawn_task",
        idempotency_key="rerun-key",
        max_attempts=1,
    )
    await store.claim_next_task(source="spawn_task", worker_id="worker-a")
    await store.fail_task_attempt(first["task_id"], last_error="fatal")

    second = await store.create_task(
        "rerun same key",
        source="spawn_task",
        idempotency_key="rerun-key",
    )

    assert second["reused_existing"] is False
    assert second["task_id"] != first["task_id"]


@pytest.mark.asyncio
async def test_task_store_valid_transitions_update_attempts_and_timestamps(
    tmp_path: Path,
) -> None:
    """Valid state transitions should update counters and timestamps."""
    store = TaskStore(tmp_path / "tasks.db")
    task = await store.create_task("Long job")

    running = await store.transition_task(task["task_id"], "running")
    assert running["status"] == "running"
    assert running["attempt_count"] == 1
    assert running["started_at"] is not None
    assert running["finished_at"] is None

    failed = await store.transition_task(
        task["task_id"],
        "failed",
        last_error="network timeout",
    )
    assert failed["status"] == "failed"
    assert failed["last_error"] == "network timeout"
    assert failed["finished_at"] is not None

    pending = await store.transition_task(task["task_id"], "pending")
    assert pending["status"] == "pending"
    assert pending["started_at"] is None
    assert pending["finished_at"] is None

    succeeded = await store.transition_task(task["task_id"], "running")
    succeeded = await store.transition_task(task["task_id"], "succeeded")
    assert succeeded["status"] == "succeeded"
    assert succeeded["attempt_count"] == 2
    assert succeeded["finished_at"] is not None
    assert succeeded["last_error"] == ""


@pytest.mark.asyncio
async def test_task_store_rejects_invalid_transition(tmp_path: Path) -> None:
    """Terminal tasks should reject unsupported transitions."""
    store = TaskStore(tmp_path / "tasks.db")
    task = await store.create_task("One-shot task")
    await store.transition_task(task["task_id"], "running")
    await store.transition_task(task["task_id"], "succeeded")

    with pytest.raises(ValueError, match="Cannot transition task"):
        await store.transition_task(task["task_id"], "running")


@pytest.mark.asyncio
async def test_task_store_reuses_completed_checkpoint_step(tmp_path: Path) -> None:
    """A completed checkpoint step should be reusable when the input hash matches."""
    store = TaskStore(tmp_path / "tasks.db")
    task = await store.create_task("checkpoint me", source="spawn_task")

    started = await store.start_task_step(
        task["task_id"],
        "agent_run",
        step_name="agent_run",
        input_payload={"message": "checkpoint me"},
        is_checkpoint=True,
    )
    assert started["status"] == "running"
    assert started["attempt_count"] == 1

    finished = await store.complete_task_step(
        task["task_id"],
        "agent_run",
        output_payload={"result_text": "cached result"},
    )
    assert finished["status"] == "succeeded"
    assert finished["output"]["result_text"] == "cached result"

    reused = await store.start_task_step(
        task["task_id"],
        "agent_run",
        step_name="agent_run",
        input_payload={"message": "checkpoint me"},
        is_checkpoint=True,
    )
    assert reused["status"] == "succeeded"
    assert reused["attempt_count"] == 1
    assert reused["output"]["result_text"] == "cached result"


@pytest.mark.asyncio
async def test_requeue_task_clears_persisted_steps(tmp_path: Path) -> None:
    """Manual requeue should drop old step checkpoints before a fresh rerun."""
    store = TaskStore(tmp_path / "tasks.db")
    task = await store.create_task("rerun me", source="spawn_task")
    await store.transition_task(task["task_id"], "running")
    await store.transition_task(task["task_id"], "failed", last_error="boom")
    await store.start_task_step(
        task["task_id"],
        "agent_run",
        step_name="agent_run",
        input_payload={"message": "rerun me"},
        is_checkpoint=True,
    )
    await store.complete_task_step(
        task["task_id"],
        "agent_run",
        output_payload={"result_text": "old result"},
    )

    requeued = await store.requeue_task(task["task_id"])
    steps = await store.list_task_steps(task["task_id"])

    assert requeued["status"] == "pending"
    assert steps == []


@pytest.mark.asyncio
async def test_rearm_task_preserves_role_task_identity_and_steps(tmp_path: Path) -> None:
    """Role-task rearm should reuse the same task row without clearing old steps."""
    store = TaskStore(tmp_path / "tasks.db")
    task = await store.create_task(
        "critic turn",
        task_type="workflow_role",
        payload={"task_key": "critic@post_tools", "turn_index": 1},
        source="workflow_role",
        max_attempts=2,
        idempotency_key="role-turn-key",
    )
    await store.transition_task(task["task_id"], "running")
    await store.start_task_step(
        task["task_id"],
        "role_runtime_ack",
        step_name="role_runtime_ack",
        input_payload={"turn_index": 1},
        is_checkpoint=True,
        idempotent=True,
    )
    await store.complete_task_step(
        task["task_id"],
        "role_runtime_ack",
        output_payload={"result_text": "turn 1", "turn_index": 1},
    )
    completed = await store.transition_task(task["task_id"], "succeeded")

    rearmed = await store.rearm_task(
        completed["task_id"],
        payload={"task_key": "critic@post_tools", "turn_index": 2, "turn_reason": "upstream_changed"},
    )

    steps = await store.list_task_steps(task["task_id"])
    assert rearmed["task_id"] == task["task_id"]
    assert rearmed["status"] == "pending"
    assert rearmed["attempt_count"] == completed["attempt_count"]
    assert rearmed["payload"]["turn_index"] == 2
    assert rearmed["payload"]["turn_reason"] == "upstream_changed"
    assert len(steps) == 1
    assert steps[0]["output"]["result_text"] == "turn 1"


@pytest.mark.asyncio
async def test_refresh_pending_task_payload_updates_role_task_in_place(tmp_path: Path) -> None:
    """Pending role-task payload refresh should keep the same task row and attempt state."""
    store = TaskStore(tmp_path / "tasks.db")
    task = await store.create_task(
        "executor turn",
        task_type="workflow_role",
        payload={"task_key": "executor@tool_phase", "turn_index": 1, "turn_reason": "initial"},
        source="workflow_role",
        max_attempts=2,
        idempotency_key="role-pending-key",
    )

    refreshed = await store.refresh_pending_task_payload(
        task["task_id"],
        payload={
            "task_key": "executor@tool_phase",
            "turn_index": 1,
            "turn_reason": "recovery_refresh",
            "recovery_task_key": "executor@tool_phase",
        },
    )

    assert refreshed["task_id"] == task["task_id"]
    assert refreshed["status"] == "pending"
    assert refreshed["attempt_count"] == 0
    assert refreshed["payload"]["turn_index"] == 1
    assert refreshed["payload"]["turn_reason"] == "recovery_refresh"
    assert refreshed["payload"]["recovery_task_key"] == "executor@tool_phase"


@pytest.mark.asyncio
async def test_refresh_running_task_payload_updates_role_task_in_place(tmp_path: Path) -> None:
    """Running role-task payload refresh should keep the same task row and claim state."""
    store = TaskStore(tmp_path / "tasks.db")
    task = await store.create_task(
        "executor running turn",
        task_type="workflow_role",
        payload={"task_key": "executor@tool_phase", "turn_index": 1, "turn_reason": "initial"},
        source="workflow_role",
        max_attempts=2,
        idempotency_key="role-running-key",
    )
    running = await store.transition_task(task["task_id"], "running")

    refreshed = await store.refresh_running_task_payload(
        running["task_id"],
        payload={
            "task_key": "executor@tool_phase",
            "turn_index": 1,
            "turn_reason": "initial",
            "deferred_recovery_payload": {"turn_reason": "recovery_reentry"},
        },
    )

    assert refreshed["task_id"] == task["task_id"]
    assert refreshed["status"] == "running"
    assert refreshed["attempt_count"] == running["attempt_count"]
    assert refreshed["payload"]["turn_index"] == 1
    assert refreshed["payload"]["deferred_recovery_payload"]["turn_reason"] == "recovery_reentry"


@pytest.mark.asyncio
async def test_task_store_lists_recent_tasks_by_status(tmp_path: Path) -> None:
    """Listing should support recent ordering and status filtering."""
    store = TaskStore(tmp_path / "tasks.db")
    first = await store.create_task("first")
    second = await store.create_task("second")
    await store.transition_task(first["task_id"], "running")
    await store.transition_task(first["task_id"], "failed", last_error="boom")

    failed_only = await store.list_tasks(status="failed")
    all_tasks = await store.list_tasks(limit=10)

    assert len(failed_only) == 1
    assert failed_only[0]["task_id"] == first["task_id"]
    assert {item["task_id"] for item in all_tasks} == {
        first["task_id"],
        second["task_id"],
    }


@pytest.mark.asyncio
async def test_task_store_claims_pending_task_by_source(tmp_path: Path) -> None:
    """Claiming should atomically move one matching pending task to running."""
    store = TaskStore(tmp_path / "tasks.db")
    await store.create_task("cron task", source="cron")
    queued = await store.create_task("spawn task", source="spawn_task")

    claimed = await store.claim_next_task(source="spawn_task", worker_id="worker-a")

    assert claimed is not None
    assert claimed["task_id"] == queued["task_id"]
    assert claimed["status"] == "running"
    assert claimed["attempt_count"] == 1
    assert claimed["claimed_by"] == "worker-a"
    assert claimed["last_heartbeat_at"] is not None


@pytest.mark.asyncio
async def test_task_store_can_defer_running_task_without_dead_lettering(
    tmp_path: Path,
) -> None:
    """Deferred running tasks should go back to pending with a later retry time."""
    store = TaskStore(tmp_path / "tasks.db")
    created = await store.create_task("wait until window ends", source="cron_delivery_retry")
    claimed = await store.claim_next_task(source="cron_delivery_retry", worker_id="worker-a")

    assert claimed is not None
    deferred = await store.defer_task_attempt(
        created["task_id"],
        next_attempt_at="2026-03-09 08:00:00",
        last_error="quiet window",
    )

    assert deferred["status"] == "pending"
    assert deferred["next_attempt_at"] == "2026-03-09 08:00:00"
    assert deferred["last_error"] == "quiet window"
    assert deferred["claimed_by"] == ""
    assert deferred["dead_lettered"] is False


@pytest.mark.asyncio
async def test_task_store_claims_higher_priority_task_first(tmp_path: Path) -> None:
    """Claiming should prefer higher-priority pending tasks."""
    store = TaskStore(tmp_path / "tasks.db")
    low = await store.create_task("low", source="spawn_task", priority=10)
    high = await store.create_task("high", source="spawn_task", priority=200)

    claimed = await store.claim_next_task(source="spawn_task", worker_id="worker-a")

    assert claimed is not None
    assert claimed["task_id"] == high["task_id"]
    still_pending = await store.get_task(low["task_id"])
    assert still_pending is not None
    assert still_pending["status"] == "pending"


@pytest.mark.asyncio
async def test_task_store_claims_highest_priority_task_across_sources(tmp_path: Path) -> None:
    """A multi-source claim should honor global priority instead of fixed source order."""
    store = TaskStore(tmp_path / "tasks.db")
    heartbeat = await store.create_task(
        "heartbeat",
        source="heartbeat_checklist",
        priority=50,
    )
    cron = await store.create_task(
        "cron",
        source="cron_job",
        priority=100,
    )
    spawn = await store.create_task(
        "spawn",
        source="spawn_task",
        priority=200,
    )

    claimed = await store.claim_next_task(
        sources=["spawn_task", "heartbeat_checklist", "cron_job"],
        worker_id="worker-a",
        max_running_tasks=2,
    )

    assert claimed is not None
    assert claimed["task_id"] == spawn["task_id"]
    assert claimed["source"] == "spawn_task"
    cron_pending = await store.get_task(cron["task_id"])
    heartbeat_pending = await store.get_task(heartbeat["task_id"])
    assert cron_pending is not None
    assert cron_pending["status"] == "pending"
    assert heartbeat_pending is not None
    assert heartbeat_pending["status"] == "pending"


@pytest.mark.asyncio
async def test_task_store_breaks_equal_priority_ties_by_source_saturation(
    tmp_path: Path,
) -> None:
    """Equal-priority multi-source claims should prefer the less-saturated source."""
    store = TaskStore(tmp_path / "tasks.db")
    running_spawn = await store.create_task(
        "running-spawn",
        source="spawn_task",
        priority=100,
    )
    await store.claim_next_task(
        source="spawn_task",
        worker_id="worker-running",
        max_running_tasks=3,
    )
    pending_spawn = await store.create_task(
        "pending-spawn",
        source="spawn_task",
        priority=100,
    )
    pending_heartbeat = await store.create_task(
        "pending-heartbeat",
        source="heartbeat_checklist",
        priority=100,
    )

    claimed = await store.claim_next_task(
        sources=["spawn_task", "heartbeat_checklist"],
        worker_id="worker-a",
        max_running_tasks=3,
    )

    assert running_spawn["task_id"] != pending_spawn["task_id"]
    assert claimed is not None
    assert claimed["task_id"] == pending_heartbeat["task_id"]
    assert claimed["source"] == "heartbeat_checklist"
    remaining_spawn = await store.get_task(pending_spawn["task_id"])
    assert remaining_spawn is not None
    assert remaining_spawn["status"] == "pending"


@pytest.mark.asyncio
async def test_task_store_starvation_protection_prefers_oldest_ready_task(tmp_path: Path) -> None:
    """An old ready task should bypass newer higher-priority work once starved."""
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    old_low = await store.create_task("old-low", source="spawn_task", priority=10)
    high = await store.create_task("new-high", source="spawn_task", priority=500)

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE tasks
        SET next_attempt_at = '2000-01-01 00:00:00'
        WHERE task_id = ?
        """,
        (old_low["task_id"],),
    )
    conn.commit()
    conn.close()

    metrics = await store.get_queue_metrics(
        source="spawn_task",
        starvation_threshold_seconds=60,
    )
    assert metrics["starved_ready_tasks"] == 1

    claimed = await store.claim_next_task(
        source="spawn_task",
        worker_id="worker-a",
        starvation_threshold_seconds=60,
    )

    assert claimed is not None
    assert claimed["task_id"] == old_low["task_id"]
    remaining = await store.get_task(high["task_id"])
    assert remaining is not None
    assert remaining["status"] == "pending"


@pytest.mark.asyncio
async def test_task_store_claim_respects_global_running_limit(tmp_path: Path) -> None:
    """Claiming should stop once the source-wide running cap is reached."""
    store = TaskStore(tmp_path / "tasks.db")
    first = await store.create_task("first", source="spawn_task", priority=200)
    second = await store.create_task("second", source="spawn_task", priority=100)

    claimed_first = await store.claim_next_task(
        source="spawn_task",
        worker_id="worker-a",
        max_running_tasks=1,
    )
    blocked = await store.claim_next_task(
        source="spawn_task",
        worker_id="worker-b",
        max_running_tasks=1,
    )

    assert claimed_first is not None
    assert claimed_first["task_id"] == first["task_id"]
    assert blocked is None

    await store.transition_task(first["task_id"], "succeeded")
    claimed_second = await store.claim_next_task(
        source="spawn_task",
        worker_id="worker-b",
        max_running_tasks=1,
    )

    assert claimed_second is not None
    assert claimed_second["task_id"] == second["task_id"]


@pytest.mark.asyncio
async def test_task_store_skips_pending_retry_until_due(tmp_path: Path) -> None:
    """Claiming should ignore pending tasks whose retry time has not arrived yet."""
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    delayed = await store.create_task(
        "retry later",
        source="spawn_task",
        retry_backoff_seconds=60,
    )

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE tasks
        SET next_attempt_at = '2999-01-01 00:00:00'
        WHERE task_id = ?
        """,
        (delayed["task_id"],),
    )
    conn.commit()
    conn.close()

    claimed = await store.claim_next_task(source="spawn_task", worker_id="worker-a")
    assert claimed is None


@pytest.mark.asyncio
async def test_task_store_skips_rate_limited_candidate_and_claims_next_available(
    tmp_path: Path,
) -> None:
    """Claiming should postpone a rate-limited task and continue to the next candidate."""
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    first = await store.create_task(
        "bucket-first",
        source="spawn_task",
        priority=200,
        rate_limit_key="search",
        rate_limit_window_seconds=60,
        rate_limit_max_claims=1,
    )
    second = await store.create_task(
        "bucket-second",
        source="spawn_task",
        priority=150,
        rate_limit_key="search",
        rate_limit_window_seconds=60,
        rate_limit_max_claims=1,
    )
    other = await store.create_task("other-task", source="spawn_task", priority=100)

    claimed_first = await store.claim_next_task(source="spawn_task", worker_id="worker-a")
    claimed_second = await store.claim_next_task(source="spawn_task", worker_id="worker-b")

    assert claimed_first is not None
    assert claimed_first["task_id"] == first["task_id"]
    assert claimed_first["last_claimed_at"] is not None
    assert claimed_second is not None
    assert claimed_second["task_id"] == other["task_id"]

    blocked = await store.get_task(second["task_id"])
    assert blocked is not None
    assert blocked["status"] == "pending"
    assert blocked["next_attempt_at"] is not None

    metrics = await store.get_queue_metrics(source="spawn_task")
    assert metrics["rate_limited_backlog"] == 1


@pytest.mark.asyncio
async def test_task_store_fail_task_attempt_requeues_before_retry_budget_exhausts(
    tmp_path: Path,
) -> None:
    """A failed attempt should return to pending while retry budget remains."""
    store = TaskStore(tmp_path / "tasks.db")
    queued = await store.create_task(
        "retry me",
        source="spawn_task",
        max_attempts=3,
        retry_backoff_seconds=15,
    )
    await store.claim_next_task(source="spawn_task", worker_id="worker-a")

    updated = await store.fail_task_attempt(queued["task_id"], last_error="temporary failure")

    assert updated["status"] == "pending"
    assert updated["attempt_count"] == 1
    assert updated["last_error"] == "temporary failure"
    assert updated["next_attempt_at"] is not None

    metrics = await store.get_queue_metrics(source="spawn_task")
    assert metrics["ready_backlog"] == 0
    assert metrics["retry_backlog"] == 1
    assert metrics["next_retry_in_seconds"] >= 0


@pytest.mark.asyncio
async def test_task_store_fail_task_attempt_marks_terminal_failure_when_out_of_retries(
    tmp_path: Path,
) -> None:
    """A failed attempt should become terminal once retry budget is exhausted."""
    store = TaskStore(tmp_path / "tasks.db")
    queued = await store.create_task(
        "fail me",
        source="spawn_task",
        max_attempts=1,
    )
    await store.claim_next_task(source="spawn_task", worker_id="worker-a")

    updated = await store.fail_task_attempt(queued["task_id"], last_error="permanent failure")

    assert updated["status"] == "failed"
    assert updated["last_error"] == "permanent failure"
    assert updated["finished_at"] is not None
    assert updated["dead_lettered"] is True
    assert updated["dead_letter_reason"] == "permanent failure"

    metrics = await store.get_queue_metrics(source="spawn_task")
    assert metrics["dead_letter_tasks"] == 1


@pytest.mark.asyncio
async def test_task_store_requeue_resets_dead_letter_state(tmp_path: Path) -> None:
    """Requeue should reset a dead-lettered task back to clean pending state."""
    store = TaskStore(tmp_path / "tasks.db")
    queued = await store.create_task(
        "requeue me",
        source="spawn_task",
        max_attempts=1,
    )
    await store.claim_next_task(source="spawn_task", worker_id="worker-a")
    await store.fail_task_attempt(queued["task_id"], last_error="fatal failure")

    requeued = await store.requeue_task(queued["task_id"])

    assert requeued["status"] == "pending"
    assert requeued["attempt_count"] == 0
    assert requeued["dead_lettered"] is False
    assert requeued["dead_letter_reason"] == ""
    assert requeued["last_error"] == ""


@pytest.mark.asyncio
async def test_task_store_heartbeat_and_recover_orphaned_tasks(tmp_path: Path) -> None:
    """Running tasks should refresh heartbeats and recover when the lease is stale."""
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    queued = await store.create_task("spawn task", source="spawn_task")
    claimed = await store.claim_next_task(source="spawn_task", worker_id="worker-a")

    assert claimed is not None
    heartbeat = await store.heartbeat_task(queued["task_id"], worker_id="worker-a")
    assert heartbeat is not None
    assert heartbeat["claimed_by"] == "worker-a"
    assert heartbeat["last_heartbeat_at"] is not None

    conn = sqlite3.connect(db_path)
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

    recovered = await store.recover_orphaned_tasks(
        lease_timeout_seconds=30,
        source="spawn_task",
    )
    assert [item["task_id"] for item in recovered] == [queued["task_id"]]
    assert recovered[0]["recovered_from_worker"] == "worker-a"
    assert recovered[0]["stale_age_seconds"] > 0

    refreshed = await store.get_task(queued["task_id"])
    assert refreshed is not None
    assert refreshed["status"] == "pending"
    assert refreshed["claimed_by"] == ""
    assert refreshed["last_heartbeat_at"] is None
    assert refreshed["attempt_count"] == 1
    assert refreshed["next_attempt_at"] is not None


@pytest.mark.asyncio
async def test_task_store_queue_metrics_include_stale_running_tasks(tmp_path: Path) -> None:
    """Queue metrics should expose stale running counts and oldest stale age."""
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    created = await store.create_task("stale runner", source="spawn_task")
    await store.claim_next_task(source="spawn_task", worker_id="worker-a")

    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        UPDATE tasks
        SET last_heartbeat_at = '2000-01-01 00:00:00'
        WHERE task_id = ?
        """,
        (created["task_id"],),
    )
    conn.commit()
    conn.close()

    metrics = await store.get_queue_metrics(
        source="spawn_task",
        lease_timeout_seconds=30,
        stall_threshold_seconds=120,
    )

    assert metrics["running_tasks"] == 1
    assert metrics["stale_running_tasks"] == 1
    assert metrics["oldest_stale_running_age_seconds"] > 0
    assert metrics["lease_timeout_seconds"] == 30
    assert metrics["stall_threshold_seconds"] == 120


@pytest.mark.asyncio
async def test_task_store_cancel_pending_and_running_tasks(tmp_path: Path) -> None:
    """Cancellation should cancel pending tasks immediately and mark running tasks."""
    store = TaskStore(tmp_path / "tasks.db")
    pending = await store.create_task("pending task", source="spawn_task", priority=10)
    running = await store.create_task("running task", source="spawn_task", priority=100)
    await store.claim_next_task(source="spawn_task", worker_id="worker-a")

    cancelled_pending = await store.request_cancel(pending["task_id"])
    assert cancelled_pending["status"] == "cancelled"
    assert cancelled_pending["cancel_requested"] is True
    assert cancelled_pending["finished_at"] is not None

    cancelled_running = await store.request_cancel(running["task_id"])
    assert cancelled_running["status"] == "running"
    assert cancelled_running["cancel_requested"] is True
