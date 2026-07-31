"""Тесты инструментов взаимодействия с человеком в контуре (hitl.*, ask.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.local.hitl import build_hitl_tools
from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. Человек в контуре и согласование (hitl.*, ask.*)")
    tools = {t.name: t for t in build_hitl_tools()}
    check("зарегистрировано 2 инструмента hitl/ask", len(tools) == 2)

    res_ask = tools["ask.human"].execute(
        question="Одобрить отчёт?", options_json='["Да", "Нет"]'
    )
    check("ask.human задаёт вопрос и возвращает ответ", "Ответ оператора: 'Да'" in res_ask)

    res_app = tools["hitl.request_approval"].execute(
        action="delete_db", reason="Тест", details_json='{"db": "test.db"}'
    )
    check("hitl.request_approval возвращает одобрение", "ОДОБРЕНО" in res_app)

    return summary("Тесты HITL и вопросов человеку")


def test_hitl_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
