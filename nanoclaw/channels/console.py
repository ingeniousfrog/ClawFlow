"""Console channel for CLI testing."""

from __future__ import annotations

import asyncio
from typing import Optional

import click

from nanoclaw.core.logger import get_logger

logger = get_logger(__name__)


class ConsoleChannel:
    """Console-based channel for CLI interaction."""

    def __init__(self, gateway: Optional[object] = None):
        """
        Initialize ConsoleChannel.

        Args:
            gateway: Gateway instance for message routing
        """
        self.gateway = gateway
        self.running = False

    async def start(self) -> None:
        """Start console interaction."""
        self.running = True
        click.echo("Console channel started. Type 'exit' to quit.\n")

        while self.running:
            try:
                user_input = await asyncio.to_thread(
                    click.prompt, "You", prompt_suffix="> "
                )
            except (EOFError, KeyboardInterrupt):
                break

            if user_input.lower() in ("exit", "quit"):
                break

            if self.gateway:
                response = await self.gateway.handle_incoming(  # type: ignore[attr-defined]
                    channel_id="console",
                    user_id="cli_user",
                    message=user_input,
                    confirm_callback=self._confirm,
                )
                click.echo(f"\nAssistant: {response}\n")

    async def _confirm(self, question: str) -> bool:
        """Prompt for confirmation."""
        click.echo(f"\n{question}")
        return click.confirm("Allow?", default=False)

    async def send_proactive(self, text: str) -> None:
        """Display proactive message."""
        click.echo(f"\n[Proactive Message]\n{text}\n")

    async def stop(self) -> None:
        """Stop console channel."""
        self.running = False
