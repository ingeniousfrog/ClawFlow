"""Shell sandbox and file guard for secure execution."""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from nanoclaw.core.logger import get_logger
from nanoclaw.security.sandbox_backends import VALID_SHELL_BACKENDS

logger = get_logger(__name__)


class SecurityError(Exception):
    """Security violation error."""

    pass


@dataclass
class ShellResult:
    """Result of shell command execution."""

    output: str
    exit_code: int


class FileGuard:
    """All file operations restricted to workspace directory only."""

    # Blocked path component names (matched case-insensitively against each part)
    BLOCKED_COMPONENTS = [
        ".env", ".git", ".ssh", ".gnupg", ".aws", ".kube", ".docker",
    ]

    # Blocked file name patterns (matched case-insensitively against the file name)
    BLOCKED_FILENAMES = [
        "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
        "private_key", "config.json",
    ]

    # Blocked file name prefixes (matched case-insensitively)
    BLOCKED_PREFIXES = [
        ".env.",  # .env.local, .env.production, etc.
    ]

    def __init__(self, workspace_dir: str | Path):
        """
        Initialize FileGuard.

        Args:
            workspace_dir: Path to the workspace directory
        """
        self.workspace = Path(workspace_dir).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)

    def validate_path(self, path: str) -> Path:
        """Validate and resolve a path within the workspace."""
        # Handle empty or root paths
        if not path or path == ".":
            return self.workspace

        # Reject null bytes (filesystem truncation attack)
        if "\x00" in path:
            raise SecurityError("ACCESS DENIED: null byte in path")

        # Resolve the path relative to workspace
        resolved = (self.workspace / path).resolve()

        # Check if resolved path is within workspace
        try:
            resolved.relative_to(self.workspace)
        except ValueError:
            raise SecurityError(f"ACCESS DENIED: path outside workspace: {path}")

        return resolved

    def is_symlink_safe(self, path: Path) -> bool:
        """
        Check if a path is a symlink pointing outside the workspace.

        Args:
            path: Path to check

        Returns:
            True if safe (not a symlink, or symlink within workspace), False otherwise
        """
        if not path.is_symlink():
            return True
        try:
            target = path.resolve()
            target.relative_to(self.workspace)
            return True
        except ValueError:
            return False

    def is_safe_to_read(self, path: Path) -> bool:
        """
        Check if a file is safe to read.

        Args:
            path: Path to check

        Returns:
            True if safe, False if blocked
        """
        # Reject symlinks pointing outside workspace
        if not self.is_symlink_safe(path):
            return False

        resolved = path.resolve()
        parts = [p.lower() for p in resolved.parts]
        filename = resolved.name.lower()

        # Check each path component against blocked components
        for component in parts:
            if component in self.BLOCKED_COMPONENTS:
                return False

        # Check filename against blocked filenames
        if filename in self.BLOCKED_FILENAMES:
            return False

        # Check filename against blocked prefixes
        for prefix in self.BLOCKED_PREFIXES:
            if filename.startswith(prefix):
                return False

        return True

    def is_safe_to_write(self, path: Path) -> bool:
        """
        Check if a file path is safe to write to.

        Args:
            path: Path to check

        Returns:
            True if safe, False if blocked
        """
        # Reject symlinks pointing outside workspace
        if path.exists() and not self.is_symlink_safe(path):
            return False

        resolved = path.resolve() if path.exists() else path
        parts = [p.lower() for p in resolved.parts]
        filename = resolved.name.lower()

        # Block writing into sensitive directories
        for component in parts:
            if component in self.BLOCKED_COMPONENTS:
                return False

        # Block overwriting sensitive filenames
        if filename in self.BLOCKED_FILENAMES:
            return False

        for prefix in self.BLOCKED_PREFIXES:
            if filename.startswith(prefix):
                return False

        return True


class ShellSandbox:
    """Sandboxed shell execution with policy gating and explicit runner modes."""

    MODE_DISABLED = "disabled"
    MODE_INLINE = "inline"
    MODE_SUBPROCESS = "subprocess"
    VALID_MODES = {MODE_DISABLED, MODE_INLINE, MODE_SUBPROCESS}
    BACKEND_NATIVE = "native"
    BACKEND_DEFAULT = "portable"
    VALID_BACKENDS = set(VALID_SHELL_BACKENDS)
    MAX_OUTPUT_CHARS = 10000
    _RUNNER_GRACE_SECONDS = 2

    # === TIER 1: BLOCKED - Never executed, instant reject ===
    BLOCKED_PATTERNS = [
        r"rm\s+(-[a-zA-Z]*)?rf\s+[/~]",  # rm -rf / or ~
        r"mkfs\.",  # format disk
        r"dd\s+if=",  # disk destroy
        r">\s*/dev/sd",  # overwrite disk
        r"chmod\s+(-R\s+)?777\s+/",  # permissions nuke
        r"curl.*\|\s*(ba)?sh",  # pipe to shell
        r"wget.*\|\s*(ba)?sh",  # pipe to shell
        r"python.*-c.*import\s+os",  # python os escape
        r"nc\s+-[le]",  # netcat listener
        r"ncat\s",  # ncat
        r"/etc/(passwd|shadow|sudoers)",  # system files
        r"~/.ssh",  # SSH keys
        r"~/.nanoclaw/config",  # our config
        r"iptables",  # firewall
        r"ufw\s",  # firewall
        r"ssh-keygen",  # key generation
        r"crontab\s+-[re]",  # system cron
        r"eval\s*\(",  # eval injection
        r"exec\s*\(",  # exec injection
        r"base64.*\|\s*(ba)?sh",  # encoded execution
        r"history\s",  # command history
        r"printenv",  # environment variables
        r"\benv\b",  # environment variables
        r"set\s*$",  # shell variables
        r"export\s+.*=",  # setting env vars
        r"source\s+",  # sourcing scripts
        r"\.\s+/",  # dot sourcing
        r"ln\s+(-[a-zA-Z]*)?s\s",  # symlink creation
        r"/proc/",  # proc filesystem access
        r"/sys/",  # sys filesystem access
        r"\bstrace\b",  # process tracing
        r"\bltrace\b",  # library tracing
        r"\bgdb\b",  # debugger
        r"\blldb\b",  # debugger
        r"\bperf\b",  # performance tracing
        r"\bdeclare\s+-p",  # dump shell variables
        r"\bcompgen\b",  # shell internals
        r"\$\{",  # variable expansion (bypass for $HOME, etc.)
        r"\$\(",  # command substitution $(...)
        r"`[^`]+`",  # backtick command substitution
    ]

    # === TIER 2: CONFIRM - Executed only after user approval ===
    CONFIRM_PATTERNS = [
        r"\brm\s",  # any delete
        r"\bmv\s",  # any move/rename
        r"pip\s+install",  # installing packages
        r"apt(-get)?\s+install",  # installing packages
        r"brew\s+install",  # installing packages
        r"npm\s+install",  # installing packages
        r"sudo\s",  # elevated privileges
        r"kill\s",  # killing processes
        r"pkill\s",  # killing processes
        r"systemctl",  # service management
        r"docker\s",  # container operations
        r"git\s+push",  # pushing code
        r"git\s+reset.*--hard",  # destructive git
        r"chmod\s",  # changing permissions
        r"chown\s",  # changing ownership
        r">\s",  # output redirection (overwrite)
    ]

    def __init__(
        self,
        workspace_dir: str | Path,
        *,
        mode: str = MODE_SUBPROCESS,
        backend: str = BACKEND_DEFAULT,
        container_image: str = "",
        confirm_dangerous: bool = True,
        default_timeout: int = 30,
        max_memory_mb: int = 512,
        max_file_size_kb: int = 8192,
        isolate_home: bool = True,
    ):
        """
        Initialize ShellSandbox.

        Args:
            workspace_dir: Directory where commands will execute
            mode: Execution mode: disabled, inline, or subprocess
            backend: Subprocess backend selector
            container_image: Container image for docker/podman backends
            confirm_dangerous: Whether confirm-pattern commands need approval
            default_timeout: Default execution timeout in seconds
            max_memory_mb: Max virtual memory for subprocess mode, 0 disables
            max_file_size_kb: Max file size writes for subprocess mode, 0 disables
            isolate_home: Whether subprocess mode uses an isolated HOME/TMP root
        """
        self.workspace = Path(workspace_dir).resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.mode = mode if mode in self.VALID_MODES else self.MODE_SUBPROCESS
        self.backend = backend if backend in self.VALID_BACKENDS else self.BACKEND_DEFAULT
        self.container_image = str(container_image or "").strip()
        self.confirm_dangerous = confirm_dangerous
        self.default_timeout = default_timeout
        self.max_memory_mb = max(0, int(max_memory_mb))
        self.max_file_size_kb = max(0, int(max_file_size_kb))
        self.isolate_home = bool(isolate_home)
        if mode not in self.VALID_MODES:
            logger.warning(
                "Unknown shell sandbox mode %r; falling back to %s",
                mode,
                self.MODE_SUBPROCESS,
            )
        if backend not in self.VALID_BACKENDS:
            logger.warning(
                "Unknown shell sandbox backend %r; falling back to %s",
                backend,
                self.BACKEND_DEFAULT,
            )

        # Pre-compile patterns for performance
        self._blocked_compiled = [
            re.compile(p, re.IGNORECASE) for p in self.BLOCKED_PATTERNS
        ]
        self._confirm_compiled = [
            re.compile(p, re.IGNORECASE) for p in self.CONFIRM_PATTERNS
        ]

    # Pattern to split chained shell commands
    _CHAIN_SPLIT = re.compile(r"\s*(?:;|&&|\|\||\|)\s*")

    def _split_commands(self, command: str) -> list[str]:
        """Split a chained command string into individual commands."""
        return [part.strip() for part in self._CHAIN_SPLIT.split(command) if part.strip()]

    def is_blocked(self, command: str) -> tuple[bool, str]:
        """
        Check if command matches blocked patterns.
        Checks both the full command and each individual chained part.

        Returns:
            (is_blocked, matched_pattern)
        """
        # Check the full command first (catches pipe patterns like curl...|sh)
        for pattern in self._blocked_compiled:
            if pattern.search(command):
                return True, pattern.pattern
        # Then check each split part individually
        parts = self._split_commands(command)
        for part in parts:
            for pattern in self._blocked_compiled:
                if pattern.search(part):
                    return True, pattern.pattern
        return False, ""

    def needs_confirmation(self, command: str) -> bool:
        """Check if command needs user confirmation."""
        # Check full command first
        for pattern in self._confirm_compiled:
            if pattern.search(command):
                return True
        # Then check each split part
        parts = self._split_commands(command)
        for part in parts:
            for pattern in self._confirm_compiled:
                if pattern.search(part):
                    return True
        return False

    def classify_command(self, command: str) -> tuple[str, str]:
        """Classify a command as blocked, confirm, or allowed."""
        is_blocked, pattern = self.is_blocked(command)
        if is_blocked:
            return "blocked", pattern
        if self.confirm_dangerous and self.needs_confirmation(command):
            return "confirm", "matched confirm rule"
        return "allowed", ""

    async def execute(
        self,
        command: str,
        timeout: Optional[int] = None,
        confirm_callback: Optional[Callable] = None,
    ) -> ShellResult:
        """Execute a command after policy checks and mode selection."""
        effective_timeout = timeout or self.default_timeout

        if self.mode == self.MODE_DISABLED:
            return ShellResult(
                output="DENIED: shell execution is disabled by config",
                exit_code=-1,
            )

        decision, reason = self.classify_command(command)
        if decision == "blocked":
            logger.warning("BLOCKED command: %s (pattern: %s)", command, reason)
            raise SecurityError("BLOCKED: dangerous command detected")

        if decision == "confirm":
            if confirm_callback is None:
                logger.warning(
                    "DENIED command (no confirmation callback): %s",
                    command,
                )
                return ShellResult(
                    output="DENIED: command requires confirmation but no callback available",
                    exit_code=-1,
                )
            approved = await confirm_callback(
                f"Agent wants to run:\n`{command}`\n\nAllow?"
            )
            if not approved:
                return ShellResult(output="User denied execution", exit_code=-1)

        if self.mode == self.MODE_INLINE:
            return await self._execute_inline(command, effective_timeout)
        return await self._execute_subprocess_runner(command, effective_timeout)

    async def _execute_inline(self, command: str, timeout: int) -> ShellResult:
        """Execute the shell command directly from the current process."""
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                self._safe_env()["SHELL"],
                "-lc",
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
                env=self._safe_env(),
                start_new_session=True,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            self._kill_process_group(process)
            return ShellResult(
                output=f"TIMEOUT: command exceeded {timeout}s", exit_code=-1
            )
        except Exception as e:
            return ShellResult(output=f"ERROR: {e}", exit_code=-1)

        return self._finalize_process_result(stdout, stderr, process.returncode or 0)

    async def _execute_subprocess_runner(
        self, command: str, timeout: int
    ) -> ShellResult:
        """Execute the command through the dedicated shell runner process."""
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(self._runner_script_path()),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.workspace),
                start_new_session=True,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(
                    self._build_runner_request(command, timeout).encode("utf-8")
                ),
                timeout=timeout + self._RUNNER_GRACE_SECONDS,
            )
        except asyncio.TimeoutError:
            self._kill_process_group(process)
            return ShellResult(
                output=f"TIMEOUT: command exceeded {timeout}s", exit_code=-1
            )
        except Exception as e:
            return ShellResult(output=f"ERROR: {e}", exit_code=-1)

        if process.returncode not in (0, None):
            detail = (
                stderr.decode(errors="replace").strip()
                or stdout.decode(errors="replace").strip()
                or f"runner exited {process.returncode}"
            )
            return ShellResult(
                output=f"ERROR: shell runner failed: {detail}",
                exit_code=-1,
            )

        try:
            payload = json.loads(stdout.decode(errors="replace") or "{}")
        except json.JSONDecodeError:
            detail = (
                stderr.decode(errors="replace").strip()
                or stdout.decode(errors="replace").strip()
                or "empty response"
            )
            return ShellResult(
                output=f"ERROR: invalid shell runner response: {detail}",
                exit_code=-1,
            )

        output = str(payload.get("output", "")).strip()
        exit_code = int(payload.get("exit_code", -1))
        return ShellResult(output=output, exit_code=exit_code)

    def _build_runner_request(self, command: str, timeout: int) -> str:
        """Serialize the runner request payload."""
        return json.dumps(
            {
                "command": command,
                "cwd": str(self.workspace),
                "env": self._safe_env(),
                "timeout": timeout,
                "max_output_chars": self.MAX_OUTPUT_CHARS,
                "backend": self.backend,
                "container_image": self.container_image,
                "limits": {
                    "memory_mb": self.max_memory_mb,
                    "file_size_kb": self.max_file_size_kb,
                    "cpu_seconds": max(1, timeout),
                    "isolate_home": self.isolate_home,
                },
            }
        )

    def _finalize_process_result(
        self, stdout: bytes, stderr: bytes, exit_code: int
    ) -> ShellResult:
        """Combine, decode, and truncate shell output."""
        output = (
            stdout.decode(errors="replace") + stderr.decode(errors="replace")
        ).strip()

        # Truncate huge outputs
        if len(output) > self.MAX_OUTPUT_CHARS:
            output = output[: self.MAX_OUTPUT_CHARS] + "\n...[truncated]"

        return ShellResult(output=output, exit_code=exit_code)

    def _kill_process_group(
        self, process: Optional[asyncio.subprocess.Process]
    ) -> None:
        """Terminate a started shell process and its process group."""
        if process is None or process.returncode is not None:
            return
        try:
            if process.pid:
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            return
        except Exception:
            try:
                process.kill()
            except Exception:
                return

    def _runner_script_path(self) -> Path:
        """Return the helper runner path for subprocess mode."""
        return Path(__file__).with_name("shell_runner.py")

    _SAFE_PATH = "/usr/local/bin:/usr/bin:/bin"

    def _safe_env(self) -> dict[str, str]:
        """Create stripped environment without sensitive variables."""
        ALLOWED_VARS = ["USER", "LANG", "LC_ALL", "TERM"]
        safe = {}
        for var in ALLOWED_VARS:
            if var in os.environ:
                safe[var] = os.environ[var]
        # Restricted PATH, only standard system directories
        safe["PATH"] = self._SAFE_PATH
        # Override HOME to workspace to prevent ~ expansion tricks
        safe["HOME"] = str(self.workspace)
        # Minimal SHELL
        safe["SHELL"] = "/bin/sh"
        return safe


# Global instances
_file_guard: Optional[FileGuard] = None
_shell_sandbox: Optional[ShellSandbox] = None


def get_file_guard() -> FileGuard:
    """Get the global FileGuard instance."""
    global _file_guard
    if _file_guard is None:
        from nanoclaw.core.config import get_workspace_path

        _file_guard = FileGuard(get_workspace_path())
    return _file_guard


def get_shell_sandbox() -> ShellSandbox:
    """Get the global ShellSandbox instance."""
    global _shell_sandbox
    if _shell_sandbox is None:
        from nanoclaw.core.config import Config, get_config, get_workspace_path

        try:
            shell_cfg = get_config().tools.shell
        except Exception:
            shell_cfg = Config().tools.shell

        if not shell_cfg.enabled:
            mode = ShellSandbox.MODE_DISABLED
        else:
            mode = shell_cfg.mode

        _shell_sandbox = ShellSandbox(
            get_workspace_path(),
            mode=mode,
            backend=shell_cfg.backend,
            container_image=shell_cfg.container_image,
            confirm_dangerous=shell_cfg.confirm_dangerous,
            default_timeout=shell_cfg.timeout,
            max_memory_mb=shell_cfg.max_memory_mb,
            max_file_size_kb=shell_cfg.max_file_size_kb,
            isolate_home=shell_cfg.isolate_home,
        )
    return _shell_sandbox


def set_file_guard(guard: FileGuard) -> None:
    """Set the global FileGuard instance."""
    global _file_guard
    _file_guard = guard


def set_shell_sandbox(sandbox: ShellSandbox) -> None:
    """Set the global ShellSandbox instance."""
    global _shell_sandbox
    _shell_sandbox = sandbox
