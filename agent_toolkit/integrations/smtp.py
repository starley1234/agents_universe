"""Интеграция с электронной почтой: SMTP (отправка) и IMAP (чтение).

ОПАСНЫЕ ДЕЙСТВИЯ: отправка письма помечена dangerous=True, так как
отправленное сообщение невозможно отозвать.

Включает автономный тестовый режим (mock mode), позволяющий проверять
логику без реального подключения к почтовым серверам.
"""
from __future__ import annotations

import email.message
import imaplib
import smtplib
import threading
from dataclasses import dataclass
from typing import Any

from ..config import settings
from ..core import Tool, ToolError


@dataclass
class MailConfig:
    smtp_host: str = settings.smtp_host or "smtp.example.com"
    smtp_port: int = settings.smtp_port or 587
    imap_host: str = settings.imap_host or "imap.example.com"
    imap_port: int = settings.imap_port or 993
    username: str = settings.smtp_user or ""
    password: str = settings.smtp_password or ""
    mock_mode: bool = settings.mock_mode


class MailService:
    def __init__(self, cfg: MailConfig | None = None) -> None:
        self.cfg = cfg or MailConfig()
        self.sent_emails: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self.inbox_emails: list[dict[str, Any]] = [
            {
                "id": "1",
                "from": "alice@example.com",
                "subject": "Аудит завершён",
                "body": "Прикладываем протокол соответствия.",
            },
            {
                "id": "2",
                "from": "bob@example.com",
                "subject": "Вопрос по выкладке",
                "body": "Проверьте долю полки на стеллаже №3.",
            },
        ]

    def send(self, to_addr: str, subject: str, body: str) -> str:
        with self._lock:
            if not to_addr:
                raise ToolError("Не указан адрес получателя (to_addr)")
            if self.cfg.mock_mode:
                self.sent_emails.append(
                    {"to": to_addr, "subject": subject, "body": body}
                )
                return (
                    f"[MOCK] Письмо отправлено для {to_addr} "
                    f"(тема: {subject!r}, длина: {len(body)} симв.)"
                )

            msg = email.message.EmailMessage()
            msg["From"] = self.cfg.username
            msg["To"] = to_addr
            msg["Subject"] = subject
            msg.set_content(body)

            try:
                with smtplib.SMTP(self.cfg.smtp_host, self.cfg.smtp_port, timeout=10) as server:
                    server.starttls()
                    if self.cfg.username and self.cfg.password:
                        server.login(self.cfg.username, self.cfg.password)
                    server.send_message(msg)
            except (smtplib.SMTPException, OSError) as exc:
                raise ToolError(f"Ошибка SMTP при отправке письма: {exc}") from exc

            return f"Письмо для {to_addr} успешно отправлено"

    def read_inbox(self, limit: int = 5) -> str:
        with self._lock:
            if self.cfg.mock_mode:
                msgs = self.inbox_emails[:limit]
                if not msgs:
                    return "(Папка Входящие пуста)"
                lines = [
                    f"- [{m['id']}] от {m['from']} — {m['subject']}: {m['body']}"
                    for m in msgs
                ]
                return "Входящие сообщения:\n" + "\n".join(lines)

            try:
                with imaplib.IMAP4_SSL(self.cfg.imap_host, self.cfg.imap_port) as mail:
                    mail.login(self.cfg.username, self.cfg.password)
                    mail.select("INBOX")
                    _, data = mail.search(None, "ALL")
                    ids = data[0].split()
                    if not ids:
                        return "(Папка Входящие пуста)"
                    lines = []
                    for msg_id in ids[-limit:]:
                        _, msg_data = mail.fetch(msg_id, "(RFC822)")
                        for resp_part in msg_data:
                            if isinstance(resp_part, tuple):
                                m = email.message_from_bytes(resp_part[1])
                                subj = m.get("Subject", "")
                                frm = m.get("From", "")
                                lines.append(f"- От {frm}: {subj}")
                    return "Входящие сообщения:\n" + "\n".join(lines)
            except (imaplib.IMAP4.error, OSError) as exc:
                raise ToolError(f"Ошибка IMAP при чтении почты: {exc}") from exc


def build_smtp_tools(service: MailService | None = None) -> list[Tool]:
    """Собрать инструменты для работы с электронной почтой (SMTP/IMAP)."""
    mail = service or MailService()

    def send_email(to_addr: str, subject: str, body: str) -> str:
        return mail.send(to_addr, subject, body)

    def read_emails(limit: int = 5) -> str:
        return mail.read_inbox(limit=limit)

    return [
        Tool(
            name="smtp.send_email",
            description="Отправить электронное письмо через SMTP. Опасное действие (dangerous=True).",
            parameters={
                "type": "object",
                "properties": {
                    "to_addr": {"type": "string", "description": "Email получателя"},
                    "subject": {"type": "string", "description": "Тема письма"},
                    "body": {"type": "string", "description": "Текст сообщения"},
                },
                "required": ["to_addr", "subject", "body"],
            },
            fn=send_email,
            skills=["email", "messaging", "smtp", "imap", "communication", "integrations"],
            attributes={
                "category": "messaging",
                "channel": "email",
                "read_only": False,
                "dangerous": True,
                "requires_network": True,
                "resource_type": "email",
                "speed": "medium",
                "tags": ["email", "smtp", "send", "mail", "communication"],
            },
            example='smtp.send_email(to_addr="test@example.com", subject="Отчёт", body="Аудит готов.")',
        ),
        Tool(
            name="smtp.read_emails",
            description="Прочитать входящие сообщения из почтового ящика (IMAP).",
            parameters={
                "type": "object",
                "properties": {
                    "limit": {
                        "type": "integer",
                        "description": "Количество сообщений для отображения (по умолчанию 5)",
                    }
                },
            },
            fn=read_emails,
            skills=["email", "messaging", "smtp", "imap", "communication", "integrations"],
            attributes={
                "category": "messaging",
                "channel": "email",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "email",
                "speed": "medium",
                "tags": ["email", "imap", "read", "inbox", "communication"],
            },
            example="smtp.read_emails(limit=3)",
        ),
    ]
