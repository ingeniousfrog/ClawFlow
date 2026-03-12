"""Feishu channel unit tests."""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from types import SimpleNamespace

import pytest

from nanoclaw.channels.feishu import FeishuChannel, PendingConfirmation
from nanoclaw.security.audit import AuditLog


class DummyGateway:
    """Minimal gateway stub used by channel tests."""

    def __init__(self) -> None:
        """Initialize gateway state used by schedule template tests."""
        self.scheduler = DummyScheduler()

    async def handle_incoming(self, **kwargs: object) -> str:
        """Return fixed response."""
        return "ok"


class DummyScheduler:
    """Small in-memory scheduler stub for Feishu command tests."""

    def __init__(self) -> None:
        """Initialize empty job storage."""
        self._next_id = 1
        self.jobs: list[dict[str, object]] = []

    async def add_job(
        self,
        name: str,
        message: str,
        cron_expr: str | None = None,
        interval_seconds: int | None = None,
        channel: str = "telegram",
        target_id: str = "",
        quiet_start: str = "",
        quiet_end: str = "",
    ) -> int:
        """Persist one fake cron job and return its id."""
        job_id = self._next_id
        self._next_id += 1
        self.jobs.append(
            {
                "id": job_id,
                "name": name,
                "message": message,
                "cron_expr": cron_expr or "",
                "interval_seconds": interval_seconds or 0,
                "channel": channel,
                "target_id": target_id,
                "quiet_start": quiet_start,
                "quiet_end": quiet_end,
                "last_run": "",
                "enabled": 1,
                "created_at": datetime(2026, 3, 8, 9, 0).isoformat(sep=" "),
            }
        )
        return job_id

    async def list_jobs(self) -> list[dict[str, object]]:
        """Return the stored fake cron jobs."""
        return list(self.jobs)

    async def list_jobs_with_runtime_state(self) -> list[dict[str, object]]:
        """Return fake cron jobs with optional runtime metadata."""
        return list(self.jobs)

    async def remove_job(self, job_id: int) -> None:
        """Delete one fake cron job."""
        self.jobs = [job for job in self.jobs if int(job["id"]) != job_id]

    async def update_job(
        self,
        job_id: int,
        *,
        name: str,
        message: str,
        cron_expr: str | None = None,
        interval_seconds: int | None = None,
        channel: str = "telegram",
        target_id: str = "",
        quiet_start: str = "",
        quiet_end: str = "",
    ) -> None:
        """Update one fake cron job in place."""
        for job in self.jobs:
            if int(job["id"]) != job_id:
                continue
            job["name"] = name
            job["message"] = message
            job["cron_expr"] = cron_expr or ""
            job["interval_seconds"] = interval_seconds or 0
            job["channel"] = channel
            job["target_id"] = target_id
            job["quiet_start"] = quiet_start
            job["quiet_end"] = quiet_end
            return

    async def toggle_job(self, job_id: int, enabled: bool) -> None:
        """Toggle one fake cron job on or off."""
        for job in self.jobs:
            if int(job["id"]) != job_id:
                continue
            job["enabled"] = 1 if enabled else 0
            return


def _build_channel(allow_from: list[str] | None = None) -> FeishuChannel:
    """Create channel instance with test config."""
    config = SimpleNamespace(
        enabled=True,
        app_id="cli_test",
        app_secret="secret_test",
        verify_token="",
        encrypt_key="",
        webhook_host="127.0.0.1",
        webhook_port=15097,
        webhook_path="/feishu/events",
        allow_from=allow_from or [],
        default_chat_id="",
    )
    return FeishuChannel(config=config, gateway=DummyGateway())


def test_is_allowed_sender_with_whitelist() -> None:
    """Allow list should match open_id/user_id/union_id fields."""
    channel = _build_channel(allow_from=["ou_1", "u_2", "un_3"])

    assert channel._is_allowed_sender({"sender_id": {"open_id": "ou_1"}}) is True
    assert channel._is_allowed_sender({"sender_id": {"user_id": "u_2"}}) is True
    assert channel._is_allowed_sender({"sender_id": {"union_id": "un_3"}}) is True
    assert channel._is_allowed_sender({"sender_id": {"open_id": "ou_x"}}) is False


def test_paper_template_passthrough_for_normal_text() -> None:
    """Non-command text should pass through unchanged."""
    channel = _build_channel()
    mapped, reply = channel._apply_common_templates("hello there")
    assert mapped == "hello there"
    assert reply == ""


def test_paper_template_builds_prompt_with_arguments() -> None:
    """`/paper` command should map to a strict paper_search prompt."""
    channel = _build_channel()
    mapped, reply = channel._apply_common_templates(
        "/paper video generation acceleration --days 7 --max 6 "
        "--providers arxiv,openalex --sort impact"
    )

    assert reply == ""
    assert "paper_search" in mapped
    assert '"topic": "video generation acceleration"' in mapped
    assert '"window_days": 7' in mapped
    assert '"max_items": 6' in mapped
    assert '"providers": "arxiv,openalex"' in mapped
    assert '"sort_by": "impact"' in mapped


def test_paper_template_returns_usage_on_missing_topic() -> None:
    """`/paper` without topic should return usage text."""
    channel = _build_channel()
    mapped, reply = channel._apply_common_templates("/paper")
    assert mapped == ""
    assert "Usage:" in reply
    assert "/paper <topic>" in reply


def test_paper_template_rejects_unknown_option() -> None:
    """Unknown `/paper` option should return a clear error."""
    channel = _build_channel()
    mapped, reply = channel._apply_common_templates("/paper llm systems --foo bar")
    assert mapped == ""
    assert "Unknown /paper option" in reply


def test_paper_template_rejects_invalid_range() -> None:
    """Out-of-range values should be rejected before hitting the LLM."""
    channel = _build_channel()
    mapped, reply = channel._apply_common_templates("/paper llm systems --max 30")
    assert mapped == ""
    assert "Invalid --max" in reply


@pytest.mark.asyncio
async def test_feedback_command_updates_latest_workflow_for_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/feedback` should update the latest workflow evaluation for the current chat."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="feishu:oc_chat_1:ou_user_1",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="show latest AI news",
        status="success",
        failure_reason="",
        total_tokens=180,
        execution_ms=420,
        llm_calls=2,
        tool_calls=1,
        final_model="gpt-5.2",
        call_chain=[{"type": "llm", "model": "gpt-5.2", "status": "success"}],
    )

    channel = _build_channel()
    reply = await channel._handle_feedback_command("/feedback 差评", "oc_chat_1", "ou_user_1")
    evaluations = await audit.get_recent_workflow_evaluations(limit=1)

    assert "Recorded `negative` feedback" in reply
    assert evaluations[0]["feedback_signal"] == "negative"


@pytest.mark.asyncio
async def test_feedback_shortcut_updates_latest_workflow_for_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """High-confidence natural feedback should reuse the same feedback path."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="feishu:oc_chat_1:ou_user_1",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="summarize this topic",
        status="success",
        failure_reason="",
        total_tokens=140,
        execution_ms=350,
        llm_calls=2,
        tool_calls=1,
        final_model="gpt-5.2",
        call_chain=[{"type": "llm", "model": "gpt-5.2", "status": "success"}],
    )

    channel = _build_channel()
    reply = await channel._handle_feedback_command("这个回答不错", "oc_chat_1", "ou_user_1")
    evaluations = await audit.get_recent_workflow_evaluations(limit=1)

    assert "Recorded `positive` feedback" in reply
    assert evaluations[0]["feedback_signal"] == "positive"


@pytest.mark.asyncio
async def test_contextual_feedback_shortcut_updates_latest_workflow_for_chat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Session-aware feedback shortcut should reuse the latest-chat feedback path."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="feishu:oc_chat_1:ou_user_1",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="summarize this topic",
        status="success",
        failure_reason="",
        total_tokens=140,
        execution_ms=350,
        llm_calls=2,
        tool_calls=1,
        final_model="gpt-5.2",
        call_chain=[{"type": "llm", "model": "gpt-5.2", "status": "success"}],
    )

    channel = _build_channel()
    reply = await channel._handle_feedback_command(
        "给刚才那条工作流好评",
        "oc_chat_1",
        "ou_user_1",
    )
    evaluations = await audit.get_recent_workflow_evaluations(limit=1)

    assert "Recorded `positive` feedback" in reply
    assert evaluations[0]["feedback_signal"] == "positive"


@pytest.mark.asyncio
async def test_feedback_command_requires_recent_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/feedback` should return a clear message when no workflow exists yet."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    channel = _build_channel()
    reply = await channel._handle_feedback_command("/feedback positive", "oc_chat_1", "ou_user_1")

    assert "No recent workflow run was found in this chat yet." in reply


@pytest.mark.asyncio
async def test_contextual_feedback_shortcut_requires_recent_workflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contextual feedback shortcut should fail clearly when the chat has no run yet."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    channel = _build_channel()
    reply = await channel._handle_feedback_command(
        "刚才那条给个差评",
        "oc_chat_1",
        "ou_user_1",
    )

    assert "No recent workflow run was found in this chat yet." in reply


def test_feedback_shortcut_does_not_consume_regular_request() -> None:
    """Regular user requests should not be mistaken for feedback shortcuts."""
    channel = _build_channel()
    reply = asyncio.run(
        channel._handle_feedback_command("帮我继续查一下这个话题", "oc_chat_1", "ou_user_1")
    )

    assert reply == ""


@pytest.mark.asyncio
async def test_workflow_report_command_returns_recommendations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/workflow report` should show aggregated workflow recommendations."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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
    evaluations = await audit.get_recent_workflow_evaluations(limit=1)
    await audit.set_workflow_feedback(evaluations[0]["workflow_run_id"], "negative")

    channel = _build_channel()
    reply = await channel._handle_workflow_command("/workflow report --days 7 --limit 3")

    assert "Workflow recommendations (7d):" in reply
    assert "grounded_current_info [attention]" in reply
    assert "feedback=0/0/1" in reply


@pytest.mark.asyncio
async def test_workflow_report_shortcut_returns_recommendations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Natural-language workflow shortcut should map to the same report path."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=120,
        execution_ms=220,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )

    channel = _build_channel()
    reply = await channel._handle_workflow_command("看看最近工作流建议")

    assert "Workflow recommendations (7d):" in reply
    assert "default_chat_loop" in reply


@pytest.mark.asyncio
async def test_workflow_report_command_filters_by_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/workflow report --status ...` should keep only matching recommendation rows."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=120,
        execution_ms=220,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )

    channel = _build_channel()
    reply = await channel._handle_workflow_command(
        "/workflow report --status attention --limit 5"
    )

    assert "Workflow recommendations (7d, status=attention):" in reply
    assert "grounded_current_info [attention]" in reply
    assert "default_chat_loop" not in reply


@pytest.mark.asyncio
async def test_workflow_report_shortcut_filters_by_attention_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attention shortcut should map to the filtered workflow report path."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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

    channel = _build_channel()
    reply = await channel._handle_workflow_command("看看需要关注的工作流建议")

    assert "status=attention" in reply
    assert "grounded_current_info [attention]" in reply


@pytest.mark.asyncio
async def test_workflow_report_command_filters_by_feedback_signal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/workflow report --feedback ...` should keep only rows with that feedback signal."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=120,
        execution_ms=220,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )
    evaluations = await audit.get_recent_workflow_evaluations(limit=2)
    await audit.set_workflow_feedback(evaluations[0]["workflow_run_id"], "positive")
    await audit.set_workflow_feedback(evaluations[1]["workflow_run_id"], "negative")

    channel = _build_channel()
    reply = await channel._handle_workflow_command(
        "/workflow report --feedback negative --limit 5"
    )

    assert "Workflow recommendations (7d, feedback=negative):" in reply
    assert "grounded_current_info [attention]" in reply
    assert "default_chat_loop" not in reply


@pytest.mark.asyncio
async def test_workflow_report_shortcut_filters_by_negative_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Negative-feedback shortcut should map to the filtered workflow report path."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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
    evaluations = await audit.get_recent_workflow_evaluations(limit=1)
    await audit.set_workflow_feedback(evaluations[0]["workflow_run_id"], "negative")

    channel = _build_channel()
    reply = await channel._handle_workflow_command("看看负反馈多的工作流建议")

    assert "feedback=negative" in reply
    assert "grounded_current_info [attention]" in reply


@pytest.mark.asyncio
async def test_workflow_recent_command_returns_recent_evaluations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/workflow recent` should show recent workflow evaluations."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=120,
        execution_ms=220,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )
    evaluations = await audit.get_recent_workflow_evaluations(limit=1)
    await audit.set_workflow_feedback(evaluations[0]["workflow_run_id"], "positive")

    channel = _build_channel()
    reply = await channel._handle_workflow_command("/workflow recent --limit 5")

    assert "Recent workflow evaluations (limit=5):" in reply
    assert "default_chat_loop [good]" in reply
    assert "feedback=positive" in reply


@pytest.mark.asyncio
async def test_workflow_recent_shortcut_returns_recent_evaluations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Natural-language recent shortcut should map to the recent workflow path."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=120,
        execution_ms=220,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )

    channel = _build_channel()
    reply = await channel._handle_workflow_command("看看最近工作流评估")

    assert "Recent workflow evaluations" in reply
    assert "default_chat_loop" in reply


@pytest.mark.asyncio
async def test_workflow_recent_command_filters_by_label_and_feedback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/workflow recent` should support label and feedback filters."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=120,
        execution_ms=220,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )
    evaluations = await audit.get_recent_workflow_evaluations(limit=2)
    await audit.set_workflow_feedback(evaluations[0]["workflow_run_id"], "positive")
    await audit.set_workflow_feedback(evaluations[1]["workflow_run_id"], "negative")

    channel = _build_channel()
    reply = await channel._handle_workflow_command(
        "/workflow recent --label poor --feedback negative"
    )

    assert "Recent workflow evaluations (limit=5, label=poor, feedback=negative):" in reply
    assert "grounded_current_info [poor]" in reply
    assert "default_chat_loop" not in reply


@pytest.mark.asyncio
async def test_workflow_recent_reference_expand_shortcut_uses_cached_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`把第一条展开` should expand the first cached recent workflow row."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=120,
        execution_ms=220,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )

    channel = _build_channel()
    recent_reply = await channel._handle_workflow_command(
        "/workflow recent --limit 2",
        "oc_chat_1",
        "ou_user_1",
    )
    expand_reply = await channel._handle_workflow_command("把第一条展开", "oc_chat_1", "ou_user_1")

    assert "Recent workflow evaluations (limit=2):" in recent_reply
    assert "Workflow suggestions for run #" in expand_reply
    assert "default_chat_loop [good]" in expand_reply


@pytest.mark.asyncio
async def test_workflow_recent_reference_feedback_shortcut_updates_cached_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`给第二条差评` should update the second cached recent workflow row."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=120,
        execution_ms=220,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )
    recent_items = await audit.get_recent_workflow_evaluations(limit=2)
    target_run_id = int(recent_items[1]["workflow_run_id"])

    channel = _build_channel()
    await channel._handle_workflow_command("/workflow recent --limit 2", "oc_chat_1", "ou_user_1")
    feedback_reply = await channel._handle_workflow_command("给第二条差评", "oc_chat_1", "ou_user_1")
    refreshed = await audit.get_workflow_evaluation(target_run_id)

    assert f"Workflow run #{target_run_id} feedback updated to negative." in feedback_reply
    assert refreshed is not None
    assert refreshed["feedback_signal"] == "negative"


@pytest.mark.asyncio
async def test_workflow_report_reference_expand_shortcut_uses_cached_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`把第一条展开` should expand the first cached workflow report row."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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

    channel = _build_channel()
    report_reply = await channel._handle_workflow_command(
        "/workflow report --days 7 --limit 3",
        "oc_chat_1",
        "ou_user_1",
    )
    expand_reply = await channel._handle_workflow_command("把第一条展开", "oc_chat_1", "ou_user_1")

    assert "Workflow recommendations (7d):" in report_reply
    assert "Workflow recommendation for grounded_current_info:" in expand_reply
    assert "Recommendations:" in expand_reply


@pytest.mark.asyncio
async def test_workflow_report_reference_feedback_shortcut_requires_recent_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinal feedback should refuse aggregated report rows and ask for `/workflow recent`."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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

    channel = _build_channel()
    await channel._handle_workflow_command("/workflow report --days 7 --limit 3", "oc_chat_1", "ou_user_1")
    feedback_reply = await channel._handle_workflow_command("给第一条差评", "oc_chat_1", "ou_user_1")

    assert "The last workflow list in this chat was an aggregated report." in feedback_reply


@pytest.mark.asyncio
async def test_workflow_recent_named_expand_shortcut_uses_cached_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow name should expand the matching cached recent row."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=120,
        execution_ms=220,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )

    channel = _build_channel()
    await channel._handle_workflow_command("/workflow recent --limit 2", "oc_chat_1", "ou_user_1")
    expand_reply = await channel._handle_workflow_command(
        "把grounded_current_info展开",
        "oc_chat_1",
        "ou_user_1",
    )

    assert "Workflow suggestions for run #" in expand_reply
    assert "grounded_current_info [poor]" in expand_reply


@pytest.mark.asyncio
async def test_workflow_recent_named_feedback_shortcut_updates_cached_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow name should update the matching cached recent row feedback."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=120,
        execution_ms=220,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )
    evaluations = await audit.get_recent_workflow_evaluations(limit=2)
    target_run_id = int(evaluations[1]["workflow_run_id"])

    channel = _build_channel()
    await channel._handle_workflow_command("/workflow recent --limit 2", "oc_chat_1", "ou_user_1")
    feedback_reply = await channel._handle_workflow_command(
        "给grounded_current_info差评",
        "oc_chat_1",
        "ou_user_1",
    )
    refreshed = await audit.get_workflow_evaluation(target_run_id)

    assert f"Workflow run #{target_run_id} feedback updated to negative." in feedback_reply
    assert refreshed is not None
    assert refreshed["feedback_signal"] == "negative"


@pytest.mark.asyncio
async def test_workflow_report_named_expand_shortcut_uses_cached_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A workflow name should expand the matching cached aggregated row."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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

    channel = _build_channel()
    await channel._handle_workflow_command("/workflow report --days 7 --limit 3", "oc_chat_1", "ou_user_1")
    expand_reply = await channel._handle_workflow_command(
        "把grounded_current_info展开",
        "oc_chat_1",
        "ou_user_1",
    )

    assert "Workflow recommendation for grounded_current_info:" in expand_reply
    assert "Recommendations:" in expand_reply


@pytest.mark.asyncio
async def test_workflow_named_reference_shortcut_rejects_ambiguous_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short ambiguous name should ask the user to disambiguate."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="paper_search",
        workflow_tags=["paper_search"],
        user_summary="video generation",
        status="success",
        failure_reason="",
        total_tokens=800,
        execution_ms=1200,
        llm_calls=2,
        tool_calls=1,
        final_model="gpt-5.2",
        call_chain=[{"type": "llm", "model": "gpt-5.2", "status": "success"}],
    )
    await audit.log_workflow_run(
        session_id="s2",
        workflow_name="paper_monitor",
        workflow_tags=["paper_monitor"],
        user_summary="video generation monitor",
        status="success",
        failure_reason="",
        total_tokens=820,
        execution_ms=1300,
        llm_calls=2,
        tool_calls=1,
        final_model="gpt-5.2",
        call_chain=[{"type": "llm", "model": "gpt-5.2", "status": "success"}],
    )

    channel = _build_channel()
    await channel._handle_workflow_command("/workflow recent --limit 2", "oc_chat_1", "ou_user_1")
    reply = await channel._handle_workflow_command("把paper展开", "oc_chat_1", "ou_user_1")

    assert "Matched more than one workflow for `paper`:" in reply
    assert "paper_search" in reply
    assert "paper_monitor" in reply


@pytest.mark.asyncio
async def test_workflow_recent_named_expand_then_feedback_shortcut_updates_and_expands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name-based expand-then-feedback phrase should update and expand one recent row."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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

    evaluations = await audit.get_recent_workflow_evaluations(limit=1)
    target_run_id = int(evaluations[0]["workflow_run_id"])

    channel = _build_channel()
    await channel._handle_workflow_command("/workflow recent --limit 1", "oc_chat_1", "ou_user_1")
    reply = await channel._handle_workflow_command(
        "把grounded_current_info展开并给个差评",
        "oc_chat_1",
        "ou_user_1",
    )
    refreshed = await audit.get_workflow_evaluation(target_run_id)

    assert f"Workflow run #{target_run_id} feedback updated to negative." in reply
    assert f"Workflow suggestions for run #{target_run_id}:" in reply
    assert refreshed is not None
    assert refreshed["feedback_signal"] == "negative"


@pytest.mark.asyncio
async def test_workflow_report_named_expand_then_feedback_shortcut_requires_recent_view(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A name-based expand-then-feedback phrase should reject aggregated report rows."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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

    channel = _build_channel()
    await channel._handle_workflow_command("/workflow report --days 7 --limit 3", "oc_chat_1", "ou_user_1")
    reply = await channel._handle_workflow_command(
        "把grounded_current_info展开并给个差评",
        "oc_chat_1",
        "ou_user_1",
    )

    assert "The last workflow list in this chat was an aggregated report." in reply


@pytest.mark.asyncio
async def test_workflow_feedback_command_updates_specific_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/workflow feedback <RUN_ID> <SIGNAL>` should update one specific workflow run."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="first",
        status="success",
        failure_reason="",
        total_tokens=120,
        execution_ms=220,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )
    await audit.log_workflow_run(
        session_id="s2",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="second",
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
    evaluations = await audit.get_recent_workflow_evaluations(limit=2)
    target_run_id = int(evaluations[1]["workflow_run_id"])

    channel = _build_channel()
    reply = await channel._handle_workflow_command(
        f"/workflow feedback {target_run_id} negative"
    )
    refreshed = await audit.get_recent_workflow_evaluations(limit=2)

    assert f"Workflow run #{target_run_id} feedback updated to negative." in reply
    assert refreshed[1]["workflow_run_id"] == target_run_id
    assert refreshed[1]["feedback_signal"] == "negative"
    assert refreshed[0]["feedback_signal"] == "unknown"


@pytest.mark.asyncio
async def test_workflow_suggest_command_expands_one_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/workflow suggest <RUN_ID>` should expand stored suggestions for one run."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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
    evaluations = await audit.get_recent_workflow_evaluations(limit=1)
    target_run_id = int(evaluations[0]["workflow_run_id"])

    channel = _build_channel()
    reply = await channel._handle_workflow_command(f"/workflow suggest {target_run_id}")

    assert f"Workflow suggestions for run #{target_run_id}:" in reply
    assert "grounded_current_info [poor]" in reply
    assert "Suggestions:" in reply
    assert "Review failure_reason before expanding this workflow." in reply


@pytest.mark.asyncio
async def test_workflow_suggest_shortcut_expands_one_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """High-confidence suggest shortcut should rewrite into `/workflow suggest`."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="s1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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
    evaluations = await audit.get_recent_workflow_evaluations(limit=1)
    target_run_id = int(evaluations[0]["workflow_run_id"])

    channel = _build_channel()
    reply = await channel._handle_workflow_command(f"看看run{target_run_id}的建议")

    assert f"Workflow suggestions for run #{target_run_id}:" in reply
    assert "Suggestions:" in reply


@pytest.mark.asyncio
async def test_contextual_workflow_suggest_shortcut_uses_latest_poor_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contextual suggest shortcut should resolve the latest poor run in the chat."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="feishu:oc_chat_1:ou_user_1",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=120,
        execution_ms=220,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )
    await audit.log_workflow_run(
        session_id="feishu:oc_chat_1:ou_user_1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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
    evaluations = await audit.get_recent_workflow_evaluations_for_session(
        "feishu:oc_chat_1:ou_user_1",
        limit=5,
    )
    target_run_id = int(evaluations[0]["workflow_run_id"])

    channel = _build_channel()
    reply = await channel._handle_workflow_command(
        "展开刚才那条差评工作流的建议",
        "oc_chat_1",
        "ou_user_1",
    )

    assert f"Workflow suggestions for run #{target_run_id}:" in reply
    assert "grounded_current_info [poor]" in reply


@pytest.mark.asyncio
async def test_contextual_workflow_feedback_and_suggest_shortcut_updates_and_expands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Combined contextual shortcut should update feedback and expand suggestions."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="feishu:oc_chat_1:ou_user_1",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=120,
        execution_ms=220,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )
    await audit.log_workflow_run(
        session_id="feishu:oc_chat_1:ou_user_1",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="latest AI news",
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
    evaluations = await audit.get_recent_workflow_evaluations_for_session(
        "feishu:oc_chat_1:ou_user_1",
        limit=5,
    )
    target_run_id = int(evaluations[0]["workflow_run_id"])

    channel = _build_channel()
    reply = await channel._handle_workflow_command(
        "对刚才那条差评工作流给个差评并展开建议",
        "oc_chat_1",
        "ou_user_1",
    )
    refreshed = await audit.get_workflow_evaluation(target_run_id)

    assert f"Workflow run #{target_run_id} feedback updated to negative." in reply
    assert f"Workflow suggestions for run #{target_run_id}:" in reply
    assert "Suggestions:" in reply
    assert refreshed is not None
    assert refreshed["feedback_signal"] == "negative"


@pytest.mark.asyncio
async def test_contextual_workflow_feedback_and_suggest_shortcut_requires_match(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Combined contextual shortcut should explain when no matching run exists."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    await audit.log_workflow_run(
        session_id="feishu:oc_chat_1:ou_user_1",
        workflow_name="default_chat_loop",
        workflow_tags=["default_chat_loop"],
        user_summary="hello",
        status="success",
        failure_reason="",
        total_tokens=120,
        execution_ms=220,
        llm_calls=1,
        tool_calls=0,
        final_model="gpt-5-mini",
        call_chain=[{"type": "llm", "model": "gpt-5-mini", "status": "success"}],
    )

    channel = _build_channel()
    reply = await channel._handle_workflow_command(
        "对刚才那条差评工作流给个差评并展开建议",
        "oc_chat_1",
        "ou_user_1",
    )

    assert "No recent poor workflow run was found in this chat yet." in reply


@pytest.mark.asyncio
async def test_contextual_workflow_suggest_shortcut_handles_missing_session_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contextual suggest shortcut should explain when no matching run exists."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    channel = _build_channel()
    reply = await channel._handle_workflow_command(
        "展开刚才那条差评工作流的建议",
        "oc_chat_1",
        "ou_user_1",
    )

    assert "No recent workflow run was found in this chat yet." in reply


@pytest.mark.asyncio
async def test_workflow_suggest_command_handles_missing_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`/workflow suggest <RUN_ID>` should reject unknown run ids."""
    monkeypatch.setattr("nanoclaw.security.audit._get_hmac_key", lambda: b"k" * 32)
    audit = AuditLog(tmp_path / "audit.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    channel = _build_channel()
    reply = await channel._handle_workflow_command("/workflow suggest 999")

    assert reply == "Workflow run #999 was not found."


def test_url_verification_rejects_invalid_verify_token() -> None:
    """Webhook URL verification should reject a mismatched token."""

    class FakeRequest:
        async def json(self) -> dict[str, str]:
            return {
                "type": "url_verification",
                "token": "wrong-token",
                "challenge": "abc",
            }

    async def _run() -> None:
        channel = _build_channel()
        channel.config.verify_token = "expected-token"
        response = await channel._handle_event(FakeRequest())  # type: ignore[arg-type]
        assert response.status == 403
        assert json.loads(response.text)["msg"] == "invalid token"

    asyncio.run(_run())


def test_event_rejects_invalid_header_token() -> None:
    """Event callback should reject a mismatched header token."""

    class FakeRequest:
        async def json(self) -> dict[str, object]:
            return {
                "header": {
                    "event_id": "evt_1",
                    "event_type": "im.message.receive_v1",
                    "token": "wrong-token",
                },
                "event": {},
            }

    async def _run() -> None:
        channel = _build_channel()
        channel.config.verify_token = "expected-token"
        response = await channel._handle_event(FakeRequest())  # type: ignore[arg-type]
        assert response.status == 403
        assert json.loads(response.text)["msg"] == "invalid token"

    asyncio.run(_run())


def test_confirmation_reply_approves_pending_request() -> None:
    """A matching `yes <id>` reply should resolve pending confirmation to True."""
    async def _run() -> None:
        channel = _build_channel()
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        confirm_id = "a1b2c3d4"
        channel._pending_confirmations[confirm_id] = PendingConfirmation(
            user_id="ou_test",
            chat_id="",
            future=future,
            created_at=time.time(),
        )

        consumed = await channel._try_consume_confirmation_reply(
            "",
            "ou_test",
            f"yes {confirm_id}",
        )

        assert consumed is True
        assert future.done() is True
        assert future.result() is True
        assert confirm_id not in channel._pending_confirmations

    asyncio.run(_run())


def test_confirmation_reply_rejects_wrong_user() -> None:
    """Reply from a different user should not resolve the pending request."""
    async def _run() -> None:
        channel = _build_channel()
        future: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        confirm_id = "b1c2d3e4"
        channel._pending_confirmations[confirm_id] = PendingConfirmation(
            user_id="ou_owner",
            chat_id="",
            future=future,
            created_at=time.time(),
        )

        consumed = await channel._try_consume_confirmation_reply(
            "",
            "ou_other",
            f"yes {confirm_id}",
        )

        assert consumed is True
        assert future.done() is False
        assert confirm_id in channel._pending_confirmations

    asyncio.run(_run())


@pytest.mark.asyncio
async def test_schedule_daily_command_creates_chat_scoped_job() -> None:
    """`/schedule daily` should create a Feishu cron job for the current chat."""
    channel = _build_channel()
    reply = await channel._handle_schedule_command(
        "/schedule daily 08:30 AI trends --channels ai,tech --max 6",
        "oc_chat_1",
    )

    jobs = await channel.gateway.scheduler.list_jobs()
    assert "Created Feishu schedule #1." in reply
    assert "Schedule: every day at 08:30" in reply
    assert "Quiet window: off" in reply
    assert len(jobs) == 1
    assert jobs[0]["channel"] == "feishu"
    assert jobs[0]["target_id"] == "oc_chat_1"
    assert jobs[0]["cron_expr"] == "30 8 * * *"
    assert "daily_digest" in str(jobs[0]["message"])
    assert '"channels": "ai,tech"' in str(jobs[0]["message"])


@pytest.mark.asyncio
async def test_schedule_command_persists_quiet_window() -> None:
    """`--mute` should persist one quiet window alongside the cron schedule."""
    channel = _build_channel()
    reply = await channel._handle_schedule_command(
        "/schedule daily 08:30 AI trends --workdays --mute 22:00-08:00",
        "oc_chat_1",
    )

    jobs = await channel.gateway.scheduler.list_jobs()
    assert "Quiet window: 22:00-08:00" in reply
    assert jobs[0]["quiet_start"] == "22:00"
    assert jobs[0]["quiet_end"] == "08:00"


@pytest.mark.asyncio
async def test_schedule_workdays_command_creates_chat_scoped_job() -> None:
    """`--workdays` should change the cron recurrence without changing workflow kind."""
    channel = _build_channel()
    reply = await channel._handle_schedule_command(
        "/schedule daily 08:30 AI trends --workdays --max 6",
        "oc_chat_1",
    )

    jobs = await channel.gateway.scheduler.list_jobs()
    assert "Schedule: every workday at 08:30" in reply
    assert jobs[0]["cron_expr"] == "30 8 * * 1-5"
    assert jobs[0]["name"] == "Feishu daily digest @ every workday at 08:30: AI trends"


@pytest.mark.asyncio
async def test_schedule_weekly_command_creates_chat_scoped_job() -> None:
    """`--weekly` should create a weekly cron expression for the chosen weekday."""
    channel = _build_channel()
    reply = await channel._handle_schedule_command(
        "/schedule hotspot 09:00 robotics --weekly fri --max 5",
        "oc_chat_1",
    )

    jobs = await channel.gateway.scheduler.list_jobs()
    assert "Schedule: every Friday at 09:00" in reply
    assert jobs[0]["cron_expr"] == "0 9 * * 5"
    assert jobs[0]["name"] == "Feishu hotspot brief @ every Friday at 09:00: robotics"


@pytest.mark.asyncio
async def test_schedule_list_only_shows_current_chat_jobs() -> None:
    """`/schedule list` should only show jobs belonging to the active chat."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Chat one digest",
        "daily one",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Chat two digest",
        "daily two",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_2",
    )
    await channel.gateway.scheduler.add_job(
        "Telegram digest",
        "daily three",
        cron_expr="0 10 * * *",
        channel="telegram",
        target_id="",
    )

    reply = await channel._handle_schedule_command("/schedule list", "oc_chat_1")

    assert "Use the `#ID` at the start of each line" in reply
    assert "#1 (Chat one digest)" in reply
    assert "Chat one digest" in reply
    assert "Chat two digest" not in reply
    assert "Telegram digest" not in reply


@pytest.mark.asyncio
async def test_schedule_list_supports_health_filter() -> None:
    """`/schedule list attention` should only show matching schedule health."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Attention digest",
        "daily one",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Healthy digest",
        "daily two",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    channel.gateway.scheduler.jobs[0]["runtime"] = {"health": "attention"}
    channel.gateway.scheduler.jobs[1]["runtime"] = {"health": "healthy"}

    reply = await channel._handle_schedule_command("/schedule list attention", "oc_chat_1")

    assert "health=attention" in reply
    assert "Attention digest" in reply
    assert "Healthy digest" not in reply


@pytest.mark.asyncio
async def test_schedule_list_supports_signal_filter() -> None:
    """`/schedule list signal recovery` should only show matching recent signals."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Recovered digest",
        "daily one",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Alerting digest",
        "daily two",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    channel.gateway.scheduler.jobs[0]["runtime"] = {
        "health": "healthy",
        "signal_timeline": [
            {"label": "recovery", "detail": "healthy after attention_initial"}
        ],
    }
    channel.gateway.scheduler.jobs[1]["runtime"] = {
        "health": "attention",
        "signal_timeline": [
            {"label": "alert", "detail": "attention_initial x1"}
        ],
    }

    reply = await channel._handle_schedule_command(
        "/schedule list signal recovery",
        "oc_chat_1",
    )

    assert "signal=recovery" in reply
    assert "Recovered digest" in reply
    assert "Alerting digest" not in reply


@pytest.mark.asyncio
async def test_schedule_show_returns_job_details() -> None:
    """`/schedule show` should display one owned job with quiet-window metadata."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Chat one digest",
        "daily one",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
        quiet_start="22:00",
        quiet_end="08:00",
    )
    channel.gateway.scheduler.jobs[0]["runtime"] = {
        "health": "retrying",
        "health_reason": "delivery retry pending",
        "notify_kind": "cron_delivery_retry_scheduled",
        "last_execution": {
            "status": "succeeded",
            "updated_at": "2026-03-08 08:30:01",
        },
        "last_delivery_retry": {
            "status": "running",
            "updated_at": "2026-03-08 08:31:15",
        },
        "signal_timeline": [
            {
                "label": "alert",
                "detail": "retrying_initial x2",
                "timestamp": "2026-03-08 08:31:15",
            },
            {
                "label": "recovery",
                "detail": "healthy after retrying_initial",
                "timestamp": "2026-03-08 09:00:00",
            },
        ],
    }

    reply = await channel._handle_schedule_command("/schedule show 1", "oc_chat_1")

    assert "Feishu schedule #1" in reply
    assert "Name: Chat one digest" in reply
    assert "Quiet window: 22:00-08:00" in reply
    assert "Health: retrying" in reply
    assert "Last notify mode: cron_delivery_retry_scheduled" in reply
    assert "Last delivery retry: running" in reply
    assert "Recent schedule signals:" in reply
    assert "- alert: retrying_initial x2 @ 2026-03-08 08:31:15" in reply


@pytest.mark.asyncio
async def test_schedule_remove_requires_same_chat_owner() -> None:
    """`/schedule remove` should refuse jobs owned by another Feishu chat."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Foreign digest",
        "daily foreign",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_2",
    )
    await channel.gateway.scheduler.add_job(
        "Local digest",
        "daily local",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )

    denied = await channel._handle_schedule_command("/schedule remove 1", "oc_chat_1")
    removed = await channel._handle_schedule_command("/schedule remove 2", "oc_chat_1")
    jobs = await channel.gateway.scheduler.list_jobs()

    assert "does not belong to this chat" in denied
    assert removed == "Removed Feishu schedule #2 (Local digest)."
    assert len(jobs) == 1
    assert jobs[0]["name"] == "Foreign digest"


@pytest.mark.asyncio
async def test_schedule_remove_matching_filters_owned_jobs_by_health() -> None:
    """Batch remove should only delete current-chat jobs matching one health filter."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Muted digest",
        "daily one",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Healthy digest",
        "daily two",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Foreign muted digest",
        "daily three",
        cron_expr="0 10 * * *",
        channel="feishu",
        target_id="oc_chat_2",
    )
    channel.gateway.scheduler.jobs[0]["runtime"] = {"health": "muted"}
    channel.gateway.scheduler.jobs[1]["runtime"] = {"health": "healthy"}
    channel.gateway.scheduler.jobs[2]["runtime"] = {"health": "muted"}

    reply = await channel._handle_schedule_command("/schedule remove muted", "oc_chat_1")
    jobs = await channel.gateway.scheduler.list_jobs()

    assert "Removed 1 Feishu schedule in this chat (health=muted):" in reply
    assert "- #1 (Muted digest)" in reply
    assert [str(job["name"]) for job in jobs] == ["Healthy digest", "Foreign muted digest"]


@pytest.mark.asyncio
async def test_schedule_remove_matching_filters_owned_jobs_by_signal() -> None:
    """Batch remove should respect recent signal filters and current chat ownership."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Recovered digest",
        "daily one",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Alerting digest",
        "daily two",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Foreign recovered digest",
        "daily three",
        cron_expr="0 10 * * *",
        channel="feishu",
        target_id="oc_chat_2",
    )
    channel.gateway.scheduler.jobs[0]["runtime"] = {
        "health": "healthy",
        "signal_timeline": [{"label": "recovery", "detail": "healthy again"}],
    }
    channel.gateway.scheduler.jobs[1]["runtime"] = {
        "health": "attention",
        "signal_timeline": [{"label": "alert", "detail": "attention_initial x1"}],
    }
    channel.gateway.scheduler.jobs[2]["runtime"] = {
        "health": "healthy",
        "signal_timeline": [{"label": "recovery", "detail": "healthy again"}],
    }

    reply = await channel._handle_schedule_command(
        "/schedule remove signal recovery",
        "oc_chat_1",
    )
    jobs = await channel.gateway.scheduler.list_jobs()

    assert "Removed 1 Feishu schedule in this chat (signal=recovery):" in reply
    assert "- #1 (Recovered digest)" in reply
    assert [str(job["name"]) for job in jobs] == ["Alerting digest", "Foreign recovered digest"]


@pytest.mark.asyncio
async def test_schedule_update_rewrites_existing_job() -> None:
    """`/schedule update` should modify an owned Feishu job in place."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Old digest",
        "old payload",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )

    reply = await channel._handle_schedule_command(
        "/schedule update 1 daily 09:15 AI infra --workdays --max 6",
        "oc_chat_1",
    )

    jobs = await channel.gateway.scheduler.list_jobs()
    assert "Updated Feishu schedule #1." in reply
    assert "Name: Feishu daily digest @ every workday at 09:15: AI infra" in reply
    assert "Tip: run `/schedule list` to see the current `#ID -> task name` mapping." in reply
    assert len(jobs) == 1
    assert jobs[0]["cron_expr"] == "15 9 * * 1-5"
    assert jobs[0]["name"] == "Feishu daily digest @ every workday at 09:15: AI infra"
    assert '"max_items": 6' in str(jobs[0]["message"])


@pytest.mark.asyncio
async def test_schedule_pause_and_resume_toggle_enabled_state() -> None:
    """Pause and resume should flip one owned Feishu job on and off."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Local digest",
        "daily local",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )

    paused = await channel._handle_schedule_command("/schedule pause 1", "oc_chat_1")
    resumed = await channel._handle_schedule_command("/schedule resume 1", "oc_chat_1")
    jobs = await channel.gateway.scheduler.list_jobs()

    assert paused == "Paused Feishu schedule #1 (Local digest)."
    assert resumed == "Enabled Feishu schedule #1 (Local digest)."
    assert jobs[0]["enabled"] == 1


@pytest.mark.asyncio
async def test_schedule_pause_matching_filters_owned_jobs_by_health() -> None:
    """Batch pause should only affect current-chat jobs matching one health filter."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Attention digest",
        "daily one",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Healthy digest",
        "daily two",
        cron_expr="0 10 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Foreign attention digest",
        "daily three",
        cron_expr="0 11 * * *",
        channel="feishu",
        target_id="oc_chat_2",
    )
    channel.gateway.scheduler.jobs[0]["runtime"] = {"health": "attention"}
    channel.gateway.scheduler.jobs[1]["runtime"] = {"health": "healthy"}
    channel.gateway.scheduler.jobs[2]["runtime"] = {"health": "attention"}

    reply = await channel._handle_schedule_command("/schedule pause attention", "oc_chat_1")
    jobs = await channel.gateway.scheduler.list_jobs()

    assert "Paused 1 Feishu schedule in this chat (health=attention):" in reply
    assert "- #1 (Attention digest)" in reply
    assert jobs[0]["enabled"] == 0
    assert jobs[1]["enabled"] == 1
    assert jobs[2]["enabled"] == 1


@pytest.mark.asyncio
async def test_schedule_resume_matching_filters_owned_jobs_by_signal() -> None:
    """Batch resume should only affect current-chat jobs matching one recent signal."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Recovered digest",
        "daily one",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Alerting digest",
        "daily two",
        cron_expr="0 10 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Foreign recovered digest",
        "daily three",
        cron_expr="0 11 * * *",
        channel="feishu",
        target_id="oc_chat_2",
    )
    channel.gateway.scheduler.jobs[0]["enabled"] = 0
    channel.gateway.scheduler.jobs[1]["enabled"] = 0
    channel.gateway.scheduler.jobs[2]["enabled"] = 0
    channel.gateway.scheduler.jobs[0]["runtime"] = {
        "health": "healthy",
        "signal_timeline": [{"label": "recovery", "detail": "healthy again"}],
    }
    channel.gateway.scheduler.jobs[1]["runtime"] = {
        "health": "attention",
        "signal_timeline": [{"label": "alert", "detail": "attention_initial x1"}],
    }
    channel.gateway.scheduler.jobs[2]["runtime"] = {
        "health": "healthy",
        "signal_timeline": [{"label": "recovery", "detail": "healthy again"}],
    }

    reply = await channel._handle_schedule_command(
        "/schedule resume signal recovery",
        "oc_chat_1",
    )
    jobs = await channel.gateway.scheduler.list_jobs()

    assert "Enabled 1 Feishu schedule in this chat (signal=recovery):" in reply
    assert "- #1 (Recovered digest)" in reply
    assert jobs[0]["enabled"] == 1
    assert jobs[1]["enabled"] == 0
    assert jobs[2]["enabled"] == 0


@pytest.mark.asyncio
async def test_natural_language_schedule_maps_to_daily_template() -> None:
    """Simple Chinese schedule text should map to the daily template."""
    channel = _build_channel()
    reply = await channel._handle_schedule_command(
        "每天早上8点给我发一份AI日报",
        "oc_chat_1",
    )

    jobs = await channel.gateway.scheduler.list_jobs()
    assert "Created Feishu schedule #1." in reply
    assert "Name: Feishu daily digest @ every day at 08:00: AI" in reply
    assert "Tip: run `/schedule list` to see the current `#ID -> task name` mapping." in reply
    assert jobs[0]["name"] == "Feishu daily digest @ every day at 08:00: AI"
    assert jobs[0]["cron_expr"] == "0 8 * * *"
    assert "daily_digest" in str(jobs[0]["message"])


@pytest.mark.asyncio
async def test_natural_language_schedule_maps_to_workday_template() -> None:
    """Chinese workday schedule text should map to a workday cron template."""
    channel = _build_channel()
    reply = await channel._handle_schedule_command(
        "工作日早上8点给我发一份AI日报",
        "oc_chat_1",
    )

    jobs = await channel.gateway.scheduler.list_jobs()
    assert "Created Feishu schedule #1." in reply
    assert "Name: Feishu daily digest @ every workday at 08:00: AI" in reply
    assert jobs[0]["name"] == "Feishu daily digest @ every workday at 08:00: AI"
    assert jobs[0]["cron_expr"] == "0 8 * * 1-5"


@pytest.mark.asyncio
async def test_natural_language_schedule_maps_to_weekly_template() -> None:
    """Chinese weekly schedule text should map to a weekly cron template."""
    channel = _build_channel()
    reply = await channel._handle_schedule_command(
        "每周一早上9点给我发机器人热点",
        "oc_chat_1",
    )

    jobs = await channel.gateway.scheduler.list_jobs()
    assert "Created Feishu schedule #1." in reply
    assert "Name: Feishu hotspot brief @ every Monday at 09:00: 机器人" in reply
    assert jobs[0]["name"] == "Feishu hotspot brief @ every Monday at 09:00: 机器人"
    assert jobs[0]["cron_expr"] == "0 9 * * 1"


@pytest.mark.asyncio
async def test_natural_language_schedule_update_shortcut_rewrites_existing_job() -> None:
    """Chinese update text should map to `/schedule update ...` for one owned job."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Old digest",
        "old payload",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )

    reply = await channel._handle_schedule_command(
        "把1号定时任务改成工作日早上8点给我发一份AI日报",
        "oc_chat_1",
    )

    jobs = await channel.gateway.scheduler.list_jobs()
    assert "Updated Feishu schedule #1." in reply
    assert "Name: Feishu daily digest @ every workday at 08:00: AI" in reply
    assert jobs[0]["name"] == "Feishu daily digest @ every workday at 08:00: AI"
    assert jobs[0]["cron_expr"] == "0 8 * * 1-5"


@pytest.mark.asyncio
async def test_named_schedule_update_shortcut_matches_one_schedule() -> None:
    """Chinese update text can target one current-chat schedule by name/topic."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Feishu daily digest @ every day at 08:00: AI",
        "daily ai",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Feishu hotspot brief @ every day at 09:00: robotics",
        "robotics hotspot",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )

    reply = await channel._handle_schedule_command(
        "把AI日报那个定时任务改成工作日早上8点给我发一份AI日报",
        "oc_chat_1",
    )
    jobs = await channel.gateway.scheduler.list_jobs()

    assert "Updated Feishu schedule #1." in reply
    assert "Name: Feishu daily digest @ every workday at 08:00: AI" in reply
    assert jobs[0]["name"] == "Feishu daily digest @ every workday at 08:00: AI"
    assert jobs[0]["cron_expr"] == "0 8 * * 1-5"
    assert jobs[1]["name"] == "Feishu hotspot brief @ every day at 09:00: robotics"


@pytest.mark.asyncio
async def test_named_schedule_update_shortcut_reports_ambiguity() -> None:
    """Named schedule update should refuse ambiguous matches."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Feishu daily digest @ every day at 08:00: AI",
        "daily ai",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Feishu paper monitor @ every day at 09:00: AI",
        "paper ai",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )

    reply = await channel._handle_schedule_command(
        "把AI那个定时任务改成工作日早上8点给我发一份AI日报",
        "oc_chat_1",
    )
    jobs = await channel.gateway.scheduler.list_jobs()

    assert "Matched more than one schedule for `AI`:" in reply
    assert "#1 (Feishu daily digest @ every day at 08:00: AI)" in reply
    assert "#2 (Feishu paper monitor @ every day at 09:00: AI)" in reply
    assert jobs[0]["cron_expr"] == "0 8 * * *"
    assert jobs[1]["cron_expr"] == "0 9 * * *"


@pytest.mark.asyncio
async def test_filtered_schedule_list_shortcut_supports_signal_alias() -> None:
    """Chinese filtered list text should map to `/schedule list signal ...`."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Alerting digest",
        "daily one",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Recovered digest",
        "daily two",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    channel.gateway.scheduler.jobs[0]["runtime"] = {
        "health": "attention",
        "signal_timeline": [{"label": "alert", "detail": "attention_initial x1"}],
    }
    channel.gateway.scheduler.jobs[1]["runtime"] = {
        "health": "healthy",
        "signal_timeline": [{"label": "recovery", "detail": "healthy again"}],
    }

    reply = await channel._handle_schedule_command("看看告警的定时任务", "oc_chat_1")

    assert "signal=alert" in reply
    assert "Alerting digest" in reply
    assert "Recovered digest" not in reply


@pytest.mark.asyncio
async def test_filtered_schedule_pause_shortcut_supports_health_alias() -> None:
    """Chinese filtered pause text should map to `/schedule pause <health>`."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Attention digest",
        "daily one",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Healthy digest",
        "daily two",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    channel.gateway.scheduler.jobs[0]["runtime"] = {"health": "attention"}
    channel.gateway.scheduler.jobs[1]["runtime"] = {"health": "healthy"}

    reply = await channel._handle_schedule_command("暂停所有需要关注的定时任务", "oc_chat_1")
    jobs = await channel.gateway.scheduler.list_jobs()

    assert "Paused 1 Feishu schedule in this chat (health=attention):" in reply
    assert "- #1 (Attention digest)" in reply
    assert jobs[0]["enabled"] == 0
    assert jobs[1]["enabled"] == 1


@pytest.mark.asyncio
async def test_filtered_schedule_resume_shortcut_supports_signal_alias() -> None:
    """Chinese filtered resume text should map to `/schedule resume signal ...`."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Alerting digest",
        "daily one",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Recovered digest",
        "daily two",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    channel.gateway.scheduler.jobs[0]["enabled"] = 0
    channel.gateway.scheduler.jobs[1]["enabled"] = 0
    channel.gateway.scheduler.jobs[0]["runtime"] = {
        "health": "attention",
        "signal_timeline": [{"label": "alert", "detail": "attention_initial x1"}],
    }
    channel.gateway.scheduler.jobs[1]["runtime"] = {
        "health": "healthy",
        "signal_timeline": [{"label": "recovery", "detail": "healthy again"}],
    }

    reply = await channel._handle_schedule_command("启用所有告警定时任务", "oc_chat_1")
    jobs = await channel.gateway.scheduler.list_jobs()

    assert "Enabled 1 Feishu schedule in this chat (signal=alert):" in reply
    assert "- #1 (Alerting digest)" in reply
    assert jobs[0]["enabled"] == 1
    assert jobs[1]["enabled"] == 0


@pytest.mark.asyncio
async def test_natural_language_schedule_pause_shortcut_pauses_job() -> None:
    """Chinese pause text should map to `/schedule pause ...`."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Local digest",
        "daily local",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )

    reply = await channel._handle_schedule_command("暂停1号定时任务", "oc_chat_1")
    jobs = await channel.gateway.scheduler.list_jobs()

    assert reply == "Paused Feishu schedule #1 (Local digest)."
    assert jobs[0]["enabled"] == 0


@pytest.mark.asyncio
async def test_named_schedule_pause_shortcut_matches_one_schedule() -> None:
    """Chinese pause text can target one current-chat schedule by name/topic."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Feishu daily digest @ every day at 08:00: AI",
        "daily ai",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Feishu hotspot brief @ every day at 09:00: robotics",
        "robotics hotspot",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )

    reply = await channel._handle_schedule_command("暂停AI日报那个定时任务", "oc_chat_1")
    jobs = await channel.gateway.scheduler.list_jobs()

    assert reply == "Paused Feishu schedule #1 (Feishu daily digest @ every day at 08:00: AI)."
    assert jobs[0]["enabled"] == 0
    assert jobs[1]["enabled"] == 1


@pytest.mark.asyncio
async def test_named_schedule_shortcut_reports_ambiguity() -> None:
    """Named schedule management should refuse ambiguous matches."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Feishu daily digest @ every day at 08:00: AI",
        "daily ai",
        cron_expr="0 8 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )
    await channel.gateway.scheduler.add_job(
        "Feishu paper monitor @ every day at 09:00: AI",
        "paper ai",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
    )

    reply = await channel._handle_schedule_command("暂停AI那个定时任务", "oc_chat_1")
    jobs = await channel.gateway.scheduler.list_jobs()

    assert "Matched more than one schedule for `AI`:" in reply
    assert "#1 (Feishu daily digest @ every day at 08:00: AI)" in reply
    assert "#2 (Feishu paper monitor @ every day at 09:00: AI)" in reply
    assert "Run `/schedule list` and use the `#ID`" in reply
    assert jobs[0]["enabled"] == 1
    assert jobs[1]["enabled"] == 1


@pytest.mark.asyncio
async def test_natural_language_schedule_show_shortcut_returns_details() -> None:
    """Chinese show text should map to `/schedule show ...`."""
    channel = _build_channel()
    await channel.gateway.scheduler.add_job(
        "Local digest",
        "daily local",
        cron_expr="0 9 * * *",
        channel="feishu",
        target_id="oc_chat_1",
        quiet_start="21:00",
        quiet_end="07:00",
    )

    reply = await channel._handle_schedule_command("看看1号定时任务", "oc_chat_1")

    assert "Feishu schedule #1" in reply
    assert "Quiet window: 21:00-07:00" in reply


@pytest.mark.asyncio
async def test_natural_language_schedule_maps_to_paper_template() -> None:
    """Simple Chinese paper-monitor text should map to the paper template."""
    channel = _build_channel()
    reply = await channel._handle_schedule_command(
        "每天9点监控 video generation acceleration 论文 最近7天 最多6篇",
        "oc_chat_1",
    )

    jobs = await channel.gateway.scheduler.list_jobs()
    assert "Created Feishu schedule #1." in reply
    assert "Name: Feishu paper monitor @ every day at 09:00: video generation acceleration" in reply
    assert jobs[0]["name"] == (
        "Feishu paper monitor @ every day at 09:00: video generation acceleration"
    )
    assert jobs[0]["cron_expr"] == "0 9 * * *"
    assert '"window_days": 7' in str(jobs[0]["message"])
    assert '"max_items": 6' in str(jobs[0]["message"])
