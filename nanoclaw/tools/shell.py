"""Shell execution tool routed through ShellSandbox."""

from __future__ import annotations

from typing import Callable, Optional

from nanoclaw.security.sandbox import SecurityError, get_shell_sandbox
from nanoclaw.tools.registry import tool

# Global confirm callback - set by channel
_confirm_callback: Optional[Callable] = None


def set_confirm_callback(callback: Optional[Callable]) -> None:
    """Set the global confirmation callback."""
    global _confirm_callback
    _confirm_callback = callback


def get_confirm_callback() -> Optional[Callable]:
    """Get the global confirmation callback."""
    return _confirm_callback


@tool(
    name="shell_exec",
    description=(
        "Execute a shell command on the server. "
        "Commands run in the workspace directory. "
        "Dangerous commands are blocked, destructive commands require user confirmation."
    ),
    parameters={
        "command": {
            "type": "string",
            "description": "Shell command to execute",
        }
    },
    catalog_summary="Run shell commands inside the sandbox with confirmation for risky actions.",
    catalog_entry_points=["run a command", "inspect the repo", "check the environment"],
    risk_level="high",
)
async def shell_exec(command: str) -> str:
    """Execute a shell command through the configured sandbox mode."""
    sandbox = get_shell_sandbox()
    confirm_cb = get_confirm_callback()

    try:
        result = await sandbox.execute(
            command, timeout=30, confirm_callback=confirm_cb
        )
        return f"Exit code: {result.exit_code}\n{result.output}"
    except SecurityError as e:
        return f"BLOCKED: {e}"
    except Exception as e:
        return f"ERROR: {e}"
