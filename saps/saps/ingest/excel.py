"""Парсер Excel (.xlsx) — второй формат отчуждаемых файлов (ТЗ п.3.1).

Тоже без openpyxl и по той же причине, что и Word-парсер: .xlsx — zip с
XML. Нужны три вещи: общая таблица строк (sharedStrings.xml), лист
(worksheets/sheetN.xml) и порядок листов (workbook.xml).

Отдельный нюанс, из-за которого наивный разбор ломается на реальных
выгрузках: Excel ПРОПУСКАЕТ пустые ячейки, а не пишет их пустыми.
Строка `<row><c r="A5">..</c><c r="D5">..</c></row>` — это A, потом
СРАЗУ D. Если читать ячейки подряд, значения уедут на две колонки
влево, и «Ответственный» окажется в «Статусе». Поэтому позиция каждой
ячейки вычисляется из её адреса (r="D5" -> колонка 3), а пропуски
заполняются пустыми строками.
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .word import (ATTR_COLUMNS, ParsedRequirement, ParseError, _apply_field,
                   _confidence, _norm_header, find_requirement_id)

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_CELL_REF = re.compile(r"^([A-Z]+)(\d+)$")


def _col_index(ref: str) -> int:
    """A -> 0, B -> 1, ..., AA -> 26."""
    m = _CELL_REF.match(ref.upper())
    if not m:
        return 0
    letters = m.group(1)
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return idx - 1


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in z.namelist():
        return []
    root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    out: list[str] = []
    for si in root.findall(f"{{{MAIN}}}si"):
        # Текст ячейки может быть разбит на несколько <t> (rich text).
        parts = [t.text or "" for t in si.iter(f"{{{MAIN}}}t")]
        out.append("".join(parts))
    return out


def _sheet_names(z: zipfile.ZipFile) -> list[tuple[str, str]]:
    """[(имя листа, путь к xml)] в порядке из книги."""
    names = z.namelist()
    if "xl/workbook.xml" not in names:
        return []
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in names:
        rroot = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
        for rel in rroot:
            rid = rel.get("Id", "")
            target = rel.get("Target", "")
            if target and not target.startswith("/"):
                target = "xl/" + target.lstrip("./")
            rels[rid] = target
    out: list[tuple[str, str]] = []
    sheets = wb.find(f"{{{MAIN}}}sheets")
    if sheets is None:
        return []
    for i, sheet in enumerate(sheets.findall(f"{{{MAIN}}}sheet"), start=1):
        name = sheet.get("name", f"Лист{i}")
        rid = sheet.get(f"{{{REL}}}id", "")
        path = rels.get(rid) or f"xl/worksheets/sheet{i}.xml"
        if path in names:
            out.append((name, path))
    return out


def read_sheet(z: zipfile.ZipFile, path: str,
               shared: list[str]) -> list[list[str]]:
    """Лист -> список строк, пропуски заполнены пустыми ячейками."""
    root = ET.fromstring(z.read(path))
    rows: list[list[str]] = []
    data = root.find(f"{{{MAIN}}}sheetData")
    if data is None:
        return rows
    for row in data.findall(f"{{{MAIN}}}row"):
        cells: dict[int, str] = {}
        for c in row.findall(f"{{{MAIN}}}c"):
            ref = c.get("r", "")
            idx = _col_index(ref) if ref else len(cells)
            ctype = c.get("t", "")
            if ctype == "inlineStr":
                is_el = c.find(f"{{{MAIN}}}is")
                value = "".join(t.text or "" for t in is_el.iter(f"{{{MAIN}}}t")) \
                    if is_el is not None else ""
            else:
                v = c.find(f"{{{MAIN}}}v")
                raw = v.text if v is not None and v.text is not None else ""
                if ctype == "s":
                    try:
                        value = shared[int(raw)]
                    except (ValueError, IndexError):
                        value = ""
                else:
                    value = raw
            if value:
                cells[idx] = value.strip()
        if not cells:
            rows.append([])
            continue
        width = max(cells) + 1
        rows.append([cells.get(i, "") for i in range(width)])
    return rows


def read_workbook(path: str | Path) -> dict[str, list[list[str]]]:
    """Прочитать .xlsx: {имя листа: строки}."""
    p = Path(path)
    if not p.exists():
        raise ParseError(f"Файл не найден: {p}")
    try:
        with zipfile.ZipFile(p) as z:
            if "xl/workbook.xml" not in z.namelist():
                raise ParseError(
                    f"{p.name}: это не книга Excel (.xlsx). Если файл в "
                    "формате .xls — пересохраните его как .xlsx.")
            shared = _shared_strings(z)
            return {name: read_sheet(z, sheet_path, shared)
                    for name, sheet_path in _sheet_names(z)}
    except zipfile.BadZipFile as exc:
        raise ParseError(
            f"{p.name}: не читается как .xlsx (повреждён или это .xls)") from exc


def _find_header(rows: list[list[str]]) -> int:
    """Индекс строки-шапки.

    Реальные выгрузки почти всегда начинаются с названия отчёта, даты и
    пустых строк, поэтому шапка редко находится в первой строке. Ищем
    первую строку, где хотя бы два столбца — известные имена полей.
    """
    for i, row in enumerate(rows[:30]):
        known = sum(1 for c in row if _norm_header(c) in ATTR_COLUMNS)
        if known >= 2:
            return i
    return -1


def parse_xlsx(path: str | Path) -> list[ParsedRequirement]:
    """Разобрать книгу: каждая строка под шапкой — кандидат в требования."""
    sheets = read_workbook(path)
    out: list[ParsedRequirement] = []
    counter = 0
    for sheet_name, rows in sheets.items():
        head = _find_header(rows)
        if head < 0:
            continue
        header = [_norm_header(c) for c in rows[head]]
        for row in rows[head + 1:]:
            if not any(c.strip() for c in row):
                continue
            req = ParsedRequirement(section_path=sheet_name,
                                    origin="excel_row")
            for i, cell in enumerate(row):
                if i >= len(header) or not header[i]:
                    continue
                field_name = ATTR_COLUMNS.get(header[i])
                if field_name:
                    _apply_field(req, field_name, cell)
                elif cell.strip():
                    req.attributes[header[i]] = cell.strip()
            if not req.text and not req.external_id:
                continue
            if not req.external_id:
                req.external_id = find_requirement_id(req.text)
            counter += 1
            req.ord = counter
            req.confidence, req.notes = _confidence(
                req.text, bool(req.external_id), "table_row")
            out.append(req)
    return out


def sheet_summary(path: str | Path) -> dict[str, Any]:
    """Что вообще лежит в книге — для диагностики импорта."""
    sheets = read_workbook(path)
    out: dict[str, Any] = {}
    for name, rows in sheets.items():
        head = _find_header(rows)
        out[name] = {
            "rows": len(rows),
            "header_row": head,
            "header": rows[head] if head >= 0 else [],
            "recognized": bool(head >= 0),
        }
    return out
