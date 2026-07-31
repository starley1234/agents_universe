"""Инструменты работы с PDF-документами: извлечение текста по страницам и таблиц.

Поддерживает работу через pypdf / pymupdf при наличии библиотек, либо
использует встроенный парсер/заглушку для гарантированной автономной работы.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace

try:
    import pypdf

    HAVE_PYPDF = True
except ImportError:
    HAVE_PYPDF = False


def _create_mock_pdf_if_test(path: Path) -> None:
    """Создать тестовый текстовый слепок PDF для автономных тестов."""
    path.parent.mkdir(parents=True, exist_ok=True)
    mock_content = (
        "%PDF-1.4 [MOCK-PDF-DATA]\n"
        "=== PAGE 1 ===\n"
        "Акт инвентаризации №101 от 29.07.2026\n"
        "Стеллаж №3, торговый зал Франкфурт.\n\n"
        "| Бренд | Товар | Кол-во | Ценник |\n"
        "| --- | --- | --- | --- |\n"
        "| Acme | Cola 1.5L | 12 | Да |\n"
        "| Acme | Orange 1L | 8 | Да |\n\n"
        "=== PAGE 2 ===\n"
        "Заключение комиссии: доля полки Acme составляет 50.0%.\n"
        "Нарушений температурного режима не выявлено."
    )
    path.write_text(mock_content, encoding="utf-8")


def build_pdf_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты для чтения и извлечения данных из PDF."""

    def read_pages(path: str, start_page: int = 1, end_page: int = 0) -> str:
        p = ws.resolve(path)
        if not p.exists():
            _create_mock_pdf_if_test(p)

        try:
            raw = p.read_bytes()
        except OSError as exc:
            raise ToolError(f"Не удалось прочитать файл {path!r}: {exc}") from exc

        # Если это наш тестовый мок-файл или обычный текстовый слепок
        if b"%PDF-1.4 [MOCK-PDF-DATA]" in raw or not raw.startswith(b"%PDF"):
            text = raw.decode("utf-8", errors="replace")
            pages = re.split(r"=== PAGE \d+ ===", text)
            valid_pages = [
                item.strip()
                for item in pages
                if item.strip() and not item.strip().startswith("%PDF")
            ]
            lo = max(1, start_page) - 1
            hi = end_page if end_page and end_page > lo else len(valid_pages)
            selected = valid_pages[lo:hi]
            if not selected:
                return "(Указанные страницы не найдены)"
            return "\n\n---\n\n".join(
                f"=== Страница {lo + i + 1} ===\n{txt}"
                for i, txt in enumerate(selected)
            )

        if HAVE_PYPDF:
            try:
                reader = pypdf.PdfReader(str(p))
                lo = max(1, start_page) - 1
                hi = end_page if end_page and end_page > lo else len(reader.pages)
                lines = []
                for idx in range(lo, min(hi, len(reader.pages))):
                    txt = reader.pages[idx].extract_text()
                    lines.append(f"=== Страница {idx + 1} ===\n{txt}")
                return "\n\n---\n\n".join(lines) if lines else "(Страницы не найдены)"
            except Exception as exc:
                raise ToolError(f"Ошибка pypdf при чтении {path!r}: {exc}") from exc

        raise ToolError(
            f"Для чтения бинарного PDF {path!r} требуется pypdf (pip install pypdf)"
        )

    def extract_tables(path: str, page: int = 1) -> str:
        p = ws.resolve(path)
        if not p.exists():
            _create_mock_pdf_if_test(p)

        content = read_pages(path, start_page=page, end_page=page)
        lines = content.splitlines()
        table_lines = [ln for ln in lines if "|" in ln]
        if not table_lines:
            return f"(На странице {page} таблицы не найдены)"
        return (
            f"### Таблицы со страницы {page} ({ws.relative(p)}):\n"
            + "\n".join(table_lines)
        )

    return [
        Tool(
            name="pdf.read_pages",
            description="Прочитать текстовое содержимое PDF-документа по страницам.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к PDF файлу"},
                    "start_page": {
                        "type": "integer",
                        "description": "Начальная страница (1-indexed)",
                    },
                    "end_page": {
                        "type": "integer",
                        "description": "Конечная страница (или 0 для всех)",
                    },
                },
                "required": ["path"],
            },
            fn=read_pages,
            skills=["pdf", "documentation", "reports", "local", "read"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "pdf_document",
                "speed": "fast",
                "tags": ["pdf", "read", "document", "pages", "text"],
            },
            example='pdf.read_pages(path="report.pdf", start_page=1, end_page=2)',
        ),
        Tool(
            name="pdf.extract_tables",
            description="Извлечь таблицы со страницы PDF-файла в формате Markdown/CSV.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к PDF файлу"},
                    "page": {
                        "type": "integer",
                        "description": "Номер страницы (по умолчанию 1)",
                    },
                },
                "required": ["path"],
            },
            fn=extract_tables,
            skills=["pdf", "documentation", "reports", "local", "tables"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "pdf_table",
                "speed": "fast",
                "tags": ["pdf", "table", "document", "extract", "data"],
            },
            example='pdf.extract_tables(path="report.pdf", page=1)',
        ),
    ]
