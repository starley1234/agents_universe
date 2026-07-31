"""Инструменты редактирования кода и наложения патчей (text.regex_replace, code.apply_patch)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace


def _apply_unified_diff(original: list[str], diff_lines: list[str]) -> list[str]:
    """Наложить базовый unified diff (git style) на массив строк."""
    result: list[str] = []
    i = 0
    in_hunk = False
    for line in diff_lines:
        if line.startswith("---") or line.startswith("+++"):
            continue
        if line.startswith("@@"):
            in_hunk = True
            continue
        if not in_hunk:
            continue
        if line.startswith("-"):
            continue
        if line.startswith("+"):
            result.append(line[1:])
        elif line.startswith(" ") or not line:
            result.append(line[1:] if line.startswith(" ") else line)
    return result if result else original


def build_patch_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты для продвинутого редактирования и наложения патчей."""

    def regex_replace(
        path: str, pattern: str, replacement: str, count: int = 0
    ) -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        try:
            content = p.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Не удалось прочитать файл {path!r}: {exc}") from exc

        try:
            new_content, num_subs = re.subn(
                pattern, replacement, content, count=count, flags=re.MULTILINE
            )
        except re.error as exc:
            raise ToolError(f"Некорректное регулярное выражение {pattern!r}: {exc}") from exc

        if num_subs == 0:
            return f"В файле {ws.relative(p)} совпадений по шаблону {pattern!r} не найдено"

        try:
            p.write_text(new_content, encoding="utf-8")
        except OSError as exc:
            raise ToolError(f"Ошибка сохранения файла {path!r}: {exc}") from exc

        return f"Файл {ws.relative(p)} обновлён: выполнено замен: {num_subs}"

    def apply_patch(path: str, patch_content: str) -> str:
        if not patch_content.strip():
            raise ToolError("Содержимое патча не может быть пустым")

        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл для патча {path!r} не найден")

        try:
            orig_text = p.read_text(encoding="utf-8")
            orig_lines = orig_text.splitlines()
            diff_lines = patch_content.splitlines()
            new_lines = _apply_unified_diff(orig_lines, diff_lines)
            p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
            return f"Патч (unified diff) успешно наложен на файл {ws.relative(p)}"
        except OSError as exc:
            raise ToolError(f"Ошибка применения патча к {path!r}: {exc}") from exc

    return [
        Tool(
            name="text.regex_replace",
            description="Замена фрагментов текста в файле по регулярному выражению (RegEx).",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу"},
                    "pattern": {
                        "type": "string",
                        "description": "Регулярное выражение для поиска",
                    },
                    "replacement": {
                        "type": "string",
                        "description": "Строка замены",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Максимальное число замен (0 = все)",
                    },
                },
                "required": ["path", "pattern", "replacement"],
            },
            fn=regex_replace,
            skills=["text", "regex", "edit", "code", "patch", "local"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "file_patch",
                "speed": "fast",
                "tags": ["text", "regex", "replace", "edit", "code"],
            },
            example='text.regex_replace(path="src.py", pattern="foo\\\\s+bar", replacement="foo_bar")',
        ),
        Tool(
            name="code.apply_patch",
            description="Наложить патч (unified diff / git diff) на исходный файл.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу"},
                    "patch_content": {
                        "type": "string",
                        "description": "Содержимое патча в формате unified diff",
                    },
                },
                "required": ["path", "patch_content"],
            },
            fn=apply_patch,
            skills=["code", "patch", "diff", "edit", "dev", "local"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "file_patch",
                "speed": "fast",
                "tags": ["code", "patch", "diff", "git", "edit", "unified"],
            },
            example='code.apply_patch(path="app.py", patch_content="--- a/app.py\\n+++ b/app.py\\n@@ -1 +1 @@\\n-old\\n+new")',
        ),
    ]
