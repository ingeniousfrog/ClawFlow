"""CLI task status and control tests."""

from __future__ import annotations

import asyncio
import json

from click.testing import CliRunner

from nanoclaw.cli.main import cli
from nanoclaw.runtime.tasks import TaskStore, set_task_store
from nanoclaw.security.audit import AuditLog


class FakeChannelGateway:
    """Gateway stub for CLI channel control tests."""

    def __init__(self) -> None:
        self.runtime = {
            "telegram": {
                "status": "running",
                "detail": "polling runtime active",
                "last_error": "",
                "last_transition_at": 123,
            },
            "feishu": {
                "status": "stopped",
                "detail": "stopped by operator",
                "last_error": "",
                "last_transition_at": 124,
            },
        }
        self.diagnostics = {
            "telegram": {
                "incoming_total": 2,
                "incoming_successes": 2,
                "incoming_failures": 0,
                "outgoing_total": 1,
                "outgoing_successes": 1,
                "outgoing_failures": 0,
                "targeted_outgoing_total": 0,
                "targeted_outgoing_successes": 0,
                "targeted_outgoing_failures": 0,
                "last_success_at": 130,
                "last_failure_at": 0,
                "last_failure_kind": "",
                "last_failure_error": "",
                "last_runtime_status": "running",
                "last_runtime_transition_at": 123,
            },
            "feishu": {
                "incoming_total": 0,
                "incoming_successes": 0,
                "incoming_failures": 0,
                "outgoing_total": 0,
                "outgoing_successes": 0,
                "outgoing_failures": 0,
                "targeted_outgoing_total": 0,
                "targeted_outgoing_successes": 0,
                "targeted_outgoing_failures": 0,
                "last_success_at": 0,
                "last_failure_at": 0,
                "last_failure_kind": "",
                "last_failure_error": "",
                "last_runtime_status": "stopped",
                "last_runtime_transition_at": 124,
            },
        }
        self.orchestration = {
            "telegram": {
                "channel_name": "telegram",
                "desired_state": "running",
                "desired_reason": "config default",
                "desired_updated_at": 123,
                "actual_status": "running",
                "actual_detail": "polling runtime active",
                "reconcile_status": "reconciled",
                "reconcile_detail": "desired `running` satisfied",
                "drift_status": "in_sync",
                "drift_since": 0,
                "drift_count": 0,
                "last_reconciled_at": 123,
                "last_action": "startup",
                "last_action_at": 123,
            },
            "feishu": {
                "channel_name": "feishu",
                "desired_state": "stopped",
                "desired_reason": "operator stop request",
                "desired_updated_at": 124,
                "actual_status": "stopped",
                "actual_detail": "stopped by operator",
                "reconcile_status": "reconciled",
                "reconcile_detail": "desired `stopped` satisfied",
                "drift_status": "in_sync",
                "drift_since": 0,
                "drift_count": 0,
                "last_reconciled_at": 124,
                "last_action": "stop",
                "last_action_at": 124,
            },
        }

    def get_channel_runtime_snapshot(self) -> dict[str, dict[str, object]]:
        """Return one runtime snapshot."""
        return {name: dict(state) for name, state in self.runtime.items()}

    def get_channel_diagnostics_snapshot(self) -> dict[str, dict[str, object]]:
        """Return one diagnostics snapshot."""
        return {name: dict(state) for name, state in self.diagnostics.items()}

    def get_channel_orchestration_snapshot(self) -> dict[str, dict[str, object]]:
        """Return one orchestration snapshot."""
        return {name: dict(state) for name, state in self.orchestration.items()}

    async def run_channel_action(self, name: str, action: str) -> dict[str, object]:
        """Apply one action to the fake channel runtime."""
        if action == "restart":
            state = {
                "status": "running",
                "detail": "polling runtime active",
                "last_error": "",
                "last_transition_at": 200,
            }
        elif action == "stop":
            state = {
                "status": "stopped",
                "detail": "stopped by operator",
                "last_error": "",
                "last_transition_at": 201,
            }
        else:
            state = {
                "status": "running",
                "detail": "webhook runtime active",
                "last_error": "",
                "last_transition_at": 202,
            }
        self.runtime[name] = state
        diagnostics = self.diagnostics.setdefault(name, {})
        diagnostics["last_runtime_status"] = state["status"]
        diagnostics["last_runtime_transition_at"] = state["last_transition_at"]
        orchestration = self.orchestration.setdefault(name, {"channel_name": name})
        orchestration["actual_status"] = state["status"]
        orchestration["actual_detail"] = state["detail"]
        orchestration["last_reconciled_at"] = state["last_transition_at"]
        orchestration["last_action"] = action
        orchestration["last_action_at"] = state["last_transition_at"]
        if action == "start":
            orchestration["desired_state"] = "running"
            orchestration["desired_reason"] = "operator start request"
        elif action == "stop":
            orchestration["desired_state"] = "stopped"
            orchestration["desired_reason"] = "operator stop request"
        elif action == "restart":
            orchestration["desired_state"] = "running"
            orchestration["desired_reason"] = "operator restart request"
        orchestration["drift_status"] = "in_sync"
        orchestration["reconcile_status"] = "reconciled"
        orchestration["reconcile_detail"] = (
            f"desired `{orchestration.get('desired_state', 'running')}` satisfied"
        )
        return dict(state)

    async def set_channel_desired_state(
        self,
        name: str,
        desired_state: str,
        *,
        reason: str = "",
        reconcile: bool = True,
    ) -> dict[str, object]:
        """Apply one desired-state update to the fake gateway."""
        orchestration = self.orchestration.setdefault(name, {"channel_name": name})
        orchestration["desired_state"] = desired_state
        orchestration["desired_reason"] = reason
        orchestration["desired_updated_at"] = 300
        orchestration["last_action"] = "set_desired_state"
        orchestration["last_action_at"] = 300
        if desired_state == "running" and reconcile:
            self.runtime[name] = {
                "status": "running",
                "detail": "webhook runtime active" if name == "feishu" else "polling runtime active",
                "last_error": "",
                "last_transition_at": 300,
            }
            orchestration["actual_status"] = "running"
            orchestration["actual_detail"] = self.runtime[name]["detail"]
            orchestration["drift_status"] = "in_sync"
            orchestration["reconcile_status"] = "reconciled"
            orchestration["reconcile_detail"] = "desired `running` satisfied"
        elif desired_state == "stopped" and reconcile:
            self.runtime[name] = {
                "status": "stopped",
                "detail": "stopped to satisfy desired state",
                "last_error": "",
                "last_transition_at": 300,
            }
            orchestration["actual_status"] = "stopped"
            orchestration["actual_detail"] = self.runtime[name]["detail"]
            orchestration["drift_status"] = "in_sync"
            orchestration["reconcile_status"] = "reconciled"
            orchestration["reconcile_detail"] = "desired `stopped` satisfied"
        else:
            orchestration["drift_status"] = "drifted"
            orchestration["reconcile_status"] = "pending"
            orchestration["reconcile_detail"] = (
                f"desired `{desired_state}` recorded; reconcile pending"
            )
        orchestration["last_reconciled_at"] = 300
        return {
            "last_transition_at": int(self.runtime.get(name, {}).get("last_transition_at", 0)),
            "last_reconciled_at": 300,
        }


def test_status_command_shows_recent_tasks(monkeypatch, tmp_path) -> None:
    """CLI status should print recent persisted task details."""
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)

    async def _seed() -> None:
        await store.create_task(
            "Investigate runtime recovery regression",
            source="spawn_task",
            session_id="telegram:42",
            priority=250,
            timeout_seconds=90,
        )
        await store.claim_next_task(source="spawn_task", worker_id="worker-a")

    asyncio.run(_seed())

    class FakeConfig:
        channels = type(
            "Channels",
            (),
            {
                "telegram": type("Telegram", (), {"enabled": False})(),
                "feishu": type("Feishu", (), {"enabled": False})(),
            },
        )()
        dashboard = type("DashboardCfg", (), {"enabled": False})()
        heartbeat = type(
            "HeartbeatCfg",
            (),
            {
                "enabled": False,
                "checklist_path": "HEARTBEAT.md",
                "notify_channel": "",
                "interval_seconds": 1800,
            },
        )()
        tools = type(
            "ToolsCfg",
            (),
            {
                "shell": type(
                    "ShellCfg",
                    (),
                    {
                        "mode": "subprocess",
                        "backend": "native",
                        "container_image": "",
                        "confirm_dangerous": True,
                        "isolate_home": True,
                        "max_memory_mb": 512,
                        "max_file_size_kb": 8192,
                    },
                )(),
                "secret_isolation": type(
                    "SecretCfg",
                    (),
                    {
                        "allow_environment_fallback": False,
                        "audit_access": True,
                    },
                )(),
                "web_search": type(
                    "WebSearchCfg",
                    (),
                    {
                        "provider": "serper",
                        "serper_max_calls": 0,
                        "allowed_hosts": ["example.com"],
                        "blocked_hosts": ["bad.example.com"],
                    },
                )()
            },
        )()

        def get_active_provider(self):
            return ("openai", "", "gpt-5.2", None)

    class FakeMemory:
        async def get_stats(self):
            return {
                "total_messages": 10,
                "sessions": 2,
                "memories": 1,
                "cron_jobs": 0,
            }

    class FakeAudit:
        async def get_stats_today(self):
            return {"messages": 2, "tool_calls": 1, "total_tokens": 20, "errors": 0, "blocked": 0}

        async def get_workflow_stats_today(self):
            return {
                "workflow_runs": 1,
                "total_tokens": 20,
                "avg_execution_ms": 123,
                "failures": 0,
            }

        async def get_workflow_evaluation_stats_today(self):
            return {
                "evaluations": 1,
                "good_runs": 1,
                "review_runs": 0,
                "poor_runs": 0,
                "positive_feedback": 1,
                "neutral_feedback": 0,
                "negative_feedback": 0,
                "unknown_feedback": 0,
                "avg_quality_score": 88,
                "avg_efficiency_score": 84,
            }

        async def get_recent_workflows(self, limit=3):
            return [
                {
                    "id": 3,
                    "workflow_name": "wechat_article_flow",
                    "status": "success",
                    "execution_ms": 220,
                    "total_tokens": 120,
                    "failure_reason": "",
                    "call_chain": [{"type": "tool", "name": "web_search", "status": "success"}],
                    "role_execution_timeline": [
                        {"role": "planner", "role_label": "planner", "stage": "pre_llm"},
                        {"role": "router", "role_label": "researcher", "stage": "pre_llm"},
                        {"role": "summarizer", "role_label": "editor", "stage": "post_tools"},
                    ],
                }
            ]

        async def get_recent_workflow_evaluations(self, limit=3):
            return [
                {
                    "workflow_run_id": 7,
                    "workflow_name": "default_chat_loop",
                    "evaluation_label": "good",
                    "quality_score": 88,
                    "efficiency_score": 84,
                    "feedback_signal": "positive",
                    "suggestions": ["Keep current workflow as the baseline path."],
                    "attention_reasons": [],
                }
            ]

        async def get_workflow_recommendations(self, days=7, limit=3):
            return [
                {
                    "workflow_name": "grounded_current_info",
                    "recommendation_status": "attention",
                    "run_count": 2,
                    "avg_quality_score": 48,
                    "avg_efficiency_score": 58,
                    "top_attention_reason": "Timeout-heavy paths are degrading this workflow.",
                    "recommendations": [
                        "Review failure paths and fallback coverage for this workflow."
                    ],
                }
            ]

        async def get_boundary_metrics(self, window_hours=24):
            return {
                "window_hours": window_hours,
                "boundary": {
                    "total": 3,
                    "allowed": 2,
                    "blocked": 1,
                    "top_tools": [{"tool_name": "web_search", "count": 2}],
                },
                "secrets": {
                    "total": 2,
                    "granted": 1,
                    "blocked": 0,
                    "missing": 1,
                    "config_sources": 1,
                    "env_sources": 0,
                    "top_tools": [{"tool_name": "web_search", "count": 2}],
                },
            }

    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr("nanoclaw.memory.store.get_memory_store", lambda: FakeMemory())
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: FakeAudit())
    monkeypatch.setattr(
        "nanoclaw.security.policy_contract.resolve_shell_backend",
        lambda backend, **kwargs: {
            "requested": "native",
            "selected": "native",
            "fallback_reason": "",
            "stronger_backend_available": False,
            "available_backends": [],
            "availability": {},
        },
    )
    monkeypatch.setattr(
        "nanoclaw.security.policy_contract.inspect_container_backend_health",
        lambda **kwargs: {
            "backend": "docker",
            "binary_available": True,
            "image_configured": False,
            "runtime_reachable": True,
            "image_present": False,
            "ready": False,
            "status": "missing_container_image",
            "detail": "containerImage is not configured",
            "runtime_version": "27.0.1",
        },
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["status"])

    assert result.exit_code == 0
    assert "Today's workflow evaluation:" in result.output
    assert "Good/Review/Poor: 1/0/0" in result.output
    assert "Avg quality: 88 Avg efficiency: 84" in result.output
    assert "Feedback +/0/-: 1/0/0" in result.output
    assert "Recent workflows:" in result.output
    assert (
        "roles=planner@pre_llm -> researcher[router]@pre_llm -> "
        "editor[summarizer]@post_tools"
    ) in result.output
    assert "Recent workflow evaluations:" in result.output
    assert (
        "run=#7 default_chat_loop [good] quality=88 "
        "efficiency=84 feedback=positive"
    ) in result.output
    assert "Workflow recommendations (7d):" in result.output
    assert "grounded_current_info [attention] runs=2 quality=48 efficiency=58" in result.output
    assert "Boundary policy:" in result.output
    assert "Contract: r1-f.v6" in result.output
    assert (
        "Shell: subprocess backend=native available=none image=off confirm=on "
        "isolateHome=on memory=512MB fileLimit=8192KB"
    ) in result.output
    assert (
        "Primary container target: docker status=missing_container_image "
        "runtime=on image=off detail=containerImage is not configured"
    ) in result.output
    assert "Verify: nanoclaw container-check --backend docker --refresh" in result.output
    assert "Remedy: Set `tools.shell.containerImage` to a local image tag for the docker backend." in result.output
    assert "Web hosts: enabled allow=1 block=1 policy=shared_tool_boundary@v0" in result.output
    assert "Secrets: envFallback=off audit=on caps=2 policy=tool_secret_broker@v0" in result.output
    assert "Channels:" in result.output
    assert "Contract: r2-f.v0" in result.output
    assert (
        "Summary: enabled=0 configured=0 running=0 failed=0 disabled=2 "
        "misconfigured=0 allowlist=0 open=0 proactiveReady=0 attention=0"
    ) in result.output
    assert "Routing: r2-f.v0" in result.output
    assert (
        "Orchestration: r2-f.v0 desiredRunning=0 desiredStopped=2 "
        "drifted=0 blocked=0 reconciling=0 interval=0s"
    ) in result.output
    assert (
        "Default proactive: request=auto selected=- status=unresolved "
        "mode=unresolved reason=telegram: channel disabled in config"
    ) in result.output
    assert (
        "Telegram: disabled mode=polling auth=open allow=0 proactive=on "
        "targeted=off confirm=on actions=none detail=disabled in config"
    ) in result.output
    assert (
        "authDetail=channel disabled in config routeMode=broadcast_allowlist "
        "routeReady=off routeRoles=none routeDetail=channel disabled in config"
    ) in result.output
    assert (
        "desired=stopped drift=in_sync reconcile=reconciled "
        "lastAction=- summary=desired `stopped` is satisfied"
    ) in result.output
    assert (
        "diag=disabled incoming=0/0 proactive=0/0 targeted=0/0 "
        "summary=channel disabled in config"
    ) in result.output
    assert (
        "Feishu: disabled mode=webhook auth=open allow=0 proactive=on "
        "targeted=on confirm=on actions=none detail=disabled in config"
    ) in result.output
    assert (
        "diag=disabled incoming=0/0 proactive=0/0 targeted=0/0 "
        "summary=channel disabled in config"
    ) in result.output
    assert (
        "Console: stopped mode=interactive auth=local allow=0 proactive=on "
        "targeted=off confirm=on actions=none detail=console runtime inactive"
    ) in result.output
    assert "Boundary activity (24h):" in result.output
    assert "Boundary decisions: allowed=2 blocked=1 total=3" in result.output
    assert "Secret access: granted=1 blocked=0 missing=1 config=1 env=0" in result.output
    assert "Queue:" in result.output
    assert "ready=0" in result.output
    assert "running=1" in result.output
    assert "rate_limited=0" in result.output
    assert "workers=1" in result.output
    assert "dead_letter=0" in result.output
    assert "starved_ready=0" in result.output
    assert "stale_running=0" in result.output
    assert "local_runtime=0/3" in result.output
    assert "global_pool=1/3" in result.output
    assert "global_saturation=33%" in result.output
    assert "stall_threshold=120s" in result.output
    assert "lease_timeout=45s" in result.output
    assert "heartbeat_interval=10s" in result.output
    assert "health=healthy" in result.output
    assert "base_alert_severity=none" in result.output
    assert "alert_channel=auto" in result.output
    assert "alert_escalation_channel=auto-secondary" in result.output
    assert "alert_cooldown=300s" in result.output
    assert "alert_escalate_after=2x" in result.output
    assert "schedule_alert_retrying_after=2x" in result.output
    assert "schedule_alert_escalate_after=3x" in result.output
    assert "Recent tasks:" in result.output
    assert "spawn_task" in result.output
    assert "worker-a" in result.output
    assert "prio=250" in result.output
    assert "timeout=90s" in result.output
    assert "backoff=30s" in result.output
    assert "rate_limit_key=-" in result.output
    assert "attempts=1/2" in result.output
    assert "Investigate runtime recovery regression" in result.output


def test_channel_list_command_shows_runtime_actions(monkeypatch) -> None:
    """CLI channel list should expose operator actions from the channel contract."""
    class FakeConfig:
        channels = type(
            "Channels",
            (),
            {
                "telegram": type(
                    "Telegram",
                    (),
                    {"enabled": True, "token": "bot-token", "allow_from": ["42"]},
                )(),
                "feishu": type(
                    "Feishu",
                    (),
                    {
                        "enabled": True,
                        "app_id": "cli_xxx",
                        "app_secret": "secret",
                        "allow_from": [],
                        "default_chat_id": "",
                    },
                )(),
            },
        )()

    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr(
        "nanoclaw.channels.gateway.get_gateway",
        lambda: FakeChannelGateway(),
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["channel", "list"])

    assert result.exit_code == 0
    assert "Channel Registry" in result.output
    assert "Contract: r2-f.v0" in result.output
    assert (
        "Orchestration: r2-f.v0 desiredRunning=1 desiredStopped=1 "
        "drifted=0 blocked=0 reconciling=0 interval=0s"
    ) in result.output
    assert (
        "Heartbeat: request=auto selected=telegram status=ready "
        "mode=auto reason=first runtime-ready candidate is telegram"
    ) in result.output
    assert (
        "Telegram: running mode=polling auth=allowlist allow=1 proactive=on "
        "targeted=off confirm=on actions=restart,stop detail=polling runtime active"
    ) in result.output
    assert (
        "authDetail=1 sender(s) allowed routeMode=broadcast_allowlist "
        "routeReady=on routeRoles=default_proactive,heartbeat,runtime_alert "
        "routeDetail=broadcast to 1 Telegram recipient(s)"
    ) in result.output
    assert (
        "desired=running drift=in_sync reconcile=reconciled "
        "lastAction=startup summary=desired `running` is satisfied"
    ) in result.output
    assert (
        "diag=healthy incoming=2/0 proactive=1/0 targeted=0/0 "
        "summary=recent proactive delivery succeeded"
    ) in result.output
    assert (
        "Feishu: stopped mode=webhook auth=open allow=0 proactive=on "
        "targeted=on confirm=on actions=start detail=stopped by operator"
    ) in result.output
    assert (
        "desired=stopped drift=in_sync reconcile=reconciled "
        "lastAction=stop summary=desired `stopped` is satisfied"
    ) in result.output
    assert (
        "diag=stopped incoming=0/0 proactive=0/0 targeted=0/0 "
        "summary=runtime stopped"
    ) in result.output


def test_channel_list_command_shows_console_runtime_channel(monkeypatch) -> None:
    """CLI channel list should surface console when it is active in runtime."""
    class FakeConfig:
        channels = type(
            "Channels",
            (),
            {
                "telegram": type(
                    "Telegram",
                    (),
                    {"enabled": False, "token": "", "allow_from": []},
                )(),
                "feishu": type(
                    "Feishu",
                    (),
                    {
                        "enabled": False,
                        "app_id": "",
                        "app_secret": "",
                        "allow_from": [],
                        "default_chat_id": "",
                    },
                )(),
            },
        )()

    gateway = FakeChannelGateway()
    gateway.runtime["console"] = {
        "status": "running",
        "detail": "active in gateway runtime",
        "last_error": "",
        "last_transition_at": 222,
    }

    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr("nanoclaw.channels.gateway.get_gateway", lambda: gateway)

    runner = CliRunner()
    result = runner.invoke(cli, ["channel", "list"])

    assert result.exit_code == 0
    assert (
        "Console: running mode=interactive auth=local allow=0 proactive=on "
        "targeted=off confirm=on actions=none detail=active in gateway runtime"
    ) in result.output


def test_channel_action_command_runs_operator_action(monkeypatch) -> None:
    """CLI channel action should dispatch to the active gateway runtime."""
    class FakeConfig:
        channels = type(
            "Channels",
            (),
            {
                "telegram": type(
                    "Telegram",
                    (),
                    {"enabled": True, "token": "bot-token", "allow_from": ["42"]},
                )(),
                "feishu": type(
                    "Feishu",
                    (),
                    {
                        "enabled": True,
                        "app_id": "cli_xxx",
                        "app_secret": "secret",
                        "allow_from": [],
                        "default_chat_id": "",
                    },
                )(),
            },
        )()

    gateway = FakeChannelGateway()
    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr("nanoclaw.channels.gateway.get_gateway", lambda: gateway)

    runner = CliRunner()
    result = runner.invoke(cli, ["channel", "action", "feishu", "start"])

    assert result.exit_code == 0
    assert "Channel Action" in result.output
    assert "Feishu: action=start status=running detail=webhook runtime active" in result.output
    assert "Desired state: running drift=in_sync reconcile=reconciled" in result.output
    assert "Reconcile detail: desired `running` satisfied" in result.output
    assert "Diagnostics: idle (runtime active with no recent channel traffic)" in result.output
    assert "Next actions: restart, stop" in result.output
    assert "Transition at: 202" in result.output


def test_channel_desired_state_command_updates_runtime_state(monkeypatch) -> None:
    """CLI desired-state command should persist desired state through the gateway."""
    class FakeConfig:
        channels = type(
            "Channels",
            (),
            {
                "telegram": type(
                    "Telegram",
                    (),
                    {"enabled": True, "token": "bot-token", "allow_from": ["42"]},
                )(),
                "feishu": type(
                    "Feishu",
                    (),
                    {
                        "enabled": True,
                        "app_id": "cli_xxx",
                        "app_secret": "secret",
                        "allow_from": [],
                        "default_chat_id": "",
                    },
                )(),
            },
        )()

    gateway = FakeChannelGateway()
    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr("nanoclaw.channels.gateway.get_gateway", lambda: gateway)

    runner = CliRunner()
    result = runner.invoke(cli, ["channel", "desired-state", "feishu", "running"])

    assert result.exit_code == 0
    assert "Channel Desired State" in result.output
    assert "Feishu: desired=running actual=running drift=in_sync reconcile=reconciled" in result.output
    assert "Summary: desired `running` is satisfied" in result.output
    assert "Next actions: restart, stop" in result.output
    assert "Transition at: 300" in result.output


def test_container_check_reports_remediation_and_exits_nonzero(monkeypatch) -> None:
    """container-check should print remediation and fail when not ready."""
    class FakeConfig:
        tools = type(
            "ToolsCfg",
            (),
            {
                "shell": type("ShellCfg", (), {"container_image": ""})(),
            },
        )()

    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr(
        "nanoclaw.security.sandbox_backends.inspect_container_backend_health",
        lambda **kwargs: {
            "backend": "docker",
            "configured_image": "",
            "runtime_reachable": False,
            "image_present": False,
            "ready": False,
            "status": "missing_container_image",
            "detail": "containerImage is not configured",
        },
    )
    monkeypatch.setattr(
        "nanoclaw.security.sandbox_backends.get_container_remediation_plan",
        lambda health, **kwargs: {
            "steps": ["Set tools.shell.containerImage before enabling docker."],
            "commands": ["nanoclaw container-check --backend docker --refresh"],
            "verify_command": "nanoclaw container-check --backend docker --refresh",
        },
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["container-check"])

    assert result.exit_code == 1
    assert "Container Readiness Check" in result.output
    assert "Status: missing_container_image" in result.output
    assert "Set tools.shell.containerImage before enabling docker." in result.output


def test_container_prepare_reports_pull_and_succeeds(monkeypatch) -> None:
    """container-prepare should report provisioning actions and finish ready."""
    class FakeConfig:
        tools = type(
            "ToolsCfg",
            (),
            {
                "shell": type("ShellCfg", (), {"container_image": "busybox:latest"})(),
            },
        )()

    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr(
        "nanoclaw.security.sandbox_backends.prepare_container_backend",
        lambda **kwargs: {
            "backend": "docker",
            "health_before": {
                "backend": "docker",
                "configured_image": "busybox:latest",
                "runtime_reachable": True,
                "image_present": False,
                "ready": False,
                "status": "image_missing",
                "detail": "No such image: busybox:latest",
            },
            "health_after": {
                "backend": "docker",
                "configured_image": "busybox:latest",
                "runtime_reachable": True,
                "image_present": True,
                "ready": True,
                "status": "ready",
                "detail": "sha256:abc",
            },
            "ready": True,
            "actions": [
                {
                    "name": "pull_image",
                    "command": "docker pull busybox:latest",
                    "success": True,
                    "detail": "Downloaded newer image",
                }
            ],
            "remediation": {
                "steps": ["Primary container target is ready."],
                "commands": ["nanoclaw container-check --backend docker --refresh --image busybox:latest"],
                "verify_command": "nanoclaw container-check --backend docker --refresh --image busybox:latest",
                "prepare_command": "nanoclaw container-prepare --backend docker --refresh --image busybox:latest --pull",
            },
        },
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["container-prepare"])

    assert result.exit_code == 0
    assert "Container Preparation" in result.output
    assert "Before: image_missing (No such image: busybox:latest)" in result.output
    assert "After: ready (sha256:abc)" in result.output
    assert "pull_image: ok" in result.output
    assert "docker pull busybox:latest" in result.output


def test_container_runtime_reports_lifecycle_and_succeeds(monkeypatch) -> None:
    """container-runtime should report lifecycle orchestration actions."""
    class FakeConfig:
        tools = type(
            "ToolsCfg",
            (),
            {
                "shell": type("ShellCfg", (), {"container_image": "busybox:latest"})(),
            },
        )()

    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: FakeConfig())
    monkeypatch.setattr(
        "nanoclaw.security.sandbox_backends.manage_container_runtime",
        lambda **kwargs: {
            "backend": "docker",
            "health_before": {
                "backend": "docker",
                "configured_image": "busybox:latest",
                "runtime_reachable": False,
                "image_present": True,
                "ready": False,
                "status": "runtime_unreachable",
                "detail": "Docker Desktop is not running",
                "drifted": True,
                "lifecycle_state": "runtime_lost",
                "drift_reason": "runtime lost after ready",
            },
            "health_after": {
                "backend": "docker",
                "configured_image": "busybox:latest",
                "runtime_reachable": True,
                "image_present": True,
                "ready": True,
                "status": "ready",
                "detail": "sha256:abc",
                "drifted": False,
            },
            "runtime_ready": True,
            "ready": True,
            "actions": [
                {
                    "name": "stop_runtime",
                    "command": "osascript -e 'quit app \"Docker\"'",
                    "success": True,
                    "detail": "ok",
                },
                {
                    "name": "start_runtime",
                    "command": "open -a Docker",
                    "success": True,
                    "detail": "ok",
                },
            ],
            "remediation": {
                "steps": ["Primary container target is ready."],
                "commands": ["nanoclaw container-check --backend docker --refresh --image busybox:latest"],
                "verify_command": "nanoclaw container-check --backend docker --refresh --image busybox:latest",
                "prepare_command": "nanoclaw container-prepare --backend docker --refresh --image busybox:latest --pull",
                "runtime_command": "nanoclaw container-runtime --backend docker --refresh --restart --image busybox:latest --prepare --pull",
            },
        },
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["container-runtime", "--restart"])

    assert result.exit_code == 0
    assert "Container Runtime Orchestration" in result.output
    assert "Before: runtime_unreachable (Docker Desktop is not running)" in result.output
    assert "After: ready (sha256:abc)" in result.output
    assert "Drift: runtime_lost (runtime lost after ready)" in result.output
    assert "stop_runtime: ok" in result.output
    assert "start_runtime: ok" in result.output
    assert "open -a Docker" in result.output


def test_task_cancel_command_updates_task_state(tmp_path) -> None:
    """CLI task-cancel should request cancellation for a persisted task."""
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)

    async def _seed() -> str:
        created = await store.create_task("cancel me", source="spawn_task")
        return created["task_id"]

    task_id = asyncio.run(_seed())

    runner = CliRunner()
    result = runner.invoke(cli, ["task-cancel", task_id])

    assert result.exit_code == 0
    assert f"Task {task_id} cancel requested." in result.output

    cancelled = asyncio.run(store.get_task(task_id))
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested"] is True


def test_task_requeue_command_resets_failed_task(tmp_path) -> None:
    """CLI task-requeue should move a dead-letter task back to pending."""
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)

    async def _seed() -> str:
        created = await store.create_task("requeue me", source="spawn_task", max_attempts=1)
        await store.claim_next_task(source="spawn_task", worker_id="worker-a")
        await store.fail_task_attempt(created["task_id"], last_error="fatal")
        return created["task_id"]

    task_id = asyncio.run(_seed())

    runner = CliRunner()
    result = runner.invoke(cli, ["task-requeue", task_id])

    assert result.exit_code == 0
    assert f"Task {task_id} requeued." in result.output

    requeued = asyncio.run(store.get_task(task_id))
    assert requeued is not None
    assert requeued["status"] == "pending"
    assert requeued["dead_lettered"] is False


def test_task_replay_command_prints_task_execution_chain(tmp_path, monkeypatch) -> None:
    """CLI task-replay should show persisted steps and tool traces for one task."""
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    async def _seed() -> str:
        created = await store.create_task("replay me", source="spawn_task")
        await store.claim_next_task(source="spawn_task", worker_id="worker-a")
        await store.start_task_step(
            created["task_id"],
            "agent_run",
            step_name="agent_run",
            input_payload={"task_description": "replay me"},
            is_checkpoint=True,
        )
        await store.complete_task_step(
            created["task_id"],
            "agent_run",
            output_payload={"result_text": "done"},
        )
        await audit.log_task_run(
            task_id=created["task_id"],
            session_id=f"task:{created['task_id']}",
            attempt_number=1,
            worker_id="worker-a",
            status="success",
            final_output_summary="done",
            execution_ms=42,
        )
        await audit.log_tool_trace(
            task_id=created["task_id"],
            session_id=f"task:{created['task_id']}",
            step_id="agent_run",
            attempt_number=1,
            tool_name="web_search",
            output_summary="done",
            status="success",
            execution_ms=12,
        )
        await audit.log(
            action_type="runtime_watchdog",
            tool_name="spawn_task",
            input_summary="event=timeout_cancelled",
            output_summary="Cancelled running task after timeout budget was exceeded.",
            status="warning",
            session_id=f"task:{created['task_id']}",
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
            session_id=f"task:{created['task_id']}",
        )
        return created["task_id"]

    task_id = asyncio.run(_seed())

    runner = CliRunner()
    result = runner.invoke(cli, ["task-replay", task_id])

    assert result.exit_code == 0
    assert f"Task {task_id}" in result.output
    assert "Steps:" in result.output
    assert "agent_run [succeeded]" in result.output
    assert "Tool traces:" in result.output
    assert "web_search [success]" in result.output
    assert "Audit events:" in result.output
    assert "runtime_watchdog [warning]" in result.output
    assert "boundary_decision [blocked]" in result.output


def test_workflow_feedback_command_updates_evaluation(tmp_path, monkeypatch) -> None:
    """CLI workflow-feedback should persist one explicit feedback signal."""
    audit = AuditLog(tmp_path / "tasks.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    async def _seed() -> int:
        await audit.log_workflow_run(
            session_id="cli:user",
            workflow_name="default_chat_loop",
            workflow_tags=["default_chat_loop"],
            user_summary="hello",
            status="success",
            failure_reason="",
            total_tokens=120,
            execution_ms=200,
            llm_calls=1,
            tool_calls=0,
            final_model="gpt-5.2",
            call_chain=[{"type": "llm", "model": "gpt-5.2", "status": "success"}],
        )
        rows = await audit.get_recent_workflow_evaluations(limit=1)
        return int(rows[0]["workflow_run_id"])

    workflow_run_id = asyncio.run(_seed())

    runner = CliRunner()
    result = runner.invoke(cli, ["workflow-feedback", str(workflow_run_id), "negative"])

    assert result.exit_code == 0
    assert f"Workflow run #{workflow_run_id} feedback updated to negative." in result.output

    evaluations = asyncio.run(audit.get_recent_workflow_evaluations(limit=1))
    assert evaluations[0]["feedback_signal"] == "negative"


def test_workflow_report_command_prints_recommendations(tmp_path, monkeypatch) -> None:
    """CLI workflow-report should print aggregated workflow recommendations."""
    audit = AuditLog(tmp_path / "tasks.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    async def _seed() -> None:
        await audit.log_workflow_run(
            session_id="cli:user",
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

    asyncio.run(_seed())

    runner = CliRunner()
    result = runner.invoke(cli, ["workflow-report", "--days", "7", "--limit", "5"])

    assert result.exit_code == 0
    assert "Workflow recommendations (7d):" in result.output
    assert "grounded_current_info" in result.output
    assert "why=" in result.output


def test_workflow_roles_command_prints_role_replay(tmp_path, monkeypatch) -> None:
    """CLI workflow-roles should print one role-level replay view."""
    audit = AuditLog(tmp_path / "tasks.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    async def _seed() -> int:
        await audit.log_workflow_run(
            session_id="cli:user",
            workflow_name="grounded_current_info",
            workflow_tags=["grounded_current_info"],
            user_summary="latest news",
            status="success",
            failure_reason="",
            total_tokens=120,
            execution_ms=220,
            llm_calls=2,
            tool_calls=1,
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
                    "stage": "pre_llm",
                    "checkpoint_id": "planner@pre_llm",
                    "status": "attached",
                    "workflow_name": "grounded_current_info",
                    "contract": {},
                    "evidence_refs": [],
                },
                {
                    "type": "workflow_role_execution",
                    "role": "critic",
                    "stage": "post_tools",
                    "checkpoint_id": "critic@post_tools",
                    "status": "attached",
                    "workflow_name": "grounded_current_info",
                    "contract": {},
                    "evidence_refs": ["ev_1"],
                },
                {
                    "type": "workflow_role_task",
                    "role": "executor",
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
                    "stage": "tool_phase",
                    "task_type": "workflow_role",
                    "source": "workflow_role",
                    "description": "grounded_current_info:executor@tool_phase",
                    "priority": 760,
                    "timeout_seconds": 600,
                    "max_attempts": 2,
                    "idempotency_key": "cli:user:grounded_current_info:executor@tool_phase",
                    "payload": {
                        "parent_task_id": "task_parent_1",
                        "depends_on": ["router@pre_llm"],
                    },
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
                    "source_workflow_run_id": 12,
                    "source_workflow_name": "grounded_current_info",
                    "source_status": "degraded",
                    "failure_reason": "paper_search:error",
                    "restored_evidence_count": 1,
                    "status": "resumed",
                    "evidence_refs": ["ev_1"],
                },
            ],
        )
        rows = await audit.get_recent_workflows(limit=1)
        return int(rows[0]["id"])

    workflow_run_id = asyncio.run(_seed())

    runner = CliRunner()
    result = runner.invoke(cli, ["workflow-roles", str(workflow_run_id)])

    assert result.exit_code == 0
    assert f"Workflow run #{workflow_run_id} grounded_current_info [success]" in result.output
    assert "shared_evidence_refs=ev_1" in result.output
    assert "Role checkpoints:" in result.output
    assert "planner@pre_llm role=planner stage=pre_llm messages=3 evidence=0 refs=-" in result.output
    assert "planner@pre_llm checkpoint=planner@pre_llm [attached] evidence=-" in result.output
    assert "critic@post_tools checkpoint=critic@post_tools [attached] evidence=ev_1" in result.output
    assert "Role task envelopes:" in result.output
    assert (
        "executor@tool_phase role=executor stage=tool_phase [attached] "
        "depends_on=router@pre_llm checkpoint=- resume=router@pre_llm retry_budget=2 "
        "evidence=ev_1"
    ) in result.output
    assert "Role runtime bridge:" in result.output
    assert (
        "executor@tool_phase role=executor type=workflow_role source=workflow_role priority=760 "
        "timeout=600 max_attempts=2 parent_task=task_parent_1 "
        "depends_on=router@pre_llm evidence=ev_1"
    ) in result.output
    assert "Role recovery timeline:" in result.output
    assert (
        "executor->router stage=post_tools [resumed] reason=paper_search:error "
        "resume=router@pre_llm attempt=1/2 remaining=1 restored_messages=3 "
        "restored_evidence=0 evidence=ev_1"
    ) in result.output
    assert "Persistent resumes:" in result.output
    assert (
        "router@pre_llm resume=router@pre_llm source_run=12 [resumed] "
        "source_status=degraded failure=paper_search:error restored_evidence=1 evidence=ev_1"
    ) in result.output


def test_workflow_roles_command_prints_article_role_labels(tmp_path, monkeypatch) -> None:
    """CLI workflow-roles should expose article-facing role labels and artifacts."""
    audit = AuditLog(tmp_path / "tasks.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)

    async def _seed() -> int:
        await audit.log_workflow_run(
            session_id="cli:user",
            workflow_name="wechat_article_flow",
            workflow_tags=["wechat_article_flow"],
            user_summary="写一篇视频生成模型加速周报",
            status="success",
            failure_reason="",
            total_tokens=180,
            execution_ms=420,
            llm_calls=2,
            tool_calls=1,
            final_model="gpt-5.2",
            call_chain=[
                {
                    "type": "workflow_role_execution",
                    "role": "router",
                    "role_label": "researcher",
                    "stage": "pre_llm",
                    "checkpoint_id": "router@pre_llm",
                    "status": "attached",
                    "workflow_name": "wechat_article_flow",
                    "handler_kind": "execution_brief",
                    "brief_content": "Researcher phase: collect article evidence first.",
                    "artifact_preview": "Evidence route ready: merge RSS and paper evidence.",
                    "contract": {},
                    "evidence_refs": ["ev_1"],
                },
                {
                    "type": "workflow_role_task",
                    "role": "summarizer",
                    "role_label": "editor",
                    "stage": "post_tools",
                    "task_key": "summarizer@post_tools",
                    "status": "attached",
                    "depends_on": ["critic@post_tools"],
                    "checkpoint_id": "summarizer@post_tools",
                    "resume_checkpoint_id": "router@pre_llm",
                    "retry_budget": 2,
                    "evidence_refs": ["ev_1"],
                },
                {
                    "type": "workflow_role_task_bridge",
                    "task_key": "summarizer@post_tools",
                    "role": "summarizer",
                    "role_label": "editor",
                    "stage": "post_tools",
                    "task_type": "workflow_role",
                    "source": "workflow_role",
                    "description": "wechat_article_flow:summarizer@post_tools",
                    "priority": 680,
                    "timeout_seconds": 180,
                    "max_attempts": 2,
                    "idempotency_key": "cli:user:wechat_article_flow:summarizer@post_tools",
                    "payload": {
                        "parent_task_id": "task_parent_article",
                        "depends_on": ["critic@post_tools"],
                    },
                    "evidence_refs": ["ev_1"],
                },
            ],
        )
        rows = await audit.get_recent_workflows(limit=1)
        return int(rows[0]["id"])

    workflow_run_id = asyncio.run(_seed())

    runner = CliRunner()
    result = runner.invoke(cli, ["workflow-roles", str(workflow_run_id)])

    assert result.exit_code == 0
    assert "researcher[router]@pre_llm checkpoint=router@pre_llm [attached]" in result.output
    assert "artifact=Evidence route ready: merge RSS and paper evidence." in result.output
    assert (
        "summarizer@post_tools role=editor[summarizer] stage=post_tools [attached]"
    ) in result.output
    assert (
        "summarizer@post_tools role=editor[summarizer] type=workflow_role "
        "source=workflow_role"
    ) in result.output


def test_persona_show_command_renders_json(monkeypatch, tmp_path) -> None:
    """persona show should expose protected persona fragments as JSON."""
    persona_path = tmp_path / "persona_fragments.json"
    persona_path.write_text(
        json.dumps(
            {
                "identity": ["Ground answers in cited evidence."],
                "style": ["Keep responses concise."],
                "reviewCount": 2,
                "lastSource": "workflow_review",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "nanoclaw.core.persona.get_default_persona_path",
        lambda: persona_path,
    )

    runner = CliRunner()
    result = runner.invoke(cli, ["persona", "show", "--format", "json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["identity"] == ["Ground answers in cited evidence."]
    assert data["style"] == ["Keep responses concise."]
    assert data["reviewCount"] == 2
    assert data["path"] == str(persona_path)


def test_persona_apply_review_command_updates_store(monkeypatch, tmp_path) -> None:
    """persona apply-review should update the protected fragment store."""
    persona_path = tmp_path / "persona_fragments.json"
    monkeypatch.setattr(
        "nanoclaw.core.persona.get_default_persona_path",
        lambda: persona_path,
    )

    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "persona",
            "apply-review",
            "--summary",
            "identity: Research-focused assistant.\nworkflow: Use grounded search first.",
            "--source",
            "manual_review",
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["identity"] == ["Research-focused assistant."]
    assert data["workflowPreferences"] == ["Use grounded search first."]
    assert data["reviewCount"] == 1
    assert data["lastSource"] == "manual_review"
