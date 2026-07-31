"""Тесты мессенджеров и электронной почты (SMTP/IMAP, Telegram, MAX Bot API)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.integrations import (
    build_max_tools,
    build_smtp_tools,
    build_telegram_tools,
)
from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. Электронная почта (SMTP / IMAP)")
    smtp_tools = {t.name: t for t in build_smtp_tools()}
    check("зарегистрировано 2 инструмента почты", len(smtp_tools) == 2)
    check("отправка почты помечена dangerous=True", smtp_tools["smtp.send_email"].dangerous is True)
    check("чтение почты безопасно (dangerous=False)", smtp_tools["smtp.read_emails"].dangerous is False)

    res_send = smtp_tools["smtp.send_email"].execute(
        to_addr="test@example.com", subject="Тест", body="Привет"
    )
    check("send_email возвращает статус отправки", "Письмо отправлено" in res_send)

    res_read = smtp_tools["smtp.read_emails"].execute(limit=2)
    check("read_emails читает входящие", "Входящие сообщения:" in res_read and "alice@example.com" in res_read)

    section("2. Мессенджер MAX Bot API")
    max_tools = {t.name: t for t in build_max_tools()}
    check("зарегистрировано 2 инструмента MAX", len(max_tools) == 2)
    check("отправка в MAX помечена dangerous=True", max_tools["max.send_message"].dangerous is True)

    res_max_send = max_tools["max.send_message"].execute(
        chat_id="chat-10", text="Отчёт готов"
    )
    check("send_message в MAX возвращает статус", "Сообщение в чат 'chat-10' отправлено" in res_max_send)

    res_max_read = max_tools["max.get_updates"].execute(limit=5)
    check("get_updates MAX возвращает обновления", "Сообщения MAX:" in res_max_read)

    section("3. Telegram Bot API")
    tg_tools = {t.name: t for t in build_telegram_tools()}
    check("зарегистрировано 2 инструмента Telegram", len(tg_tools) == 2)
    check("отправка в Telegram помечена dangerous=True", tg_tools["telegram.send_message"].dangerous is True)

    res_tg_send = tg_tools["telegram.send_message"].execute(
        chat_id="123456", text="Сообщение бота"
    )
    check("send_message в Telegram работает", "Сообщение отправлено в чат '123456'" in res_tg_send)

    res_tg_read = tg_tools["telegram.get_updates"].execute(limit=5)
    check("get_updates Telegram читает сообщения", "Сообщения Telegram:" in res_tg_read)

    return summary("Тесты мессенджеров и почты")


def test_messaging_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
