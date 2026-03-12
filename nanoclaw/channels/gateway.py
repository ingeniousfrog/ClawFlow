"""Central message router between channels, agent, and scheduler."""
from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from typing import TYPE_CHECKING, Any, Callable, Optional

from nanoclaw.channels.registry import get_channel_runtime_registry
from nanoclaw.channels.state_store import get_channel_state_store
from nanoclaw.core.llm import ConnectionPool
from nanoclaw.core.logger import get_logger

if TYPE_CHECKING:
    from nanoclaw.core.config import Config

logger = get_logger(__name__)


class Gateway:
    """Central message router between channels, agent, and scheduler."""

    _RECONCILE_INTERVAL_SECONDS = 10

    def __init__(self, config: "Config"):
        """
        Initialize Gateway.

        Args:
            config: Application configuration
        """
        self.config = config
        self._channel_registry = get_channel_runtime_registry()
        self._managed_channels = tuple(self._channel_registry.managed_channel_names())
        self.channels: dict[str, Any] = {}
        self.scheduler: Any = None
        self.heartbeat: Any = None
        self.dashboard: Any = None
        self._agent: Any = None
        self._stop_event: Optional[asyncio.Event] = None
        self._channel_runtime: dict[str, dict[str, object]] = {}
        self._channel_diagnostics: dict[str, dict[str, object]] = {
            name: self._new_channel_diagnostics(name)
            for name in self._channel_registry.channel_names()
        }
        self._channel_orchestration: dict[str, dict[str, object]] = {
            name: self._new_channel_orchestration(
                name,
                desired_state=self._desired_state_from_config(name),
            )
            for name in self._managed_channels
        }
        self._state_store: Any = None
        self._reconcile_task: Optional[asyncio.Task[None]] = None
        self._reconcile_lock = asyncio.Lock()

    @property
    def agent(self) -> Any:
        """Lazy-load agent to avoid circular imports."""
        if self._agent is None:
            from nanoclaw.core.agent import get_agent

            self._agent = get_agent()
        return self._agent

    async def start(self) -> None:
        """Start all components."""
        # Set global gateway reference
        set_gateway(self)
        from nanoclaw.runtime.tasks import get_task_store
        from nanoclaw.tools.spawn import start_background_runtime

        get_task_store()
        await start_background_runtime()

        # Debug: verify logger state
        import logging
        root = logging.getLogger("nanoclaw")
        logger.debug(f"Logger state: level={root.level}, handlers={len(root.handlers)}")

        # Log provider and model
        provider, _, _, base_url = self.config.get_active_provider()
        model = self.config.get_default_model()
        if base_url:
            logger.info(f"Provider: {provider} ({base_url})")
        else:
            logger.info(f"Provider: {provider}")
        logger.info(f"Model: {model}")

        for name in self._managed_channels:
            spec = self._channel_registry.get(name)
            if spec is None:
                continue
            self._set_channel_runtime(
                name,
                "configured" if spec.is_enabled(self.config) else "disabled",
                detail=(
                    "configured; waiting for reconcile"
                    if spec.is_enabled(self.config)
                    else "disabled in config"
                ),
            )
        await self._load_channel_orchestration_state()
        await self.reconcile_channels(trigger="startup")

        # Start cron scheduler
        from nanoclaw.cron.scheduler import Scheduler

        self.scheduler = Scheduler(self.config, self)
        await self.scheduler.start()
        logger.info("Cron scheduler started")

        # Start heartbeat runner
        if self.config.heartbeat.enabled:
            from nanoclaw.cron.heartbeat import HeartbeatRunner

            self.heartbeat = HeartbeatRunner(self.config.heartbeat, self)
            await self.heartbeat.start()
            logger.info("Heartbeat runner started")

        # Start dashboard if enabled
        if self.config.dashboard.enabled:
            from nanoclaw.dashboard.server import Dashboard

            self.dashboard = Dashboard(self.config, self)
            await self.dashboard.start(port=self.config.dashboard.port)

        self._reconcile_task = asyncio.create_task(self._run_reconcile_loop())

        logger.info("nanoClaw is running!")

        # Keep running until shutdown signal
        self._stop_event = asyncio.Event()

        # Set up signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self._handle_signal)
            except NotImplementedError:
                # Windows doesn't support add_signal_handler
                pass

        await self._stop_event.wait()
        await self.stop()

    def _handle_signal(self) -> None:
        """Handle shutdown signal."""
        logger.info("Shutdown signal received...")
        if self._stop_event:
            self._stop_event.set()

    async def handle_incoming(
        self,
        channel_id: str,
        user_id: str,
        message: str,
        confirm_callback: Optional[Callable] = None,
    ) -> str:
        """
        Route incoming message to agent, return response.

        Args:
            channel_id: Channel identifier
            user_id: User identifier
            message: User's message
            confirm_callback: Optional confirmation callback

        Returns:
            Agent response
        """
        session_id = f"{channel_id}:{user_id}"
        logger.debug(f"Gateway handling message for session {session_id}")

        # Set shell confirm callback
        from nanoclaw.tools.shell import set_confirm_callback

        set_confirm_callback(confirm_callback)

        try:
            response = await self.agent.run(
                user_message=message,
                session_id=session_id,
                confirm_callback=confirm_callback,
            )
            self._record_channel_incoming(
                channel_id,
                session_id=session_id,
                success=True,
            )
            return response
        except Exception as e:
            self._record_channel_incoming(
                channel_id,
                session_id=session_id,
                success=False,
                error=str(e),
            )
            logger.error(f"Agent error: {e}")
            return f"Sorry, something went wrong: {e}"

    async def send_proactive(
        self, text: str, channel: str = "telegram"
    ) -> None:
        """
        Send proactive message via specified channel.

        Args:
            text: Message text
            channel: Target channel name
        """
        if channel not in self.channels:
            self._record_channel_outgoing(
                channel,
                success=False,
                target_id="",
                delivery_kind="proactive",
                error="channel runtime unavailable",
            )
            return
        try:
            await self.channels[channel].send_proactive(text)
        except Exception as exc:
            self._record_channel_outgoing(
                channel,
                success=False,
                target_id="",
                delivery_kind="proactive",
                error=str(exc),
            )
            raise
        self._record_channel_outgoing(
            channel,
            success=True,
            target_id="",
            delivery_kind="proactive",
        )

    async def send_proactive_targeted(
        self,
        *,
        channel: str,
        text: str,
        target_id: str,
    ) -> bool:
        """Send one targeted proactive message when the channel supports it."""
        if channel not in self.channels:
            self._record_channel_outgoing(
                channel,
                success=False,
                target_id=target_id,
                delivery_kind="targeted_proactive",
                error="channel runtime unavailable",
            )
            return False
        transport = self.channels[channel]
        if not target_id:
            self._record_channel_outgoing(
                channel,
                success=False,
                target_id="",
                delivery_kind="targeted_proactive",
                error="target_id required",
            )
            return False
        if not hasattr(transport, "send_proactive_to"):
            self._record_channel_outgoing(
                channel,
                success=False,
                target_id=target_id,
                delivery_kind="targeted_proactive",
                error="targeted proactive delivery unsupported",
            )
            return False
        try:
            sent = bool(await transport.send_proactive_to(target_id, text))
        except Exception as exc:
            self._record_channel_outgoing(
                channel,
                success=False,
                target_id=target_id,
                delivery_kind="targeted_proactive",
                error=str(exc),
            )
            raise
        self._record_channel_outgoing(
            channel,
            success=sent,
            target_id=target_id,
            delivery_kind="targeted_proactive",
            error="" if sent else "targeted proactive send returned false",
        )
        return sent

    async def run_channel_action(self, name: str, action: str) -> dict[str, object]:
        """Run one operator action against a managed channel."""
        normalized_name = name.strip().lower()
        normalized_action = action.strip().lower()
        if normalized_name not in self._managed_channels:
            raise ValueError(f"Unknown managed channel `{normalized_name}`")
        await self._ensure_orchestration_loaded()

        if normalized_action == "start":
            await self._set_desired_state(
                normalized_name,
                "running",
                reason="operator start request",
            )
            results = await self.reconcile_channels(
                trigger="operator_start",
                channel_name=normalized_name,
            )
            return dict(results[normalized_name])
        if normalized_action == "stop":
            await self._set_desired_state(
                normalized_name,
                "stopped",
                reason="operator stop request",
            )
            results = await self.reconcile_channels(
                trigger="operator_stop",
                channel_name=normalized_name,
            )
            return dict(results[normalized_name])
        if normalized_action == "restart":
            await self._set_desired_state(
                normalized_name,
                "running",
                reason="operator restart request",
            )
            await self._stop_managed_channel(
                normalized_name,
                detail="stopped for operator restart",
            )
            results = await self.reconcile_channels(
                trigger="operator_restart",
                channel_name=normalized_name,
                force_restart=True,
            )
            return dict(results[normalized_name])
        if normalized_action == "recover":
            await self._set_desired_state(
                normalized_name,
                "running",
                reason="operator recovery request",
            )
            results = await self.reconcile_channels(
                trigger="operator_recover",
                channel_name=normalized_name,
                force_restart=True,
            )
            return dict(results[normalized_name])
        if normalized_action == "reconcile":
            results = await self.reconcile_channels(
                trigger="operator_reconcile",
                channel_name=normalized_name,
            )
            return dict(results[normalized_name])
        raise ValueError(f"Unsupported channel action `{normalized_action}`")

    async def set_channel_desired_state(
        self,
        name: str,
        desired_state: str,
        *,
        reason: str = "",
        reconcile: bool = True,
    ) -> dict[str, object]:
        """Persist one channel desired state and optionally reconcile it."""
        normalized_name = name.strip().lower()
        normalized_desired_state = desired_state.strip().lower()
        if normalized_name not in self._managed_channels:
            raise ValueError(f"Unknown managed channel `{normalized_name}`")
        if normalized_desired_state not in {"running", "stopped"}:
            raise ValueError(
                f"Unsupported desired state `{normalized_desired_state}`"
            )
        await self._ensure_orchestration_loaded()
        await self._set_desired_state(
            normalized_name,
            normalized_desired_state,
            reason=reason or f"operator desired-state set to {normalized_desired_state}",
        )
        if reconcile:
            results = await self.reconcile_channels(
                trigger="operator_desired_state",
                channel_name=normalized_name,
            )
            return dict(results[normalized_name])
        refreshed = await self._refresh_channel_orchestration(
            normalized_name,
            reconcile_status="pending",
            reconcile_detail=(
                f"desired `{normalized_desired_state}` recorded; reconcile pending"
            ),
            last_action="set_desired_state",
            persist=True,
        )
        return dict(refreshed)

    async def stop(self) -> None:
        """Graceful shutdown of all components."""
        logger.info("Stopping nanoClaw...")

        if self._reconcile_task is not None:
            self._reconcile_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reconcile_task
            self._reconcile_task = None

        # Stop channels
        for name, channel in self.channels.items():
            try:
                await channel.stop()
                self._set_channel_runtime(name, "stopped", detail="stopped cleanly")
                await self._refresh_channel_orchestration(
                    name,
                    reconcile_status="drifted",
                    reconcile_detail="runtime stopped during gateway shutdown",
                )
                logger.info(f"Stopped {name} channel")
            except Exception as e:
                self._set_channel_runtime(
                    name,
                    "failed",
                    detail=f"stop failed for {name}",
                    last_error=str(e),
                )
                await self._refresh_channel_orchestration(
                    name,
                    reconcile_status="blocked",
                    reconcile_detail=str(e),
                    last_action="stop",
                )
                logger.error(f"Error stopping {name}: {e}")

        # Stop scheduler
        if self.scheduler:
            await self.scheduler.stop()
            logger.info("Stopped scheduler")

        # Stop heartbeat
        if self.heartbeat:
            await self.heartbeat.stop()
            logger.info("Stopped heartbeat")

        # Stop background runtime helper tasks
        from nanoclaw.tools.spawn import stop_background_runtime

        await stop_background_runtime()

        # Stop dashboard
        if self.dashboard:
            await self.dashboard.stop()
            logger.info("Stopped dashboard")

        # Close connection pool
        await ConnectionPool.close()

        logger.info("nanoClaw stopped.")

    def get_channel_runtime_snapshot(self) -> dict[str, dict[str, object]]:
        """Return one copy of channel runtime lifecycle state."""
        return {
            name: dict(state) for name, state in self._channel_runtime.items()
        }

    def get_channel_diagnostics_snapshot(self) -> dict[str, dict[str, object]]:
        """Return one copy of channel runtime diagnostics."""
        return {
            name: dict(state) for name, state in self._channel_diagnostics.items()
        }

    def get_channel_orchestration_snapshot(self) -> dict[str, dict[str, object]]:
        """Return one copy of desired-state orchestration metadata."""
        return {
            name: dict(state) for name, state in self._channel_orchestration.items()
        }

    def _set_channel_runtime(
        self,
        name: str,
        status: str,
        *,
        detail: str = "",
        last_error: str = "",
    ) -> None:
        """Record one channel runtime lifecycle transition."""
        self._channel_runtime[name] = {
            "status": status,
            "detail": detail,
            "last_error": last_error,
            "last_transition_at": int(time.time()),
        }
        diagnostics = self._ensure_channel_diagnostics(name)
        diagnostics["last_runtime_status"] = status
        diagnostics["last_runtime_transition_at"] = int(time.time())
        if last_error:
            diagnostics["last_failure_at"] = int(time.time())
            diagnostics["last_failure_kind"] = "runtime"
            diagnostics["last_failure_error"] = last_error

    async def _ensure_orchestration_loaded(self) -> None:
        """Load persisted channel orchestration rows when needed."""
        if self._state_store is None:
            self._state_store = get_channel_state_store()
        if any(
            not state.get("desired_updated_at")
            for state in self._channel_orchestration.values()
        ):
            await self._load_channel_orchestration_state()

    async def _load_channel_orchestration_state(self) -> None:
        """Load or initialize desired-state orchestration rows."""
        if self._state_store is None:
            self._state_store = get_channel_state_store()
        persisted = await self._state_store.list_states()
        for name in self._managed_channels:
            desired_state = self._desired_state_from_config(name)
            state = dict(
                persisted.get(name)
                or self._new_channel_orchestration(name, desired_state=desired_state)
            )
            if not state.get("desired_state"):
                state["desired_state"] = desired_state
            if not state.get("desired_updated_at"):
                state["desired_updated_at"] = int(time.time())
            self._channel_orchestration[name] = state
            await self._refresh_channel_orchestration(name, persist=False)
        await self._persist_all_channel_orchestration()

    async def _persist_channel_orchestration(self, name: str) -> None:
        """Persist one orchestration row."""
        if self._state_store is None:
            return
        state = dict(self._channel_orchestration.get(name) or {})
        if state:
            await self._state_store.save_state(state)

    async def _persist_all_channel_orchestration(self) -> None:
        """Persist all known orchestration rows."""
        for name in self._managed_channels:
            await self._persist_channel_orchestration(name)

    def _desired_state_from_config(self, name: str) -> str:
        """Return the default desired state derived from config."""
        spec = self._channel_registry.get(name)
        if spec is None:
            return "stopped"
        return "running" if spec.is_enabled(self.config) else "stopped"

    @staticmethod
    def _new_channel_orchestration(
        name: str,
        *,
        desired_state: str,
    ) -> dict[str, object]:
        """Build the default desired-state row for one managed channel."""
        return {
            "channel_name": name,
            "desired_state": desired_state,
            "desired_reason": "config default",
            "desired_updated_at": 0,
            "actual_status": "",
            "actual_detail": "",
            "reconcile_status": "",
            "reconcile_detail": "",
            "drift_status": "unknown",
            "drift_since": 0,
            "drift_count": 0,
            "last_reconciled_at": 0,
            "last_action": "",
            "last_action_at": 0,
        }

    def _ensure_channel_orchestration(self, name: str) -> dict[str, object]:
        """Return one mutable orchestration row for the channel."""
        state = self._channel_orchestration.get(name)
        if state is None:
            state = self._new_channel_orchestration(
                name,
                desired_state=self._desired_state_from_config(name),
            )
            self._channel_orchestration[name] = state
        return state

    async def _set_desired_state(
        self,
        name: str,
        desired_state: str,
        *,
        reason: str,
    ) -> None:
        """Persist one desired state change for a managed channel."""
        state = self._ensure_channel_orchestration(name)
        state["desired_state"] = desired_state
        state["desired_reason"] = reason
        state["desired_updated_at"] = int(time.time())
        await self._refresh_channel_orchestration(name, persist=True)

    async def reconcile_channels(
        self,
        *,
        trigger: str,
        channel_name: str = "",
        force_restart: bool = False,
    ) -> dict[str, dict[str, object]]:
        """Run one desired-state reconciliation pass."""
        await self._ensure_orchestration_loaded()
        targets = [channel_name] if channel_name else list(self._managed_channels)
        results: dict[str, dict[str, object]] = {}
        async with self._reconcile_lock:
            for name in targets:
                if name not in self._managed_channels:
                    continue
                results[name] = await self._reconcile_one_channel(
                    name,
                    trigger=trigger,
                    force_restart=force_restart,
                )
        return results

    async def _reconcile_one_channel(
        self,
        name: str,
        *,
        trigger: str,
        force_restart: bool,
    ) -> dict[str, object]:
        """Bring one managed channel closer to its desired state."""
        state = self._ensure_channel_orchestration(name)
        state["reconcile_status"] = "reconciling"
        state["reconcile_detail"] = f"checking desired state via {trigger}"
        await self._persist_channel_orchestration(name)

        desired_state = str(state.get("desired_state") or "stopped")
        actual_status = str((self._channel_runtime.get(name) or {}).get("status") or "")
        action_taken = ""
        reconcile_detail = ""

        if desired_state == "running":
            can_start, reason = self._can_start_channel(name)
            if force_restart and actual_status == "running":
                await self._stop_managed_channel(name, detail=f"stopped for {trigger}")
                actual_status = "stopped"
                action_taken = "restart"
            if actual_status != "running":
                if not can_start:
                    blocked_status = self._resolve_blocked_runtime_status(reason)
                    if actual_status != blocked_status:
                        self._set_channel_runtime(
                            name,
                            blocked_status,
                            detail=reason,
                        )
                        actual_status = blocked_status
                    reconcile_detail = reason
                else:
                    runtime = await self._start_managed_channel(name)
                    actual_status = str(runtime.get("status") or "")
                    if not action_taken:
                        action_taken = (
                            "recover"
                            if trigger.startswith("operator_recover")
                            else "start"
                        )
                    if actual_status != "running":
                        reconcile_detail = str(
                            runtime.get("last_error") or runtime.get("detail") or ""
                        )
        elif actual_status in {"running", "starting"} or name in self.channels:
            await self._stop_managed_channel(
                name,
                detail="stopped to satisfy desired state",
            )
            action_taken = "stop"

        updated = await self._refresh_channel_orchestration(
            name,
            reconcile_status="reconciled",
            reconcile_detail=reconcile_detail,
            last_action=action_taken or trigger,
            persist=False,
        )
        if str(updated.get("drift_status") or "") == "converging":
            updated["reconcile_status"] = "reconciling"
            updated["reconcile_detail"] = (
                reconcile_detail or f"runtime moving toward desired `{desired_state}`"
            )
        elif str(updated.get("drift_status") or "") == "blocked":
            updated["reconcile_status"] = "blocked"
            updated["reconcile_detail"] = (
                reconcile_detail
                or str(updated.get("actual_detail") or "")
                or f"desired `{desired_state}` blocked"
            )
        elif str(updated.get("drift_status") or "") == "drifted":
            updated["reconcile_status"] = "drifted"
            updated["reconcile_detail"] = (
                reconcile_detail
                or f"desired `{desired_state}` differs from actual "
                f"`{updated.get('actual_status') or '-'}`"
            )
        await self._persist_channel_orchestration(name)
        return dict(updated)

    async def _refresh_channel_orchestration(
        self,
        name: str,
        *,
        reconcile_status: str = "",
        reconcile_detail: str = "",
        last_action: str = "",
        persist: bool = True,
    ) -> dict[str, object]:
        """Refresh one orchestration row from the current runtime state."""
        state = self._ensure_channel_orchestration(name)
        runtime = dict(self._channel_runtime.get(name) or {})
        actual_status = str(runtime.get("status") or "")
        actual_detail = str(runtime.get("detail") or "")
        desired_state = str(state.get("desired_state") or "stopped")
        drift_status = self._resolve_drift_status(desired_state, actual_status)
        previous_drift = str(state.get("drift_status") or "unknown")
        now = int(time.time())

        state["actual_status"] = actual_status
        state["actual_detail"] = actual_detail
        state["last_reconciled_at"] = now
        if reconcile_status:
            state["reconcile_status"] = reconcile_status
        elif drift_status == "in_sync":
            state["reconcile_status"] = "reconciled"
        elif drift_status == "converging":
            state["reconcile_status"] = "reconciling"
        elif drift_status == "blocked":
            state["reconcile_status"] = "blocked"
        else:
            state["reconcile_status"] = "drifted"
        if reconcile_detail:
            state["reconcile_detail"] = reconcile_detail
        elif drift_status == "in_sync":
            state["reconcile_detail"] = f"desired `{desired_state}` satisfied"
        elif drift_status == "converging":
            state["reconcile_detail"] = f"moving toward desired `{desired_state}`"
        elif drift_status == "blocked":
            state["reconcile_detail"] = (
                actual_detail or "channel blocked by config or runtime state"
            )
        else:
            state["reconcile_detail"] = (
                f"desired `{desired_state}` differs from actual `{actual_status or '-'}`"
            )
        state["drift_status"] = drift_status
        if drift_status in {"drifted", "blocked"}:
            if previous_drift not in {"drifted", "blocked"}:
                state["drift_since"] = now
                state["drift_count"] = int(state.get("drift_count") or 0) + 1
        else:
            state["drift_since"] = 0
        if last_action:
            state["last_action"] = last_action
            state["last_action_at"] = now
        if persist:
            await self._persist_channel_orchestration(name)
        return state

    @staticmethod
    def _resolve_drift_status(desired_state: str, actual_status: str) -> str:
        """Return one compact desired-vs-actual drift state."""
        if desired_state == "running":
            if actual_status == "running":
                return "in_sync"
            if actual_status == "starting":
                return "converging"
            if actual_status in {"disabled", "misconfigured"}:
                return "blocked"
            return "drifted"
        if actual_status in {"", "configured", "disabled", "stopped", "misconfigured"}:
            return "in_sync"
        if actual_status == "starting":
            return "converging"
        return "drifted"

    def _can_start_channel(self, name: str) -> tuple[bool, str]:
        """Return whether config allows the channel to start."""
        spec = self._channel_registry.get(name)
        if spec is None:
            return False, "unknown channel"
        return spec.validate_start(self.config)

    @staticmethod
    def _resolve_blocked_runtime_status(reason: str) -> str:
        """Map one start-blocking reason into a runtime status."""
        if reason == "channel disabled in config":
            return "disabled"
        return "misconfigured"

    async def _run_reconcile_loop(self) -> None:
        """Periodically reconcile runtime channels against desired state."""
        try:
            while True:
                await asyncio.sleep(self._RECONCILE_INTERVAL_SECONDS)
                await self.reconcile_channels(trigger="periodic")
        except asyncio.CancelledError:
            raise

    async def _start_managed_channel(self, name: str) -> dict[str, object]:
        """Start one managed channel and return its runtime state."""
        spec = self._channel_registry.get(name)
        if spec is None or not spec.managed:
            raise ValueError(f"Unknown managed channel `{name}`")
        can_start, reason = spec.validate_start(self.config)
        if not can_start:
            blocked_status = self._resolve_blocked_runtime_status(reason)
            self._set_channel_runtime(name, blocked_status, detail=reason)
            raise RuntimeError(f"{spec.label} is not startable: {reason}")
        if name in self.channels:
            self._set_channel_runtime(name, "running", detail=spec.running_detail)
            return dict(self._channel_runtime[name])

        self._set_channel_runtime(name, "starting", detail=spec.starting_detail)
        channel = spec.create_channel(self.config, self)

        try:
            started = await channel.start()
        except Exception as exc:
            self._set_channel_runtime(
                name,
                "failed",
                detail=f"{name} start failed",
                last_error=str(exc),
            )
            logger.error(f"{spec.label} channel failed to start: {exc}")
            return dict(self._channel_runtime[name])

        if started:
            self.channels[name] = channel
            self._set_channel_runtime(name, "running", detail=spec.running_detail)
            logger.info(f"{spec.label} channel started")
        else:
            self._set_channel_runtime(
                name,
                "failed",
                detail=f"{name} start returned inactive",
                last_error="startup did not activate channel runtime",
            )
            logger.error(f"{spec.label} channel failed to start")
        return dict(self._channel_runtime[name])

    async def _stop_managed_channel(
        self,
        name: str,
        *,
        detail: str = "stopped by operator",
    ) -> dict[str, object]:
        """Stop one managed channel and return its runtime state."""
        if name not in self._managed_channels:
            raise ValueError(f"Unknown managed channel `{name}`")

        channel = self.channels.get(name)
        if channel is None:
            current = dict(self._channel_runtime.get(name) or {})
            if str(current.get("status") or "") == "disabled":
                return current
            self._set_channel_runtime(name, "stopped", detail="no active runtime")
            return dict(self._channel_runtime[name])

        try:
            await channel.stop()
        except Exception as exc:
            self._set_channel_runtime(
                name,
                "failed",
                detail=f"{name} stop failed",
                last_error=str(exc),
            )
            logger.error(f"Error stopping {name} channel: {exc}")
            return dict(self._channel_runtime[name])

        self.channels.pop(name, None)
        self._set_channel_runtime(name, "stopped", detail=detail)
        logger.info(f"{name.capitalize()} channel stopped")
        return dict(self._channel_runtime[name])

    @staticmethod
    def _new_channel_diagnostics(name: str) -> dict[str, object]:
        """Build the default diagnostics payload for one channel."""
        return {
            "channel": name,
            "incoming_total": 0,
            "incoming_successes": 0,
            "incoming_failures": 0,
            "outgoing_total": 0,
            "outgoing_successes": 0,
            "outgoing_failures": 0,
            "targeted_outgoing_total": 0,
            "targeted_outgoing_successes": 0,
            "targeted_outgoing_failures": 0,
            "last_incoming_at": 0,
            "last_incoming_session": "",
            "last_outgoing_at": 0,
            "last_outgoing_status": "",
            "last_outgoing_kind": "",
            "last_outgoing_target": "",
            "last_success_at": 0,
            "last_failure_at": 0,
            "last_failure_kind": "",
            "last_failure_error": "",
            "last_runtime_status": "disabled",
            "last_runtime_transition_at": 0,
        }

    def _ensure_channel_diagnostics(self, name: str) -> dict[str, object]:
        """Return one mutable diagnostics row for the channel."""
        diagnostics = self._channel_diagnostics.get(name)
        if diagnostics is None:
            diagnostics = self._new_channel_diagnostics(name)
            self._channel_diagnostics[name] = diagnostics
        return diagnostics

    def _record_channel_incoming(
        self,
        name: str,
        *,
        session_id: str,
        success: bool,
        error: str = "",
    ) -> None:
        """Record one inbound agent-routing event for a channel."""
        if name not in self._channel_diagnostics:
            return
        diagnostics = self._ensure_channel_diagnostics(name)
        diagnostics["incoming_total"] = int(diagnostics["incoming_total"]) + 1
        diagnostics["last_incoming_at"] = int(time.time())
        diagnostics["last_incoming_session"] = session_id
        if success:
            diagnostics["incoming_successes"] = int(diagnostics["incoming_successes"]) + 1
            diagnostics["last_success_at"] = int(time.time())
            return
        diagnostics["incoming_failures"] = int(diagnostics["incoming_failures"]) + 1
        diagnostics["last_failure_at"] = int(time.time())
        diagnostics["last_failure_kind"] = "incoming"
        diagnostics["last_failure_error"] = error

    def _record_channel_outgoing(
        self,
        name: str,
        *,
        success: bool,
        target_id: str,
        delivery_kind: str,
        error: str = "",
    ) -> None:
        """Record one proactive delivery attempt for a channel."""
        if name not in self._channel_diagnostics:
            return
        diagnostics = self._ensure_channel_diagnostics(name)
        now = int(time.time())
        diagnostics["last_outgoing_at"] = now
        diagnostics["last_outgoing_status"] = "success" if success else "error"
        diagnostics["last_outgoing_kind"] = delivery_kind
        diagnostics["last_outgoing_target"] = target_id
        if delivery_kind == "targeted_proactive":
            diagnostics["targeted_outgoing_total"] = int(diagnostics["targeted_outgoing_total"]) + 1
            if success:
                diagnostics["targeted_outgoing_successes"] = int(
                    diagnostics["targeted_outgoing_successes"]
                ) + 1
            else:
                diagnostics["targeted_outgoing_failures"] = int(
                    diagnostics["targeted_outgoing_failures"]
                ) + 1
        else:
            diagnostics["outgoing_total"] = int(diagnostics["outgoing_total"]) + 1
            if success:
                diagnostics["outgoing_successes"] = int(diagnostics["outgoing_successes"]) + 1
            else:
                diagnostics["outgoing_failures"] = int(diagnostics["outgoing_failures"]) + 1
        if success:
            diagnostics["last_success_at"] = now
            return
        diagnostics["last_failure_at"] = now
        diagnostics["last_failure_kind"] = delivery_kind
        diagnostics["last_failure_error"] = error


# Global gateway instance
_gateway: Optional[Gateway] = None


def get_gateway() -> Optional[Gateway]:
    """Get the global Gateway instance."""
    return _gateway


def set_gateway(gateway: Gateway) -> None:
    """Set the global Gateway instance."""
    global _gateway
    _gateway = gateway
