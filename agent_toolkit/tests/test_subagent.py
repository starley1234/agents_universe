"""Тесты инструментов многоагентной оркестрации и MapReduce (agent.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.local.subagent import build_subagent_tools
from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. Многоагентная оркестрация, субагенты и MapReduce (agent.*)")
    tools = {t.name: t for t in build_subagent_tools()}
    check("зарегистрировано 4 инструмента agent", len(tools) == 4)

    res_list = tools["agent.list_agents"].execute()
    check("agent.list_agents возвращает список (researcher, coder, auditor)", "researcher" in res_list and "auditor" in res_list)

    res_call = tools["agent.call_subagent"].execute(
        agent_name="auditor", task="Проверить отчёт на соответствие нормам"
    )
    check("agent.call_subagent успешно вызывает субагента", "SUBAGENT AUDITOR" in res_call)

    res_mr = tools["agent.parallel_map_reduce"].execute(
        agent_name="researcher", tasks_json='["Анализ рынка А", "Анализ рынка Б"]'
    )
    check("agent.parallel_map_reduce выполняет задачи параллельно и сводит отчёт", "MapReduce" in res_mr and "2/2" in res_mr and "Сводный отчёт" in res_mr)

    return summary("Тесты субагентов и MapReduce")


def test_subagent_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
