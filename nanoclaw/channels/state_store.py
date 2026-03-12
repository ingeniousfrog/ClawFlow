"""SQLite-backed desired-state store for gateway channel orchestration."""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any, Optional

from nanoclaw.core.config import get_data_path


class ChannelStateStore:
    """Persist compact desired-state and reconcile metadata for managed channels."""

    def __init__(self, db_path: str | Path):
        """Initialize the store and create tables if needed."""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Create the channel state table when it does not exist."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS channel_runtime_state (
                    channel_name TEXT PRIMARY KEY,
                    desired_state TEXT NOT NULL DEFAULT 'stopped',
                    desired_reason TEXT NOT NULL DEFAULT '',
                    desired_updated_at INTEGER NOT NULL DEFAULT 0,
                    actual_status TEXT NOT NULL DEFAULT '',
                    actual_detail TEXT NOT NULL DEFAULT '',
                    reconcile_status TEXT NOT NULL DEFAULT '',
                    reconcile_detail TEXT NOT NULL DEFAULT '',
                    drift_status TEXT NOT NULL DEFAULT 'unknown',
                    drift_since INTEGER NOT NULL DEFAULT 0,
                    drift_count INTEGER NOT NULL DEFAULT 0,
                    last_reconciled_at INTEGER NOT NULL DEFAULT 0,
                    last_action TEXT NOT NULL DEFAULT '',
                    last_action_at INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _normalize_row(row: sqlite3.Row) -> dict[str, Any]:
        """Return one compact normalized row."""
        return {
            "channel_name": str(row["channel_name"]),
            "desired_state": str(row["desired_state"] or "stopped"),
            "desired_reason": str(row["desired_reason"] or ""),
            "desired_updated_at": int(row["desired_updated_at"] or 0),
            "actual_status": str(row["actual_status"] or ""),
            "actual_detail": str(row["actual_detail"] or ""),
            "reconcile_status": str(row["reconcile_status"] or ""),
            "reconcile_detail": str(row["reconcile_detail"] or ""),
            "drift_status": str(row["drift_status"] or "unknown"),
            "drift_since": int(row["drift_since"] or 0),
            "drift_count": int(row["drift_count"] or 0),
            "last_reconciled_at": int(row["last_reconciled_at"] or 0),
            "last_action": str(row["last_action"] or ""),
            "last_action_at": int(row["last_action_at"] or 0),
        }

    async def list_states(self) -> dict[str, dict[str, Any]]:
        """Return all persisted channel states keyed by channel name."""
        return await asyncio.to_thread(self._list_states_sync)

    def _list_states_sync(self) -> dict[str, dict[str, Any]]:
        """Read all persisted channel states."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT * FROM channel_runtime_state ORDER BY channel_name"
            ).fetchall()
        finally:
            conn.close()
        return {
            str(row["channel_name"]): self._normalize_row(row)
            for row in rows
        }

    async def save_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Insert or update one full channel state row."""
        normalized = {
            "channel_name": str(state.get("channel_name") or "").strip(),
            "desired_state": str(state.get("desired_state") or "stopped").strip() or "stopped",
            "desired_reason": str(state.get("desired_reason") or "").strip(),
            "desired_updated_at": int(state.get("desired_updated_at") or 0),
            "actual_status": str(state.get("actual_status") or "").strip(),
            "actual_detail": str(state.get("actual_detail") or "").strip(),
            "reconcile_status": str(state.get("reconcile_status") or "").strip(),
            "reconcile_detail": str(state.get("reconcile_detail") or "").strip(),
            "drift_status": str(state.get("drift_status") or "unknown").strip() or "unknown",
            "drift_since": int(state.get("drift_since") or 0),
            "drift_count": int(state.get("drift_count") or 0),
            "last_reconciled_at": int(state.get("last_reconciled_at") or 0),
            "last_action": str(state.get("last_action") or "").strip(),
            "last_action_at": int(state.get("last_action_at") or 0),
        }
        if not normalized["channel_name"]:
            raise ValueError("channel_name is required")
        await asyncio.to_thread(self._save_state_sync, normalized)
        return normalized

    def _save_state_sync(self, state: dict[str, Any]) -> None:
        """Persist one normalized channel state row."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO channel_runtime_state (
                    channel_name,
                    desired_state,
                    desired_reason,
                    desired_updated_at,
                    actual_status,
                    actual_detail,
                    reconcile_status,
                    reconcile_detail,
                    drift_status,
                    drift_since,
                    drift_count,
                    last_reconciled_at,
                    last_action,
                    last_action_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(channel_name) DO UPDATE SET
                    desired_state = excluded.desired_state,
                    desired_reason = excluded.desired_reason,
                    desired_updated_at = excluded.desired_updated_at,
                    actual_status = excluded.actual_status,
                    actual_detail = excluded.actual_detail,
                    reconcile_status = excluded.reconcile_status,
                    reconcile_detail = excluded.reconcile_detail,
                    drift_status = excluded.drift_status,
                    drift_since = excluded.drift_since,
                    drift_count = excluded.drift_count,
                    last_reconciled_at = excluded.last_reconciled_at,
                    last_action = excluded.last_action,
                    last_action_at = excluded.last_action_at
                """,
                (
                    state["channel_name"],
                    state["desired_state"],
                    state["desired_reason"],
                    state["desired_updated_at"],
                    state["actual_status"],
                    state["actual_detail"],
                    state["reconcile_status"],
                    state["reconcile_detail"],
                    state["drift_status"],
                    state["drift_since"],
                    state["drift_count"],
                    state["last_reconciled_at"],
                    state["last_action"],
                    state["last_action_at"],
                ),
            )
            conn.commit()
        finally:
            conn.close()


_channel_state_store: Optional[ChannelStateStore] = None


def get_channel_state_store() -> ChannelStateStore:
    """Return the global channel state store."""
    global _channel_state_store
    if _channel_state_store is None:
        _channel_state_store = ChannelStateStore(get_data_path() / "nanoclaw.db")
    return _channel_state_store


def set_channel_state_store(store: ChannelStateStore | None) -> None:
    """Replace the global channel state store instance."""
    global _channel_state_store
    _channel_state_store = store
