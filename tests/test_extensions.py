"""Extension manifest catalog tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from click.testing import CliRunner

from nanoclaw.cli.main import cli
from nanoclaw.core.config import ExtensionPolicyConfig
from nanoclaw.core.plugins import reset_plugin_registry


def _write_manifest(path: Path, payload: dict[str, object]) -> None:
    """Write one JSON manifest payload."""
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_extensions_command_lists_manifest_backed_extensions(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """CLI should expose skills, channels, and search providers from manifests."""
    builtin_skills = tmp_path / "builtin_skills"
    builtin_channels = tmp_path / "builtin_channels"
    builtin_tools = tmp_path / "builtin_tools"
    user_skills = tmp_path / "user_skills"
    user_extensions = tmp_path / "user_extensions"
    for directory in (
        builtin_skills,
        builtin_channels,
        builtin_tools,
        user_skills,
        user_extensions,
    ):
        directory.mkdir()

    _write_manifest(
        builtin_skills / "weather.plugin.json",
        {
            "name": "get_weather",
            "kind": "skill",
            "module": "weather",
            "toolNames": ["get_weather"],
            "summary": "Weather skill",
        },
    )
    _write_manifest(
        builtin_channels / "console.plugin.json",
        {
            "name": "console_channel",
            "kind": "channel",
            "module": "console",
            "provides": ["console"],
            "summary": "Console channel",
            "metadata": {
                "contract": {"deliveryMode": "interactive", "managed": False},
                "routing": {"priorities": {"default_proactive": 3}},
            },
        },
    )
    _write_manifest(
        builtin_tools / "rss.plugin.json",
        {
            "name": "rss_provider",
            "kind": "search_provider",
            "module": "search_providers",
            "provides": ["rss"],
            "summary": "RSS provider",
            "metadata": {
                "runtime": {
                    "handlerPath": "search_providers:rss",
                    "aliases": ["rss-news"],
                    "autoPriority": 50,
                }
            },
        },
    )

    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_plugin_dir",
        lambda: builtin_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_channel_plugin_dir",
        lambda: builtin_channels,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_provider_plugin_dir",
        lambda: builtin_tools,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_plugin_dir",
        lambda: user_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_extension_dir",
        lambda: user_extensions,
    )
    reset_plugin_registry()

    runner = CliRunner()
    result = runner.invoke(cli, ["extensions", "--format", "json"])

    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["summary"]["total"] == 3
    assert data["summary"]["skills"] == 1
    assert data["summary"]["channels"] == 1
    assert data["summary"]["search_providers"] == 1
    assert data["skills"][0]["name"] == "get_weather"
    assert data["channels"][0]["name"] == "console"
    assert data["channels"][0]["metadata"]["contract"]["deliveryMode"] == "interactive"
    assert data["search_providers"][0]["name"] == "rss"
    assert data["search_providers"][0]["metadata"]["runtime"]["autoPriority"] == 50
    reset_plugin_registry()


def test_extension_install_and_verify_commands(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """CLI install/verify flow should create a trusted local runtime extension."""
    source_dir = tmp_path / "source"
    builtin_skills = tmp_path / "builtin_skills"
    builtin_channels = tmp_path / "builtin_channels"
    builtin_tools = tmp_path / "builtin_tools"
    user_skills = tmp_path / "user_skills"
    user_extensions = tmp_path / "user_extensions"
    for directory in (
        source_dir,
        builtin_skills,
        builtin_channels,
        builtin_tools,
        user_skills,
        user_extensions,
    ):
        directory.mkdir()
        directory.chmod(0o700)

    (source_dir / "demo_provider.py").write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "from nanoclaw.tools.search_providers import SearchProviderResult",
                "",
                "async def demo_provider(query, web_cfg, plan=None):",
                "    return SearchProviderResult(text='ok', ok=True, provider='demo')",
            ]
        ),
        encoding="utf-8",
    )
    (source_dir / "demo_provider.plugin.json").write_text(
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
                "riskLevel": "medium",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_plugin_dir",
        lambda: builtin_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_channel_plugin_dir",
        lambda: builtin_channels,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_provider_plugin_dir",
        lambda: builtin_tools,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_plugin_dir",
        lambda: user_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_extension_dir",
        lambda: user_extensions,
    )
    fake_config = SimpleNamespace(
        extensions=SimpleNamespace(
            require_install_receipt=True,
            require_signed_bundles=False,
            max_risk_level="medium",
        )
    )
    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: fake_config)
    reset_plugin_registry()

    runner = CliRunner()
    install_result = runner.invoke(
        cli,
        ["extension-install", str(source_dir / "demo_provider.plugin.json")],
    )
    assert install_result.exit_code == 0
    assert "Installed search_provider `demo`" in install_result.output

    verify_result = runner.invoke(cli, ["extension-verify", "--format", "json"])
    assert verify_result.exit_code == 0
    verify_data = json.loads(verify_result.output)
    assert verify_data[0]["status"] == "trusted"
    assert verify_data[0]["permissions"] == ["outbound_http"]

    catalog_result = runner.invoke(cli, ["extensions", "--kind", "search_provider", "--format", "json"])
    assert catalog_result.exit_code == 0
    catalog_data = json.loads(catalog_result.output)
    assert catalog_data["search_providers"][0]["trust_status"] == "trusted"
    assert catalog_data["search_providers"][0]["sandbox_policy"] == "inherits_core_boundary"


def test_extension_pack_and_signed_bundle_install(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """CLI should pack and install one signed local extension bundle."""
    source_dir = tmp_path / "source"
    builtin_skills = tmp_path / "builtin_skills"
    builtin_channels = tmp_path / "builtin_channels"
    builtin_tools = tmp_path / "builtin_tools"
    user_skills = tmp_path / "user_skills"
    user_extensions = tmp_path / "user_extensions"
    for directory in (
        source_dir,
        builtin_skills,
        builtin_channels,
        builtin_tools,
        user_skills,
        user_extensions,
    ):
        directory.mkdir()
        directory.chmod(0o700)

    secret_file = tmp_path / "publisher.secret"
    secret_file.write_text("super-secret", encoding="utf-8")
    (source_dir / "demo_provider.py").write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "from nanoclaw.tools.search_providers import SearchProviderResult",
                "",
                "async def demo_provider(query, web_cfg, plan=None):",
                "    return SearchProviderResult(text='ok', ok=True, provider='demo')",
            ]
        ),
        encoding="utf-8",
    )
    (source_dir / "demo_provider.plugin.json").write_text(
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
                "riskLevel": "medium",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_plugin_dir",
        lambda: builtin_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_channel_plugin_dir",
        lambda: builtin_channels,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_provider_plugin_dir",
        lambda: builtin_tools,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_plugin_dir",
        lambda: user_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_extension_dir",
        lambda: user_extensions,
    )
    fake_config = SimpleNamespace(
        extensions=SimpleNamespace(
            require_install_receipt=True,
            require_signed_bundles=True,
            max_risk_level="medium",
            trusted_publishers={"acme": "super-secret"},
        )
    )
    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: fake_config)
    reset_plugin_registry()

    bundle_path = tmp_path / "demo_provider.ncext.zip"
    runner = CliRunner()
    pack_result = runner.invoke(
        cli,
        [
            "extension-pack",
            str(source_dir / "demo_provider.plugin.json"),
            "--output",
            str(bundle_path),
            "--publisher",
            "acme",
            "--secret-file",
            str(secret_file),
        ],
    )
    assert pack_result.exit_code == 0
    assert "Signed: true" in pack_result.output

    install_result = runner.invoke(cli, ["extension-install", str(bundle_path)])
    assert install_result.exit_code == 0
    assert "publisher=acme" in install_result.output
    assert "signatureVerified=true" in install_result.output

    verify_result = runner.invoke(cli, ["extension-verify", "--format", "json"])
    assert verify_result.exit_code == 0
    verify_data = json.loads(verify_result.output)
    assert verify_data[0]["distribution_type"] == "bundle"
    assert verify_data[0]["publisher"] == "acme"
    assert verify_data[0]["signature_verified"] is True


def test_extension_pack_and_install_supports_publisher_key_ids(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Signed bundle install should preserve the bundle signing key ID."""
    source_dir = tmp_path / "source"
    builtin_skills = tmp_path / "builtin_skills"
    builtin_channels = tmp_path / "builtin_channels"
    builtin_tools = tmp_path / "builtin_tools"
    user_skills = tmp_path / "user_skills"
    user_extensions = tmp_path / "user_extensions"
    for directory in (
        source_dir,
        builtin_skills,
        builtin_channels,
        builtin_tools,
        user_skills,
        user_extensions,
    ):
        directory.mkdir()
        directory.chmod(0o700)

    secret_file = tmp_path / "publisher.secret"
    secret_file.write_text("rotated-secret", encoding="utf-8")
    (source_dir / "demo_provider.py").write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "from nanoclaw.tools.search_providers import SearchProviderResult",
                "",
                "async def demo_provider(query, web_cfg, plan=None):",
                "    return SearchProviderResult(text='ok', ok=True, provider='demo')",
            ]
        ),
        encoding="utf-8",
    )
    (source_dir / "demo_provider.plugin.json").write_text(
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
                "riskLevel": "medium",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_plugin_dir",
        lambda: builtin_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_channel_plugin_dir",
        lambda: builtin_channels,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_provider_plugin_dir",
        lambda: builtin_tools,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_plugin_dir",
        lambda: user_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_extension_dir",
        lambda: user_extensions,
    )
    fake_config = SimpleNamespace(
        extensions=ExtensionPolicyConfig(
            **{
                "requireInstallReceipt": True,
                "requireSignedBundles": True,
                "maxRiskLevel": "medium",
                "trustedPublishers": {
                    "acme": {
                        "activeKeyId": "2026-q1",
                        "keys": {
                            "2025-q4": "older-secret",
                            "2026-q1": "rotated-secret",
                        },
                        "revokedKeyIds": [],
                    }
                },
            }
        )
    )
    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: fake_config)
    reset_plugin_registry()

    bundle_path = tmp_path / "demo_provider.ncext.zip"
    runner = CliRunner()
    pack_result = runner.invoke(
        cli,
        [
            "extension-pack",
            str(source_dir / "demo_provider.plugin.json"),
            "--output",
            str(bundle_path),
            "--publisher",
            "acme",
            "--key-id",
            "2026-q1",
            "--secret-file",
            str(secret_file),
        ],
    )
    assert pack_result.exit_code == 0
    assert "keyId=2026-q1" in pack_result.output

    install_result = runner.invoke(cli, ["extension-install", str(bundle_path)])
    assert install_result.exit_code == 0
    assert "keyId=2026-q1" in install_result.output

    verify_result = runner.invoke(cli, ["extension-verify", "--format", "json"])
    assert verify_result.exit_code == 0
    verify_data = json.loads(verify_result.output)
    assert verify_data[0]["status"] == "trusted"
    assert verify_data[0]["key_id"] == "2026-q1"


def test_extension_install_rejects_revoked_publisher(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Signed bundle install should reject publishers revoked in policy."""
    source_dir = tmp_path / "source"
    builtin_skills = tmp_path / "builtin_skills"
    builtin_channels = tmp_path / "builtin_channels"
    builtin_tools = tmp_path / "builtin_tools"
    user_skills = tmp_path / "user_skills"
    user_extensions = tmp_path / "user_extensions"
    for directory in (
        source_dir,
        builtin_skills,
        builtin_channels,
        builtin_tools,
        user_skills,
        user_extensions,
    ):
        directory.mkdir()
        directory.chmod(0o700)

    secret_file = tmp_path / "publisher.secret"
    secret_file.write_text("super-secret", encoding="utf-8")
    (source_dir / "demo_provider.py").write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "from nanoclaw.tools.search_providers import SearchProviderResult",
                "",
                "async def demo_provider(query, web_cfg, plan=None):",
                "    return SearchProviderResult(text='ok', ok=True, provider='demo')",
            ]
        ),
        encoding="utf-8",
    )
    (source_dir / "demo_provider.plugin.json").write_text(
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
                "riskLevel": "medium",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_plugin_dir",
        lambda: builtin_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_channel_plugin_dir",
        lambda: builtin_channels,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_provider_plugin_dir",
        lambda: builtin_tools,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_plugin_dir",
        lambda: user_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_extension_dir",
        lambda: user_extensions,
    )
    reset_plugin_registry()

    bundle_path = tmp_path / "demo_provider.ncext.zip"
    runner = CliRunner()
    pack_result = runner.invoke(
        cli,
        [
            "extension-pack",
            str(source_dir / "demo_provider.plugin.json"),
            "--output",
            str(bundle_path),
            "--publisher",
            "acme",
            "--secret-file",
            str(secret_file),
        ],
    )
    assert pack_result.exit_code == 0

    fake_config = SimpleNamespace(
        extensions=ExtensionPolicyConfig(
            **{
                "requireInstallReceipt": True,
                "requireSignedBundles": True,
                "maxRiskLevel": "medium",
                "trustedPublishers": {"acme": "super-secret"},
                "revokedPublishers": ["acme"],
            }
        )
    )
    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: fake_config)

    install_result = runner.invoke(cli, ["extension-install", str(bundle_path)])

    assert install_result.exit_code != 0
    assert install_result.exception is not None
    assert "revoked" in str(install_result.exception)


def test_extension_verify_reports_revoked_key_after_rotation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Verify should keep old keys trusted until policy explicitly revokes them."""
    source_dir = tmp_path / "source"
    builtin_skills = tmp_path / "builtin_skills"
    builtin_channels = tmp_path / "builtin_channels"
    builtin_tools = tmp_path / "builtin_tools"
    user_skills = tmp_path / "user_skills"
    user_extensions = tmp_path / "user_extensions"
    for directory in (
        source_dir,
        builtin_skills,
        builtin_channels,
        builtin_tools,
        user_skills,
        user_extensions,
    ):
        directory.mkdir()
        directory.chmod(0o700)

    old_secret_file = tmp_path / "old.secret"
    old_secret_file.write_text("old-secret", encoding="utf-8")
    (source_dir / "demo_provider.py").write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "from nanoclaw.tools.search_providers import SearchProviderResult",
                "",
                "async def demo_provider(query, web_cfg, plan=None):",
                "    return SearchProviderResult(text='ok', ok=True, provider='demo')",
            ]
        ),
        encoding="utf-8",
    )
    (source_dir / "demo_provider.plugin.json").write_text(
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
                "riskLevel": "medium",
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_plugin_dir",
        lambda: builtin_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_channel_plugin_dir",
        lambda: builtin_channels,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_provider_plugin_dir",
        lambda: builtin_tools,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_plugin_dir",
        lambda: user_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_extension_dir",
        lambda: user_extensions,
    )
    reset_plugin_registry()

    bundle_path = tmp_path / "demo_provider.ncext.zip"
    runner = CliRunner()
    pack_result = runner.invoke(
        cli,
        [
            "extension-pack",
            str(source_dir / "demo_provider.plugin.json"),
            "--output",
            str(bundle_path),
            "--publisher",
            "acme",
            "--key-id",
            "2025-q4",
            "--secret-file",
            str(old_secret_file),
        ],
    )
    assert pack_result.exit_code == 0

    initial_policy = SimpleNamespace(
        extensions=ExtensionPolicyConfig(
            **{
                "requireInstallReceipt": True,
                "requireSignedBundles": True,
                "maxRiskLevel": "medium",
                "trustedPublishers": {
                    "acme": {
                        "activeKeyId": "2025-q4",
                        "keys": {
                            "2025-q4": "old-secret",
                            "2026-q1": "new-secret",
                        },
                        "revokedKeyIds": [],
                    }
                },
            }
        )
    )
    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: initial_policy)

    install_result = runner.invoke(cli, ["extension-install", str(bundle_path)])
    assert install_result.exit_code == 0

    rotated_policy = SimpleNamespace(
        extensions=ExtensionPolicyConfig(
            **{
                "requireInstallReceipt": True,
                "requireSignedBundles": True,
                "maxRiskLevel": "medium",
                "trustedPublishers": {
                    "acme": {
                        "activeKeyId": "2026-q1",
                        "keys": {
                            "2025-q4": "old-secret",
                            "2026-q1": "new-secret",
                        },
                        "revokedKeyIds": [],
                    }
                },
            }
        )
    )
    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: rotated_policy)

    verify_before_revoke = runner.invoke(cli, ["extension-verify", "--format", "json"])
    assert verify_before_revoke.exit_code == 0
    before_data = json.loads(verify_before_revoke.output)
    assert before_data[0]["status"] == "trusted"
    assert before_data[0]["key_id"] == "2025-q4"

    revoked_policy = SimpleNamespace(
        extensions=ExtensionPolicyConfig(
            **{
                "requireInstallReceipt": True,
                "requireSignedBundles": True,
                "maxRiskLevel": "medium",
                "trustedPublishers": {
                    "acme": {
                        "activeKeyId": "2026-q1",
                        "keys": {
                            "2025-q4": "old-secret",
                            "2026-q1": "new-secret",
                        },
                        "revokedKeyIds": ["2025-q4"],
                    }
                },
            }
        )
    )
    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: revoked_policy)

    verify_after_revoke = runner.invoke(cli, ["extension-verify", "--format", "json"])
    assert verify_after_revoke.exit_code == 1
    after_data = json.loads(verify_after_revoke.output)
    assert after_data[0]["status"] == "revoked"
    assert "revoked" in after_data[0]["reason"]
    assert after_data[0]["key_id"] == "2025-q4"


def test_extension_registry_update_flow(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """CLI should install and update signed bundles from a configured registry."""
    source_dir = tmp_path / "source"
    builtin_skills = tmp_path / "builtin_skills"
    builtin_channels = tmp_path / "builtin_channels"
    builtin_tools = tmp_path / "builtin_tools"
    user_skills = tmp_path / "user_skills"
    user_extensions = tmp_path / "user_extensions"
    for directory in (
        source_dir,
        builtin_skills,
        builtin_channels,
        builtin_tools,
        user_skills,
        user_extensions,
    ):
        directory.mkdir()
        directory.chmod(0o700)

    secret_file = tmp_path / "publisher.secret"
    secret_file.write_text("super-secret", encoding="utf-8")
    registry_path = tmp_path / "registry.json"
    bundle_v1 = tmp_path / "demo_provider_v1.ncext.zip"
    bundle_v2 = tmp_path / "demo_provider_v2.ncext.zip"

    def _write_provider(version: str) -> None:
        (source_dir / "demo_provider.py").write_text(
            "\n".join(
                [
                    "from __future__ import annotations",
                    "from nanoclaw.tools.search_providers import SearchProviderResult",
                    "",
                    "async def demo_provider(query, web_cfg, plan=None):",
                    f"    return SearchProviderResult(text='version={version}', ok=True, provider='demo')",
                ]
            ),
            encoding="utf-8",
        )
        (source_dir / "demo_provider.plugin.json").write_text(
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
                        "distribution": {"version": version},
                    },
                    "riskLevel": "medium",
                }
            ),
            encoding="utf-8",
        )

    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_plugin_dir",
        lambda: builtin_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_channel_plugin_dir",
        lambda: builtin_channels,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_builtin_provider_plugin_dir",
        lambda: builtin_tools,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_plugin_dir",
        lambda: user_skills,
    )
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_extension_dir",
        lambda: user_extensions,
    )

    _write_provider("1.0.0")
    runner = CliRunner()
    pack_v1 = runner.invoke(
        cli,
        [
            "extension-pack",
            str(source_dir / "demo_provider.plugin.json"),
            "--output",
            str(bundle_v1),
            "--publisher",
            "acme",
            "--secret-file",
            str(secret_file),
        ],
    )
    assert pack_v1.exit_code == 0
    registry_path.write_text(
        json.dumps(
            {
                "registryName": "demo",
                "extensions": [
                    {
                        "kind": "search_provider",
                        "name": "demo",
                        "version": "1.0.0",
                        "summary": "Demo provider",
                        "publisher": "acme",
                        "bundleUrl": str(bundle_v1),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    fake_config = SimpleNamespace(
        extensions=SimpleNamespace(
            require_install_receipt=True,
            require_signed_bundles=True,
            max_risk_level="medium",
            trusted_publishers={"acme": "super-secret"},
            registry_url=str(registry_path),
        )
    )
    monkeypatch.setattr("nanoclaw.core.config.get_config", lambda: fake_config)
    reset_plugin_registry()

    list_before = runner.invoke(cli, ["extension-registry", "--format", "json"])
    assert list_before.exit_code == 0
    before_data = json.loads(list_before.output)
    assert before_data["entries"][0]["status"] == "available"

    install_v1 = runner.invoke(cli, ["extension-update", "--name", "demo"])
    assert install_v1.exit_code == 0
    assert "Version: 1.0.0" in install_v1.output

    verify_v1 = runner.invoke(cli, ["extension-verify", "--format", "json"])
    assert verify_v1.exit_code == 0
    verify_v1_data = json.loads(verify_v1.output)
    assert verify_v1_data[0]["version"] == "1.0.0"
    assert verify_v1_data[0]["registry_source"] == str(registry_path)

    _write_provider("1.1.0")
    pack_v2 = runner.invoke(
        cli,
        [
            "extension-pack",
            str(source_dir / "demo_provider.plugin.json"),
            "--output",
            str(bundle_v2),
            "--publisher",
            "acme",
            "--secret-file",
            str(secret_file),
        ],
    )
    assert pack_v2.exit_code == 0
    registry_path.write_text(
        json.dumps(
            {
                "registryName": "demo",
                "extensions": [
                    {
                        "kind": "search_provider",
                        "name": "demo",
                        "version": "1.1.0",
                        "summary": "Demo provider",
                        "publisher": "acme",
                        "bundleUrl": str(bundle_v2),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    list_after = runner.invoke(cli, ["extension-registry", "--format", "json"])
    assert list_after.exit_code == 0
    after_data = json.loads(list_after.output)
    assert after_data["entries"][0]["status"] == "update_available"
    assert after_data["entries"][0]["installed_version"] == "1.0.0"

    install_v2 = runner.invoke(cli, ["extension-update", "--name", "demo"])
    assert install_v2.exit_code == 0
    assert "Version: 1.1.0" in install_v2.output

    verify_v2 = runner.invoke(cli, ["extension-verify", "--format", "json"])
    assert verify_v2.exit_code == 0
    verify_v2_data = json.loads(verify_v2.output)
    assert verify_v2_data[0]["version"] == "1.1.0"
