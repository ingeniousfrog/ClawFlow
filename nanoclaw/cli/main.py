"""CLI commands for nanoClaw."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Callable
import zipfile

import click


# --- Interactive selector (arrow keys navigation) ---


def _summarize_workflow_chain(call_chain: list[dict]) -> str:
    """Return a compact ordered list of tool names from a workflow call chain."""
    tools = [str(item.get("name", "")) for item in call_chain if item.get("type") == "tool"]
    tools = [name for name in tools if name]
    return " -> ".join(tools[:4]) if tools else "none"


def _format_role_display(role: str, role_label: str) -> str:
    """Return one compact role label for CLI output."""
    if role_label and role_label != role:
        return f"{role_label}[{role}]"
    return role or role_label or "-"


def _format_role_timeline_item(item: dict) -> str:
    """Return one compact role@stage string for recent workflow output."""
    role = str(item.get("role") or "")
    role_label = str(item.get("role_label") or role)
    stage = str(item.get("stage") or "")
    return f"{_format_role_display(role, role_label)}@{stage}"


def _format_tool_counts(items: list[dict]) -> str:
    """Return one compact `tool=count` summary."""
    if not items:
        return "-"
    return ", ".join(
        f"{item.get('tool_name', '-')}={item.get('count', 0)}" for item in items
    )


def _read_key() -> str:
    """Read a single keypress from terminal. Returns key name."""
    try:
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            ch = sys.stdin.read(1)
            if ch == "\x1b":  # Escape sequence
                ch2 = sys.stdin.read(1)
                if ch2 == "[":
                    ch3 = sys.stdin.read(1)
                    if ch3 == "A":
                        return "up"
                    elif ch3 == "B":
                        return "down"
                return "escape"
            elif ch in ("\r", "\n"):
                return "enter"
            elif ch == "\x03":  # Ctrl+C
                raise KeyboardInterrupt
            return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    except ImportError:
        # Windows fallback
        try:
            import msvcrt

            ch = msvcrt.getch()  # type: ignore[attr-defined]
            if ch in (b"\x00", b"\xe0"):  # Special key prefix
                ch2 = msvcrt.getch()  # type: ignore[attr-defined]
                if ch2 == b"H":
                    return "up"
                elif ch2 == b"P":
                    return "down"
            elif ch == b"\r":
                return "enter"
            return ch.decode("utf-8", errors="ignore")
        except ImportError:
            return input() or "enter"


def _clear_lines(n: int) -> None:
    """Move cursor up n lines and clear them."""
    for _ in range(n):
        sys.stdout.write("\x1b[A")  # Move up
        sys.stdout.write("\x1b[2K")  # Clear line
    sys.stdout.flush()


def select(
    options: list[tuple[str, str]],
    title: str = "",
    default: int = 0,
) -> int:
    """
    Interactive selector with arrow key navigation.

    Args:
        options: List of (value, label) tuples
        title: Optional title to display
        default: Default selected index

    Returns:
        Selected index
    """
    # Check if we have a real terminal
    if not sys.stdin.isatty():
        # Fallback to numbered input
        if title:
            click.echo(title)
        for i, (_, label) in enumerate(options):
            marker = ">" if i == default else " "
            click.echo(f"  {marker} {i + 1}. {label}")
        choice = click.prompt("Choice", type=int, default=default + 1)
        return max(0, min(choice - 1, len(options) - 1))

    selected = default

    def render() -> None:
        if title:
            click.echo(title)
        for i, (_, label) in enumerate(options):
            if i == selected:
                # Highlighted: cyan background or inverse
                click.echo(f"  \x1b[36m> {label}\x1b[0m")
            else:
                click.echo(f"    {label}")

    render()
    lines_to_clear = len(options) + (1 if title else 0)

    try:
        while True:
            key = _read_key()
            if key == "up":
                selected = (selected - 1) % len(options)
            elif key == "down":
                selected = (selected + 1) % len(options)
            elif key == "enter":
                # Clear and show final selection
                _clear_lines(lines_to_clear)
                _, label = options[selected]
                if title:
                    click.echo(title)
                click.echo(f"  \x1b[32m> {label}\x1b[0m")
                return selected
            elif key == "escape":
                return default

            # Re-render
            _clear_lines(lines_to_clear)
            render()
    except KeyboardInterrupt:
        click.echo("\nAborted.")
        sys.exit(1)


def confirm_interactive(prompt: str, default: bool = True) -> bool:
    """Interactive yes/no with arrow keys."""
    options = [("yes", "Yes"), ("no", "No")]
    default_idx = 0 if default else 1
    result = select(options, title=prompt, default=default_idx)
    return result == 0


@click.group()
@click.version_option(package_name="nanoclaw-ai")
def cli() -> None:
    """nanoClaw - Secure personal AI assistant"""
    pass


@cli.command()
def init() -> None:
    """Initialize nanoClaw with interactive wizard."""
    asyncio.run(setup_wizard())


async def setup_wizard() -> None:
    """Interactive setup wizard."""
    click.echo("\nWelcome to nanoClaw setup!\n")

    config: dict = {}

    # Step 1: LLM Provider
    providers = [
        ("openrouter", "OpenRouter (recommended - one key, all models)"),
        ("anthropic", "Anthropic API"),
        ("openai", "OpenAI API"),
        ("deepseek", "DeepSeek API"),
        ("local", "Local model (Ollama, LM Studio, etc.)"),
    ]
    choice = select(providers, title="Step 1/4: LLM Provider", default=0)

    if choice == 0:  # OpenRouter
        click.echo()
        api_key = click.prompt("  OpenRouter API key (openrouter.ai/keys)")
        config["providers"] = {"openrouter": {"apiKey": api_key}}

        click.echo()
        models = [
            ("anthropic/claude-sonnet-4-5", "claude-sonnet-4.5 (recommended)"),
            ("anthropic/claude-opus-4-5", "claude-opus-4.5 (smartest)"),
            ("openai/gpt-5", "gpt-5"),
            ("openai/gpt-5-mini", "gpt-5-mini (fast)"),
            ("google/gemini-3-pro", "gemini-3-pro"),
            ("google/gemini-3-flash", "gemini-3-flash (fast)"),
            ("deepseek/deepseek-chat", "deepseek-v3.2 (budget)"),
            ("deepseek/deepseek-reasoner", "deepseek-reasoner (thinking)"),
        ]
        model_idx = select(models, title="  Choose default model:", default=0)
        config["agents"] = {"defaults": {"model": models[model_idx][0]}}

    elif choice == 1:  # Anthropic
        click.echo()
        api_key = click.prompt("  Anthropic API key")
        click.echo()
        models = [
            ("claude-sonnet-4-5", "claude-sonnet-4.5 (recommended)"),
            ("claude-opus-4-5", "claude-opus-4.5 (smartest)"),
            ("claude-haiku-4-5", "claude-haiku-4.5 (fast, cheap)"),
        ]
        model_idx = select(models, title="  Choose model:", default=0)
        config["providers"] = {
            "anthropic": {
                "apiKey": api_key,
                "defaultModel": models[model_idx][0],
            }
        }

    elif choice == 2:  # OpenAI
        click.echo()
        api_key = click.prompt("  OpenAI API key")
        click.echo()
        models = [
            ("gpt-5", "gpt-5 (recommended)"),
            ("gpt-5.2", "gpt-5.2 (latest)"),
            ("gpt-5.1", "gpt-5.1"),
            ("gpt-5-mini", "gpt-5-mini (fast, cheap)"),
            ("gpt-5-nano", "gpt-5-nano (fastest, cheapest)"),
        ]
        model_idx = select(models, title="  Choose model:", default=0)
        config["providers"] = {
            "openai": {"apiKey": api_key, "defaultModel": models[model_idx][0]}
        }

    elif choice == 3:  # DeepSeek
        click.echo()
        api_key = click.prompt("  DeepSeek API key (platform.deepseek.com)")
        click.echo()
        models = [
            ("deepseek-chat", "deepseek-chat (V3)"),
            ("deepseek-reasoner", "deepseek-reasoner (R1)"),
        ]
        model_idx = select(models, title="  Choose model:", default=0)
        config["providers"] = {
            "deepseek": {
                "apiKey": api_key,
                "defaultModel": models[model_idx][0],
            }
        }

    elif choice == 4:  # Local model
        click.echo()
        local_providers = [
            ("ollama", "Ollama (localhost:11434)"),
            ("lmstudio", "LM Studio (localhost:1234)"),
            ("custom", "Custom URL"),
        ]
        local_choice = select(local_providers, title="  Local provider:", default=0)
        click.echo()

        if local_choice == 0:  # Ollama
            base_url = "http://localhost:11434/v1"
            model = click.prompt("  Model name (e.g., llama3, mistral)", default="llama3")
        elif local_choice == 1:  # LM Studio
            base_url = "http://localhost:1234/v1"
            model = click.prompt("  Model name", default="local-model")
        else:  # Custom
            base_url = click.prompt("  Base URL")
            model = click.prompt("  Model name")

        api_key = click.prompt(
            "  API key (leave empty if not required)",
            default="",
            show_default=False,
        )
        config["providers"] = {
            "openai": {
                "apiKey": api_key or "not-needed",
                "defaultModel": model,
                "baseUrl": base_url,
            }
        }

    # Step 2: Telegram
    click.echo()
    use_telegram = confirm_interactive("Step 2/4: Connect Telegram?", default=True)
    if use_telegram:
        click.echo()
        token = click.prompt("  Bot token (from @BotFather)")
        user_id = click.prompt("  Your Telegram user ID (from @userinfobot)")
        config["channels"] = {
            "telegram": {"enabled": True, "token": token, "allowFrom": [user_id]}
        }
    else:
        config["channels"] = {"telegram": {"enabled": False}}

    # Step 3: Web Search
    click.echo()
    search_modes = [
        ("rss", "RSS only (free, recommended)"),
        ("auto", "Auto (Serper/Brave first, RSS supplement)"),
        ("brave", "Brave only"),
        ("serper", "Serper only"),
        ("disabled", "Disabled"),
    ]
    search_mode_idx = select(search_modes, title="Step 3/4: Web search mode", default=0)
    search_mode = search_modes[search_mode_idx][0]
    config["tools"] = {
        "webSearch": {
            "provider": search_mode,
            "rssSourcesPath": "assets/rss-sources.json",
            "preferMainland": True,
            "mainlandOnly": False,
            "rssConcurrency": 8,
            "rssRetries": 1,
        }
    }

    if search_mode in {"auto", "brave"}:
        click.echo()
        search_key = click.prompt(
            "  Brave Search API key (leave empty to skip)",
            default="",
            show_default=False,
        )
        if search_key:
            config["tools"]["webSearch"]["apiKey"] = search_key
    if search_mode in {"auto", "serper"}:
        click.echo()
        search_key = click.prompt(
            "  Serper API key (leave empty to configure later)",
            default="",
            show_default=False,
        )
        if search_key:
            config["tools"]["webSearch"]["serperApiKey"] = search_key
        serper_limit = click.prompt(
            "  Serper max calls (0 for unlimited)",
            type=int,
            default=0,
        )
        if serper_limit > 0:
            config["tools"]["webSearch"]["serperMaxCalls"] = int(serper_limit)

    # Step 4: Save
    click.echo("\nStep 4/4: Saving configuration...")

    config_dir = Path.home() / ".nanoclaw"
    config_dir.mkdir(exist_ok=True)
    (config_dir / "workspace").mkdir(exist_ok=True)
    (config_dir / "data").mkdir(exist_ok=True)

    config_path = config_dir / "config.json"
    config_path.write_text(json.dumps(config, indent=2))

    # Set secure permissions
    try:
        config_path.chmod(0o600)
        (config_dir / "workspace").chmod(0o700)
    except Exception:
        pass  # May fail on Windows

    click.echo(f"  Config saved: {config_path}")
    click.echo(f"  Workspace: {config_dir / 'workspace'}")

    # Run security check
    click.echo("\n  Running security check...")
    from nanoclaw.security.doctor import SecurityDoctor

    doctor = SecurityDoctor()
    results = await doctor.check_all()
    for r in results:
        icon = "[OK]" if r.passed else "[!!]" if r.severity == "critical" else "[??]"
        click.echo(f"  {icon} {r.name}: {r.message}")

    click.echo(
        """
nanoClaw is ready!

  Start agent:    nanoclaw serve
  Chat from CLI:  nanoclaw chat "hello"
  View status:    nanoclaw status
  Security check: nanoclaw doctor
"""
    )


@cli.command()
@click.option("-m", "--message", help="One-shot message")
@click.option("-v", "--verbose", is_flag=True, help="Show detailed logs (tool calls, thinking)")
def chat(message: str | None, verbose: bool) -> None:
    """Chat with nanoClaw agent."""
    if verbose:
        import os
        os.environ["NANOCLAW_VERBOSE"] = "1"
        from nanoclaw.core.logger import set_verbose
        set_verbose(True)
    if message:
        asyncio.run(one_shot_chat(message))
    else:
        asyncio.run(interactive_chat())


async def one_shot_chat(message: str) -> None:
    """Run a single message through the agent."""
    from nanoclaw.core.agent import get_agent
    from nanoclaw.core.llm import ConnectionPool
    from nanoclaw.tools.shell import set_confirm_callback

    async def cli_confirm(question: str) -> bool:
        click.echo(f"\n{question}")
        return click.confirm("Allow?", default=False)

    set_confirm_callback(cli_confirm)

    try:
        agent = get_agent()
        response = await agent.run(message, session_id="cli")
        click.echo(f"\n{response}")
    finally:
        await ConnectionPool.close()


async def interactive_chat() -> None:
    """Interactive chat REPL."""
    from nanoclaw.core.agent import get_agent
    from nanoclaw.core.llm import ConnectionPool
    from nanoclaw.tools.shell import set_confirm_callback

    async def cli_confirm(question: str) -> bool:
        click.echo(f"\n{question}")
        return click.confirm("Allow?", default=False)

    set_confirm_callback(cli_confirm)

    click.echo("nanoClaw Interactive Chat")
    click.echo("Type 'exit' or 'quit' to leave\n")

    agent = get_agent()
    session_id = "cli_interactive"

    try:
        while True:
            try:
                user_input = click.prompt("You", prompt_suffix="> ")
            except (EOFError, KeyboardInterrupt):
                break

            if user_input.lower() in ("exit", "quit"):
                break

            response = await agent.run(user_input, session_id=session_id)
            click.echo(f"\nAssistant: {response}\n")
    finally:
        await ConnectionPool.close()


@cli.command()
@click.option("-v", "--verbose", is_flag=True, help="Show detailed logs (tool calls, thinking)")
def serve(verbose: bool) -> None:
    """Start nanoClaw gateway (Telegram + Cron + Dashboard)."""
    if verbose:
        import os
        import logging
        os.environ["NANOCLAW_VERBOSE"] = "1"
        # Import and set verbose BEFORE any other nanoclaw imports
        from nanoclaw.core.logger import set_verbose
        set_verbose(True)
        # Verify logger state
        root = logging.getLogger("nanoclaw")
        click.echo(f"Verbose logging enabled (level={root.level}, handlers={len(root.handlers)})")
    asyncio.run(start_gateway())


async def start_gateway() -> None:
    """Start the gateway with all components."""
    from nanoclaw.channels.gateway import Gateway
    from nanoclaw.core.config import get_config

    config = get_config()
    gateway = Gateway(config)
    await gateway.start()


@cli.command()
def status() -> None:
    """Show nanoClaw status."""
    asyncio.run(show_status())


def _summarize_task_description(text: str, limit: int = 48) -> str:
    """Return a compact one-line task description preview."""
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= limit:
        return cleaned
    return f"{cleaned[:limit - 3]}..."


def _resolve_channel_gateway() -> object | None:
    """Return the active gateway only when it exposes channel runtime state."""
    from nanoclaw.channels.gateway import get_gateway

    gateway = get_gateway()
    if gateway is None or not hasattr(gateway, "get_channel_runtime_snapshot"):
        return None
    return gateway


def _print_channel_contract(channel_contract: dict) -> None:
    """Render one compact channel contract section for CLI output."""
    click.echo("\nChannels:")
    click.echo(f"  Contract: {channel_contract['contract_version']}")
    channel_summary = channel_contract["summary"]
    click.echo(
        "  "
        f"Summary: enabled={channel_summary['enabled_count']} "
        f"configured={channel_summary['configured_count']} "
        f"running={channel_summary['running_count']} "
        f"failed={channel_summary['failed_count']} "
        f"disabled={channel_summary['disabled_count']} "
        f"misconfigured={channel_summary['misconfigured_count']} "
        f"allowlist={channel_summary['allowlist_auth_count']} "
        f"open={channel_summary['open_auth_count']} "
        f"proactiveReady={channel_summary['proactive_ready_count']} "
        f"attention={channel_summary.get('diagnostic_attention_count', 0)}"
    )
    routing_policy = dict(channel_contract.get("routing_policy") or {})
    orchestration = dict(channel_contract.get("orchestration") or {})
    if routing_policy:
        click.echo(f"  Routing: {routing_policy.get('policy_version') or '-'}")
        for key, label in (
            ("default_proactive", "Default proactive"),
            ("heartbeat", "Heartbeat"),
            ("runtime_alert", "Runtime alert"),
            ("runtime_alert_escalation", "Alert escalation"),
        ):
            route = dict(routing_policy.get(key) or {})
            click.echo(
                "  "
                f"{label}: request={route.get('requested_channel') or 'auto'} "
                f"selected={route.get('selected_channel') or '-'} "
                f"status={route.get('status') or 'unresolved'} "
                f"mode={route.get('selection_kind') or 'unresolved'} "
                f"reason={route.get('reason') or '-'}"
            )
    if orchestration:
        click.echo(
            "  "
            f"Orchestration: {orchestration.get('policy_version') or '-'} "
            f"desiredRunning={orchestration.get('desired_running_count', 0)} "
            f"desiredStopped={orchestration.get('desired_stopped_count', 0)} "
            f"drifted={orchestration.get('drifted_count', 0)} "
            f"blocked={orchestration.get('blocked_count', 0)} "
            f"reconciling={orchestration.get('reconciling_count', 0)} "
            f"interval={orchestration.get('reconcile_interval_seconds', 0)}s"
        )
    for channel_name, channel_info in channel_contract["channels"].items():
        diagnostics = dict(channel_info.get("diagnostics") or {})
        actions = ",".join(channel_info.get("operator_actions", [])) or "none"
        click.echo(
            "  "
            f"{channel_info['label']}: {channel_info['status']} "
            f"mode={channel_info['delivery_mode']} "
            f"auth={channel_info['auth_mode']} "
            f"allow={channel_info['allowlist_count']} "
            f"proactive={'on' if channel_info['supports_proactive'] else 'off'} "
            f"targeted={'on' if channel_info['supports_targeted_proactive'] else 'off'} "
            f"confirm={'on' if channel_info['supports_confirmation'] else 'off'} "
            f"actions={actions} "
            f"detail={channel_info['detail']}"
        )
        click.echo(
            "    "
            f"authDetail={channel_info.get('auth_detail') or '-'} "
            f"routeMode={channel_info.get('proactive_target_mode') or '-'} "
            f"routeReady={'on' if channel_info.get('routing_ready') else 'off'} "
            f"routeRoles={','.join(channel_info.get('route_roles', [])) or 'none'} "
            f"routeDetail={channel_info.get('routing_detail') or '-'}"
        )
        click.echo(
            "    "
            f"desired={channel_info.get('desired_state') or '-'} "
            f"drift={channel_info.get('drift_status') or '-'} "
            f"reconcile={channel_info.get('reconcile_status') or '-'} "
            f"lastAction={channel_info.get('last_action') or '-'} "
            f"summary={channel_info.get('drift_summary') or '-'}"
        )
        click.echo(
            "    "
            f"diag={channel_info.get('diagnostic_health') or '-'} "
            f"incoming={diagnostics.get('incoming_total', 0)}/"
            f"{diagnostics.get('incoming_failures', 0)} "
            f"proactive={diagnostics.get('outgoing_total', 0)}/"
            f"{diagnostics.get('outgoing_failures', 0)} "
            f"targeted={diagnostics.get('targeted_outgoing_total', 0)}/"
            f"{diagnostics.get('targeted_outgoing_failures', 0)} "
            f"summary={channel_info.get('diagnostic_summary') or '-'}"
        )


async def show_status() -> None:
    """Display current status."""
    from nanoclaw.channels.contract import build_channel_contract
    from nanoclaw.core.config import get_config
    from nanoclaw.memory.store import get_memory_store
    from nanoclaw.security.policy_contract import build_boundary_policy_contract
    from nanoclaw.security.audit import get_audit_log
    from nanoclaw.runtime.tasks import get_task_store
    from nanoclaw.tools.spawn import (
        get_background_runtime_metrics,
        summarize_runtime_health,
    )

    try:
        config = get_config()
    except FileNotFoundError:
        click.echo("nanoClaw not configured. Run 'nanoclaw init' first.")
        return

    click.echo("\nnanoClaw Status")
    click.echo("=" * 40)

    # Provider
    provider, _, model, _ = config.get_active_provider()
    click.echo(f"Provider: {provider}")
    click.echo(f"Model: {model}")
    web_search = config.tools.web_search
    click.echo(f"Web search provider: {web_search.provider}")
    boundary_policy = build_boundary_policy_contract(config)
    channel_gateway = _resolve_channel_gateway()
    channel_contract = build_channel_contract(config, channel_gateway)
    shell_policy = boundary_policy["shell"]
    container_target = shell_policy["primary_container_target"]
    web_host_policy = boundary_policy["web_hosts"]
    secret_policy = boundary_policy["secrets"]
    shell_backend = str(shell_policy["backend_selected"])
    if shell_policy["backend_requested"] != shell_policy["backend_selected"]:
        shell_backend = (
            f"{shell_policy['backend_requested']}->{shell_policy['backend_selected']}"
        )
    stronger_available = (
        ",".join(shell_policy["available_backends"])
        if shell_policy["stronger_backend_available"]
        else "none"
    )
    click.echo("\nBoundary policy:")
    click.echo(f"  Contract: {boundary_policy['contract_version']}")
    click.echo(
        "  "
        f"Shell: {shell_policy['mode']} "
        f"backend={shell_backend} "
        f"available={stronger_available} "
        f"image={'on' if shell_policy['container_image_configured'] else 'off'} "
        f"confirm={'on' if shell_policy['confirm_dangerous'] else 'off'} "
        f"isolateHome={'on' if shell_policy['isolate_home'] else 'off'} "
        f"memory={shell_policy['max_memory_mb']}MB "
        f"fileLimit={shell_policy['max_file_size_kb']}KB"
    )
    click.echo(
        "  "
        f"Primary container target: {container_target['backend']} "
        f"status={container_target['status']} "
        f"runtime={'on' if container_target['runtime_reachable'] else 'off'} "
        f"image={'on' if container_target['image_present'] else 'off'} "
        f"detail={container_target['detail'] or '-'}"
    )
    if container_target.get("drifted"):
        click.echo(
            "  "
            f"Drift: {container_target.get('lifecycle_state') or 'unknown'} "
            f"reason={container_target.get('drift_reason') or '-'}"
        )
    if not container_target["ready"]:
        if container_target.get("runtime_command"):
            click.echo(
                "  "
                f"Lifecycle: {container_target['runtime_command']}"
            )
        if container_target.get("prepare_command"):
            click.echo(
                "  "
                f"Prepare: {container_target['prepare_command']}"
            )
        click.echo(
            "  "
            f"Verify: {container_target['verify_command']}"
        )
        if container_target["remediation_steps"]:
            click.echo(
                "  "
                f"Remedy: {container_target['remediation_steps'][0]}"
            )
        elif container_target["remediation_commands"]:
            click.echo(
                "  "
                f"Next: {container_target['remediation_commands'][0]}"
            )
    click.echo(
        "  "
        f"Web hosts: {'enabled' if web_host_policy['host_policy_enabled'] else 'disabled'} "
        f"allow={web_host_policy['allowed_hosts_count']} "
        f"block={web_host_policy['blocked_hosts_count']} "
        f"policy={web_host_policy['policy_name']}@{web_host_policy['policy_version']}"
    )
    click.echo(
        "  "
        f"Secrets: envFallback={'on' if secret_policy['allow_environment_fallback'] else 'off'} "
        f"audit={'on' if secret_policy['audit_access'] else 'off'} "
        f"caps={secret_policy['web_search_capability_count']} "
        f"policy={secret_policy['policy_name']}@{secret_policy['policy_version']}"
    )

    _print_channel_contract(channel_contract)
    click.echo(f"Dashboard: {'enabled' if config.dashboard.enabled else 'disabled'}")
    heartbeat_state = "enabled" if config.heartbeat.enabled else "disabled"
    click.echo(f"Heartbeat: {heartbeat_state}")
    if config.heartbeat.enabled:
        click.echo(f"  Checklist: {config.heartbeat.checklist_path}")
        target = config.heartbeat.notify_channel or "auto"
        click.echo(f"  Notify channel: {target}")
        click.echo(f"  Interval: {config.heartbeat.interval_seconds}s")

    # Stats
    try:
        memory = get_memory_store()
        stats = await memory.get_stats()
        click.echo(f"\nMessages: {stats['total_messages']}")
        click.echo(f"Sessions: {stats['sessions']}")
        click.echo(f"Memories: {stats['memories']}")
        click.echo(f"Cron jobs: {stats['cron_jobs']}")
    except Exception:
        pass

    # Today's audit stats
    try:
        audit = get_audit_log()
        today = await audit.get_stats_today()
        workflow_today = await audit.get_workflow_stats_today()
        workflow_eval_today = await audit.get_workflow_evaluation_stats_today()
        recent_workflows = await audit.get_recent_workflows(limit=3)
        recent_workflow_evals = await audit.get_recent_workflow_evaluations(limit=3)
        workflow_recommendations = await audit.get_workflow_recommendations(days=7, limit=3)
        boundary_metrics = await audit.get_boundary_metrics(window_hours=24)
        if web_search.serper_max_calls > 0:
            serper_usage = await audit.get_provider_usage(
                "serper",
                web_search.serper_max_calls,
            )
            click.echo("\nSerper quota:")
            click.echo(
                f"  Remaining: {serper_usage['remaining_calls']}/{serper_usage['max_calls']}"
            )
            click.echo(f"  Used: {serper_usage['used_calls']}")
        click.echo("\nToday's activity:")
        click.echo(f"  Messages: {today['messages']}")
        click.echo(f"  Tool calls: {today['tool_calls']}")
        click.echo(f"  Tokens: {today['total_tokens']}")
        if today['errors'] > 0:
            click.echo(f"  Errors: {today['errors']}")
        if today['blocked'] > 0:
            click.echo(f"  Blocked: {today['blocked']}")
        click.echo("\nBoundary activity (24h):")
        click.echo(
            "  "
            f"Boundary decisions: "
            f"allowed={boundary_metrics['boundary']['allowed']} "
            f"blocked={boundary_metrics['boundary']['blocked']} "
            f"total={boundary_metrics['boundary']['total']}"
        )
        click.echo(
            "  "
            f"Top boundary tools: {_format_tool_counts(boundary_metrics['boundary']['top_tools'])}"
        )
        click.echo(
            "  "
            f"Secret access: "
            f"granted={boundary_metrics['secrets']['granted']} "
            f"blocked={boundary_metrics['secrets']['blocked']} "
            f"missing={boundary_metrics['secrets']['missing']} "
            f"config={boundary_metrics['secrets']['config_sources']} "
            f"env={boundary_metrics['secrets']['env_sources']}"
        )
        click.echo(
            "  "
            f"Top secret tools: {_format_tool_counts(boundary_metrics['secrets']['top_tools'])}"
        )
        click.echo("\nToday's workflows:")
        click.echo(f"  Runs: {workflow_today['workflow_runs']}")
        click.echo(f"  Tokens: {workflow_today['total_tokens']}")
        click.echo(f"  Avg latency: {workflow_today['avg_execution_ms']}ms")
        if workflow_today["failures"] > 0:
            click.echo(f"  Non-success: {workflow_today['failures']}")
        click.echo("\nToday's workflow evaluation:")
        click.echo(
            "  "
            f"Good/Review/Poor: "
            f"{workflow_eval_today['good_runs']}/"
            f"{workflow_eval_today['review_runs']}/"
            f"{workflow_eval_today['poor_runs']}"
        )
        click.echo(
            "  "
            f"Avg quality: {workflow_eval_today['avg_quality_score']} "
            f"Avg efficiency: {workflow_eval_today['avg_efficiency_score']}"
        )
        click.echo(
            "  "
            f"Feedback +/0/-: "
            f"{workflow_eval_today['positive_feedback']}/"
            f"{workflow_eval_today['neutral_feedback']}/"
            f"{workflow_eval_today['negative_feedback']}"
        )
        if recent_workflows:
            click.echo("\nRecent workflows:")
            for item in recent_workflows:
                tool_chain = _summarize_workflow_chain(item.get("call_chain", []))
                role_chain = " -> ".join(
                    _format_role_timeline_item(step)
                    for step in list(item.get("role_execution_timeline") or [])[:4]
                    if step.get("role")
                ) or "-"
                click.echo(
                    "  - "
                    f"#{item['id']} {item['workflow_name']} [{item['status']}] "
                    f"{item['execution_ms']}ms {item['total_tokens']} tok "
                    f"tools={tool_chain} roles={role_chain}"
                )
                if item.get("failure_reason"):
                    click.echo(f"    reason={item['failure_reason']}")
        if recent_workflow_evals:
            click.echo("\nRecent workflow evaluations:")
            for item in recent_workflow_evals:
                click.echo(
                    "  - "
                    f"run=#{item['workflow_run_id']} "
                    f"{item['workflow_name']} [{item['evaluation_label']}] "
                    f"quality={item['quality_score']} "
                    f"efficiency={item['efficiency_score']} "
                    f"feedback={item['feedback_signal']}"
                )
                if item.get("suggestions"):
                    click.echo(f"    next={item['suggestions'][0]}")
                if item.get("attention_reasons"):
                    click.echo(f"    why={item['attention_reasons'][0]}")
        if workflow_recommendations:
            click.echo("\nWorkflow recommendations (7d):")
            for item in workflow_recommendations:
                click.echo(
                    "  - "
                    f"{item['workflow_name']} [{item['recommendation_status']}] "
                    f"runs={item['run_count']} "
                    f"quality={item['avg_quality_score']} "
                    f"efficiency={item['avg_efficiency_score']}"
                )
                if item.get("recommendations"):
                    click.echo(f"    next={item['recommendations'][0]}")
                if item.get("top_attention_reason"):
                    click.echo(f"    why={item['top_attention_reason']}")
    except Exception:
        pass

    # Recent tasks
    try:
        task_store = get_task_store()
        runtime_metrics = get_background_runtime_metrics()
        queue_metrics = await task_store.get_queue_metrics(
            starvation_threshold_seconds=runtime_metrics["starvation_threshold_seconds"],
            lease_timeout_seconds=runtime_metrics["lease_timeout_seconds"],
            stall_threshold_seconds=runtime_metrics["stall_threshold_seconds"],
        )
        runtime_health = summarize_runtime_health(queue_metrics)
        capacity = max(1, int(runtime_metrics["capacity"]))
        global_running = int(queue_metrics["running_tasks"])
        global_saturation = int((global_running / capacity) * 100)
        click.echo("\nQueue:")
        click.echo(
            "  "
            f"ready={queue_metrics['ready_backlog']} "
            f"retry={queue_metrics['retry_backlog']} "
            f"rate_limited={queue_metrics['rate_limited_backlog']} "
            f"running={queue_metrics['running_tasks']} "
            f"workers={queue_metrics['running_workers']} "
            f"dead_letter={queue_metrics['dead_letter_tasks']} "
            f"starved_ready={queue_metrics['starved_ready_tasks']} "
            f"stale_running={queue_metrics['stale_running_tasks']} "
            f"cancel_requested={queue_metrics['cancel_requested_running']}"
        )
        click.echo(
            "  "
            f"oldest_ready_age={queue_metrics['oldest_ready_age_seconds']}s "
            f"oldest_stale_age={queue_metrics['oldest_stale_running_age_seconds']}s "
            f"next_retry_in={queue_metrics['next_retry_in_seconds']}s "
            f"local_runtime={runtime_metrics['active_tasks']}/{runtime_metrics['capacity']} "
            f"global_pool={global_running}/{runtime_metrics['capacity']} "
            f"local_saturation={runtime_metrics['saturation_pct']}% "
            f"global_saturation={global_saturation}% "
            f"starvation_threshold={runtime_metrics['starvation_threshold_seconds']}s "
            f"stall_threshold={runtime_metrics['stall_threshold_seconds']}s "
            f"lease_timeout={runtime_metrics['lease_timeout_seconds']}s "
            f"heartbeat_interval={runtime_metrics['heartbeat_interval_seconds']}s"
        )
        click.echo(
            "  "
            f"health={runtime_health['status']} "
            f"reasons={runtime_health['summary']} "
            f"base_alert_severity={runtime_health['base_alert_severity']} "
            f"alert_channel={runtime_metrics['alert_channel']} "
            f"alert_escalation_channel={runtime_metrics['alert_escalation_channel']} "
            f"alert_cooldown={runtime_metrics['alert_cooldown_seconds']}s "
            f"alert_escalate_after={runtime_metrics['alert_escalate_after']}x "
            f"schedule_alert_retrying_after="
            f"{runtime_metrics['schedule_alert_retrying_after']}x "
            f"schedule_alert_escalate_after="
            f"{runtime_metrics['schedule_alert_escalate_after']}x"
        )
        recent_tasks = await task_store.list_tasks(limit=5)
        if recent_tasks:
            click.echo("\nRecent tasks:")
            for item in recent_tasks:
                owner = item.get("claimed_by") or "-"
                heartbeat = item.get("last_heartbeat_at") or "-"
                click.echo(
                    "  - "
                    f"{item['task_id']} [{item['status']}] "
                    f"prio={item.get('priority', 100)} "
                    f"attempts={item['attempt_count']}/{item.get('max_attempts', 1)} "
                    f"owner={owner}"
                )
                click.echo(
                    "    "
                    f"source={item.get('source') or '-'} "
                    f"timeout={item.get('timeout_seconds', 0)}s "
                    f"backoff={item.get('retry_backoff_seconds', 0)}s "
                    f"rate_limit_key={item.get('rate_limit_key') or '-'} "
                    f"idempotency_key={item.get('idempotency_key') or '-'} "
                    f"rate_limit="
                    f"{item.get('rate_limit_max_claims', 0)}/"
                    f"{item.get('rate_limit_window_seconds', 0)} "
                    f"dead_letter={int(bool(item.get('dead_lettered')))} "
                    f"cancel_requested={int(bool(item.get('cancel_requested')))} "
                    f"heartbeat={heartbeat}"
                )
                click.echo(
                    "    "
                    f"retry_at={item.get('next_attempt_at') or '-'} "
                    f"last_claimed_at={item.get('last_claimed_at') or '-'} "
                    f"desc={_summarize_task_description(item.get('description', ''))}"
                )
                if item.get("last_error"):
                    click.echo(f"    error={item['last_error']}")
                if item.get("dead_letter_reason"):
                    click.echo(f"    dead_letter_reason={item['dead_letter_reason']}")
    except Exception:
        pass


@cli.group()
def channel() -> None:
    """Inspect or control channel runtime state."""
    pass


@channel.command("list")
def channel_list() -> None:
    """Show the channel registry contract."""
    asyncio.run(show_channel_list())


async def show_channel_list() -> None:
    """Display the compact channel registry contract."""
    from nanoclaw.channels.contract import build_channel_contract
    from nanoclaw.core.config import get_config

    try:
        config = get_config()
    except FileNotFoundError:
        click.echo("nanoClaw not configured. Run 'nanoclaw init' first.")
        return

    click.echo("\nChannel Registry")
    click.echo("=" * 40)
    gateway = _resolve_channel_gateway()
    if gateway is None:
        click.echo("Runtime: config-only snapshot (no active gateway in this process)")
    _print_channel_contract(build_channel_contract(config, gateway))


@channel.command("action")
@click.argument("name")
@click.argument(
    "action",
    type=click.Choice(
        ["start", "stop", "restart", "recover", "reconcile"],
        case_sensitive=False,
    ),
)
def channel_action(name: str, action: str) -> None:
    """Run one operator action against a managed channel."""
    try:
        asyncio.run(run_channel_action_command(name, action))
    except FileNotFoundError:
        click.echo("nanoClaw not configured. Run 'nanoclaw init' first.")
        raise SystemExit(1)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


async def run_channel_action_command(name: str, action: str) -> None:
    """Run one channel action and print the refreshed channel state."""
    from nanoclaw.channels.contract import build_channel_contract
    from nanoclaw.channels.gateway import get_gateway
    from nanoclaw.core.config import get_config

    config = get_config()
    gateway = get_gateway()
    if gateway is None or not hasattr(gateway, "run_channel_action"):
        raise RuntimeError(
            "No active gateway runtime in this process. Use the dashboard operator API "
            "or run the action inside the active runtime process."
        )

    normalized_name = name.strip().lower()
    normalized_action = action.strip().lower()
    runtime = await gateway.run_channel_action(normalized_name, normalized_action)
    channel_contract = build_channel_contract(config, gateway)
    channel_info = dict(channel_contract["channels"].get(normalized_name) or {})
    if not channel_info:
        raise ValueError(f"Unknown managed channel `{normalized_name}`")

    click.echo("\nChannel Action")
    click.echo("=" * 40)
    click.echo(
        f"{channel_info['label']}: action={normalized_action} "
        f"status={channel_info['status']} detail={channel_info['detail']}"
    )
    if channel_info.get("last_error"):
        click.echo(f"Last error: {channel_info['last_error']}")
    click.echo(
        "Desired state: "
        f"{channel_info.get('desired_state') or '-'} "
        f"drift={channel_info.get('drift_status') or '-'} "
        f"reconcile={channel_info.get('reconcile_status') or '-'}"
    )
    click.echo(
        "Reconcile detail: "
        f"{channel_info.get('reconcile_detail') or '-'}"
    )
    click.echo(
        "Diagnostics: "
        f"{channel_info.get('diagnostic_health') or '-'} "
        f"({channel_info.get('diagnostic_summary') or '-'})"
    )
    click.echo(
        "Next actions: "
        + (", ".join(channel_info.get("operator_actions", [])) or "none")
    )
    click.echo(f"Transition at: {runtime.get('last_transition_at', 0)}")


@channel.command("desired-state")
@click.argument("name")
@click.argument("desired_state", type=click.Choice(["running", "stopped"], case_sensitive=False))
@click.option(
    "--no-reconcile",
    is_flag=True,
    help="Only record the desired state without running an immediate reconcile pass.",
)
def channel_desired_state(name: str, desired_state: str, no_reconcile: bool) -> None:
    """Persist one desired state for a managed channel."""
    try:
        asyncio.run(
            set_channel_desired_state_command(
                name,
                desired_state,
                reconcile=not no_reconcile,
            )
        )
    except FileNotFoundError:
        click.echo("nanoClaw not configured. Run 'nanoclaw init' first.")
        raise SystemExit(1)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


async def set_channel_desired_state_command(
    name: str,
    desired_state: str,
    *,
    reconcile: bool,
) -> None:
    """Set one channel desired state and print the refreshed contract entry."""
    from nanoclaw.channels.contract import build_channel_contract
    from nanoclaw.channels.gateway import get_gateway
    from nanoclaw.core.config import get_config

    config = get_config()
    gateway = get_gateway()
    if gateway is None or not hasattr(gateway, "set_channel_desired_state"):
        raise RuntimeError(
            "No active gateway runtime in this process. Use the dashboard operator API "
            "or run the action inside the active runtime process."
        )

    normalized_name = name.strip().lower()
    normalized_desired_state = desired_state.strip().lower()
    runtime = await gateway.set_channel_desired_state(
        normalized_name,
        normalized_desired_state,
        reason="cli desired-state update",
        reconcile=reconcile,
    )
    channel_contract = build_channel_contract(config, gateway)
    channel_info = dict(channel_contract["channels"].get(normalized_name) or {})
    if not channel_info:
        raise ValueError(f"Unknown managed channel `{normalized_name}`")

    click.echo("\nChannel Desired State")
    click.echo("=" * 40)
    click.echo(
        f"{channel_info['label']}: desired={normalized_desired_state} "
        f"actual={channel_info.get('actual_status') or '-'} "
        f"drift={channel_info.get('drift_status') or '-'} "
        f"reconcile={channel_info.get('reconcile_status') or '-'}"
    )
    click.echo(
        "Summary: "
        f"{channel_info.get('drift_summary') or '-'}"
    )
    click.echo(
        "Diagnostics: "
        f"{channel_info.get('diagnostic_health') or '-'} "
        f"({channel_info.get('diagnostic_summary') or '-'})"
    )
    click.echo(
        "Next actions: "
        + (", ".join(channel_info.get("operator_actions", [])) or "none")
    )
    click.echo(f"Transition at: {runtime.get('last_transition_at', 0)}")


@cli.command("task-cancel")
@click.argument("task_id")
def task_cancel(task_id: str) -> None:
    """Request cancellation for one persisted task."""
    asyncio.run(cancel_task(task_id))


async def cancel_task(task_id: str) -> None:
    """Request task cancellation and print the resulting state."""
    from nanoclaw.runtime.tasks import get_task_store

    try:
        task = await get_task_store().request_cancel(task_id)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"Task {task['task_id']} cancel requested. "
        f"status={task['status']} cancel_requested={int(task['cancel_requested'])}"
    )


@cli.command("task-requeue")
@click.argument("task_id")
def task_requeue(task_id: str) -> None:
    """Move one failed or cancelled task back to pending."""
    asyncio.run(requeue_task(task_id))


async def requeue_task(task_id: str) -> None:
    """Requeue one terminal task and reset its retry state."""
    from nanoclaw.runtime.tasks import get_task_store

    try:
        task = await get_task_store().requeue_task(task_id)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"Task {task['task_id']} requeued. "
        f"status={task['status']} attempts={task['attempt_count']}/{task.get('max_attempts', 1)}"
    )


@cli.command("task-replay")
@click.argument("task_id")
def task_replay(task_id: str) -> None:
    """Show one persisted task replay with steps and tool traces."""
    asyncio.run(show_task_replay(task_id))


async def show_task_replay(task_id: str) -> None:
    """Print one structured task replay to the terminal."""
    from nanoclaw.security.audit import get_audit_log

    replay = await get_audit_log().get_task_replay(task_id)
    if replay is None:
        raise click.ClickException(f"Task `{task_id}` not found.")

    task = replay["task"]
    click.echo(
        f"Task {task['task_id']} [{task['status']}] "
        f"source={task.get('source') or '-'} attempts={task.get('attempt_count', 0)}"
    )
    click.echo(
        f"  session={task.get('session_id') or '-'} "
        f"claimed_by={task.get('claimed_by') or '-'} "
        f"updated_at={task.get('updated_at') or '-'}"
    )
    click.echo(f"  desc={_summarize_task_description(task.get('description', ''))}")

    if replay["task_runs"]:
        click.echo("\nTask runs:")
        for item in replay["task_runs"]:
            click.echo(
                "  - "
                f"attempt={item.get('attempt_number', 0)} "
                f"status={item.get('status', '')} "
                f"worker={item.get('worker_id') or '-'} "
                f"latency={item.get('execution_ms', 0)}ms"
            )
            if item.get("failure_reason"):
                click.echo(f"    reason={item['failure_reason']}")

    if replay["steps"]:
        click.echo("\nSteps:")
        for item in replay["steps"]:
            click.echo(
                "  - "
                f"{item.get('step_id')} [{item.get('status')}] "
                f"attempts={item.get('attempt_count', 0)} "
                f"checkpoint={int(bool(item.get('is_checkpoint')))} "
                f"idempotent={int(bool(item.get('idempotent')))}"
            )
            if item.get("last_error"):
                click.echo(f"    error={item['last_error']}")

    if replay["tool_traces"]:
        click.echo("\nTool traces:")
        for item in replay["tool_traces"]:
            click.echo(
                "  - "
                f"attempt={item.get('attempt_number', 0)} "
                f"step={item.get('step_id') or '-'} "
                f"{item.get('tool_name')} [{item.get('status')}] "
                f"cached={int(bool(item.get('cached')))} "
                f"{item.get('execution_ms', 0)}ms"
            )
            if item.get("output_summary"):
                click.echo(f"    output={item['output_summary']}")

    if replay["workflow_runs"]:
        click.echo("\nWorkflow runs:")
        for item in replay["workflow_runs"]:
            click.echo(
                "  - "
                f"{item.get('workflow_name')} [{item.get('status')}] "
                f"{item.get('execution_ms', 0)}ms "
                f"{item.get('total_tokens', 0)} tok"
            )

    if replay.get("audit_events"):
        click.echo("\nAudit events:")
        for item in replay["audit_events"]:
            click.echo(
                "  - "
                f"{item.get('action_type')} [{item.get('status')}] "
                f"tool={item.get('tool_name') or '-'} "
                f"at={item.get('timestamp') or '-'}"
            )
            if item.get("input_summary"):
                click.echo(f"    in={item['input_summary']}")
            if item.get("output_summary"):
                click.echo(f"    out={item['output_summary']}")


@cli.command("workflow-feedback")
@click.argument("workflow_run_id", type=int)
@click.argument(
    "feedback_signal",
    type=click.Choice(["positive", "neutral", "negative", "unknown"]),
)
def workflow_feedback(workflow_run_id: int, feedback_signal: str) -> None:
    """Record one explicit feedback signal for a workflow evaluation."""
    asyncio.run(record_workflow_feedback(workflow_run_id, feedback_signal))


async def record_workflow_feedback(
    workflow_run_id: int,
    feedback_signal: str,
) -> None:
    """Persist one explicit workflow feedback signal and print the result."""
    from nanoclaw.security.audit import get_audit_log

    try:
        item = await get_audit_log().set_workflow_feedback(
            workflow_run_id,
            feedback_signal,
        )
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(
        f"Workflow run #{item['workflow_run_id']} feedback updated to "
        f"{item['feedback_signal']}."
    )


@cli.command("workflow-report")
@click.option("--days", default=7, show_default=True, type=int, help="Rolling evaluation window.")
@click.option("--limit", default=5, show_default=True, type=int, help="Maximum rows to show.")
@click.option(
    "--format",
    "output_format",
    default="text",
    show_default=True,
    type=click.Choice(["text", "json"]),
    help="Output format.",
)
def workflow_report(days: int, limit: int, output_format: str) -> None:
    """Show aggregated workflow recommendations from recent evaluations."""
    asyncio.run(show_workflow_report(days, limit, output_format))


async def show_workflow_report(days: int, limit: int, output_format: str) -> None:
    """Print one aggregated workflow recommendation report."""
    from nanoclaw.security.audit import get_audit_log

    try:
        items = await get_audit_log().get_workflow_recommendations(days=days, limit=limit)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if output_format == "json":
        click.echo(json.dumps(items, indent=2, ensure_ascii=False))
        return

    if not items:
        click.echo(f"No workflow recommendations found in the last {days} day(s).")
        return

    click.echo(f"Workflow recommendations ({days}d):")
    for item in items:
        click.echo(
            "  - "
            f"{item['workflow_name']} [{item['recommendation_status']}] "
            f"runs={item['run_count']} "
            f"good/review/poor={item['good_runs']}/{item['review_runs']}/{item['poor_runs']}"
        )
        click.echo(
            "    "
            f"quality={item['avg_quality_score']} "
            f"efficiency={item['avg_efficiency_score']} "
            f"feedback={item['positive_feedback']}/"
            f"{item['neutral_feedback']}/"
            f"{item['negative_feedback']}"
        )
        if item.get("recommendations"):
            click.echo(f"    next={item['recommendations'][0]}")
        if item.get("top_attention_reason"):
            click.echo(f"    why={item['top_attention_reason']}")


@cli.command("workflow-roles")
@click.argument("workflow_run_id", type=int)
def workflow_roles(workflow_run_id: int) -> None:
    """Show one role-level replay view for a workflow run."""
    asyncio.run(show_workflow_roles(workflow_run_id))


async def show_workflow_roles(workflow_run_id: int) -> None:
    """Print one compact role-level workflow replay."""
    from nanoclaw.security.audit import get_audit_log

    replay = await get_audit_log().get_workflow_role_replay(workflow_run_id)
    if replay is None:
        raise click.ClickException(f"Workflow run `{workflow_run_id}` not found.")

    click.echo(
        f"Workflow run #{replay['id']} {replay['workflow_name']} [{replay['status']}]"
    )
    click.echo(
        "  "
        f"shared_evidence_refs="
        f"{', '.join(replay.get('shared_evidence_refs') or []) or '-'}"
    )
    if replay.get("role_checkpoint_timeline"):
        click.echo("\nRole checkpoints:")
        for item in replay["role_checkpoint_timeline"]:
            click.echo(
                "  - "
                f"{item.get('checkpoint_id') or '-'} "
                f"role={item.get('role') or '-'} "
                f"stage={item.get('stage') or '-'} "
                f"messages={item.get('message_count', 0)} "
                f"evidence={item.get('evidence_count', 0)} "
                f"refs={', '.join(item.get('evidence_refs') or []) or '-'}"
            )
    if not replay.get("role_execution_timeline"):
        click.echo("  No role execution timeline recorded.")
        return
    click.echo("\nRole execution timeline:")
    for item in replay["role_execution_timeline"]:
        role_display = _format_role_display(
            str(item.get("role") or ""),
            str(item.get("role_label") or item.get("role") or ""),
        )
        click.echo(
            "  - "
            f"{role_display}@{item.get('stage')} "
            f"checkpoint={item.get('checkpoint_id') or '-'} "
            f"[{item.get('status')}] "
            f"evidence={', '.join(item.get('evidence_refs') or []) or '-'}"
        )
        if item.get("artifact_preview"):
            click.echo(f"    artifact={item['artifact_preview']}")
    if replay.get("role_task_timeline"):
        click.echo("\nRole task envelopes:")
        for item in replay["role_task_timeline"]:
            role_display = _format_role_display(
                str(item.get("role") or ""),
                str(item.get("role_label") or item.get("role") or ""),
            )
            click.echo(
                "  - "
                f"{item.get('task_key') or '-'} "
                f"role={role_display} "
                f"stage={item.get('stage') or '-'} "
                f"[{item.get('status') or '-'}] "
                f"depends_on={', '.join(item.get('depends_on') or []) or '-'} "
                f"checkpoint={item.get('checkpoint_id') or '-'} "
                f"resume={item.get('resume_checkpoint_id') or '-'} "
                f"retry_budget={item.get('retry_budget', 0)} "
                f"evidence={', '.join(item.get('evidence_refs') or []) or '-'}"
            )
    if replay.get("role_task_bridge_timeline"):
        click.echo("\nRole runtime bridge:")
        for item in replay["role_task_bridge_timeline"]:
            payload = dict(item.get("payload") or {})
            role_display = _format_role_display(
                str(item.get("role") or ""),
                str(item.get("role_label") or item.get("role") or ""),
            )
            click.echo(
                "  - "
                f"{item.get('task_key') or '-'} "
                f"role={role_display} "
                f"type={item.get('task_type') or '-'} "
                f"source={item.get('source') or '-'} "
                f"priority={item.get('priority', 0)} "
                f"timeout={item.get('timeout_seconds', 0)} "
                f"max_attempts={item.get('max_attempts', 0)} "
                f"parent_task={payload.get('parent_task_id') or '-'} "
                f"depends_on={', '.join(payload.get('depends_on') or []) or '-'} "
                f"evidence={', '.join(item.get('evidence_refs') or []) or '-'}"
            )
    if replay.get("role_recovery_timeline"):
        click.echo("\nRole recovery timeline:")
        for item in replay["role_recovery_timeline"]:
            click.echo(
                "  - "
                f"{item.get('failed_role')}->{item.get('recovery_role')} "
                f"stage={item.get('stage')} "
                f"[{item.get('status')}] "
                f"reason={item.get('reason')} "
                f"resume={item.get('resume_checkpoint_id') or '-'} "
                f"attempt={item.get('attempt_number', 0)}/{item.get('budget_limit', 0)} "
                f"remaining={item.get('remaining_budget', 0)} "
                f"restored_messages={item.get('restored_messages', 0)} "
                f"restored_evidence={item.get('restored_evidence_count', 0)} "
                f"evidence={', '.join(item.get('evidence_refs') or []) or '-'}"
            )
    if replay.get("role_resume_timeline"):
        click.echo("\nPersistent resumes:")
        for item in replay["role_resume_timeline"]:
            click.echo(
                "  - "
                f"{item.get('role') or '-'}@{item.get('stage') or '-'} "
                f"resume={item.get('resume_checkpoint_id') or '-'} "
                f"source_run={item.get('source_workflow_run_id', 0)} "
                f"[{item.get('status') or '-'}] "
                f"source_status={item.get('source_status') or '-'} "
                f"failure={item.get('failure_reason') or '-'} "
                f"restored_evidence={item.get('restored_evidence_count', 0)} "
                f"evidence={', '.join(item.get('evidence_refs') or []) or '-'}"
            )


@cli.command()
@click.option(
    "--kind",
    "selected_kind",
    type=click.Choice(["all", "tool", "skill", "workflow"]),
    default="all",
    show_default=True,
    help="Filter the catalog to one section.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format.",
)
def capabilities(selected_kind: str, output_format: str) -> None:
    """Show built-in tools, skills, and default workflows."""
    from nanoclaw.core.capabilities import render_capability_json, render_capability_text

    if output_format == "json":
        click.echo(render_capability_json(selected_kind))
        return
    click.echo(render_capability_text(selected_kind))


@cli.group()
def persona() -> None:
    """Inspect or update protected persona fragments."""
    pass


@persona.command("show")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format.",
)
def persona_show(output_format: str) -> None:
    """Show protected persona fragments."""
    from nanoclaw.core.persona import PersonaStore, render_persona_json, render_persona_text

    store = PersonaStore()
    fragments = store.load()
    if output_format == "json":
        click.echo(render_persona_json(fragments, store.path))
        return
    click.echo(render_persona_text(fragments, store.path))


@persona.command("apply-review")
@click.option(
    "--summary",
    help="Reviewed summary content using identity:/style:/workflow:/config: lines or JSON.",
)
@click.option(
    "--file",
    "summary_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to one reviewed summary file.",
)
@click.option(
    "--source",
    default="reviewed_summary",
    show_default=True,
    help="Short source label stored with the update.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format.",
)
def persona_apply_review(
    summary: str | None,
    summary_file: Path | None,
    source: str,
    output_format: str,
) -> None:
    """Apply one reviewed summary to protected persona fragments."""
    from nanoclaw.core.persona import PersonaStore, render_persona_json, render_persona_text

    if bool(summary) == bool(summary_file):
        raise click.ClickException("Provide exactly one of --summary or --file.")

    review_text = str(summary or "").strip()
    if summary_file is not None:
        review_text = summary_file.read_text(encoding="utf-8")

    store = PersonaStore()
    try:
        fragments = store.apply_review_summary(review_text, source=source)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if output_format == "json":
        click.echo(render_persona_json(fragments, store.path))
        return
    click.echo(render_persona_text(fragments, store.path))


@cli.command()
@click.option(
    "--kind",
    "selected_kind",
    type=click.Choice(["all", "skill", "channel", "search_provider"]),
    default="all",
    show_default=True,
    help="Filter the extension catalog to one section.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format.",
)
def extensions(selected_kind: str, output_format: str) -> None:
    """Show manifest-backed installable extensions."""
    from nanoclaw.core.extensions import render_extension_json, render_extension_text

    if output_format == "json":
        click.echo(render_extension_json(selected_kind))
        return
    click.echo(render_extension_text(selected_kind))


@cli.command("extension-install")
@click.argument(
    "source_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--overwrite/--no-overwrite",
    default=False,
    show_default=True,
    help="Replace an existing installed extension with the same files.",
)
def extension_install(source_path: Path, overwrite: bool) -> None:
    """Install one local extension manifest or signed bundle into ~/.nanoclaw/extensions."""
    from nanoclaw.core.extension_installer import (
        install_extension_bundle,
        install_extension_manifest,
    )
    from nanoclaw.core.config import get_config
    from nanoclaw.core.plugins import reset_plugin_registry
    from nanoclaw.channels.registry import reset_channel_runtime_registry
    from nanoclaw.tools.search_providers import reset_search_provider_registry

    is_bundle = zipfile.is_zipfile(source_path)
    if is_bundle:
        try:
            policy = get_config().extensions
            trusted_publishers = dict(policy.trusted_publishers or {})
            require_signed_bundles = bool(policy.require_signed_bundles)
        except Exception:
            policy = None
            trusted_publishers = {}
            require_signed_bundles = False
        result = install_extension_bundle(
            source_path,
            overwrite=overwrite,
            trusted_publishers=trusted_publishers,
            require_signed_bundles=require_signed_bundles,
            publisher_policy=policy,
        )
    else:
        result = install_extension_manifest(source_path, overwrite=overwrite)
    reset_plugin_registry()
    reset_channel_runtime_registry()
    reset_search_provider_registry()
    click.echo(
        f"Installed {result['kind']} `{result['name']}` to {result['manifest_path']}\n"
        f"Trust: {result['trust_status']}\n"
        f"Distribution: {result['distribution_type']} "
        f"version={result.get('version') or '-'} "
        f"publisher={result['publisher'] or '-'} "
        f"keyId={result.get('key_id') or '-'} "
        f"signatureVerified={str(bool(result['signature_verified'])).lower()}\n"
        f"Registry: {result.get('registry_source') or '-'}\n"
        f"Files: {result['installed_files']}\n"
        f"Verify: {result['verify_command']}"
    )


@cli.command("extension-pack")
@click.argument(
    "manifest_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Bundle output path, for example ./demo_provider.ncext.zip",
)
@click.option(
    "--publisher",
    default="",
    help="Publisher name to embed in the bundle signature.",
)
@click.option(
    "--key-id",
    default="default",
    show_default=True,
    help="Publisher key id to embed in the bundle signature.",
)
@click.option(
    "--secret-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Read the publisher shared secret from a local file.",
)
def extension_pack(
    manifest_path: Path,
    output_path: Path,
    publisher: str,
    key_id: str,
    secret_file: Path | None,
) -> None:
    """Pack one local channel/search-provider extension into a distributable bundle."""
    from nanoclaw.core.extension_installer import pack_extension_manifest

    shared_secret = ""
    if secret_file is not None:
        shared_secret = secret_file.read_text(encoding="utf-8").strip()
    result = pack_extension_manifest(
        manifest_path,
        output_path=output_path,
        publisher=publisher.strip(),
        key_id=key_id.strip(),
        shared_secret=shared_secret,
    )
    click.echo(
        f"Packed {result['kind']} `{result['name']}` to {result['bundle_path']}\n"
        f"Signed: {str(bool(result['signed'])).lower()} "
        f"publisher={result['publisher'] or '-'} "
        f"keyId={result.get('key_id') or '-'} files={result['file_count']}"
    )


@cli.command("extension-verify")
@click.option(
    "--name",
    default="",
    help="Verify only one installed extension by primary name.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format.",
)
def extension_verify(name: str, output_format: str) -> None:
    """Verify installed local channel/search-provider extensions against trust receipts."""
    from nanoclaw.core.extension_installer import verify_installed_extensions
    from nanoclaw.core.plugins import get_user_extension_dir

    try:
        from nanoclaw.core.config import get_config

        policy = get_config().extensions
    except Exception:
        policy = type(
            "_DefaultExtensionPolicy",
            (),
            {
                "require_install_receipt": True,
                "require_signed_bundles": False,
                "max_risk_level": "medium",
            },
        )()
    results = verify_installed_extensions(
        base_dir=get_user_extension_dir(),
        selected_name=name.strip(),
        require_install_receipt=bool(policy.require_install_receipt),
        require_signed_bundles=bool(policy.require_signed_bundles),
        max_risk_level=str(policy.max_risk_level or "medium"),
        publisher_policy=policy,
    )
    if output_format == "json":
        click.echo(json.dumps(results, indent=2, sort_keys=True))
    elif not results:
        click.echo("No installed user channel/search-provider extensions found.")
    else:
        for item in results:
            click.echo(
                f"{item['kind']}:{item['name']} "
                f"status={item['status']} "
                f"risk={item['risk_level']} "
                f"sandbox={item.get('sandbox_policy') or '-'} "
                f"permissions={', '.join(item.get('permissions') or []) or '-'} "
                f"version={item.get('version') or '-'} "
                f"distribution={item.get('distribution_type') or '-'} "
                f"publisher={item.get('publisher') or '-'} "
                f"keyId={item.get('key_id') or '-'} "
                f"registry={item.get('registry_source') or '-'} "
                f"signatureVerified={str(bool(item.get('signature_verified', False))).lower()}"
            )
            click.echo(f"  {item['reason']}")
    if any(not item.get("allowed", False) for item in results):
        raise SystemExit(1)


@cli.command("extension-registry")
@click.option(
    "--url",
    "registry_url",
    default="",
    help="Registry JSON URL or local file path. Defaults to extensions.registryUrl.",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    help="Output format.",
)
def extension_registry(registry_url: str, output_format: str) -> None:
    """List the configured remote extension registry with local update status."""
    from nanoclaw.core.extension_registry import list_registry_entries
    from nanoclaw.core.plugins import get_user_extension_dir

    try:
        from nanoclaw.core.config import get_config

        source = registry_url.strip() or str(get_config().extensions.registry_url or "").strip()
    except Exception:
        source = registry_url.strip()
    if not source:
        raise click.ClickException("No extension registry configured. Set extensions.registryUrl.")

    entries = list_registry_entries(
        source=source,
        install_dir=get_user_extension_dir(),
    )
    if output_format == "json":
        click.echo(json.dumps({"source": source, "entries": entries}, indent=2, sort_keys=True))
        return
    click.echo(f"Extension registry: {source}")
    click.echo(f"Entries: {len(entries)}")
    for item in entries:
        click.echo(
            f"{item['kind']}:{item['name']} "
            f"status={item['status']} "
            f"version={item.get('version') or '-'} "
            f"installed={item.get('installed_version') or '-'} "
            f"publisher={item.get('publisher') or '-'}"
        )
        if item.get("summary"):
            click.echo(f"  {item['summary']}")


@cli.command("extension-update")
@click.option(
    "--name",
    required=True,
    help="Primary extension name from the configured registry.",
)
@click.option(
    "--url",
    "registry_url",
    default="",
    help="Registry JSON URL or local file path. Defaults to extensions.registryUrl.",
)
@click.option(
    "--overwrite/--no-overwrite",
    default=True,
    show_default=True,
    help="Replace installed extension files when applying the registry bundle.",
)
def extension_update(name: str, registry_url: str, overwrite: bool) -> None:
    """Install or update one signed extension bundle from the remote registry."""
    from nanoclaw.channels.registry import reset_channel_runtime_registry
    from nanoclaw.core.extension_registry import install_registry_extension
    from nanoclaw.core.plugins import get_user_extension_dir, reset_plugin_registry
    from nanoclaw.tools.search_providers import reset_search_provider_registry

    try:
        from nanoclaw.core.config import get_config

        policy = get_config().extensions
        source = registry_url.strip() or str(policy.registry_url or "").strip()
        trusted_publishers = dict(policy.trusted_publishers or {})
    except Exception:
        policy = None
        source = registry_url.strip()
        trusted_publishers = {}
    if not source:
        raise click.ClickException("No extension registry configured. Set extensions.registryUrl.")

    result = install_registry_extension(
        source=source,
        name=name,
        install_dir=get_user_extension_dir(),
        overwrite=overwrite,
        trusted_publishers=trusted_publishers,
        publisher_policy=policy,
    )
    reset_plugin_registry()
    reset_channel_runtime_registry()
    reset_search_provider_registry()
    click.echo(
        f"Updated {result['kind']} `{result['name']}` from registry {source}\n"
        f"Version: {result.get('version') or '-'}\n"
        f"Distribution: {result['distribution_type']} "
        f"publisher={result['publisher'] or '-'} "
        f"keyId={result.get('key_id') or '-'} "
        f"signatureVerified={str(bool(result['signature_verified'])).lower()}\n"
        f"Verify: {result['verify_command']}"
    )


@cli.command()
def doctor() -> None:
    """Run security check."""
    asyncio.run(run_doctor())


async def run_doctor() -> None:
    """Run security doctor checks."""
    from nanoclaw.security.doctor import SecurityDoctor

    doctor = SecurityDoctor()
    results = await doctor.check_all()
    click.echo(doctor.format_report(results))


@cli.command("container-check")
@click.option(
    "--backend",
    type=click.Choice(["docker", "podman"]),
    default=None,
    help="Override the container backend to inspect.",
)
@click.option(
    "--image",
    default=None,
    help="Override the container image to inspect.",
)
@click.option(
    "--refresh/--cached",
    default=True,
    show_default=True,
    help="Force a fresh runtime/image check instead of using the cache.",
)
def container_check(
    backend: str | None,
    image: str | None,
    refresh: bool,
) -> None:
    """Check primary container backend readiness and show remediation steps."""
    success = asyncio.run(run_container_check(backend, image, refresh))
    if not success:
        raise SystemExit(1)


@cli.command("container-prepare")
@click.option(
    "--backend",
    type=click.Choice(["docker", "podman"]),
    default=None,
    help="Override the container backend to prepare.",
)
@click.option(
    "--image",
    default=None,
    help="Override the container image to prepare.",
)
@click.option(
    "--refresh/--cached",
    default=True,
    show_default=True,
    help="Force a fresh runtime/image check instead of using the cache.",
)
@click.option(
    "--pull/--no-pull",
    default=True,
    show_default=True,
    help="Allow the command to pull the configured image when it is missing.",
)
def container_prepare(
    backend: str | None,
    image: str | None,
    refresh: bool,
    pull: bool,
) -> None:
    """Prepare the primary container backend and re-check readiness."""
    success = asyncio.run(run_container_prepare(backend, image, refresh, pull))
    if not success:
        raise SystemExit(1)


@cli.command("container-runtime")
@click.option(
    "--backend",
    type=click.Choice(["docker", "podman"]),
    default=None,
    help="Override the container backend runtime to manage.",
)
@click.option(
    "--image",
    default=None,
    help="Override the container image used for follow-up readiness checks.",
)
@click.option(
    "--refresh/--cached",
    default=True,
    show_default=True,
    help="Force a fresh runtime/image check instead of using the cache.",
)
@click.option(
    "--start/--no-start",
    default=True,
    show_default=True,
    help="Attempt to start the runtime when it is unreachable.",
)
@click.option(
    "--restart/--no-restart",
    default=False,
    show_default=True,
    help="Attempt a runtime restart when drift or loss is detected.",
)
@click.option(
    "--prepare/--no-prepare",
    default=True,
    show_default=True,
    help="Run the image-preparation step after the runtime becomes reachable.",
)
@click.option(
    "--pull/--no-pull",
    default=False,
    show_default=True,
    help="Allow image pull during the follow-up preparation step.",
)
@click.option(
    "--wait-seconds",
    default=30,
    show_default=True,
    type=int,
    help="Maximum time to wait for the runtime to become reachable.",
)
def container_runtime(
    backend: str | None,
    image: str | None,
    refresh: bool,
    start: bool,
    restart: bool,
    prepare: bool,
    pull: bool,
    wait_seconds: int,
) -> None:
    """Manage runtime lifecycle for the primary container backend."""
    success = asyncio.run(
        run_container_runtime(
            backend,
            image,
            refresh,
            start,
            restart,
            prepare,
            pull,
            wait_seconds,
        )
    )
    if not success:
        raise SystemExit(1)


async def run_container_check(
    backend: str | None,
    image: str | None,
    refresh: bool,
) -> bool:
    """Inspect container readiness and print remediation guidance."""
    from nanoclaw.core.config import get_config
    from nanoclaw.security.sandbox_backends import (
        PRIMARY_CONTAINER_BACKEND,
        get_container_remediation_plan,
        inspect_container_backend_health,
    )

    try:
        config = get_config()
        configured_image = config.tools.shell.container_image
    except FileNotFoundError:
        configured_image = ""

    selected_backend = backend or PRIMARY_CONTAINER_BACKEND
    selected_image = image if image is not None else configured_image
    health = inspect_container_backend_health(
        backend=selected_backend,
        container_image=selected_image,
        force_refresh=refresh,
    )
    remediation = get_container_remediation_plan(
        health,
        backend=selected_backend,
        container_image=selected_image,
    )

    click.echo("\nContainer Readiness Check")
    click.echo("=" * 40)
    click.echo(f"Backend: {health['backend']}")
    click.echo(f"Image: {health['configured_image'] or '-'}")
    click.echo(f"Status: {health['status']}")
    click.echo(f"Runtime reachable: {'yes' if health['runtime_reachable'] else 'no'}")
    click.echo(f"Image present: {'yes' if health['image_present'] else 'no'}")
    click.echo(f"Detail: {health['detail'] or '-'}")

    if remediation["steps"] or remediation["commands"]:
        click.echo("\nRemediation:")
        for item in remediation["steps"]:
            click.echo(f"  - {item}")
        for command in remediation["commands"]:
            click.echo(f"  - Run: {command}")

    return bool(health["ready"])


async def run_container_prepare(
    backend: str | None,
    image: str | None,
    refresh: bool,
    pull: bool,
) -> bool:
    """Prepare container readiness and print provisioning actions."""
    from nanoclaw.core.config import get_config
    from nanoclaw.security.sandbox_backends import (
        PRIMARY_CONTAINER_BACKEND,
        prepare_container_backend,
    )

    try:
        config = get_config()
        configured_image = config.tools.shell.container_image
    except FileNotFoundError:
        configured_image = ""

    selected_backend = backend or PRIMARY_CONTAINER_BACKEND
    selected_image = image if image is not None else configured_image
    preparation = prepare_container_backend(
        backend=selected_backend,
        container_image=selected_image,
        force_refresh=refresh,
        allow_pull=pull,
    )
    before = preparation["health_before"]
    after = preparation["health_after"]
    remediation = preparation["remediation"]

    click.echo("\nContainer Preparation")
    click.echo("=" * 40)
    click.echo(f"Backend: {after['backend']}")
    click.echo(f"Image: {after['configured_image'] or '-'}")
    click.echo(f"Before: {before['status']} ({before['detail'] or '-'})")
    click.echo(f"After: {after['status']} ({after['detail'] or '-'})")
    click.echo(f"Runtime reachable: {'yes' if after['runtime_reachable'] else 'no'}")
    click.echo(f"Image present: {'yes' if after['image_present'] else 'no'}")

    actions = preparation["actions"]
    if actions:
        click.echo("\nActions:")
        for action in actions:
            status = "ok" if action["success"] else "failed"
            click.echo(f"  - {action['name']}: {status}")
            click.echo(f"    {action['command']}")
            if action["detail"]:
                click.echo(f"    {action['detail']}")

    if remediation["steps"] or remediation["commands"]:
        click.echo("\nNext steps:")
        for item in remediation["steps"]:
            click.echo(f"  - {item}")
        for command in remediation["commands"]:
            click.echo(f"  - Run: {command}")

    return bool(preparation["ready"])


async def run_container_runtime(
    backend: str | None,
    image: str | None,
    refresh: bool,
    start: bool,
    restart: bool,
    prepare: bool,
    pull: bool,
    wait_seconds: int,
) -> bool:
    """Manage container runtime lifecycle and print orchestration actions."""
    from nanoclaw.core.config import get_config
    from nanoclaw.security.sandbox_backends import (
        PRIMARY_CONTAINER_BACKEND,
        manage_container_runtime,
    )

    try:
        config = get_config()
        configured_image = config.tools.shell.container_image
    except FileNotFoundError:
        configured_image = ""

    selected_backend = backend or PRIMARY_CONTAINER_BACKEND
    selected_image = image if image is not None else configured_image
    lifecycle = manage_container_runtime(
        backend=selected_backend,
        container_image=selected_image,
        force_refresh=refresh,
        allow_start=start,
        allow_restart=restart,
        allow_prepare=prepare,
        allow_pull=pull,
        wait_timeout_seconds=max(0, wait_seconds),
    )
    before = lifecycle["health_before"]
    after = lifecycle["health_after"]
    remediation = lifecycle["remediation"]

    click.echo("\nContainer Runtime Orchestration")
    click.echo("=" * 40)
    click.echo(f"Backend: {after['backend']}")
    click.echo(f"Image: {after['configured_image'] or '-'}")
    click.echo(f"Before: {before['status']} ({before['detail'] or '-'})")
    click.echo(f"After: {after['status']} ({after['detail'] or '-'})")
    click.echo(f"Runtime reachable: {'yes' if after['runtime_reachable'] else 'no'}")
    click.echo(f"Image present: {'yes' if after['image_present'] else 'no'}")
    drift_state = after if after.get("drifted") else before if before.get("drifted") else None
    if drift_state:
        click.echo(
            f"Drift: {drift_state.get('lifecycle_state') or 'unknown'} "
            f"({drift_state.get('drift_reason') or '-'})"
        )

    actions = lifecycle["actions"]
    if actions:
        click.echo("\nActions:")
        for action in actions:
            status = "ok" if action["success"] else "failed"
            click.echo(f"  - {action['name']}: {status}")
            click.echo(f"    {action['command']}")
            if action["detail"]:
                click.echo(f"    {action['detail']}")

    if remediation["steps"] or remediation["commands"]:
        click.echo("\nNext steps:")
        for item in remediation["steps"]:
            click.echo(f"  - {item}")
        for command in remediation["commands"]:
            click.echo(f"  - Run: {command}")

    if prepare:
        return bool(lifecycle["ready"])
    return bool(lifecycle["runtime_ready"])


@cli.group()
def cron() -> None:
    """Manage scheduled tasks."""
    pass


@cron.command()
@click.option("--name", required=True, help="Job name")
@click.option("--message", required=True, help="Message to send to agent")
@click.option("--cron", "cron_expr", help="Cron expression (e.g., '0 9 * * *')")
@click.option("--every", type=int, help="Repeat every N seconds")
def add(name: str, message: str, cron_expr: str | None, every: int | None) -> None:
    """Add a scheduled task."""
    asyncio.run(add_cron_job(name, message, cron_expr, every))


async def add_cron_job(
    name: str, message: str, cron_expr: str | None, every: int | None
) -> None:
    """Add a cron job."""
    from nanoclaw.cron.scheduler import get_scheduler

    if not cron_expr and not every:
        click.echo("Error: Must specify --cron or --every")
        return

    scheduler = get_scheduler()
    job_id = await scheduler.add_job(
        name=name,
        message=message,
        cron_expr=cron_expr,
        interval_seconds=every,
    )
    click.echo(f"Created job #{job_id}: {name}")


@cron.command("list")
def list_jobs() -> None:
    """List all scheduled tasks."""
    asyncio.run(list_cron_jobs())


async def list_cron_jobs() -> None:
    """List cron jobs."""
    from nanoclaw.cron.scheduler import get_scheduler

    scheduler = get_scheduler()
    jobs = await scheduler.list_jobs()

    if not jobs:
        click.echo("No scheduled jobs.")
        return

    click.echo("\nScheduled Jobs:")
    click.echo("-" * 60)
    for job in jobs:
        status = "enabled" if job["enabled"] else "disabled"
        schedule = job["cron_expr"] or f"every {job['interval_seconds']}s"
        click.echo(f"#{job['id']} [{status}] {job['name']}")
        click.echo(f"   Schedule: {schedule}")
        click.echo(f"   Message: {job['message'][:50]}...")
        click.echo()


@cron.command()
@click.argument("job_id", type=int)
def remove(job_id: int) -> None:
    """Remove a scheduled task."""
    asyncio.run(remove_cron_job(job_id))


async def remove_cron_job(job_id: int) -> None:
    """Remove a cron job."""
    from nanoclaw.cron.scheduler import get_scheduler

    scheduler = get_scheduler()
    await scheduler.remove_job(job_id)
    click.echo(f"Removed job #{job_id}")


# --- Config management ---


@cli.command()
def config() -> None:
    """Interactive config editor."""
    config_path = Path.home() / ".nanoclaw" / "config.json"
    if not config_path.exists():
        click.echo("No config found. Run 'nanoclaw init' first.")
        return

    data = json.loads(config_path.read_text())

    def save() -> None:
        config_path.write_text(json.dumps(data, indent=2))

    while True:
        # Show current status
        click.echo("\n  Current configuration:")
        provider, model = _get_current_provider_info(data)
        click.echo(f"  Provider: {provider}")
        click.echo(f"  Model: {model}")
        tg_enabled = data.get("channels", {}).get("telegram", {}).get("enabled", False)
        fs_enabled = data.get("channels", {}).get("feishu", {}).get("enabled", False)
        click.echo(f"  Telegram: {'enabled' if tg_enabled else 'disabled'}")
        click.echo(f"  Feishu: {'enabled' if fs_enabled else 'disabled'}")
        web_search = data.get("tools", {}).get("webSearch", {})
        provider = web_search.get("provider", "rss")
        brave_key = web_search.get("apiKey", "")
        serper_key = web_search.get("serperApiKey", "")
        brave_state = "set" if brave_key else "not set"
        serper_state = "set" if serper_key else "not set"
        click.echo(
            "  Web search: "
            f"provider={provider}, brave_key={brave_state}, serper_key={serper_state}"
        )
        click.echo()

        options = [
            ("provider", "LLM Provider & Model"),
            ("telegram", "Telegram"),
            ("feishu", "Feishu"),
            ("tools", "Tools (web search, etc.)"),
            ("show", "Show full config (JSON)"),
            ("exit", "Exit"),
        ]
        choice = select(options, title="  Settings:", default=0)

        if choice == 0:
            _edit_provider(data, save)
        elif choice == 1:
            _edit_telegram(data, save)
        elif choice == 2:
            _edit_feishu(data, save)
        elif choice == 3:
            _edit_tools(data, save)
        elif choice == 4:
            masked = _mask_secrets(data)
            click.echo(json.dumps(masked, indent=2))
        elif choice == 5:
            break

    click.echo("Done.")


def _get_current_provider_info(data: dict) -> tuple[str, str]:
    """Get current provider and model from config."""
    providers = data.get("providers", {})
    if "deepseek" in providers:
        model = providers["deepseek"].get("defaultModel", "")
        return "DeepSeek", model
    elif "openrouter" in providers:
        model = data.get("agents", {}).get("defaults", {}).get("model", "")
        return "OpenRouter", model
    elif "anthropic" in providers:
        model = providers["anthropic"].get("defaultModel", "")
        return "Anthropic", model
    elif "openai" in providers:
        model = providers["openai"].get("defaultModel", "")
        base_url = providers["openai"].get("baseUrl", "")
        if base_url:
            return "Local/Custom", model
        return "OpenAI", model
    return "Not configured", ""


def _mask_secrets(obj: dict | list | str, key: str = "") -> dict | list | str:
    """Recursively mask sensitive values."""
    sensitive_keys = {
        "apikey",
        "token",
        "password",
        "secret",
        "sessiontoken",
        "appsecret",
        "verifytoken",
        "encryptkey",
    }

    if isinstance(obj, dict):
        return {k: _mask_secrets(v, k) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_mask_secrets(item, key) for item in obj]
    elif isinstance(obj, str) and key.lower() in sensitive_keys and obj:
        return obj[:4] + "****" + obj[-4:] if len(obj) > 8 else "****"
    return obj


def _edit_provider(data: dict, save: Callable[[], None]) -> None:
    """Edit LLM provider settings."""
    while True:
        click.echo()
        provider, model = _get_current_provider_info(data)
        click.echo(f"  Current: {provider} / {model}")
        click.echo()

        options = [
            ("openrouter", "OpenRouter"),
            ("anthropic", "Anthropic API"),
            ("openai", "OpenAI API"),
            ("deepseek", "DeepSeek API"),
            ("local", "Local model"),
            ("model", "Change model only"),
            ("back", "Back"),
        ]
        choice = select(options, title="  Select provider:", default=0)

        if choice == 6:  # Back
            return

        if choice == 5:  # Change model only
            _change_model_only(data, save)
            continue

        # Clear old providers
        data["providers"] = {}

        if choice == 0:  # OpenRouter
            click.echo()
            click.echo("  (leave empty to cancel)")
            api_key = click.prompt("  OpenRouter API key", default="", show_default=False)
            if not api_key:
                click.echo("  Cancelled.")
                continue
            models = [
                ("anthropic/claude-sonnet-4-5", "claude-sonnet-4.5"),
                ("anthropic/claude-opus-4-5", "claude-opus-4.5"),
                ("openai/gpt-5", "gpt-5"),
                ("google/gemini-3-pro", "gemini-3-pro"),
                ("deepseek/deepseek-chat", "deepseek-v3.2"),
                ("back", "Back"),
            ]
            click.echo()
            model_idx = select(models, title="  Model:", default=0)
            if models[model_idx][0] == "back":
                click.echo("  Cancelled.")
                continue
            data["providers"]["openrouter"] = {"apiKey": api_key}
            data["agents"] = {"defaults": {"model": models[model_idx][0]}}

        elif choice == 1:  # Anthropic
            click.echo()
            click.echo("  (leave empty to cancel)")
            api_key = click.prompt("  Anthropic API key", default="", show_default=False)
            if not api_key:
                click.echo("  Cancelled.")
                continue
            models = [
                ("claude-sonnet-4-5", "claude-sonnet-4.5"),
                ("claude-opus-4-5", "claude-opus-4.5"),
                ("claude-haiku-4-5", "claude-haiku-4.5"),
                ("back", "Back"),
            ]
            click.echo()
            model_idx = select(models, title="  Model:", default=0)
            if models[model_idx][0] == "back":
                click.echo("  Cancelled.")
                continue
            data["providers"]["anthropic"] = {
                "apiKey": api_key,
                "defaultModel": models[model_idx][0],
            }

        elif choice == 2:  # OpenAI
            click.echo()
            click.echo("  (leave empty to cancel)")
            api_key = click.prompt("  OpenAI API key", default="", show_default=False)
            if not api_key:
                click.echo("  Cancelled.")
                continue
            models = [
                ("gpt-5", "gpt-5"),
                ("gpt-5.2", "gpt-5.2"),
                ("gpt-5-mini", "gpt-5-mini"),
                ("gpt-5-nano", "gpt-5-nano"),
                ("back", "Back"),
            ]
            click.echo()
            model_idx = select(models, title="  Model:", default=0)
            if models[model_idx][0] == "back":
                click.echo("  Cancelled.")
                continue
            data["providers"]["openai"] = {
                "apiKey": api_key,
                "defaultModel": models[model_idx][0],
            }

        elif choice == 3:  # DeepSeek
            click.echo()
            click.echo("  (leave empty to cancel)")
            api_key = click.prompt(
                "  DeepSeek API key (platform.deepseek.com)",
                default="",
                show_default=False,
            )
            if not api_key:
                click.echo("  Cancelled.")
                continue
            models = [
                ("deepseek-chat", "deepseek-chat (V3)"),
                ("deepseek-reasoner", "deepseek-reasoner (R1)"),
                ("back", "Back"),
            ]
            click.echo()
            model_idx = select(models, title="  Model:", default=0)
            if models[model_idx][0] == "back":
                click.echo("  Cancelled.")
                continue
            data["providers"]["deepseek"] = {
                "apiKey": api_key,
                "defaultModel": models[model_idx][0],
            }

        elif choice == 4:  # Local
            click.echo()
            click.echo("  (leave both empty to cancel)")
            base_url = click.prompt("  Base URL", default="http://localhost:11434/v1")
            model = click.prompt("  Model name", default="llama3")
            # Only cancel if user explicitly cleared defaults
            if not base_url and not model:
                click.echo("  Cancelled.")
                continue
            data["providers"]["openai"] = {
                "apiKey": "not-needed",
                "defaultModel": model or "llama3",
                "baseUrl": base_url or "http://localhost:11434/v1",
            }

        save()
        click.echo("  Saved.")
        return  # Exit to main menu after saving


def _change_model_only(data: dict, save: Callable[[], None]) -> None:
    """Change model without changing provider."""
    providers = data.get("providers", {})

    if "deepseek" in providers:
        models = [
            ("deepseek-chat", "deepseek-chat (V3)"),
            ("deepseek-reasoner", "deepseek-reasoner (R1)"),
            ("back", "Back"),
        ]
        click.echo()
        model_idx = select(models, title="  Model:", default=0)
        if models[model_idx][0] == "back":
            return
        data["providers"]["deepseek"]["defaultModel"] = models[model_idx][0]

    elif "openrouter" in providers:
        models = [
            ("anthropic/claude-sonnet-4-5", "claude-sonnet-4.5"),
            ("anthropic/claude-opus-4-5", "claude-opus-4.5"),
            ("openai/gpt-5", "gpt-5"),
            ("google/gemini-3-pro", "gemini-3-pro"),
            ("deepseek/deepseek-chat", "deepseek-v3.2"),
            ("back", "Back"),
        ]
        click.echo()
        model_idx = select(models, title="  Model:", default=0)
        if models[model_idx][0] == "back":
            return
        if "agents" not in data:
            data["agents"] = {}
        if "defaults" not in data["agents"]:
            data["agents"]["defaults"] = {}
        data["agents"]["defaults"]["model"] = models[model_idx][0]

    elif "anthropic" in providers:
        models = [
            ("claude-sonnet-4-5", "claude-sonnet-4.5"),
            ("claude-opus-4-5", "claude-opus-4.5"),
            ("claude-haiku-4-5", "claude-haiku-4.5"),
            ("back", "Back"),
        ]
        click.echo()
        model_idx = select(models, title="  Model:", default=0)
        if models[model_idx][0] == "back":
            return
        data["providers"]["anthropic"]["defaultModel"] = models[model_idx][0]

    elif "openai" in providers:
        base_url = providers["openai"].get("baseUrl")
        if base_url:
            # Local - manual input
            click.echo()
            click.echo("  (leave empty to cancel)")
            model = click.prompt("  Model name", default="", show_default=False)
            if not model:
                click.echo("  Cancelled.")
                return
            data["providers"]["openai"]["defaultModel"] = model
        else:
            models = [
                ("gpt-5", "gpt-5"),
                ("gpt-5.2", "gpt-5.2"),
                ("gpt-5-mini", "gpt-5-mini"),
                ("gpt-5-nano", "gpt-5-nano"),
                ("back", "Back"),
            ]
            click.echo()
            model_idx = select(models, title="  Model:", default=0)
            if models[model_idx][0] == "back":
                return
            data["providers"]["openai"]["defaultModel"] = models[model_idx][0]
    else:
        click.echo("  No provider configured.")
        return

    save()
    click.echo("  Saved.")


def _edit_telegram(data: dict, save: Callable[[], None]) -> None:
    """Edit Telegram settings."""
    while True:
        click.echo()
        channels = data.get("channels", {})
        tg = channels.get("telegram", {})

        current_enabled = tg.get("enabled", False)
        current_users = tg.get("allowFrom", [])
        current_token = tg.get("token", "")
        masked_token = current_token[:4] + "****" if current_token else "(not set)"

        click.echo(f"  Enabled: {current_enabled}")
        click.echo(f"  Token: {masked_token}")
        click.echo(f"  Allowed users: {current_users}")
        click.echo()

        options = [
            ("toggle", f"{'Disable' if current_enabled else 'Enable'} Telegram"),
            ("token", "Change bot token"),
            ("users", "Edit allowed users"),
            ("back", "Back"),
        ]
        choice = select(options, title="  Edit:", default=0)

        if choice == 3:  # Back
            return

        if choice == 0:  # Toggle
            tg["enabled"] = not current_enabled
            click.echo(f"  Telegram {'enabled' if tg['enabled'] else 'disabled'}")
        elif choice == 1:  # Token
            click.echo()
            click.echo("  (leave empty to cancel)")
            token = click.prompt("  Bot token", default="", show_default=False)
            if not token:
                click.echo("  Cancelled.")
                continue
            tg["token"] = token
        elif choice == 2:  # Users
            click.echo()
            click.echo("  (leave empty to cancel)")
            users = click.prompt(
                "  Allowed user IDs (comma-separated)",
                default="",
                show_default=False,
            )
            if not users:
                click.echo("  Cancelled.")
                continue
            tg["allowFrom"] = [u.strip() for u in users.split(",")]

        if "channels" not in data:
            data["channels"] = {}
        data["channels"]["telegram"] = tg
        save()
        click.echo("  Saved.")


def _edit_feishu(data: dict, save: Callable[[], None]) -> None:
    """Edit Feishu settings."""
    while True:
        click.echo()
        channels = data.setdefault("channels", {})
        fs = channels.setdefault("feishu", {})

        current_enabled = fs.get("enabled", False)
        current_app_id = fs.get("appId", "")
        current_app_secret = fs.get("appSecret", "")
        current_verify_token = fs.get("verifyToken", "")
        current_host = fs.get("webhookHost", "0.0.0.0")
        current_port = fs.get("webhookPort", 15097)
        current_path = fs.get("webhookPath", "/feishu/events")
        current_allow_from = fs.get("allowFrom", [])
        current_default_chat = fs.get("defaultChatId", "")

        def _mask(value: str) -> str:
            if not value:
                return "(not set)"
            if len(value) <= 8:
                return "****"
            return value[:4] + "****" + value[-4:]

        click.echo(f"  Enabled: {current_enabled}")
        click.echo(f"  appId: {current_app_id or '(not set)'}")
        click.echo(f"  appSecret: {_mask(current_app_secret)}")
        click.echo(f"  verifyToken: {_mask(current_verify_token)}")
        click.echo(f"  webhook: http://{current_host}:{current_port}{current_path}")
        click.echo(f"  allowFrom: {current_allow_from}")
        click.echo(f"  defaultChatId: {current_default_chat or '(not set)'}")
        click.echo()

        options = [
            ("toggle", f"{'Disable' if current_enabled else 'Enable'} Feishu"),
            ("credentials", "Set appId/appSecret"),
            ("verify", "Set verifyToken"),
            ("webhook", "Set webhook host/port/path"),
            ("allow", "Edit allowFrom"),
            ("default_chat", "Set defaultChatId"),
            ("back", "Back"),
        ]
        choice = select(options, title="  Edit:", default=0)

        if choice == 6:  # Back
            return

        if choice == 0:  # Toggle
            fs["enabled"] = not current_enabled
            click.echo(f"  Feishu {'enabled' if fs['enabled'] else 'disabled'}")
            save()
            click.echo("  Saved.")
            continue

        if choice == 1:  # Credentials
            click.echo()
            app_id = click.prompt("  appId", default=current_app_id or "", show_default=False)
            app_secret = click.prompt(
                "  appSecret",
                default=current_app_secret or "",
                show_default=False,
            )
            if app_id:
                fs["appId"] = app_id
            if app_secret:
                fs["appSecret"] = app_secret
            save()
            click.echo("  Saved.")
            continue

        if choice == 2:  # verify token
            click.echo()
            verify_token = click.prompt(
                "  verifyToken",
                default=current_verify_token or "",
                show_default=False,
            )
            fs["verifyToken"] = verify_token
            save()
            click.echo("  Saved.")
            continue

        if choice == 3:  # webhook
            click.echo()
            host = click.prompt("  webhookHost", default=current_host)
            port = click.prompt("  webhookPort", type=int, default=int(current_port))
            path = click.prompt("  webhookPath", default=current_path)
            fs["webhookHost"] = host
            fs["webhookPort"] = int(port)
            fs["webhookPath"] = path if path.startswith("/") else f"/{path}"
            save()
            click.echo("  Saved.")
            continue

        if choice == 4:  # allow list
            click.echo()
            values = click.prompt(
                "  allowFrom values (comma-separated, empty clears all)",
                default=",".join(current_allow_from),
                show_default=False,
            )
            allow_list = [item.strip() for item in values.split(",") if item.strip()]
            fs["allowFrom"] = allow_list
            save()
            click.echo("  Saved.")
            continue

        if choice == 5:  # default chat id
            click.echo()
            default_chat_id = click.prompt(
                "  defaultChatId",
                default=current_default_chat or "",
                show_default=False,
            )
            fs["defaultChatId"] = default_chat_id
            save()
            click.echo("  Saved.")
            continue


def _edit_tools(data: dict, save: Callable[[], None]) -> None:
    """Edit tools settings."""
    while True:
        click.echo()
        tools = data.setdefault("tools", {})
        web_search = tools.setdefault("webSearch", {})

        provider = web_search.get("provider", "rss")
        current_key = web_search.get("apiKey", "")
        current_serper_key = web_search.get("serperApiKey", "")
        serper_max_calls = int(web_search.get("serperMaxCalls", 0) or 0)
        prefer_mainland = web_search.get("preferMainland", True)
        mainland_only = web_search.get("mainlandOnly", False)
        rss_concurrency = web_search.get("rssConcurrency", 8)
        rss_retries = web_search.get("rssRetries", 1)
        masked_key = current_key[:4] + "****" if current_key else "(not set)"
        masked_serper_key = (
            current_serper_key[:4] + "****" if current_serper_key else "(not set)"
        )

        click.echo(f"  Provider: {provider}")
        click.echo(f"  Brave Search API key: {masked_key}")
        click.echo(f"  Serper API key: {masked_serper_key}")
        click.echo(f"  Serper max calls: {serper_max_calls or 'unlimited'}")
        click.echo(f"  preferMainland: {prefer_mainland}")
        click.echo(f"  mainlandOnly: {mainland_only}")
        click.echo(f"  rssConcurrency: {rss_concurrency}")
        click.echo(f"  rssRetries: {rss_retries}")
        click.echo()

        options = [
            ("provider", "Change provider"),
            ("brave", "Set Brave Search API key"),
            ("serper", "Set Serper API key"),
            ("serper_limit", "Set Serper max calls"),
            ("prefer_mainland", "Toggle preferMainland"),
            ("mainland_only", "Toggle mainlandOnly"),
            ("rss_concurrency", "Set rssConcurrency"),
            ("rss_retries", "Set rssRetries"),
            ("back", "Back"),
        ]
        choice = select(options, title="  Edit:", default=0)

        if choice == 8:  # Back
            return

        if choice == 0:  # Provider
            click.echo()
            providers = [
                ("rss", "rss"),
                ("auto", "auto"),
                ("brave", "brave"),
                ("serper", "serper"),
                ("disabled", "disabled"),
            ]
            provider_idx = select(providers, title="  Provider:", default=0)
            web_search["provider"] = providers[provider_idx][0]
            if "rssSourcesPath" not in web_search:
                web_search["rssSourcesPath"] = "assets/rss-sources.json"
            if "preferMainland" not in web_search:
                web_search["preferMainland"] = True
            if "mainlandOnly" not in web_search:
                web_search["mainlandOnly"] = False
            save()
            click.echo("  Saved.")
            continue

        if choice == 1:  # Brave key
            click.echo()
            click.echo("  (leave empty to cancel)")
            api_key = click.prompt("  Brave Search API key", default="", show_default=False)
            if not api_key:
                click.echo("  Cancelled.")
                continue
            web_search["apiKey"] = api_key
            save()
            click.echo("  Saved.")
            continue

        if choice == 2:  # Serper key
            click.echo()
            click.echo("  (leave empty to cancel)")
            api_key = click.prompt("  Serper API key", default="", show_default=False)
            if not api_key:
                click.echo("  Cancelled.")
                continue
            web_search["serperApiKey"] = api_key
            save()
            click.echo("  Saved.")
            continue

        if choice == 3:  # serper_limit
            click.echo()
            value = click.prompt(
                "  Serper max calls (0 for unlimited)",
                type=int,
                default=int(serper_max_calls),
            )
            web_search["serperMaxCalls"] = max(0, int(value))
            save()
            click.echo(f"  serperMaxCalls set to {web_search['serperMaxCalls']}")
            continue

        if choice == 4:  # prefer_mainland
            web_search["preferMainland"] = not prefer_mainland
            save()
            click.echo(f"  preferMainland set to {web_search['preferMainland']}")
            continue

        if choice == 5:  # mainland_only
            web_search["mainlandOnly"] = not mainland_only
            save()
            click.echo(f"  mainlandOnly set to {web_search['mainlandOnly']}")
            continue

        if choice == 6:  # rss_concurrency
            click.echo()
            value = click.prompt(
                "  rssConcurrency (1-16)",
                type=int,
                default=int(rss_concurrency),
            )
            web_search["rssConcurrency"] = max(1, min(int(value), 16))
            save()
            click.echo(f"  rssConcurrency set to {web_search['rssConcurrency']}")
            continue

        if choice == 7:  # rss_retries
            click.echo()
            value = click.prompt(
                "  rssRetries (1-3)",
                type=int,
                default=int(rss_retries),
            )
            web_search["rssRetries"] = max(1, min(int(value), 3))
            save()
            click.echo(f"  rssRetries set to {web_search['rssRetries']}")
            continue


if __name__ == "__main__":
    cli()
