"""Локальные файловые инструменты: чтение, запись, редактирование, поиск."""
from __future__ import annotations

import fnmatch
import hashlib
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace

MAX_READ = 200_000
MAX_LIST = 500


def build_file_tools(ws: Workspace) -> list[Tool]:
    """Собрать набор инструментов для работы с файлами внутри рабочей области."""

    def read_file(path: str, start: int = 1, end: int = 0) -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        if p.is_dir():
            raise ToolError(f"{path!r} — это директория, не файл")
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise ToolError(f"Не удалось прочитать {path!r}: {exc}") from exc

        lines = text.splitlines()
        if start > 1 or end:
            lo = max(1, start) - 1
            hi = end if end and end > lo else len(lines)
            lines = lines[lo:hi]
            body = "\n".join(f"{lo + i + 1:>6}\t{ln}" for i, ln in enumerate(lines))
        else:
            body = text
        if len(body) > MAX_READ:
            body = body[:MAX_READ] + f"\n... обрезано на {MAX_READ} символах"
        return body or "(файл пуст)"

    def write_file(path: str, content: str) -> str:
        p = ws.resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            p.write_text(content, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Не удалось записать в {path!r}: {exc}") from exc
        return (
            f"Записано {ws.relative(p)} "
            f"({len(content)} символов, {len(content.splitlines())} строк)"
        )

    def edit_file(path: str, old_text: str, new_text: str) -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        try:
            content = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Не удалось прочитать {path!r}: {exc}") from exc

        count = content.count(old_text)
        if count == 0:
            raise ToolError(f"Фрагмент не найден в файле {path!r}")
        if count > 1:
            raise ToolError(
                f"Фрагмент встречается {count} раз(а), требуется уникальный участок кода"
            )

        updated = content.replace(old_text, new_text, 1)
        try:
            p.write_text(updated, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Не удалось сохранить изменения в {path!r}: {exc}") from exc
        return f"Файл {ws.relative(p)} успешно отредактирован"

    def list_dir(path: str = ".") -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Путь {path!r} не найден")
        if not p.is_dir():
            raise ToolError(f"{path!r} — это файл, не директория")

        items: list[str] = []
        try:
            for child in sorted(p.iterdir()):
                rel = ws.relative(child)
                if child.is_dir():
                    items.append(f"[DIR]  {rel}/")
                else:
                    size = child.stat().st_size
                    items.append(f"[FILE] {rel} ({size} B)")
                if len(items) >= MAX_LIST:
                    items.append(f"... (показаны первые {MAX_LIST} элементов)")
                    break
        except OSError as exc:
            raise ToolError(f"Ошибка чтения директории {path!r}: {exc}") from exc

        return "\n".join(items) if items else "(директория пуста)"

    def find_files(pattern: str, path: str = ".") -> str:
        p = ws.resolve(path)
        if not p.exists() or not p.is_dir():
            raise ToolError(f"Директория {path!r} не найдена")

        matches: list[str] = []
        try:
            for child in p.rglob("*"):
                if fnmatch.fnmatch(child.name, pattern):
                    matches.append(ws.relative(child))
                if len(matches) >= MAX_LIST:
                    matches.append(f"... (ограничение в {MAX_LIST} результатов)")
                    break
        except OSError as exc:
            raise ToolError(f"Ошибка поиска в {path!r}: {exc}") from exc

        return "\n".join(sorted(matches)) if matches else "(ничего не найдено)"

    def file_info(path: str) -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        st = p.stat()
        if p.is_dir():
            return f"Директория: {ws.relative(p)}\nРазмер: {st.st_size} B"
        sha = hashlib.sha256()
        with p.open("rb") as f:
            while chunk := f.read(65536):
                sha.update(chunk)
        lines = len(p.read_text(encoding="utf-8", errors="replace").splitlines())
        return (
            f"Файл: {ws.relative(p)}\n"
            f"Размер: {st.st_size} B\n"
            f"Строк: {lines}\n"
            f"SHA256: {sha.hexdigest()}"
        )

    def remove_file(path: str) -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        if p.is_dir():
            raise ToolError(f"{path!r} — это директория, используйте удаление папок осторожно")
        try:
            p.unlink()
        except OSError as exc:
            raise ToolError(f"Не удалось удалить файл {path!r}: {exc}") from exc
        return f"Файл {ws.relative(p)} удалён"

    return [
        Tool(
            name="files.read_file",
            description="Прочитать содержимое текстового файла с номерами строк.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу"},
                    "start": {"type": "integer", "description": "Номер первой строки"},
                    "end": {"type": "integer", "description": "Номер последней строки"},
                },
                "required": ["path"],
            },
            fn=read_file,
            skills=["files", "local", "filesystem", "read"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "file",
                "speed": "fast",
                "tags": ["file", "read", "text", "filesystem"],
            },
            example='files.read_file(path="notes.txt")',
        ),
        Tool(
            name="files.write_file",
            description="Создать или перезаписать текстовый файл.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу"},
                    "content": {"type": "string", "description": "Полное содержимое файла"},
                },
                "required": ["path", "content"],
            },
            fn=write_file,
            skills=["files", "local", "filesystem", "write"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "file",
                "speed": "fast",
                "tags": ["file", "write", "save", "text"],
            },
            example='files.write_file(path="report.md", content="# Заголовок")',
        ),
        Tool(
            name="files.edit_file",
            description="Точечная замена фрагмента текста в файле (по точному совпадению).",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу"},
                    "old_text": {"type": "string", "description": "Заменяемый текст"},
                    "new_text": {"type": "string", "description": "Новый текст"},
                },
                "required": ["path", "old_text", "new_text"],
            },
            fn=edit_file,
            skills=["files", "local", "filesystem", "edit"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "file",
                "speed": "fast",
                "tags": ["file", "edit", "patch", "text"],
            },
            example='files.edit_file(path="main.py", old_text="x = 1", new_text="x = 2")',
        ),
        Tool(
            name="files.list_dir",
            description="Посмотреть содержимое директории с размерами файлов.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к директории"}
                },
            },
            fn=list_dir,
            skills=["files", "local", "filesystem", "read"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "directory",
                "speed": "fast",
                "tags": ["file", "list", "directory", "folder"],
            },
            example='files.list_dir(path="data")',
        ),
        Tool(
            name="files.find_files",
            description="Найти файлы по шаблону/маске (например, '*.md' или 'test_*').",
            parameters={
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Маска файла (glob pattern)"},
                    "path": {"type": "string", "description": "Стартовая папка"},
                },
                "required": ["pattern"],
            },
            fn=find_files,
            skills=["files", "local", "filesystem", "search"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "file",
                "speed": "fast",
                "tags": ["file", "search", "find", "glob"],
            },
            example='files.find_files(pattern="*.json")',
        ),
        Tool(
            name="files.file_info",
            description="Получить метаданные файла: размер, количество строк и SHA256.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу"}
                },
                "required": ["path"],
            },
            fn=file_info,
            skills=["files", "local", "filesystem", "read"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "file",
                "speed": "fast",
                "tags": ["file", "stat", "metadata", "hash"],
            },
            example='files.file_info(path="data/records.csv")',
        ),
        Tool(
            name="files.remove_file",
            description="Удалить файл внутри рабочей области.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу"}
                },
                "required": ["path"],
            },
            fn=remove_file,
            skills=["files", "local", "filesystem", "delete"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": True,
                "resource_type": "file",
                "speed": "fast",
                "tags": ["file", "delete", "remove"],
            },
            example='files.remove_file(path="tmp.txt")',
        ),
    ]
