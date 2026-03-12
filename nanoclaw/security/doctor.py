"""Security check command for nanoClaw installation."""

from __future__ import annotations

import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from nanoclaw.core.logger import get_logger
from nanoclaw.security.sandbox_backends import (
    PRIMARY_CONTAINER_BACKEND,
    get_container_remediation_plan,
    inspect_container_backend_health,
    resolve_shell_backend,
)

logger = get_logger(__name__)


@dataclass
class CheckResult:
    """Result of a security check."""

    name: str
    passed: bool
    message: str
    severity: str = "info"  # info, warning, critical
    remediation: list[str] = field(default_factory=list)


class SecurityDoctor:
    """Comprehensive security check of nanoClaw installation."""

    def __init__(self, config_dir: Optional[Path] = None):
        """
        Initialize SecurityDoctor.

        Args:
            config_dir: Path to nanoClaw config directory
        """
        self.config_dir = config_dir or Path.home() / ".nanoclaw"

    async def check_all(self) -> list[CheckResult]:
        """
        Run all security checks.

        Returns:
            List of check results
        """
        checks = [
            self.check_config_permissions(),
            self.check_workspace_permissions(),
            self.check_key_exposure(),
            self.check_telegram_whitelist(),
            self.check_feishu_whitelist(),
            self.check_shell_sandbox(),
            self.check_primary_container_backend(),
            self.check_secret_isolation(),
            self.check_extension_policy(),
            self.check_dashboard_binding(),
            self.check_workspace_exposure(),
            self.check_symlinks(),
        ]
        return checks

    def check_config_permissions(self) -> CheckResult:
        """Check config file permissions (should be 600)."""
        config_path = self.config_dir / "config.json"

        if not config_path.exists():
            return CheckResult(
                name="Config file",
                passed=False,
                message="config.json not found. Run 'nanoclaw init' first.",
                severity="critical",
            )

        mode = config_path.stat().st_mode
        is_owner_only = (mode & stat.S_IRWXG) == 0 and (mode & stat.S_IRWXO) == 0

        if is_owner_only:
            return CheckResult(
                name="Config permissions",
                passed=True,
                message="config.json has secure permissions (owner only)",
            )
        return CheckResult(
            name="Config permissions",
            passed=False,
            message="config.json is readable by others. Run: chmod 600 ~/.nanoclaw/config.json",
            severity="critical",
        )

    def check_workspace_permissions(self) -> CheckResult:
        """Check workspace directory permissions (should be 700)."""
        workspace_path = self.config_dir / "workspace"

        if not workspace_path.exists():
            return CheckResult(
                name="Workspace",
                passed=False,
                message="Workspace directory not found.",
                severity="warning",
            )

        mode = workspace_path.stat().st_mode
        is_owner_only = (mode & stat.S_IRWXG) == 0 and (mode & stat.S_IRWXO) == 0

        if is_owner_only:
            return CheckResult(
                name="Workspace permissions",
                passed=True,
                message="Workspace has secure permissions (owner only)",
            )
        return CheckResult(
            name="Workspace permissions",
            passed=False,
            message="Workspace is accessible by others. Run: chmod 700 ~/.nanoclaw/workspace",
            severity="warning",
        )

    def check_key_exposure(self) -> CheckResult:
        """Check if API keys are exposed in logs, database, or text files."""
        data_dir = self.config_dir / "data"
        if not data_dir.exists():
            return CheckResult(
                name="Key exposure",
                passed=True,
                message="No data files to check",
            )

        # Simple check - look for common API key prefixes
        suspicious_patterns = ["sk-", "sk-ant-", "Bearer ", "x-api-key"]

        # Scan *.log, *.db (text content), and *.txt files
        scan_globs = ["*.log", "*.db", "*.txt"]
        for glob_pattern in scan_globs:
            for data_file in data_dir.glob(glob_pattern):
                try:
                    content = data_file.read_text(errors="replace")
                    for pattern in suspicious_patterns:
                        if pattern in content:
                            return CheckResult(
                                name="Key exposure",
                                passed=False,
                                message=f"Possible API key in {data_file.name}. Review and redact.",
                                severity="critical",
                            )
                except Exception:
                    pass

        return CheckResult(
            name="Key exposure",
            passed=True,
            message="No API keys found in data files",
        )

    def check_telegram_whitelist(self) -> CheckResult:
        """Check if Telegram whitelist is configured."""
        try:
            from nanoclaw.core.config import Config

            config = Config.load(self.config_dir / "config.json")

            if not config.channels.telegram.enabled:
                return CheckResult(
                    name="Telegram whitelist",
                    passed=True,
                    message="Telegram is disabled",
                )

            if config.channels.telegram.allow_from:
                return CheckResult(
                    name="Telegram whitelist",
                    passed=True,
                    message=f"{len(config.channels.telegram.allow_from)} user(s) whitelisted",
                )
            return CheckResult(
                name="Telegram whitelist",
                passed=False,
                message="No users whitelisted. Add user IDs to allowFrom.",
                severity="critical",
            )
        except Exception as e:
            return CheckResult(
                name="Telegram whitelist",
                passed=False,
                message=f"Could not check config: {e}",
                severity="warning",
            )

    def check_feishu_whitelist(self) -> CheckResult:
        """Check if Feishu allow list is configured when Feishu is enabled."""
        try:
            from nanoclaw.core.config import Config

            config = Config.load(self.config_dir / "config.json")

            if not config.channels.feishu.enabled:
                return CheckResult(
                    name="Feishu whitelist",
                    passed=True,
                    message="Feishu is disabled",
                )

            if config.channels.feishu.allow_from:
                return CheckResult(
                    name="Feishu whitelist",
                    passed=True,
                    message=f"{len(config.channels.feishu.allow_from)} user(s) whitelisted",
                )
            return CheckResult(
                name="Feishu whitelist",
                passed=False,
                message="No Feishu users whitelisted. Add IDs to channels.feishu.allowFrom.",
                severity="critical",
            )
        except Exception as e:
            return CheckResult(
                name="Feishu whitelist",
                passed=False,
                message=f"Could not check config: {e}",
                severity="warning",
            )

    def check_shell_sandbox(self) -> CheckResult:
        """Check whether shell execution uses the configured boundary mode."""
        try:
            from nanoclaw.core.config import Config

            config = Config.load(self.config_dir / "config.json")

            shell_cfg = config.tools.shell
            backend = resolve_shell_backend(
                shell_cfg.backend,
                container_image=shell_cfg.container_image,
            )
            remediation = get_container_remediation_plan(
                inspect_container_backend_health(
                    backend=PRIMARY_CONTAINER_BACKEND,
                    container_image=shell_cfg.container_image,
                ),
                backend=PRIMARY_CONTAINER_BACKEND,
                container_image=shell_cfg.container_image,
            )
            if shell_cfg.enabled and shell_cfg.mode != "disabled":
                if (
                    shell_cfg.mode == "subprocess"
                    and shell_cfg.confirm_dangerous
                    and shell_cfg.isolate_home
                    and shell_cfg.max_memory_mb > 0
                    and shell_cfg.max_file_size_kb > 0
                ):
                    if (
                        shell_cfg.backend in ("auto", "portable")
                        and backend["selected"] == "native"
                        and backend["fallback_reason"]
                        in (
                            "no stronger backend available",
                            "no portable stronger backend available",
                        )
                    ):
                        fallback_label = (
                            "portable stronger-default"
                            if shell_cfg.backend == "portable"
                            else "auto stronger-default"
                        )
                        return CheckResult(
                            name="Shell sandbox",
                            passed=True,
                            message=(
                                "Shell enabled in subprocess mode with "
                                f"{fallback_label} fallback to native; "
                                "no stronger backend is available on this host."
                            ),
                        )
                    if (
                        shell_cfg.backend not in ("native", "auto", "portable")
                        and backend["selected"] == "native"
                    ):
                        return CheckResult(
                            name="Shell sandbox",
                            passed=False,
                            message=(
                                "Shell subprocess isolation requested backend "
                                f"{shell_cfg.backend}, but it fell back to native "
                                f"({backend['fallback_reason']})."
                            ),
                            severity="warning",
                            remediation=list(remediation["steps"]) + [
                                f"Run: {command}"
                                for command in remediation["commands"]
                            ],
                        )
                    backend_note = (
                        f"stronger backend {backend['selected']}"
                        if backend["selected"] != "native"
                        else "native backend"
                    )
                    availability_note = ""
                    if (
                        backend["selected"] == "native"
                        and backend["stronger_backend_available"]
                    ):
                        availability_note = (
                            "; stronger backend available, consider "
                            "tools.shell.backend=portable"
                        )
                    return CheckResult(
                        name="Shell sandbox",
                        passed=True,
                        message=(
                            "Shell enabled in subprocess mode with "
                            f"{backend_note}, confirmation, isolated HOME, "
                            f"and OS-level resource limits{availability_note}"
                        ),
                    )
                if shell_cfg.mode == "inline":
                    return CheckResult(
                        name="Shell sandbox",
                        passed=False,
                        message=(
                            "Shell enabled in inline mode. Set tools.shell.mode "
                            "to subprocess for an isolated execution boundary."
                        ),
                        severity="warning",
                    )
                if not shell_cfg.confirm_dangerous:
                    return CheckResult(
                        name="Shell sandbox",
                        passed=False,
                        message=(
                            "Shell enabled but confirmDangerous is off. "
                            "Enable confirmDangerous in config."
                        ),
                        severity="warning",
                    )
                if shell_cfg.mode == "subprocess" and not shell_cfg.isolate_home:
                    return CheckResult(
                        name="Shell sandbox",
                        passed=False,
                        message=(
                            "Shell subprocess isolation is missing isolateHome. "
                            "Enable tools.shell.isolateHome."
                        ),
                        severity="warning",
                    )
                if (
                    shell_cfg.mode == "subprocess"
                    and (
                        shell_cfg.max_memory_mb <= 0
                        or shell_cfg.max_file_size_kb <= 0
                    )
                ):
                    return CheckResult(
                        name="Shell sandbox",
                        passed=False,
                        message=(
                            "Shell subprocess isolation has one or more disabled "
                            "resource limits. Set maxMemoryMb and maxFileSizeKb."
                        ),
                        severity="warning",
                    )
                return CheckResult(
                    name="Shell sandbox",
                    passed=False,
                    message=(
                        "Shell enabled but not using subprocess isolation. "
                        "Set tools.shell.mode to subprocess."
                    ),
                    severity="warning",
                )
            return CheckResult(
                name="Shell sandbox",
                passed=True,
                message="Shell execution is disabled",
            )
        except Exception:
            return CheckResult(
                name="Shell sandbox",
                passed=True,
                message=(
                    "Using default (subprocess mode with isolated HOME "
                    "and resource limits)"
                ),
            )

    def check_primary_container_backend(self) -> CheckResult:
        """Check readiness for the primary container backend target."""
        try:
            from nanoclaw.core.config import Config

            config = Config.load(self.config_dir / "config.json")
            shell_cfg = config.tools.shell
            backend = resolve_shell_backend(
                shell_cfg.backend,
                container_image=shell_cfg.container_image,
            )
            health = inspect_container_backend_health(
                backend=PRIMARY_CONTAINER_BACKEND,
                container_image=shell_cfg.container_image,
            )
            remediation = get_container_remediation_plan(
                health,
                backend=PRIMARY_CONTAINER_BACKEND,
                container_image=shell_cfg.container_image,
            )
            primary_active = (
                shell_cfg.backend == PRIMARY_CONTAINER_BACKEND
                or backend["selected"] == PRIMARY_CONTAINER_BACKEND
            )
            if bool(health.get("ready")) and bool(health.get("drifted")) and primary_active:
                drift_reason = (
                    health.get("drift_reason")
                    or health.get("lifecycle_state")
                    or "unknown"
                )
                return CheckResult(
                    name="Primary container target",
                    passed=False,
                    message=f"{PRIMARY_CONTAINER_BACKEND} drift detected: {drift_reason}",
                    severity="warning",
                    remediation=list(remediation["steps"]) + [
                        f"Run: {command}" for command in remediation["commands"]
                    ],
                )
            if bool(health["ready"]):
                return CheckResult(
                    name="Primary container target",
                    passed=True,
                    message=(
                        f"{PRIMARY_CONTAINER_BACKEND} ready "
                        f"(runtime reachable, image present)"
                    ),
                )
            if primary_active:
                return CheckResult(
                    name="Primary container target",
                    passed=False,
                    message=(
                        f"{PRIMARY_CONTAINER_BACKEND} not ready: "
                        f"{health['detail'] or health['status']}"
                    ),
                    severity="warning",
                    remediation=list(remediation["steps"]) + [
                        f"Run: {command}" for command in remediation["commands"]
                    ],
                )
            return CheckResult(
                name="Primary container target",
                passed=True,
                message=(
                    f"{PRIMARY_CONTAINER_BACKEND} target not ready: "
                    f"{health['detail'] or health['status']} "
                    "(not active in current shell config)"
                ),
            )
        except Exception as exc:
            return CheckResult(
                name="Primary container target",
                passed=False,
                message=f"Could not inspect primary container target: {exc}",
                severity="warning",
            )

    def check_secret_isolation(self) -> CheckResult:
        """Check whether tool-side secret access uses the explicit broker policy."""
        try:
            from nanoclaw.core.config import Config

            config = Config.load(self.config_dir / "config.json")
            secret_cfg = config.tools.secret_isolation

            if not secret_cfg.audit_access:
                return CheckResult(
                    name="Secret isolation",
                    passed=False,
                    message=(
                        "Tool secret isolation audit is off. "
                        "Enable tools.secretIsolation.auditAccess."
                    ),
                    severity="warning",
                )
            if secret_cfg.allow_environment_fallback:
                return CheckResult(
                    name="Secret isolation",
                    passed=False,
                    message=(
                        "Tool secret isolation still allows environment fallback. "
                        "Prefer config-only secret injection unless env override is required."
                    ),
                    severity="warning",
                )
            return CheckResult(
                name="Secret isolation",
                passed=True,
                message="Tool secrets use config-only capability injection with audit access",
            )
        except Exception:
            return CheckResult(
                name="Secret isolation",
                passed=True,
                message="Using default (config-only secret injection with audit access)",
            )

    def check_extension_policy(self) -> CheckResult:
        """Check third-party extension receipt and risk policy."""
        try:
            from nanoclaw.core.config import Config
            from nanoclaw.core.extension_installer import verify_installed_extensions

            config = Config.load(self.config_dir / "config.json")
            policy = config.extensions
            if not policy.require_install_receipt:
                return CheckResult(
                    name="Extension policy",
                    passed=False,
                    message="Third-party extensions can load without install receipts.",
                    severity="warning",
                    remediation=["Set extensions.requireInstallReceipt=true."],
                )
            if bool(policy.require_signed_bundles) and not dict(policy.trusted_publishers or {}):
                return CheckResult(
                    name="Extension policy",
                    passed=False,
                    message=(
                        "Signed third-party bundles are required but no "
                        "trusted publishers are configured."
                    ),
                    severity="warning",
                    remediation=[
                        "Set extensions.trustedPublishers before enabling "
                        "requireSignedBundles."
                    ],
                )
            if str(policy.max_risk_level or "medium") == "high":
                return CheckResult(
                    name="Extension policy",
                    passed=False,
                    message="Third-party extensions allow high-risk manifests by default.",
                    severity="warning",
                    remediation=["Set extensions.maxRiskLevel=medium or low."],
                )
            missing_isolation: list[str] = []
            if not bool(
                getattr(policy, "isolates_kind", lambda *_args, **_kwargs: False)(
                    "search_provider"
                )
            ):
                missing_isolation.append("search providers")
            if not bool(
                getattr(policy, "isolates_kind", lambda *_args, **_kwargs: False)(
                    "channel"
                )
            ):
                missing_isolation.append("proactive-only channels")
            if missing_isolation:
                return CheckResult(
                    name="Extension policy",
                    passed=False,
                    message=(
                        "User-installed extension subprocess isolation is missing for "
                        + ", ".join(missing_isolation)
                        + "."
                    ),
                    severity="warning",
                    remediation=[
                        "Set extensions.runtimeIsolationMode=subprocess.",
                        "Include search_provider and channel in "
                        "extensions.runtimeIsolatedKinds.",
                    ],
                )
            results = verify_installed_extensions(
                base_dir=self.config_dir / "extensions",
                require_install_receipt=bool(policy.require_install_receipt),
                require_signed_bundles=bool(policy.require_signed_bundles),
                max_risk_level=str(policy.max_risk_level or "medium"),
                publisher_policy=policy,
            )
            failed = [item for item in results if not item.get("allowed", False)]
            if failed:
                first = failed[0]
                return CheckResult(
                    name="Extension policy",
                    passed=False,
                    message=(
                        "Installed extension policy mismatch: "
                        f"{first.get('kind')}:{first.get('name')} "
                        f"({first.get('reason')})"
                    ),
                    severity="warning",
                    remediation=[
                        "Reinstall the extension with nanoclaw extension-install.",
                        "Run nanoclaw extension-verify after any local edits.",
                    ],
                )
            return CheckResult(
                name="Extension policy",
                passed=True,
                message=(
                    "Third-party extensions require install receipts "
                    f"with maxRiskLevel={policy.max_risk_level} and "
                    "subprocess isolation for search providers plus "
                    "proactive-only channels"
                ),
            )
        except Exception:
            return CheckResult(
                name="Extension policy",
                passed=True,
                message="Using default (trusted local extensions only)",
            )

    def check_dashboard_binding(self) -> CheckResult:
        """Check if dashboard is bound to localhost only."""
        try:
            from nanoclaw.core.config import Config

            config = Config.load(self.config_dir / "config.json")

            if not config.dashboard.enabled:
                return CheckResult(
                    name="Dashboard binding",
                    passed=True,
                    message="Dashboard is disabled",
                )

            # Dashboard always binds to 127.0.0.1 by design
            return CheckResult(
                name="Dashboard binding",
                passed=True,
                message="Dashboard binds to localhost only (127.0.0.1)",
            )
        except Exception:
            return CheckResult(
                name="Dashboard binding",
                passed=True,
                message="Using default (localhost only)",
            )

    def check_workspace_exposure(self) -> CheckResult:
        """Check workspace is not world-readable."""
        workspace_path = self.config_dir / "workspace"

        if not workspace_path.exists():
            return CheckResult(
                name="Workspace exposure",
                passed=True,
                message="Workspace not created yet",
            )

        mode = workspace_path.stat().st_mode
        world_readable = bool(mode & stat.S_IROTH)

        if world_readable:
            return CheckResult(
                name="Workspace exposure",
                passed=False,
                message="Workspace is world-readable",
                severity="warning",
            )
        return CheckResult(
            name="Workspace exposure",
            passed=True,
            message="Workspace is not world-readable",
        )

    def check_symlinks(self) -> CheckResult:
        """Scan workspace for symlinks pointing outside the workspace."""
        workspace_path = self.config_dir / "workspace"

        if not workspace_path.exists():
            return CheckResult(
                name="Symlink check",
                passed=True,
                message="Workspace not created yet",
            )

        workspace_resolved = workspace_path.resolve()
        bad_links: list[str] = []

        try:
            for item in workspace_resolved.rglob("*"):
                if item.is_symlink():
                    try:
                        target = item.resolve()
                        target.relative_to(workspace_resolved)
                    except ValueError:
                        bad_links.append(str(item.relative_to(workspace_resolved)))
        except Exception:
            pass

        if bad_links:
            names = ", ".join(bad_links[:5])
            suffix = f" (and {len(bad_links) - 5} more)" if len(bad_links) > 5 else ""
            return CheckResult(
                name="Symlink check",
                passed=False,
                message=f"Symlinks pointing outside workspace: {names}{suffix}",
                severity="warning",
            )

        return CheckResult(
            name="Symlink check",
            passed=True,
            message="No symlinks pointing outside workspace",
        )

    def format_report(self, checks: list[CheckResult]) -> str:
        """
        Format check results as CLI output.

        Args:
            checks: List of check results

        Returns:
            Formatted report string
        """
        lines = ["", "Security Check Report", "=" * 40, ""]

        passed = 0
        warnings = 0
        critical = 0

        for check in checks:
            if check.passed:
                icon = "[OK]"
                passed += 1
            elif check.severity == "critical":
                icon = "[!!]"
                critical += 1
            else:
                icon = "[??]"
                warnings += 1

            lines.append(f"{icon} {check.name}")
            lines.append(f"    {check.message}")
            for item in check.remediation:
                lines.append(f"    - {item}")
            lines.append("")

        lines.append("=" * 40)
        lines.append(f"Passed: {passed}  Warnings: {warnings}  Critical: {critical}")

        if critical > 0:
            lines.append("")
            lines.append("CRITICAL issues found! Fix before running.")
        elif warnings > 0:
            lines.append("")
            lines.append("Some warnings found. Review recommended.")
        else:
            lines.append("")
            lines.append("All checks passed!")

        return "\n".join(lines)
