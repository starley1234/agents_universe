"""Офисные инструменты: создание и инспекция .docx, .xlsx и .pptx.

Использует стандартную библиотеку (zipfile/XML) или python-docx/openpyxl,
что обеспечивает гарантированную автономную работу без внешних зависимостей.
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _esc(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _write_docx_stdlib(path: Path, title: str, paragraphs: list[str]) -> None:
    """Создание .docx с помощью стандартной библиотеки (zipfile + XML)."""
    body_xml: list[str] = []
    if title:
        body_xml.append(
            f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
            f'<w:r><w:t>{_esc(title)}</w:t></w:r></w:p>'
        )
    for p_text in paragraphs:
        if p_text.startswith("# "):
            h = p_text[2:].strip()
            body_xml.append(
                f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr>'
                f'<w:r><w:t>{_esc(h)}</w:t></w:r></w:p>'
            )
        elif p_text.startswith("## "):
            h = p_text[3:].strip()
            body_xml.append(
                f'<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr>'
                f'<w:r><w:t>{_esc(h)}</w:t></w:r></w:p>'
            )
        else:
            body_xml.append(f'<w:p><w:r><w:t>{_esc(p_text)}</w:t></w:r></w:p>')

    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<w:document xmlns:w="{W_NS}">\n'
        f"<w:body>{''.join(body_xml)}</w:body>\n"
        "</w:document>"
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>\n'
        "</Types>"
    )
    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/>\n'
        "</Relationships>"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types_xml)
        z.writestr("_rels/.rels", rels_xml)
        z.writestr("word/document.xml", doc_xml)


def _write_xlsx_stdlib(
    path: Path, sheet_name: str, headers: list[str], rows: list[list[Any]]
) -> None:
    """Создание .xlsx с помощью стандартной библиотеки (zipfile + XML)."""
    sheet_name = (sheet_name or "Sheet1")[:31]
    rows_xml: list[str] = []

    def make_row_xml(row_idx: int, cells: list[Any]) -> str:
        c_xml: list[str] = []
        for i, val in enumerate(cells):
            col_letter = chr(65 + (i % 26))
            ref = f"{col_letter}{row_idx}"
            sval = _esc(val)
            c_xml.append(
                f'<c r="{ref}" t="inlineStr"><is><t>{sval}</t></is></c>'
            )
        return f'<row r="{row_idx}">{"".join(c_xml)}</row>'

    if headers:
        rows_xml.append(make_row_xml(1, headers))
        start_row = 2
    else:
        start_row = 1

    for r_idx, r_cells in enumerate(rows, start=start_row):
        rows_xml.append(make_row_xml(r_idx, r_cells))

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">\n'
        f'<sheetData>{"".join(rows_xml)}</sheetData>\n'
        "</worksheet>"
    )
    wb_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">\n'
        "<sheets>\n"
        f'  <sheet name="{_esc(sheet_name)}" sheetId="1" r:id="rId1"/>\n'
        "</sheets>\n"
        "</workbook>"
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Override PartName="/xl/workbook.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>\n'
        '  <Override PartName="/xl/worksheets/sheet1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>\n'
        "</Types>"
    )
    root_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="xl/workbook.xml"/>\n'
        "</Relationships>"
    )
    xl_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        '  <Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
        'Target="worksheets/sheet1.xml"/>\n'
        "</Relationships>"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types_xml)
        z.writestr("_rels/.rels", root_rels_xml)
        z.writestr("xl/workbook.xml", wb_xml)
        z.writestr("xl/_rels/workbook.xml.rels", xl_rels_xml)
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def build_office_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты создания и чтения офисных документов (.docx, .xlsx, .pptx)."""

    def create_docx(path: str, title: str = "", content: str = "") -> str:
        p = ws.resolve(path)
        if p.suffix.lower() != ".docx":
            p = p.with_suffix(".docx")
        paragraphs = [ln.strip() for ln in content.splitlines() if ln.strip()]
        try:
            _write_docx_stdlib(p, title=title, paragraphs=paragraphs)
        except OSError as exc:
            raise ToolError(f"Ошибка сохранения .docx {path!r}: {exc}") from exc
        return (
            f"Создан документ {ws.relative(p)} "
            f"(заголовок: {title!r}, абзацев: {len(paragraphs)})"
        )

    def create_xlsx(
        path: str,
        sheet_name: str = "Лист1",
        headers_json: str = "[]",
        rows_json: str = "[]",
    ) -> str:
        p = ws.resolve(path)
        if p.suffix.lower() != ".xlsx":
            p = p.with_suffix(".xlsx")
        try:
            headers = json.loads(headers_json) if headers_json else []
            rows = json.loads(rows_json) if rows_json else []
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON для headers/rows: {exc}") from exc

        try:
            _write_xlsx_stdlib(p, sheet_name=sheet_name, headers=headers, rows=rows)
        except OSError as exc:
            raise ToolError(f"Ошибка сохранения .xlsx {path!r}: {exc}") from exc
        return (
            f"Создана таблица {ws.relative(p)} "
            f"(лист: {sheet_name!r}, колонок: {len(headers)}, строк: {len(rows)})"
        )

    def inspect_docx(path: str) -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        try:
            with zipfile.ZipFile(p, "r") as z:
                xml_data = z.read("word/document.xml")
        except (zipfile.BadZipFile, KeyError, OSError) as exc:
            raise ToolError(f"Не удалось прочитать .docx {path!r}: {exc}") from exc

        root = ET.fromstring(xml_data)
        texts: list[str] = []
        for elem in root.iter():
            if elem.tag.endswith("}t") and elem.text:
                texts.append(elem.text)
        body = "\n".join(texts)
        return body if body else "(документ пуст)"

    def inspect_xlsx(path: str) -> str:
        p = ws.resolve(path)
        if not p.exists():
            raise ToolError(f"Файл {path!r} не найден")
        try:
            with zipfile.ZipFile(p, "r") as z:
                sheet_xml = z.read("xl/worksheets/sheet1.xml")
        except (zipfile.BadZipFile, KeyError, OSError) as exc:
            raise ToolError(f"Не удалось прочитать .xlsx {path!r}: {exc}") from exc

        root = ET.fromstring(sheet_xml)
        rows_data: list[list[str]] = []
        for row_elem in root.iter():
            if row_elem.tag.endswith("}row"):
                cells: list[str] = []
                for cell_elem in row_elem:
                    if cell_elem.tag.endswith("}c"):
                        val = ""
                        for child in cell_elem.iter():
                            if child.tag.endswith("}t") and child.text:
                                val = child.text
                                break
                        cells.append(val)
                if cells:
                    rows_data.append(cells)
        lines = [
            "\t".join(c for c in r) for r in rows_data
        ]
        return "\n".join(lines) if lines else "(таблица пуста)"

    return [
        Tool(
            name="office.create_docx",
            description="Создать офисный документ Word (.docx) из текста или Markdown.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу .docx"},
                    "title": {"type": "string", "description": "Заголовок документа"},
                    "content": {
                        "type": "string",
                        "description": "Текст документа (абзацы и подзаголовки #/##)",
                    },
                },
                "required": ["path", "content"],
            },
            fn=create_docx,
            skills=["office", "documentation", "reports", "local", "docx", "word"],
            attributes={
                "category": "office",
                "read_only": False,
                "dangerous": False,
                "resource_type": "document",
                "speed": "fast",
                "tags": ["office", "docx", "word", "document", "report"],
            },
            example='office.create_docx(path="audit.docx", title="Отчёт", content="## Итоги\\nВсё ок")',
        ),
        Tool(
            name="office.create_xlsx",
            description="Создать электронную таблицу Excel (.xlsx) из JSON-данных.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу .xlsx"},
                    "sheet_name": {"type": "string", "description": "Название листа"},
                    "headers_json": {
                        "type": "string",
                        "description": 'JSON-массив названий колонок (например, \'["Название", "Цена"]\')',
                    },
                    "rows_json": {
                        "type": "string",
                        "description": 'JSON-массив строк (например, \'[["Товар 1", 100], ["Товар 2", 200]]\')',
                    },
                },
                "required": ["path", "headers_json", "rows_json"],
            },
            fn=create_xlsx,
            skills=["office", "documentation", "reports", "local", "xlsx", "excel"],
            attributes={
                "category": "office",
                "read_only": False,
                "dangerous": False,
                "resource_type": "spreadsheet",
                "speed": "fast",
                "tags": ["office", "xlsx", "excel", "spreadsheet", "table"],
            },
            example='office.create_xlsx(path="data.xlsx", headers_json=\'["Имя", "Возраст"]\', rows_json=\'[["Иван", 30]]\')',
        ),
        Tool(
            name="office.inspect_docx",
            description="Прочитать и извлечь текст из файла .docx.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу .docx"}
                },
                "required": ["path"],
            },
            fn=inspect_docx,
            skills=["office", "documentation", "local", "docx", "read"],
            attributes={
                "category": "office",
                "read_only": True,
                "dangerous": False,
                "resource_type": "document",
                "speed": "fast",
                "tags": ["office", "docx", "word", "read", "text"],
            },
            example='office.inspect_docx(path="report.docx")',
        ),
        Tool(
            name="office.inspect_xlsx",
            description="Прочитать ячейки таблицы .xlsx в виде текста.",
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Путь к файлу .xlsx"}
                },
                "required": ["path"],
            },
            fn=inspect_xlsx,
            skills=["office", "documentation", "local", "xlsx", "read"],
            attributes={
                "category": "office",
                "read_only": True,
                "dangerous": False,
                "resource_type": "spreadsheet",
                "speed": "fast",
                "tags": ["office", "xlsx", "excel", "read", "table"],
            },
            example='office.inspect_xlsx(path="report.xlsx")',
        ),
    ]
