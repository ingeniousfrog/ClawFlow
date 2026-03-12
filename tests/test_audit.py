"""Audit log tests."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from nanoclaw.security.audit import AuditLog
from nanoclaw.runtime.tasks import TaskStore


@pytest.mark.asyncio
async def test_workflow_run_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Workflow telemetry rows should persist and return parsed JSON fields."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")

    await audit.log_workflow_run(
        session_id="cli:user",
        workflow_name="grounded_current_info",
        workflow_identity="workflow_chain_1",
        workflow_tags=["default_chat_loop", "grounded_current_info"],
        user_summary="show latest AI papers",
        status="degraded",
        failure_reason="paper_search:error",
        total_tokens=321,
        execution_ms=1450,
        llm_calls=3,
        tool_calls=2,
        final_model="gpt-5.2",
        call_chain=[
            {
                "type": "workflow_role_checkpoint",
                "checkpoint_id": "planner@pre_llm",
                "role": "planner",
                "stage": "pre_llm",
                "message_count": 3,
                "evidence_count": 0,
                "evidence_refs": [],
                "evidence_items": [],
            },
            {
                "type": "workflow_role_execution",
                "role": "planner",
                "role_label": "planner",
                "stage": "pre_llm",
                "checkpoint_id": "planner@pre_llm",
                "status": "attached",
                "workflow_name": "grounded_current_info",
                "handler_kind": "execution_brief",
                "artifact_preview": "Plan checkpoints and execution order.",
                "contract": {"needs_grounded": True},
                "evidence_refs": [],
            },
            {
                "type": "workflow_role_task",
                "role": "executor",
                "role_label": "executor",
                "stage": "tool_phase",
                "task_key": "executor@tool_phase",
                "status": "attached",
                "depends_on": ["router@pre_llm"],
                "checkpoint_id": "",
                "resume_checkpoint_id": "router@pre_llm",
                "retry_budget": 2,
                "evidence_refs": ["ev_1"],
            },
            {
                "type": "workflow_role_task_bridge",
                "task_key": "executor@tool_phase",
                "role": "executor",
                "role_label": "executor",
                "stage": "tool_phase",
                "task_type": "workflow_role",
                "source": "workflow_role",
                "description": "grounded_current_info:executor@tool_phase",
                "priority": 760,
                "timeout_seconds": 600,
                "max_attempts": 2,
                "idempotency_key": "cli:user:grounded_current_info:executor@tool_phase",
                "payload": {
                    "session_id": "cli:user",
                    "parent_task_id": "task_parent_1",
                    "workflow_name": "grounded_current_info",
                    "role": "executor",
                    "stage": "tool_phase",
                    "task_key": "executor@tool_phase",
                    "depends_on": ["router@pre_llm"],
                    "checkpoint_id": "",
                    "resume_checkpoint_id": "router@pre_llm",
                    "retry_budget": 2,
                    "evidence_refs": ["ev_1"],
                    "evidence_snapshot": {
                        "count": 1,
                        "items": [
                            {
                                "evidence_id": "ev_1",
                                "tool_name": "paper_search",
                                "url": "https://example.com/paper",
                            }
                        ],
                    },
                },
                "evidence_refs": ["ev_1"],
            },
            {"type": "llm", "model": "gpt-5.2", "status": "success", "tokens": 120},
            {
                "type": "tool",
                "name": "paper_search",
                "status": "error",
                "execution_ms": 55,
                "cached": False,
                "evidence_refs": ["ev_1"],
            },
            {
                "type": "workflow_role_recovery",
                "failed_role": "executor",
                "recovery_role": "router",
                "stage": "post_tools",
                "reason": "paper_search:error",
                "resume_checkpoint_id": "router@pre_llm",
                "attempt_number": 1,
                "budget_limit": 2,
                "remaining_budget": 1,
                "restored_messages": 3,
                "restored_evidence_count": 0,
                "status": "resumed",
                "evidence_refs": ["ev_1"],
            },
            {
                "type": "workflow_role_resume",
                "role": "router",
                "stage": "pre_llm",
                "resume_checkpoint_id": "router@pre_llm",
                "source_workflow_run_id": 11,
                "source_workflow_name": "grounded_current_info",
                "source_status": "degraded",
                "failure_reason": "paper_search:error",
                "restored_evidence_count": 1,
                "status": "resumed",
                "evidence_refs": ["ev_1"],
            },
        ],
    )

    rows = await audit.get_recent_workflows(limit=5)
    assert len(rows) == 1
    row = rows[0]
    assert row["workflow_name"] == "grounded_current_info"
    assert row["workflow_identity"] == "workflow_chain_1"
    assert row["workflow_tags"] == ["default_chat_loop", "grounded_current_info"]
    assert row["call_chain"][5]["name"] == "paper_search"
    assert row["role_checkpoint_timeline"][0]["checkpoint_id"] == "planner@pre_llm"
    assert row["role_execution_timeline"][0]["role"] == "planner"
    assert row["role_execution_timeline"][0]["role_label"] == "planner"
    assert row["role_execution_timeline"][0]["checkpoint_id"] == "planner@pre_llm"
    assert row["role_execution_timeline"][0]["artifact_preview"] == (
        "Plan checkpoints and execution order."
    )
    assert row["role_task_timeline"][0]["task_key"] == "executor@tool_phase"
    assert row["role_task_timeline"][0]["role_label"] == "executor"
    assert row["role_task_timeline"][0]["retry_budget"] == 2
    assert row["role_task_bridge_timeline"][0]["task_type"] == "workflow_role"
    assert row["role_task_bridge_timeline"][0]["role_label"] == "executor"
    assert row["role_task_bridge_timeline"][0]["payload"]["parent_task_id"] == "task_parent_1"
    assert row["role_recovery_timeline"][0]["failed_role"] == "executor"
    assert row["role_recovery_timeline"][0]["resume_checkpoint_id"] == "router@pre_llm"
    assert row["role_recovery_timeline"][0]["status"] == "resumed"
    assert row["role_resume_timeline"][0]["source_workflow_run_id"] == 11
    assert row["role_resume_timeline"][0]["restored_evidence_count"] == 1
    assert row["shared_evidence_refs"] == ["ev_1"]
    assert row["failure_reason"] == "paper_search:error"
    assert row["integrity_hash"]

    evaluations = await audit.get_recent_workflow_evaluations(limit=5)
    assert len(evaluations) == 1
    evaluation = evaluations[0]
    assert evaluation["workflow_name"] == "grounded_current_info"
    assert evaluation["evaluation_label"] == "poor"
    assert evaluation["quality_score"] < 55
    assert evaluation["suggestions"]
    assert "tool_failure" in evaluation["failure_classes"]
    assert evaluation["attention_reasons"]
    assert evaluation["follow_up_actions"]
    assert evaluation["integrity_hash"]

    role_replay = await audit.get_workflow_role_replay(row["id"])
    assert role_replay is not None
    assert role_replay["workflow_name"] == "grounded_current_info"
    assert role_replay["workflow_identity"] == "workflow_chain_1"
    assert role_replay["role_checkpoint_timeline"][0]["message_count"] == 3
    assert role_replay["role_execution_timeline"][0]["role"] == "planner"
    assert role_replay["role_execution_timeline"][0]["role_label"] == "planner"
    assert role_replay["role_task_timeline"][0]["depends_on"] == ["router@pre_llm"]
    assert role_replay["role_task_bridge_timeline"][0]["source"] == "workflow_role"
    assert role_replay["role_recovery_timeline"][0]["recovery_role"] == "router"
    assert role_replay["role_recovery_timeline"][0]["remaining_budget"] == 1
    assert role_replay["role_recovery_timeline"][0]["restored_messages"] == 3
    assert role_replay["role_resume_timeline"][0]["resume_checkpoint_id"] == "router@pre_llm"
    assert role_replay["shared_evidence_refs"] == ["ev_1"]


@pytest.mark.asyncio
async def test_workflow_run_identity_falls_back_to_context_when_column_empty(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workflow rows should expose identity from context during transitional replay."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")

    await audit.log_workflow_run(
        session_id="cli:user",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=12,
        execution_ms=30,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[
            {
                "type": "workflow_context",
                "name": "workflow_identity",
                "status": "attached",
                "value": "workflow_fallback",
            },
            {"type": "llm", "model": "gpt-5-mini", "status": "success"},
        ],
    )

    rows = await audit.get_recent_workflows(limit=1)

    assert rows[0]["workflow_identity"] == "workflow_fallback"


@pytest.mark.asyncio
async def test_workflow_run_migration_adds_workflow_identity_column(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Opening an older audit database should add the workflow_identity column."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    db_path = tmp_path / "audit.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE workflow_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL DEFAULT (datetime('now')),
            session_id TEXT NOT NULL,
            workflow_name TEXT NOT NULL,
            workflow_tags TEXT DEFAULT '[]',
            user_summary TEXT,
            status TEXT NOT NULL DEFAULT 'success',
            failure_reason TEXT DEFAULT '',
            total_tokens INTEGER DEFAULT 0,
            execution_ms INTEGER DEFAULT 0,
            llm_calls INTEGER DEFAULT 0,
            tool_calls INTEGER DEFAULT 0,
            final_model TEXT DEFAULT '',
            call_chain TEXT DEFAULT '[]',
            integrity_hash TEXT
        )
        """
    )
    conn.commit()
    conn.close()

    AuditLog(db_path)

    conn = sqlite3.connect(db_path)
    columns = {
        str(row[1]): str(row[2])
        for row in conn.execute("PRAGMA table_info(workflow_runs)").fetchall()
    }
    indexes = {
        str(row[1])
        for row in conn.execute("PRAGMA index_list(workflow_runs)").fetchall()
    }
    conn.close()

    assert columns["workflow_identity"] == "TEXT"
    assert "idx_workflow_identity" in indexes


@pytest.mark.asyncio
async def test_get_latest_role_resume_state_returns_latest_resumable_checkpoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit should surface one latest resumable checkpoint for a session workflow."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")

    await audit.log_workflow_run(
        session_id="cli:user",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest news",
        status="degraded",
        failure_reason="provider_timeout",
        total_tokens=210,
        execution_ms=820,
        llm_calls=2,
        tool_calls=1,
        final_model="gpt-5.2",
        call_chain=[
            {
                "type": "workflow_role_checkpoint",
                "checkpoint_id": "router@pre_llm",
                "role": "router",
                "stage": "pre_llm",
                "message_count": 4,
                "evidence_count": 1,
                "evidence_refs": ["ev_1"],
                "evidence_items": [
                    {
                        "evidence_id": "ev_1",
                        "tool_name": "web_search",
                        "url": "https://example.com/article",
                        "title": "Example article",
                        "snippet": "Example snippet",
                    }
                ],
            },
            {
                "type": "workflow_role_recovery",
                "failed_role": "executor",
                "recovery_role": "router",
                "stage": "post_tools",
                "reason": "provider_timeout",
                "resume_checkpoint_id": "router@pre_llm",
                "attempt_number": 1,
                "budget_limit": 2,
                "remaining_budget": 1,
                "restored_messages": 2,
                "restored_evidence_count": 1,
                "status": "resumed",
                "evidence_refs": ["ev_1"],
            },
        ],
    )

    item = await audit.get_latest_role_resume_state("cli:user", "grounded_current_info")

    assert item is not None
    assert item["source_workflow_run_id"] > 0
    assert item["resume_checkpoint_id"] == "router@pre_llm"
    assert item["role"] == "router"
    assert item["stage"] == "pre_llm"
    assert item["evidence_refs"] == ["ev_1"]
    assert item["evidence_snapshot"]["count"] == 1
    assert item["evidence_snapshot"]["items"][0]["evidence_id"] == "ev_1"


@pytest.mark.asyncio
async def test_get_latest_role_resume_state_matches_parent_session_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audit resume lookup should also match degraded runs by parent-session context."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")

    await audit.log_workflow_run(
        session_id="task:old_parent",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest news",
        status="degraded",
        failure_reason="provider_timeout",
        total_tokens=210,
        execution_ms=820,
        llm_calls=2,
        tool_calls=1,
        final_model="gpt-5.2",
        call_chain=[
            {
                "type": "workflow_context",
                "name": "parent_session_id",
                "status": "attached",
                "value": "telegram:42",
            },
            {
                "type": "workflow_role_checkpoint",
                "checkpoint_id": "router@pre_llm",
                "role": "router",
                "stage": "pre_llm",
                "message_count": 4,
                "evidence_count": 1,
                "evidence_refs": ["ev_1"],
                "evidence_items": [
                    {
                        "evidence_id": "ev_1",
                        "tool_name": "web_search",
                        "url": "https://example.com/article",
                        "title": "Example article",
                        "snippet": "Example snippet",
                    }
                ],
            },
            {
                "type": "workflow_role_recovery",
                "failed_role": "executor",
                "recovery_role": "router",
                "stage": "post_tools",
                "reason": "provider_timeout",
                "resume_checkpoint_id": "router@pre_llm",
                "attempt_number": 1,
                "budget_limit": 2,
                "remaining_budget": 1,
                "restored_messages": 2,
                "restored_evidence_count": 1,
                "status": "resumed",
                "evidence_refs": ["ev_1"],
            },
        ],
    )

    item = await audit.get_latest_role_resume_state("telegram:42", "grounded_current_info")

    assert item is not None
    assert item["source_workflow_run_id"] > 0
    assert item["resume_checkpoint_id"] == "router@pre_llm"
    assert item["evidence_snapshot"]["count"] == 1


@pytest.mark.asyncio
async def test_get_latest_role_resume_state_prefers_workflow_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit workflow identity should win over recency and session fallback."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")

    await audit.log_workflow_run(
        session_id="task:older_parent",
        workflow_name="grounded_current_info",
        workflow_identity="workflow_target",
        workflow_tags=["grounded_current_info"],
        user_summary="preferred resume chain",
        status="degraded",
        failure_reason="provider_timeout",
        total_tokens=210,
        execution_ms=820,
        llm_calls=2,
        tool_calls=1,
        final_model="gpt-5.2",
        call_chain=[
            {
                "type": "workflow_role_checkpoint",
                "checkpoint_id": "router@pre_llm",
                "role": "router",
                "stage": "pre_llm",
                "message_count": 4,
                "evidence_count": 1,
                "evidence_refs": ["ev_target"],
                "evidence_items": [
                    {
                        "evidence_id": "ev_target",
                        "tool_name": "web_search",
                        "url": "https://example.com/target",
                    }
                ],
            },
            {
                "type": "workflow_role_recovery",
                "failed_role": "executor",
                "recovery_role": "router",
                "stage": "post_tools",
                "reason": "provider_timeout",
                "resume_checkpoint_id": "router@pre_llm",
                "attempt_number": 1,
                "budget_limit": 2,
                "remaining_budget": 1,
                "restored_messages": 2,
                "restored_evidence_count": 1,
                "status": "resumed",
                "evidence_refs": ["ev_target"],
            },
        ],
    )
    await audit.log_workflow_run(
        session_id="task:newer_parent",
        workflow_name="grounded_current_info",
        workflow_identity="workflow_other",
        workflow_tags=["grounded_current_info"],
        user_summary="newer unrelated chain",
        status="degraded",
        failure_reason="provider_timeout",
        total_tokens=210,
        execution_ms=820,
        llm_calls=2,
        tool_calls=1,
        final_model="gpt-5.2",
        call_chain=[
            {
                "type": "workflow_role_checkpoint",
                "checkpoint_id": "router@pre_llm",
                "role": "router",
                "stage": "pre_llm",
                "message_count": 4,
                "evidence_count": 1,
                "evidence_refs": ["ev_other"],
                "evidence_items": [
                    {
                        "evidence_id": "ev_other",
                        "tool_name": "web_search",
                        "url": "https://example.com/other",
                    }
                ],
            },
            {
                "type": "workflow_role_recovery",
                "failed_role": "executor",
                "recovery_role": "router",
                "stage": "post_tools",
                "reason": "provider_timeout",
                "resume_checkpoint_id": "router@pre_llm",
                "attempt_number": 1,
                "budget_limit": 2,
                "remaining_budget": 1,
                "restored_messages": 2,
                "restored_evidence_count": 1,
                "status": "resumed",
                "evidence_refs": ["ev_other"],
            },
        ],
    )

    item = await audit.get_latest_role_resume_state(
        "telegram:42",
        "grounded_current_info",
        workflow_identity="workflow_target",
    )

    assert item is not None
    assert item["workflow_identity"] == "workflow_target"
    assert item["evidence_refs"] == ["ev_target"]


@pytest.mark.asyncio
async def test_workflow_stats_today(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Workflow stats should aggregate today's runs."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=100,
        execution_ms=200,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )
    await audit.log_workflow_run(
        session_id="s2",
        workflow_name="heartbeat_checklist",
        workflow_tags=["heartbeat_checklist"],
        user_summary="Heartbeat checklist run",
        status="degraded",
        failure_reason="grounded_evidence_missing",
        total_tokens=220,
        execution_ms=800,
        llm_calls=2,
        tool_calls=1,
        final_model="gpt-5.2",
        call_chain=[
            {"type": "llm", "model": "gpt-5.2", "status": "success"},
            {"type": "tool", "name": "web_search", "status": "success"},
        ],
    )

    stats = await audit.get_workflow_stats_today()
    assert stats["workflow_runs"] == 2
    assert stats["failures"] == 1
    assert stats["total_tokens"] == 320
    assert stats["avg_execution_ms"] >= 500
    assert stats["max_execution_ms"] == 800

    evaluation_stats = await audit.get_workflow_evaluation_stats_today()
    assert evaluation_stats["evaluations"] == 2
    assert evaluation_stats["good_runs"] == 1
    assert evaluation_stats["review_runs"] == 1
    assert evaluation_stats["poor_runs"] == 0
    assert evaluation_stats["avg_quality_score"] >= 70
    assert evaluation_stats["avg_efficiency_score"] >= 80
    assert evaluation_stats["positive_feedback"] == 0
    assert evaluation_stats["neutral_feedback"] == 0
    assert evaluation_stats["negative_feedback"] == 0


@pytest.mark.asyncio
async def test_boundary_metrics_aggregate_boundary_and_secret_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Boundary metrics should summarize recent boundary and secret-access events."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")

    await audit.log(
        action_type="boundary_decision",
        tool_name="web_fetch",
        input_summary=(
            "operation=web_fetch boundary=outbound_url action=fetch "
            "target=https://example.com/article"
        ),
        output_summary="policy=shared_tool_boundary version=v0 decision=allowed",
        status="success",
        session_id="task:one",
    )
    await audit.log(
        action_type="boundary_decision",
        tool_name="web_fetch",
        input_summary=(
            "operation=web_fetch boundary=outbound_url action=fetch "
            "target=https://bad.example.com/article"
        ),
        output_summary="policy=shared_tool_boundary version=v0 decision=blocked",
        status="blocked",
        session_id="task:one",
    )
    await audit.log(
        action_type="secret_access",
        tool_name="web_search",
        input_summary=(
            "capability=web_search.serper_api_key "
            "source=config:tools.webSearch.serperApiKey"
        ),
        output_summary="policy=tool_secret_broker version=v0 decision=granted",
        status="success",
        session_id="task:one",
    )
    await audit.log(
        action_type="secret_access",
        tool_name="web_search",
        input_summary="capability=web_search.brave_api_key source=none",
        output_summary="policy=tool_secret_broker version=v0 decision=missing",
        status="success",
        session_id="task:one",
    )

    metrics = await audit.get_boundary_metrics(window_hours=24)

    assert metrics["boundary"]["total"] == 2
    assert metrics["boundary"]["allowed"] == 1
    assert metrics["boundary"]["blocked"] == 1
    assert metrics["boundary"]["top_tools"][0]["tool_name"] == "web_fetch"
    assert metrics["secrets"]["total"] == 2
    assert metrics["secrets"]["granted"] == 1
    assert metrics["secrets"]["missing"] == 1
    assert metrics["secrets"]["config_sources"] == 1
    assert metrics["secrets"]["env_sources"] == 0


@pytest.mark.asyncio
async def test_set_workflow_feedback_updates_evaluation_stats(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit workflow feedback should update the derived evaluation row."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=100,
        execution_ms=200,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )

    evaluations = await audit.get_recent_workflow_evaluations(limit=1)
    updated = await audit.set_workflow_feedback(
        evaluations[0]["workflow_run_id"],
        "positive",
    )

    assert updated["feedback_signal"] == "positive"

    stats = await audit.get_workflow_evaluation_stats_today()
    assert stats["positive_feedback"] == 1
    assert stats["neutral_feedback"] == 0
    assert stats["negative_feedback"] == 0


@pytest.mark.asyncio
async def test_set_latest_workflow_feedback_uses_latest_session_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session-scoped feedback should update the latest workflow run only."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")

    await audit.log_workflow_run(
        session_id="feishu:oc_chat_1:ou_user_1",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="first",
        status="success",
        failure_reason="",
        total_tokens=100,
        execution_ms=200,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )
    await audit.log_workflow_run(
        session_id="feishu:oc_chat_1:ou_user_1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="second",
        status="success",
        failure_reason="",
        total_tokens=220,
        execution_ms=360,
        llm_calls=2,
        tool_calls=1,
        final_model="gpt-5.2",
        call_chain=[{"type": "llm", "model": "gpt-5.2", "status": "success"}],
    )

    updated = await audit.set_latest_workflow_feedback(
        "feishu:oc_chat_1:ou_user_1",
        "negative",
    )
    evaluations = await audit.get_recent_workflow_evaluations(limit=5)

    assert updated["workflow_name"] == "grounded_current_info"
    assert updated["feedback_signal"] == "negative"
    assert evaluations[0]["workflow_name"] == "grounded_current_info"
    assert evaluations[0]["feedback_signal"] == "negative"
    assert evaluations[1]["workflow_name"] == "default_chat_loop"
    assert evaluations[1]["feedback_signal"] == "unknown"


@pytest.mark.asyncio
async def test_workflow_recommendations_aggregate_by_workflow_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workflow recommendations should aggregate runs by workflow name."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest news",
        status="degraded",
        failure_reason="provider_timeout",
        total_tokens=2200,
        execution_ms=7200,
        llm_calls=3,
        tool_calls=2,
        final_model="gpt-5.2",
        call_chain=[
            {"type": "llm", "model": "gpt-5.2", "status": "success"},
            {"type": "tool", "name": "web_search", "status": "timeout"},
        ],
    )
    await audit.log_workflow_run(
        session_id="s2",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest market news",
        status="success",
        failure_reason="",
        total_tokens=2100,
        execution_ms=6400,
        llm_calls=3,
        tool_calls=2,
        final_model="gpt-5.2",
        call_chain=[
            {"type": "llm", "model": "gpt-5.2", "status": "success"},
            {"type": "tool", "name": "web_search", "status": "success"},
        ],
    )
    evaluations = await audit.get_recent_workflow_evaluations(limit=2)
    await audit.set_workflow_feedback(evaluations[0]["workflow_run_id"], "negative")
    await audit.set_workflow_feedback(evaluations[1]["workflow_run_id"], "negative")

    recommendations = await audit.get_workflow_recommendations(days=7, limit=5)

    assert recommendations[0]["workflow_name"] == "grounded_current_info"
    assert recommendations[0]["run_count"] == 2
    assert recommendations[0]["negative_feedback"] == 2
    assert recommendations[0]["recommendation_status"] == "attention"
    assert recommendations[0]["recommendations"]
    assert recommendations[0]["top_attention_reason"]
    assert recommendations[0]["top_follow_up_action"]


@pytest.mark.asyncio
async def test_workflow_evaluation_v2_surfaces_structured_failure_profile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workflow evaluations should expose structured failure classes and follow-up actions."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI model launches",
        status="degraded",
        failure_reason="provider_timeout",
        total_tokens=2300,
        execution_ms=8100,
        llm_calls=4,
        tool_calls=2,
        final_model="gpt-5.2",
        call_chain=[
            {"type": "llm", "model": "gpt-5.2", "status": "success"},
            {"type": "tool", "name": "web_search", "status": "timeout"},
            {"type": "workflow_role_recovery", "status": "resumed", "reason": "provider_timeout"},
        ],
    )

    evaluations = await audit.get_recent_workflow_evaluations(limit=1)

    assert len(evaluations) == 1
    item = evaluations[0]
    assert item["evaluation_label"] in {"review", "poor"}
    assert "workflow_degraded" in item["failure_classes"]
    assert "provider_timeout" in item["failure_classes"]
    assert "latency_pressure" in item["failure_classes"]
    assert item["attention_reasons"][0]
    assert item["follow_up_actions"][0]


@pytest.mark.asyncio
async def test_provider_usage_quota_tracking(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider usage should persist counters and enforce max calls."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")

    first = await audit.consume_provider_call("serper", 2)
    second = await audit.consume_provider_call("serper", 2)
    third = await audit.consume_provider_call("serper", 2)
    usage = await audit.get_provider_usage("serper", 2)

    assert first == {
        "allowed": True,
        "used_calls": 1,
        "remaining_calls": 1,
        "max_calls": 2,
    }
    assert second == {
        "allowed": True,
        "used_calls": 2,
        "remaining_calls": 0,
        "max_calls": 2,
    }
    assert third == {
        "allowed": False,
        "used_calls": 2,
        "remaining_calls": 0,
        "max_calls": 2,
    }
    assert usage == {
        "used_calls": 2,
        "remaining_calls": 0,
        "max_calls": 2,
    }


@pytest.mark.asyncio
async def test_task_replay_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Task replay should aggregate task rows, steps, task runs, tool traces, and workflows."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    db_path = tmp_path / "audit.db"
    audit = AuditLog(db_path)
    store = TaskStore(db_path)

    task = await store.create_task("Replay me", source="spawn_task", session_id="telegram:1")
    await store.claim_next_task(source="spawn_task", worker_id="worker-a")
    await store.start_task_step(
        task["task_id"],
        "agent_run",
        step_name="agent_run",
        input_payload={"task_description": "Replay me"},
        is_checkpoint=True,
    )
    await store.complete_task_step(
        task["task_id"],
        "agent_run",
        output_payload={"result_text": "done"},
    )
    await audit.log_task_run(
        task_id=task["task_id"],
        session_id=f"task:{task['task_id']}",
        attempt_number=1,
        worker_id="worker-a",
        status="success",
        final_output_summary="done",
        execution_ms=123,
    )
    await audit.log_tool_trace(
        task_id=task["task_id"],
        session_id=f"task:{task['task_id']}",
        step_id="agent_run",
        attempt_number=1,
        tool_name="web_search",
        input_summary="api_key=secret-value",
        output_summary="Bearer abcdefg",
        status="success",
        execution_ms=45,
        cached=False,
    )
    await audit.log_workflow_run(
        session_id=f"task:{task['task_id']}",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="Replay me",
        status="success",
        failure_reason="",
        total_tokens=20,
        execution_ms=100,
        llm_calls=1,
        tool_calls=1,
        final_model="gpt-5.2",
        call_chain=[{"type": "tool", "name": "web_search", "status": "success"}],
    )
    await audit.log(
        action_type="secret_access",
        tool_name="web_search",
        input_summary=(
            "capability=web_search.serper_api_key "
            "source=config:tools.webSearch.serperApiKey"
        ),
        output_summary="policy=tool_secret_broker decision=granted",
        status="success",
        session_id=f"task:{task['task_id']}",
    )
    await audit.log(
        action_type="boundary_decision",
        tool_name="web_fetch",
        input_summary=(
            "operation=web_fetch boundary=outbound_url action=fetch "
            "target=https://example.com/article"
        ),
        output_summary="policy=shared_tool_boundary decision=blocked reason=host denied",
        status="blocked",
        session_id=f"task:{task['task_id']}",
    )

    replay = await audit.get_task_replay(task["task_id"])

    assert replay is not None
    assert replay["task"]["task_id"] == task["task_id"]
    assert replay["steps"][0]["step_id"] == "agent_run"
    assert replay["task_runs"][0]["attempt_number"] == 1
    assert replay["tool_traces"][0]["tool_name"] == "web_search"
    assert replay["tool_traces"][0]["input_summary"] == "api_key=[redacted]"
    assert replay["tool_traces"][0]["output_summary"] == "Bearer [redacted]"
    assert replay["workflow_runs"][0]["workflow_name"] == "default_chat_loop"
    assert [item["action_type"] for item in replay["audit_events"]] == [
        "secret_access",
        "boundary_decision",
    ]
