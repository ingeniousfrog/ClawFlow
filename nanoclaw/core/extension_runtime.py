"""Runtime helpers for isolated third-party extension execution."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from nanoclaw.core.config import get_config
from nanoclaw.core.logger import get_logger

logger = get_logger(__name__)


def should_isolate_extension_runtime(*, kind: str, source_scope: str) -> bool:
    """Return whether the current config isolates one extension kind."""
    if str(source_scope or "").strip() != "user":
        return False
    try:
        policy = get_config().extensions
        return bool(policy.isolates_kind(kind))
    except Exception:
        return kind == "search_provider" and source_scope == "user"


def get_extension_isolation_timeout() -> int:
    """Return the configured subprocess timeout for isolated extensions."""
    try:
        timeout = int(get_config().extensions.isolated_timeout_seconds)
    except Exception:
        timeout = 15
    return max(5, timeout)


async def run_isolated_extension(
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Execute one supported extension request in a separate Python process."""
    timeout = get_extension_isolation_timeout()
    process = None
    with tempfile.TemporaryDirectory(prefix="nanoclaw-ext-") as isolated_root:
        child_env = _build_child_env(Path(isolated_root))
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "nanoclaw.core.extension_runner",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_project_root()),
            env=child_env,
        )
        stdin_bytes = json.dumps(payload).encode("utf-8")
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(stdin_bytes),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.communicate()
            raise ValueError(f"extension isolation timeout after {timeout}s")
    if process.returncode != 0:
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        detail = stdout_text or stderr_text or "extension subprocess failed"
        raise ValueError(detail)
    try:
        response = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        logger.error("Failed to decode isolated extension response: %s", exc)
        raise ValueError("extension subprocess returned invalid JSON") from exc
    if not bool(response.get("ok", False)):
        raise ValueError(str(response.get("error") or "extension subprocess failed"))
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError("extension subprocess returned invalid result payload")
    return result


async def run_isolated_search_provider(
    *,
    handler_path: str,
    manifest_name: str,
    manifest_path: str,
    source_scope: str,
    query: str,
    web_config: Any,
    plan: Any | None = None,
) -> dict[str, Any]:
    """Execute one user search provider in an isolated subprocess."""
    return await run_isolated_extension(
        payload={
            "kind": "search_provider",
            "handlerPath": handler_path,
            "manifestName": manifest_name,
            "manifestPath": manifest_path,
            "sourceScope": source_scope,
            "query": query,
            "webConfig": _model_dump(web_config),
            "plan": _model_dump(plan) if plan is not None else None,
        }
    )


async def run_isolated_channel_action(
    *,
    factory_path: str,
    manifest_name: str,
    manifest_path: str,
    source_scope: str,
    channel_config: Any,
    action: str,
    text: str = "",
    target_id: str = "",
) -> dict[str, Any]:
    """Execute one channel action in an isolated subprocess."""
    return await run_isolated_extension(
        payload={
            "kind": "channel",
            "factoryPath": factory_path,
            "manifestName": manifest_name,
            "manifestPath": manifest_path,
            "sourceScope": source_scope,
            "channelConfig": _model_dump(channel_config),
            "action": action,
            "text": text,
            "targetId": target_id,
        }
    )


class SubprocessChannelProxy:
    """Proxy a proactive-only user channel through isolated subprocess actions."""

    def __init__(
        self,
        *,
        factory_path: str,
        manifest_name: str,
        manifest_path: str,
        source_scope: str,
        channel_config: Any,
    ) -> None:
        self.factory_path = factory_path
        self.manifest_name = manifest_name
        self.manifest_path = manifest_path
        self.source_scope = source_scope
        self.channel_config = channel_config

    async def start(self) -> bool:
        """Preflight the channel by running its `start()` hook in isolation."""
        result = await run_isolated_channel_action(
            factory_path=self.factory_path,
            manifest_name=self.manifest_name,
            manifest_path=self.manifest_path,
            source_scope=self.source_scope,
            channel_config=self.channel_config,
            action="start",
        )
        return bool(result.get("result", True))

    async def stop(self) -> None:
        """Run the channel stop hook in isolation."""
        await run_isolated_channel_action(
            factory_path=self.factory_path,
            manifest_name=self.manifest_name,
            manifest_path=self.manifest_path,
            source_scope=self.source_scope,
            channel_config=self.channel_config,
            action="stop",
        )

    async def send_proactive(self, text: str) -> None:
        """Run proactive delivery in isolation."""
        await run_isolated_channel_action(
            factory_path=self.factory_path,
            manifest_name=self.manifest_name,
            manifest_path=self.manifest_path,
            source_scope=self.source_scope,
            channel_config=self.channel_config,
            action="send_proactive",
            text=text,
        )

    async def send_proactive_to(self, target_id: str, text: str) -> bool:
        """Run targeted proactive delivery in isolation."""
        result = await run_isolated_channel_action(
            factory_path=self.factory_path,
            manifest_name=self.manifest_name,
            manifest_path=self.manifest_path,
            source_scope=self.source_scope,
            channel_config=self.channel_config,
            action="send_proactive_to",
            text=text,
            target_id=target_id,
        )
        return bool(result.get("result", False))


def _build_child_env(isolated_root: Path) -> dict[str, str]:
    """Return a stripped child environment for extension subprocesses."""
    tmp_dir = isolated_root / "tmp"
    config_dir = isolated_root / ".config"
    cache_dir = isolated_root / ".cache"
    data_dir = isolated_root / ".local" / "share"
    for path in (tmp_dir, config_dir, cache_dir, data_dir):
        path.mkdir(parents=True, exist_ok=True)

    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "LC_ALL": os.environ.get("LC_ALL", os.environ.get("LANG", "C.UTF-8")),
        "HOME": str(isolated_root),
        "TMPDIR": str(tmp_dir),
        "TMP": str(tmp_dir),
        "TEMP": str(tmp_dir),
        "XDG_CONFIG_HOME": str(config_dir),
        "XDG_CACHE_HOME": str(cache_dir),
        "XDG_DATA_HOME": str(data_dir),
        "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    python_path = os.environ.get("PYTHONPATH", "").strip()
    if python_path:
        child_env["PYTHONPATH"] = python_path
    return child_env


def _project_root() -> Path:
    """Return the repository root used for isolated extension subprocesses."""
    return Path(__file__).resolve().parents[2]


def _model_dump(value: Any) -> dict[str, Any]:
    """Dump a pydantic-like object into a plain dictionary."""
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, dict):
        return dict(value)
    return {
        key: getattr(value, key)
        for key in dir(value)
        if not key.startswith("_") and not callable(getattr(value, key))
    }
