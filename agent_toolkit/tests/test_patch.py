"""Тесты инструментов редактирования и наложения патчей (patch.*, text.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import Workspace
from agent_toolkit.local.patch import build_patch_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        ws = Workspace(tmp.path("ws"))
        section("1. Инструменты регулярных выражений и патчей (text.*, code.apply_patch)")
        tools = {t.name: t for t in build_patch_tools(ws)}
        check("зарегистрировано 2 инструмента patch", len(tools) == 2)

        p1 = ws.resolve("file.txt")
        p1.write_text("hello   world\ntest 123", encoding="utf-8")
        res_re = tools["text.regex_replace"].execute(
            path="file.txt", pattern=r"hello\s+world", replacement="hello_agent"
        )
        check("regex_replace заменяет по регулярному выражению", "выполнено замен: 1" in res_re)
        check("файл обновлён", "hello_agent" in p1.read_text(encoding="utf-8"))

        p2 = ws.resolve("code.py")
        p2.write_text("def old(): pass\n", encoding="utf-8")
        patch_txt = "--- a/code.py\n+++ b/code.py\n@@ -1,1 +1,1 @@\n-def old(): pass\n+def new(): return 42"
        res_patch = tools["code.apply_patch"].execute(path="code.py", patch_content=patch_txt)
        check("apply_patch накладывает unified diff патч", "успешно наложен" in res_patch)
        check("содержимое файла изменено патчем", "def new(): return 42" in p2.read_text(encoding="utf-8"))

    return summary("Тесты патчей и RegEx")


def test_patch_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
