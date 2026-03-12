"""Channel contract tests."""

from __future__ import annotations

from types import SimpleNamespace

from nanoclaw.channels.contract import build_channel_contract, resolve_channel_route
from nanoclaw.channels.registry import reset_channel_runtime_registry
from nanoclaw.core.extension_installer import install_extension_manifest
from nanoclaw.core.plugins import reset_plugin_registry


def setup_function() -> None:
    """Reset manifest-backed channel registries before each test."""
    reset_plugin_registry()
    reset_channel_runtime_registry()


def teardown_function() -> None:
    """Reset manifest-backed channel registries after each test."""
    reset_plugin_registry()
    reset_channel_runtime_registry()


def test_build_channel_contract_uses_config_and_runtime_overlay() -> None:
    """Contract should merge config state with gateway runtime lifecycle."""
    config = SimpleNamespace(
        channels=SimpleNamespace(
            telegram=SimpleNamespace(
                enabled=True,
                token="bot-token",
                allow_from=["1", "2"],
            ),
            feishu=SimpleNamespace(
                enabled=True,
                app_id="cli_xxx",
                app_secret="",
                allow_from=[],
                default_chat_id="",
            ),
        )
    )
    gateway = SimpleNamespace(
        get_channel_runtime_snapshot=lambda: {
            "telegram": {
                "status": "running",
                "detail": "polling runtime active",
                "last_error": "",
                "last_transition_at": 123,
            },
            "feishu": {
                "status": "failed",
                "detail": "webhook bind failed",
                "last_error": "address already in use",
                "last_transition_at": 124,
            },
        },
        get_channel_diagnostics_snapshot=lambda: {
            "telegram": {
                "incoming_total": 2,
                "incoming_successes": 1,
                "incoming_failures": 1,
                "outgoing_total": 1,
                "outgoing_successes": 1,
                "outgoing_failures": 0,
                "targeted_outgoing_total": 0,
                "targeted_outgoing_successes": 0,
                "targeted_outgoing_failures": 0,
                "last_success_at": 150,
                "last_failure_at": 200,
                "last_failure_kind": "incoming",
                "last_failure_error": "agent timeout",
                "last_runtime_status": "running",
                "last_runtime_transition_at": 123,
            },
            "feishu": {
                "incoming_total": 0,
                "incoming_successes": 0,
                "incoming_failures": 0,
                "outgoing_total": 0,
                "outgoing_successes": 0,
                "outgoing_failures": 0,
                "targeted_outgoing_total": 0,
                "targeted_outgoing_successes": 0,
                "targeted_outgoing_failures": 0,
                "last_success_at": 0,
                "last_failure_at": 124,
                "last_failure_kind": "runtime",
                "last_failure_error": "address already in use",
                "last_runtime_status": "failed",
                "last_runtime_transition_at": 124,
            },
        },
        get_channel_orchestration_snapshot=lambda: {
            "telegram": {
                "channel_name": "telegram",
                "desired_state": "running",
                "desired_reason": "config default",
                "desired_updated_at": 123,
                "actual_status": "running",
                "actual_detail": "polling runtime active",
                "reconcile_status": "reconciled",
                "reconcile_detail": "desired `running` satisfied",
                "drift_status": "in_sync",
                "drift_since": 0,
                "drift_count": 0,
                "last_reconciled_at": 123,
                "last_action": "startup",
                "last_action_at": 123,
            },
            "feishu": {
                "channel_name": "feishu",
                "desired_state": "running",
                "desired_reason": "operator start request",
                "desired_updated_at": 124,
                "actual_status": "failed",
                "actual_detail": "webhook bind failed",
                "reconcile_status": "drifted",
                "reconcile_detail": "desired `running` differs from actual `failed`",
                "drift_status": "drifted",
                "drift_since": 124,
                "drift_count": 1,
                "last_reconciled_at": 124,
                "last_action": "recover",
                "last_action_at": 124,
            },
        },
    )

    contract = build_channel_contract(config, gateway)

    assert contract["contract_version"] == "r2-f.v0"
    assert contract["summary"]["enabled_count"] == 2
    assert contract["summary"]["running_count"] == 1
    assert contract["summary"]["failed_count"] == 1
    assert contract["summary"]["allowlist_auth_count"] == 1
    assert contract["summary"]["proactive_ready_count"] == 1
    assert contract["summary"]["diagnostic_attention_count"] == 1
    assert contract["summary"]["desired_running_count"] == 2
    assert contract["summary"]["drifted_count"] == 1
    assert contract["channels"]["console"]["status"] == "stopped"
    assert contract["channels"]["console"]["supports_operator_control"] is False
    assert contract["channels"]["console"]["auth_mode"] == "local"
    assert contract["channels"]["console"]["routing_ready"] is False
    assert contract["channels"]["telegram"]["status"] == "running"
    assert contract["channels"]["telegram"]["operator_actions"] == ["restart", "stop"]
    assert contract["channels"]["telegram"]["auth_mode"] == "allowlist"
    assert contract["channels"]["telegram"]["auth_detail"] == "2 sender(s) allowed"
    assert contract["channels"]["telegram"]["allowlist_count"] == 2
    assert contract["channels"]["telegram"]["routing_ready"] is True
    assert contract["channels"]["telegram"]["diagnostic_health"] == "attention"
    assert contract["channels"]["telegram"]["diagnostic_summary"] == "incoming failed: agent timeout"
    assert contract["channels"]["telegram"]["diagnostics"]["incoming_failures"] == 1
    assert contract["channels"]["telegram"]["desired_state"] == "running"
    assert contract["channels"]["telegram"]["drift_status"] == "in_sync"
    assert contract["channels"]["telegram"]["reconcile_status"] == "reconciled"
    assert contract["channels"]["telegram"]["route_roles"] == [
        "default_proactive",
        "heartbeat",
        "runtime_alert",
    ]
    assert contract["channels"]["feishu"]["status"] == "failed"
    assert contract["channels"]["feishu"]["operator_actions"] == ["recover", "stop", "reconcile"]
    assert contract["channels"]["feishu"]["configured"] is False
    assert contract["channels"]["feishu"]["routing_ready"] is False
    assert contract["channels"]["feishu"]["last_error"] == "address already in use"
    assert contract["channels"]["feishu"]["diagnostic_health"] == "failed"
    assert contract["channels"]["feishu"]["drift_status"] == "drifted"
    assert contract["orchestration"]["policy_version"] == "r2-f.v0"
    assert contract["orchestration"]["drifted_count"] == 1
    assert contract["routing_policy"]["heartbeat"]["selected_channel"] == "telegram"
    assert contract["routing_policy"]["runtime_alert"]["selected_channel"] == "telegram"


def test_build_channel_contract_falls_back_to_gateway_channels() -> None:
    """Contract should infer running state from a gateway channel map when needed."""
    config = SimpleNamespace()
    gateway = SimpleNamespace(channels={"feishu": object()})

    contract = build_channel_contract(config, gateway)

    assert contract["channels"]["telegram"]["status"] == "disabled"
    assert contract["channels"]["feishu"]["status"] == "running"
    assert contract["channels"]["feishu"]["detail"] == "active in gateway runtime"
    assert contract["channels"]["feishu"]["operator_actions"] == ["restart", "stop"]
    assert contract["channels"]["telegram"]["operator_actions"] == []
    assert contract["routing_policy"]["default_proactive"]["selected_channel"] == ""
    assert contract["routing_policy"]["default_proactive"]["status"] == "unresolved"


def test_build_channel_contract_includes_console_runtime_channel() -> None:
    """Contract should surface console as a runtime-only first-class channel."""
    config = SimpleNamespace()
    gateway = SimpleNamespace(
        channels={"console": object()},
        get_channel_runtime_snapshot=lambda: {
            "console": {
                "status": "running",
                "detail": "active in gateway runtime",
                "last_error": "",
                "last_transition_at": 321,
            }
        },
    )

    contract = build_channel_contract(config, gateway)

    assert contract["channels"]["console"]["status"] == "running"
    assert contract["channels"]["console"]["delivery_mode"] == "interactive"
    assert contract["channels"]["console"]["supports_operator_control"] is False
    assert contract["channels"]["console"]["operator_actions"] == []
    assert contract["channels"]["console"]["routing_ready"] is True
    assert contract["routing_policy"]["default_proactive"]["selected_channel"] == "console"
    assert contract["routing_policy"]["default_proactive"]["status"] == "ready"


def test_build_channel_contract_includes_user_installed_manifest_channel(
    tmp_path,
    monkeypatch,
) -> None:
    """Contract should surface a manifest-backed custom channel using extension config."""
    builtin_skills = tmp_path / "builtin_skills"
    user_skills = tmp_path / "user_skills"
    user_extensions = tmp_path / "user_extensions"
    for directory in (builtin_skills, user_skills, user_extensions):
        directory.mkdir()
        directory.chmod(0o700)

    (user_extensions / "demo_channel.py").write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "",
                "class DemoChannel:",
                "    def __init__(self, config, gateway):",
                "        self.config = config",
                "        self.gateway = gateway",
            ]
        ),
        encoding="utf-8",
    )
    (user_extensions / "demo_channel.py").chmod(0o600)
    (user_extensions / "demo_channel.plugin.json").write_text(
        (
            "{"
            '"name":"demo_channel",'
            '"kind":"channel",'
            '"module":"demo_channel",'
            '"provides":["demo"],'
            '"summary":"Demo channel",'
            '"metadata":{'
            '"contract":{"label":"Demo","deliveryMode":"custom","managed":true,'
            '"supportsIncoming":true,"supportsProactive":true,'
            '"supportsTargetedProactive":false,"supportsConfirmation":false},'
            '"runtime":{"configName":"demo","factoryPath":"demo_channel:DemoChannel",'
            '"requiredFields":["token"],"missingReason":"token missing",'
            '"readyReason":"token configured"},'
            '"security":{"permissions":["incoming_messages","proactive_delivery"],'
            '"sandboxPolicy":"inherits_core_boundary"},'
            '"routing":{"targetMode":"broadcast_allowlist",'
            '"targetMissingReason":"no proactive recipients in allowFrom",'
            '"targetReadyDetail":"broadcast to {count} demo recipient(s)",'
            '"priorities":{"default_proactive":5}}'
            "},"
            '"enabled":true'
            "}"
        ),
        encoding="utf-8",
    )
    (user_extensions / "demo_channel.plugin.json").chmod(0o600)

    monkeypatch.setattr("nanoclaw.core.plugins.get_user_plugin_dir", lambda: user_skills)
    monkeypatch.setattr(
        "nanoclaw.core.plugins.get_user_extension_dir",
        lambda: user_extensions,
    )
    install_extension_manifest(
        user_extensions / "demo_channel.plugin.json",
        destination_dir=user_extensions,
        overwrite=True,
    )
    reset_plugin_registry()
    reset_channel_runtime_registry()

    config = SimpleNamespace(
        channels=SimpleNamespace(
            telegram=SimpleNamespace(enabled=False, token="", allow_from=[]),
            feishu=SimpleNamespace(
                enabled=False,
                app_id="",
                app_secret="",
                allow_from=[],
                default_chat_id="",
            ),
            extensions={
                "demo": {
                    "enabled": True,
                    "token": "demo-token",
                    "allowFrom": ["demo-user"],
                }
            },
        )
    )
    gateway = SimpleNamespace(
        get_channel_runtime_snapshot=lambda: {
            "demo": {
                "status": "running",
                "detail": "demo runtime active",
                "last_error": "",
                "last_transition_at": 111,
            }
        }
    )

    contract = build_channel_contract(config, gateway)

    assert contract["channels"]["demo"]["label"] == "Demo"
    assert contract["channels"]["demo"]["delivery_mode"] == "custom"
    assert contract["channels"]["demo"]["configured"] is True
    assert contract["channels"]["demo"]["routing_ready"] is True
    assert contract["channels"]["demo"]["auth_mode"] == "allowlist"
    assert contract["routing_policy"]["default_proactive"]["selected_channel"] == "demo"
    reset_plugin_registry()
    reset_channel_runtime_registry()


def test_resolve_channel_route_prefers_runtime_ready_channel() -> None:
    """Route resolver should pick one runtime-ready fallback when preferred is unavailable."""
    config = SimpleNamespace(
        channels=SimpleNamespace(
            telegram=SimpleNamespace(
                enabled=True,
                token="bot-token",
                allow_from=["42"],
            ),
            feishu=SimpleNamespace(
                enabled=True,
                app_id="cli_xxx",
                app_secret="secret",
                allow_from=[],
                default_chat_id="oc_chat_1",
            ),
        )
    )
    gateway = SimpleNamespace(
        channels={"feishu": object()},
        get_channel_runtime_snapshot=lambda: {
            "telegram": {
                "status": "failed",
                "detail": "polling stopped",
                "last_error": "network issue",
                "last_transition_at": 100,
            },
            "feishu": {
                "status": "running",
                "detail": "webhook runtime active",
                "last_error": "",
                "last_transition_at": 101,
            },
        },
    )

    route = resolve_channel_route(
        config,
        gateway,
        purpose="heartbeat",
        preferred_channel="telegram",
    )

    assert route["selected_channel"] == "feishu"
    assert route["status"] == "ready"
    assert route["selection_kind"] == "fallback"
    assert route["reason"] == "preferred unavailable; fell back from telegram"


def test_resolve_channel_route_returns_configured_only_when_runtime_is_inactive() -> None:
    """Route resolver should expose configured-only routes when no runtime is attached."""
    config = SimpleNamespace(
        channels=SimpleNamespace(
            telegram=SimpleNamespace(
                enabled=True,
                token="bot-token",
                allow_from=["42"],
            ),
            feishu=SimpleNamespace(
                enabled=True,
                app_id="cli_xxx",
                app_secret="secret",
                allow_from=[],
                default_chat_id="oc_chat_1",
            ),
        )
    )

    route = resolve_channel_route(config, None, purpose="default_proactive")

    assert route["selected_channel"] == "telegram"
    assert route["status"] == "configured_only"
    assert route["selection_kind"] == "configured_only"
    assert route["reason"] == "telegram configured but runtime inactive"
