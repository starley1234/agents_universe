"""Связь с внешним миром: почта, Telegram, MAX.

Отправка наружу — единственное необратимое действие агента. Файл можно
откатить снимком, отправленное письмо вернуть нельзя. Поэтому здесь
защита строже, чем в остальной системе:

  БЕЛЫЙ СПИСОК. Адрес или чат, которого нет в конфиге, — отказ. Не
  предупреждение, а отказ: агент, ошибившийся получателем, отправит
  внутренний черновик клиенту, и это уже не исправить.

  ЧЕРНОВИКИ. Пока белый список пуст, ничего никуда не уходит: письмо
  сохраняется файлом в outbox/, человек читает и отправляет сам.
  Молчаливого «ничего не сделано» тут быть не должно, поэтому агент
  получает явный ответ, что это черновик, а не отправка.

  ПАРОЛИ ТОЛЬКО ИЗ ОКРУЖЕНИЯ. В конфиге их нет — он кладётся в git.

Чтение почты (IMAP) отдельным инструментом: читать безопасно, и белый
список на него не распространяется.

Границы честно: вложения по IMAP скачиваются в рабочую папку, но
исполняемые файлы не сохраняются. HTML-письма приводятся к тексту
грубо, вёрстка теряется.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import json
import mimetypes
import os
import re
import smtplib
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable

from ..tools.base import Tool, ToolError, Workspace

TIMEOUT = 30
MAX_BODY = 20_000
#: расширения, которые не сохраняем из писем ни при каких условиях
DANGEROUS_EXT = {".exe", ".scr", ".bat", ".cmd", ".com", ".pif", ".vbs",
                 ".js", ".jse", ".wsf", ".msi", ".jar", ".ps1", ".lnk"}

MAX_API = "https://platform-api2.max.ru"
TG_API = "https://api.telegram.org"


@dataclass
class CommsConfig:
    """Настройки связи. Пароли и токены берутся из окружения."""
    # почта
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_from: str = ""
    smtp_ssl: bool = False              # True = SMTPS(465), иначе STARTTLS
    imap_host: str = ""
    imap_port: int = 993
    imap_user: str = ""
    # белый список получателей: адреса почты, id чатов
    allow_to: list[str] = field(default_factory=list)
    # мессенджеры
    telegram_chat: str = ""
    max_chat: str = ""
    # черновики, когда отправка не разрешена
    outbox: str = "outbox"

    @property
    def smtp_pass(self) -> str:
        return os.getenv("AGENT_SMTP_PASS", "")

    @property
    def imap_pass(self) -> str:
        return os.getenv("AGENT_IMAP_PASS", "") or self.smtp_pass

    @property
    def tg_token(self) -> str:
        return os.getenv("AGENT_TG_TOKEN", "")

    @property
    def max_token(self) -> str:
        return os.getenv("AGENT_MAX_TOKEN", "")


def allowed(cfg: CommsConfig, target: str) -> bool:
    """Разрешён ли получатель.

    Сравниваем без регистра и пробелов. Поддержан вид '*@example.com' —
    целый домен, это частый рабочий случай. Пустой список означает
    «никому», а НЕ «всем»: умолчание обязано быть безопасным.
    """
    t = (target or "").strip().lower()
    if not t:
        return False
    for rule in cfg.allow_to:
        r = str(rule).strip().lower()
        if not r:
            continue
        if r == t:
            return True
        if r.startswith("*@") and t.endswith(r[1:]):
            return True
    return False


def _html_to_text(html_src: str) -> str:
    t = re.sub(r"(?is)<(script|style).*?</\1>", " ", html_src)
    t = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", t)
    t = re.sub(r"<[^>]+>", " ", t)
    t = (t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
          .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"[ \t]{2,}", " ", re.sub(r"\n{3,}", "\n\n", t)).strip()


def _post_json(url: str, payload: dict[str, Any],
               headers: dict[str, str] | None = None) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise ToolError(f"сервис ответил {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise ToolError(f"нет связи: {exc.reason}") from exc


# ═══════════════════════════ инструменты ═══════════════════════════
def build(ws: Workspace, cfg: CommsConfig,
          confirm: Callable[[str, str], bool] | None = None) -> list[Tool]:

    def _draft(kind: str, to: str, subject: str, body: str,
               why: str) -> str:
        """Сохранить черновик вместо отправки и честно об этом сказать."""
        d = ws.resolve(cfg.outbox)
        d.mkdir(parents=True, exist_ok=True)
        n = len(list(d.glob("*.txt"))) + 1
        safe = re.sub(r"[^\w.@-]+", "_", to)[:40] or "без-адресата"
        p = d / f"{n:03d}-{kind}-{safe}.txt"
        p.write_text(f"кому: {to}\nтема: {subject}\n\n{body}\n",
                     encoding="utf-8")
        return (f"НЕ ОТПРАВЛЕНО. {why}\n"
                f"Черновик сохранён: {ws.relative(p)} — человек прочитает "
                f"и отправит сам. Не считай это выполненной отправкой.")

    def _check_target(kind: str, to: str, subject: str, body: str) -> str | None:
        """Вернёт текст-черновик, если отправлять нельзя, иначе None."""
        if not cfg.allow_to:
            return _draft(kind, to, subject, body,
                          "Белый список получателей пуст — отправка наружу "
                          "запрещена (allow_to в конфиге).")
        if not allowed(cfg, to):
            return _draft(kind, to, subject, body,
                          f"Получателя {to!r} нет в белом списке "
                          f"({', '.join(cfg.allow_to)}).")
        return None

    # ------------------------------------------------------------ почта
    def send_email(to: str, subject: str, body: str,
                   attachments: str = "") -> str:
        if not to.strip():
            raise ToolError("Не указан получатель")
        blocked = _check_target("email", to, subject, body)
        if blocked:
            return blocked
        if not cfg.smtp_host:
            return _draft("email", to, subject, body,
                          "SMTP не настроен (smtp_host в конфиге).")
        if not cfg.smtp_pass:
            return _draft("email", to, subject, body,
                          "Нет пароля: переменная окружения AGENT_SMTP_PASS "
                          "не задана.")

        msg = EmailMessage()
        msg["From"] = cfg.smtp_from or cfg.smtp_user
        msg["To"] = to
        msg["Subject"] = subject or "(без темы)"
        msg.set_content(body)

        added = []
        for raw in [a.strip() for a in attachments.split(",") if a.strip()]:
            p = ws.resolve(raw)
            if not p.exists():
                raise ToolError(f"Вложение {raw!r} не найдено")
            ctype, _ = mimetypes.guess_type(p.name)
            maintype, _, subtype = (ctype or "application/octet-stream"
                                    ).partition("/")
            msg.add_attachment(p.read_bytes(), maintype=maintype,
                               subtype=subtype, filename=p.name)
            added.append(p.name)

        if confirm is not None:
            what = f"письмо на {to}: {subject}"
            if not confirm(what, "отправка наружу необратима"):
                return _draft("email", to, subject, body,
                              "Оператор отклонил отправку.")
        try:
            if cfg.smtp_ssl:
                srv: Any = smtplib.SMTP_SSL(
                    cfg.smtp_host, cfg.smtp_port, timeout=TIMEOUT,
                    context=ssl.create_default_context())
            else:
                srv = smtplib.SMTP(cfg.smtp_host, cfg.smtp_port,
                                   timeout=TIMEOUT)
                srv.starttls(context=ssl.create_default_context())
            with srv:
                srv.login(cfg.smtp_user or cfg.smtp_from, cfg.smtp_pass)
                srv.send_message(msg)
        except (smtplib.SMTPException, OSError) as exc:
            raise ToolError(f"Письмо не отправлено: {exc}") from exc
        extra = f", вложений: {len(added)} ({', '.join(added)})" if added else ""
        return f"Письмо отправлено на {to}: «{subject}»{extra}"

    def _imap() -> imaplib.IMAP4_SSL:
        if not cfg.imap_host:
            raise ToolError("IMAP не настроен (imap_host в конфиге)")
        if not cfg.imap_pass:
            raise ToolError("Нет пароля: задайте AGENT_IMAP_PASS")
        try:
            m = imaplib.IMAP4_SSL(cfg.imap_host, cfg.imap_port,
                                  timeout=TIMEOUT)
            m.login(cfg.imap_user or cfg.smtp_user, cfg.imap_pass)
            return m
        except (imaplib.IMAP4.error, OSError) as exc:
            raise ToolError(f"Не подключиться к почте: {exc}") from exc

    def list_email(folder: str = "INBOX", limit: int = 10,
                   query: str = "") -> str:
        m = _imap()
        try:
            typ, _ = m.select(folder, readonly=True)
            if typ != "OK":
                raise ToolError(f"Папка {folder!r} не открывается")
            crit = "ALL"
            if query.strip():
                # IMAP ищет по подстроке в теме и теле
                q = query.replace('"', " ")
                crit = f'(OR SUBJECT "{q}" BODY "{q}")'
            typ, data = m.search(None, crit)
            ids = data[0].split()[-max(1, min(limit, 50)):]
            if not ids:
                return (f"В {folder} ничего не найдено"
                        + (f" по запросу {query!r}" if query else ""))
            out = []
            for i in reversed(ids):
                typ, d = m.fetch(i, "(BODY.PEEK[HEADER.FIELDS "
                                    "(FROM SUBJECT DATE)])")
                if typ != "OK" or not d or not isinstance(d[0], tuple):
                    continue
                hdr = email.message_from_bytes(d[0][1],
                                               policy=email.policy.default)
                out.append(f"#{i.decode()} {hdr.get('Date', '')[:31]} | "
                           f"{hdr.get('From', '')[:50]} | "
                           f"{hdr.get('Subject', '(без темы)')[:70]}")
            return (f"Писем в {folder}: {len(out)}\n" + "\n".join(out)
                    + "\nЧитать: read_email с номером #N")
        finally:
            try:
                m.logout()
            except Exception:
                pass

    def read_email(uid: str, folder: str = "INBOX",
                   save_attachments: bool = False) -> str:
        m = _imap()
        try:
            m.select(folder, readonly=True)
            typ, d = m.fetch(str(uid).lstrip("#"), "(RFC822)")
            if typ != "OK" or not d or not isinstance(d[0], tuple):
                raise ToolError(f"Письмо {uid} не найдено в {folder}")
            msg = email.message_from_bytes(d[0][1],
                                           policy=email.policy.default)
            text, files = "", []
            for part in msg.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                name = part.get_filename()
                ctype = part.get_content_type()
                if name:
                    ext = Path(name).suffix.lower()
                    if ext in DANGEROUS_EXT:
                        files.append(f"{name} — НЕ СОХРАНЁН (исполняемый)")
                        continue
                    if save_attachments:
                        safe = re.sub(r"[^\w.@ -]+", "_", name)[:80]
                        p = ws.resolve(f"attachments/{safe}")
                        p.parent.mkdir(parents=True, exist_ok=True)
                        p.write_bytes(part.get_payload(decode=True) or b"")
                        files.append(f"{ws.relative(p)} "
                                     f"({p.stat().st_size:,} байт)")
                    else:
                        files.append(f"{name} (не сохранён)")
                elif ctype == "text/plain" and not text:
                    text = part.get_content()
                elif ctype == "text/html" and not text:
                    text = _html_to_text(part.get_content())
            head = (f"От: {msg.get('From', '')}\nКому: {msg.get('To', '')}\n"
                    f"Дата: {msg.get('Date', '')}\n"
                    f"Тема: {msg.get('Subject', '(без темы)')}")
            body = (text or "(пустое тело)")[:MAX_BODY]
            att = ("\n\nВложения:\n" + "\n".join(f"- {f}" for f in files)
                   if files else "")
            return f"{head}\n\n{body}{att}"
        finally:
            try:
                m.logout()
            except Exception:
                pass

    # ------------------------------------------------------- мессенджеры
    def send_telegram(text: str, chat: str = "") -> str:
        target = (chat or cfg.telegram_chat).strip()
        if not target:
            raise ToolError("Не указан чат (telegram_chat в конфиге)")
        blocked = _check_target("telegram", target, "Telegram", text)
        if blocked:
            return blocked
        if not cfg.tg_token:
            return _draft("telegram", target, "Telegram", text,
                          "Нет токена: задайте AGENT_TG_TOKEN.")
        if confirm is not None and not confirm(
                f"сообщение в Telegram {target}", "отправка наружу необратима"):
            return _draft("telegram", target, "Telegram", text,
                          "Оператор отклонил отправку.")
        # 4096 — предел Telegram; режем по границе строки, а не посреди слова
        chunks = _split_text(text, 4096)
        for i, part in enumerate(chunks, 1):
            _post_json(f"{TG_API}/bot{cfg.tg_token}/sendMessage",
                       {"chat_id": target, "text": part,
                        "disable_web_page_preview": True})
        return (f"Отправлено в Telegram (чат {target})"
                + (f", частей: {len(chunks)}" if len(chunks) > 1 else ""))

    def send_max(text: str, chat: str = "") -> str:
        target = (chat or cfg.max_chat).strip()
        if not target:
            raise ToolError("Не указан чат (max_chat в конфиге)")
        blocked = _check_target("max", target, "MAX", text)
        if blocked:
            return blocked
        if not cfg.max_token:
            return _draft("max", target, "MAX", text,
                          "Нет токена: задайте AGENT_MAX_TOKEN.")
        if confirm is not None and not confirm(
                f"сообщение в MAX {target}", "отправка наружу необратима"):
            return _draft("max", target, "MAX", text,
                          "Оператор отклонил отправку.")
        # MAX: chat_id в query, текст в теле, токен ТОЛЬКО в заголовке
        # Authorization — передача в query больше не поддерживается.
        # Предел текста 4000, а не 4096 как в Telegram.
        chunks = _split_text(text, 4000)
        key = "user_id" if target.lstrip("-").isdigit() and not \
            target.startswith("-") else "chat_id"
        for part in chunks:
            url = f"{MAX_API}/messages?{urllib.parse.urlencode({key: target})}"
            _post_json(url, {"text": part},
                       headers={"Authorization": cfg.max_token})
        return (f"Отправлено в MAX (чат {target})"
                + (f", частей: {len(chunks)}" if len(chunks) > 1 else ""))

    def comms_status() -> str:
        rows = [
            f"почта  отправка : {cfg.smtp_host or '—'}"
            + ("" if cfg.smtp_pass else "  (нет AGENT_SMTP_PASS)"),
            f"почта  чтение   : {cfg.imap_host or '—'}"
            + ("" if cfg.imap_pass else "  (нет AGENT_IMAP_PASS)"),
            f"telegram         : {'токен есть' if cfg.tg_token else 'нет AGENT_TG_TOKEN'}"
            + (f", чат {cfg.telegram_chat}" if cfg.telegram_chat else ""),
            f"MAX              : {'токен есть' if cfg.max_token else 'нет AGENT_MAX_TOKEN'}"
            + (f", чат {cfg.max_chat}" if cfg.max_chat else ""),
        ]
        wl = (", ".join(cfg.allow_to) if cfg.allow_to
              else "ПУСТ — отправка запрещена, всё уходит в черновики")
        return ("Связь с внешним миром\n" + "\n".join(rows)
                + f"\nбелый список     : {wl}")

    return [
        Tool("send_email",
             "Отправить письмо. Получатель обязан быть в белом списке, "
             "иначе письмо сохранится черновиком и НЕ уйдёт. Вложения — "
             "пути через запятую.",
             {"type": "object",
              "properties": {
                  "to": {"type": "string", "description": "Адрес получателя"},
                  "subject": {"type": "string"},
                  "body": {"type": "string"},
                  "attachments": {"type": "string",
                                  "description": "Файлы через запятую"}},
              "required": ["to", "subject", "body"]},
             send_email,
             dangerous=True),
        Tool("list_email",
             "Показать последние письма: дата, отправитель, тема. "
             "Можно искать по слову в теме или теле.",
             {"type": "object",
              "properties": {
                  "folder": {"type": "string", "description": "По умолчанию INBOX"},
                  "limit": {"type": "integer"},
                  "query": {"type": "string", "description": "Слово для поиска"}},
              "required": []},
             list_email),
        Tool("read_email",
             "Прочитать письмо по номеру из list_email. Вложения можно "
             "сохранить в рабочую папку; исполняемые файлы не сохраняются.",
             {"type": "object",
              "properties": {
                  "uid": {"type": "string", "description": "Номер #N"},
                  "folder": {"type": "string"},
                  "save_attachments": {"type": "boolean"}},
              "required": ["uid"]},
             read_email),
        Tool("send_telegram",
             "Отправить сообщение в Telegram. Чат обязан быть в белом "
             "списке, иначе сообщение сохранится черновиком и НЕ уйдёт.",
             {"type": "object",
              "properties": {
                  "text": {"type": "string"},
                  "chat": {"type": "string", "description": "id чата"}},
              "required": ["text"]},
             send_telegram,
             dangerous=True),
        Tool("send_max",
             "Отправить сообщение в мессенджер MAX. Чат обязан быть в "
             "белом списке, иначе сообщение сохранится черновиком.",
             {"type": "object",
              "properties": {
                  "text": {"type": "string"},
                  "chat": {"type": "string", "description": "id чата"}},
              "required": ["text"]},
             send_max,
             dangerous=True),
        Tool("comms_status",
             "Что настроено для связи: почта, мессенджеры, белый список. "
             "Вызови перед отправкой, если не уверен.",
             {"type": "object", "properties": {}, "required": []},
             comms_status),
    ]


def _split_text(text: str, limit: int) -> list[str]:
    """Разрезать по границе строки: разрыв посреди слова читается плохо."""
    if len(text) <= limit:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:            # одна строка длиннее предела
            if cur:
                out.append(cur)
                cur = ""
            out.append(line[:limit])
            line = line[limit:]
        if len(cur) + len(line) + 1 > limit:
            out.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        out.append(cur)
    return out
