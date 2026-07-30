"""Инструмент проверки статических HTML-сайтов для агентов MAOS."""
from __future__ import annotations

from typing import Any

from ..site_qa.local import check_site
from .base import Tool, ToolError, Workspace


def build(ws: Workspace) -> list[Tool]:
    def check_site_tool(subdir: str = "") -> str:
        subdir = (subdir or "").strip()
        try:
            target_dir = ws.resolve(subdir or ".")
        except Exception as exc:
            raise ToolError(f"Недопустимый путь для проверки: {exc}") from exc

        if not target_dir.exists() or not target_dir.is_dir():
            raise ToolError(f"Директория сайта {subdir!r} не найдена в workspace")

        res = check_site(target_dir)
        pages = res.get("pages", 0)
        errors = res.get("errors", [])
        if res.get("ok", False):
            return f"Сайт успешно проверен: найдено страниц — {pages}, ошибок нет."
        err_text = "\n".join(f"  - {err}" for err in errors[:20])
        more_info = f"\n  ... и ещё {len(errors) - 20} ошибок" if len(errors) > 20 else ""
        return f"Сайт проверен (страниц: {pages}). Найдено ошибок: {len(errors)}\n{err_text}{more_info}"

    return [
        Tool(
            "check_site",
            "Проверяет статический HTML-сайт в рабочей папке агента на наличие ошибок (отсутствие <title>, битые локальные ссылки и пути вне сайта).",
            {
                "type": "object",
                "properties": {
                    "subdir": {
                        "type": "string",
                        "description": "Подпапка в рабочей директории агента, где лежат HTML-файлы (по умолчанию корень рабочей папки)."
                    }
                },
                "required": []
            },
            check_site_tool
        )
    ]
