"""Isolated subprocess entrypoint for user-installed extension execution."""

from __future__ import annotations

import asyncio
import json
import sys
from typing import Any

from nanoclaw.core.config import WebSearchConfig
from nanoclaw.core.plugins import load_manifest_object
from nanoclaw.tools.search_planner import SearchQueryPlan


async def _run(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one supported extension runtime request."""
    request_kind = str(payload.get("kind") or "").strip()
    if request_kind == "search_provider":
        return await _run_search_provider(payload)
    if request_kind == "channel":
        return await _run_channel_action(payload)
    raise ValueError(f"Unsupported extension runner kind: {request_kind}")


async def _run_search_provider(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one isolated search-provider request."""
    handler = load_manifest_object(
        str(payload.get("handlerPath") or ""),
        manifest_name=str(payload.get("manifestName") or ""),
        manifest_path=str(payload.get("manifestPath") or ""),
        source_scope=str(payload.get("sourceScope") or ""),
    )
    if not callable(handler):
        raise ValueError("Extension handler is not callable.")

    web_cfg = WebSearchConfig(**dict(payload.get("webConfig") or {}))
    plan_payload = payload.get("plan")
    plan = SearchQueryPlan(**plan_payload) if isinstance(plan_payload, dict) else None
    result = await handler(
        str(payload.get("query") or ""),
        web_cfg,
        plan,
    )
    return _normalize_result(result)


async def _run_channel_action(payload: dict[str, Any]) -> dict[str, Any]:
    """Execute one isolated channel action."""
    factory = load_manifest_object(
        str(payload.get("factoryPath") or ""),
        manifest_name=str(payload.get("manifestName") or ""),
        manifest_path=str(payload.get("manifestPath") or ""),
        source_scope=str(payload.get("sourceScope") or ""),
    )
    channel = factory(_build_config_object(payload.get("channelConfig")), None)
    action = str(payload.get("action") or "").strip()
    if action not in {"start", "stop", "send_proactive", "send_proactive_to"}:
        raise ValueError(f"Unsupported isolated channel action: {action}")
    method = getattr(channel, action, None)
    if method is None or not callable(method):
        raise ValueError(f"Channel does not implement `{action}`.")
    if action == "send_proactive":
        result = await method(str(payload.get("text") or ""))
        return {"action": action, "ok": True, "result": bool(result) if result is not None else True}
    if action == "send_proactive_to":
        result = await method(
            str(payload.get("targetId") or ""),
            str(payload.get("text") or ""),
        )
        return {"action": action, "ok": bool(result), "result": bool(result)}
    result = await method()
    return {"action": action, "ok": True, "result": bool(result) if action == "start" else True}


def _normalize_result(result: Any) -> dict[str, Any]:
    """Normalize one extension return value into JSON-friendly output."""
    if hasattr(result, "model_dump"):
        data = result.model_dump(mode="json")
    elif isinstance(result, dict):
        data = dict(result)
    else:
        data = {
            "text": str(result),
            "ok": False,
            "provider": "extension",
        }
    return {
        "text": str(data.get("text") or ""),
        "ok": bool(data.get("ok", False)),
        "provider": str(data.get("provider") or "extension"),
        "evidence_items": list(data.get("evidence_items") or []),
    }


def _build_config_object(value: Any) -> Any:
    """Return a config object supporting both attr and item access."""
    if isinstance(value, dict):
        return _ConfigNamespace(
            {
                str(key): _build_config_object(item)
                for key, item in value.items()
            }
        )
    if isinstance(value, list):
        return [_build_config_object(item) for item in value]
    return value


class _ConfigNamespace(dict):
    """Small dict wrapper that also supports attribute access."""

    def __getattr__(self, name: str) -> Any:
        if name in self:
            return self[name]
        raise AttributeError(name)


def main() -> int:
    """Run one extension request from stdin and write JSON to stdout."""
    try:
        payload = json.loads(sys.stdin.read() or "{}")
        result = asyncio.run(_run(payload))
        sys.stdout.write(json.dumps({"ok": True, "result": result}))
        return 0
    except Exception as exc:
        sys.stdout.write(
            json.dumps(
                {
                    "ok": False,
                    "error": str(exc),
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
