"""Интеграция с мессенджером MAX Bot API (https://dev.max.ru/docs-api).

ОПАСНЫЕ ДЕЙСТВИЯ: отправка сообщения в чат помечена dangerous=True.
Поддерживается автономный режим (mock mode) для тестов без сетевого токена.
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
class MaxConfig:
    token: str = ""
    api_url: str = "https://platform-api2.max.ru"
    mock_mode: bool = True  # По умолчанию mock для автономных тестов


class MaxService:
    def __init__(self, cfg: MaxConfig | None = None) -> None:
        self.cfg = cfg or MaxConfig()
        self.sent_messages: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self.updates: list[dict[str, Any]] = [
            {"update_id": 101, "chat_id": "chat-1", "text": "Привет! Нужен отчёт."},
            {"update_id": 102, "chat_id": "chat-1", "text": "Аудит полки №4"},
        ]

    def send_message(self, chat_id: str, text: str) -> str:
        with self._lock:
            if not chat_id or not text:
                raise ToolError("chat_id и text являются обязательными")
            if self.cfg.mock_mode:
                self.sent_messages.append({"chat_id": chat_id, "text": text})
                return (
                    f"[MOCK MAX] Сообщение в чат {chat_id!r} отправлено "
                    f"({len(text)} символов)"
                )

            url = f"{self.cfg.api_url}/bot/v1/messages"
            payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Authorization": f"Bearer {self.cfg.token}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    msg_id = data.get("message_id", "ok")
                    return f"Сообщение в MAX отправлено (ID: {msg_id})"
            except (urllib.error.URLError, OSError) as exc:
                raise ToolError(f"Ошибка API MAX при отправке сообщения: {exc}") from exc

    def get_updates(self, limit: int = 5) -> str:
        with self._lock:
            if self.cfg.mock_mode:
                msgs = self.updates[:limit]
                if not msgs:
                    return "(Новых сообщений в MAX нет)"
                lines = [
                    f"- [id={m['update_id']}] чат {m['chat_id']}: {m['text']}"
                    for m in msgs
                ]
                return "Сообщения MAX:\n" + "\n".join(lines)

            url = f"{self.cfg.api_url}/bot/v1/updates?limit={limit}"
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self.cfg.token}"}
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    items = data.get("updates", [])
                    lines = [
                        f"- [id={u.get('update_id')}] чат {u.get('chat_id')}: {u.get('text')}"
                        for u in items
                    ]
                    return (
                        "Сообщения MAX:\n" + "\n".join(lines)
                        if lines
                        else "(Новых сообщений нет)"
                    )
            except (urllib.error.URLError, OSError) as exc:
                raise ToolError(f"Ошибка получения обновлений MAX: {exc}") from exc


def build_max_tools(service: MaxService | None = None) -> list[Tool]:
    """Собрать инструменты взаимодействия с мессенджером MAX."""
    srv = service or MaxService()

    def send_message(chat_id: str, text: str) -> str:
        return srv.send_message(chat_id, text)

    def get_updates(limit: int = 5) -> str:
        return srv.get_updates(limit)

    return [
        Tool(
            name="max.send_message",
            description="Отправить сообщение в чат мессенджера MAX (https://dev.max.ru/docs-api).",
            parameters={
                "type": "object",
                "properties": {
                    "chat_id": {"type": "string", "description": "ID чата в MAX"},
                    "text": {"type": "string", "description": "Текст сообщения"},
                },
                "required": ["chat_id", "text"],
            },
            fn=send_message,
            skills=["messaging", "max", "chat", "communication", "integrations"],
            attributes={
                "category": "messaging",
                "channel": "max",
                "read_only": False,
                "dangerous": True,
                "requires_network": True,
                "resource_type": "message",
                "speed": "fast",
                "tags": ["max", "messaging", "chat", "send", "communication"],
            },
            example='max.send_message(chat_id="dev-chat", text="Привет от агента!")',
        ),
        Tool(
            name="max.get_updates",
            description="Получить последние сообщения/обновления бота MAX.",
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
            skills=["messaging", "max", "chat", "communication", "integrations"],
            attributes={
                "category": "messaging",
                "channel": "max",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "message",
                "speed": "fast",
                "tags": ["max", "messaging", "chat", "read", "inbox"],
            },
            example="max.get_updates(limit=5)",
        ),
    ]
