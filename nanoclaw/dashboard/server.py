"""Lightweight admin dashboard with aiohttp.
运维可视化层：读状态、读记忆、看审计、管 cron。
1、提供本地运维面板和 API。
2、查看状态、记忆、审计、技能列表。
3、在线管理 cron 任务（增删查）。
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import aiohttp.web

from nanoclaw.core.logger import get_logger

if TYPE_CHECKING:
    from nanoclaw.channels.gateway import Gateway
    from nanoclaw.core.config import Config

logger = get_logger(__name__)


def _format_role_display(role: str, role_label: str) -> str:
    """Return one compact dashboard-facing role label."""
    if role_label and role_label != role:
        return f"{role_label}[{role}]"
    return role or role_label or "-"


class Dashboard:
    """
    Lightweight admin dashboard.
    Single HTML page served by aiohttp.
    Localhost only by default.
    """

    def __init__(self, config: "Config", gateway: "Gateway"):
        """
        Initialize Dashboard.

        Args:
            config: Application configuration
            gateway: Gateway instance
        """
        self.config = config
        self.gateway = gateway
        self._password: Optional[str] = config.dashboard.password
        self.app = aiohttp.web.Application(
            client_max_size=1024 * 1024,  # 1 MB
            middlewares=[self._auth_middleware],
        )
        self.runner: Optional[aiohttp.web.AppRunner] = None
        self._start_time = time.time()
        self._setup_routes()

    @aiohttp.web.middleware
    async def _auth_middleware(
        self, request: aiohttp.web.Request, handler
    ) -> aiohttp.web.StreamResponse:
        """Bearer token auth middleware for /api/* endpoints."""
        if self._password and request.path.startswith("/api/"):
            auth_header = request.headers.get("Authorization", "")
            expected = f"Bearer {self._password}"
            if not secrets.compare_digest(auth_header, expected):
                return aiohttp.web.json_response(
                    {"error": "Unauthorized"}, status=401
                )
        return await handler(request)

    def _setup_routes(self) -> None:
        """Set up HTTP routes."""
        self.app.router.add_get("/", self._serve_html)
        self.app.router.add_get("/api/status", self._api_status)
        self.app.router.add_get("/api/channels", self._api_channels)
        self.app.router.add_post(
            "/api/channels/{channel_name}/action",
            self._api_channel_action,
        )
        self.app.router.add_post(
            "/api/channels/{channel_name}/desired-state",
            self._api_channel_desired_state,
        )
        self.app.router.add_get("/api/memory", self._api_memory)
        self.app.router.add_get("/api/audit", self._api_audit)
        self.app.router.add_get("/api/workflows", self._api_workflows)
        self.app.router.add_get("/api/workflows/{workflow_run_id}/roles", self._api_workflow_roles)
        self.app.router.add_get("/api/workflow-evaluations", self._api_workflow_evaluations)
        self.app.router.add_get("/api/workflow-recommendations", self._api_workflow_recommendations)
        self.app.router.add_post(
            "/api/workflow-evaluations/{workflow_run_id}/feedback",
            self._api_workflow_feedback,
        )
        self.app.router.add_get("/api/tasks", self._api_tasks)
        self.app.router.add_get("/api/tasks/{task_id}/replay", self._api_task_replay)
        self.app.router.add_post("/api/tasks/{task_id}/cancel", self._api_task_cancel)
        self.app.router.add_post("/api/tasks/{task_id}/requeue", self._api_task_requeue)
        self.app.router.add_get("/api/cron", self._api_cron_list)
        self.app.router.add_get("/api/cron/groups", self._api_cron_groups)
        self.app.router.add_post("/api/cron", self._api_cron_add)
        self.app.router.add_post("/api/cron/groups/action", self._api_cron_group_action)
        self.app.router.add_post("/api/cron/{id}/toggle", self._api_cron_toggle)
        self.app.router.add_delete("/api/cron/{id}", self._api_cron_remove)
        self.app.router.add_get("/api/skills", self._api_skills)

    async def start(self, port: int = 18790) -> None:
        """
        Start dashboard server.

        Args:
            port: Port to listen on
        """
        self.runner = aiohttp.web.AppRunner(self.app)
        await self.runner.setup()

        # LOCALHOST ONLY - not accessible from outside
        site = aiohttp.web.TCPSite(self.runner, "127.0.0.1", port)
        await site.start()

        logger.info(f"Dashboard: http://localhost:{port}")
        if self._password:
            masked = self._password[:4] + "****" if len(self._password) > 4 else "****"
            logger.info(f"Dashboard auth enabled (token: {masked})")
        else:
            logger.warning("Dashboard has no password set — API is unauthenticated")

    async def _serve_html(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Serve the single-page dashboard HTML."""
        html_path = Path(__file__).parent / "index.html"
        if html_path.exists():
            return aiohttp.web.FileResponse(html_path)  # type: ignore[return-value]
        return aiohttp.web.Response(
            text="Dashboard HTML not found", status=404
        )

    async def _api_status(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Return current status as JSON."""
        from nanoclaw.memory.store import get_memory_store
        from nanoclaw.security.audit import get_audit_log
        from nanoclaw.channels.contract import build_channel_contract
        from nanoclaw.security.policy_contract import build_boundary_policy_contract
        from nanoclaw.runtime.tasks import get_task_store
        from nanoclaw.tools.spawn import (
            get_background_runtime_metrics,
            summarize_runtime_health,
        )

        try:
            memory = get_memory_store()
            stats = await memory.get_stats()
        except Exception:
            stats = {}

        try:
            audit = get_audit_log()
            today = await audit.get_stats_today()
            boundary_metrics = await audit.get_boundary_metrics(window_hours=24)
            workflow_today = await audit.get_workflow_stats_today()
            workflow_eval_today = await audit.get_workflow_evaluation_stats_today()
            recent_workflows = await audit.get_recent_workflows(limit=5)
            recent_workflow_evaluations = await audit.get_recent_workflow_evaluations(limit=5)
            workflow_recommendations = await audit.get_workflow_recommendations(limit=5)
            serper_usage = None
            if self.config.tools.web_search.serper_max_calls > 0:
                serper_usage = await audit.get_provider_usage(
                    "serper",
                    self.config.tools.web_search.serper_max_calls,
                )
        except Exception:
            today = {}
            boundary_metrics = {}
            workflow_today = {}
            workflow_eval_today = {}
            recent_workflows = []
            recent_workflow_evaluations = []
            workflow_recommendations = []
            serper_usage = None

        try:
            task_store = get_task_store()
            recent_tasks = await task_store.list_tasks(limit=5)
            runtime_metrics = get_background_runtime_metrics()
            queue_metrics = await task_store.get_queue_metrics(
                starvation_threshold_seconds=runtime_metrics["starvation_threshold_seconds"],
                lease_timeout_seconds=runtime_metrics["lease_timeout_seconds"],
                stall_threshold_seconds=runtime_metrics["stall_threshold_seconds"],
            )
            runtime_health = summarize_runtime_health(queue_metrics)
            capacity = max(1, int(runtime_metrics["capacity"]))
            queue_metrics = {
                **queue_metrics,
                "global_saturation_pct": int(
                    (int(queue_metrics["running_tasks"]) / capacity) * 100
                ),
            }
        except Exception:
            recent_tasks = []
            queue_metrics = {}
            runtime_metrics = {}
            runtime_health = {
                "status": "unknown",
                "reasons": [],
                "summary": "unknown",
                "base_alert_severity": "none",
            }

        uptime = int(time.time() - self._start_time)
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)

        channel_contract = build_channel_contract(self.config, self.gateway)

        return aiohttp.web.json_response(
            {
                "status": "online",
                "uptime": f"{hours}h {minutes}m {seconds}s",
                "channels": channel_contract["channels"],
                "channel_contract": channel_contract,
                "model": self.config.get_default_model(),
                "stats": stats,
                "today": today,
                "boundary_policy": build_boundary_policy_contract(self.config),
                "boundary_metrics": boundary_metrics,
                "workflow_today": workflow_today,
                "workflow_eval_today": workflow_eval_today,
                "provider_usage": {"serper": serper_usage} if serper_usage else {},
                "queue": {
                    **queue_metrics,
                    **runtime_metrics,
                },
                "runtime_health": runtime_health,
                "recent_workflows": [
                    self._compact_workflow_entry(item) for item in recent_workflows
                ],
                "recent_workflow_evaluations": [
                    self._compact_workflow_evaluation_entry(item)
                    for item in recent_workflow_evaluations
                ],
                "workflow_recommendations": [
                    self._compact_workflow_recommendation_entry(item)
                    for item in workflow_recommendations
                ],
                "recent_tasks": [
                    self._compact_task_entry(item) for item in recent_tasks
                ],
            }
        )

    async def _api_channels(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Return the operator-facing channel contract."""
        from nanoclaw.channels.contract import build_channel_contract

        return aiohttp.web.json_response(
            build_channel_contract(self.config, self.gateway)
        )

    async def _api_channel_action(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Run one operator action against a managed channel."""
        from nanoclaw.channels.contract import build_channel_contract

        try:
            data = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "Invalid JSON"}, status=400)

        channel_name = str(request.match_info.get("channel_name") or "").strip().lower()
        action = str(data.get("action") or "").strip().lower()
        if not channel_name:
            return aiohttp.web.json_response({"error": "channel_name required"}, status=400)
        if not action:
            return aiohttp.web.json_response({"error": "action required"}, status=400)
        if not hasattr(self.gateway, "run_channel_action"):
            return aiohttp.web.json_response(
                {"error": "Gateway runtime does not support channel control"},
                status=503,
            )

        try:
            runtime = await self.gateway.run_channel_action(channel_name, action)
        except ValueError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=400)
        except RuntimeError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=409)

        contract = build_channel_contract(self.config, self.gateway)
        channel_entry = dict(contract["channels"].get(channel_name) or {})
        return aiohttp.web.json_response(
            {
                "action": action,
                "channel_name": channel_name,
                "runtime": runtime,
                "contract_version": contract["contract_version"],
                "summary": contract["summary"],
                "channel": channel_entry,
            }
        )

    async def _api_channel_desired_state(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Persist one desired state for a managed channel."""
        from nanoclaw.channels.contract import build_channel_contract

        try:
            data = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "Invalid JSON"}, status=400)

        channel_name = str(request.match_info.get("channel_name") or "").strip().lower()
        desired_state = str(data.get("desired_state") or "").strip().lower()
        reconcile = bool(data.get("reconcile", True))
        if not channel_name:
            return aiohttp.web.json_response({"error": "channel_name required"}, status=400)
        if not desired_state:
            return aiohttp.web.json_response({"error": "desired_state required"}, status=400)
        if not hasattr(self.gateway, "set_channel_desired_state"):
            return aiohttp.web.json_response(
                {"error": "Gateway runtime does not support desired-state control"},
                status=503,
            )

        try:
            runtime = await self.gateway.set_channel_desired_state(
                channel_name,
                desired_state,
                reason="dashboard desired-state update",
                reconcile=reconcile,
            )
        except ValueError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=400)
        except RuntimeError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=409)

        contract = build_channel_contract(self.config, self.gateway)
        channel_entry = dict(contract["channels"].get(channel_name) or {})
        return aiohttp.web.json_response(
            {
                "channel_name": channel_name,
                "desired_state": desired_state,
                "reconcile": reconcile,
                "runtime": runtime,
                "contract_version": contract["contract_version"],
                "summary": contract["summary"],
                "channel": channel_entry,
            }
        )

    async def _api_memory(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Return all memories."""
        from nanoclaw.memory.store import get_memory_store

        memory = get_memory_store()
        memories = await memory.get_all_memories()
        return aiohttp.web.json_response(memories)

    async def _api_audit(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Return recent audit log entries."""
        from nanoclaw.security.audit import get_audit_log

        try:
            limit = int(request.query.get("limit", "50"))
        except (ValueError, TypeError):
            return aiohttp.web.json_response(
                {"error": "Invalid limit parameter"}, status=400
            )
        limit = max(1, min(limit, 500))

        audit = get_audit_log()
        entries = await audit.get_recent(limit=limit)
        return aiohttp.web.json_response(entries)

    async def _api_workflows(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Return recent workflow telemetry entries."""
        from nanoclaw.security.audit import get_audit_log

        try:
            limit = int(request.query.get("limit", "20"))
        except (ValueError, TypeError):
            return aiohttp.web.json_response(
                {"error": "Invalid limit parameter"}, status=400
            )
        limit = max(1, min(limit, 200))

        audit = get_audit_log()
        entries = await audit.get_recent_workflows(limit=limit)
        return aiohttp.web.json_response(entries)

    async def _api_workflow_roles(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Return one compact role-level replay for a workflow run."""
        from nanoclaw.security.audit import get_audit_log

        raw_run_id = str(request.match_info.get("workflow_run_id", "")).strip()
        if not raw_run_id:
            return aiohttp.web.json_response(
                {"error": "Missing workflow run ID"},
                status=400,
            )
        try:
            workflow_run_id = int(raw_run_id)
        except ValueError:
            return aiohttp.web.json_response(
                {"error": "Invalid workflow run ID"},
                status=400,
            )
        item = await get_audit_log().get_workflow_role_replay(workflow_run_id)
        if item is None:
            return aiohttp.web.json_response(
                {"error": f"Workflow run `{workflow_run_id}` not found."},
                status=404,
            )
        return aiohttp.web.json_response(item)

    async def _api_workflow_evaluations(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Return recent workflow evaluation entries."""
        from nanoclaw.security.audit import get_audit_log

        try:
            limit = int(request.query.get("limit", "20"))
        except (ValueError, TypeError):
            return aiohttp.web.json_response(
                {"error": "Invalid limit parameter"}, status=400
            )
        limit = max(1, min(limit, 200))

        audit = get_audit_log()
        entries = await audit.get_recent_workflow_evaluations(limit=limit)
        return aiohttp.web.json_response(entries)

    async def _api_workflow_feedback(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Persist one explicit workflow feedback signal."""
        from nanoclaw.security.audit import get_audit_log

        raw_run_id = str(request.match_info.get("workflow_run_id", "")).strip()
        if not raw_run_id:
            return aiohttp.web.json_response(
                {"error": "Missing workflow run ID"},
                status=400,
            )
        try:
            workflow_run_id = int(raw_run_id)
        except ValueError:
            return aiohttp.web.json_response(
                {"error": "Invalid workflow run ID"},
                status=400,
            )
        try:
            data = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "Invalid JSON"}, status=400)
        feedback_signal = str(data.get("feedback") or "").strip().lower()
        try:
            item = await get_audit_log().set_workflow_feedback(
                workflow_run_id,
                feedback_signal,
            )
        except KeyError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=404)
        except ValueError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=400)
        return aiohttp.web.json_response(
            self._compact_workflow_evaluation_entry(item)
        )

    async def _api_workflow_recommendations(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Return aggregated workflow recommendations."""
        from nanoclaw.security.audit import get_audit_log

        try:
            days = int(request.query.get("days", "7"))
            limit = int(request.query.get("limit", "10"))
        except (ValueError, TypeError):
            return aiohttp.web.json_response(
                {"error": "Invalid days or limit parameter"},
                status=400,
            )
        try:
            entries = await get_audit_log().get_workflow_recommendations(
                days=days,
                limit=limit,
            )
        except ValueError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=400)
        return aiohttp.web.json_response(
            [self._compact_workflow_recommendation_entry(item) for item in entries]
        )

    async def _api_tasks(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Return recent persisted task entries."""
        from nanoclaw.runtime.tasks import get_task_store

        try:
            limit = int(request.query.get("limit", "20"))
        except (ValueError, TypeError):
            return aiohttp.web.json_response(
                {"error": "Invalid limit parameter"}, status=400
            )
        limit = max(1, min(limit, 200))

        status_filter = request.query.get("status", "").strip().lower() or None
        try:
            entries = await get_task_store().list_tasks(limit=limit, status=status_filter)
        except ValueError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=400)

        return aiohttp.web.json_response(
            [self._compact_task_entry(item) for item in entries]
        )

    async def _api_task_cancel(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Request cancellation for one persisted task."""
        from nanoclaw.runtime.tasks import get_task_store

        task_id = str(request.match_info.get("task_id", "")).strip()
        if not task_id:
            return aiohttp.web.json_response({"error": "Missing task ID"}, status=400)
        try:
            task = await get_task_store().request_cancel(task_id)
        except KeyError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=404)
        except ValueError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=400)
        return aiohttp.web.json_response(self._compact_task_entry(task))

    async def _api_task_replay(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Return one structured task replay bundle."""
        from nanoclaw.security.audit import get_audit_log

        task_id = str(request.match_info.get("task_id", "")).strip()
        if not task_id:
            return aiohttp.web.json_response({"error": "Missing task ID"}, status=400)
        replay = await get_audit_log().get_task_replay(task_id)
        if replay is None:
            return aiohttp.web.json_response({"error": "Task not found"}, status=404)
        return aiohttp.web.json_response(replay)

    async def _api_task_requeue(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Move one failed or cancelled task back to pending."""
        from nanoclaw.runtime.tasks import get_task_store
        from nanoclaw.tools.spawn import wake_background_runtime

        task_id = str(request.match_info.get("task_id", "")).strip()
        if not task_id:
            return aiohttp.web.json_response({"error": "Missing task ID"}, status=400)
        try:
            task = await get_task_store().requeue_task(task_id)
            if task.get("source") == "spawn_task":
                wake_background_runtime()
        except KeyError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=404)
        except ValueError as exc:
            return aiohttp.web.json_response({"error": str(exc)}, status=400)
        return aiohttp.web.json_response(self._compact_task_entry(task))

    async def _api_cron_list(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Return all cron jobs."""
        from nanoclaw.cron.scheduler import get_scheduler

        health_filter = self._parse_cron_health_filter(request)
        signal_filter = self._parse_cron_signal_filter(request)
        if health_filter is None or signal_filter is None:
            return aiohttp.web.json_response({"error": "Invalid cron filter"}, status=400)
        scheduler = get_scheduler()
        if hasattr(scheduler, "list_jobs_with_runtime_state"):
            jobs = await scheduler.list_jobs_with_runtime_state()
        else:
            jobs = await scheduler.list_jobs()
        jobs = [
            item for item in jobs
            if self._cron_job_matches_filters(item, health_filter, signal_filter)
        ]
        return aiohttp.web.json_response(
            [self._compact_cron_entry(item) for item in jobs]
        )

    async def _api_cron_groups(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Return cron jobs grouped by channel and target scope."""
        from nanoclaw.cron.scheduler import get_scheduler

        health_filter = self._parse_cron_health_filter(request)
        signal_filter = self._parse_cron_signal_filter(request)
        if health_filter is None or signal_filter is None:
            return aiohttp.web.json_response({"error": "Invalid cron filter"}, status=400)
        scheduler = get_scheduler()
        if hasattr(scheduler, "list_jobs_with_runtime_state"):
            jobs = await scheduler.list_jobs_with_runtime_state()
        else:
            jobs = await scheduler.list_jobs()
        groups: dict[str, dict] = {}
        for item in jobs:
            if not self._cron_job_matches_filters(item, health_filter, signal_filter):
                continue
            compact = self._compact_cron_entry(item)
            group_key = f"{compact['channel']}::{compact['target_id'] or 'default'}"
            group = groups.setdefault(
                group_key,
                {
                    "group_key": group_key,
                    "channel": compact["channel"],
                    "target_id": compact["target_id"],
                    "target_label": compact["target_label"],
                    "job_count": 0,
                    "enabled_jobs": 0,
                    "jobs": [],
                },
            )
            group["jobs"].append(compact)
            group["job_count"] += 1
            group["enabled_jobs"] += 1 if compact["enabled"] else 0

        ordered = sorted(
            groups.values(),
            key=lambda item: (str(item["channel"]), str(item["target_label"])),
        )
        for group in ordered:
            group["jobs"] = sorted(group["jobs"], key=lambda item: int(item["id"]))
        return aiohttp.web.json_response(ordered)

    async def _api_cron_add(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Add a new cron job."""
        from nanoclaw.cron.scheduler import get_scheduler

        try:
            data = await request.json()
        except Exception:
            return aiohttp.web.json_response(
                {"error": "Invalid JSON"}, status=400
            )

        name = data.get("name")
        message = data.get("message")
        cron_expr = data.get("cron_expr")
        interval = data.get("interval_seconds")

        if not name or not message:
            return aiohttp.web.json_response(
                {"error": "name and message required"}, status=400
            )

        scheduler = get_scheduler()
        job_id = await scheduler.add_job(
            name=name,
            message=message,
            cron_expr=cron_expr,
            interval_seconds=interval,
        )

        return aiohttp.web.json_response({"id": job_id, "name": name})

    async def _api_cron_toggle(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Enable or disable one cron job."""
        from nanoclaw.cron.scheduler import get_scheduler

        try:
            job_id = int(request.match_info["id"])
        except (ValueError, TypeError):
            return aiohttp.web.json_response({"error": "Invalid job ID"}, status=400)
        try:
            data = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "Invalid JSON"}, status=400)
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            return aiohttp.web.json_response(
                {"error": "enabled must be a boolean"}, status=400
            )

        scheduler = get_scheduler()
        jobs = await scheduler.list_jobs()
        if not any(int(item.get("id", 0)) == job_id for item in jobs):
            return aiohttp.web.json_response({"error": "Job not found"}, status=404)
        await scheduler.toggle_job(job_id, enabled)
        refreshed = await scheduler.list_jobs()
        for item in refreshed:
            if int(item.get("id", 0)) == job_id:
                return aiohttp.web.json_response(self._compact_cron_entry(item))
        return aiohttp.web.json_response({"error": "Job not found"}, status=404)

    async def _api_cron_group_action(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Apply one batch action to visible jobs inside one cron group."""
        from nanoclaw.cron.scheduler import get_scheduler

        try:
            data = await request.json()
        except Exception:
            return aiohttp.web.json_response({"error": "Invalid JSON"}, status=400)

        action = str(data.get("action") or "").strip().lower()
        if action not in {"pause", "resume", "remove"}:
            return aiohttp.web.json_response({"error": "Invalid action"}, status=400)
        group_key = str(data.get("group_key") or "").strip()
        if not group_key:
            return aiohttp.web.json_response({"error": "group_key required"}, status=400)
        health_filter = self._validate_cron_health_filter(data.get("health"))
        signal_filter = self._validate_cron_signal_filter(data.get("signal"))
        if health_filter is None or signal_filter is None:
            return aiohttp.web.json_response({"error": "Invalid cron filter"}, status=400)

        scheduler = get_scheduler()
        if hasattr(scheduler, "list_jobs_with_runtime_state"):
            jobs = await scheduler.list_jobs_with_runtime_state()
        else:
            jobs = await scheduler.list_jobs()
        matching: list[int] = []
        for item in jobs:
            compact = self._compact_cron_entry(item)
            item_group_key = f"{compact['channel']}::{compact['target_id'] or 'default'}"
            if item_group_key != group_key:
                continue
            if not self._cron_job_matches_filters(item, health_filter, signal_filter):
                continue
            matching.append(int(compact["id"]))
        if not matching:
            return aiohttp.web.json_response(
                {"error": "No jobs matched current group filters"}, status=404
            )

        if action == "remove":
            for job_id in matching:
                await scheduler.remove_job(job_id)
        else:
            enabled = action == "resume"
            for job_id in matching:
                await scheduler.toggle_job(job_id, enabled)
        return aiohttp.web.json_response(
            {
                "group_key": group_key,
                "action": action,
                "count": len(matching),
                "ids": matching,
                "health": health_filter,
                "signal": signal_filter,
            }
        )

    async def _api_cron_remove(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Remove a cron job."""
        from nanoclaw.cron.scheduler import get_scheduler

        try:
            job_id = int(request.match_info["id"])
        except (ValueError, TypeError):
            return aiohttp.web.json_response(
                {"error": "Invalid job ID"}, status=400
            )
        scheduler = get_scheduler()
        await scheduler.remove_job(job_id)
        return aiohttp.web.json_response({"removed": job_id})

    async def _api_skills(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        """Return list of available skills/tools."""
        from nanoclaw.tools.registry import get_tool_registry

        registry = get_tool_registry()
        tools = []
        for name, info in registry.tools.items():
            tools.append(
                {
                    "name": name,
                    "description": info.description,
                }
            )
        return aiohttp.web.json_response(tools)

    @staticmethod
    def _compact_workflow_entry(item: dict) -> dict:
        """Return a small workflow summary for the dashboard status endpoint."""
        tools = [
            str(step.get("name", ""))
            for step in item.get("call_chain", [])
            if step.get("type") == "tool" and step.get("name")
        ]
        role_chain = []
        for step in list(item.get("role_execution_timeline") or [])[:4]:
            if not step.get("role"):
                continue
            role_name = _format_role_display(
                str(step.get("role") or ""),
                str(step.get("role_label") or step.get("role") or ""),
            )
            role_chain.append(f"{role_name}@{step.get('stage')}")
        return {
            "id": item.get("id"),
            "timestamp": item.get("timestamp"),
            "workflow_name": item.get("workflow_name"),
            "status": item.get("status"),
            "execution_ms": item.get("execution_ms"),
            "total_tokens": item.get("total_tokens"),
            "tool_chain": tools[:4],
            "role_chain": role_chain,
            "shared_evidence_refs": list(item.get("shared_evidence_refs") or [])[:4],
            "failure_reason": item.get("failure_reason", ""),
        }

    @staticmethod
    def _compact_workflow_evaluation_entry(item: dict) -> dict:
        """Return a small workflow evaluation summary for dashboard status views."""
        return {
            "id": item.get("id"),
            "workflow_run_id": item.get("workflow_run_id"),
            "timestamp": item.get("timestamp"),
            "workflow_name": item.get("workflow_name"),
            "workflow_status": item.get("workflow_status"),
            "evaluation_label": item.get("evaluation_label"),
            "quality_score": item.get("quality_score"),
            "efficiency_score": item.get("efficiency_score"),
            "feedback_signal": item.get("feedback_signal"),
            "suggestions": list(item.get("suggestions") or [])[:2],
            "failure_classes": list(item.get("failure_classes") or [])[:3],
            "attention_reasons": list(item.get("attention_reasons") or [])[:2],
            "follow_up_actions": list(item.get("follow_up_actions") or [])[:2],
        }

    @staticmethod
    def _compact_workflow_recommendation_entry(item: dict) -> dict:
        """Return a compact workflow recommendation summary."""
        return {
            "workflow_name": item.get("workflow_name"),
            "recommendation_status": item.get("recommendation_status"),
            "run_count": item.get("run_count"),
            "good_runs": item.get("good_runs"),
            "review_runs": item.get("review_runs"),
            "poor_runs": item.get("poor_runs"),
            "positive_feedback": item.get("positive_feedback"),
            "neutral_feedback": item.get("neutral_feedback"),
            "negative_feedback": item.get("negative_feedback"),
            "avg_quality_score": item.get("avg_quality_score"),
            "avg_efficiency_score": item.get("avg_efficiency_score"),
            "avg_tokens": item.get("avg_tokens"),
            "avg_execution_ms": item.get("avg_execution_ms"),
            "last_seen_at": item.get("last_seen_at"),
            "top_failure_class": item.get("top_failure_class"),
            "top_attention_reason": item.get("top_attention_reason"),
            "top_follow_up_action": item.get("top_follow_up_action"),
            "attention_reasons": list(item.get("attention_reasons") or [])[:2],
            "follow_up_actions": list(item.get("follow_up_actions") or [])[:2],
            "recommendations": list(item.get("recommendations") or [])[:2],
        }

    @staticmethod
    def _parse_cron_health_filter(request: aiohttp.web.Request) -> str | None:
        """Parse the optional cron health filter from one request."""
        return Dashboard._validate_cron_health_filter(request.query.get("health"))

    @staticmethod
    def _parse_cron_signal_filter(request: aiohttp.web.Request) -> str | None:
        """Parse the optional cron signal-label filter from one request."""
        return Dashboard._validate_cron_signal_filter(request.query.get("signal"))

    @staticmethod
    def _validate_cron_health_filter(raw_value: object) -> str | None:
        """Validate one optional cron health filter value."""
        value = str(raw_value or "all").strip().lower() or "all"
        allowed = {"all", "healthy", "retrying", "attention", "muted", "idle"}
        return value if value in allowed else None

    @staticmethod
    def _validate_cron_signal_filter(raw_value: object) -> str | None:
        """Validate one optional cron signal-label filter value."""
        value = str(raw_value or "all").strip().lower() or "all"
        allowed = {"all", "alert", "escalation", "recovery"}
        return value if value in allowed else None

    @staticmethod
    def _cron_job_matches_filters(
        item: dict,
        health_filter: str,
        signal_filter: str,
    ) -> bool:
        """Return whether one cron job matches the current dashboard filters."""
        runtime = dict(item.get("runtime") or {})
        health = str(runtime.get("health") or "idle").lower()
        if health_filter != "all" and health != health_filter:
            return False
        if signal_filter == "all":
            return True
        timeline = list(runtime.get("signal_timeline") or [])
        return any(
            str(signal.get("label") or "").lower() == signal_filter
            for signal in timeline
        )

    @staticmethod
    def _compact_cron_entry(item: dict) -> dict:
        """Return a compact cron summary for dashboard APIs."""
        message = " ".join(str(item.get("message", "")).split())
        if len(message) > 140:
            message = f"{message[:137]}..."
        channel = str(item.get("channel") or "telegram")
        target_id = str(item.get("target_id") or "")
        target_label = f"{channel}:{target_id}" if target_id else f"{channel}:default"
        quiet_start = str(item.get("quiet_start") or "")
        quiet_end = str(item.get("quiet_end") or "")
        runtime = dict(item.get("runtime") or {})
        last_execution = dict(runtime.get("last_execution") or {})
        last_delivery_retry = dict(runtime.get("last_delivery_retry") or {})
        signal_timeline = list(runtime.get("signal_timeline") or [])
        last_signal = signal_timeline[0] if signal_timeline else {}
        if quiet_start and quiet_end:
            quiet_window = f"{quiet_start}-{quiet_end}"
        else:
            quiet_window = "off"
        if item.get("cron_expr"):
            schedule_text = str(item.get("cron_expr"))
        else:
            interval = int(item.get("interval_seconds") or 0)
            schedule_text = f"every {interval}s" if interval > 0 else "unspecified"
        return {
            "id": int(item.get("id", 0)),
            "name": str(item.get("name") or ""),
            "message": message,
            "schedule_text": schedule_text,
            "cron_expr": str(item.get("cron_expr") or ""),
            "interval_seconds": int(item.get("interval_seconds") or 0),
            "channel": channel,
            "target_id": target_id,
            "target_label": target_label,
            "quiet_start": quiet_start,
            "quiet_end": quiet_end,
            "quiet_window": quiet_window,
            "enabled": bool(item.get("enabled")),
            "last_run": item.get("last_run"),
            "created_at": item.get("created_at"),
            "health": str(runtime.get("health") or "idle"),
            "health_reason": str(runtime.get("health_reason") or ""),
            "last_notify_kind": str(runtime.get("notify_kind") or ""),
            "last_execution_status": str(last_execution.get("status") or ""),
            "last_execution_task_id": str(last_execution.get("task_id") or ""),
            "last_execution_updated_at": last_execution.get("updated_at"),
            "last_delivery_retry_status": str(last_delivery_retry.get("status") or ""),
            "last_delivery_retry_task_id": str(last_delivery_retry.get("task_id") or ""),
            "last_delivery_retry_updated_at": last_delivery_retry.get("updated_at"),
            "signal_timeline": signal_timeline[:3],
            "last_signal_label": str(last_signal.get("label") or ""),
            "last_signal_detail": str(last_signal.get("detail") or ""),
            "last_signal_at": last_signal.get("timestamp"),
        }

    @staticmethod
    def _compact_task_entry(item: dict) -> dict:
        """Return a small task summary for status and dashboard task views."""
        description = " ".join(str(item.get("description", "")).split())
        if len(description) > 120:
            description = f"{description[:117]}..."
        return {
            "task_id": item.get("task_id"),
            "task_type": item.get("task_type"),
            "status": item.get("status"),
            "description": description,
            "source": item.get("source", ""),
            "session_id": item.get("session_id", ""),
            "priority": item.get("priority", 100),
            "timeout_seconds": item.get("timeout_seconds", 1800),
            "max_attempts": item.get("max_attempts", 1),
            "retry_backoff_seconds": item.get("retry_backoff_seconds", 0),
            "rate_limit_key": item.get("rate_limit_key", ""),
            "rate_limit_window_seconds": item.get("rate_limit_window_seconds", 0),
            "rate_limit_max_claims": item.get("rate_limit_max_claims", 0),
            "idempotency_key": item.get("idempotency_key", ""),
            "next_attempt_at": item.get("next_attempt_at"),
            "last_claimed_at": item.get("last_claimed_at"),
            "cancel_requested": bool(item.get("cancel_requested")),
            "dead_lettered": bool(item.get("dead_lettered")),
            "dead_letter_reason": item.get("dead_letter_reason", ""),
            "dead_lettered_at": item.get("dead_lettered_at"),
            "attempt_count": item.get("attempt_count", 0),
            "claimed_by": item.get("claimed_by", ""),
            "last_heartbeat_at": item.get("last_heartbeat_at"),
            "updated_at": item.get("updated_at"),
            "started_at": item.get("started_at"),
            "finished_at": item.get("finished_at"),
            "last_error": item.get("last_error", ""),
        }

    async def stop(self) -> None:
        """Stop dashboard server."""
        if self.runner:
            await self.runner.cleanup()
