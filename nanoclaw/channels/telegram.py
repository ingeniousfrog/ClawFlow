"""Telegram bot channel using polling."""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

from nanoclaw.core.logger import get_logger

if TYPE_CHECKING:
    from nanoclaw.channels.gateway import Gateway
    from nanoclaw.core.config import TelegramConfig

logger = get_logger(__name__)


class TelegramChannel:
    """
    Async Telegram bot using python-telegram-bot v20+.
    Uses POLLING (not webhook) - no open ports needed.
    """

    def __init__(self, config: "TelegramConfig", gateway: "Gateway"):
        """
        Initialize TelegramChannel.

        Args:
            config: Telegram configuration
            gateway: Gateway for message routing
        """
        self.token = config.token
        self.allowed_users = set(config.allow_from)
        self.gateway = gateway
        self.app: Any = None
        self._pending_confirmations: dict[str, asyncio.Future] = {}

    async def start(self) -> bool:
        """Start Telegram bot with polling."""
        try:
            from telegram.ext import (
                ApplicationBuilder,
                CallbackQueryHandler,
                MessageHandler,
                filters,
            )
        except ImportError:
            logger.error("python-telegram-bot not installed")
            return False

        self.app = ApplicationBuilder().token(self.token).build()

        # Handle text messages
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_message)
        )

        # Handle /start command
        self.app.add_handler(
            MessageHandler(filters.Command(["start"]), self.handle_start)  # type: ignore[arg-type]
        )

        # Handle Yes/No button callbacks
        self.app.add_handler(CallbackQueryHandler(self.handle_confirmation))

        # Start polling
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

        logger.info("Telegram bot started (polling mode)")
        return True

    async def handle_start(self, update: Any, context: Any) -> None:
        """Handle /start command."""
        user_id = str(update.effective_user.id)

        if user_id not in self.allowed_users:
            await update.message.reply_text(
                "Not authorized. Your ID is not in the allow list.\n"
                f"Your user ID: {user_id}"
            )
            return

        await update.message.reply_text(
            "nanoClaw is ready!\n\n"
            "Send me a message and I'll help you out."
        )

    async def handle_message(self, update: Any, context: Any) -> None:
        """Handle incoming user message."""
        user_id = str(update.effective_user.id)
        message_text = update.message.text

        logger.debug(f"Telegram message from {user_id}: {message_text}")

        # Whitelist check
        if user_id not in self.allowed_users:
            await update.message.reply_text(
                "Not authorized. Your ID is not in the allow list."
            )
            return

        # Show typing indicator
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="typing"
        )

        # Route to agent via gateway
        logger.debug("Routing to agent...")
        response = await self.gateway.handle_incoming(
            channel_id="telegram",
            user_id=user_id,
            message=message_text,
            confirm_callback=lambda q: self._ask_confirmation(update, context, q),
        )

        logger.debug(f"Agent response: {response[:100]}...")
        # Send response
        await self._send_response(update, response)

    async def _ask_confirmation(
        self, update: Any, context: Any, question: str
    ) -> bool:
        """Send Yes/No inline keyboard to user. Wait for response."""
        try:
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        except ImportError:
            return False

        confirm_id = str(uuid.uuid4())
        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "Yes", callback_data=f"confirm:{confirm_id}:yes"
                    ),
                    InlineKeyboardButton(
                        "No", callback_data=f"confirm:{confirm_id}:no"
                    ),
                ]
            ]
        )

        await update.message.reply_text(question, reply_markup=keyboard)

        # Wait for callback (max 5 minutes)
        future: asyncio.Future[bool] = asyncio.get_event_loop().create_future()
        self._pending_confirmations[confirm_id] = future

        try:
            result = await asyncio.wait_for(future, timeout=300)
            return result
        except asyncio.TimeoutError:
            return False  # Default: deny on timeout
        finally:
            self._pending_confirmations.pop(confirm_id, None)

    async def handle_confirmation(self, update: Any, context: Any) -> None:
        """Handle Yes/No button press."""
        query = update.callback_query
        data = query.data  # "confirm:{id}:yes" or "confirm:{id}:no"

        try:
            _, confirm_id, answer = data.split(":")
        except ValueError:
            return

        if confirm_id in self._pending_confirmations:
            self._pending_confirmations[confirm_id].set_result(answer == "yes")

        label = "Approved" if answer == "yes" else "Denied"
        await query.edit_message_text(f"{query.message.text}\n\n[{label}]")

    async def _send_response(self, update: Any, response: str) -> None:
        """Send response, splitting if necessary."""
        MAX_LEN = 4000  # Leave room for formatting

        if len(response) <= MAX_LEN:
            try:
                await update.message.reply_text(response, parse_mode="Markdown")
            except Exception:
                # Fallback without markdown if parsing fails
                await update.message.reply_text(response)
        else:
            # Split into chunks
            chunks = [
                response[i : i + MAX_LEN] for i in range(0, len(response), MAX_LEN)
            ]
            for chunk in chunks:
                try:
                    await update.message.reply_text(chunk, parse_mode="Markdown")
                except Exception:
                    await update.message.reply_text(chunk)

    async def send_proactive(self, text: str) -> None:
        """Send a proactive message (from cron or background task)."""
        if not self.app:
            return

        for user_id in self.allowed_users:
            try:
                await self.app.bot.send_message(
                    chat_id=int(user_id),
                    text=text,
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error(f"Failed to send proactive message to {user_id}: {e}")

    async def stop(self) -> None:
        """Graceful shutdown."""
        if self.app:
            try:
                await self.app.updater.stop()
                await self.app.stop()
                await self.app.shutdown()
            except Exception as e:
                logger.error(f"Error stopping Telegram bot: {e}")
