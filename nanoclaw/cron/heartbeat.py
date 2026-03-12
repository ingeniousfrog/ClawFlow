"""Periodic heartbeat runner based on a workspace checklist."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from nanoclaw.core.config import get_workspace_path
from nanoclaw.core.logger import get_logger
from nanoclaw.security.sandbox import FileGuard, SecurityError

if TYPE_CHECKING:
    from nanoclaw.channels.gateway import Gateway
    from nanoclaw.core.config import HeartbeatConfig

logger = get_logger(__name__)

HEARTBEAT_TASK_SOURCE = "heartbeat_checklist"
HEARTBEAT_TASK_DESCRIPTION = "Heartbeat checklist run"
HEARTBEAT_TASK_PRIORITY = 900
HEARTBEAT_TASK_TIMEOUT_SECONDS = 300
HEARTBEAT_TASK_MAX_ATTEMPTS = 2


class HeartbeatRunner:
    """Run a periodic checklist and only notify when action is needed."""

    OK_SENTINEL = "HEARTBEAT_OK"

    def __init__(self, config: "HeartbeatConfig", gateway: "Gateway") -> None:
        """Initialize heartbeat state."""
        self.config = config
        self.gateway = gateway
        self.running = False
        self._task: Optional[asyncio.Task[None]] = None

    async def start(self) -> None:
        """Start the background heartbeat loop."""
        if not self.config.enabled or self._task is not None:
            return
        self.running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Heartbeat started: interval=%ss checklist=%s",
            self.config.interval_seconds,
            self.config.checklist_path,
        )

    async def stop(self) -> None:
        """Stop the background heartbeat loop."""
        self.running = False
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _loop(self) -> None:
        """Enqueue heartbeat checks on the configured interval."""
        delay = max(5, int(self.config.interval_seconds))
        while self.running:
            await asyncio.sleep(delay)
            if not self.running:
                break
            try:
                await self.enqueue_once()
            except Exception as exc:
                logger.error("Heartbeat run failed: %s", exc)

    async def enqueue_once(self) -> str:
        """Persist one heartbeat task and wake the shared runtime."""
        if not self.config.enabled:
            return "disabled"
        active = await self._find_active_task()
        if active is not None:
            logger.debug(
                "Heartbeat enqueue skipped: active runtime task already exists as %s",
                active["task_id"],
            )
            return "active"

        from nanoclaw.runtime.tasks import get_task_store
        from nanoclaw.tools.spawn import wake_background_runtime

        retry_backoff_seconds = max(30, min(int(self.config.interval_seconds), 300))
        task = await get_task_store().create_task(
            HEARTBEAT_TASK_DESCRIPTION,
            task_type="heartbeat",
            payload={
                "checklist_path": self.config.checklist_path,
                "notify_channel": self.config.notify_channel,
            },
            source=HEARTBEAT_TASK_SOURCE,
            session_id="heartbeat:system",
            priority=HEARTBEAT_TASK_PRIORITY,
            timeout_seconds=HEARTBEAT_TASK_TIMEOUT_SECONDS,
            max_attempts=HEARTBEAT_TASK_MAX_ATTEMPTS,
            retry_backoff_seconds=retry_backoff_seconds,
        )
        logger.info("Heartbeat queued as runtime task %s", task["task_id"])
        wake_background_runtime()
        return "queued"

    async def run_once(self) -> str:
        """Run one heartbeat check and notify only when needed."""
        if not self.config.enabled:
            return "disabled"

        checklist_path = self._resolve_checklist_path()
        if checklist_path is None:
            return "blocked"
        if not checklist_path.exists():
            logger.debug("Heartbeat skipped: checklist missing at %s", checklist_path)
            return "missing"

        checklist_text = await self._read_checklist(checklist_path)
        if checklist_text is None:
            return "error"
        if not checklist_text.strip():
            logger.debug("Heartbeat skipped: checklist empty at %s", checklist_path)
            return "empty"

        response = await self._run_agent_check(checklist_text)
        if response is None:
            return "error"
        if response.strip() == self.OK_SENTINEL:
            logger.debug("Heartbeat OK")
            return "ok"

        notify_channel = self._resolve_notify_channel()
        if not notify_channel:
            logger.warning("Heartbeat produced findings but no active channel is available")
            return "unrouted"

        await self.gateway.send_proactive(
            f"**Heartbeat**\n\n{response}",
            channel=notify_channel,
        )
        logger.info("Heartbeat sent proactive update via %s", notify_channel)
        return "notified"

    async def _find_active_task(self) -> Optional[dict[str, Any]]:
        """Return the current pending or running heartbeat runtime task."""
        from nanoclaw.runtime.tasks import get_task_store

        store = get_task_store()
        for status in ("running", "pending"):
            for item in await store.list_tasks(limit=20, status=status):
                if item.get("source") == HEARTBEAT_TASK_SOURCE:
                    return item
        return None

    def _resolve_checklist_path(self) -> Optional[Path]:
        """Resolve the checklist path inside the protected workspace."""
        guard = FileGuard(get_workspace_path())
        try:
            return guard.validate_path(self.config.checklist_path)
        except SecurityError as exc:
            logger.error("Heartbeat checklist path rejected: %s", exc)
            return None

    async def _read_checklist(self, checklist_path: Path) -> Optional[str]:
        """Read the checklist file content."""
        try:
            return await asyncio.to_thread(checklist_path.read_text, encoding="utf-8")
        except Exception as exc:
            logger.error("Failed to read heartbeat checklist %s: %s", checklist_path, exc)
            return None

    async def _run_agent_check(self, checklist_text: str) -> Optional[str]:
        """Run one agent turn against the heartbeat checklist."""
        prompt = (
            "Heartbeat checklist run.\n"
            "Read the checklist below and execute only the checks that are explicitly listed.\n"
            "Use tools when needed.\n"
            f"If nothing needs attention, reply EXACTLY with {self.OK_SENTINEL}.\n"
            "If something needs attention, reply with a concise actionable summary.\n"
            "Do not greet. Do not mention the sentinel unless nothing needs attention.\n\n"
            "## HEARTBEAT.md\n\n"
            f"{checklist_text}"
        )
        try:
            return await self.gateway.handle_incoming(
                channel_id="heartbeat",
                user_id="system",
                message=prompt,
            )
        except Exception as exc:
            logger.error("Heartbeat agent execution failed: %s", exc)
            return None

    def _resolve_notify_channel(self) -> str:
        """Resolve the configured or first available proactive channel."""
        if hasattr(self.gateway, "config"):
            try:
                from nanoclaw.channels.contract import resolve_channel_route

                route = resolve_channel_route(
                    self.gateway.config,
                    self.gateway,
                    purpose="heartbeat",
                    preferred_channel=str(self.config.notify_channel or ""),
                )
                if route["status"] == "ready":
                    return str(route["selected_channel"] or "")
            except Exception:
                pass
        channels = getattr(self.gateway, "channels", {})
        preferred = str(self.config.notify_channel or "").strip()
        if preferred and preferred in channels:
            return preferred
        for name in ("feishu", "telegram", "console"):
            if name in channels:
                return name
        return ""
