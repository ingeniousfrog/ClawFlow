"""Persistent cron scheduler with SQLite storage."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from nanoclaw.core.logger import get_logger

if TYPE_CHECKING:
    from nanoclaw.channels.gateway import Gateway
    from nanoclaw.core.config import Config

logger = get_logger(__name__)

CRON_TASK_SOURCE = "cron_job"
CRON_TASK_PRIORITY = 700
CRON_TASK_TIMEOUT_SECONDS = 900
CRON_TASK_MAX_ATTEMPTS = 2


class Scheduler:
    """Persistent cron jobs. Stored in SQLite, survives restart."""

    def __init__(self, config: "Config", gateway: "Gateway"):
        """
        Initialize Scheduler.

        Args:
            config: Application configuration
            gateway: Gateway for message routing
        """
        self.config = config
        self.gateway = gateway
        self.running = False
        self._task: Optional[asyncio.Task] = None
        self._db_path = self._get_db_path()

    def _get_db_path(self) -> Path:
        """Get database path."""
        from nanoclaw.core.config import get_data_path

        return get_data_path() / "nanoclaw.db"

    async def start(self) -> None:
        """Start checking jobs every 60 seconds."""
        self.running = True
        self._task = asyncio.create_task(self._loop())
        logger.debug("Scheduler started")

    async def _loop(self) -> None:
        """Main scheduler loop."""
        while self.running:
            try:
                await self._check_and_run()
            except Exception as e:
                logger.error(f"Scheduler error: {e}")
            await asyncio.sleep(60)

    async def _check_and_run(self) -> None:
        """Check all jobs, run those that are due."""
        jobs = await self._get_enabled_jobs()
        now = datetime.now(UTC).replace(tzinfo=None)

        for job in jobs:
            should_run = False

            if job["cron_expr"]:
                try:
                    from croniter import croniter  # type: ignore[import-untyped]

                    last_run = (
                        datetime.fromisoformat(job["last_run"])
                        if job["last_run"]
                        else now - timedelta(days=1)
                    )
                    cron = croniter(job["cron_expr"], last_run)
                    next_run = cron.get_next(datetime)
                    should_run = next_run <= now
                except ImportError:
                    logger.warning("croniter not installed, skipping cron expression jobs")
                except Exception as e:
                    logger.error(f"Cron parse error for job {job['id']}: {e}")

            elif job["interval_seconds"]:
                if job["last_run"]:
                    last = datetime.fromisoformat(job["last_run"])
                    should_run = (now - last).total_seconds() >= job["interval_seconds"]
                else:
                    should_run = True

            if should_run:
                await self._enqueue_job(job)
                await self._update_last_run(job["id"])

    async def _enqueue_job(self, job: dict) -> None:
        """Persist one due cron job into the shared runtime."""
        try:
            from nanoclaw.runtime.tasks import get_task_store
            from nanoclaw.tools.spawn import wake_background_runtime

            task = await get_task_store().create_task(
                f"Cron job: {job['name']}",
                task_type="cron",
                payload={
                    "job_id": int(job["id"]),
                    "job_name": str(job["name"]),
                    "message": str(job["message"]),
                    "channel": str(job.get("channel") or "telegram"),
                    "target_id": str(job.get("target_id") or ""),
                    "quiet_start": str(job.get("quiet_start") or ""),
                    "quiet_end": str(job.get("quiet_end") or ""),
                },
                source=CRON_TASK_SOURCE,
                session_id="cron:system",
                priority=CRON_TASK_PRIORITY,
                timeout_seconds=CRON_TASK_TIMEOUT_SECONDS,
                max_attempts=CRON_TASK_MAX_ATTEMPTS,
            )
            logger.info("Queued cron job %s as runtime task %s", job["id"], task["task_id"])
            wake_background_runtime()
        except Exception as e:
            logger.error(f"Cron job '{job['name']}' failed to queue: {e}")

    async def _get_enabled_jobs(self) -> list[dict]:
        """Get all enabled jobs."""

        def _query() -> list[dict]:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute(
                "SELECT * FROM cron_jobs WHERE enabled = 1"
            )
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]

        return await asyncio.to_thread(_query)

    async def _update_last_run(self, job_id: int) -> None:
        """Update job's last_run timestamp."""

        def _update() -> None:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "UPDATE cron_jobs SET last_run = datetime('now') WHERE id = ?",
                (job_id,),
            )
            conn.commit()
            conn.close()

        await asyncio.to_thread(_update)

    async def add_job(
        self,
        name: str,
        message: str,
        cron_expr: Optional[str] = None,
        interval_seconds: Optional[int] = None,
        channel: str = "telegram",
        target_id: str = "",
        quiet_start: str = "",
        quiet_end: str = "",
    ) -> int:
        """
        Add a new cron job.

        Args:
            name: Job name
            message: Message to send to agent
            cron_expr: Cron expression (e.g., '0 9 * * *')
            interval_seconds: Interval in seconds (alternative to cron)
            channel: Target channel
            target_id: Optional channel-specific target identifier
            quiet_start: Optional local quiet-window start time (`HH:MM`)
            quiet_end: Optional local quiet-window end time (`HH:MM`)

        Returns:
            Job ID
        """

        def _insert() -> int:
            conn = sqlite3.connect(self._db_path)
            cursor = conn.execute(
                """
                INSERT INTO cron_jobs (
                    name,
                    message,
                    cron_expr,
                    interval_seconds,
                    channel,
                    target_id,
                    quiet_start,
                    quiet_end
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    name,
                    message,
                    cron_expr,
                    interval_seconds,
                    channel,
                    target_id,
                    quiet_start,
                    quiet_end,
                ),
            )
            conn.commit()
            job_id = cursor.lastrowid
            conn.close()
            return job_id or 0

        return await asyncio.to_thread(_insert)

    async def remove_job(self, job_id: int) -> None:
        """Remove a cron job."""

        def _delete() -> None:
            conn = sqlite3.connect(self._db_path)
            conn.execute("DELETE FROM cron_jobs WHERE id = ?", (job_id,))
            conn.commit()
            conn.close()

        await asyncio.to_thread(_delete)

    async def update_job(
        self,
        job_id: int,
        *,
        name: str,
        message: str,
        cron_expr: Optional[str] = None,
        interval_seconds: Optional[int] = None,
        channel: str = "telegram",
        target_id: str = "",
        quiet_start: str = "",
        quiet_end: str = "",
    ) -> None:
        """Update one existing cron job in place."""

        def _update() -> None:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                """
                UPDATE cron_jobs
                SET name = ?,
                    message = ?,
                    cron_expr = ?,
                    interval_seconds = ?,
                    channel = ?,
                    target_id = ?,
                    quiet_start = ?,
                    quiet_end = ?
                WHERE id = ?
                """,
                (
                    name,
                    message,
                    cron_expr,
                    interval_seconds,
                    channel,
                    target_id,
                    quiet_start,
                    quiet_end,
                    job_id,
                ),
            )
            conn.commit()
            conn.close()

        await asyncio.to_thread(_update)

    async def list_jobs(self) -> list[dict]:
        """List all cron jobs."""

        def _query() -> list[dict]:
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM cron_jobs ORDER BY id")
            rows = cursor.fetchall()
            conn.close()
            return [dict(row) for row in rows]

        return await asyncio.to_thread(_query)

    @staticmethod
    def _compact_runtime_task(task: dict) -> dict:
        """Return a small runtime summary for one persisted task row."""
        return {
            "task_id": str(task.get("task_id") or ""),
            "source": str(task.get("source") or ""),
            "status": str(task.get("status") or ""),
            "attempt_count": int(task.get("attempt_count") or 0),
            "updated_at": task.get("updated_at"),
            "last_error": str(task.get("last_error") or ""),
        }

    @staticmethod
    def _derive_runtime_health(runtime: dict[str, Any]) -> tuple[str, str]:
        """Summarize one schedule's recent execution health."""
        notify_kind = str(runtime.get("notify_kind") or "")
        last_execution = dict(runtime.get("last_execution") or {})
        last_delivery_retry = dict(runtime.get("last_delivery_retry") or {})
        delivery_status = str(last_delivery_retry.get("status") or "")
        execution_status = str(last_execution.get("status") or "")

        if delivery_status in {"failed", "cancelled"}:
            return "attention", f"delivery retry {delivery_status}"
        if execution_status in {"failed", "cancelled"}:
            return "attention", f"latest execution {execution_status}"
        if delivery_status in {"pending", "running"}:
            return "retrying", f"delivery retry {delivery_status}"
        if notify_kind == "cron_delivery_retry_scheduled":
            return "retrying", "delivery retry scheduled"
        if notify_kind == "cron_suppressed":
            return "muted", "suppressed by quiet window"
        if execution_status == "succeeded":
            return "healthy", "latest execution succeeded"
        return "idle", "no recent runtime state"

    @staticmethod
    def _extract_summary_field(summary: str, key: str) -> str:
        """Extract one `key=value` token from a stored audit summary."""
        prefix = f"{key}="
        for token in str(summary or "").split():
            if token.startswith(prefix):
                return token[len(prefix):]
        return ""

    @classmethod
    def _compact_schedule_signal(cls, entry: dict[str, Any]) -> dict[str, Any]:
        """Return a small operator-facing schedule signal entry."""
        action_type = str(entry.get("action_type") or "")
        input_summary = str(entry.get("input_summary") or "")
        repeat_count = cls._extract_summary_field(input_summary, "repeat_count")
        if action_type == "schedule_recovery":
            previous_stage = cls._extract_summary_field(input_summary, "previous_stage")
            detail = f"healthy after {previous_stage or 'previous issue'}"
            label = "recovery"
        else:
            stage = cls._extract_summary_field(input_summary, "stage")
            detail = stage or action_type
            if repeat_count:
                detail = f"{detail} x{repeat_count}"
            label = "escalation" if action_type == "schedule_alert_escalation" else "alert"
        return {
            "timestamp": entry.get("timestamp"),
            "action_type": action_type,
            "status": str(entry.get("status") or ""),
            "label": label,
            "detail": detail,
        }

    async def list_jobs_with_runtime_state(
        self,
        *,
        task_limit: int = 300,
        audit_limit: int = 200,
    ) -> list[dict]:
        """List cron jobs with the latest runtime execution and delivery state."""
        jobs = await self.list_jobs()
        try:
            from nanoclaw.runtime.tasks import get_task_store

            store = get_task_store()
            tasks = await store.list_tasks(limit=task_limit)
        except Exception:
            return jobs
        try:
            from nanoclaw.security.audit import get_audit_log

            audit_entries = await get_audit_log().get_recent(limit=audit_limit)
        except Exception:
            audit_entries = []

        latest_execution_by_job: dict[int, dict] = {}
        latest_delivery_by_task: dict[str, dict] = {}
        schedule_signals_by_job: dict[int, list[dict[str, Any]]] = {}
        for task in tasks:
            source = str(task.get("source") or "")
            payload = dict(task.get("payload") or {})
            if source == CRON_TASK_SOURCE:
                try:
                    job_id = int(payload.get("job_id") or 0)
                except (TypeError, ValueError):
                    continue
                if job_id > 0 and job_id not in latest_execution_by_job:
                    latest_execution_by_job[job_id] = task
            elif source == "cron_delivery_retry":
                original_task_id = str(payload.get("original_task_id") or "")
                if original_task_id and original_task_id not in latest_delivery_by_task:
                    latest_delivery_by_task[original_task_id] = task
        for entry in audit_entries:
            action_type = str(entry.get("action_type") or "")
            if action_type not in {
                "schedule_alert",
                "schedule_alert_escalation",
                "schedule_recovery",
            }:
                continue
            session_id = str(entry.get("session_id") or "")
            if not session_id.startswith("schedule:"):
                continue
            try:
                job_id = int(session_id.split(":", 1)[1])
            except (IndexError, TypeError, ValueError):
                continue
            signal_timeline = schedule_signals_by_job.setdefault(job_id, [])
            if len(signal_timeline) >= 3:
                continue
            signal_timeline.append(self._compact_schedule_signal(entry))

        result: list[dict] = []
        for job in jobs:
            item = dict(job)
            runtime = {
                "notify_kind": "",
                "last_execution": None,
                "last_delivery_retry": None,
                "signal_timeline": [],
            }
            try:
                job_id = int(item.get("id") or 0)
            except (TypeError, ValueError):
                job_id = 0
            last_execution = latest_execution_by_job.get(job_id)
            if last_execution is not None:
                runtime["last_execution"] = self._compact_runtime_task(last_execution)
                task_id = str(last_execution.get("task_id") or "")
                delivery_retry = latest_delivery_by_task.get(task_id)
                if delivery_retry is not None:
                    runtime["last_delivery_retry"] = self._compact_runtime_task(delivery_retry)
                try:
                    steps = await store.list_task_steps(task_id)
                except Exception:
                    steps = []
                for step in steps:
                    if str(step.get("step_id") or "") != "cron_notify":
                        continue
                    runtime["notify_kind"] = str((step.get("output") or {}).get("kind") or "")
                    break
            health, health_reason = self._derive_runtime_health(runtime)
            runtime["health"] = health
            runtime["health_reason"] = health_reason
            runtime["signal_timeline"] = schedule_signals_by_job.get(job_id, [])
            item["runtime"] = runtime
            result.append(item)
        return result

    async def toggle_job(self, job_id: int, enabled: bool) -> None:
        """Enable or disable a job."""

        def _update() -> None:
            conn = sqlite3.connect(self._db_path)
            conn.execute(
                "UPDATE cron_jobs SET enabled = ? WHERE id = ?",
                (1 if enabled else 0, job_id),
            )
            conn.commit()
            conn.close()

        await asyncio.to_thread(_update)

    async def stop(self) -> None:
        """Stop the scheduler."""
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass


# Global scheduler instance
_scheduler: Optional[Scheduler] = None


def get_scheduler() -> Scheduler:
    """Get the global Scheduler instance."""
    global _scheduler
    if _scheduler is None:
        from nanoclaw.channels.gateway import get_gateway
        from nanoclaw.core.config import get_config

        config = get_config()
        gateway = get_gateway()
        # Create a minimal scheduler without gateway for CLI use
        _scheduler = Scheduler(config, gateway)  # type: ignore
    return _scheduler


def set_scheduler(scheduler: Scheduler) -> None:
    """Set the global Scheduler instance."""
    global _scheduler
    _scheduler = scheduler
