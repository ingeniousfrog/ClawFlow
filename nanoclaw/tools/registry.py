"""Tool registration system with decorator-based discovery.
提供统一的工具注册+Schematic生成+动态调用+skills加载机制"""

from __future__ import annotations

import importlib
import importlib.util
import os
import stat
import sys
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import Any, Callable, Optional

from nanoclaw.core.logger import get_logger
from nanoclaw.core.plugins import load_plugin_manifests_from_directory

logger = get_logger(__name__)


@dataclass
class ToolInfo:
    """Information about a registered tool."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable
    needs_confirmation: bool = False
    required_params: list[str] = field(default_factory=list)
    source_module: str = ""
    catalog_summary: str = ""
    catalog_entry_points: list[str] = field(default_factory=list)
    risk_level: str = ""


# Global tool registry
_registry: dict[str, ToolInfo] = {}
_core_tools_loaded = False
_protected_tool_names: set[str] = set()

_CORE_TOOL_MODULES = (
    "nanoclaw.tools.capabilities",
    "nanoclaw.tools.files",
    "nanoclaw.tools.memory_tools",
    "nanoclaw.tools.shell",
    "nanoclaw.tools.spawn",
    "nanoclaw.tools.web",
    "nanoclaw.tools.web_workflows",
)

_CORE_TOOL_NAMES = {
    "capability_list",
    "file_read",
    "file_write",
    "file_list",
    "shell_exec",
    "web_search",
    "web_fetch",
    "memory_save",
    "memory_search",
    "spawn_task",
}


def _core_tools_present() -> bool:
    """Check if all core tools are registered."""
    return _CORE_TOOL_NAMES.issubset(_registry.keys())


def _load_core_tools() -> None:
    """Ensure core tool modules are imported and registered."""
    global _core_tools_loaded
    if _core_tools_loaded and _core_tools_present():
        return

    needs_reload = not _core_tools_present()
    for module_name in _CORE_TOOL_MODULES:
        try:
            if needs_reload and module_name in sys.modules:
                importlib.reload(sys.modules[module_name])
            else:
                importlib.import_module(module_name)
        except Exception as e:
            logger.error(f"Failed to load core tool module {module_name}: {e}")

    _core_tools_loaded = True
    _protected_tool_names.update(_CORE_TOOL_NAMES)


def _can_register_tool(name: str, source_module: str) -> tuple[bool, str]:
    """Return whether a tool registration should be accepted."""
    existing = _registry.get(name)
    if existing is None:
        return True, ""
    if existing.source_module == source_module:
        return True, ""
    if name in _protected_tool_names:
        return (
            False,
            f"protected tool `{name}` already registered by `{existing.source_module}`",
        )
    return (
        False,
        f"tool `{name}` already registered by `{existing.source_module}`",
    )


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    needs_confirmation: bool = False,
    required: Optional[list[str]] = None,
    catalog_summary: str = "",
    catalog_entry_points: Optional[list[str]] = None,
    risk_level: str = "",
) -> Callable:
    """
    将函数封装为ToolInfo，写入全局_registry。
    Decorator to register a tool.

    Args:
        name: Tool name (used in LLM tool_calls)
        description: Human-readable description for LLM
        parameters: JSON Schema for parameters
        needs_confirmation: If True, always asks user before executing
        required: List of required parameter names (defaults to all)

    Example:
        @tool(
            name="web_search",
            description="Search the internet",
            parameters={"query": {"type": "string", "description": "Search query"}}
        )
        async def web_search(query: str) -> str:
            ...
    """

    def decorator(func: Callable) -> Callable:
        source_module = getattr(func, "__module__", "")
        req_params = required if required is not None else list(parameters.keys())
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            return await func(*args, **kwargs)

        allowed, reason = _can_register_tool(name, source_module)
        if not allowed:
            logger.warning("Skipping tool registration from %s: %s", source_module, reason)
            return wrapper

        _registry[name] = ToolInfo(
            name=name,
            description=description,
            parameters=parameters,
            handler=func,
            needs_confirmation=needs_confirmation,
            required_params=req_params,
            source_module=source_module,
            catalog_summary=catalog_summary,
            catalog_entry_points=list(catalog_entry_points or []),
            risk_level=risk_level,
        )
        if _tool_registry is not None:
            _tool_registry.tools[name] = _registry[name]

        return wrapper

    return decorator


class ToolRegistry:
    """Registry for managing and executing tools."""

    def __init__(self) -> None:
        """Initialize with copy of global registry."""
        self.tools: dict[str, ToolInfo] = dict(_registry)
        self.protected_names: set[str] = set(_protected_tool_names)

    def get_schemas(self) -> list[dict[str, Any]]:
        """
        把工具元数据转换成OpenAI function-calling结构。
        Generate OpenAI-compatible tool schemas for LLM.

        Returns:
            List of tool schemas in OpenAI format
        """
        schemas = []
        for name, info in self.tools.items():
            schemas.append(
                {
                    "type": "function",
                    "function": {
                        "name": info.name,
                        "description": info.description,
                        "parameters": {
                            "type": "object",
                            "properties": info.parameters,
                            "required": info.required_params,
                        },
                    },
                }
            )
        return schemas

    def get_tool_names(self) -> list[str]:
        """Get list of registered tool names."""
        return list(self.tools.keys())

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        confirm_callback: Optional[Callable] = None,
    ) -> str:
        """
        根据工具名找到handler，注入参数并执行。
        Execute a tool by name with given arguments.

        Args:
            name: Tool name
            arguments: Tool arguments
            confirm_callback: Optional async callback for user confirmation

        Returns:
            Tool result as string
        """
        if name not in self.tools:
            return f"Unknown tool: {name}"

        tool_info = self.tools[name]

        # Tools that always need confirmation
        if tool_info.needs_confirmation and confirm_callback:
            import json

            approved = await confirm_callback(
                f"Tool `{name}` wants to run with:\n"
                f"```\n{json.dumps(arguments, indent=2)}\n```\n\nAllow?"
            )
            if not approved:
                return "User denied this action."

        try:
            # Handle malformed LLM output: {'parameters': {'query': '...'}}
            if "parameters" in arguments and len(arguments) == 1:
                logger.warning(f"Tool {name}: unwrapping nested 'parameters' from LLM")
                arguments = arguments["parameters"]

            result = await tool_info.handler(**arguments)
            return str(result)
        except TypeError as e:
            return f"Invalid arguments for {name}: {e}"
        except Exception as e:
            return f"Tool {name} failed: {e}"

    def load_skills(self, skills_dir: str) -> None:
        """
        从目录动态加载.py技能，并做文件权限/所有者检查，防止被他人篡改。
        Auto-discover and load .py files from skills directory.

        Only loads files owned by the current user and not writable by
        group/others (prevents tampering by other users on shared systems).

        Args:
            skills_dir: Path to skills directory
        """
        skills_path = Path(skills_dir)
        if not skills_path.exists():
            logger.debug(f"Skills directory not found: {skills_dir}")
            return

        manifest_map = {
            item.module: item
            for item in load_plugin_manifests_from_directory(skills_path)
            if item.kind == "skill"
        }

        # Check directory ownership and permissions
        try:
            dir_stat = skills_path.stat()
            if dir_stat.st_uid != os.getuid():
                logger.warning(f"Skills directory not owned by current user: {skills_dir}")
                return
            if dir_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                logger.warning(f"Skills directory writable by others: {skills_dir}")
                return
        except OSError:
            return

        for py_file in skills_path.glob("*.py"):
            if py_file.name.startswith("_"):
                continue

            manifest = manifest_map.get(py_file.stem)
            if manifest and not manifest.enabled:
                logger.info("Skipping disabled skill manifest for %s", py_file.name)
                continue

            # Validate file ownership and permissions
            try:
                file_stat = py_file.stat()
                if file_stat.st_uid != os.getuid():
                    logger.warning(f"Skipping skill {py_file.name}: not owned by current user")
                    continue
                if file_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    logger.warning(f"Skipping skill {py_file.name}: writable by group/others")
                    continue
            except OSError:
                continue

            try:
                spec = importlib.util.spec_from_file_location(py_file.stem, py_file)
                if spec and spec.loader:
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    logger.debug(f"Loaded skill: {py_file.name}")
            except Exception as e:
                logger.error(f"Failed to load skill {py_file.name}: {e}")

        # Merge newly loaded tools
        self.tools.update(_registry)
        self.protected_names.update(_protected_tool_names)

    def register(self, tool_info: ToolInfo) -> None:
        """Manually register a tool."""
        allowed, reason = _can_register_tool(tool_info.name, tool_info.source_module)
        if not allowed:
            logger.warning("Skipping manual tool registration: %s", reason)
            return
        self.tools[tool_info.name] = tool_info
        _registry[tool_info.name] = tool_info

    def protect_tool_names(self, names: list[str]) -> None:
        """Protect tool names from later overrides."""
        self.protected_names.update(names)
        _protected_tool_names.update(names)


# Global registry instance
_tool_registry: Optional[ToolRegistry] = None


def get_tool_registry() -> ToolRegistry:
    """Get the global tool registry instance."""
    global _tool_registry
    _load_core_tools()
    if _tool_registry is None:
        _tool_registry = ToolRegistry()
    else:
        _tool_registry.tools.update(_registry)
    return _tool_registry


def reset_registry() -> None:
    """Reset the global registry (useful for testing)."""
    global _tool_registry, _registry, _core_tools_loaded, _protected_tool_names
    _registry.clear()
    _tool_registry = None
    _core_tools_loaded = False
    _protected_tool_names.clear()
