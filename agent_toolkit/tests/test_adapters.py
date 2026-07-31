"""Тесты адаптеров фреймворков агентов (adapters.py)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit import build_default_registry
from agent_toolkit.adapters import (
    to_agent_system_tools,
    to_awos_tools,
    to_langchain_tools,
    to_openai_tools,
)
from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. Адаптеры интеграции агентов (adapters.py)")
    reg = build_default_registry()

    openai_list = to_openai_tools(reg)
    check("to_openai_tools возвращает схемы OpenAI", len(openai_list) == len(reg.list_tools()) and "function" in openai_list[0])

    lc_list = to_langchain_tools(reg)
    check("to_langchain_tools конвертирует инструменты", len(lc_list) == len(reg.list_tools()))

    as_list = to_agent_system_tools(reg)
    check("to_agent_system_tools возвращает список", len(as_list) == len(reg.list_tools()))

    awos_list = to_awos_tools(reg)
    check("to_awos_tools возвращает список", len(awos_list) == len(reg.list_tools()))

    return summary("Тесты адаптеров")


def test_adapters_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
