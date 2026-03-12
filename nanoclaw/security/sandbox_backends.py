"""Experimental stronger shell backends for subprocess shell execution."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

VALID_SHELL_BACKENDS = (
    "native",
    "portable",
    "auto",
    "docker",
    "podman",
    "bubblewrap",
    "sandbox-exec",
)
DEFAULT_SHELL_BACKEND = "portable"
PRIMARY_CONTAINER_BACKEND = "docker"
_CONTAINER_SHELL_BACKENDS = ("docker", "podman")
_STRONGER_SHELL_BACKENDS = _CONTAINER_SHELL_BACKENDS + (
    "bubblewrap",
    "sandbox-exec",
)
_HEALTH_CACHE_TTL_SECONDS = 15.0
_RUNTIME_WAIT_TIMEOUT_SECONDS = 30.0
_RUNTIME_WAIT_INTERVAL_SECONDS = 2.0
_health_cache: dict[tuple[str, str, str], tuple[float, dict[str, object]]] = {}
_lifecycle_cache: dict[tuple[str, str, str], dict[str, object]] = {}


def detect_shell_backend_availability(
    *,
    container_image: str = "",
    which: Callable[[str], str | None] | None = None,
    platform: str | None = None,
) -> dict[str, dict[str, str | bool]]:
    """Return availability details for supported shell backends."""
    which_fn = which or shutil.which
    current_platform = platform or sys.platform
    image_configured = bool(container_image.strip())
    docker_path = which_fn("docker") or ""
    podman_path = which_fn("podman") or ""
    bubblewrap_path = which_fn("bwrap") or ""
    sandbox_exec_path = which_fn("sandbox-exec") or ""

    return {
        "native": {
            "available": True,
            "path": "",
            "reason": "built_in",
        },
        "docker": {
            "available": bool(docker_path and image_configured),
            "path": docker_path,
            "reason": (
                "ok"
                if docker_path and image_configured
                else "missing_container_image"
                if docker_path
                else "missing_binary"
            ),
        },
        "podman": {
            "available": bool(podman_path and image_configured),
            "path": podman_path,
            "reason": (
                "ok"
                if podman_path and image_configured
                else "missing_container_image"
                if podman_path
                else "missing_binary"
            ),
        },
        "bubblewrap": {
            "available": bool(current_platform.startswith("linux") and bubblewrap_path),
            "path": bubblewrap_path,
            "reason": (
                "ok"
                if current_platform.startswith("linux") and bubblewrap_path
                else "unsupported_platform"
                if not current_platform.startswith("linux")
                else "missing_binary"
            ),
        },
        "sandbox-exec": {
            "available": bool(current_platform == "darwin" and sandbox_exec_path),
            "path": sandbox_exec_path,
            "reason": (
                "ok"
                if current_platform == "darwin" and sandbox_exec_path
                else "unsupported_platform"
                if current_platform != "darwin"
                else "missing_binary"
            ),
        },
    }


def resolve_shell_backend(
    requested: str,
    *,
    container_image: str = "",
    which: Callable[[str], str | None] | None = None,
    platform: str | None = None,
) -> dict[str, object]:
    """Resolve one requested backend into the backend that will actually run."""
    availability = detect_shell_backend_availability(
        container_image=container_image,
        which=which,
        platform=platform,
    )
    current_platform = platform or sys.platform
    normalized = (
        requested if requested in VALID_SHELL_BACKENDS else DEFAULT_SHELL_BACKEND
    )
    auto_order = _auto_backend_order(current_platform)
    portable_order = _portable_backend_order(current_platform)

    selected = "native"
    fallback_reason = ""
    if normalized == "auto":
        for candidate in auto_order:
            if bool(availability.get(candidate, {}).get("available")):
                selected = candidate
                break
        if selected == "native":
            fallback_reason = "no stronger backend available"
    elif normalized == "portable":
        for candidate in portable_order:
            if bool(availability.get(candidate, {}).get("available")):
                selected = candidate
                break
        if selected == "native":
            fallback_reason = "no portable stronger backend available"
    elif normalized == "native":
        selected = "native"
    elif bool(availability.get(normalized, {}).get("available")):
        selected = normalized
    else:
        selected = "native"
        fallback_reason = f"{normalized} unavailable"

    stronger_available = any(
        bool(availability[name]["available"]) for name in _STRONGER_SHELL_BACKENDS
    )
    return {
        "requested": normalized,
        "selected": selected,
        "fallback_reason": fallback_reason,
        "stronger_backend_available": stronger_available,
        "available_backends": [
            name
            for name in _STRONGER_SHELL_BACKENDS
            if bool(availability[name]["available"])
        ],
        "availability": availability,
    }


def inspect_container_backend_health(
    *,
    backend: str = PRIMARY_CONTAINER_BACKEND,
    container_image: str = "",
    which: Callable[[str], str | None] | None = None,
    run_command: Callable[[list[str]], tuple[int, str, str]] | None = None,
    platform: str | None = None,
    force_refresh: bool = False,
) -> dict[str, object]:
    """Inspect one container backend for runtime and image readiness."""
    current_platform = platform or sys.platform
    normalized_backend = (
        backend if backend in _CONTAINER_SHELL_BACKENDS else PRIMARY_CONTAINER_BACKEND
    )
    cache_key = (normalized_backend, container_image.strip(), current_platform)
    if which is None and run_command is None and not force_refresh:
        cached = _health_cache.get(cache_key)
        now = time.monotonic()
        if cached and now - cached[0] < _HEALTH_CACHE_TTL_SECONDS:
            return dict(cached[1])

    availability = detect_shell_backend_availability(
        container_image=container_image,
        which=which,
        platform=platform,
    )
    backend_info = availability[normalized_backend]
    binary_path = str(backend_info["path"] or "").strip()
    image_name = container_image.strip()
    result: dict[str, object] = {
        "backend": normalized_backend,
        "binary_available": bool(binary_path),
        "image_configured": bool(image_name),
        "configured_image": image_name,
        "runtime_reachable": False,
        "image_present": False,
        "ready": False,
        "status": "missing_binary",
        "detail": "",
        "runtime_version": "",
        "lifecycle_state": "first_observation",
        "drifted": False,
        "drift_reason": "",
        "previous_status": "",
        "previous_runtime_version": "",
        "last_transition_at": 0,
        "last_ready_at": 0,
    }

    if not binary_path:
        result["status"] = "missing_binary"
        result["detail"] = f"{normalized_backend} binary not found"
    elif not image_name:
        result["status"] = "missing_container_image"
        result["detail"] = "containerImage is not configured"
    else:
        runner = run_command or _run_health_command
        runtime_rc, runtime_out, runtime_err = runner(
            [binary_path, *_runtime_version_command(normalized_backend)]
        )
        runtime_detail = _clean_health_output(runtime_out or runtime_err)
        if runtime_rc != 0:
            result["status"] = "runtime_unreachable"
            result["detail"] = runtime_detail or f"{normalized_backend} runtime unreachable"
        else:
            result["runtime_reachable"] = True
            result["runtime_version"] = runtime_detail
            image_rc, image_out, image_err = runner(
                [binary_path, *_image_inspect_command(normalized_backend, image_name)]
            )
            image_detail = _clean_health_output(image_out or image_err)
            if image_rc != 0:
                result["status"] = "image_missing"
                result["detail"] = image_detail or f"container image missing: {image_name}"
            else:
                result["image_present"] = True
                result["ready"] = True
                result["status"] = "ready"
                result["detail"] = image_detail or image_name

    result = _apply_lifecycle_state(cache_key, result)

    if which is None and run_command is None:
        _health_cache[cache_key] = (time.monotonic(), dict(result))
    return result


def get_container_remediation_plan(
    health: dict[str, object],
    *,
    backend: str | None = None,
    container_image: str = "",
    platform: str | None = None,
) -> dict[str, object]:
    """Return remediation guidance for one container backend health state."""
    selected_backend = str(
        health.get("backend") or backend or PRIMARY_CONTAINER_BACKEND
    ).strip() or PRIMARY_CONTAINER_BACKEND
    image_name = str(
        health.get("configured_image") or container_image or ""
    ).strip()
    current_platform = platform or sys.platform
    verify_command = f"nanoclaw container-check --backend {selected_backend} --refresh"
    if image_name:
        verify_command += f" --image {image_name}"
    prepare_command = ""
    if image_name:
        prepare_command = (
            f"nanoclaw container-prepare --backend {selected_backend} --refresh "
            f"--image {shlex.quote(image_name)}"
        )
    commands: list[str] = []
    steps: list[str] = []
    status = str(health.get("status") or "")
    pull_command = ""
    start_runtime_command = _runtime_start_command(
        selected_backend,
        platform=current_platform,
    )
    restart_runtime_command = _runtime_restart_command(
        selected_backend,
        platform=current_platform,
    )
    runtime_command = ""
    drifted = bool(health.get("drifted"))
    lifecycle_state = str(health.get("lifecycle_state") or "")
    drift_reason = str(health.get("drift_reason") or "")

    if status == "missing_binary":
        steps.append(
            f"Install {selected_backend} and make sure `{selected_backend}` is on PATH."
        )
        steps.append("Re-run the container readiness check after installation.")
        commands.append(verify_command)
    elif status == "missing_container_image":
        steps.append(
            "Set `tools.shell.containerImage` to a local image tag for the "
            f"{selected_backend} backend."
        )
        steps.append("Re-run the container readiness check after config update.")
        commands.append(verify_command)
    elif status == "runtime_unreachable":
        steps.append(
            f"Start the {selected_backend} runtime/daemon and verify the runtime responds."
        )
        if drifted and drift_reason:
            steps.append(f"Runtime drift detected: {drift_reason}.")
        if drifted and restart_runtime_command:
            runtime_command = (
                f"nanoclaw container-runtime --backend {selected_backend} "
                "--refresh --restart"
            )
            if image_name:
                runtime_command += f" --image {shlex.quote(image_name)} --prepare --pull"
        elif start_runtime_command:
            runtime_command = (
                f"nanoclaw container-runtime --backend {selected_backend} "
                "--refresh --start"
            )
            if image_name:
                runtime_command += f" --image {shlex.quote(image_name)} --prepare --pull"
        if runtime_command:
            commands.append(runtime_command)
        if drifted and restart_runtime_command:
            commands.append(restart_runtime_command)
        if start_runtime_command:
            commands.append(start_runtime_command)
        version_format = "'{{.Version}}'" if selected_backend == "podman" else "'{{.Server.Version}}'"
        commands.append(f"{selected_backend} version --format {version_format}")
        if prepare_command:
            commands.append(prepare_command)
        commands.append(verify_command)
    elif status == "image_missing":
        if image_name:
            steps.append(
                f"Pull or build the configured image `{image_name}` locally."
            )
            if prepare_command:
                prepare_command += " --pull"
            pull_command = f"{selected_backend} pull {shlex.quote(image_name)}"
            commands.append(pull_command)
            inspect_format = "'{{.ID}}'" if selected_backend == "podman" else "'{{.Id}}'"
            commands.append(
                f"{selected_backend} image inspect {shlex.quote(image_name)} "
                f"--format {inspect_format}"
            )
            if prepare_command:
                commands.append(prepare_command)
        else:
            steps.append("Configure a local container image before using the backend.")
        commands.append(verify_command)
    elif status == "ready":
        steps.append("Primary container target is ready.")
        if drifted and lifecycle_state == "runtime_version_changed":
            steps.append(f"Runtime drift detected: {drift_reason}.")
            if restart_runtime_command:
                runtime_command = (
                    f"nanoclaw container-runtime --backend {selected_backend} "
                    "--refresh --restart"
                )
                if image_name:
                    runtime_command += (
                        f" --image {shlex.quote(image_name)} --prepare --pull"
                    )
                commands.append(runtime_command)
                commands.append(restart_runtime_command)
        if prepare_command:
            commands.append(prepare_command)
        commands.append(verify_command)
    else:
        steps.append("Inspect the container backend state and re-run readiness checks.")
        if prepare_command:
            commands.append(prepare_command)
        commands.append(verify_command)

    return {
        "steps": steps,
        "commands": commands,
        "verify_command": verify_command,
        "prepare_command": prepare_command,
        "start_runtime_command": start_runtime_command,
        "restart_runtime_command": restart_runtime_command,
        "runtime_command": runtime_command,
        "pull_command": pull_command,
    }


def prepare_container_backend(
    *,
    backend: str = PRIMARY_CONTAINER_BACKEND,
    container_image: str = "",
    which: Callable[[str], str | None] | None = None,
    run_command: Callable[[list[str]], tuple[int, str, str]] | None = None,
    platform: str | None = None,
    force_refresh: bool = False,
    allow_pull: bool = False,
) -> dict[str, object]:
    """Prepare one container backend by provisioning the image when allowed."""
    normalized_backend = (
        backend if backend in _CONTAINER_SHELL_BACKENDS else PRIMARY_CONTAINER_BACKEND
    )
    health_before = inspect_container_backend_health(
        backend=normalized_backend,
        container_image=container_image,
        which=which,
        run_command=run_command,
        platform=platform,
        force_refresh=force_refresh,
    )
    health_after = dict(health_before)
    actions: list[dict[str, object]] = []

    if (
        str(health_before.get("status") or "") == "image_missing"
        and allow_pull
        and str(health_before.get("configured_image") or container_image or "").strip()
    ):
        availability = detect_shell_backend_availability(
            container_image=container_image,
            which=which,
            platform=platform,
        )
        executable = str(availability[normalized_backend]["path"] or normalized_backend)
        image_name = str(
            health_before.get("configured_image") or container_image or ""
        ).strip()
        pull_argv = [executable, *_image_pull_command(normalized_backend, image_name)]
        display_pull_argv = [
            normalized_backend,
            *_image_pull_command(normalized_backend, image_name),
        ]
        runner = run_command or _run_preparation_command
        pull_rc, pull_out, pull_err = runner(pull_argv)
        pull_detail = _clean_health_output(pull_out or pull_err)
        actions.append(
            {
                "name": "pull_image",
                "command": _format_shell_command(display_pull_argv),
                "success": pull_rc == 0,
                "detail": pull_detail or ("ok" if pull_rc == 0 else "pull failed"),
            }
        )
        if pull_rc == 0:
            health_after = inspect_container_backend_health(
                backend=normalized_backend,
                container_image=container_image,
                which=which,
                run_command=run_command,
                platform=platform,
                force_refresh=True,
            )

    remediation = get_container_remediation_plan(
        health_after,
        backend=normalized_backend,
        container_image=container_image,
        platform=platform,
    )
    return {
        "backend": normalized_backend,
        "health_before": health_before,
        "health_after": health_after,
        "runtime_ready": bool(health_after.get("runtime_reachable")),
        "ready": bool(health_after.get("ready")),
        "actions": actions,
        "remediation": remediation,
    }


def manage_container_runtime(
    *,
    backend: str = PRIMARY_CONTAINER_BACKEND,
    container_image: str = "",
    which: Callable[[str], str | None] | None = None,
    run_command: Callable[[list[str]], tuple[int, str, str]] | None = None,
    platform: str | None = None,
    force_refresh: bool = False,
    allow_start: bool = False,
    allow_restart: bool = False,
    allow_prepare: bool = False,
    allow_pull: bool = False,
    wait_timeout_seconds: float = _RUNTIME_WAIT_TIMEOUT_SECONDS,
    wait_interval_seconds: float = _RUNTIME_WAIT_INTERVAL_SECONDS,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, object]:
    """Manage one container runtime lifecycle and optionally prepare the image."""
    normalized_backend = (
        backend if backend in _CONTAINER_SHELL_BACKENDS else PRIMARY_CONTAINER_BACKEND
    )
    runner = run_command or _run_preparation_command
    health_before = inspect_container_backend_health(
        backend=normalized_backend,
        container_image=container_image,
        which=which,
        run_command=run_command,
        platform=platform,
        force_refresh=force_refresh,
    )
    health_after = dict(health_before)
    actions: list[dict[str, object]] = []
    should_restart = bool(allow_restart and _should_restart_runtime(health_after))
    should_start = bool(
        not should_restart
        and str(health_after.get("status") or "") == "runtime_unreachable"
        and allow_start
    )

    runtime_steps: list[tuple[str, list[str]]] = []
    if should_restart:
        runtime_steps = _runtime_restart_steps(normalized_backend, platform=platform)
    elif should_start:
        start_argv = _runtime_start_argv(normalized_backend, platform=platform)
        if start_argv:
            runtime_steps = [("start_runtime", start_argv)]

    if runtime_steps:
        all_steps_ok = True
        for action_name, argv in runtime_steps:
            rc, out, err = runner(argv)
            detail = _clean_health_output(out or err)
            success = rc == 0
            actions.append(
                {
                    "name": action_name,
                    "command": _format_shell_command(argv),
                    "success": success,
                    "detail": detail or ("ok" if success else f"{action_name} failed"),
                }
            )
            if not success:
                all_steps_ok = False
                break
        if all_steps_ok:
            waited_health, waited_seconds = _wait_for_runtime_ready(
                backend=normalized_backend,
                container_image=container_image,
                which=which,
                run_command=run_command,
                platform=platform,
                timeout_seconds=wait_timeout_seconds,
                interval_seconds=wait_interval_seconds,
                sleep=sleep,
            )
            health_after = waited_health
            actions.append(
                {
                    "name": "wait_runtime",
                    "command": (
                        f"wait for {normalized_backend} runtime "
                        f"({int(wait_timeout_seconds)}s timeout)"
                    ),
                    "success": bool(waited_health.get("runtime_reachable")),
                    "detail": (
                        waited_health.get("runtime_version")
                        or waited_health.get("detail")
                        or f"waited {waited_seconds:.1f}s"
                    ),
                }
            )

    if (
        allow_prepare
        and bool(health_after.get("runtime_reachable"))
        and str(health_after.get("status") or "") == "image_missing"
    ):
        preparation = prepare_container_backend(
            backend=normalized_backend,
            container_image=container_image,
            which=which,
            run_command=run_command,
            platform=platform,
            force_refresh=True,
            allow_pull=allow_pull,
        )
        health_after = preparation["health_after"]
        actions.extend(list(preparation["actions"]))

    remediation = get_container_remediation_plan(
        health_after,
        backend=normalized_backend,
        container_image=container_image,
        platform=platform,
    )
    return {
        "backend": normalized_backend,
        "health_before": health_before,
        "health_after": health_after,
        "runtime_ready": bool(health_after.get("runtime_reachable")),
        "ready": bool(health_after.get("ready")),
        "actions": actions,
        "remediation": remediation,
    }


def build_shell_backend_command(
    *,
    backend: str,
    shell: str,
    command: str,
    cwd: Path,
    env: dict[str, str],
    isolated_root: Path,
    container_image: str = "",
    limits: dict[str, object] | None = None,
) -> list[str]:
    """Build the exec argv for the selected shell backend."""
    if backend in _CONTAINER_SHELL_BACKENDS:
        return _build_container_command(
            backend=backend,
            shell=shell,
            command=command,
            cwd=cwd,
            env=env,
            isolated_root=isolated_root,
            container_image=container_image,
            limits=limits or {},
        )
    if backend == "bubblewrap":
        return _build_bubblewrap_command(
            shell=shell,
            command=command,
            cwd=cwd,
            env=env,
            isolated_root=isolated_root,
        )
    if backend == "sandbox-exec":
        return _build_sandbox_exec_command(
            shell=shell,
            command=command,
            cwd=cwd,
            isolated_root=isolated_root,
        )
    return [shell, "-lc", command]


def _runtime_version_command(backend: str) -> list[str]:
    """Return one runtime-health command for the selected container backend."""
    if backend == "podman":
        return ["version", "--format", "{{.Version}}"]
    return ["version", "--format", "{{.Server.Version}}"]


def _image_inspect_command(backend: str, image_name: str) -> list[str]:
    """Return one image-readiness command for the selected container backend."""
    format_arg = "{{.Id}}" if backend == "docker" else "{{.ID}}"
    return ["image", "inspect", image_name, "--format", format_arg]


def _image_pull_command(backend: str, image_name: str) -> list[str]:
    """Return one image-provisioning command for the selected container backend."""
    _ = backend
    return ["pull", image_name]


def _run_health_command(argv: list[str]) -> tuple[int, str, str]:
    """Run one short-lived health command for backend readiness checks."""
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def _run_preparation_command(argv: list[str]) -> tuple[int, str, str]:
    """Run one longer-lived image provisioning command."""
    completed = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def _format_shell_command(argv: list[str]) -> str:
    """Render one argv list as a shell-safe display string."""
    return " ".join(shlex.quote(part) for part in argv)


def _runtime_start_command(backend: str, *, platform: str | None = None) -> str:
    """Return one best-effort runtime start command for the selected backend."""
    argv = _runtime_start_argv(backend, platform=platform)
    return _format_shell_command(argv) if argv else ""


def _runtime_start_argv(backend: str, *, platform: str | None = None) -> list[str]:
    """Return one best-effort runtime start argv for the selected backend."""
    current_platform = platform or sys.platform
    if backend == "docker":
        if current_platform == "darwin":
            return ["open", "-a", "Docker"]
        if current_platform.startswith("linux"):
            return ["sudo", "systemctl", "start", "docker"]
        return []
    if backend == "podman":
        if current_platform == "darwin":
            return ["podman", "machine", "start"]
        if current_platform.startswith("linux"):
            return ["systemctl", "--user", "start", "podman.socket"]
    return []


def _runtime_restart_command(backend: str, *, platform: str | None = None) -> str:
    """Return one best-effort runtime restart command string."""
    steps = _runtime_restart_steps(backend, platform=platform)
    if not steps:
        return ""
    return " && ".join(_format_shell_command(argv) for _, argv in steps)


def _runtime_restart_steps(
    backend: str,
    *,
    platform: str | None = None,
) -> list[tuple[str, list[str]]]:
    """Return one best-effort runtime restart step sequence."""
    current_platform = platform or sys.platform
    if backend == "docker":
        if current_platform == "darwin":
            return [
                ("stop_runtime", ["osascript", "-e", 'quit app "Docker"']),
                ("start_runtime", ["open", "-a", "Docker"]),
            ]
        if current_platform.startswith("linux"):
            return [("restart_runtime", ["sudo", "systemctl", "restart", "docker"])]
        return []
    if backend == "podman":
        if current_platform == "darwin":
            return [
                ("stop_runtime", ["podman", "machine", "stop"]),
                ("start_runtime", ["podman", "machine", "start"]),
            ]
        if current_platform.startswith("linux"):
            return [
                ("restart_runtime", ["systemctl", "--user", "restart", "podman.socket"])
            ]
    return []


def _wait_for_runtime_ready(
    *,
    backend: str,
    container_image: str,
    which: Callable[[str], str | None] | None = None,
    run_command: Callable[[list[str]], tuple[int, str, str]] | None = None,
    platform: str | None = None,
    timeout_seconds: float = _RUNTIME_WAIT_TIMEOUT_SECONDS,
    interval_seconds: float = _RUNTIME_WAIT_INTERVAL_SECONDS,
    sleep: Callable[[float], None] | None = None,
) -> tuple[dict[str, object], float]:
    """Poll until the runtime becomes reachable or the timeout expires."""
    health = inspect_container_backend_health(
        backend=backend,
        container_image=container_image,
        which=which,
        run_command=run_command,
        platform=platform,
        force_refresh=True,
    )
    if bool(health.get("runtime_reachable")) or timeout_seconds <= 0:
        return health, 0.0

    sleeper = sleep or time.sleep
    start = time.monotonic()
    deadline = start + max(timeout_seconds, 0.0)
    while time.monotonic() < deadline:
        wait_seconds = min(interval_seconds, max(0.0, deadline - time.monotonic()))
        if wait_seconds > 0:
            sleeper(wait_seconds)
        health = inspect_container_backend_health(
            backend=backend,
            container_image=container_image,
            which=which,
            run_command=run_command,
            platform=platform,
            force_refresh=True,
        )
        if bool(health.get("runtime_reachable")):
            break
    return health, max(0.0, time.monotonic() - start)


def _auto_backend_order(platform: str) -> tuple[str, ...]:
    """Return backend preference order for explicit auto mode."""
    if platform.startswith("linux"):
        return ("docker", "podman", "bubblewrap", "sandbox-exec", "native")
    if platform == "darwin":
        return ("docker", "podman", "sandbox-exec", "bubblewrap", "native")
    return ("docker", "podman", "bubblewrap", "sandbox-exec", "native")


def _portable_backend_order(platform: str) -> tuple[str, ...]:
    """Return backend preference order for the portable stronger default path."""
    if platform.startswith("linux"):
        return ("bubblewrap", "docker", "podman", "sandbox-exec", "native")
    if platform == "darwin":
        return ("sandbox-exec", "docker", "podman", "bubblewrap", "native")
    return ("docker", "podman", "bubblewrap", "sandbox-exec", "native")


def _apply_lifecycle_state(
    cache_key: tuple[str, str, str],
    result: dict[str, object],
) -> dict[str, object]:
    """Annotate one health result with lifecycle drift state."""
    observed_at = int(time.time())
    previous = _lifecycle_cache.get(cache_key)
    lifecycle_state = "first_observation"
    drifted = False
    drift_reason = ""
    last_transition_at = observed_at
    last_ready_at = observed_at if bool(result.get("ready")) else 0

    if previous:
        lifecycle_state, drifted, drift_reason = _derive_lifecycle_state(previous, result)
        last_transition_at = int(previous.get("last_transition_at") or observed_at)
        if lifecycle_state != "steady":
            last_transition_at = observed_at
        last_ready_at = int(previous.get("last_ready_at") or 0)
        if bool(result.get("ready")):
            last_ready_at = observed_at
        result["previous_status"] = str(previous.get("status") or "")
        result["previous_runtime_version"] = str(previous.get("runtime_version") or "")

    result["lifecycle_state"] = lifecycle_state
    result["drifted"] = drifted
    result["drift_reason"] = drift_reason
    result["last_transition_at"] = last_transition_at
    result["last_ready_at"] = last_ready_at

    _lifecycle_cache[cache_key] = {
        "status": str(result.get("status") or ""),
        "runtime_reachable": bool(result.get("runtime_reachable")),
        "image_present": bool(result.get("image_present")),
        "ready": bool(result.get("ready")),
        "runtime_version": str(result.get("runtime_version") or ""),
        "last_transition_at": last_transition_at,
        "last_ready_at": last_ready_at,
    }
    return result


def _derive_lifecycle_state(
    previous: dict[str, object],
    current: dict[str, object],
) -> tuple[str, bool, str]:
    """Compare two health snapshots and derive the lifecycle state."""
    prev_status = str(previous.get("status") or "")
    curr_status = str(current.get("status") or "")
    prev_runtime = bool(previous.get("runtime_reachable"))
    curr_runtime = bool(current.get("runtime_reachable"))
    prev_image = bool(previous.get("image_present"))
    curr_image = bool(current.get("image_present"))
    prev_version = str(previous.get("runtime_version") or "")
    curr_version = str(current.get("runtime_version") or "")

    if prev_runtime and not curr_runtime:
        return ("runtime_lost", True, f"runtime lost after {prev_status or 'reachable'}")
    if prev_image and not curr_image and curr_status == "image_missing":
        return ("image_lost", True, "configured image disappeared from local runtime")
    if prev_version and curr_version and prev_version != curr_version:
        return (
            "runtime_version_changed",
            True,
            f"runtime version changed from {prev_version} to {curr_version}",
        )
    if not prev_runtime and curr_runtime:
        return ("runtime_recovered", False, "")
    if prev_status != curr_status:
        return ("status_changed", False, f"status changed from {prev_status} to {curr_status}")
    return ("steady", False, "")


def _should_restart_runtime(health: dict[str, object]) -> bool:
    """Return whether the current health state should use restart automation."""
    lifecycle_state = str(health.get("lifecycle_state") or "")
    return lifecycle_state in {"runtime_lost", "runtime_version_changed"}


def _clean_health_output(raw: str) -> str:
    """Normalize one backend-health output string."""
    cleaned = " ".join(raw.strip().split())
    return cleaned[:160]


def _build_container_command(
    *,
    backend: str,
    shell: str,
    command: str,
    cwd: Path,
    env: dict[str, str],
    isolated_root: Path,
    container_image: str,
    limits: dict[str, object],
) -> list[str]:
    """Build one container runtime command for shell execution."""
    availability = detect_shell_backend_availability(container_image=container_image)
    executable = str(availability[backend]["path"] or backend)
    if not container_image.strip():
        raise ValueError(f"{backend} backend requires tools.shell.containerImage")

    args = [
        executable,
        "run",
        "--rm",
        "--init",
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
    ]
    uid = getattr(os, "getuid", lambda: 65534)()
    gid = getattr(os, "getgid", lambda: 65534)()
    args.extend(["--user", f"{uid}:{gid}"])

    memory_mb = max(0, int(limits.get("memory_mb", 0) or 0))
    if memory_mb > 0:
        args.extend(["--memory", f"{memory_mb}m"])
    args.extend(["--pids-limit", "64"])

    for key, value in sorted(env.items()):
        args.extend(["-e", f"{key}={value}"])

    args.extend(["-v", f"{cwd}:{cwd}:rw"])
    args.extend(["-v", f"{isolated_root}:{isolated_root}:rw"])
    args.extend(["--workdir", str(cwd)])
    args.append(container_image.strip())
    args.extend([shell, "-lc", command])
    return args


def _build_bubblewrap_command(
    *,
    shell: str,
    command: str,
    cwd: Path,
    env: dict[str, str],
    isolated_root: Path,
) -> list[str]:
    """Build one experimental bubblewrap command line."""
    availability = detect_shell_backend_availability()
    executable = str(availability["bubblewrap"]["path"] or "bwrap")
    args = [
        executable,
        "--die-with-parent",
        "--new-session",
        "--unshare-all",
        "--clearenv",
    ]
    if Path("/proc").exists():
        args.extend(["--proc", "/proc"])
    if Path("/dev").exists():
        args.extend(["--dev", "/dev"])

    for host_path in _existing_bind_paths(
        "/bin",
        "/etc",
        "/lib",
        "/lib64",
        "/opt",
        "/sbin",
        "/usr",
        "/run/current-system/sw",
        "/nix",
    ):
        args.extend(["--ro-bind", host_path, host_path])

    args.extend(["--bind", str(cwd), str(cwd)])
    args.extend(["--bind", str(isolated_root), str(isolated_root)])
    args.extend(["--chdir", str(cwd)])

    for key, value in sorted(env.items()):
        args.extend(["--setenv", key, value])

    args.extend([shell, "-lc", command])
    return args


def _build_sandbox_exec_command(
    *,
    shell: str,
    command: str,
    cwd: Path,
    isolated_root: Path,
) -> list[str]:
    """Build one experimental sandbox-exec command line."""
    availability = detect_shell_backend_availability()
    executable = str(availability["sandbox-exec"]["path"] or "sandbox-exec")
    profile_path = isolated_root / "sandbox-exec.sb"
    profile_path.write_text(
        _build_sandbox_exec_profile(cwd=cwd, isolated_root=isolated_root),
        encoding="utf-8",
    )
    return [executable, "-f", str(profile_path), shell, "-lc", command]


def _build_sandbox_exec_profile(*, cwd: Path, isolated_root: Path) -> str:
    """Build one narrow sandbox-exec profile for workspace shell commands."""
    readable_paths = _existing_bind_paths(
        "/Applications",
        "/System",
        "/bin",
        "/dev",
        "/etc",
        "/opt",
        "/private/etc",
        "/private/tmp",
        "/private/var",
        "/sbin",
        "/tmp",
        "/usr",
        "/usr/local",
    )
    allowed_rw_paths = sorted(
        {
            str(cwd),
            str(cwd.resolve()),
            str(isolated_root),
            str(isolated_root.resolve()),
        }
    )
    lines = [
        "(version 1)",
        "(deny default)",
        "(allow process-exec)",
        "(allow process-fork)",
        "(allow signal (target self))",
        "(allow sysctl-read)",
        "(allow file-read-metadata)",
    ]
    for path in readable_paths:
        lines.append(f"(allow file-read* (subpath {json.dumps(path)}))")
    for path in allowed_rw_paths:
        lines.append(f"(allow file-read* (subpath {json.dumps(path)}))")
        lines.append(f"(allow file-write* (subpath {json.dumps(path)}))")
    return "\n".join(lines) + "\n"


def _existing_bind_paths(*paths: str) -> list[str]:
    """Return the existing host paths from a candidate list."""
    existing: list[str] = []
    for raw in paths:
        path = Path(raw)
        if path.exists():
            existing.append(str(path))
    return existing
