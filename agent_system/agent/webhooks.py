"""Приём вложений от Telegram/MAX через Webhook (не Long Polling).

ПОЧЕМУ ЭТО ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ ЧАСТЬ tools/messaging.py: то, что здесь
происходит, — не инструмент для модели (агент его не вызывает сам), а
входная точка снаружи: платформа стучится к НАМ, когда пользователь что-то
прислал боту. tools/messaging.py по-прежнему отвечает только за то, что
агент делает ПО СВОЕЙ инициативе (отправить письмо/сообщение, спросить
Long Polling); webhooks.py дергает те же сырые HTTP-функции
(telegram_api_request/max_api_request и «скачать вложение»), но по
внешнему триггеру и в фоновом потоке.

ГЛАВНАЯ ПРОБЛЕМА, КОТОРУЮ ЭТО РЕШАЕТ: `telegram_get_updates`/
`max_get_updates` (Long Polling, см. tools/messaging.py) видят ТОЛЬКО
текст входящего сообщения — сам файл/фото туда не попадает, агент может
только вежливо попросить прислать текстом. Вебхук даёт полный объект
входящего сообщения, включая вложения, и это единственный способ
скачать реальный файл, а не описание того, что он существует.

ОГРАНИЧЕНИЕ ПО ВРЕМЕНИ ОТВЕТА (важно для реализации): MAX ТРЕБУЕТ
HTTP 200 от Webhook-endpoint в течение 30 секунд, иначе доставка
считается неуспешной и повторяется до 10 раз с растущим интервалом
(https://dev.max.ru/docs-api/methods/POST/subscriptions) — то есть
задвоит обработку одного и того же сообщения. Telegram менее строг, но
держит соединение открытым, пока бот не ответит. Прогон агента может
идти десятки секунд (несколько шагов, вызовы LLM), поэтому сама
обработка уходит в ФОНОВЫЙ ПОТОК, а на сам HTTP-запрос отвечаем 200
немедленно — так делают все продакшн-интеграции с этими платформами.

ПРОВЕРКА ПОДЛИННОСТИ ЗАПРОСА (обязательна, не опциональна): без неё
любой в интернете, узнавший URL вебхука, может подсунуть поддельное
"входящее сообщение" и заставить агента выполнить произвольную задачу
от чужого имени. Поэтому:
  * Telegram — заголовок `X-Telegram-Bot-Api-Secret-Token`, значение
    задаётся параметром `secret_token` в setWebhook
    (см. register_telegram_webhook).
  * MAX — заголовок `X-Max-Bot-Api-Secret`, значение задаётся параметром
    `secret` в POST /subscriptions (см. register_max_webhook).
Если TelegramConfig.webhook_secret / MaxConfig.webhook_secret пусты,
agent/server.py вообще не поднимает соответствующий маршрут — тихого
"вебхук без секрета" в этой системе нет, см. serve() в server.py.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from .build import build_agent
from .config import Config
from .tools.base import Workspace
from .tools.messaging import (
    MaxConfig,
    TelegramConfig,
    max_api_request,
    max_download_attachment,
    telegram_api_request,
    telegram_download_file,
)

INBOX_DIRNAME = "inbox"            # workspace/inbox/<channel>/<chat>/...
MAX_TASK_TEXT = 4000                # обрезка текста сообщения в задаче агенту
MAX_REPLY_TEXT = 4000                # оба API режут длинные сообщения сами,
                                     # обрезаем заранее, чтобы не ловить их ошибку


def _safe_name(name: str) -> str:
    """Убрать всё, что может вывести имя файла за пределы inbox-папки.

    Платформы присылают имя файла как есть (в т.ч. от пользователя) —
    доверять ему нельзя: '../../etc/passwd' в качестве file_name такой же
    реальный кейс, как markdown-обёртка пути в Workspace.clean().
    """
    name = Path(name).name.strip() or "file"
    cleaned = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)
    return cleaned[:200] or "file"


def _inbox_dir(ws: Workspace, channel: str, chat_key: Any) -> Path:
    d = ws.root / INBOX_DIRNAME / channel / _safe_name(str(chat_key))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _run_in_background(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, daemon=True).start()


def _build_task(text: str, saved_files: list[str]) -> str:
    """Единая формулировка задачи для агента — общая для Telegram и MAX."""
    parts = []
    if saved_files:
        parts.append("Пользователь прислал во вложении файл(ы): "
                     + ", ".join(saved_files) + ".")
        parts.append("Разберись, что в них (тип документа, ключевое "
                     "содержимое), и дай короткий понятный ответ по существу — "
                     "это уйдёт напрямую пользователю в чат, а не в отчёт.")
    if text:
        parts.append(f"Текст сообщения пользователя: {text[:MAX_TASK_TEXT]}")
    if not parts:
        parts.append("Пользователь прислал сообщение без текста и вложений "
                     "(например, стикер) — вежливо уточни, что нужно сделать.")
    return "\n".join(parts)


def _run_agent(cfg: Config, task: str,
              on_event: Callable[[str, dict], None] | None) -> str:
    """Собрать агента под конфиг вебхука и выполнить задачу.

    Тот же приём копирования конфига, что и в server.py._cfg_for: не
    трогаем оригинальный cfg сервера, apply_profile() применяется к
    независимой копии.
    """
    run_cfg = Config(**{**cfg.__dict__})
    if cfg.messaging.webhook_profile:
        run_cfg.apply_profile(cfg.messaging.webhook_profile)
    agent = build_agent(run_cfg, on_event=on_event)
    res = agent.run(task)
    return (res.answer or "(агент не дал ответа)")[:MAX_REPLY_TEXT]


# ============================================================= Telegram
def verify_telegram_secret(headers: Any, cfg: TelegramConfig) -> bool:
    """True только если секрет настроен И совпадает — сравнение по факту,
    не «секрет пуст, значит проверка не нужна» (см. serve() в server.py,
    где маршрут вообще не создаётся без webhook_secret)."""
    if not cfg.webhook_secret:
        return False
    got = headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    return got == cfg.webhook_secret


def register_telegram_webhook(cfg: TelegramConfig, url: str) -> dict[str, Any]:
    """POST setWebhook — вызывается один раз при развёртывании бота

    (например, из отдельного скрипта или консоли), НЕ автоматически при
    старте сервера: URL сервера снаружи агент не знает, а тестам реальный
    внешний HTTPS-адрес не нужен.
    """
    if not cfg.webhook_secret:
        raise ValueError(
            "Задайте telegram.webhook_secret перед регистрацией вебхука — "
            "без него Telegram будет присылать запросы без заголовка "
            "X-Telegram-Bot-Api-Secret-Token, а сервер их отклонит."
        )
    return telegram_api_request(cfg, "setWebhook", {
        "url": url, "secret_token": cfg.webhook_secret,
        "allowed_updates": ["message", "edited_message"],
    })


def _telegram_attachments(message: dict[str, Any]) -> list[tuple[str, str]]:
    """Список (file_id, предпочитаемое имя файла) во входящем сообщении."""
    out: list[tuple[str, str]] = []
    if message.get("document"):
        doc = message["document"]
        out.append((doc["file_id"], doc.get("file_name") or "document"))
    if message.get("photo"):
        # Telegram присылает один и тот же кадр в НЕСКОЛЬКИХ разрешениях —
        # это не несколько фотографий, берём только последний (наибольший).
        best = message["photo"][-1]
        out.append((best["file_id"], "photo.jpg"))
    if message.get("voice"):
        out.append((message["voice"]["file_id"], "voice.ogg"))
    if message.get("audio"):
        a = message["audio"]
        out.append((a["file_id"], a.get("file_name") or "audio"))
    if message.get("video"):
        out.append((message["video"]["file_id"], "video.mp4"))
    return out


def _reply_telegram(cfg: TelegramConfig, chat_id: Any, text: str) -> None:
    try:
        telegram_api_request(cfg, "sendMessage",
                             {"chat_id": chat_id, "text": text[:MAX_REPLY_TEXT]})
    except Exception:
        pass  # отправка ответа — best-effort: вебхук уже принят, не роняем его


def process_telegram_update(cfg: Config, update: dict[str, Any],
                            on_event: Callable[[str, dict], None] | None = None
                            ) -> None:
    """Обработать один Update Telegram: скачать вложения, запустить агента,

    ответить в тот же чат. Синхронная функция — вызывающий код
    (dispatch_telegram) уносит её в фоновый поток.
    """
    message = update.get("message") or update.get("edited_message")
    if not message:
        return  # тип обновления без сообщения (статус доставки и т.п.)
    tg_cfg = cfg.messaging.telegram
    chat_id = (message.get("chat") or {}).get("id")
    if chat_id is None:
        return
    text = (message.get("text") or message.get("caption") or "").strip()

    ws = Workspace(cfg.workspace)
    saved: list[str] = []
    try:
        for file_id, suggested_name in _telegram_attachments(message):
            data, remote_name = telegram_download_file(tg_cfg, file_id)
            name = _safe_name(remote_name or suggested_name)
            dest = _inbox_dir(ws, "telegram", chat_id)
            path = dest / name
            if path.exists():
                path = dest / f"{int(time.time())}_{name}"
            path.write_bytes(data)
            saved.append(str(ws.relative(path)))
    except Exception as exc:
        _reply_telegram(tg_cfg, chat_id, f"Не удалось скачать вложение: {exc}")
        return

    if not saved and not text:
        return  # пустое событие (стикер, реакция и т.п.) — агента не тревожим

    try:
        answer = _run_agent(cfg, _build_task(text, saved), on_event)
    except Exception as exc:
        answer = f"Не удалось обработать сообщение: {exc}"
    _reply_telegram(tg_cfg, chat_id, answer)


def dispatch_telegram(cfg: Config, update: dict[str, Any],
                      on_event: Callable[[str, dict], None] | None = None) -> None:
    """Точка входа из server.py: обработать один Update в фоновом потоке —

    HTTP-обработчик отвечает 200 сразу же, не дожидаясь агента."""
    _run_in_background(lambda: process_telegram_update(cfg, update, on_event))


# ================================================================== MAX
def verify_max_secret(headers: Any, cfg: MaxConfig) -> bool:
    if not cfg.webhook_secret:
        return False
    got = headers.get("X-Max-Bot-Api-Secret", "")
    return got == cfg.webhook_secret


def register_max_webhook(cfg: MaxConfig, url: str,
                         update_types: list[str] | None = None) -> dict[str, Any]:
    if not cfg.webhook_secret:
        raise ValueError(
            "Задайте max.webhook_secret перед регистрацией вебхука — без "
            "него MAX будет присылать запросы без заголовка "
            "X-Max-Bot-Api-Secret, а сервер их отклонит."
        )
    body = {"url": url, "secret": cfg.webhook_secret,
           "update_types": update_types or ["message_created", "bot_started"]}
    return max_api_request(cfg, "/subscriptions", "POST", body=body)


def _max_attachments(msg_body: dict[str, Any]) -> list[tuple[str, str]]:
    """Список (прямая ссылка, имя файла) во входящем сообщении MAX.

    В отличие от Telegram, MAX кладёт готовую скачиваемую ссылку прямо в
    attachment.payload.url — отдельного вызова вроде getFile не нужно.
    """
    out: list[tuple[str, str]] = []
    for att in msg_body.get("attachments") or []:
        kind = att.get("type")
        payload = att.get("payload") or {}
        url = payload.get("url")
        if not url:
            continue  # контакт/локация/стикер — не файл, ссылки на скачивание нет
        if kind == "image":
            out.append((url, "photo.jpg"))
        elif kind == "file":
            out.append((url, att.get("filename") or "file"))
        elif kind in ("video", "audio"):
            out.append((url, kind))
    return out


def _reply_max(cfg: MaxConfig, user_id: Any, chat_id: Any, text: str) -> None:
    params: dict[str, Any] = {}
    if user_id:
        params["user_id"] = user_id
    elif chat_id:
        params["chat_id"] = chat_id
    else:
        return
    try:
        max_api_request(cfg, "/messages", "POST", params=params,
                        body={"text": text[:MAX_REPLY_TEXT]})
    except Exception:
        pass  # best-effort, см. _reply_telegram


def process_max_update(cfg: Config, update: dict[str, Any],
                       on_event: Callable[[str, dict], None] | None = None) -> None:
    if update.get("update_type") != "message_created":
        return  # bot_started/message_edited и т.п. — вложений там не бывает
    message = update.get("message") or {}
    body = message.get("body") or {}
    recipient = message.get("recipient") or {}
    max_cfg = cfg.messaging.max
    user_id = (message.get("sender") or {}).get("user_id")
    chat_id = recipient.get("chat_id")
    # Личный диалог адресуется user_id, групповой чат/канал — chat_id;
    # оба поля не бывают заданы одновременно осмысленно, приоритет chat_id.
    target_user_id = None if chat_id else user_id
    text = (body.get("text") or "").strip()

    ws = Workspace(cfg.workspace)
    saved: list[str] = []
    try:
        for url, suggested_name in _max_attachments(body):
            data = max_download_attachment(url)
            name = _safe_name(suggested_name)
            dest = _inbox_dir(ws, "max", chat_id or user_id or "unknown")
            path = dest / name
            if path.exists():
                path = dest / f"{int(time.time())}_{name}"
            path.write_bytes(data)
            saved.append(str(ws.relative(path)))
    except Exception as exc:
        _reply_max(max_cfg, target_user_id, chat_id,
                  f"Не удалось скачать вложение: {exc}")
        return

    if not saved and not text:
        return

    try:
        answer = _run_agent(cfg, _build_task(text, saved), on_event)
    except Exception as exc:
        answer = f"Не удалось обработать сообщение: {exc}"
    _reply_max(max_cfg, target_user_id, chat_id, answer)


def dispatch_max(cfg: Config, update: dict[str, Any],
                 on_event: Callable[[str, dict], None] | None = None) -> None:
    _run_in_background(lambda: process_max_update(cfg, update, on_event))
