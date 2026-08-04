"""Интеграция с электронной почтой: SMTP (отправка с вложениями) и IMAP (чтение).

Поддерживает:
  - Implicit SSL (порт 465, SMTP_USE_SSL=true) — для большинства российских хостингов
  - STARTTLS (порт 587, SMTP_USE_SSL=false) — Gmail, Outlook
  - Вложения файлов из workspace (attachments)
  - HTML body (опционально)
  - Множественные получатели (через запятую)

ОПАСНЫЕ ДЕЙСТВИЯ: отправка письма помечена dangerous=True.
"""
from __future__ import annotations

import email.message
import email.mime.application
import email.mime.multipart
import email.mime.text
import email.utils
import imaplib
import mimetypes
import smtplib
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import settings
from ..core import Tool, ToolError, Workspace


@dataclass
class MailConfig:
    smtp_host: str = settings.smtp_host or "smtp.example.com"
    smtp_port: int = settings.smtp_port or 587
    imap_host: str = settings.imap_host or "imap.example.com"
    imap_port: int = settings.imap_port or 993
    username: str = settings.smtp_user or ""
    password: str = settings.smtp_password or ""
    from_addr: str = settings.smtp_from or settings.smtp_user or ""
    use_ssl: bool = settings.smtp_use_ssl  # True = implicit SSL (port 465), False = STARTTLS (port 587)
    mock_mode: bool = settings.mock_mode


class MailService:
    def __init__(self, cfg: MailConfig | None = None, ws: Workspace | None = None) -> None:
        self.cfg = cfg or MailConfig()
        self.ws = ws
        self.sent_emails: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self.inbox_emails: list[dict[str, Any]] = [
            {"id": "1", "from": "alice@example.com", "subject": "Аудит завершён", "body": "Прикладываем протокол."},
            {"id": "2", "from": "bob@example.com", "subject": "Вопрос по выкладке", "body": "Проверьте долю полки."},
        ]

    def send(
        self,
        to_addr: str,
        subject: str,
        body: str,
        attachments: list[str] | None = None,
        html_body: str = "",
    ) -> str:
        """Отправить письмо с опциональными вложениями."""
        with self._lock:
            if not to_addr:
                raise ToolError("Не указан адрес получателя (to_addr)")

            # Парсим множественные получатели
            recipients = [a.strip() for a in to_addr.split(",") if a.strip()]
            if not recipients:
                raise ToolError("Список получателей пуст")

            # Разрешаем вложения
            attachment_paths: list[Path] = []
            if attachments and self.ws:
                for att_name in attachments:
                    p = self.ws.resolve(att_name)
                    if p.exists() and p.is_file():
                        attachment_paths.append(p)
                    else:
                        raise ToolError(f"Файл вложения {att_name!r} не найден в workspace")

            # Mock mode
            if self.cfg.mock_mode:
                self.sent_emails.append({
                    "to": to_addr, "subject": subject, "body": body,
                    "attachments": [p.name for p in attachment_paths],
                })
                att_info = f", вложений: {len(attachment_paths)}" if attachment_paths else ""
                return (
                    f"[MOCK] Письмо отправлено для {to_addr} "
                    f"(тема: {subject!r}, {len(body)} симв.{att_info})"
                )

            # Определяем From
            from_addr = self.cfg.from_addr or self.cfg.username
            if not from_addr:
                raise ToolError("Не указан MAIL_FROM_ADDRESS или MAIL_USERNAME")

            # Создаём MIME-сообщение
            if attachment_paths or html_body:
                msg = email.mime.multipart.MIMEMultipart("mixed")
                # Текстовая и HTML части
                alt = email.mime.multipart.MIMEMultipart("alternative")
                alt.attach(email.mime.text.MIMEText(body, "plain", "utf-8"))
                if html_body:
                    alt.attach(email.mime.text.MIMEText(html_body, "html", "utf-8"))
                msg.attach(alt)
                # Вложения
                for att_path in attachment_paths:
                    ctype, _ = mimetypes.guess_type(str(att_path))
                    if ctype is None:
                        ctype = "application/octet-stream"
                    maintype, subtype = ctype.split("/", 1)
                    with open(att_path, "rb") as f:
                        att = email.mime.application.MIMEApplication(
                            f.read(), _subtype=subtype
                        )
                    att.add_header(
                        "Content-Disposition", "attachment",
                        filename=att_path.name,
                    )
                    msg.attach(att)
            else:
                msg = email.message.EmailMessage()
                msg.set_content(body)

            msg["From"] = from_addr
            msg["To"] = ", ".join(recipients)
            msg["Subject"] = subject
            msg["Date"] = email.utils.formatdate(localtime=True)

            # Отправка
            try:
                if self.cfg.use_ssl:
                    # Implicit SSL (порт 465) — большинство российских хостингов
                    with smtplib.SMTP_SSL(
                        self.cfg.smtp_host, self.cfg.smtp_port, timeout=15
                    ) as server:
                        if self.cfg.username and self.cfg.password:
                            server.login(self.cfg.username, self.cfg.password)
                        server.sendmail(from_addr, recipients, msg.as_string())
                else:
                    # STARTTLS (порт 587) — Gmail, Outlook
                    with smtplib.SMTP(
                        self.cfg.smtp_host, self.cfg.smtp_port, timeout=15
                    ) as server:
                        server.ehlo()
                        server.starttls()
                        server.ehlo()
                        if self.cfg.username and self.cfg.password:
                            server.login(self.cfg.username, self.cfg.password)
                        server.sendmail(from_addr, recipients, msg.as_string())
            except (smtplib.SMTPException, OSError) as exc:
                raise ToolError(
                    f"Ошибка SMTP ({'SSL' if self.cfg.use_ssl else 'STARTTLS'} "
                    f"{self.cfg.smtp_host}:{self.cfg.smtp_port}): {exc}"
                ) from exc

            att_info = f", вложений: {len(attachment_paths)}" if attachment_paths else ""
            return f"Письмо для {to_addr} успешно отправлено (тема: {subject!r}{att_info})"

    def read_inbox(self, limit: int = 5) -> str:
        with self._lock:
            if self.cfg.mock_mode:
                msgs = self.inbox_emails[:limit]
                if not msgs:
                    return "(Папка Входящие пуста)"
                lines = [f"- [{m['id']}] от {m['from']} — {m['subject']}: {m['body']}" for m in msgs]
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


def build_smtp_tools(
    service: MailService | None = None, ws: Workspace | None = None
) -> list[Tool]:
    """Собрать инструменты для работы с электронной почтой (SMTP/IMAP)."""
    mail = service or MailService(ws=ws)

    def send_email(
        to_addr: str,
        subject: str,
        body: str,
        attachments_json: str = "[]",
        html_body: str = "",
    ) -> str:
        try:
            import json as _json
            attachments = _json.loads(attachments_json) if attachments_json else []
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON в attachments_json: {exc}") from exc
        if not isinstance(attachments, list):
            attachments = []
        return mail.send(to_addr, subject, body, attachments=attachments, html_body=html_body)

    def read_emails(limit: int = 5) -> str:
        return mail.read_inbox(limit=limit)

    return [
        Tool(
            name="smtp.send_email",
            description="Отправить email через SMTP с опциональными вложениями файлов из workspace. Поддерживает SSL (порт 465) и STARTTLS (порт 587).",
            parameters={
                "type": "object",
                "properties": {
                    "to_addr": {"type": "string", "description": "Email получателя (или несколько через запятую)"},
                    "subject": {"type": "string", "description": "Тема письма"},
                    "body": {"type": "string", "description": "Текст сообщения"},
                    "attachments_json": {
                        "type": "string",
                        "description": 'JSON-массив путей к файлам в workspace для вложения (\'["report.xlsx", "chart.png"]\')',
                    },
                    "html_body": {"type": "string", "description": "Опциональный HTML-вариант письма"},
                },
                "required": ["to_addr", "subject", "body"],
            },
            fn=send_email,
            skills=["email", "messaging", "smtp", "communication", "integrations"],
            attributes={
                "category": "integration",
                "channel": "email",
                "read_only": False,
                "dangerous": True,
                "requires_network": True,
                "resource_type": "email",
                "speed": "medium",
                "tags": ["email", "smtp", "send", "mail", "attachment"],
            },
            example='smtp.send_email(to_addr="user@mail.ru", subject="Отчёт", body="Во вложении", attachments_json=\'["report.xlsx"]\')',
        ),
        Tool(
            name="smtp.read_emails",
            description="Прочитать входящие сообщения из почтового ящика (IMAP).",
            parameters={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Количество сообщений (по умолчанию 5)"},
                },
            },
            fn=read_emails,
            skills=["email", "messaging", "imap", "communication", "integrations"],
            attributes={
                "category": "integration",
                "channel": "email",
                "read_only": True,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "email",
                "speed": "medium",
                "tags": ["email", "imap", "read", "inbox"],
            },
            example="smtp.read_emails(limit=3)",
        ),
    ]
