"""Инструменты связи с внешним миром: email (SMTP/IMAP), Telegram Bot API,
мессенджер MAX Bot API (https://dev.max.ru/docs-api).

ГЛАВНЫЙ ПРИНЦИП: отправка сообщения — НЕОБРАТИМОЕ действие (в отличие от
чтения файла или запроса к БД: письмо ушло, сообщение увидели, файл не
отозвать). Поэтому, как и `run_command` в tools/shell.py, инструменты
отправки помечены `dangerous=True` и в режиме подтверждения (когда задан
`confirm`) спрашивают оператора перед реальной отправкой. Автономный
прогон без оператора должен явно включить `confirm_sends=False` в
конфиге, беря ответственность на себя, — тихого умолчания «шлём всё
подряд» здесь нет.

Три канала, три протокола:
  email     — SMTP (smtplib) для отправки, IMAP (imaplib) для чтения.
              Только стандартная библиотека.
  telegram  — Bot API, обычный HTTPS/JSON (urllib, как в agent/mcp.py).
  max       — Bot API мессенджера MAX (platform-api2.max.ru), тот же
              протокол: HTTPS/JSON с заголовком Authorization: <token>.

Ключи/токены НЕ читаются из файла конфигурации напрямую в этом модуле —
конфигурация приходит объектом MessagingConfig, который agent/config.py
собирает из окружения (см. .env.example), в духе остальной системы:
«ключи в файле не храним».
"""
from __future__ import annotations

import email.message
import imaplib
import json
import mimetypes
import smtplib
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.header import decode_header
from pathlib import Path
from typing import Any, Callable

from .base import Tool, ToolError, Workspace

MAX_ATTACH_SIZE = 20_000_000     # 20 МБ — грубая защита от раздутых вложений


# ============================================================== конфиги
@dataclass
class EmailConfig:
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_use_ssl: bool = False        # неявный TLS (обычно порт 465)
    smtp_starttls: bool = True        # STARTTLS на обычном порту (587);
                                       # выключается только для локального
                                       # релея без шифрования (тесты, intranet)
    from_addr: str = ""               # пусто -> берём smtp_user

    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    imap_password: str = ""
    imap_use_ssl: bool = True          # выключается только для локального
                                        # сервера без TLS (тесты, intranet)

    def smtp_ready(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.smtp_password)

    def imap_ready(self) -> bool:
        return bool(self.imap_host and self.imap_user and self.imap_password)


@dataclass
class TelegramConfig:
    bot_token: str = ""
    api_base: str = "https://api.telegram.org"
    rate_limit: float = 1.0           # не чаще 1 сообщения в секунду
    # Секрет для Webhook (см. agent/webhooks.py): Telegram присылает его в
    # заголовке X-Telegram-Bot-Api-Secret-Token каждого запроса на вебхук.
    # Пусто = маршрут /webhook/telegram в agent/server.py не активируется
    # (см. там же) — приём вложений без проверенного секрета означает, что
    # ЛЮБОЙ желающий может отправить POST и заставить агента выполнить
    # придуманную задачу, поэтому включение только по явному значению.
    webhook_secret: str = ""

    def ready(self) -> bool:
        return bool(self.bot_token)


@dataclass
class MaxConfig:
    """MAX (мессенджер VK/Mail): https://dev.max.ru/docs-api"""
    bot_token: str = ""
    api_base: str = "https://platform-api2.max.ru"
    rate_limit: float = 0.6           # лимит платформы: 2 сообщения/сек в чат
    # Секрет для Webhook: MAX присылает его в заголовке
    # X-Max-Bot-Api-Secret (см. POST /subscriptions). Та же логика, что у
    # TelegramConfig.webhook_secret — пусто -> маршрут выключен.
    webhook_secret: str = ""

    def ready(self) -> bool:
        return bool(self.bot_token)


@dataclass
class MessagingConfig:
    email: EmailConfig = field(default_factory=EmailConfig)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    max: MaxConfig = field(default_factory=MaxConfig)
    # см. пояснение в шапке файла: отправка — необратимое действие
    confirm_sends: bool = True
    # Профиль, применяемый при обработке ВХОДЯЩИХ сообщений через Webhook
    # (см. agent/webhooks.py) — например "intake" для разбора вложений.
    # Пусто = использовать тот же профиль/навыки, с которым запущен сервер.
    webhook_profile: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "MessagingConfig":
        def _clean(d: dict[str, Any]) -> dict[str, Any]:
            # ключи вида "_комментарий" — как в examples/config.mcp.json,
            # пояснения прямо в JSON-конфиге, не поля датакласса
            return {k: v for k, v in d.items() if not k.startswith("_")}

        data = _clean(dict(data or {}))
        email_cfg = EmailConfig(**_clean(data.pop("email", {}) or {}))
        tg_cfg = TelegramConfig(**_clean(data.pop("telegram", {}) or {}))
        max_cfg = MaxConfig(**_clean(data.pop("max", {}) or {}))
        confirm_sends = bool(data.pop("confirm_sends", True))
        webhook_profile = str(data.pop("webhook_profile", "") or "")
        return cls(email=email_cfg, telegram=tg_cfg, max=max_cfg,
                   confirm_sends=confirm_sends, webhook_profile=webhook_profile)




# ============================================================ rate limit
class _RateLimiter:
    """Простой минимальный интервал между вызовами — как MCPClient.wait_for_slot,
    но без зависимости от mcp.py (разные модули, общий приём)."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        delta = time.time() - self._last
        wait = self.min_interval - delta
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()


def _ask(confirm: Callable[[str, str], bool] | None, confirm_sends: bool,
         action: str, detail: str) -> None:
    """Общая точка подтверждения перед необратимой отправкой."""
    if not confirm_sends:
        return
    if confirm is None or not confirm(detail, action):
        raise ToolError(
            f"Отправка отклонена оператором ({action}). Сообщение НЕ отправлено."
        )


# =============================================================== email
def _decode_mime_words(s: str) -> str:
    parts = decode_header(s or "")
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            out.append(text.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(text)
    return "".join(out)


def build_email_tools(ws: Workspace, cfg: EmailConfig, confirm_sends: bool,
                      confirm: Callable[[str, str], bool] | None) -> list[Tool]:

    def email_send(to: str, subject: str, body: str, cc: str = "",
                   attachments: str = "", html: bool = False) -> str:
        if not cfg.smtp_ready():
            raise ToolError(
                "SMTP не настроен. Заполните email.smtp_host/smtp_user/"
                "smtp_password в конфиге (пароль — через переменную "
                "окружения, см. .env.example)."
            )
        recipients = [a.strip() for a in to.split(",") if a.strip()]
        if not recipients:
            raise ToolError("Не указан ни один получатель (to)")
        cc_list = [a.strip() for a in cc.split(",") if a.strip()]

        msg = email.message.EmailMessage()
        msg["From"] = cfg.from_addr or cfg.smtp_user
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        if cc_list:
            msg["Cc"] = ", ".join(cc_list)
        if html:
            msg.set_content("Письмо в формате HTML. Откройте в почтовом клиенте.")
            msg.add_alternative(body, subtype="html")
        else:
            msg.set_content(body)

        att_names = [a.strip() for a in attachments.replace(",", "\n").splitlines()
                    if a.strip()]
        total_size = 0
        for rel in att_names:
            p = ws.resolve(rel)
            if not p.exists() or not p.is_file():
                raise ToolError(f"Вложение {rel!r} не найдено")
            size = p.stat().st_size
            total_size += size
            if total_size > MAX_ATTACH_SIZE:
                raise ToolError(
                    f"Суммарный размер вложений превышает "
                    f"{MAX_ATTACH_SIZE // 1_000_000} МБ"
                )
            ctype, _ = mimetypes.guess_type(p.name)
            maintype, subtype = (ctype.split("/", 1) if ctype
                                 else ("application", "octet-stream"))
            msg.add_attachment(p.read_bytes(), maintype=maintype,
                              subtype=subtype, filename=p.name)

        summary = (f"Кому: {', '.join(recipients)}"
                  + (f"; Копия: {', '.join(cc_list)}" if cc_list else "")
                  + f"\nТема: {subject}\n"
                  + (f"Вложений: {len(att_names)}\n" if att_names else "")
                  + f"\n{body[:500]}")
        _ask(confirm, confirm_sends, "отправка письма", summary)

        try:
            if cfg.smtp_use_ssl:
                server = smtplib.SMTP_SSL(cfg.smtp_host, cfg.smtp_port, timeout=30)
            else:
                server = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port, timeout=30)
                if cfg.smtp_starttls:
                    server.starttls()
            with server:
                server.login(cfg.smtp_user, cfg.smtp_password)
                server.send_message(msg, to_addrs=recipients + cc_list)
        except smtplib.SMTPException as exc:
            raise ToolError(f"Ошибка отправки письма: {exc}") from exc
        except OSError as exc:
            raise ToolError(f"Не удалось соединиться с SMTP {cfg.smtp_host}: {exc}") from exc

        return (f"Письмо отправлено: {', '.join(recipients)}, тема {subject!r}"
               + (f", вложений: {len(att_names)}" if att_names else ""))

    def _imap_connect() -> imaplib.IMAP4:
        if not cfg.imap_ready():
            raise ToolError(
                "IMAP не настроен. Заполните email.imap_host/imap_user/"
                "imap_password в конфиге."
            )
        try:
            if cfg.imap_use_ssl:
                conn = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port, timeout=30)
            else:
                conn = imaplib.IMAP4(cfg.imap_host, cfg.imap_port, timeout=30)
            conn.login(cfg.imap_user, cfg.imap_password)
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ToolError(f"Не удалось подключиться к IMAP {cfg.imap_host}: {exc}") from exc
        return conn


    def email_list(folder: str = "INBOX", limit: int = 10,
                   unread_only: bool = False) -> str:
        conn = _imap_connect()
        try:
            typ, _ = conn.select(folder, readonly=True)
            if typ != "OK":
                raise ToolError(f"Папка {folder!r} не найдена")
            criterion = "UNSEEN" if unread_only else "ALL"
            typ, data = conn.search(None, criterion)
            if typ != "OK":
                raise ToolError("Ошибка поиска писем")
            ids = data[0].split()
            if not ids:
                return f"{folder}: писем нет" + (" (непрочитанных)" if unread_only else "")
            ids = ids[-limit:][::-1]  # последние N, новые сверху
            lines = [f"{folder}: {len(ids)} писем (из {len(data[0].split())})"]
            for mid in ids:
                typ, msg_data = conn.fetch(mid, "(BODY.PEEK[HEADER.FIELDS "
                                                "(FROM SUBJECT DATE))")
                if typ != "OK" or not msg_data or not msg_data[0]:
                    continue
                raw = msg_data[0][1]
                m = email.message_from_bytes(raw)
                subj = _decode_mime_words(m.get("Subject", "(без темы)"))
                frm = _decode_mime_words(m.get("From", "?"))
                date = m.get("Date", "?")
                lines.append(f"  #{mid.decode()}: от {frm} — {subj!r} ({date})")
            return "\n".join(lines)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            conn.logout()

    def email_read(message_id: str, folder: str = "INBOX") -> str:
        conn = _imap_connect()
        try:
            typ, _ = conn.select(folder, readonly=True)
            if typ != "OK":
                raise ToolError(f"Папка {folder!r} не найдена")
            typ, msg_data = conn.fetch(message_id.encode(), "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                raise ToolError(f"Письмо #{message_id} не найдено в {folder!r}")
            raw = msg_data[0][1]
            m = email.message_from_bytes(raw)
            subj = _decode_mime_words(m.get("Subject", "(без темы)"))
            frm = _decode_mime_words(m.get("From", "?"))
            to = _decode_mime_words(m.get("To", "?"))
            date = m.get("Date", "?")

            body = ""
            attachments = []
            if m.is_multipart():
                for part in m.walk():
                    disp = str(part.get("Content-Disposition") or "")
                    ctype = part.get_content_type()
                    if "attachment" in disp:
                        attachments.append(part.get_filename() or "(без имени)")
                        continue
                    if ctype == "text/plain" and not body:
                        payload = part.get_payload(decode=True)
                        if payload:
                            body = payload.decode(
                                part.get_content_charset() or "utf-8", errors="replace")
            else:
                payload = m.get_payload(decode=True)
                if payload:
                    body = payload.decode(m.get_content_charset() or "utf-8",
                                          errors="replace")

            out = [f"От: {frm}", f"Кому: {to}", f"Дата: {date}",
                  f"Тема: {subj}"]
            if attachments:
                out.append(f"Вложения: {', '.join(attachments)}")
            out.append("")
            out.append(body[:8000] + ("\n... обрезано" if len(body) > 8000 else ""))
            return "\n".join(out)
        finally:
            try:
                conn.close()
            except Exception:
                pass
            conn.logout()

    return [
        Tool("email_send",
             "Отправить письмо по SMTP. Необратимое действие — в режиме "
             "подтверждения запрашивает согласие оператора. Вложения — "
             "файлы из рабочей папки, через запятую или каждое с новой строки.",
             {"type": "object",
              "properties": {
                  "to": {"type": "string", "description": "Получатели через запятую"},
                  "subject": {"type": "string"},
                  "body": {"type": "string"},
                  "cc": {"type": "string"},
                  "attachments": {"type": "string"},
                  "html": {"type": "boolean", "description": "body — HTML, а не текст"}},
              "required": ["to", "subject", "body"]},
             email_send, dangerous=True),
        Tool("email_list",
             "Список писем в папке по IMAP: отправитель, тема, дата. "
             "Новые сверху.",
             {"type": "object",
              "properties": {
                  "folder": {"type": "string"},
                  "limit": {"type": "integer"},
                  "unread_only": {"type": "boolean"}},
              "required": []},
             email_list),
        Tool("email_read",
             "Прочитать письмо целиком по номеру (id из email_list): "
             "заголовки, текст, список вложений (сами файлы не скачивает).",
             {"type": "object",
              "properties": {
                  "message_id": {"type": "string"},
                  "folder": {"type": "string"}},
              "required": ["message_id"]},
             email_read),
    ]


# ============================================================= telegram
def telegram_api_request(cfg: TelegramConfig, method: str,
                         payload: dict[str, Any] | None = None,
                         files: dict[str, tuple[str, bytes, str]] | None = None
                         ) -> dict[str, Any]:
    """Сырой вызов Telegram Bot API — общая точка для Tool-обёрток ниже И

    agent/webhooks.py (автоматический ответ на входящее сообщение). Без
    ожидания rate-limit: у build_telegram_tools свой _RateLimiter вокруг
    этой функции (частые вызовы модели), а webhooks.py отвечает на
    входящее сообщение один раз — рисковать забанить бота почти нечем,
    заводить для этого отдельный лимитер excessive.
    """
    if not cfg.ready():
        raise ToolError(
            "Telegram не настроен. Укажите telegram.bot_token в конфиге "
            "(через переменную окружения, см. .env.example)."
        )
    url = f"{cfg.api_base}/bot{cfg.bot_token}/{method}"
    if files:
        boundary = "----agentboundary"
        body = bytearray()
        for key, val in (payload or {}).items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                    f'name="{key}"\r\n\r\n{val}\r\n').encode()
        for key, (filename, data, ctype) in files.items():
            body += (f"--{boundary}\r\nContent-Disposition: form-data; "
                    f'name="{key}"; filename="{filename}"\r\n'
                    f"Content-Type: {ctype}\r\n\r\n").encode()
            body += data
            body += b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        req = urllib.request.Request(
            url, data=bytes(body), method="POST",
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    else:
        req = urllib.request.Request(
            url, data=json.dumps(payload or {}).encode(), method="POST",
            headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise ToolError(f"Telegram API HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(f"Не достучались до Telegram API: {exc}") from exc
    if not data.get("ok"):
        raise ToolError(f"Telegram API отказал: {data.get('description')}")
    return data.get("result", {})


def telegram_download_file(cfg: TelegramConfig, file_id: str,
                           max_size: int = MAX_ATTACH_SIZE) -> tuple[bytes, str]:
    """Скачать вложение по file_id (см. приём вложений в webhooks.py).

    Двухшаговый протокол Telegram: getFile отдаёт file_path (доступен
    ~час), затем сам файл лежит на отдельном домене /file/bot<token>/...
    Тело здесь не связано с диалоговой моделью — это чистый httр-клиент,
    используется и Tool'ом (если понадобится), и вебхуком.
    """
    info = telegram_api_request(cfg, "getFile", {"file_id": file_id})
    file_path = info.get("file_path")
    if not file_path:
        raise ToolError(f"Telegram не отдал file_path для file_id={file_id}")
    size = info.get("file_size") or 0
    if size and size > max_size:
        raise ToolError(
            f"Файл {file_path} больше {max_size // 1_000_000} МБ — не скачиваем"
        )
    url = f"{cfg.api_base}/file/bot{cfg.bot_token}/{file_path}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read(max_size + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(f"Не удалось скачать файл Telegram: {exc}") from exc
    if len(data) > max_size:
        raise ToolError(f"Файл {file_path} больше {max_size // 1_000_000} МБ")
    return data, Path(file_path).name


def build_telegram_tools(ws: Workspace, cfg: TelegramConfig, confirm_sends: bool,
                         confirm: Callable[[str, str], bool] | None) -> list[Tool]:
    limiter = _RateLimiter(cfg.rate_limit)

    def _call(method: str, payload: dict[str, Any] | None = None,
             files: dict[str, tuple[str, bytes, str]] | None = None) -> dict[str, Any]:
        limiter.wait()
        return telegram_api_request(cfg, method, payload, files)

    def telegram_send_message(chat_id: str, text: str,
                              parse_mode: str = "") -> str:
        if not text.strip():
            raise ToolError("Пустой текст сообщения")
        _ask(confirm, confirm_sends, "отправка в Telegram",
            f"Чат {chat_id}\n\n{text[:500]}")
        payload: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if parse_mode.strip():
            payload["parse_mode"] = parse_mode
        res = _call("sendMessage", payload)
        return f"Отправлено в Telegram (чат {chat_id}), message_id={res.get('message_id')}"

    def telegram_send_file(chat_id: str, file_path: str, caption: str = "") -> str:
        p = ws.resolve(file_path)
        if not p.exists() or not p.is_file():
            raise ToolError(f"Файл {file_path!r} не найден")
        if p.stat().st_size > MAX_ATTACH_SIZE:
            raise ToolError(f"Файл больше {MAX_ATTACH_SIZE // 1_000_000} МБ")
        _ask(confirm, confirm_sends, "отправка файла в Telegram",
            f"Чат {chat_id}\nФайл: {ws.relative(p)}\n{caption[:300]}")
        ctype, _ = mimetypes.guess_type(p.name)
        payload = {"chat_id": chat_id}
        if caption.strip():
            payload["caption"] = caption
        res = _call("sendDocument", payload,
                   files={"document": (p.name, p.read_bytes(),
                                       ctype or "application/octet-stream")})
        return (f"Файл {ws.relative(p)} отправлен в Telegram (чат {chat_id}), "
               f"message_id={res.get('message_id')}")

    def telegram_get_updates(limit: int = 10, offset: int = 0) -> str:
        res = _call("getUpdates", {"limit": max(1, min(100, limit)),
                                   "offset": offset, "timeout": 0})
        if not res:
            return "Новых обновлений нет"
        lines = [f"Обновлений: {len(res)}"]
        for upd in res:
            msg = upd.get("message") or {}
            frm = (msg.get("from") or {}).get("username") \
                or (msg.get("from") or {}).get("first_name", "?")
            chat_id = (msg.get("chat") or {}).get("id")
            text = msg.get("text", "(без текста)")
            lines.append(f"  update_id={upd.get('update_id')} чат={chat_id} "
                        f"от={frm}: {text[:200]}")
        lines.append(f"\nСледующий offset: {res[-1]['update_id'] + 1}")
        return "\n".join(lines)

    return [
        Tool("telegram_send_message",
             "Отправить текстовое сообщение через Telegram-бота. "
             "Необратимое действие — запрашивает подтверждение оператора, "
             "если оно включено. parse_mode: 'Markdown'/'HTML' или пусто.",
             {"type": "object",
              "properties": {
                  "chat_id": {"type": "string",
                              "description": "ID чата/пользователя или @username канала"},
                  "text": {"type": "string"},
                  "parse_mode": {"type": "string"}},
              "required": ["chat_id", "text"]},
             telegram_send_message, dangerous=True),
        Tool("telegram_send_file",
             "Отправить файл из рабочей папки через Telegram-бота.",
             {"type": "object",
              "properties": {
                  "chat_id": {"type": "string"},
                  "file_path": {"type": "string"},
                  "caption": {"type": "string"}},
              "required": ["chat_id", "file_path"]},
             telegram_send_file, dangerous=True),
        Tool("telegram_get_updates",
             "Получить новые входящие сообщения боту (long polling с "
             "timeout=0 — разовый опрос). Используйте offset из предыдущего "
             "ответа, чтобы не читать одно и то же дважды.",
             {"type": "object",
              "properties": {
                  "limit": {"type": "integer"},
                  "offset": {"type": "integer",
                             "description": "update_id, с которого читать"}},
              "required": []},
             telegram_get_updates),
    ]


# ==================================================================== max
def max_api_request(cfg: MaxConfig, method: str, http_method: str,
                    params: dict[str, Any] | None = None,
                    body: dict[str, Any] | None = None) -> dict[str, Any]:
    """Сырой вызов MAX Bot API — общая точка для build_max_tools И

    agent/webhooks.py, по тому же принципу, что telegram_api_request.
    """
    if not cfg.ready():
        raise ToolError(
            "MAX не настроен. Укажите max.bot_token в конфиге (через "
            "переменную окружения, см. .env.example)."
        )
    qs = ""
    if params:
        clean = {k: v for k, v in params.items() if v is not None}
        if clean:
            qs = "?" + urllib.parse.urlencode(clean)
    url = f"{cfg.api_base}{method}{qs}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=http_method,
        headers={"Authorization": cfg.bot_token,
                "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise ToolError(f"MAX API HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(f"Не достучались до MAX API: {exc}") from exc


def max_download_attachment(url: str, max_size: int = MAX_ATTACH_SIZE) -> bytes:
    """Скачать вложение MAX по прямой ссылке из payload.url.

    В отличие от Telegram, MAX отдаёт готовую скачиваемую ссылку прямо во
    входящем событии (см. https://dev.max.ru/docs-api) — отдельного
    getFile-запроса не требуется, только сама загрузка по HTTP.
    """
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read(max_size + 1)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise ToolError(f"Не удалось скачать вложение MAX: {exc}") from exc
    if len(data) > max_size:
        raise ToolError(f"Вложение больше {max_size // 1_000_000} МБ")
    return data



def build_max_tools(ws: Workspace, cfg: MaxConfig, confirm_sends: bool,
                    confirm: Callable[[str, str], bool] | None) -> list[Tool]:
    limiter = _RateLimiter(cfg.rate_limit)

    def _request(method: str, http_method: str, params: dict[str, Any] | None = None,
                body: dict[str, Any] | None = None) -> dict[str, Any]:
        limiter.wait()
        return max_api_request(cfg, method, http_method, params, body)

    def max_send_message(text: str, user_id: str = "", chat_id: str = "") -> str:
        if not text.strip():
            raise ToolError("Пустой текст сообщения")
        if not user_id and not chat_id:
            raise ToolError("Укажите user_id (личный диалог) или chat_id (чат/канал)")
        target = f"user_id={user_id}" if user_id else f"chat_id={chat_id}"
        _ask(confirm, confirm_sends, "отправка в MAX", f"{target}\n\n{text[:500]}")
        params: dict[str, Any] = {}
        if user_id:
            params["user_id"] = user_id
        if chat_id:
            params["chat_id"] = chat_id
        res = _request("/messages", "POST", params=params, body={"text": text})
        mid = (res.get("message") or {}).get("body", {}).get("mid")
        return f"Отправлено в MAX ({target}), message_id={mid}"

    def max_get_updates(limit: int = 10, marker: int = 0) -> str:
        params: dict[str, Any] = {"limit": max(1, min(1000, limit))}
        if marker:
            params["marker"] = marker
        res = _request("/updates", "GET", params=params)
        updates = res.get("updates") or []
        if not updates:
            return "Новых обновлений нет"
        lines = [f"Обновлений: {len(updates)}"]
        for upd in updates:
            kind = upd.get("update_type", "?")
            msg = upd.get("message") or {}
            sender = (msg.get("sender") or {}).get("name", "?")
            text = (msg.get("body") or {}).get("text", "")
            lines.append(f"  [{kind}] от {sender}: {text[:200]}")
        if res.get("marker"):
            lines.append(f"\nСледующий marker: {res['marker']}")
        return "\n".join(lines)

    return [
        Tool("max_send_message",
             "Отправить текстовое сообщение через бота мессенджера MAX "
             "(dev.max.ru). Укажите user_id (личный диалог) или chat_id "
             "(групповой чат/канал). Необратимое действие — запрашивает "
             "подтверждение оператора, если оно включено.",
             {"type": "object",
              "properties": {
                  "text": {"type": "string"},
                  "user_id": {"type": "string"},
                  "chat_id": {"type": "string"}},
              "required": ["text"]},
             max_send_message, dangerous=True),
        Tool("max_get_updates",
             "Получить новые события бота MAX через Long Polling (годится "
             "для разработки/тестирования; для продакшна платформа "
             "рекомендует Webhook, здесь не реализован).",
             {"type": "object",
              "properties": {
                  "limit": {"type": "integer"},
                  "marker": {"type": "integer",
                             "description": "marker из предыдущего ответа"}},
              "required": []},
             max_get_updates),
    ]


def build(ws: Workspace, cfg: MessagingConfig,
         confirm: Callable[[str, str], bool] | None = None) -> list[Tool]:
    """Собирает инструменты только для НАСТРОЕННЫХ каналов.

    Канал без токена/пароля просто не появляется в списке инструментов —
    агент не должен видеть возможность, которой не может воспользоваться,
    и не тратит шаг на попытку с понятной, но бесполезной ошибкой.
    """
    tools: list[Tool] = []
    if cfg.email.smtp_ready() or cfg.email.imap_ready():
        tools.extend(build_email_tools(ws, cfg.email, cfg.confirm_sends, confirm))
    if cfg.telegram.ready():
        tools.extend(build_telegram_tools(ws, cfg.telegram, cfg.confirm_sends, confirm))
    if cfg.max.ready():
        tools.extend(build_max_tools(ws, cfg.max, cfg.confirm_sends, confirm))
    return tools
