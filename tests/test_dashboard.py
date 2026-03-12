"""Dashboard task visibility tests."""

from __future__ import annotations

import json
from types import SimpleNamespace

from aiohttp.test_utils import make_mocked_request

from nanoclaw.dashboard.server import Dashboard
from nanoclaw.runtime.tasks import TaskStore, set_task_store
from nanoclaw.security.audit import AuditLog


def _dashboard_config():
    """Return a minimal config stub for dashboard tests."""
    return SimpleNamespace(
        dashboard=SimpleNamespace(password=None),
        tools=SimpleNamespace(
            web_search=SimpleNamespace(
                serper_max_calls=0,
                allowed_hosts=["example.com"],
                blocked_hosts=["bad.example.com"],
            ),
            shell=SimpleNamespace(
                mode="subprocess",
                backend="native",
                container_image="",
                confirm_dangerous=True,
                isolate_home=True,
                max_memory_mb=512,
                max_file_size_kb=8192,
            ),
            secret_isolation=SimpleNamespace(
                allow_environment_fallback=False,
                audit_access=True,
            ),
        ),
        get_default_model=lambda: "gpt-5.2",
    )


class DummyCronScheduler:
    """Minimal in-memory scheduler stub for dashboard cron tests."""

    def __init__(self, jobs: list[dict]) -> None:
        self.jobs = [dict(item) for item in jobs]

    async def list_jobs(self) -> list[dict]:
        """Return all tracked jobs."""
        return [dict(item) for item in self.jobs]

    async def list_jobs_with_runtime_state(self) -> list[dict]:
        """Return tracked jobs including optional runtime metadata."""
        return [dict(item) for item in self.jobs]

    async def toggle_job(self, job_id: int, enabled: bool) -> None:
        """Update one job's enabled state."""
        for item in self.jobs:
            if int(item["id"]) == job_id:
                item["enabled"] = 1 if enabled else 0
                return
        raise KeyError(job_id)

    async def remove_job(self, job_id: int) -> None:
        """Delete one tracked cron job."""
        before = len(self.jobs)
        self.jobs = [item for item in self.jobs if int(item["id"]) != job_id]
        if len(self.jobs) == before:
            raise KeyError(job_id)


class FakeChannelGateway:
    """Minimal gateway stub for dashboard channel control tests."""

    def __init__(self) -> None:
        self.channels = {"telegram": object()}
        self._runtime = {
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
        self._diagnostics = {
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
        self._orchestration = {
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
        """Return current runtime state."""
        return {name: dict(state) for name, state in self._runtime.items()}

    def get_channel_diagnostics_snapshot(self) -> dict[str, dict[str, object]]:
        """Return current channel diagnostics state."""
        return {name: dict(state) for name, state in self._diagnostics.items()}

    def get_channel_orchestration_snapshot(self) -> dict[str, dict[str, object]]:
        """Return current desired-state orchestration state."""
        return {name: dict(state) for name, state in self._orchestration.items()}

    async def run_channel_action(self, name: str, action: str) -> dict[str, object]:
        """Apply one channel action to the fake runtime state."""
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
        self._runtime[name] = state
        diagnostics = self._diagnostics.setdefault(name, {})
        diagnostics["last_runtime_status"] = state["status"]
        diagnostics["last_runtime_transition_at"] = state["last_transition_at"]
        orchestration = self._orchestration.setdefault(name, {"channel_name": name})
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
        if state["status"] == "running":
            self.channels[name] = object()
        else:
            self.channels.pop(name, None)
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
        orchestration = self._orchestration.setdefault(name, {"channel_name": name})
        orchestration["desired_state"] = desired_state
        orchestration["desired_reason"] = reason
        orchestration["desired_updated_at"] = 300
        orchestration["last_action"] = "set_desired_state"
        orchestration["last_action_at"] = 300
        if desired_state == "running" and reconcile:
            state = {
                "status": "running",
                "detail": "webhook runtime active" if name == "feishu" else "polling runtime active",
                "last_error": "",
                "last_transition_at": 300,
            }
            self._runtime[name] = state
            orchestration["actual_status"] = "running"
            orchestration["actual_detail"] = state["detail"]
            orchestration["drift_status"] = "in_sync"
            orchestration["reconcile_status"] = "reconciled"
            orchestration["reconcile_detail"] = "desired `running` satisfied"
            self.channels[name] = object()
        elif desired_state == "stopped" and reconcile:
            state = {
                "status": "stopped",
                "detail": "stopped to satisfy desired state",
                "last_error": "",
                "last_transition_at": 300,
            }
            self._runtime[name] = state
            orchestration["actual_status"] = "stopped"
            orchestration["actual_detail"] = state["detail"]
            orchestration["drift_status"] = "in_sync"
            orchestration["reconcile_status"] = "reconciled"
            orchestration["reconcile_detail"] = "desired `stopped` satisfied"
            self.channels.pop(name, None)
        else:
            orchestration["drift_status"] = "drifted"
            orchestration["reconcile_status"] = "pending"
            orchestration["reconcile_detail"] = (
                f"desired `{desired_state}` recorded; reconcile pending"
            )
        orchestration["last_reconciled_at"] = 300
        return {
            "last_transition_at": int(self._runtime.get(name, {}).get("last_transition_at", 0)),
            "last_reconciled_at": 300,
        }


async def test_dashboard_tasks_api_returns_recent_tasks(tmp_path) -> None:
    """Dashboard should expose compact recent task rows via /api/tasks."""
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)
    created = await store.create_task(
        "Research recovery behavior",
        source="spawn_task",
        session_id="feishu:user-1",
        priority=250,
        timeout_seconds=90,
    )
    await store.claim_next_task(source="spawn_task", worker_id="worker-a")

    dashboard = Dashboard(_dashboard_config(), SimpleNamespace(channels={}))
    request = make_mocked_request("GET", "/api/tasks?limit=5")

    response = await dashboard._api_tasks(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload[0]["task_id"] == created["task_id"]
    assert payload[0]["status"] == "running"
    assert payload[0]["claimed_by"] == "worker-a"
    assert payload[0]["attempt_count"] == 1
    assert payload[0]["priority"] == 250
    assert payload[0]["timeout_seconds"] == 90
    assert payload[0]["max_attempts"] == 2
    assert payload[0]["retry_backoff_seconds"] == 30
    assert payload[0]["rate_limit_key"] == ""
    assert payload[0]["rate_limit_window_seconds"] == 0
    assert payload[0]["rate_limit_max_claims"] == 0
    assert payload[0]["cancel_requested"] is False


async def test_dashboard_status_includes_recent_tasks(tmp_path, monkeypatch) -> None:
    """Dashboard status summary should include task and workflow evaluation summaries."""
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    created = await store.create_task("Queued follow-up", source="spawn_task")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
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
    await audit.log_workflow_run(
        session_id="feishu:user-1",
        workflow_name="wechat_article_flow",
        workflow_tags=["wechat_article_flow"],
        user_summary="Queued follow-up",
        status="success",
        failure_reason="",
        total_tokens=120,
        execution_ms=240,
        llm_calls=1,
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
                "role_label": "planner",
                "stage": "pre_llm",
                "status": "attached",
                "workflow_name": "wechat_article_flow",
                "contract": {},
                "evidence_refs": [],
            },
            {
                "type": "tool",
                "name": "web_search",
                "status": "success",
                "evidence_refs": ["ev_1"],
            },
            {
                "type": "workflow_role_execution",
                "role": "summarizer",
                "role_label": "editor",
                "stage": "post_tools",
                "status": "attached",
                "workflow_name": "wechat_article_flow",
                "contract": {},
                "evidence_refs": ["ev_1"],
            },
        ],
    )
    evaluations = await audit.get_recent_workflow_evaluations(limit=1)
    await audit.set_workflow_feedback(evaluations[0]["workflow_run_id"], "positive")
    await audit.log(
        action_type="secret_access",
        tool_name="web_search",
        input_summary=(
            "capability=web_search.serper_api_key "
            "source=config:tools.webSearch.serperApiKey"
        ),
        output_summary="policy=tool_secret_broker version=v0 decision=granted",
        status="success",
        session_id=f"task:{created['task_id']}",
    )
    await audit.log(
        action_type="boundary_decision",
        tool_name="web_fetch",
        input_summary=(
            "operation=web_fetch boundary=outbound_url action=fetch "
            "target=https://example.com/article"
        ),
        output_summary="policy=shared_tool_boundary version=v0 decision=blocked",
        status="blocked",
        session_id=f"task:{created['task_id']}",
    )

    dashboard = Dashboard(
        _dashboard_config(),
        SimpleNamespace(channels={"feishu": object(), "console": object()}),
    )
    request = make_mocked_request("GET", "/api/status")

    response = await dashboard._api_status(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload["status"] == "online"
    assert payload["channels"]["feishu"]["status"] == "running"
    assert payload["channel_contract"]["contract_version"] == "r2-f.v0"
    assert payload["channel_contract"]["summary"]["running_count"] == 2
    assert payload["channel_contract"]["channels"]["feishu"]["operator_actions"] == [
        "restart",
        "stop",
    ]
    assert payload["channel_contract"]["channels"]["console"]["status"] == "running"
    assert payload["channel_contract"]["channels"]["console"]["operator_actions"] == []
    assert payload["channel_contract"]["channels"]["feishu"]["desired_state"] == "running"
    assert payload["channel_contract"]["channels"]["feishu"]["diagnostic_health"] == "idle"
    assert payload["channel_contract"]["routing_policy"]["policy_version"] == "r2-f.v0"
    assert payload["channel_contract"]["orchestration"]["policy_version"] == "r2-f.v0"
    assert payload["boundary_policy"]["contract_version"] == "r1-f.v6"
    assert payload["boundary_policy"]["shell"]["backend_requested"] == "native"
    assert payload["boundary_policy"]["shell"]["backend_selected"] == "native"
    assert payload["boundary_policy"]["shell"]["stronger_backend_available"] is False
    assert payload["boundary_policy"]["shell"]["container_image_configured"] is False
    assert payload["boundary_policy"]["shell"]["primary_container_target"]["backend"] == "docker"
    assert (
        payload["boundary_policy"]["shell"]["primary_container_target"]["status"]
        == "missing_container_image"
    )
    assert (
        payload["boundary_policy"]["shell"]["primary_container_target"]["verify_command"]
        == "nanoclaw container-check --backend docker --refresh"
    )
    assert (
        payload["boundary_policy"]["shell"]["primary_container_target"]["prepare_command"]
        == ""
    )
    assert (
        payload["boundary_policy"]["shell"]["primary_container_target"]["runtime_command"]
        == ""
    )
    assert payload["boundary_policy"]["web_hosts"]["allowed_hosts_count"] == 1
    assert payload["boundary_policy"]["web_hosts"]["blocked_hosts_count"] == 1
    assert payload["boundary_policy"]["secrets"]["web_search_capability_count"] == 2
    assert payload["boundary_metrics"]["boundary"]["blocked"] == 1
    assert payload["boundary_metrics"]["secrets"]["granted"] == 1
    assert payload["queue"]["ready_backlog"] == 1
    assert payload["queue"]["retry_backlog"] == 0
    assert payload["queue"]["rate_limited_backlog"] == 0
    assert payload["queue"]["dead_letter_tasks"] == 0
    assert payload["queue"]["starved_ready_tasks"] == 0
    assert payload["queue"]["stale_running_tasks"] == 0
    assert payload["queue"]["running_workers"] == 0
    assert payload["queue"]["global_saturation_pct"] == 0
    assert payload["queue"]["lease_timeout_seconds"] == 45
    assert payload["queue"]["heartbeat_interval_seconds"] == 10
    assert payload["queue"]["stall_threshold_seconds"] == 120
    assert payload["queue"]["alert_channel"] == "auto"
    assert payload["queue"]["alert_escalation_channel"] == "auto-secondary"
    assert payload["queue"]["alert_cooldown_seconds"] == 300
    assert payload["queue"]["alert_escalate_after"] == 2
    assert payload["queue"]["schedule_alert_retrying_after"] == 2
    assert payload["queue"]["schedule_alert_escalate_after"] == 3
    assert payload["runtime_health"]["status"] == "healthy"
    assert payload["runtime_health"]["summary"] == "healthy"
    assert payload["runtime_health"]["base_alert_severity"] == "none"
    assert payload["workflow_eval_today"]["evaluations"] == 1
    assert payload["workflow_eval_today"]["good_runs"] == 1
    assert payload["workflow_eval_today"]["positive_feedback"] == 1
    assert payload["workflow_eval_today"]["avg_quality_score"] >= 80
    assert (
        payload["recent_workflow_evaluations"][0]["workflow_run_id"]
        == evaluations[0]["workflow_run_id"]
    )
    assert payload["recent_workflow_evaluations"][0]["workflow_name"] == "wechat_article_flow"
    assert payload["recent_workflow_evaluations"][0]["evaluation_label"] == "good"
    assert payload["recent_workflow_evaluations"][0]["feedback_signal"] == "positive"
    assert payload["workflow_recommendations"][0]["workflow_name"] == "wechat_article_flow"
    assert payload["recent_workflows"][0]["role_chain"] == [
        "planner@pre_llm",
        "editor[summarizer]@post_tools",
    ]
    assert payload["recent_workflows"][0]["shared_evidence_refs"] == ["ev_1"]
    assert payload["recent_tasks"][0]["task_id"] == created["task_id"]
    assert payload["recent_tasks"][0]["status"] == "pending"


async def test_dashboard_channels_api_returns_operator_contract() -> None:
    """Dashboard should expose the operator-facing channel contract."""
    dashboard = Dashboard(_dashboard_config(), FakeChannelGateway())
    request = make_mocked_request("GET", "/api/channels")

    response = await dashboard._api_channels(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload["contract_version"] == "r2-f.v0"
    assert payload["channels"]["telegram"]["status"] == "running"
    assert payload["channels"]["telegram"]["operator_actions"] == ["restart", "stop"]
    assert payload["channels"]["telegram"]["diagnostic_health"] == "healthy"
    assert payload["channels"]["telegram"]["diagnostics"]["outgoing_total"] == 1
    assert payload["channels"]["telegram"]["desired_state"] == "running"
    assert payload["channels"]["telegram"]["drift_status"] == "in_sync"
    assert payload["channels"]["feishu"]["status"] == "stopped"
    assert payload["channels"]["feishu"]["operator_actions"] == ["start"]
    assert payload["channels"]["feishu"]["diagnostic_health"] == "stopped"
    assert payload["channels"]["feishu"]["desired_state"] == "stopped"


async def test_dashboard_channel_action_api_updates_runtime_state() -> None:
    """Dashboard should run one channel operator action through the gateway."""
    dashboard = Dashboard(_dashboard_config(), FakeChannelGateway())
    request = make_mocked_request(
        "POST",
        "/api/channels/feishu/action",
        match_info={"channel_name": "feishu"},
    )
    request._payload = None
    request._read_bytes = json.dumps({"action": "start"}).encode()
    request._headers = {"Content-Type": "application/json"}

    response = await dashboard._api_channel_action(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload["action"] == "start"
    assert payload["contract_version"] == "r2-f.v0"
    assert payload["channel_name"] == "feishu"
    assert payload["channel"]["status"] == "running"
    assert payload["channel"]["detail"] == "webhook runtime active"
    assert payload["channel"]["operator_actions"] == ["restart", "stop"]
    assert payload["channel"]["diagnostic_health"] == "idle"
    assert payload["channel"]["desired_state"] == "running"
    assert payload["channel"]["drift_status"] == "in_sync"
    assert payload["summary"]["running_count"] == 2


async def test_dashboard_channel_desired_state_api_updates_runtime_state() -> None:
    """Dashboard should persist desired state through the gateway."""
    dashboard = Dashboard(_dashboard_config(), FakeChannelGateway())
    request = make_mocked_request(
        "POST",
        "/api/channels/feishu/desired-state",
        match_info={"channel_name": "feishu"},
    )
    request._payload = None
    request._read_bytes = json.dumps(
        {"desired_state": "running", "reconcile": True}
    ).encode()
    request._headers = {"Content-Type": "application/json"}

    response = await dashboard._api_channel_desired_state(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload["desired_state"] == "running"
    assert payload["reconcile"] is True
    assert payload["contract_version"] == "r2-f.v0"
    assert payload["channel_name"] == "feishu"
    assert payload["channel"]["status"] == "running"
    assert payload["channel"]["desired_state"] == "running"
    assert payload["channel"]["drift_status"] == "in_sync"
    assert payload["summary"]["running_count"] == 2


async def test_dashboard_workflow_evaluations_api_returns_recent_entries(
    tmp_path,
    monkeypatch,
) -> None:
    """Dashboard should expose recent workflow evaluations."""
    audit = AuditLog(tmp_path / "tasks.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    await audit.log_workflow_run(
        session_id="cli:user",
        workflow_name="grounded_current_info",
        workflow_tags=["grounded_current_info"],
        user_summary="show me live news",
        status="degraded",
        failure_reason="provider_timeout",
        total_tokens=2200,
        execution_ms=7500,
        llm_calls=3,
        tool_calls=2,
        final_model="gpt-5.2",
        call_chain=[
            {"type": "llm", "model": "gpt-5.2", "status": "success"},
            {"type": "tool", "name": "web_search", "status": "timeout"},
        ],
    )

    dashboard = Dashboard(_dashboard_config(), SimpleNamespace(channels={}))
    request = make_mocked_request("GET", "/api/workflow-evaluations?limit=5")

    response = await dashboard._api_workflow_evaluations(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload[0]["workflow_run_id"] > 0
    assert payload[0]["workflow_name"] == "grounded_current_info"
    assert payload[0]["evaluation_label"] in {"review", "poor"}
    assert payload[0]["suggestions"]


async def test_dashboard_workflow_feedback_api_updates_signal(
    tmp_path,
    monkeypatch,
) -> None:
    """Dashboard should expose a feedback write endpoint for workflow evaluations."""
    audit = AuditLog(tmp_path / "tasks.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
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
    evaluation = (await audit.get_recent_workflow_evaluations(limit=1))[0]

    dashboard = Dashboard(_dashboard_config(), SimpleNamespace(channels={}))
    request = make_mocked_request(
        "POST",
        f"/api/workflow-evaluations/{evaluation['workflow_run_id']}/feedback",
        match_info={"workflow_run_id": str(evaluation["workflow_run_id"])},
    )
    request._payload = None
    request._read_bytes = json.dumps({"feedback": "negative"}).encode()
    request._headers = {"Content-Type": "application/json"}

    response = await dashboard._api_workflow_feedback(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload["workflow_run_id"] == evaluation["workflow_run_id"]
    assert payload["feedback_signal"] == "negative"


async def test_dashboard_workflow_recommendations_api_returns_entries(
    tmp_path,
    monkeypatch,
) -> None:
    """Dashboard should expose aggregated workflow recommendations."""
    audit = AuditLog(tmp_path / "tasks.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
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

    dashboard = Dashboard(_dashboard_config(), SimpleNamespace(channels={}))
    request = make_mocked_request("GET", "/api/workflow-recommendations?days=7&limit=5")

    response = await dashboard._api_workflow_recommendations(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload[0]["workflow_name"] == "grounded_current_info"
    assert payload[0]["recommendation_status"] in {"attention", "optimize"}
    assert payload[0]["recommendations"]


async def test_dashboard_workflow_roles_api_returns_role_replay(
    tmp_path,
    monkeypatch,
) -> None:
    """Dashboard should expose role-level workflow replay."""
    audit = AuditLog(tmp_path / "tasks.db")
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
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
                "role_label": "planner",
                "stage": "pre_llm",
                "checkpoint_id": "planner@pre_llm",
                "status": "attached",
                "workflow_name": "grounded_current_info",
                "handler_kind": "execution_brief",
                "artifact_preview": "Plan checkpoints and execution order.",
                "contract": {},
                "evidence_refs": [],
            },
            {
                "type": "workflow_role_execution",
                "role": "critic",
                "role_label": "critic",
                "stage": "post_tools",
                "checkpoint_id": "critic@post_tools",
                "status": "attached",
                "workflow_name": "grounded_current_info",
                "handler_kind": "execution_brief",
                "artifact_preview": "Review evidence quality and gaps.",
                "contract": {},
                "evidence_refs": ["ev_1"],
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
                "source_workflow_run_id": 7,
                "source_workflow_name": "grounded_current_info",
                "source_status": "degraded",
                "failure_reason": "paper_search:error",
                "restored_evidence_count": 1,
                "status": "resumed",
                "evidence_refs": ["ev_1"],
            },
        ],
    )
    workflow = (await audit.get_recent_workflows(limit=1))[0]

    dashboard = Dashboard(_dashboard_config(), SimpleNamespace(channels={}))
    request = make_mocked_request(
        "GET",
        f"/api/workflows/{workflow['id']}/roles",
        match_info={"workflow_run_id": str(workflow["id"])},
    )

    response = await dashboard._api_workflow_roles(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload["id"] == workflow["id"]
    assert payload["workflow_name"] == "grounded_current_info"
    assert payload["shared_evidence_refs"] == ["ev_1"]
    assert payload["role_checkpoint_timeline"][0]["checkpoint_id"] == "planner@pre_llm"
    assert payload["role_execution_timeline"][0]["role"] == "planner"
    assert payload["role_execution_timeline"][0]["role_label"] == "planner"
    assert payload["role_execution_timeline"][0]["checkpoint_id"] == "planner@pre_llm"
    assert payload["role_execution_timeline"][0]["artifact_preview"] == (
        "Plan checkpoints and execution order."
    )
    assert payload["role_task_timeline"][0]["task_key"] == "executor@tool_phase"
    assert payload["role_task_timeline"][0]["role_label"] == "executor"
    assert payload["role_task_timeline"][0]["depends_on"] == ["router@pre_llm"]
    assert payload["role_task_bridge_timeline"][0]["task_type"] == "workflow_role"
    assert payload["role_task_bridge_timeline"][0]["role_label"] == "executor"
    assert payload["role_task_bridge_timeline"][0]["payload"]["parent_task_id"] == "task_parent_1"
    assert payload["role_recovery_timeline"][0]["failed_role"] == "executor"
    assert payload["role_recovery_timeline"][0]["resume_checkpoint_id"] == "router@pre_llm"
    assert payload["role_recovery_timeline"][0]["status"] == "resumed"
    assert payload["role_resume_timeline"][0]["source_workflow_run_id"] == 7
    assert payload["role_resume_timeline"][0]["restored_evidence_count"] == 1


async def test_dashboard_task_cancel_api_requests_cancellation(tmp_path) -> None:
    """Dashboard should expose a task cancellation endpoint."""
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)
    created = await store.create_task("cancel from dashboard", source="spawn_task")

    dashboard = Dashboard(_dashboard_config(), SimpleNamespace(channels={}))
    request = make_mocked_request(
        "POST",
        f"/api/tasks/{created['task_id']}/cancel",
        match_info={"task_id": created["task_id"]},
    )

    response = await dashboard._api_task_cancel(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload["task_id"] == created["task_id"]
    assert payload["status"] == "cancelled"
    assert payload["cancel_requested"] is True


async def test_dashboard_task_requeue_api_resets_dead_letter_task(tmp_path) -> None:
    """Dashboard should expose a task requeue endpoint for dead-letter tasks."""
    store = TaskStore(tmp_path / "tasks.db")
    set_task_store(store)
    created = await store.create_task("requeue from dashboard", source="spawn_task", max_attempts=1)
    await store.claim_next_task(source="spawn_task", worker_id="worker-a")
    await store.fail_task_attempt(created["task_id"], last_error="fatal")

    dashboard = Dashboard(_dashboard_config(), SimpleNamespace(channels={}))
    request = make_mocked_request(
        "POST",
        f"/api/tasks/{created['task_id']}/requeue",
        match_info={"task_id": created["task_id"]},
    )

    response = await dashboard._api_task_requeue(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload["task_id"] == created["task_id"]
    assert payload["status"] == "pending"
    assert payload["dead_lettered"] is False


async def test_dashboard_task_replay_api_returns_structured_trace(
    tmp_path,
    monkeypatch,
) -> None:
    """Dashboard should expose a structured replay payload for one task."""
    db_path = tmp_path / "tasks.db"
    store = TaskStore(db_path)
    audit = AuditLog(db_path)
    set_task_store(store)
    monkeypatch.setattr("nanoclaw.security.audit.get_audit_log", lambda: audit)
    created = await store.create_task("replay from dashboard", source="spawn_task")
    await store.claim_next_task(source="spawn_task", worker_id="worker-a")
    await store.start_task_step(
        created["task_id"],
        "agent_run",
        step_name="agent_run",
        input_payload={"task_description": "replay from dashboard"},
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
        execution_ms=33,
    )
    await audit.log_tool_trace(
        task_id=created["task_id"],
        session_id=f"task:{created['task_id']}",
        step_id="agent_run",
        attempt_number=1,
        tool_name="web_search",
        output_summary="done",
        status="success",
        execution_ms=11,
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

    dashboard = Dashboard(_dashboard_config(), SimpleNamespace(channels={}))
    request = make_mocked_request(
        "GET",
        f"/api/tasks/{created['task_id']}/replay",
        match_info={"task_id": created["task_id"]},
    )

    response = await dashboard._api_task_replay(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload["task"]["task_id"] == created["task_id"]
    assert payload["steps"][0]["step_id"] == "agent_run"
    assert payload["task_runs"][0]["status"] == "success"
    assert payload["tool_traces"][0]["tool_name"] == "web_search"
    assert payload["audit_events"][0]["action_type"] == "boundary_decision"


async def test_dashboard_cron_groups_api_returns_grouped_jobs(monkeypatch) -> None:
    """Dashboard should group cron jobs by channel and target scope."""
    scheduler = DummyCronScheduler(
        [
            {
                "id": 1,
                "name": "Morning AI",
                "message": "Send the AI morning summary",
                "cron_expr": "30 8 * * 1-5",
                "interval_seconds": None,
                "channel": "feishu",
                "target_id": "oc_chat_alpha",
                "quiet_start": "22:00",
                "quiet_end": "08:00",
                "last_run": "2026-03-08 08:30:00",
                "enabled": 1,
                "created_at": "2026-03-07 08:00:00",
                "runtime": {
                    "health": "retrying",
                    "health_reason": "delivery retry pending",
                    "notify_kind": "cron_delivery_retry_scheduled",
                    "last_execution": {
                        "task_id": "task-cron-1",
                        "status": "succeeded",
                        "updated_at": "2026-03-08 08:30:05",
                    },
                    "last_delivery_retry": {
                        "task_id": "task-delivery-1",
                        "status": "pending",
                        "updated_at": "2026-03-08 08:31:10",
                    },
                    "signal_timeline": [
                        {
                            "label": "alert",
                            "detail": "retrying_initial x2",
                            "timestamp": "2026-03-08 08:31:10",
                        },
                        {
                            "label": "recovery",
                            "detail": "healthy after retrying_initial",
                            "timestamp": "2026-03-08 09:00:00",
                        },
                    ],
                },
            },
            {
                "id": 2,
                "name": "Paper watch",
                "message": "Monitor new video generation papers",
                "cron_expr": "0 9 * * 1-5",
                "interval_seconds": None,
                "channel": "feishu",
                "target_id": "oc_chat_alpha",
                "quiet_start": "",
                "quiet_end": "",
                "last_run": None,
                "enabled": 0,
                "created_at": "2026-03-07 09:00:00",
            },
            {
                "id": 3,
                "name": "Global digest",
                "message": "Summarize headlines",
                "cron_expr": "",
                "interval_seconds": 3600,
                "channel": "telegram",
                "target_id": "",
                "quiet_start": "",
                "quiet_end": "",
                "last_run": None,
                "enabled": 1,
                "created_at": "2026-03-07 10:00:00",
            },
        ]
    )
    monkeypatch.setattr("nanoclaw.cron.scheduler.get_scheduler", lambda: scheduler)

    dashboard = Dashboard(_dashboard_config(), SimpleNamespace(channels={}))
    request = make_mocked_request("GET", "/api/cron/groups")

    response = await dashboard._api_cron_groups(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert len(payload) == 2
    assert payload[0]["target_label"] == "feishu:oc_chat_alpha"
    assert payload[0]["job_count"] == 2
    assert payload[0]["enabled_jobs"] == 1
    assert payload[0]["jobs"][0]["quiet_window"] == "22:00-08:00"
    assert payload[0]["jobs"][0]["health"] == "retrying"
    assert payload[0]["jobs"][0]["last_notify_kind"] == "cron_delivery_retry_scheduled"
    assert payload[0]["jobs"][0]["last_delivery_retry_status"] == "pending"
    assert payload[0]["jobs"][0]["last_signal_label"] == "alert"
    assert payload[0]["jobs"][0]["signal_timeline"][0]["detail"] == "retrying_initial x2"
    assert payload[0]["jobs"][1]["quiet_window"] == "off"
    assert payload[1]["target_label"] == "telegram:default"
    assert payload[1]["jobs"][0]["schedule_text"] == "every 3600s"


async def test_dashboard_cron_groups_support_health_and_signal_filters(monkeypatch) -> None:
    """Dashboard cron groups should filter jobs by health and recent signal."""
    scheduler = DummyCronScheduler(
        [
            {
                "id": 1,
                "name": "Attention job",
                "message": "Needs attention",
                "cron_expr": "30 8 * * 1-5",
                "interval_seconds": None,
                "channel": "feishu",
                "target_id": "oc_chat_alpha",
                "quiet_start": "",
                "quiet_end": "",
                "last_run": "2026-03-08 08:30:00",
                "enabled": 1,
                "created_at": "2026-03-07 08:00:00",
                "runtime": {
                    "health": "attention",
                    "health_reason": "latest execution failed",
                    "signal_timeline": [
                        {
                            "label": "escalation",
                            "detail": "attention_escalated x2",
                            "timestamp": "2026-03-08 08:31:10",
                        }
                    ],
                },
            },
            {
                "id": 2,
                "name": "Healthy job",
                "message": "Already recovered",
                "cron_expr": "0 9 * * 1-5",
                "interval_seconds": None,
                "channel": "feishu",
                "target_id": "oc_chat_alpha",
                "quiet_start": "",
                "quiet_end": "",
                "last_run": "2026-03-08 09:00:00",
                "enabled": 1,
                "created_at": "2026-03-07 09:00:00",
                "runtime": {
                    "health": "healthy",
                    "health_reason": "latest execution succeeded",
                    "signal_timeline": [
                        {
                            "label": "recovery",
                            "detail": "healthy after attention_initial",
                            "timestamp": "2026-03-08 09:00:00",
                        }
                    ],
                },
            },
        ]
    )
    monkeypatch.setattr("nanoclaw.cron.scheduler.get_scheduler", lambda: scheduler)

    dashboard = Dashboard(_dashboard_config(), SimpleNamespace(channels={}))
    request = make_mocked_request("GET", "/api/cron/groups?health=attention&signal=escalation")

    response = await dashboard._api_cron_groups(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert len(payload) == 1
    assert payload[0]["target_label"] == "feishu:oc_chat_alpha"
    assert payload[0]["job_count"] == 1
    assert payload[0]["jobs"][0]["name"] == "Attention job"
    assert payload[0]["jobs"][0]["health"] == "attention"
    assert payload[0]["jobs"][0]["last_signal_label"] == "escalation"


async def test_dashboard_cron_toggle_api_updates_job_enabled_state(monkeypatch) -> None:
    """Dashboard should pause or resume cron jobs through the scheduler."""
    scheduler = DummyCronScheduler(
        [
            {
                "id": 7,
                "name": "Night digest",
                "message": "Send the nightly digest",
                "cron_expr": "0 21 * * *",
                "interval_seconds": None,
                "channel": "feishu",
                "target_id": "oc_chat_beta",
                "quiet_start": "",
                "quiet_end": "",
                "last_run": None,
                "enabled": 1,
                "created_at": "2026-03-07 21:00:00",
            }
        ]
    )
    monkeypatch.setattr("nanoclaw.cron.scheduler.get_scheduler", lambda: scheduler)

    class JsonRequest:
        """Tiny request stub with JSON support."""

        def __init__(self, payload: dict, match_info: dict[str, str]) -> None:
            self._payload = payload
            self.match_info = match_info

        async def json(self) -> dict:
            """Return the stored JSON payload."""
            return self._payload

    dashboard = Dashboard(_dashboard_config(), SimpleNamespace(channels={}))
    request = JsonRequest({"enabled": False}, {"id": "7"})

    response = await dashboard._api_cron_toggle(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload["id"] == 7
    assert payload["enabled"] is False
    assert scheduler.jobs[0]["enabled"] == 0


async def test_dashboard_cron_group_action_toggles_filtered_jobs(monkeypatch) -> None:
    """Dashboard should batch-pause only visible jobs in one filtered group."""
    scheduler = DummyCronScheduler(
        [
            {
                "id": 1,
                "name": "Alpha attention",
                "message": "Needs attention",
                "cron_expr": "30 8 * * 1-5",
                "interval_seconds": None,
                "channel": "feishu",
                "target_id": "oc_chat_alpha",
                "quiet_start": "",
                "quiet_end": "",
                "last_run": None,
                "enabled": 1,
                "created_at": "2026-03-07 08:00:00",
                "runtime": {"health": "attention"},
            },
            {
                "id": 2,
                "name": "Alpha healthy",
                "message": "All good",
                "cron_expr": "0 9 * * 1-5",
                "interval_seconds": None,
                "channel": "feishu",
                "target_id": "oc_chat_alpha",
                "quiet_start": "",
                "quiet_end": "",
                "last_run": None,
                "enabled": 1,
                "created_at": "2026-03-07 09:00:00",
                "runtime": {"health": "healthy"},
            },
            {
                "id": 3,
                "name": "Beta attention",
                "message": "Other group",
                "cron_expr": "0 10 * * 1-5",
                "interval_seconds": None,
                "channel": "feishu",
                "target_id": "oc_chat_beta",
                "quiet_start": "",
                "quiet_end": "",
                "last_run": None,
                "enabled": 1,
                "created_at": "2026-03-07 10:00:00",
                "runtime": {"health": "attention"},
            },
        ]
    )
    monkeypatch.setattr("nanoclaw.cron.scheduler.get_scheduler", lambda: scheduler)

    class JsonRequest:
        """Tiny request stub with JSON support."""

        def __init__(self, payload: dict) -> None:
            self._payload = payload

        async def json(self) -> dict:
            """Return the stored JSON payload."""
            return self._payload

    dashboard = Dashboard(_dashboard_config(), SimpleNamespace(channels={}))
    request = JsonRequest(
        {
            "group_key": "feishu::oc_chat_alpha",
            "action": "pause",
            "health": "attention",
            "signal": "all",
        }
    )

    response = await dashboard._api_cron_group_action(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload["action"] == "pause"
    assert payload["count"] == 1
    assert payload["ids"] == [1]
    assert scheduler.jobs[0]["enabled"] == 0
    assert scheduler.jobs[1]["enabled"] == 1
    assert scheduler.jobs[2]["enabled"] == 1


async def test_dashboard_cron_group_action_removes_filtered_jobs(monkeypatch) -> None:
    """Dashboard should batch-remove only visible jobs in one filtered group."""
    scheduler = DummyCronScheduler(
        [
            {
                "id": 4,
                "name": "Alpha recovered",
                "message": "Recovered",
                "cron_expr": "30 8 * * 1-5",
                "interval_seconds": None,
                "channel": "feishu",
                "target_id": "oc_chat_alpha",
                "quiet_start": "",
                "quiet_end": "",
                "last_run": None,
                "enabled": 1,
                "created_at": "2026-03-07 08:00:00",
                "runtime": {
                    "health": "healthy",
                    "signal_timeline": [{"label": "recovery", "detail": "healthy again"}],
                },
            },
            {
                "id": 5,
                "name": "Alpha alerting",
                "message": "Alerting",
                "cron_expr": "0 9 * * 1-5",
                "interval_seconds": None,
                "channel": "feishu",
                "target_id": "oc_chat_alpha",
                "quiet_start": "",
                "quiet_end": "",
                "last_run": None,
                "enabled": 1,
                "created_at": "2026-03-07 09:00:00",
                "runtime": {
                    "health": "attention",
                    "signal_timeline": [{"label": "alert", "detail": "attention_initial x1"}],
                },
            },
            {
                "id": 6,
                "name": "Beta recovered",
                "message": "Other group",
                "cron_expr": "0 10 * * 1-5",
                "interval_seconds": None,
                "channel": "feishu",
                "target_id": "oc_chat_beta",
                "quiet_start": "",
                "quiet_end": "",
                "last_run": None,
                "enabled": 1,
                "created_at": "2026-03-07 10:00:00",
                "runtime": {
                    "health": "healthy",
                    "signal_timeline": [{"label": "recovery", "detail": "healthy again"}],
                },
            },
        ]
    )
    monkeypatch.setattr("nanoclaw.cron.scheduler.get_scheduler", lambda: scheduler)

    class JsonRequest:
        """Tiny request stub with JSON support."""

        def __init__(self, payload: dict) -> None:
            self._payload = payload

        async def json(self) -> dict:
            """Return the stored JSON payload."""
            return self._payload

    dashboard = Dashboard(_dashboard_config(), SimpleNamespace(channels={}))
    request = JsonRequest(
        {
            "group_key": "feishu::oc_chat_alpha",
            "action": "remove",
            "health": "all",
            "signal": "recovery",
        }
    )

    response = await dashboard._api_cron_group_action(request)
    assert response.status == 200

    payload = json.loads(response.text)
    assert payload["action"] == "remove"
    assert payload["count"] == 1
    assert payload["ids"] == [4]
    assert [int(item["id"]) for item in scheduler.jobs] == [5, 6]
