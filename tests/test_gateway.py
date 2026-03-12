"""Gateway channel control tests."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from nanoclaw.channels.gateway import Gateway
from nanoclaw.channels.registry import reset_channel_runtime_registry
from nanoclaw.channels.state_store import ChannelStateStore, set_channel_state_store
from nanoclaw.core.config import ExtensionPolicyConfig
from nanoclaw.core.extension_installer import install_extension_manifest
from nanoclaw.core.plugins import reset_plugin_registry


@pytest.fixture(autouse=True)
def _channel_state_store(tmp_path) -> ChannelStateStore:
    """Use one isolated desired-state store per test."""
    store = ChannelStateStore(tmp_path / "nanoclaw.db")
    set_channel_state_store(store)
    yield store
    set_channel_state_store(None)


@pytest.fixture(autouse=True)
def _reset_channel_registries() -> None:
    """Keep manifest-backed channel registries isolated between tests."""
    reset_plugin_registry()
    reset_channel_runtime_registry()
    yield
    reset_plugin_registry()
    reset_channel_runtime_registry()


@pytest.mark.asyncio
async def test_gateway_run_channel_action_restarts_managed_channel(monkeypatch) -> None:
    """Gateway should stop then start a managed channel for restart."""
    gateway = Gateway(
        SimpleNamespace(
            channels=SimpleNamespace(
                telegram=SimpleNamespace(enabled=True, token="bot-token"),
                feishu=SimpleNamespace(enabled=False, app_id="", app_secret=""),
            )
        )
    )
    calls: list[tuple[str, str, str]] = []

    async def fake_stop(name: str, *, detail: str = "") -> dict[str, object]:
        calls.append(("stop", name, detail))
        gateway._set_channel_runtime(name, "stopped", detail=detail)
        gateway.channels.pop(name, None)
        return dict(gateway.get_channel_runtime_snapshot()[name])

    async def fake_start(name: str) -> dict[str, object]:
        calls.append(("start", name, ""))
        gateway._set_channel_runtime(name, "running", detail="polling runtime active")
        gateway.channels[name] = object()
        return dict(gateway.get_channel_runtime_snapshot()[name])

    monkeypatch.setattr(gateway, "_stop_managed_channel", fake_stop)
    monkeypatch.setattr(gateway, "_start_managed_channel", fake_start)

    runtime = await gateway.run_channel_action("telegram", "restart")

    assert calls == [
        ("stop", "telegram", "stopped for operator restart"),
        ("start", "telegram", ""),
    ]
    assert runtime["actual_status"] == "running"
    assert runtime["desired_state"] == "running"
    assert runtime["drift_status"] == "in_sync"


@pytest.mark.asyncio
async def test_gateway_run_channel_action_rejects_unknown_channel() -> None:
    """Gateway should reject unknown managed channels."""
    gateway = Gateway(SimpleNamespace())

    with pytest.raises(ValueError, match="Unknown managed channel"):
        await gateway.run_channel_action("console", "restart")


@pytest.mark.asyncio
async def test_gateway_handle_incoming_records_success_diagnostics() -> None:
    """Gateway should record successful incoming message diagnostics."""
    gateway = Gateway(SimpleNamespace())

    class FakeAgent:
        async def run(self, **kwargs) -> str:
            return "ok"

    gateway._agent = FakeAgent()

    response = await gateway.handle_incoming("telegram", "42", "hello")
    diagnostics = gateway.get_channel_diagnostics_snapshot()["telegram"]

    assert response == "ok"
    assert diagnostics["incoming_total"] == 1
    assert diagnostics["incoming_successes"] == 1
    assert diagnostics["incoming_failures"] == 0
    assert diagnostics["last_incoming_session"] == "telegram:42"
    assert diagnostics["last_success_at"] > 0


@pytest.mark.asyncio
async def test_gateway_handle_incoming_records_failure_diagnostics() -> None:
    """Gateway should record failed incoming message diagnostics."""
    gateway = Gateway(SimpleNamespace())

    class FakeAgent:
        async def run(self, **kwargs) -> str:
            raise RuntimeError("agent timeout")

    gateway._agent = FakeAgent()

    response = await gateway.handle_incoming("feishu", "u-1", "hello")
    diagnostics = gateway.get_channel_diagnostics_snapshot()["feishu"]

    assert "Sorry, something went wrong: agent timeout" == response
    assert diagnostics["incoming_total"] == 1
    assert diagnostics["incoming_successes"] == 0
    assert diagnostics["incoming_failures"] == 1
    assert diagnostics["last_failure_kind"] == "incoming"
    assert diagnostics["last_failure_error"] == "agent timeout"


@pytest.mark.asyncio
async def test_gateway_manifest_registry_splits_managed_and_runtime_only_channels() -> None:
    """Gateway should derive managed/runtime-only channel sets from manifests."""
    gateway = Gateway(SimpleNamespace())

    diagnostics = gateway.get_channel_diagnostics_snapshot()
    orchestration = gateway.get_channel_orchestration_snapshot()

    assert set(diagnostics) >= {"telegram", "feishu", "console"}
    assert set(orchestration) == {"telegram", "feishu"}


@pytest.mark.asyncio
async def test_gateway_starts_user_installed_manifest_channel(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway should start a user-installed channel from a safe local extension module."""
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
                "        self.running = False",
                "",
                "    async def start(self) -> bool:",
                "        self.running = True",
                "        return True",
                "",
                "    async def stop(self) -> None:",
                "        self.running = False",
                "",
                "    async def send_proactive(self, text: str) -> None:",
                "        return None",
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
            '"readyReason":"token configured","startingDetail":"starting demo runtime",'
            '"runningDetail":"demo runtime active"},'
            '"security":{"permissions":["incoming_messages","proactive_delivery"],'
            '"sandboxPolicy":"inherits_core_boundary"},'
            '"routing":{"targetMode":"broadcast_allowlist",'
            '"targetMissingReason":"no proactive recipients in allowFrom",'
            '"targetReadyDetail":"broadcast to {count} demo recipient(s)",'
            '"priorities":{"default_proactive":50}}'
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

    gateway = Gateway(
        SimpleNamespace(
            channels=SimpleNamespace(
                telegram=SimpleNamespace(enabled=False, token=""),
                feishu=SimpleNamespace(enabled=False, app_id="", app_secret=""),
                extensions={
                    "demo": {
                        "enabled": True,
                        "token": "demo-token",
                        "allowFrom": ["cli-user"],
                    }
                },
            )
        )
    )

    runtime = await gateway._start_managed_channel("demo")

    assert runtime["status"] == "running"
    assert runtime["detail"] == "demo runtime active"
    assert "demo" in gateway.channels
    reset_plugin_registry()
    reset_channel_runtime_registry()


@pytest.mark.asyncio
async def test_gateway_runs_proactive_only_user_channel_in_subprocess(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gateway should isolate proactive-only user channels behind a subprocess proxy."""
    builtin_skills = tmp_path / "builtin_skills"
    user_skills = tmp_path / "user_skills"
    user_extensions = tmp_path / "user_extensions"
    output_path = tmp_path / "demo_channel.log"
    for directory in (builtin_skills, user_skills, user_extensions):
        directory.mkdir()
        directory.chmod(0o700)

    (user_extensions / "demo_channel.py").write_text(
        "\n".join(
            [
                "from __future__ import annotations",
                "import os",
                "from pathlib import Path",
                "",
                "class DemoChannel:",
                "    def __init__(self, config, gateway):",
                "        self.config = config",
                "        self.gateway = gateway",
                "",
                "    async def start(self) -> bool:",
                "        Path(self.config.outputPath).write_text(",
                "            f'start:{os.getpid()}\\n',",
                "            encoding='utf-8',",
                "        )",
                "        return True",
                "",
                "    async def stop(self) -> None:",
                "        with Path(self.config.outputPath).open('a', encoding='utf-8') as handle:",
                "            handle.write(f'stop:{os.getpid()}\\n')",
                "",
                "    async def send_proactive(self, text: str) -> None:",
                "        with Path(self.config.outputPath).open('a', encoding='utf-8') as handle:",
                "            handle.write(f'send:{os.getpid()}:{text}\\n')",
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
            '"summary":"Demo proactive-only channel",'
            '"metadata":{'
            '"contract":{"label":"Demo","deliveryMode":"custom","managed":true,'
            '"supportsIncoming":false,"supportsProactive":true,'
            '"supportsTargetedProactive":false,"supportsConfirmation":false},'
            '"runtime":{"configName":"demo","factoryPath":"demo_channel:DemoChannel",'
            '"requiredFields":["outputPath"],"missingReason":"outputPath missing",'
            '"readyReason":"outputPath configured","startingDetail":"starting demo runtime",'
            '"runningDetail":"demo runtime active"},'
            '"security":{"permissions":["proactive_delivery"],'
            '"sandboxPolicy":"inherits_core_boundary"},'
            '"routing":{"targetMode":"broadcast_allowlist",'
            '"targetMissingReason":"no proactive recipients in allowFrom",'
            '"targetReadyDetail":"broadcast ready",'
            '"priorities":{"default_proactive":40}}'
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
    monkeypatch.setattr(
        "nanoclaw.core.config.get_config",
        lambda: SimpleNamespace(
            extensions=ExtensionPolicyConfig(
                **{
                    "requireInstallReceipt": True,
                    "requireSignedBundles": False,
                    "maxRiskLevel": "medium",
                    "runtimeIsolationMode": "subprocess",
                    "runtimeIsolatedKinds": ["channel"],
                }
            )
        ),
    )
    install_extension_manifest(
        user_extensions / "demo_channel.plugin.json",
        destination_dir=user_extensions,
        overwrite=True,
    )
    reset_plugin_registry()
    reset_channel_runtime_registry()

    gateway = Gateway(
        SimpleNamespace(
            channels=SimpleNamespace(
                telegram=SimpleNamespace(enabled=False, token=""),
                feishu=SimpleNamespace(enabled=False, app_id="", app_secret=""),
                extensions={
                    "demo": {
                        "enabled": True,
                        "outputPath": str(output_path),
                        "allowFrom": ["cli-user"],
                    }
                },
            )
        )
    )

    runtime = await gateway._start_managed_channel("demo")
    await gateway.send_proactive("hello", channel="demo")
    await gateway._stop_managed_channel("demo")

    log_lines = output_path.read_text(encoding="utf-8").splitlines()

    assert runtime["status"] == "running"
    assert runtime["detail"] == "demo runtime active"
    assert "demo" not in gateway.channels
    assert [line.split(":", 1)[0] for line in log_lines] == ["start", "send", "stop"]
    assert log_lines[1].endswith(":hello")
    assert all(f":{os.getpid()}" not in line for line in log_lines)


@pytest.mark.asyncio
async def test_gateway_send_proactive_targeted_records_delivery_diagnostics() -> None:
    """Gateway should record targeted proactive delivery diagnostics."""
    gateway = Gateway(SimpleNamespace())

    class FakeTargetedChannel:
        async def send_proactive_to(self, target_id: str, text: str) -> bool:
            assert target_id == "oc_chat_1"
            assert text == "hello"
            return True

    gateway.channels["feishu"] = FakeTargetedChannel()

    sent = await gateway.send_proactive_targeted(
        channel="feishu",
        text="hello",
        target_id="oc_chat_1",
    )
    diagnostics = gateway.get_channel_diagnostics_snapshot()["feishu"]

    assert sent is True
    assert diagnostics["targeted_outgoing_total"] == 1
    assert diagnostics["targeted_outgoing_successes"] == 1
    assert diagnostics["targeted_outgoing_failures"] == 0
    assert diagnostics["last_outgoing_kind"] == "targeted_proactive"
    assert diagnostics["last_outgoing_target"] == "oc_chat_1"


@pytest.mark.asyncio
async def test_gateway_reconcile_start_persists_desired_state(monkeypatch) -> None:
    """Gateway should persist desired state and converge running channels."""
    config = SimpleNamespace(
        channels=SimpleNamespace(
            telegram=SimpleNamespace(enabled=True, token="bot-token"),
            feishu=SimpleNamespace(enabled=False, app_id="", app_secret=""),
        )
    )
    gateway = Gateway(config)

    async def fake_start(name: str) -> dict[str, object]:
        gateway._set_channel_runtime(name, "running", detail="polling runtime active")
        gateway.channels[name] = object()
        return dict(gateway.get_channel_runtime_snapshot()[name])

    monkeypatch.setattr(gateway, "_start_managed_channel", fake_start)

    await gateway._load_channel_orchestration_state()
    result = await gateway.run_channel_action("telegram", "start")
    snapshot = gateway.get_channel_orchestration_snapshot()["telegram"]

    assert result["desired_state"] == "running"
    assert result["actual_status"] == "running"
    assert result["reconcile_status"] == "reconciled"
    assert snapshot["desired_state"] == "running"
    assert snapshot["last_action"] in {"start", "operator_start"}


@pytest.mark.asyncio
async def test_gateway_set_channel_desired_state_records_pending_without_reconcile() -> None:
    """Gateway should persist desired state without immediate reconcile when requested."""
    config = SimpleNamespace(
        channels=SimpleNamespace(
            telegram=SimpleNamespace(enabled=True, token="bot-token"),
            feishu=SimpleNamespace(enabled=False, app_id="", app_secret=""),
        )
    )
    gateway = Gateway(config)

    await gateway._load_channel_orchestration_state()
    result = await gateway.set_channel_desired_state(
        "telegram",
        "stopped",
        reason="operator desired-state update",
        reconcile=False,
    )

    assert result["desired_state"] == "stopped"
    assert result["reconcile_status"] == "pending"
    assert result["last_action"] == "set_desired_state"


@pytest.mark.asyncio
async def test_gateway_reconcile_marks_blocked_when_config_disables_channel() -> None:
    """Gateway should expose blocked drift when desired state cannot be satisfied."""
    config = SimpleNamespace(
        channels=SimpleNamespace(
            telegram=SimpleNamespace(enabled=False, token=""),
            feishu=SimpleNamespace(enabled=False, app_id="", app_secret=""),
        )
    )
    gateway = Gateway(config)

    await gateway._load_channel_orchestration_state()
    await gateway._set_desired_state(
        "telegram",
        "running",
        reason="operator start request",
    )
    results = await gateway.reconcile_channels(trigger="operator_start", channel_name="telegram")

    assert results["telegram"]["drift_status"] == "blocked"
    assert results["telegram"]["reconcile_status"] == "blocked"
    assert "disabled in config" in results["telegram"]["reconcile_detail"]
