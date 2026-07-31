"""Тесты инструментов песочницы: выполнение Shell-команд и Python (sandbox.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import Workspace
from agent_toolkit.local.sandbox import build_sandbox_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        ws = Workspace(tmp.path("ws"))
        section("1. Инструменты песочницы Shell и Python (shell.*, python.*)")
        tools = {t.name: t for t in build_sandbox_tools(ws)}
        check("зарегистрировано 2 инструмента sandbox", len(tools) == 2)
        check("shell.run_command помечен dangerous=True", tools["shell.run_command"].dangerous is True)
        check("python.exec_snippet помечен dangerous=True", tools["python.exec_snippet"].dangerous is True)

        res_sh = tools["shell.run_command"].execute(command="echo 'Sandbox ok'")
        check("run_command выполняет shell и возвращает вывод", "Sandbox ok" in res_sh)

        res_py = tools["python.exec_snippet"].execute(code="x = 7 * 8\nprint('Result:', x)")
        check("exec_snippet выполняет Python и возвращает вывод", "Result: 56" in res_py)

    return summary("Тесты инструментов песочницы")


def test_sandbox_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
