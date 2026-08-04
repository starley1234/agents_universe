"""Тесты локальных файловых инструментов и безопасности Workspace."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import (
    ArtifactStore,
    ToolError,
    Workspace,
    WorkspaceError,
)
from agent_toolkit.local import build_file_tools
from tests.harness import TempWorkspace, check, check_raises, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        section("1. Безопасность Workspace")
        ws = Workspace(tmp.path("ws"))
        check("корень Workspace создан", ws.root.exists())
        check("разрешение простого пути", ws.resolve("a.txt").name == "a.txt")
        check("разрешение вложенной папки", ws.resolve("sub/b.txt").parent.name == "sub")
        check_raises("выход через .. запрещён", WorkspaceError, ws.resolve, "../etc/passwd")
        check_raises("глубокий выход запрещён", WorkspaceError, ws.resolve, "a/../../secret")
        check("очистка markdown-ссылки", ws.clean("[test.txt](http://test.txt)") == "test.txt")
        check("очистка кавычек", ws.clean('"test.txt"') == "test.txt")

        section("2. Локальные файловые инструменты (files.*)")
        tools = {t.name: t for t in build_file_tools(ws)}
        check("все инструменты зарегистрированы", len(tools) == 10)

        # write_file
        res_write = tools["files.write_file"].execute(
            path="hello.txt", content="Hello, world!\nSecond line"
        )
        check("write_file возвращает статус", "Записано hello.txt" in res_write)
        check("файл реально создан", ws.exists("hello.txt"))

        # read_file
        res_read = tools["files.read_file"].execute(path="hello.txt")
        check("read_file читает полное содержимое", "Hello, world!" in res_read and "Second line" in res_read)
        res_lines = tools["files.read_file"].execute(path="hello.txt", start=1, end=1)
        check("read_file с диапазоном строк содержит номера", "1\t" in res_lines and "Hello, world!" in res_lines)

        # edit_file
        res_edit = tools["files.edit_file"].execute(
            path="hello.txt", old_text="world", new_text="Agent"
        )
        check("edit_file сообщает об успехе", "успешно отредактирован" in res_edit)
        check("содержимое файла обновлено", "Hello, Agent!" in ws.resolve("hello.txt").read_text(encoding="utf-8"))
        check_raises(
            "ошибка при отсутствии старого текста",
            ToolError,
            tools["files.edit_file"].execute,
            path="hello.txt",
            old_text="nonexistent",
            new_text="xyz",
        )

        # list_dir
        res_list = tools["files.list_dir"].execute(path=".")
        check("list_dir показывает файл", "hello.txt" in res_list and "[FILE]" in res_list)

        # find_files
        res_find = tools["files.find_files"].execute(pattern="*.txt")
        check("find_files находит файл по маске", "hello.txt" in res_find)

        # file_info
        res_info = tools["files.file_info"].execute(path="hello.txt")
        check("file_info возвращает размер и SHA256", "Размер:" in res_info and "SHA256:" in res_info)

        # remove_file
        res_rm = tools["files.remove_file"].execute(path="hello.txt")
        check("remove_file удаляет файл", "удалён" in res_rm)
        check("файл больше не существует", not ws.exists("hello.txt"))

        section("3. Хранилище артефактов (ArtifactStore)")
        store = ArtifactStore(workspace=ws)
        art = store.save_text("report.md", "# Отчёт\nУспешно", metadata={"tags": ["report"]})
        check("артефакт сохранён", art.name == "report.md" and art.size > 0)
        check("артефакт в индексе", len(store.list()) == 1)
        check("поиск артефакта по тегу", len(store.list(tag="report")) == 1)
        store.remove("report.md")
        check("артефакт удалён", len(store.list()) == 0)

    return summary("Тесты файловых инструментов")


def test_files_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
