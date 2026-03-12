"""Security doctor tests."""

from __future__ import annotations

import json
from pathlib import Path

from nanoclaw.security.doctor import SecurityDoctor


def _write_config(
    config_dir: Path,
    shell_overrides: dict[str, object],
    *,
    secret_overrides: dict[str, object] | None = None,
    extension_overrides: dict[str, object] | None = None,
) -> None:
    """Write a minimal config file for doctor checks."""
    config_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "tools": {
            "shell": {
                "enabled": True,
                "mode": "subprocess",
                "backend": "native",
                "containerImage": "",
                "timeout": 30,
                "maxMemoryMb": 512,
                "maxFileSizeKb": 8192,
                "isolateHome": True,
                "confirmDangerous": True,
            },
            "secretIsolation": {
                "allowEnvironmentFallback": False,
                "auditAccess": True,
            },
        },
        "extensions": {
            "requireInstallReceipt": True,
            "requireSignedBundles": False,
            "maxRiskLevel": "medium",
            "trustedPublishers": {},
            "runtimeIsolationMode": "subprocess",
            "runtimeIsolatedKinds": ["search_provider", "channel"],
            "isolatedTimeoutSeconds": 15,
        },
    }
    config["tools"]["shell"].update(shell_overrides)
    if secret_overrides:
        config["tools"]["secretIsolation"].update(secret_overrides)
    if extension_overrides:
        config["extensions"].update(extension_overrides)
    (config_dir / "config.json").write_text(json.dumps(config))


def test_security_doctor_accepts_hard_isolation_shell_config(tmp_path: Path) -> None:
    """Doctor should pass when subprocess hard isolation is enabled."""
    _write_config(tmp_path, {})

    result = SecurityDoctor(config_dir=tmp_path).check_shell_sandbox()

    assert result.passed is True
    assert "isolated HOME" in result.message


def test_security_doctor_accepts_disabled_shell(tmp_path: Path) -> None:
    """Doctor should pass when shell execution is explicitly disabled."""
    _write_config(tmp_path, {"enabled": False, "mode": "disabled"})

    result = SecurityDoctor(config_dir=tmp_path).check_shell_sandbox()

    assert result.passed is True
    assert result.message == "Shell execution is disabled"


def test_security_doctor_warns_for_inline_shell(tmp_path: Path) -> None:
    """Doctor should warn when shell execution stays inline."""
    _write_config(tmp_path, {"mode": "inline"})

    result = SecurityDoctor(config_dir=tmp_path).check_shell_sandbox()

    assert result.passed is False
    assert result.severity == "warning"
    assert "inline mode" in result.message


def test_security_doctor_warns_when_requested_backend_falls_back(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Doctor should warn when a stronger backend is requested but unavailable."""
    _write_config(tmp_path, {"backend": "bubblewrap"})
    monkeypatch.setattr(
        "nanoclaw.security.doctor.resolve_shell_backend",
        lambda backend, **kwargs: {
            "selected": "native",
            "fallback_reason": "bubblewrap unavailable",
            "stronger_backend_available": False,
        },
    )

    result = SecurityDoctor(config_dir=tmp_path).check_shell_sandbox()

    assert result.passed is False
    assert result.severity == "warning"
    assert "fell back to native" in result.message


def test_security_doctor_accepts_auto_backend_when_stronger_backend_resolves(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Doctor should pass when auto resolves to a stronger sandbox backend."""
    _write_config(tmp_path, {"backend": "auto"})
    monkeypatch.setattr(
        "nanoclaw.security.doctor.resolve_shell_backend",
        lambda backend, **kwargs: {
            "selected": "bubblewrap",
            "fallback_reason": "",
            "stronger_backend_available": True,
        },
    )

    result = SecurityDoctor(config_dir=tmp_path).check_shell_sandbox()

    assert result.passed is True
    assert "stronger backend bubblewrap" in result.message


def test_security_doctor_accepts_portable_backend_without_local_stronger_option(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Doctor should accept portable fallback when no stronger backend exists."""
    _write_config(tmp_path, {"backend": "portable"})
    monkeypatch.setattr(
        "nanoclaw.security.doctor.resolve_shell_backend",
        lambda backend, **kwargs: {
            "selected": "native",
            "fallback_reason": "no portable stronger backend available",
            "stronger_backend_available": False,
        },
    )

    result = SecurityDoctor(config_dir=tmp_path).check_shell_sandbox()

    assert result.passed is True
    assert "portable stronger-default fallback to native" in result.message


def test_security_doctor_warns_when_container_backend_cannot_activate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Doctor should warn when a container backend cannot activate."""
    _write_config(tmp_path, {"backend": "docker", "containerImage": "busybox:latest"})
    monkeypatch.setattr(
        "nanoclaw.security.doctor.resolve_shell_backend",
        lambda backend, **kwargs: {
            "selected": "native",
            "fallback_reason": "docker unavailable",
            "stronger_backend_available": False,
        },
    )

    result = SecurityDoctor(config_dir=tmp_path).check_shell_sandbox()

    assert result.passed is False
    assert result.severity == "warning"
    assert "docker" in result.message


def test_security_doctor_warns_when_primary_container_target_is_not_ready(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Doctor should warn when the primary container target is active but not ready."""
    _write_config(tmp_path, {"backend": "auto"})
    monkeypatch.setattr(
        "nanoclaw.security.doctor.resolve_shell_backend",
        lambda backend, **kwargs: {
            "selected": "docker",
            "fallback_reason": "",
            "stronger_backend_available": True,
        },
    )
    monkeypatch.setattr(
        "nanoclaw.security.doctor.inspect_container_backend_health",
        lambda **kwargs: {
            "backend": "docker",
            "ready": False,
            "status": "missing_container_image",
            "detail": "containerImage is not configured",
        },
    )

    result = SecurityDoctor(config_dir=tmp_path).check_primary_container_backend()

    assert result.passed is False
    assert result.severity == "warning"
    assert "containerImage is not configured" in result.message
    assert result.remediation


def test_security_doctor_exposes_prepare_command_for_missing_image(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Doctor should surface the orchestration path for a missing container image."""
    _write_config(tmp_path, {"backend": "docker", "containerImage": "busybox:latest"})
    monkeypatch.setattr(
        "nanoclaw.security.doctor.inspect_container_backend_health",
        lambda **kwargs: {
            "backend": "docker",
            "configured_image": "busybox:latest",
            "ready": False,
            "status": "image_missing",
            "detail": "No such image: busybox:latest",
        },
    )

    result = SecurityDoctor(config_dir=tmp_path).check_primary_container_backend()

    assert result.passed is False
    assert any("container-prepare" in item for item in result.remediation)


def test_security_doctor_exposes_runtime_command_for_unreachable_runtime(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Doctor should surface lifecycle orchestration for an unreachable runtime."""
    _write_config(tmp_path, {"backend": "docker", "containerImage": "busybox:latest"})
    monkeypatch.setattr(
        "nanoclaw.security.doctor.inspect_container_backend_health",
        lambda **kwargs: {
            "backend": "docker",
            "configured_image": "busybox:latest",
            "ready": False,
            "status": "runtime_unreachable",
            "detail": "Docker Desktop is not running",
        },
    )

    result = SecurityDoctor(config_dir=tmp_path).check_primary_container_backend()

    assert result.passed is False
    assert any("container-runtime" in item for item in result.remediation)


def test_security_doctor_warns_when_ready_runtime_has_drift(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Doctor should warn when the primary runtime is ready but drifted."""
    _write_config(tmp_path, {"backend": "docker", "containerImage": "busybox:latest"})
    monkeypatch.setattr(
        "nanoclaw.security.doctor.inspect_container_backend_health",
        lambda **kwargs: {
            "backend": "docker",
            "configured_image": "busybox:latest",
            "ready": True,
            "status": "ready",
            "detail": "sha256:abc",
            "drifted": True,
            "drift_reason": "runtime version changed from 27.0.1 to 27.1.0",
            "lifecycle_state": "runtime_version_changed",
        },
    )

    result = SecurityDoctor(config_dir=tmp_path).check_primary_container_backend()

    assert result.passed is False
    assert result.severity == "warning"
    assert "drift detected" in result.message
    assert any("container-runtime" in item for item in result.remediation)


def test_security_doctor_accepts_ready_primary_container_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Doctor should pass when the primary container target is ready."""
    _write_config(tmp_path, {"backend": "docker", "containerImage": "busybox:latest"})
    monkeypatch.setattr(
        "nanoclaw.security.doctor.inspect_container_backend_health",
        lambda **kwargs: {
            "backend": "docker",
            "ready": True,
            "status": "ready",
            "detail": "busybox:latest",
        },
    )

    result = SecurityDoctor(config_dir=tmp_path).check_primary_container_backend()

    assert result.passed is True
    assert "docker ready" in result.message


def test_security_doctor_report_includes_remediation_lines() -> None:
    """Formatted doctor reports should render remediation bullets."""
    doctor = SecurityDoctor()
    report = doctor.format_report(
        [
            doctor.check_workspace_exposure(),
            type(
                "Check",
                (),
                {
                    "name": "Primary container target",
                    "passed": False,
                    "message": "docker not ready: image missing",
                    "severity": "warning",
                    "remediation": [
                        "Pull the image locally.",
                        "Run: nanoclaw container-check --backend docker --refresh",
                    ],
                },
            )(),
        ]
    )

    assert "Pull the image locally." in report
    assert "Run: nanoclaw container-check --backend docker --refresh" in report


def test_security_doctor_warns_when_isolated_home_is_disabled(tmp_path: Path) -> None:
    """Doctor should flag subprocess mode without isolated HOME."""
    _write_config(tmp_path, {"isolateHome": False})

    result = SecurityDoctor(config_dir=tmp_path).check_shell_sandbox()

    assert result.passed is False
    assert result.severity == "warning"
    assert "isolateHome" in result.message


def test_security_doctor_warns_when_resource_limits_are_disabled(tmp_path: Path) -> None:
    """Doctor should flag subprocess mode without hard resource limits."""
    _write_config(tmp_path, {"maxMemoryMb": 0, "maxFileSizeKb": 0})

    result = SecurityDoctor(config_dir=tmp_path).check_shell_sandbox()

    assert result.passed is False
    assert result.severity == "warning"
    assert "resource limits" in result.message


def test_security_doctor_accepts_config_only_secret_isolation(tmp_path: Path) -> None:
    """Doctor should pass when tool secrets stay config-only and audited."""
    _write_config(tmp_path, {})

    result = SecurityDoctor(config_dir=tmp_path).check_secret_isolation()

    assert result.passed is True
    assert "config-only" in result.message


def test_security_doctor_warns_when_secret_env_fallback_is_enabled(tmp_path: Path) -> None:
    """Doctor should warn when tool secrets can still come from process env."""
    _write_config(
        tmp_path,
        {},
        secret_overrides={"allowEnvironmentFallback": True},
    )

    result = SecurityDoctor(config_dir=tmp_path).check_secret_isolation()

    assert result.passed is False
    assert result.severity == "warning"
    assert "environment fallback" in result.message


def test_security_doctor_warns_when_secret_audit_is_disabled(tmp_path: Path) -> None:
    """Doctor should warn when secret-capability audit logging is disabled."""
    _write_config(
        tmp_path,
        {},
        secret_overrides={"auditAccess": False},
    )

    result = SecurityDoctor(config_dir=tmp_path).check_secret_isolation()

    assert result.passed is False
    assert result.severity == "warning"
    assert "audit is off" in result.message


def test_security_doctor_accepts_trusted_extension_policy(tmp_path: Path) -> None:
    """Doctor should accept the default trusted-only extension policy."""
    _write_config(tmp_path, {})

    result = SecurityDoctor(config_dir=tmp_path).check_extension_policy()

    assert result.passed is True
    assert "require install receipts" in result.message
    assert "proactive-only channels" in result.message


def test_security_doctor_warns_when_extension_receipts_are_not_required(
    tmp_path: Path,
) -> None:
    """Doctor should warn when manual local extension loading stays enabled."""
    _write_config(
        tmp_path,
        {},
        extension_overrides={"requireInstallReceipt": False},
    )

    result = SecurityDoctor(config_dir=tmp_path).check_extension_policy()

    assert result.passed is False
    assert result.severity == "warning"
    assert "without install receipts" in result.message


def test_security_doctor_warns_when_signed_bundles_have_no_trusted_publishers(
    tmp_path: Path,
) -> None:
    """Doctor should warn when signed bundles are required without publisher trust config."""
    _write_config(
        tmp_path,
        {},
        extension_overrides={"requireSignedBundles": True},
    )

    result = SecurityDoctor(config_dir=tmp_path).check_extension_policy()

    assert result.passed is False
    assert result.severity == "warning"
    assert "no trusted publishers" in result.message


def test_security_doctor_warns_when_search_provider_isolation_is_disabled(
    tmp_path: Path,
) -> None:
    """Doctor should warn when user provider subprocess isolation is disabled."""
    _write_config(
        tmp_path,
        {},
        extension_overrides={"runtimeIsolationMode": "disabled"},
    )

    result = SecurityDoctor(config_dir=tmp_path).check_extension_policy()

    assert result.passed is False
    assert result.severity == "warning"
    assert "subprocess isolation" in result.message


def test_security_doctor_warns_when_channel_isolation_is_missing(
    tmp_path: Path,
) -> None:
    """Doctor should warn when proactive-only user channels stay in-process."""
    _write_config(
        tmp_path,
        {},
        extension_overrides={"runtimeIsolatedKinds": ["search_provider"]},
    )

    result = SecurityDoctor(config_dir=tmp_path).check_extension_policy()

    assert result.passed is False
    assert result.severity == "warning"
    assert "proactive-only channels" in result.message
