"""Desired-state persistence tests for gateway channel orchestration."""

from __future__ import annotations

import pytest

from nanoclaw.channels.state_store import ChannelStateStore


@pytest.mark.asyncio
async def test_channel_state_store_persists_rows(tmp_path) -> None:
    """Channel state rows should round-trip through SQLite."""
    store = ChannelStateStore(tmp_path / "nanoclaw.db")

    await store.save_state(
        {
            "channel_name": "telegram",
            "desired_state": "running",
            "desired_reason": "config default",
            "desired_updated_at": 100,
            "actual_status": "running",
            "actual_detail": "polling runtime active",
            "reconcile_status": "reconciled",
            "reconcile_detail": "desired `running` satisfied",
            "drift_status": "in_sync",
            "drift_since": 0,
            "drift_count": 0,
            "last_reconciled_at": 100,
            "last_action": "startup",
            "last_action_at": 100,
        }
    )

    rows = await store.list_states()

    assert rows["telegram"]["desired_state"] == "running"
    assert rows["telegram"]["actual_status"] == "running"
    assert rows["telegram"]["reconcile_status"] == "reconciled"
