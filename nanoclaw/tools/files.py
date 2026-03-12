"""File operation tools restricted by the shared boundary policy."""

from __future__ import annotations

import os

from nanoclaw.security.boundary import get_tool_boundary_policy
from nanoclaw.tools.registry import tool


@tool(
    name="file_read",
    description="Read the contents of a file from the workspace.",
    parameters={
        "path": {
            "type": "string",
            "description": (
                "File path relative to workspace "
                "(e.g., 'report.md', 'data/results.csv')"
            ),
        }
    },
    catalog_summary="Read a file inside the workspace.",
    catalog_entry_points=["read this file", "show file content"],
    risk_level="low",
)
async def file_read(path: str) -> str:
    """Read file from workspace."""
    policy = get_tool_boundary_policy()
    allowed, reason, safe_path = policy.validate_file_read(
        path,
        operation="file_read",
    )
    if not allowed or safe_path is None:
        return reason

    if not safe_path.exists():
        return f"File not found: {path}"

    if not safe_path.is_file():
        return f"Not a file: {path}"

    # Use O_NOFOLLOW to atomically reject symlinks at open() time (no TOCTOU)
    try:
        fd = os.open(str(safe_path), os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as e:
        if e.errno == 40:  # ELOOP, is a symlink
            return f"ACCESS DENIED: symlink points outside workspace: {path}"
        return f"Error reading file: {e}"

    try:
        # Read 16KB to guarantee 4000+ chars even with 4-byte UTF-8
        raw = os.read(fd, 16384)
        content = raw.decode("utf-8", errors="replace")
    except Exception as e:
        return f"Error reading file: {e}"
    finally:
        os.close(fd)

    if len(content) > 4000:
        content = content[:4000] + "\n...[truncated]"

    return content


@tool(
    name="file_write",
    description="Create or overwrite a file in the workspace.",
    parameters={
        "path": {
            "type": "string",
            "description": "File path relative to workspace",
        },
        "content": {
            "type": "string",
            "description": "Content to write",
        },
    },
    catalog_summary="Write a file inside the workspace.",
    catalog_entry_points=["write this file", "save output"],
    risk_level="medium",
)
async def file_write(path: str, content: str) -> str:
    """Write file to workspace."""
    policy = get_tool_boundary_policy()
    allowed, reason, safe_path = policy.validate_file_write(
        path,
        operation="file_write",
    )
    if not allowed or safe_path is None:
        return reason

    try:
        # Create parent directories if needed
        safe_path.parent.mkdir(parents=True, exist_ok=True)

        # Use O_NOFOLLOW to atomically reject symlinks at open() time (no TOCTOU)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
        fd = os.open(str(safe_path), flags, 0o644)
        try:
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as e:
        if e.errno == 40:  # ELOOP, is a symlink
            return f"ACCESS DENIED: symlink at write target: {path}"
        return f"Error writing file: {e}"
    except Exception as e:
        return f"Error writing file: {e}"

    size = safe_path.stat().st_size
    return f"Written {size} bytes to {path}"


@tool(
    name="file_list",
    description="List files and directories in the workspace.",
    parameters={
        "path": {
            "type": "string",
            "description": "Directory path relative to workspace. Use '.' for root.",
        }
    },
    required=[],  # path is optional, defaults to '.'
    catalog_summary="List files or directories inside the workspace.",
    catalog_entry_points=["list files", "show directory tree"],
    risk_level="low",
)
async def file_list(path: str = ".") -> str:
    """List directory contents in workspace."""
    policy = get_tool_boundary_policy()
    allowed, reason, safe_path = policy.validate_workspace_path(
        path,
        operation="file_list",
    )
    if not allowed or safe_path is None:
        return reason

    if not safe_path.exists():
        return f"Directory not found: {path}"

    if not safe_path.is_dir():
        return f"Not a directory: {path}"

    # Dotfile prefixes to hide from listing
    HIDDEN_PREFIXES = (".env", ".git", ".ssh", ".aws", ".kube", ".docker", ".gnupg")

    entries = []
    try:
        for item in sorted(safe_path.iterdir()):
            name_lower = item.name.lower()
            if any(name_lower.startswith(prefix) for prefix in HIDDEN_PREFIXES):
                continue
            if item.is_dir():
                entries.append(f"[DIR]  {item.name}/")
            else:
                size = item.stat().st_size
                entries.append(f"[FILE] {item.name} ({size}B)")
    except Exception as e:
        return f"Error listing directory: {e}"

    return "\n".join(entries) if entries else "(empty directory)"
