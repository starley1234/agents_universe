"""Интеграция с Telegram Bot API.

ОПАСНЫЕ ДЕЙСТВИЯ: отправка сообщения помечена dangerous=True.
Поддерживается автономный режим (mock mode) для выполнения тестов без сетевого токена.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from ..core import Tool, ToolError


@dataclass
class TelegramConfig:
    token: str = ""
    api_url: str = "https://api.telegram.org"
    mock_mode: bool = True  # По умолчанию mock


class TelegramService:
    def __init__(self, cfg: TelegramConfig | None = None) -> None:
        self.cfg = cfg or TelegramConfig()
        self.sent_messages: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self.updates: list[dict[str, Any]] = [
            {"update_id": 1, "message": {"chat": {"id": "1001"}, "text": "Привет"}},
            {"update_id": 2, "message": {"chat": {"id": "1001"}, "text": "Сделай отчёт"}},
        ]

    def send_message(self, chat_id: str, text: str) -> str:
        with self._lock:
            if not chat_id or not text:
                raise ToolError("chat_id и text являются обязательными")
            if self.cfg.mock_mode:
                self.sent_messages.append({"chat_id": chat_id, "text": text})
                return (
                    f"[MOCK TELEGRAM] Сообщение отправлено в чат {chat_id!r} "
                    f"({len(text)} символов)"
                )

            url = f"{self.cfg.api_url}/bot{self.cfg.token}/sendMessage"
            payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if not data.get("ok"):
                        raise ToolError(f"Ошибка от Telegram API: {data.get('description')}")
                    msg_id = data.get("result", {}).get("message_id", 0)
                    return f"Сообщение в Telegram отправлено (message_id={msg_id})"
            except (urllib.error.URLError, OSError) as exc:
                raise ToolError(f"Ошибка Telegram Bot API при отправке: {exc}") from exc

    def get_updates(self, limit: int = 5) -> str:
        with self._lock:
            if self.cfg.mock_mode:
                items = self.updates[:limit]
                if not items:
                    return "(Новых сообщений в Telegram нет)"
                lines = []
                for it in items:
                    msg = it.get("message", {})
                    chat_id = msg.get("chat", {}).get("id")
                    txt = msg.get("text", "")
                    lines.append(f"- [chat {chat_id}]: {txt}")
                return "Сообщения Telegram:\n" + "\n".join(lines)

            url = f"{self.cfg.api_url}/bot{self.cfg.token}/getUpdates?limit={limit}"
            try:
                with urllib.request.urlopen(url, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    items = data.get("result", [])
                    lines = [
                        f"- [chat {u.get('message', {}).get('chat', {}).get('id')}]: "
                        f"{u.get('message', {}).get('text', '')}"
                        for u in items
                    ]
                    return (
                        "Сообщения Telegram:\n" + "\n".join(lines)
                        if lines
                        else "(Новых сообщений нет)"
                    )
            except (urllib.error.URLError, OSError) as exc:
                raise ToolError(f"Ошибка получения обновлений Telegram: {exc}") from exc


def build_telegram_tools(service: TelegramService | None = None) -> list[Tool]:
    """Собрать инструменты для взаимодействия с Telegram Bot API."""
    srv = service or TelegramService()

    def send_message(chat_id: str, text: str) -> str:
        return srv.send_message(chat_id, text)

    def get_updates(limit: int = 5) -> str:
        return srv.get_updates(limit)

    return [
        Tool(
            name="telegram.send_message",
            description="Отправить сообщение в Telegram чат по chat_id. Опасное действие (dangerous=True).",
            parameters={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "ID чата Telegram"},
                    "text": {"type": "string", "description": "Текст сообщения"},
                },
                "required": ["chat_id", "text"],
            },
            fn=send_message,
            skills=["messaging", "telegram", "chat", "bot", "communication", "integrations"],
            attributes={
                "category": "messaging",
                "channel": "telegram",
                "read_only": False,
                "dangerous": True,
                "requires_network": True,
                "resource_type": "message",
                "speed": "fast",
                "tags": ["telegram", "tg", "bot", "send", "chat", "communication"],
            },
            example='telegram.send_message(chat_id="12345", text="Аудит готов.")',
        ),
        Tool(
            name="telegram.get_updates",
            description="Получить список последних входящих сообщений бота в Telegram.",
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Число сообщений (по умолчанию 5)",
                    }
                },
            },
            fn=get_updates,
            skills=["messaging", "telegram", "chat", "bot", "communication", "integrations"],
            attributes={
                "category": "messaging",
                "channel": "telegram",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "message",
                "speed": "fast",
                "tags": ["telegram", "tg", "bot", "read", "inbox", "chat"],
            },
            example="telegram.get_updates(limit=5)",
        ),
    ]
