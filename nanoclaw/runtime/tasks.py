"""Persistent task table and minimal task state machine."""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

TASK_STATUSES = {"pending", "running", "succeeded", "failed", "cancelled"}
TASK_STEP_STATUSES = {"pending", "running", "succeeded", "failed", "cancelled"}
TASK_TRANSITIONS = {
    "pending": {"running", "failed", "cancelled"},
    "running": {"pending", "succeeded", "failed", "cancelled"},
    "failed": {"pending"},
    "cancelled": {"pending"},
    "succeeded": set(),
}


class TaskStore:
    """SQLite-backed task persistence with guarded status transitions."""

    def __init__(self, db_path: str | Path):
        """Initialize the store and create tables if needed."""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Create the tasks table and supporting indexes."""
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL DEFAULT 'background',
                status TEXT NOT NULL DEFAULT 'pending',
                description TEXT NOT NULL,
                payload TEXT NOT NULL DEFAULT '{}',
                source TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                priority INTEGER NOT NULL DEFAULT 100,
                timeout_seconds INTEGER NOT NULL DEFAULT 1800,
                max_attempts INTEGER NOT NULL DEFAULT 2,
                retry_backoff_seconds INTEGER NOT NULL DEFAULT 30,
                rate_limit_key TEXT NOT NULL DEFAULT '',
                rate_limit_window_seconds INTEGER NOT NULL DEFAULT 0,
                rate_limit_max_claims INTEGER NOT NULL DEFAULT 0,
                idempotency_key TEXT NOT NULL DEFAULT '',
                next_attempt_at TEXT NOT NULL DEFAULT (datetime('now')),
                cancel_requested INTEGER NOT NULL DEFAULT 0,
                dead_lettered INTEGER NOT NULL DEFAULT 0,
                dead_letter_reason TEXT NOT NULL DEFAULT '',
                dead_lettered_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                started_at TEXT,
                finished_at TEXT,
                last_claimed_at TEXT,
                claimed_by TEXT NOT NULL DEFAULT '',
                last_heartbeat_at TEXT
            );
            CREATE TABLE IF NOT EXISTS task_steps (
                task_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                step_name TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                input_json TEXT NOT NULL DEFAULT '{}',
                input_hash TEXT NOT NULL DEFAULT '',
                output_json TEXT NOT NULL DEFAULT '{}',
                is_checkpoint INTEGER NOT NULL DEFAULT 0,
                idempotent INTEGER NOT NULL DEFAULT 0,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now')),
                started_at TEXT,
                finished_at TEXT,
                PRIMARY KEY (task_id, step_id)
            );
            """
        )
        self._migrate_schema(conn)
        self._ensure_indexes(conn)
        conn.commit()
        conn.close()

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        """Backfill newer task columns for existing databases."""
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "claimed_by" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN claimed_by TEXT NOT NULL DEFAULT ''"
            )
        if "last_heartbeat_at" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN last_heartbeat_at TEXT"
            )
        if "priority" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN priority INTEGER NOT NULL DEFAULT 100"
            )
        if "timeout_seconds" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN timeout_seconds INTEGER NOT NULL DEFAULT 1800"
            )
        if "max_attempts" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 2"
            )
        if "retry_backoff_seconds" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN retry_backoff_seconds INTEGER NOT NULL DEFAULT 30"
            )
        if "rate_limit_key" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN rate_limit_key TEXT NOT NULL DEFAULT ''"
            )
        if "rate_limit_window_seconds" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN rate_limit_window_seconds INTEGER NOT NULL DEFAULT 0"
            )
        if "rate_limit_max_claims" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN rate_limit_max_claims INTEGER NOT NULL DEFAULT 0"
            )
        if "idempotency_key" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN idempotency_key TEXT NOT NULL DEFAULT ''"
            )
        if "next_attempt_at" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN next_attempt_at TEXT NOT NULL DEFAULT (datetime('now'))"
            )
        if "cancel_requested" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN cancel_requested INTEGER NOT NULL DEFAULT 0"
            )
        if "dead_lettered" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN dead_lettered INTEGER NOT NULL DEFAULT 0"
            )
        if "dead_letter_reason" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN dead_letter_reason TEXT NOT NULL DEFAULT ''"
            )
        if "dead_lettered_at" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN dead_lettered_at TEXT"
            )
        if "last_claimed_at" not in columns:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN last_claimed_at TEXT"
            )

    @staticmethod
    def _ensure_indexes(conn: sqlite3.Connection) -> None:
        """Create indexes after schema migration has added newer columns."""
        conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_tasks_source_status ON tasks(source, status);
            CREATE INDEX IF NOT EXISTS idx_tasks_queue
                ON tasks(status, cancel_requested, priority DESC, created_at ASC);
            CREATE INDEX IF NOT EXISTS idx_tasks_schedule
                ON tasks(status, cancel_requested, next_attempt_at, priority DESC, created_at ASC);
            CREATE INDEX IF NOT EXISTS idx_tasks_idempotency
                ON tasks(source, idempotency_key, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_task_steps_task
                ON task_steps(task_id, status, updated_at DESC);
            """
        )

    @staticmethod
    def _utc_now() -> str:
        """Return the current UTC timestamp in SQLite-compatible format."""
        return datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def _parse_timestamp(value: Any) -> Optional[datetime]:
        """Parse one SQLite timestamp into an aware UTC datetime."""
        if not value:
            return None
        try:
            return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        except ValueError:
            return None

    @classmethod
    def _seconds_since_timestamp(cls, value: Any) -> Optional[int]:
        """Return elapsed whole seconds since one stored UTC timestamp."""
        parsed = cls._parse_timestamp(value)
        if parsed is None:
            return None
        return max(0, int((datetime.now(UTC) - parsed).total_seconds()))

    @classmethod
    def _seconds_until_timestamp(cls, value: Any) -> Optional[int]:
        """Return remaining whole seconds until one stored UTC timestamp."""
        parsed = cls._parse_timestamp(value)
        if parsed is None:
            return None
        return max(0, int((parsed - datetime.now(UTC)).total_seconds()))

    @staticmethod
    def _validate_status(status: str) -> str:
        """Validate and normalize a task status."""
        normalized = status.strip().lower()
        if normalized not in TASK_STATUSES:
            allowed = ", ".join(sorted(TASK_STATUSES))
            raise ValueError(f"Invalid task status `{status}`. Use {allowed}.")
        return normalized

    @staticmethod
    def can_transition(current: str, target: str) -> bool:
        """Return True when one status transition is allowed."""
        current_state = TaskStore._validate_status(current)
        target_state = TaskStore._validate_status(target)
        if current_state == target_state:
            return True
        return target_state in TASK_TRANSITIONS[current_state]

    @staticmethod
    def _loads_payload(raw: str) -> dict[str, Any]:
        """Parse stored JSON payload and fall back to an empty object."""
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _dumps_payload(payload: Optional[dict[str, Any]]) -> str:
        """Serialize one JSON payload in a stable form for storage and hashing."""
        return json.dumps(payload or {}, ensure_ascii=True, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _hash_payload(cls, payload: Optional[dict[str, Any]]) -> str:
        """Hash one normalized JSON payload for checkpoint reuse checks."""
        return hashlib.sha256(cls._dumps_payload(payload).encode("utf-8")).hexdigest()

    def _normalize_row(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a SQLite row into a task dictionary."""
        item = dict(row)
        item["payload"] = self._loads_payload(str(item.get("payload", "{}")))
        item["cancel_requested"] = bool(item.get("cancel_requested", 0))
        item["dead_lettered"] = bool(item.get("dead_lettered", 0))
        return item

    def _normalize_step_row(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a SQLite row into a task-step dictionary."""
        item = dict(row)
        item["input"] = self._loads_payload(str(item.pop("input_json", "{}")))
        item["output"] = self._loads_payload(str(item.pop("output_json", "{}")))
        item["is_checkpoint"] = bool(item.get("is_checkpoint", 0))
        item["idempotent"] = bool(item.get("idempotent", 0))
        return item

    @staticmethod
    def _normalize_priority(priority: int) -> int:
        """Clamp priority to a small stable range."""
        return max(0, min(int(priority), 1000))

    @staticmethod
    def _normalize_timeout_seconds(timeout_seconds: int) -> int:
        """Normalize timeout seconds; zero disables timeout checks."""
        return max(0, int(timeout_seconds))

    @staticmethod
    def _normalize_max_attempts(max_attempts: int) -> int:
        """Clamp max attempts to a compact retry range."""
        return max(1, min(int(max_attempts), 10))

    @staticmethod
    def _normalize_retry_backoff_seconds(retry_backoff_seconds: int) -> int:
        """Clamp retry backoff seconds to a safe scheduler range."""
        return max(0, min(int(retry_backoff_seconds), 86400))

    @staticmethod
    def _normalize_starvation_threshold_seconds(starvation_threshold_seconds: int) -> int:
        """Clamp starvation threshold to a safe scheduling range."""
        return max(0, min(int(starvation_threshold_seconds), 86400))

    @staticmethod
    def _normalize_max_running_tasks(max_running_tasks: int) -> int:
        """Clamp a running-task cap; zero disables the global limit."""
        return max(0, min(int(max_running_tasks), 32))

    @staticmethod
    def _normalize_rate_limit_window_seconds(rate_limit_window_seconds: int) -> int:
        """Clamp a rate-limit window to a safe range."""
        return max(0, min(int(rate_limit_window_seconds), 86400))

    @staticmethod
    def _normalize_rate_limit_max_claims(rate_limit_max_claims: int) -> int:
        """Clamp rate-limited claims per window; zero disables the bucket."""
        return max(0, min(int(rate_limit_max_claims), 32))

    @staticmethod
    def _normalize_step_id(step_id: str) -> str:
        """Validate one stable step identifier."""
        normalized = step_id.strip().lower()
        if not normalized:
            raise ValueError("step_id is required.")
        return normalized

    @staticmethod
    def _normalize_idempotency_key(idempotency_key: str) -> str:
        """Normalize one optional idempotency key."""
        return idempotency_key.strip()[:120]

    async def create_task(
        self,
        description: str,
        *,
        task_type: str = "background",
        payload: Optional[dict[str, Any]] = None,
        source: str = "",
        session_id: str = "",
        priority: int = 100,
        timeout_seconds: int = 1800,
        max_attempts: int = 2,
        retry_backoff_seconds: int = 30,
        rate_limit_key: str = "",
        rate_limit_window_seconds: int = 0,
        rate_limit_max_claims: int = 0,
        idempotency_key: str = "",
    ) -> dict[str, Any]:
        """Create one pending task and return the persisted row."""
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        task_type = task_type.strip() or "background"
        payload_json = json.dumps(payload or {}, ensure_ascii=True)
        normalized_priority = self._normalize_priority(priority)
        normalized_timeout = self._normalize_timeout_seconds(timeout_seconds)
        normalized_max_attempts = self._normalize_max_attempts(max_attempts)
        normalized_backoff = self._normalize_retry_backoff_seconds(retry_backoff_seconds)
        normalized_rate_limit_key = rate_limit_key.strip()
        normalized_rate_limit_window = self._normalize_rate_limit_window_seconds(
            rate_limit_window_seconds
        )
        normalized_rate_limit_max_claims = self._normalize_rate_limit_max_claims(
            rate_limit_max_claims
        )
        normalized_idempotency_key = self._normalize_idempotency_key(idempotency_key)
        if not normalized_rate_limit_key:
            normalized_rate_limit_window = 0
            normalized_rate_limit_max_claims = 0

        def _insert() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            if normalized_idempotency_key:
                existing = conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE source = ?
                      AND idempotency_key = ?
                      AND status IN ('pending', 'running', 'succeeded')
                    ORDER BY created_at DESC, task_id DESC
                    LIMIT 1
                    """,
                    (source, normalized_idempotency_key),
                ).fetchone()
                if existing is not None:
                    conn.commit()
                    conn.close()
                    reused = self._normalize_row(existing)
                    reused["reused_existing"] = True
                    return reused
            conn.execute(
                """
                INSERT INTO tasks (
                    task_id, task_type, status, description, payload,
                    source, session_id, priority, timeout_seconds,
                    max_attempts, retry_backoff_seconds,
                    rate_limit_key, rate_limit_window_seconds, rate_limit_max_claims,
                    idempotency_key,
                    next_attempt_at
                )
                VALUES (?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
                """,
                (
                    task_id,
                    task_type,
                    description,
                    payload_json,
                    source,
                    session_id,
                    normalized_priority,
                    normalized_timeout,
                    normalized_max_attempts,
                    normalized_backoff,
                    normalized_rate_limit_key,
                    normalized_rate_limit_window,
                    normalized_rate_limit_max_claims,
                    normalized_idempotency_key,
                ),
            )
            cursor = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            conn.commit()
            conn.close()
            if row is None:
                raise RuntimeError(f"Failed to create task `{task_id}`.")
            created = self._normalize_row(row)
            created["reused_existing"] = False
            return created

        return await asyncio.to_thread(_insert)

    async def get_task(self, task_id: str) -> Optional[dict[str, Any]]:
        """Return one persisted task by ID."""

        def _query() -> Optional[dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            conn.close()
            return self._normalize_row(row) if row else None

        return await asyncio.to_thread(_query)

    async def refresh_pending_task_payload(
        self,
        task_id: str,
        *,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Replace one pending workflow-role task payload without changing task identity."""
        payload_json = self._dumps_payload(payload)

        def _refresh() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                conn.close()
                raise KeyError(f"Task `{task_id}` not found.")

            status = str(row["status"] or "")
            task_type = str(row["task_type"] or "")
            if task_type != "workflow_role":
                conn.close()
                raise ValueError(
                    f"Task `{task_id}` must be `workflow_role` before payload refresh."
                )
            if status != "pending":
                conn.close()
                raise ValueError(f"Task `{task_id}` must be `pending` before payload refresh.")

            conn.execute(
                """
                UPDATE tasks
                SET payload = ?,
                    updated_at = datetime('now'),
                    cancel_requested = 0,
                    dead_lettered = 0,
                    dead_letter_reason = '',
                    dead_lettered_at = NULL,
                    last_error = ''
                WHERE task_id = ?
                """,
                (payload_json, task_id),
            )
            updated = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            conn.commit()
            conn.close()
            if updated is None:
                raise RuntimeError(f"Failed to refresh task `{task_id}` payload.")
            return self._normalize_row(updated)

        return await asyncio.to_thread(_refresh)

    async def refresh_running_task_payload(
        self,
        task_id: str,
        *,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Replace one running workflow-role task payload without disturbing its current claim."""
        payload_json = self._dumps_payload(payload)

        def _refresh() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                conn.close()
                raise KeyError(f"Task `{task_id}` not found.")

            status = str(row["status"] or "")
            task_type = str(row["task_type"] or "")
            if task_type != "workflow_role":
                conn.close()
                raise ValueError(
                    f"Task `{task_id}` must be `workflow_role` before running payload refresh."
                )
            if status != "running":
                conn.close()
                raise ValueError(f"Task `{task_id}` must be `running` before payload refresh.")

            conn.execute(
                """
                UPDATE tasks
                SET payload = ?,
                    updated_at = datetime('now'),
                    dead_lettered = 0,
                    dead_letter_reason = '',
                    dead_lettered_at = NULL
                WHERE task_id = ?
                """,
                (payload_json, task_id),
            )
            updated = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            conn.commit()
            conn.close()
            if updated is None:
                raise RuntimeError(f"Failed to refresh running task `{task_id}` payload.")
            return self._normalize_row(updated)

        return await asyncio.to_thread(_refresh)

    async def list_tasks(
        self,
        *,
        limit: int = 50,
        status: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        """List recent tasks, optionally filtered by one status."""
        limit = max(1, min(int(limit), 200))
        status_filter = self._validate_status(status) if status else None

        def _query() -> list[dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            if status_filter:
                cursor = conn.execute(
                    """
                    SELECT * FROM tasks
                    WHERE status = ?
                    ORDER BY created_at DESC, task_id DESC
                    LIMIT ?
                    """,
                    (status_filter, limit),
                )
            else:
                cursor = conn.execute(
                    """
                    SELECT * FROM tasks
                    ORDER BY created_at DESC, task_id DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            rows = cursor.fetchall()
            conn.close()
            return [self._normalize_row(row) for row in rows]

        return await asyncio.to_thread(_query)

    async def list_child_tasks(
        self,
        parent_task_id: str,
        *,
        source: str = "",
    ) -> list[dict[str, Any]]:
        """List tasks whose payload points at one parent task identifier."""
        parent_id = str(parent_task_id or "").strip()
        source_filter = source.strip()
        if not parent_id:
            return []

        def _query() -> list[dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            try:
                if source_filter:
                    rows = conn.execute(
                        """
                        SELECT * FROM tasks
                        WHERE source = ?
                          AND json_extract(payload, '$.parent_task_id') = ?
                        ORDER BY created_at ASC, task_id ASC
                        """,
                        (source_filter, parent_id),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT * FROM tasks
                        WHERE json_extract(payload, '$.parent_task_id') = ?
                        ORDER BY created_at ASC, task_id ASC
                        """,
                        (parent_id,),
                    ).fetchall()
            except sqlite3.OperationalError:
                if source_filter:
                    rows = conn.execute(
                        """
                        SELECT * FROM tasks
                        WHERE source = ?
                        ORDER BY created_at ASC, task_id ASC
                        """,
                        (source_filter,),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        """
                        SELECT * FROM tasks
                        ORDER BY created_at ASC, task_id ASC
                        """
                    ).fetchall()
            finally:
                conn.close()
            items = [self._normalize_row(row) for row in rows]
            return [
                item
                for item in items
                if str(item.get("payload", {}).get("parent_task_id") or "") == parent_id
            ]

        return await asyncio.to_thread(_query)

    async def get_task_step(self, task_id: str, step_id: str) -> Optional[dict[str, Any]]:
        """Return one persisted step for a task."""
        normalized_step_id = self._normalize_step_id(step_id)

        def _query() -> Optional[dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                """
                SELECT * FROM task_steps
                WHERE task_id = ? AND step_id = ?
                """,
                (task_id, normalized_step_id),
            ).fetchone()
            conn.close()
            return self._normalize_step_row(row) if row else None

        return await asyncio.to_thread(_query)

    async def list_task_steps(self, task_id: str) -> list[dict[str, Any]]:
        """List persisted steps for one task in stable step order."""

        def _query() -> list[dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                """
                SELECT * FROM task_steps
                WHERE task_id = ?
                ORDER BY created_at ASC, step_id ASC
                """,
                (task_id,),
            ).fetchall()
            conn.close()
            return [self._normalize_step_row(row) for row in rows]

        return await asyncio.to_thread(_query)

    async def start_task_step(
        self,
        task_id: str,
        step_id: str,
        *,
        step_name: str = "",
        input_payload: Optional[dict[str, Any]] = None,
        is_checkpoint: bool = False,
        idempotent: bool = False,
    ) -> dict[str, Any]:
        """Start one task step or reuse a completed checkpoint with matching input."""
        normalized_step_id = self._normalize_step_id(step_id)
        normalized_step_name = step_name.strip() or normalized_step_id
        input_json = self._dumps_payload(input_payload)
        input_hash = self._hash_payload(input_payload)

        def _start() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            task_row = conn.execute(
                "SELECT task_id FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if task_row is None:
                conn.close()
                raise KeyError(f"Task `{task_id}` not found.")

            existing = conn.execute(
                """
                SELECT * FROM task_steps
                WHERE task_id = ? AND step_id = ?
                """,
                (task_id, normalized_step_id),
            ).fetchone()
            if (
                existing is not None
                and bool(existing["is_checkpoint"])
                and str(existing["status"]) == "succeeded"
                and str(existing["input_hash"] or "") == input_hash
            ):
                conn.commit()
                conn.close()
                return self._normalize_step_row(existing)

            next_attempt = int(existing["attempt_count"] or 0) + 1 if existing else 1
            conn.execute(
                """
                INSERT INTO task_steps (
                    task_id, step_id, step_name, status,
                    input_json, input_hash, output_json,
                    is_checkpoint, idempotent, attempt_count,
                    last_error, created_at, updated_at, started_at, finished_at
                )
                VALUES (?, ?, ?, 'running', ?, ?, '{}', ?, ?, ?, '', datetime('now'),
                        datetime('now'), datetime('now'), NULL)
                ON CONFLICT(task_id, step_id) DO UPDATE SET
                    step_name = excluded.step_name,
                    status = 'running',
                    input_json = excluded.input_json,
                    input_hash = excluded.input_hash,
                    output_json = '{}',
                    is_checkpoint = excluded.is_checkpoint,
                    idempotent = excluded.idempotent,
                    attempt_count = ?,
                    last_error = '',
                    updated_at = datetime('now'),
                    started_at = datetime('now'),
                    finished_at = NULL
                """,
                (
                    task_id,
                    normalized_step_id,
                    normalized_step_name,
                    input_json,
                    input_hash,
                    1 if is_checkpoint else 0,
                    1 if idempotent else 0,
                    next_attempt,
                    next_attempt,
                ),
            )
            updated = conn.execute(
                """
                SELECT * FROM task_steps
                WHERE task_id = ? AND step_id = ?
                """,
                (task_id, normalized_step_id),
            ).fetchone()
            conn.commit()
            conn.close()
            if updated is None:
                raise RuntimeError(f"Failed to start step `{normalized_step_id}` for `{task_id}`.")
            return self._normalize_step_row(updated)

        return await asyncio.to_thread(_start)

    async def complete_task_step(
        self,
        task_id: str,
        step_id: str,
        *,
        output_payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """Mark one running task step as succeeded."""
        normalized_step_id = self._normalize_step_id(step_id)
        output_json = self._dumps_payload(output_payload)

        def _complete() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM task_steps
                WHERE task_id = ? AND step_id = ?
                """,
                (task_id, normalized_step_id),
            ).fetchone()
            if row is None:
                conn.close()
                raise KeyError(f"Step `{normalized_step_id}` for task `{task_id}` not found.")

            conn.execute(
                """
                UPDATE task_steps
                SET status = 'succeeded',
                    output_json = ?,
                    last_error = '',
                    updated_at = datetime('now'),
                    finished_at = datetime('now')
                WHERE task_id = ? AND step_id = ?
                """,
                (output_json, task_id, normalized_step_id),
            )
            updated = conn.execute(
                """
                SELECT * FROM task_steps
                WHERE task_id = ? AND step_id = ?
                """,
                (task_id, normalized_step_id),
            ).fetchone()
            conn.commit()
            conn.close()
            if updated is None:
                raise RuntimeError(
                    f"Failed to complete step `{normalized_step_id}` for `{task_id}`."
                )
            return self._normalize_step_row(updated)

        return await asyncio.to_thread(_complete)

    async def fail_task_step(
        self,
        task_id: str,
        step_id: str,
        *,
        last_error: str,
    ) -> dict[str, Any]:
        """Mark one task step as failed and persist its last error."""
        normalized_step_id = self._normalize_step_id(step_id)
        error_text = last_error[:300]

        def _fail() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM task_steps
                WHERE task_id = ? AND step_id = ?
                """,
                (task_id, normalized_step_id),
            ).fetchone()
            if row is None:
                conn.close()
                raise KeyError(f"Step `{normalized_step_id}` for task `{task_id}` not found.")

            conn.execute(
                """
                UPDATE task_steps
                SET status = 'failed',
                    last_error = ?,
                    updated_at = datetime('now'),
                    finished_at = datetime('now')
                WHERE task_id = ? AND step_id = ?
                """,
                (error_text, task_id, normalized_step_id),
            )
            updated = conn.execute(
                """
                SELECT * FROM task_steps
                WHERE task_id = ? AND step_id = ?
                """,
                (task_id, normalized_step_id),
            ).fetchone()
            conn.commit()
            conn.close()
            if updated is None:
                raise RuntimeError(f"Failed to fail step `{normalized_step_id}` for `{task_id}`.")
            return self._normalize_step_row(updated)

        return await asyncio.to_thread(_fail)

    async def clear_task_steps(self, task_id: str) -> None:
        """Delete all persisted steps for one task."""

        def _clear() -> None:
            conn = sqlite3.connect(self.db_path)
            conn.execute("DELETE FROM task_steps WHERE task_id = ?", (task_id,))
            conn.commit()
            conn.close()

        await asyncio.to_thread(_clear)

    async def transition_task(
        self,
        task_id: str,
        target_status: str,
        *,
        last_error: str = "",
    ) -> dict[str, Any]:
        """Transition one task to a new status and return the updated row."""
        target = self._validate_status(target_status)

        def _transition() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            if row is None:
                conn.close()
                raise KeyError(f"Task `{task_id}` not found.")

            current = str(row["status"])
            if not self.can_transition(current, target):
                conn.close()
                raise ValueError(
                    f"Cannot transition task `{task_id}` from `{current}` to `{target}`."
                )

            attempt_count = int(row["attempt_count"] or 0)
            started_at: Optional[str] = row["started_at"]
            finished_at: Optional[str] = row["finished_at"]
            error_text = last_error[:300] if last_error else str(row["last_error"] or "")

            if target == "running":
                attempt_count += 1
                finished_at = None
            elif target == "pending":
                started_at = None
                finished_at = None
                error_text = last_error[:300] if last_error else ""
            elif target in {"succeeded", "failed", "cancelled"}:
                finished_at = "now"
                if target in {"succeeded", "cancelled"} and not last_error:
                    error_text = ""

            conn.execute(
                """
                UPDATE tasks
                SET status = ?,
                    attempt_count = ?,
                    last_error = ?,
                    updated_at = datetime('now'),
                    cancel_requested = CASE
                        WHEN ? = 'cancelled' THEN 1
                        ELSE 0
                    END,
                    next_attempt_at = CASE
                        WHEN ? = 'pending' THEN datetime('now')
                        ELSE next_attempt_at
                    END,
                    dead_lettered = 0,
                    dead_letter_reason = '',
                    dead_lettered_at = NULL,
                    started_at = CASE
                        WHEN ? = 'running' THEN COALESCE(started_at, datetime('now'))
                        WHEN ? = 'pending' THEN NULL
                        ELSE started_at
                    END,
                    finished_at = CASE
                        WHEN ? IN ('succeeded', 'failed', 'cancelled')
                            THEN datetime('now')
                        WHEN ? IN ('pending', 'running')
                            THEN NULL
                        ELSE finished_at
                    END,
                    claimed_by = CASE
                        WHEN ? = 'running' THEN claimed_by
                        ELSE ''
                    END,
                    last_heartbeat_at = CASE
                        WHEN ? = 'running' THEN COALESCE(last_heartbeat_at, datetime('now'))
                        ELSE NULL
                    END
                WHERE task_id = ?
                """,
                (
                    target,
                    attempt_count,
                    error_text,
                    target,
                    target,
                    target,
                    target,
                    target,
                    target,
                    target,
                    target,
                    task_id,
                ),
            )
            cursor = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            updated = cursor.fetchone()
            conn.commit()
            conn.close()
            if updated is None:
                raise RuntimeError(f"Failed to update task `{task_id}`.")
            return self._normalize_row(updated)

        return await asyncio.to_thread(_transition)

    async def claim_next_task(
        self,
        *,
        source: str = "",
        sources: Optional[list[str]] = None,
        worker_id: str = "",
        starvation_threshold_seconds: int = 0,
        max_running_tasks: int = 0,
    ) -> Optional[dict[str, Any]]:
        """Atomically claim one ready task, optionally across multiple sources."""
        source_filter = source.strip()
        source_filters = list(
            dict.fromkeys(item.strip() for item in (sources or []) if item and item.strip())
        )
        if source_filter and not source_filters:
            source_filters = [source_filter]
        owner = worker_id.strip()
        starvation_threshold = self._normalize_starvation_threshold_seconds(
            starvation_threshold_seconds
        )
        running_limit = self._normalize_max_running_tasks(max_running_tasks)
        if not owner:
            raise ValueError("worker_id is required when claiming a task.")

        def _claim() -> Optional[dict[str, Any]]:
            def _iter_candidates(
                conn: sqlite3.Connection,
                where_clause: str,
                params: list[Any],
                ready_expr: str,
                threshold_text: str,
            ) -> list[sqlite3.Row]:
                seen: set[str] = set()
                rows: list[sqlite3.Row] = []
                if starvation_threshold > 0:
                    for item in conn.execute(
                        f"""
                        SELECT * FROM tasks
                        WHERE {where_clause}
                          AND {ready_expr} <= ?
                        ORDER BY {ready_expr} ASC, task_id ASC
                        LIMIT 25
                        """,
                        tuple([*params, threshold_text]),
                    ).fetchall():
                        task_id = str(item["task_id"])
                        if task_id not in seen:
                            seen.add(task_id)
                            rows.append(item)
                for item in conn.execute(
                    f"""
                    SELECT * FROM tasks
                    WHERE {where_clause}
                    ORDER BY priority DESC, {ready_expr} ASC, task_id ASC
                    LIMIT 50
                    """,
                    tuple(params),
                    ).fetchall():
                        task_id = str(item["task_id"])
                        if task_id not in seen:
                            seen.add(task_id)
                            rows.append(item)
                return rows

            def _ready_at(row: sqlite3.Row) -> str:
                return str(row["next_attempt_at"] or row["created_at"] or "")

            def _source_running_counts(conn: sqlite3.Connection) -> dict[str, int]:
                if len(source_filters) <= 1:
                    return {}
                placeholders = ",".join("?" for _ in source_filters)
                rows = conn.execute(
                    f"""
                    SELECT source, COUNT(*) AS running_count
                    FROM tasks
                    WHERE status = 'running'
                      AND source IN ({placeholders})
                    GROUP BY source
                    """,
                    tuple(source_filters),
                ).fetchall()
                return {
                    str(item["source"] or ""): int(item["running_count"] or 0)
                    for item in rows
                }

            def _apply_source_fairness(
                conn: sqlite3.Connection,
                candidates: list[sqlite3.Row],
                threshold_text: str,
            ) -> list[sqlite3.Row]:
                if len(source_filters) <= 1 or len(candidates) <= 1:
                    return candidates
                if starvation_threshold > 0 and _ready_at(candidates[0]) <= threshold_text:
                    return candidates
                top_priority = int(candidates[0]["priority"] or 0)
                tied = [
                    item for item in candidates
                    if int(item["priority"] or 0) == top_priority
                ]
                if len(tied) <= 1:
                    return candidates
                running_counts = _source_running_counts(conn)
                if len({str(item["source"] or "") for item in tied}) <= 1:
                    return candidates
                ordered = sorted(
                    tied,
                    key=lambda item: (
                        running_counts.get(str(item["source"] or ""), 0),
                        _ready_at(item),
                        str(item["task_id"] or ""),
                    ),
                )
                ordered_ids = {str(item["task_id"] or "") for item in ordered}
                return ordered + [
                    item for item in candidates
                    if str(item["task_id"] or "") not in ordered_ids
                ]

            def _apply_rate_limit(conn: sqlite3.Connection, row: sqlite3.Row) -> bool:
                rate_limit_key = str(row["rate_limit_key"] or "").strip()
                if not rate_limit_key:
                    return False
                window_seconds = self._normalize_rate_limit_window_seconds(
                    int(row["rate_limit_window_seconds"] or 0)
                )
                max_claims = self._normalize_rate_limit_max_claims(
                    int(row["rate_limit_max_claims"] or 0)
                )
                if window_seconds <= 0 or max_claims <= 0:
                    return False

                window_start = (
                    datetime.now(UTC) - timedelta(seconds=window_seconds)
                ).strftime("%Y-%m-%d %H:%M:%S")
                usage = conn.execute(
                    """
                    SELECT COUNT(*) AS claim_count,
                           MIN(last_claimed_at) AS oldest_claimed_at
                    FROM tasks
                    WHERE rate_limit_key = ?
                      AND COALESCE(last_claimed_at, '') != ''
                      AND last_claimed_at >= ?
                    """,
                    (rate_limit_key, window_start),
                ).fetchone()
                claim_count = int(usage["claim_count"] or 0) if usage else 0
                if claim_count < max_claims:
                    return False

                oldest_claimed_at = self._parse_timestamp(
                    usage["oldest_claimed_at"] if usage else None
                )
                next_allowed_dt = (
                    oldest_claimed_at + timedelta(seconds=window_seconds)
                    if oldest_claimed_at is not None
                    else datetime.now(UTC) + timedelta(seconds=window_seconds)
                )
                next_allowed = next_allowed_dt.strftime("%Y-%m-%d %H:%M:%S")
                current_next_attempt = self._parse_timestamp(row["next_attempt_at"])
                if current_next_attempt is None or current_next_attempt < next_allowed_dt:
                    conn.execute(
                        """
                        UPDATE tasks
                        SET next_attempt_at = ?,
                            updated_at = datetime('now')
                        WHERE task_id = ?
                        """,
                        (next_allowed, str(row["task_id"])),
                    )
                return True

            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            now = self._utc_now()
            threshold = (
                datetime.now(UTC) - timedelta(seconds=starvation_threshold)
            ).strftime("%Y-%m-%d %H:%M:%S")
            ready_expr = "COALESCE(next_attempt_at, created_at)"
            conditions = [
                "status = 'pending'",
                "cancel_requested = 0",
                f"{ready_expr} <= ?",
            ]
            params: list[Any] = [now]
            if source_filters:
                placeholders = ",".join("?" for _ in source_filters)
                conditions.append(f"source IN ({placeholders})")
                params.extend(source_filters)
            where_clause = " AND ".join(conditions)
            if running_limit > 0:
                running_clause = "status = 'running'"
                running_params: list[Any] = []
                if source_filters:
                    placeholders = ",".join("?" for _ in source_filters)
                    running_clause += f" AND source IN ({placeholders})"
                    running_params.extend(source_filters)
                running_count = conn.execute(
                    f"SELECT COUNT(*) FROM tasks WHERE {running_clause}",
                    tuple(running_params),
                ).fetchone()[0]
                if int(running_count or 0) >= running_limit:
                    conn.commit()
                    conn.close()
                    return None
            row = None
            candidates = _apply_source_fairness(
                conn,
                _iter_candidates(
                    conn,
                    where_clause,
                    params,
                    ready_expr,
                    threshold,
                ),
                threshold,
            )
            for candidate in candidates:
                if _apply_rate_limit(conn, candidate):
                    continue
                row = candidate
                break
            if row is None:
                conn.commit()
                conn.close()
                return None

            task_id = str(row["task_id"])
            attempt_count = int(row["attempt_count"] or 0) + 1
            conn.execute(
                """
                UPDATE tasks
                SET status = 'running',
                    attempt_count = ?,
                    updated_at = datetime('now'),
                    started_at = COALESCE(started_at, datetime('now')),
                    finished_at = NULL,
                    last_claimed_at = ?,
                    claimed_by = ?,
                    last_heartbeat_at = datetime('now'),
                    cancel_requested = 0
                WHERE task_id = ?
                """,
                (attempt_count, now, owner, task_id),
            )
            cursor = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            updated = cursor.fetchone()
            conn.commit()
            conn.close()
            return self._normalize_row(updated) if updated else None

        return await asyncio.to_thread(_claim)

    async def heartbeat_task(
        self,
        task_id: str,
        *,
        worker_id: str,
    ) -> Optional[dict[str, Any]]:
        """Refresh the lease heartbeat for one running task."""
        owner = worker_id.strip()
        if not owner:
            raise ValueError("worker_id is required when heartbeating a task.")

        def _heartbeat() -> Optional[dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                UPDATE tasks
                SET updated_at = datetime('now'),
                    last_heartbeat_at = datetime('now')
                WHERE task_id = ?
                  AND status = 'running'
                  AND claimed_by = ?
                """,
                (task_id, owner),
            )
            cursor = conn.execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,))
            row = cursor.fetchone()
            conn.commit()
            conn.close()
            if row is None or str(row["claimed_by"] or "") != owner:
                return None
            return self._normalize_row(row)

        return await asyncio.to_thread(_heartbeat)

    async def request_cancel(self, task_id: str) -> dict[str, Any]:
        """Request cancellation for one task."""

        def _cancel() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                conn.close()
                raise KeyError(f"Task `{task_id}` not found.")

            status = str(row["status"])
            if status == "pending":
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'cancelled',
                        cancel_requested = 1,
                        updated_at = datetime('now'),
                        finished_at = datetime('now'),
                        claimed_by = '',
                        last_heartbeat_at = NULL
                    WHERE task_id = ?
                    """,
                    (task_id,),
                )
            elif status == "running":
                conn.execute(
                    """
                    UPDATE tasks
                    SET cancel_requested = 1,
                        updated_at = datetime('now')
                    WHERE task_id = ?
                    """,
                    (task_id,),
                )
            elif status == "cancelled":
                conn.close()
                raise ValueError(f"Task `{task_id}` is already cancelled.")
            else:
                conn.close()
                raise ValueError(f"Task `{task_id}` is already `{status}`.")

            updated = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            conn.commit()
            conn.close()
            if updated is None:
                raise RuntimeError(f"Failed to cancel task `{task_id}`.")
            return self._normalize_row(updated)

        return await asyncio.to_thread(_cancel)

    async def fail_task_attempt(self, task_id: str, *, last_error: str) -> dict[str, Any]:
        """Persist one failed running attempt and requeue when retry budget remains."""
        error_text = last_error[:300]

        def _fail() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                conn.close()
                raise KeyError(f"Task `{task_id}` not found.")

            if str(row["status"]) != "running":
                conn.close()
                raise ValueError(f"Task `{task_id}` is not running.")

            attempt_count = int(row["attempt_count"] or 0)
            max_attempts = self._normalize_max_attempts(int(row["max_attempts"] or 1))
            backoff_seconds = self._normalize_retry_backoff_seconds(
                int(row["retry_backoff_seconds"] or 0)
            )
            can_retry = attempt_count < max_attempts

            if can_retry:
                next_attempt_at = (
                    datetime.now(UTC) + timedelta(seconds=backoff_seconds)
                ).strftime("%Y-%m-%d %H:%M:%S")
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'pending',
                        last_error = ?,
                        updated_at = datetime('now'),
                        next_attempt_at = ?,
                        dead_lettered = 0,
                        dead_letter_reason = '',
                        dead_lettered_at = NULL,
                        started_at = NULL,
                        finished_at = NULL,
                        claimed_by = '',
                        last_heartbeat_at = NULL,
                        cancel_requested = 0
                    WHERE task_id = ?
                    """,
                    (error_text, next_attempt_at, task_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'failed',
                        last_error = ?,
                        updated_at = datetime('now'),
                        dead_lettered = 1,
                        dead_letter_reason = ?,
                        dead_lettered_at = datetime('now'),
                        finished_at = datetime('now'),
                        claimed_by = '',
                        last_heartbeat_at = NULL,
                        cancel_requested = 0
                    WHERE task_id = ?
                    """,
                    (error_text, error_text, task_id),
                )

            updated = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            conn.commit()
            conn.close()
            if updated is None:
                raise RuntimeError(f"Failed to record failure for task `{task_id}`.")
            return self._normalize_row(updated)

        return await asyncio.to_thread(_fail)

    async def defer_task_attempt(
        self,
        task_id: str,
        *,
        next_attempt_at: str,
        last_error: str = "",
    ) -> dict[str, Any]:
        """Move one running task back to pending for a later retry window."""
        scheduled_for = self._parse_timestamp(next_attempt_at)
        if scheduled_for is None:
            raise ValueError("next_attempt_at must be a valid UTC timestamp.")
        scheduled_text = scheduled_for.strftime("%Y-%m-%d %H:%M:%S")
        error_text = last_error[:300]

        def _defer() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                conn.close()
                raise KeyError(f"Task `{task_id}` not found.")

            if str(row["status"]) != "running":
                conn.close()
                raise ValueError(f"Task `{task_id}` is not running.")

            conn.execute(
                """
                UPDATE tasks
                SET status = 'pending',
                    last_error = ?,
                    updated_at = datetime('now'),
                    next_attempt_at = ?,
                    dead_lettered = 0,
                    dead_letter_reason = '',
                    dead_lettered_at = NULL,
                    started_at = NULL,
                    finished_at = NULL,
                    claimed_by = '',
                    last_heartbeat_at = NULL,
                    cancel_requested = 0
                WHERE task_id = ?
                """,
                (error_text, scheduled_text, task_id),
            )
            updated = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            conn.commit()
            conn.close()
            if updated is None:
                raise RuntimeError(f"Failed to defer task `{task_id}`.")
            return self._normalize_row(updated)

        return await asyncio.to_thread(_defer)

    async def rearm_task(
        self,
        task_id: str,
        *,
        payload: Optional[dict[str, Any]] = None,
        next_attempt_at: str = "",
    ) -> dict[str, Any]:
        """Move one succeeded workflow-role task back to pending with updated payload."""
        scheduled_for = self._parse_timestamp(next_attempt_at) if next_attempt_at else None
        scheduled_text = (
            scheduled_for.strftime("%Y-%m-%d %H:%M:%S")
            if scheduled_for is not None
            else self._utc_now()
        )
        payload_json = self._dumps_payload(payload)

        def _rearm() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                conn.close()
                raise KeyError(f"Task `{task_id}` not found.")

            status = str(row["status"] or "")
            task_type = str(row["task_type"] or "")
            if task_type != "workflow_role":
                conn.close()
                raise ValueError(
                    f"Task `{task_id}` must be `workflow_role` before rearm."
                )
            if status != "succeeded":
                conn.close()
                raise ValueError(f"Task `{task_id}` must be `succeeded` before rearm.")

            conn.execute(
                """
                UPDATE tasks
                SET status = 'pending',
                    payload = ?,
                    updated_at = datetime('now'),
                    next_attempt_at = ?,
                    cancel_requested = 0,
                    dead_lettered = 0,
                    dead_letter_reason = '',
                    dead_lettered_at = NULL,
                    last_error = '',
                    started_at = NULL,
                    finished_at = NULL,
                    claimed_by = '',
                    last_heartbeat_at = NULL
                WHERE task_id = ?
                """,
                (payload_json, scheduled_text, task_id),
            )
            updated = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            conn.commit()
            conn.close()
            if updated is None:
                raise RuntimeError(f"Failed to rearm task `{task_id}`.")
            return self._normalize_row(updated)

        return await asyncio.to_thread(_rearm)

    async def requeue_task(self, task_id: str) -> dict[str, Any]:
        """Move one terminal task back to pending and reset retry state."""

        def _requeue() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            if row is None:
                conn.close()
                raise KeyError(f"Task `{task_id}` not found.")

            status = str(row["status"] or "")
            if status not in {"failed", "cancelled"}:
                conn.close()
                raise ValueError(
                    f"Task `{task_id}` must be `failed` or `cancelled` before requeue."
                )

            conn.execute(
                """
                UPDATE tasks
                SET status = 'pending',
                    updated_at = datetime('now'),
                    next_attempt_at = datetime('now'),
                    dead_lettered = 0,
                    dead_letter_reason = '',
                    dead_lettered_at = NULL,
                    cancel_requested = 0,
                    attempt_count = 0,
                    last_error = '',
                    started_at = NULL,
                    finished_at = NULL,
                    claimed_by = '',
                    last_heartbeat_at = NULL
                WHERE task_id = ?
                """,
                (task_id,),
            )
            conn.execute("DELETE FROM task_steps WHERE task_id = ?", (task_id,))
            updated = conn.execute(
                "SELECT * FROM tasks WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            conn.commit()
            conn.close()
            if updated is None:
                raise RuntimeError(f"Failed to requeue task `{task_id}`.")
            return self._normalize_row(updated)

        return await asyncio.to_thread(_requeue)

    async def get_queue_metrics(
        self,
        *,
        source: str = "",
        starvation_threshold_seconds: int = 0,
        lease_timeout_seconds: int = 0,
        stall_threshold_seconds: int = 0,
    ) -> dict[str, Any]:
        """Return queue backlog and retry timing metrics for one source."""
        source_filter = source.strip()
        now = self._utc_now()
        starvation_threshold = self._normalize_starvation_threshold_seconds(
            starvation_threshold_seconds
        )
        lease_timeout = max(0, int(lease_timeout_seconds))
        stall_threshold = max(0, int(stall_threshold_seconds))
        starved_before = (
            datetime.now(UTC) - timedelta(seconds=starvation_threshold)
        ).strftime("%Y-%m-%d %H:%M:%S")
        stale_before = (
            datetime.now(UTC) - timedelta(seconds=lease_timeout)
        ).strftime("%Y-%m-%d %H:%M:%S")

        def _query() -> dict[str, Any]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            where_clause = ""
            ready_expr = "COALESCE(next_attempt_at, created_at)"
            stale_expr = "COALESCE(last_heartbeat_at, updated_at, started_at, created_at)"
            params: list[Any] = [
                now,
                now,
                now,
                starved_before,
                now,
                now,
                lease_timeout,
                stale_before,
                lease_timeout,
                stale_before,
            ]
            if source_filter:
                where_clause = "WHERE source = ?"
                params.append(source_filter)
            row = conn.execute(
                f"""
                SELECT
                    COUNT(*) AS total_tasks,
                    SUM(CASE
                        WHEN status = 'pending'
                         AND cancel_requested = 0
                         AND {ready_expr} <= ?
                        THEN 1 ELSE 0 END
                    ) AS ready_backlog,
                    SUM(CASE
                        WHEN status = 'pending'
                         AND cancel_requested = 0
                         AND {ready_expr} > ?
                        THEN 1 ELSE 0 END
                    ) AS retry_backlog,
                    SUM(CASE WHEN status = 'running' THEN 1 ELSE 0 END) AS running_tasks,
                    SUM(CASE
                        WHEN status = 'running' AND cancel_requested = 1
                        THEN 1 ELSE 0 END
                    ) AS cancel_requested_running,
                    SUM(CASE
                        WHEN status = 'failed' AND dead_lettered = 1
                        THEN 1 ELSE 0 END
                    ) AS dead_letter_tasks,
                    SUM(CASE
                        WHEN status = 'pending'
                         AND cancel_requested = 0
                         AND {ready_expr} > ?
                         AND rate_limit_key != ''
                        THEN 1 ELSE 0 END
                    ) AS rate_limited_backlog,
                    COUNT(DISTINCT CASE
                        WHEN status = 'running' AND claimed_by != ''
                        THEN claimed_by END
                    ) AS running_workers,
                    SUM(CASE
                        WHEN status = 'pending'
                         AND cancel_requested = 0
                         AND {ready_expr} <= ?
                        THEN 1 ELSE 0 END
                    ) AS starved_ready_tasks,
                    MIN(CASE
                        WHEN status = 'pending'
                         AND cancel_requested = 0
                         AND {ready_expr} <= ?
                        THEN {ready_expr} END
                    ) AS oldest_ready_created_at,
                    MIN(CASE
                        WHEN status = 'pending'
                         AND cancel_requested = 0
                         AND {ready_expr} > ?
                        THEN {ready_expr} END
                    ) AS next_retry_at,
                    SUM(CASE
                        WHEN ? > 0
                         AND status = 'running'
                         AND {stale_expr} <= ?
                        THEN 1 ELSE 0 END
                    ) AS stale_running_tasks,
                    MIN(CASE
                        WHEN ? > 0
                         AND status = 'running'
                         AND {stale_expr} <= ?
                        THEN {stale_expr} END
                    ) AS oldest_stale_reference_at
                FROM tasks
                {where_clause}
                """,
                tuple(params),
            ).fetchone()
            conn.close()
            data = dict(row) if row is not None else {}
            oldest_ready_age = self._seconds_since_timestamp(data.get("oldest_ready_created_at"))
            next_retry_in = self._seconds_until_timestamp(data.get("next_retry_at"))
            oldest_stale_age = self._seconds_since_timestamp(data.get("oldest_stale_reference_at"))
            return {
                "source": source_filter,
                "total_tasks": int(data.get("total_tasks") or 0),
                "ready_backlog": int(data.get("ready_backlog") or 0),
                "retry_backlog": int(data.get("retry_backlog") or 0),
                "running_tasks": int(data.get("running_tasks") or 0),
                "running_workers": int(data.get("running_workers") or 0),
                "cancel_requested_running": int(data.get("cancel_requested_running") or 0),
                "dead_letter_tasks": int(data.get("dead_letter_tasks") or 0),
                "rate_limited_backlog": int(data.get("rate_limited_backlog") or 0),
                "starved_ready_tasks": int(data.get("starved_ready_tasks") or 0),
                "stale_running_tasks": int(data.get("stale_running_tasks") or 0),
                "oldest_ready_age_seconds": oldest_ready_age or 0,
                "oldest_stale_running_age_seconds": oldest_stale_age or 0,
                "next_retry_in_seconds": next_retry_in or 0,
                "starvation_threshold_seconds": starvation_threshold,
                "lease_timeout_seconds": lease_timeout,
                "stall_threshold_seconds": stall_threshold,
            }

        return await asyncio.to_thread(_query)

    async def recover_orphaned_tasks(
        self,
        *,
        lease_timeout_seconds: int,
        source: str = "",
        exclude_worker_id: str = "",
    ) -> list[dict[str, Any]]:
        """Move stale running tasks back to pending for re-claim."""
        timeout_seconds = max(1, int(lease_timeout_seconds))
        source_filter = source.strip()
        excluded_worker = exclude_worker_id.strip()
        threshold = (
            datetime.now(UTC) - timedelta(seconds=timeout_seconds)
        ).strftime("%Y-%m-%d %H:%M:%S")

        def _recover() -> list[dict[str, Any]]:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("BEGIN IMMEDIATE")
            stale_expr = "COALESCE(last_heartbeat_at, updated_at, started_at, created_at)"
            query = f"""
                SELECT *, {stale_expr} AS stale_reference_at
                FROM tasks
                WHERE status = 'running'
                  AND {stale_expr} <= ?
            """
            params: list[Any] = [threshold]
            if source_filter:
                query += " AND source = ?"
                params.append(source_filter)
            if excluded_worker:
                query += " AND (claimed_by = '' OR claimed_by != ?)"
                params.append(excluded_worker)
            query += " ORDER BY created_at ASC, task_id ASC"
            rows = conn.execute(query, tuple(params)).fetchall()
            recovered: list[dict[str, Any]] = []
            for row in rows:
                task_id = str(row["task_id"])
                conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'pending',
                        updated_at = datetime('now'),
                        next_attempt_at = datetime('now'),
                        started_at = NULL,
                        finished_at = NULL,
                        claimed_by = '',
                        last_heartbeat_at = NULL
                    WHERE task_id = ?
                    """,
                    (task_id,),
                )
                updated = conn.execute(
                    "SELECT * FROM tasks WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                if updated is not None:
                    item = self._normalize_row(updated)
                    item["recovered_from_worker"] = str(row["claimed_by"] or "")
                    item["stale_reference_at"] = row["stale_reference_at"]
                    item["stale_age_seconds"] = (
                        self._seconds_since_timestamp(row["stale_reference_at"]) or 0
                    )
                    recovered.append(item)
            conn.commit()
            conn.close()
            return recovered

        return await asyncio.to_thread(_recover)


_task_store: Optional[TaskStore] = None


def get_task_store() -> TaskStore:
    """Return the global task store."""
    global _task_store
    if _task_store is None:
        from nanoclaw.core.config import get_data_path

        _task_store = TaskStore(get_data_path() / "nanoclaw.db")
    return _task_store


def set_task_store(store: TaskStore) -> None:
    """Replace the global task store instance."""
    global _task_store
    _task_store = store
