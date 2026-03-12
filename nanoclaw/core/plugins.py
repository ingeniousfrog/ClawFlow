"""Lightweight plugin manifest registry for installable extensions."""

from __future__ import annotations

import importlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import sys
from typing import Optional

from pydantic import BaseModel, Field

from nanoclaw.core.extension_installer import (
    USER_INSTALLABLE_KINDS,
    evaluate_runtime_extension_policy,
)
from nanoclaw.core.logger import get_logger

logger = get_logger(__name__)

_PLUGIN_SUFFIX = ".plugin.json"


class PluginManifest(BaseModel):
    """Declarative metadata for one installable plugin."""

    name: str
    kind: str = "skill"
    module: str = ""
    provides: list[str] = Field(default_factory=list)
    tool_names: list[str] = Field(default_factory=list, alias="toolNames")
    summary: str = ""
    entry_points: list[str] = Field(default_factory=list, alias="entryPoints")
    triggers: list[str] = Field(default_factory=list)
    dependencies: list[str] = Field(default_factory=list)
    metadata: dict[str, object] = Field(default_factory=dict)
    risk_level: str = Field(default="low", alias="riskLevel")
    enabled: bool = True
    source_scope: str = Field(default="", alias="sourceScope")
    manifest_path: str = Field(default="", alias="manifestPath")

    model_config = {"populate_by_name": True}

    @property
    def provided_names(self) -> list[str]:
        """Return the extension identifiers surfaced by this manifest."""
        if self.provides:
            return list(self.provides)
        if self.tool_names:
            return list(self.tool_names)
        return [self.name]

    @property
    def primary_name(self) -> str:
        """Return the main identifier exposed by this manifest."""
        provided = self.provided_names
        if provided:
            return provided[0]
        return self.name

    @property
    def primary_tool_name(self) -> str:
        """Return the main tool exposed by this manifest."""
        return self.primary_name


class PluginRegistry:
    """Cached registry for plugin manifests."""

    def __init__(self, manifests: list[PluginManifest]) -> None:
        """Store manifests and build lookup tables."""
        self.manifests = manifests

    def get_enabled_manifests(self, kind: str = "") -> list[PluginManifest]:
        """Return enabled manifests, applying source precedence by kind+name."""
        resolved = self.get_manifest_map(kind, include_disabled=True)
        manifests = [item for item in resolved.values() if item.enabled]
        return sorted(
            manifests,
            key=lambda item: (item.kind, item.source_scope != "builtin", item.primary_name),
        )

    def get_enabled_skill_manifests(self) -> list[PluginManifest]:
        """Return enabled skill manifests in stable order."""
        return self.get_enabled_manifests("skill")

    def get_enabled_channel_manifests(self) -> list[PluginManifest]:
        """Return enabled channel manifests in stable order."""
        return self.get_enabled_manifests("channel")

    def get_enabled_search_provider_manifests(self) -> list[PluginManifest]:
        """Return enabled search-provider manifests in stable order."""
        return self.get_enabled_manifests("search_provider")

    def get_skill_trigger_map(self) -> dict[str, list[str]]:
        """Return manifest-backed trigger keywords keyed by tool name."""
        trigger_map: dict[str, list[str]] = {}
        for item in self.get_enabled_skill_manifests():
            if not item.triggers:
                continue
            for tool_name in item.provided_names:
                trigger_map[tool_name] = list(item.triggers)
        return trigger_map

    def get_skill_manifest_map(self) -> dict[str, PluginManifest]:
        """Return enabled skill manifests keyed by primary tool name."""
        return {
            item.primary_name: item
            for item in self.get_enabled_skill_manifests()
        }

    def get_manifest_map(
        self,
        kind: str = "",
        *,
        include_disabled: bool = False,
    ) -> dict[str, PluginManifest]:
        """Return manifests keyed by primary name after source-precedence resolution."""
        resolved: dict[tuple[str, str], PluginManifest] = {}
        for item in sorted(self.manifests, key=_manifest_sort_key):
            if kind and item.kind != kind:
                continue
            key = (item.kind, item.primary_name)
            existing = resolved.get(key)
            if existing is None or _manifest_precedence(item) >= _manifest_precedence(existing):
                resolved[key] = item
        if include_disabled:
            return {
                _manifest_map_key(item, kind): item
                for item in resolved.values()
            }
        return {
            _manifest_map_key(item, kind): item
            for item in resolved.values()
            if item.enabled
        }

    def get_extension_summary(self) -> dict[str, int]:
        """Return compact enabled-manifest counts by extension kind."""
        summary = {
            "total": 0,
            "skills": 0,
            "channels": 0,
            "search_providers": 0,
        }
        for item in self.get_enabled_manifests():
            summary["total"] += 1
            if item.kind == "skill":
                summary["skills"] += 1
            elif item.kind == "channel":
                summary["channels"] += 1
            elif item.kind == "search_provider":
                summary["search_providers"] += 1
        return summary


_plugin_registry: Optional[PluginRegistry] = None


def get_builtin_plugin_dir() -> Path:
    """Return the built-in skill manifest directory."""
    return Path(__file__).resolve().parent.parent / "skills"


def get_builtin_channel_plugin_dir() -> Path:
    """Return the built-in channel manifest directory."""
    return Path(__file__).resolve().parent.parent / "channels"


def get_builtin_provider_plugin_dir() -> Path:
    """Return the built-in search-provider manifest directory."""
    return Path(__file__).resolve().parent.parent / "tools"


def get_user_plugin_dir() -> Path:
    """Return the user skill manifest directory."""
    return Path.home() / ".nanoclaw" / "skills"


def get_user_extension_dir() -> Path:
    """Return the user extension manifest directory."""
    return Path.home() / ".nanoclaw" / "extensions"


def load_plugin_manifests_from_directory(
    directory: str | Path,
    *,
    source_scope: str = "",
) -> list[PluginManifest]:
    """Load all `*.plugin.json` files from one directory."""
    base_dir = Path(directory)
    if not base_dir.exists():
        return []
    enforce_user_security = _should_enforce_runtime_security(base_dir, source_scope)
    if enforce_user_security and not _is_secure_extension_path(base_dir):
        logger.warning("Skipping unsafe user extension directory: %s", base_dir)
        return []
    (
        require_install_receipt,
        require_signed_bundles,
        max_risk_level,
        publisher_policy,
    ) = _get_extension_policy()

    manifests: list[PluginManifest] = []
    for manifest_path in sorted(base_dir.glob(f"*{_PLUGIN_SUFFIX}")):
        if enforce_user_security and not _is_secure_extension_path(manifest_path):
            logger.warning("Skipping unsafe user extension manifest: %s", manifest_path)
            continue
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = PluginManifest(**raw)
        except Exception as exc:
            logger.error("Failed to parse plugin manifest %s: %s", manifest_path, exc)
            continue
        if (
            enforce_user_security
            and manifest.kind in USER_INSTALLABLE_KINDS
            and base_dir.resolve() == get_user_extension_dir().resolve()
        ):
            policy = evaluate_runtime_extension_policy(
                manifest_path=manifest_path,
                kind=manifest.kind,
                primary_name=manifest.primary_name,
                module_name=manifest.module.strip(),
                metadata=dict(manifest.metadata or {}),
                risk_level=manifest.risk_level,
                require_install_receipt=require_install_receipt,
                require_signed_bundles=require_signed_bundles,
                max_risk_level=max_risk_level,
                publisher_policy=publisher_policy,
            )
            if not policy.allowed:
                logger.warning(
                    "Skipping user extension manifest %s: %s",
                    manifest_path,
                    policy.reason,
                )
                continue

        module = manifest.module.strip() or _manifest_module_stem(manifest_path)
        provided_names = manifest.provided_names
        source = manifest.source_scope.strip() or source_scope or _infer_source_scope(base_dir)
        manifests.append(
            _model_copy(
                manifest,
                update={
                    "module": module,
                    "provides": provided_names,
                    "tool_names": provided_names,
                    "source_scope": source,
                    "manifest_path": str(manifest_path),
                },
            )
        )
    return manifests


def get_plugin_registry(*, force_reload: bool = False) -> PluginRegistry:
    """Return the cached plugin manifest registry."""
    global _plugin_registry
    if _plugin_registry is None or force_reload:
        manifests = []
        for directory, source_scope in (
            (get_builtin_plugin_dir(), "builtin"),
            (get_builtin_channel_plugin_dir(), "builtin"),
            (get_builtin_provider_plugin_dir(), "builtin"),
            (get_user_plugin_dir(), "user"),
            (get_user_extension_dir(), "user"),
        ):
            manifests.extend(
                load_plugin_manifests_from_directory(
                    directory,
                    source_scope=source_scope,
                )
            )
        _plugin_registry = PluginRegistry(manifests)
    return _plugin_registry


def reset_plugin_registry() -> None:
    """Reset the cached plugin registry."""
    global _plugin_registry
    _plugin_registry = None


def load_manifest_object(
    import_path: str,
    *,
    manifest_name: str,
    manifest_path: str = "",
    source_scope: str = "",
) -> object:
    """Load one `module:attribute` object referenced by a manifest."""
    normalized = str(import_path or "").strip()
    if ":" not in normalized:
        raise ValueError(
            f"Manifest `{manifest_name}` uses invalid import path `{import_path}`."
        )
    module_name, attribute_path = normalized.split(":", 1)
    module_name = module_name.strip()
    attribute_path = attribute_path.strip()
    if not module_name or not attribute_path:
        raise ValueError(
            f"Manifest `{manifest_name}` uses invalid import path `{import_path}`."
        )
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError:
        module = _load_local_manifest_module(
            module_name,
            manifest_name=manifest_name,
            manifest_path=manifest_path,
            source_scope=source_scope,
        )
    value: object = module
    for attribute in attribute_path.split("."):
        if not hasattr(value, attribute):
            raise ValueError(
                f"Manifest `{manifest_name}` references missing object "
                f"`{attribute_path}` from `{module_name}`."
            )
        value = getattr(value, attribute)
    return value


def _manifest_module_stem(path: Path) -> str:
    """Convert `weather.plugin.json` -> `weather`."""
    return path.name[: -len(_PLUGIN_SUFFIX)]


def _infer_source_scope(directory: Path) -> str:
    """Infer source scope for manifests outside the default locations."""
    resolved = directory.resolve()
    builtin_dirs = {
        get_builtin_plugin_dir().resolve(),
        get_builtin_channel_plugin_dir().resolve(),
        get_builtin_provider_plugin_dir().resolve(),
    }
    user_dirs = {
        get_user_plugin_dir().resolve(),
        get_user_extension_dir().resolve(),
    }
    if resolved in builtin_dirs:
        return "builtin"
    if resolved in user_dirs:
        return "user"
    return "custom"


def _manifest_precedence(item: PluginManifest) -> int:
    """Return precedence for manifest override resolution."""
    if item.source_scope == "builtin":
        return 0
    if item.source_scope == "user":
        return 1
    return 2


def _manifest_sort_key(item: PluginManifest) -> tuple[int, str, str, str]:
    """Return a stable manifest ordering before precedence resolution."""
    return (
        _manifest_precedence(item),
        item.kind,
        item.primary_name,
        item.manifest_path,
    )


def _manifest_map_key(item: PluginManifest, kind: str) -> str:
    """Return the external manifest-map key for the requested scope."""
    if kind:
        return item.primary_name
    return f"{item.kind}:{item.primary_name}"


def _model_copy(item: PluginManifest, update: dict[str, object]) -> PluginManifest:
    """Copy a pydantic model across v1 and v2."""
    if hasattr(item, "model_copy"):
        return item.model_copy(update=update, deep=True)
    return item.copy(update=update, deep=True)


def _should_enforce_runtime_security(directory: Path, source_scope: str) -> bool:
    """Return whether manifest loading should enforce user-extension permissions."""
    if source_scope == "user":
        return True
    resolved = directory.resolve()
    user_dirs = {
        get_user_plugin_dir().resolve(),
        get_user_extension_dir().resolve(),
    }
    return resolved in user_dirs


def _get_extension_policy() -> tuple[bool, bool, str, object | None]:
    """Return the current third-party extension trust policy."""
    try:
        from nanoclaw.core.config import get_config

        policy = get_config().extensions
        return (
            bool(getattr(policy, "require_install_receipt", True)),
            bool(getattr(policy, "require_signed_bundles", False)),
            str(getattr(policy, "max_risk_level", "medium") or "medium"),
            policy,
        )
    except Exception:
        return True, False, "medium", None


def _is_secure_extension_path(path: Path) -> bool:
    """Return whether one user extension path is owned by the current user and private."""
    try:
        file_stat = path.stat()
    except OSError:
        return False
    if file_stat.st_uid != os.getuid():
        return False
    if file_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        return False
    return True


def _load_local_manifest_module(
    module_name: str,
    *,
    manifest_name: str,
    manifest_path: str,
    source_scope: str,
) -> object:
    """Load one local user-extension module adjacent to its manifest."""
    if not manifest_path:
        raise ValueError(
            f"Manifest `{manifest_name}` could not resolve a local extension module."
        )
    manifest_file = Path(manifest_path).resolve()
    manifest_dir = manifest_file.parent
    if source_scope == "user":
        if not _is_secure_extension_path(manifest_dir) or not _is_secure_extension_path(manifest_file):
            raise ValueError(f"Manifest `{manifest_name}` is in an unsafe user extension path.")
    module_path, is_package = _resolve_local_module_path(module_name, manifest_dir)
    if source_scope == "user":
        if not _is_secure_extension_path(module_path):
            raise ValueError(
                f"Manifest `{manifest_name}` references an unsafe user extension module."
            )
        if is_package and not _is_secure_extension_path(module_path.parent):
            raise ValueError(
                f"Manifest `{manifest_name}` references an unsafe user extension package."
            )
    unique_name = f"nanoclaw_user_ext_{abs(hash(str(module_path)))}"
    if unique_name in sys.modules:
        return sys.modules[unique_name]
    kwargs = (
        {"submodule_search_locations": [str(module_path.parent)]}
        if is_package
        else {}
    )
    spec = importlib.util.spec_from_file_location(unique_name, module_path, **kwargs)
    if spec is None or spec.loader is None:
        raise ValueError(
            f"Manifest `{manifest_name}` could not load local module `{module_name}`."
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(unique_name, None)
        raise
    return module


def _resolve_local_module_path(module_name: str, manifest_dir: Path) -> tuple[Path, bool]:
    """Resolve one dotted module name to an adjacent file or package path."""
    relative_path = Path(*module_name.split("."))
    file_candidate = (manifest_dir / relative_path).with_suffix(".py")
    if file_candidate.exists():
        resolved = file_candidate.resolve()
        if not resolved.is_relative_to(manifest_dir):
            raise ValueError("Local extension module escapes the extension directory.")
        return resolved, False
    package_candidate = (manifest_dir / relative_path / "__init__.py")
    if package_candidate.exists():
        resolved = package_candidate.resolve()
        if not resolved.is_relative_to(manifest_dir):
            raise ValueError("Local extension package escapes the extension directory.")
        return resolved, True
    raise ValueError(f"Local extension module `{module_name}` was not found next to its manifest.")
