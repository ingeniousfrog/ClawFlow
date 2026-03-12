"""Per-tool runtime context helpers."""

from __future__ import annotations

from copy import deepcopy
from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolRuntimeContext:
    """Context available to the currently executing tool."""

    session_id: str = ""
    task_id: str = ""
    step_id: str = ""
    task_attempt: int = 0
    workflow_identity: str = ""


_tool_runtime_context: ContextVar[ToolRuntimeContext | None] = ContextVar(
    "tool_runtime_context",
    default=None,
)
_boundary_decision_trace: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "boundary_decision_trace",
    default=None,
)
_secret_access_trace: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "secret_access_trace",
    default=None,
)


def get_tool_runtime_context() -> ToolRuntimeContext:
    """Return the active tool runtime context or an empty default."""
    return _tool_runtime_context.get() or ToolRuntimeContext()


def set_tool_runtime_context(
    session_id: str = "",
    task_id: str = "",
    step_id: str = "",
    task_attempt: int = 0,
    workflow_identity: str = "",
) -> Token[ToolRuntimeContext | None]:
    """Install runtime context for one tool execution."""
    return _tool_runtime_context.set(
        ToolRuntimeContext(
            session_id=session_id,
            task_id=task_id,
            step_id=step_id,
            task_attempt=task_attempt,
            workflow_identity=workflow_identity,
        )
    )


def reset_tool_runtime_context(token: Token[ToolRuntimeContext | None]) -> None:
    """Restore the previous tool runtime context."""
    _tool_runtime_context.reset(token)


def begin_boundary_decision_trace() -> Token[list[dict[str, Any]] | None]:
    """Start one fresh boundary-decision trace for the active tool run."""
    return _boundary_decision_trace.set([])


def record_boundary_decision(decision: dict[str, Any]) -> None:
    """Append one boundary decision to the active tool trace."""
    trace = _boundary_decision_trace.get()
    if trace is None:
        return
    trace.append(dict(decision))


def get_boundary_decision_trace() -> list[dict[str, Any]]:
    """Return a copy of the active boundary-decision trace."""
    trace = _boundary_decision_trace.get()
    if not trace:
        return []
    return [deepcopy(item) for item in trace]


def reset_boundary_decision_trace(
    token: Token[list[dict[str, Any]] | None],
) -> None:
    """Restore the previous boundary-decision trace."""
    _boundary_decision_trace.reset(token)


def begin_secret_access_trace() -> Token[list[dict[str, Any]] | None]:
    """Start one fresh secret-access trace for the active tool run."""
    return _secret_access_trace.set([])


def record_secret_access(access: dict[str, Any]) -> None:
    """Append one secret-access record to the active tool trace."""
    trace = _secret_access_trace.get()
    if trace is None:
        return
    trace.append(dict(access))


def get_secret_access_trace() -> list[dict[str, Any]]:
    """Return a copy of the active secret-access trace."""
    trace = _secret_access_trace.get()
    if not trace:
        return []
    return [deepcopy(item) for item in trace]


def reset_secret_access_trace(
    token: Token[list[dict[str, Any]] | None],
) -> None:
    """Restore the previous secret-access trace."""
    _secret_access_trace.reset(token)
