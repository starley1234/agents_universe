"""Telegram notification sender via Bot API."""

from __future__ import annotations

import httpx
from loguru import logger

from astra.config import settings


class TelegramNotifier:
    """Sends messages via Telegram Bot API."""

    @property
    def _base_url(self) -> str:
        return f"https://api.telegram.org/bot{settings.telegram_bot_token}"

    async def send(self, chat_id: str | int, text: str, parse_mode: str = "HTML") -> None:
        """Send a message to a Telegram chat."""
        if not settings.telegram_bot_token:
            logger.warning("Telegram bot token not configured, skipping notification")
            return

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self._base_url}/sendMessage",
                json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode},
            )
            if resp.status_code == 200:
                logger.info("📱  Telegram message sent to chat {}", chat_id)
            else:
                logger.error("Telegram send failed: {} {}", resp.status_code, resp.text)

    async def send_milestone(
        self,
        chat_id: str | int,
        project_name: str,
        title: str,
        content: str,
    ) -> None:
        text = (
            f"🎯 <b>Milestone Reached</b>\n"
            f"Project: <b>{project_name}</b>\n"
            f"{title}\n\n"
            f"{content[:3000]}"
        )
        await self.send(chat_id, text)


telegram_notifier = TelegramNotifier()
