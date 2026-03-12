"""Plugin manifest registry tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from nanoclaw.core.plugins import (
    PluginManifest,
    PluginRegistry,
    load_plugin_manifests_from_directory,
)


def test_load_plugin_manifests_from_directory_reads_skill_metadata(tmp_path: Path) -> None:
    """Manifest loader should parse one skill manifest and normalize defaults."""
    manifest_path = tmp_path / "demo.plugin.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "demo_tool",
                "kind": "skill",
                "summary": "Demo plugin",
                "triggers": ["demo", "example"],
                "metadata": {"runtime": {"handlerPath": "demo.module:run"}},
                "riskLevel": "medium",
            }
        ),
        encoding="utf-8",
    )

    manifests = load_plugin_manifests_from_directory(tmp_path, source_scope="custom")

    assert len(manifests) == 1
    manifest = manifests[0]
    assert manifest.module == "demo"
    assert manifest.provides == ["demo_tool"]
    assert manifest.tool_names == ["demo_tool"]
    assert manifest.triggers == ["demo", "example"]
    assert manifest.metadata == {"runtime": {"handlerPath": "demo.module:run"}}
    assert manifest.risk_level == "medium"
    assert manifest.source_scope == "custom"


def test_plugin_registry_prefers_higher_scope_manifest_for_same_extension() -> None:
    """Registry should let higher-scope manifests override built-in metadata."""
    registry = PluginRegistry(
        [
            PluginManifest(
                name="console_builtin",
                kind="channel",
                module="console",
                provides=["console"],
                summary="Built-in console channel",
                sourceScope="builtin",
                enabled=True,
            ),
            PluginManifest(
                name="console_override",
                kind="channel",
                module="console",
                provides=["console"],
                summary="Disabled override",
                sourceScope="user",
                enabled=False,
            ),
        ]
    )

    assert registry.get_enabled_channel_manifests() == []
    manifest_map = registry.get_manifest_map("channel", include_disabled=True)
    assert manifest_map["console"].summary == "Disabled override"


def test_load_plugin_manifests_skips_unsafe_user_manifest(
    tmp_path: Path,
) -> None:
    """User manifests should be ignored when writable by group or others."""
    manifest_path = tmp_path / "unsafe.plugin.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "unsafe_provider",
                "kind": "search_provider",
                "module": "unsafe_provider",
                "provides": ["unsafe"],
                "summary": "Should be skipped",
            }
        ),
        encoding="utf-8",
    )
    manifest_path.chmod(0o666)

    manifests = load_plugin_manifests_from_directory(tmp_path, source_scope="user")

    assert manifests == []


def test_load_plugin_manifests_skips_untrusted_user_runtime_extension(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """User channel/provider manifests should require an install receipt by default."""
    module_path = tmp_path / "demo_provider.py"
    module_path.write_text(
        "async def demo_provider(query, web_cfg, plan=None):\n    return None\n",
        encoding="utf-8",
    )
    module_path.chmod(0o600)
    manifest_path = tmp_path / "demo.plugin.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "demo_provider",
                "kind": "search_provider",
                "module": "demo_provider",
                "provides": ["demo"],
                "summary": "Demo provider",
                "metadata": {
                    "runtime": {"handlerPath": "demo_provider:demo_provider"},
                },
            }
        ),
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    tmp_path.chmod(0o700)

    monkeypatch.setattr("nanoclaw.core.plugins.get_user_extension_dir", lambda: tmp_path)
    manifests = load_plugin_manifests_from_directory(tmp_path, source_scope="user")

    assert manifests == []


def test_load_plugin_manifests_skips_unsigned_user_extension_when_policy_requires_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Unsigned local runtime extensions should be blocked when signed bundles are required."""
    module_path = tmp_path / "demo_provider.py"
    module_path.write_text(
        "async def demo_provider(query, web_cfg, plan=None):\n    return None\n",
        encoding="utf-8",
    )
    module_path.chmod(0o600)
    manifest_path = tmp_path / "demo.plugin.json"
    manifest_path.write_text(
        json.dumps(
            {
                "name": "demo_provider",
                "kind": "search_provider",
                "module": "demo_provider",
                "provides": ["demo"],
                "summary": "Demo provider",
                "metadata": {
                    "runtime": {"handlerPath": "demo_provider:demo_provider"},
                    "security": {
                        "permissions": ["outbound_http"],
                        "sandboxPolicy": "inherits_core_boundary",
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    manifest_path.chmod(0o600)
    tmp_path.chmod(0o700)

    monkeypatch.setattr("nanoclaw.core.plugins.get_user_extension_dir", lambda: tmp_path)
    monkeypatch.setattr(
        "nanoclaw.core.config.get_config",
        lambda: SimpleNamespace(
            extensions=SimpleNamespace(
                require_install_receipt=False,
                require_signed_bundles=True,
                max_risk_level="medium",
            )
        ),
    )

    manifests = load_plugin_manifests_from_directory(tmp_path, source_scope="user")

    assert manifests == []
