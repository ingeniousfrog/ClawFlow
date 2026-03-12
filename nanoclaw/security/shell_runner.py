"""Dedicated subprocess runner for shell commands."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import tempfile
from pathlib import Path

from nanoclaw.security.sandbox_backends import (
    build_shell_backend_command,
    resolve_shell_backend,
)


async def _run_command(payload: dict[str, object]) -> dict[str, object]:
    """Run one shell command from a JSON payload."""
    command = str(payload.get("command", ""))
    cwd = Path(str(payload.get("cwd", "."))).resolve()
    env_payload = payload.get("env", {})
    env = dict(env_payload) if isinstance(env_payload, dict) else {}
    timeout = int(payload.get("timeout", 30))
    max_output_chars = int(payload.get("max_output_chars", 10000))
    requested_backend = str(payload.get("backend", "portable") or "portable")
    container_image = str(payload.get("container_image", "") or "")
    limits_payload = payload.get("limits", {})
    limits = dict(limits_payload) if isinstance(limits_payload, dict) else {}

    process = None
    isolate_home = bool(limits.get("isolate_home", False))

    with tempfile.TemporaryDirectory(prefix="nanoclaw-shell-") as isolated_root:
        isolated_root_path = Path(isolated_root)
        child_env = _build_child_env(
            env,
            isolated_root=isolated_root_path if isolate_home else None,
        )
        backend = resolve_shell_backend(
            requested_backend,
            container_image=container_image,
        )
        try:
            selected_backend = str(backend["selected"])
            argv = build_shell_backend_command(
                backend=selected_backend,
                shell=child_env.get("SHELL", "/bin/sh"),
                command=command,
                cwd=cwd,
                env=child_env,
                isolated_root=isolated_root_path,
                container_image=container_image,
                limits=limits,
            )
            stdout, stderr, exit_code = await _run_backend_process(
                argv=argv,
                cwd=cwd,
                env=child_env,
                timeout=timeout,
                limits=limits,
            )
            combined_output = (
                stdout.decode(errors="replace") + stderr.decode(errors="replace")
            ).strip()
            if _should_retry_with_native(
                requested_backend=requested_backend,
                selected_backend=selected_backend,
                exit_code=exit_code,
                output=combined_output,
            ):
                selected_backend = "native"
                argv = build_shell_backend_command(
                    backend=selected_backend,
                    shell=child_env.get("SHELL", "/bin/sh"),
                    command=command,
                    cwd=cwd,
                    env=child_env,
                    isolated_root=isolated_root_path,
                    container_image=container_image,
                    limits=limits,
                )
                stdout, stderr, exit_code = await _run_backend_process(
                    argv=argv,
                    cwd=cwd,
                    env=child_env,
                    timeout=timeout,
                    limits=limits,
                )
            process = _CompletedProcess(exit_code=exit_code)
        except asyncio.TimeoutError:
            _kill_process_group(process)
            return {
                "output": f"TIMEOUT: command exceeded {timeout}s",
                "exit_code": -1,
                "backend": str(backend["selected"]),
            }
        except Exception as exc:
            return {
                "output": f"ERROR: {exc}",
                "exit_code": -1,
                "backend": str(backend["selected"]),
            }

    output = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()
    if len(output) > max_output_chars:
        output = output[:max_output_chars] + "\n...[truncated]"
    return {
        "output": output,
        "exit_code": process.returncode or 0,
        "backend": selected_backend,
    }


class _CompletedProcess:
    """Small returncode holder for the runner result path."""

    def __init__(self, *, exit_code: int) -> None:
        self.returncode = exit_code


async def _run_backend_process(
    *,
    argv: list[str],
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    limits: dict[str, object],
) -> tuple[bytes, bytes, int]:
    """Execute one backend argv and return raw process results."""
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(cwd),
        env=env,
        start_new_session=True,
        preexec_fn=_build_preexec(limits),
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        _kill_process_group(process)
        raise
    return stdout, stderr, process.returncode or 0


def _should_retry_with_native(
    *,
    requested_backend: str,
    selected_backend: str,
    exit_code: int,
    output: str,
) -> bool:
    """Retry once with native when portable host-local isolation cannot activate."""
    if requested_backend not in {"portable", "auto"}:
        return False
    if selected_backend not in {"bubblewrap", "sandbox-exec"}:
        return False
    if exit_code == 0:
        return False
    lowered = output.lower()
    return any(
        marker in lowered
        for marker in (
            "operation not permitted",
            "sandbox_apply",
            "failed to create new namespace",
            "creating new namespace",
        )
    )


def _build_child_env(
    env: dict[str, object],
    *,
    isolated_root: Path | None,
) -> dict[str, str]:
    """Build one child environment with optional isolated HOME/TMP roots."""
    child_env = {str(key): str(value) for key, value in env.items()}
    child_env["PYTHONNOUSERSITE"] = "1"
    child_env["PYTHONDONTWRITEBYTECODE"] = "1"
    child_env["HISTFILE"] = "/dev/null"

    if isolated_root is None:
        return child_env

    tmp_dir = isolated_root / "tmp"
    config_dir = isolated_root / ".config"
    cache_dir = isolated_root / ".cache"
    data_dir = isolated_root / ".local" / "share"
    for path in (tmp_dir, config_dir, cache_dir, data_dir):
        path.mkdir(parents=True, exist_ok=True)

    child_env["HOME"] = str(isolated_root)
    child_env["TMPDIR"] = str(tmp_dir)
    child_env["TMP"] = str(tmp_dir)
    child_env["TEMP"] = str(tmp_dir)
    child_env["XDG_CONFIG_HOME"] = str(config_dir)
    child_env["XDG_CACHE_HOME"] = str(cache_dir)
    child_env["XDG_DATA_HOME"] = str(data_dir)
    child_env["GIT_CONFIG_NOSYSTEM"] = "1"
    child_env["GIT_CONFIG_GLOBAL"] = str(config_dir / "gitconfig")
    return child_env


def _build_preexec(limits: dict[str, object]) -> object | None:
    """Build a Unix pre-exec hook that applies OS-level resource limits."""
    try:
        import resource
    except Exception:
        return None

    memory_mb = max(0, int(limits.get("memory_mb", 0) or 0))
    file_size_kb = max(0, int(limits.get("file_size_kb", 0) or 0))
    cpu_seconds = max(1, int(limits.get("cpu_seconds", 1) or 1))

    def _apply() -> None:
        os.umask(0o077)
        if hasattr(resource, "RLIMIT_CORE"):
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if hasattr(resource, "RLIMIT_CPU"):
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds))
        if hasattr(resource, "RLIMIT_NOFILE"):
            resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        if file_size_kb > 0 and hasattr(resource, "RLIMIT_FSIZE"):
            limit_bytes = file_size_kb * 1024
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit_bytes, limit_bytes))
        if memory_mb > 0:
            limit_bytes = memory_mb * 1024 * 1024
            for name in ("RLIMIT_AS", "RLIMIT_DATA"):
                limit_key = getattr(resource, name, None)
                if limit_key is None:
                    continue
                try:
                    resource.setrlimit(limit_key, (limit_bytes, limit_bytes))
                except (OSError, ValueError):
                    continue

    return _apply


def _kill_process_group(process: asyncio.subprocess.Process | None) -> None:
    """Terminate a started child process group."""
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


async def _main() -> int:
    """Read a request from stdin, execute it, and write JSON to stdout."""
    raw = await asyncio.to_thread(sys.stdin.buffer.read)
    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        json.dump(
            {
                "output": f"ERROR: invalid runner payload: {exc}",
                "exit_code": -1,
            },
            sys.stdout,
        )
        sys.stdout.write("\n")
        return 0

    result = await _run_command(payload)
    json.dump(result, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
