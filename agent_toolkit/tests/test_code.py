"""Тесты инструментов контроля версий (git.*) и проверки кода (code.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import Workspace
from agent_toolkit.local.code import build_code_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        ws = Workspace(tmp.path("ws"))
        section("1. Инструменты Git и анализа кода (git.*, code.*)")
        tools = {t.name: t for t in build_code_tools(ws)}
        check("зарегистрировано 5 инструментов code/git", len(tools) == 5)

        res_status = tools["git.status"].execute(path=".")
        check("git.status возвращает вывод", "Git Status" in res_status)

        res_diff = tools["git.diff"].execute(path=".")
        check("git.diff работает", "Git Diff" in res_diff)

        res_log = tools["git.log"].execute(path=".", limit=3)
        check("git.log работает", "Последние коммиты" in res_log)

        # Создадим тестовый python файл
        p_py = ws.resolve("sample.py")
        p_py.write_text("x = 10 + 20\nprint(x)\n", encoding="utf-8")

        res_lint = tools["code.run_linter"].execute(path="sample.py")
        check("code.run_linter успешно проверяет синтаксис", "прошла успешно" in res_lint)

        p_bad = ws.resolve("bad.py")
        p_bad.write_text("def broken(: print('err')\n", encoding="utf-8")
        res_bad = tools["code.run_linter"].execute(path="bad.py")
        check("code.run_linter обнаруживает ошибку синтаксиса", "Ошибки синтаксиса" in res_bad)

        p_test = ws.resolve("test_sample.py")
        p_test.write_text(
            "import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertEqual(1, 1)\n",
            encoding="utf-8",
        )
        res_test = tools["code.run_tests"].execute(test_file="test_sample.py")
        check("code.run_tests выполняет тест и возвращает статус", "ТЕСТЫ ПРОЙДЕНЫ" in res_test)

    return summary("Тесты Git и анализа кода")


def test_code_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
