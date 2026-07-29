"""Создание .docx и .xlsx без внешних библиотек (ТЗ п.6.3).

Симметрично парсерам: те читают Office-форматы стандартной библиотекой,
эти — пишут. Оба формата суть zip с XML, и минимальный корректный файл
собирается из пяти-шести частей. Проверено открытием в Word/Excel и
обратным разбором собственным парсером САПС (см. тесты: roundtrip).

Зачем своё, если есть python-docx/openpyxl: та же причина, что и для
парсеров — установка пакетов в КБ согласуется, а выгрузка «в Excel для
совещания» нужна сразу. Объём кода невелик, потому что нам не нужен
редактор документов: нужны заголовки, абзацы и таблицы.
"""
from __future__ import annotations

import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
S = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"
PR = "http://schemas.openxmlformats.org/package/2006/relationships"


def _esc(value: Any) -> str:
    text = "" if value is None else str(value)
    out = (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
           .replace('"', "&quot;"))
    # Управляющие символы делают файл нечитаемым для Word/Excel.
    return "".join(ch for ch in out if ch >= " " or ch in "\t\n")


# ============================ WORD ========================================
def _w_paragraph(text: str, *, style: str = "", bold: bool = False,
                 size: int | None = None) -> str:
    props = []
    if style:
        props.append(f'<w:pStyle w:val="{_esc(style)}"/>')
    ppr = f"<w:pPr>{''.join(props)}</w:pPr>" if props else ""
    rprops = []
    if bold:
        rprops.append("<w:b/>")
    if size:
        rprops.append(f'<w:sz w:val="{size * 2}"/>')
    rpr = f"<w:rPr>{''.join(rprops)}</w:rPr>" if rprops else ""
    # Многострочный текст: переносы внутри абзаца — <w:br/>.
    lines = _esc(text).split("\n")
    runs = []
    for i, line in enumerate(lines):
        if i:
            runs.append("<w:r><w:br/></w:r>")
        runs.append(f'<w:r>{rpr}<w:t xml:space="preserve">{line}</w:t></w:r>')
    return f"<w:p>{ppr}{''.join(runs)}</w:p>"


def _w_heading(text: str, level: int = 1) -> str:
    # И стиль, и outlineLvl: стиль для Word, outlineLvl — чтобы наш же
    # парсер (и чужие инструменты) видели иерархию без таблицы стилей.
    return (f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/>'
            f'<w:outlineLvl w:val="{level - 1}"/></w:pPr>'
            f'<w:r><w:rPr><w:b/><w:sz w:val="{(28 - level * 2) * 2}"/></w:rPr>'
            f'<w:t xml:space="preserve">{_esc(text)}</w:t></w:r></w:p>')


def _w_table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    def cell(value: Any, bold: bool = False) -> str:
        rpr = "<w:rPr><w:b/></w:rPr>" if bold else ""
        text = _esc(value).split("\n")
        runs = []
        for i, line in enumerate(text):
            if i:
                runs.append("<w:r><w:br/></w:r>")
            runs.append(f'<w:r>{rpr}<w:t xml:space="preserve">{line}</w:t></w:r>')
        return (f'<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>'
                f'<w:p>{"".join(runs)}</w:p></w:tc>')

    out = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
           '<w:tblW w:w="5000" w:type="pct"/>'
           '<w:tblBorders>'
           '<w:top w:val="single" w:sz="4" w:color="999999"/>'
           '<w:left w:val="single" w:sz="4" w:color="999999"/>'
           '<w:bottom w:val="single" w:sz="4" w:color="999999"/>'
           '<w:right w:val="single" w:sz="4" w:color="999999"/>'
           '<w:insideH w:val="single" w:sz="4" w:color="999999"/>'
           '<w:insideV w:val="single" w:sz="4" w:color="999999"/>'
           '</w:tblBorders></w:tblPr>']
    if header:
        out.append("<w:tr>" + "".join(cell(h, True) for h in header) + "</w:tr>")
    for row in rows:
        out.append("<w:tr>" + "".join(cell(c) for c in row) + "</w:tr>")
    out.append("</w:tbl>")
    return "".join(out)


def write_docx(path: str | Path, blocks: Sequence[dict[str, Any]], *,
               title: str = "") -> Path:
    """Собрать .docx из блоков.

    Блок: {"type": "heading"|"paragraph"|"table", ...}
      heading:   {"text": str, "level": int}
      paragraph: {"text": str, "bold": bool}
      table:     {"header": [...], "rows": [[...], ...]}
    """
    body: list[str] = []
    if title:
        body.append(_w_heading(title, 1))
    for block in blocks:
        kind = block.get("type", "paragraph")
        if kind == "heading":
            body.append(_w_heading(str(block.get("text", "")),
                                   int(block.get("level", 1))))
        elif kind == "table":
            body.append(_w_table(block.get("header", []),
                                 block.get("rows", [])))
        else:
            body.append(_w_paragraph(str(block.get("text", "")),
                                     bold=bool(block.get("bold"))))
    body.append('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
                '<w:pgMar w:top="1134" w:right="850" w:bottom="1134" '
                'w:left="1134"/></w:sectPr>')

    document = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<w:document xmlns:w="{W}"><w:body>{"".join(body)}</w:body>'
                f'</w:document>')

    content_types = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{CT}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
        'package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" ContentType="application/'
        'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>')
    rels = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{PR}">'
            f'<Relationship Id="rId1" Type="{R}/officeDocument" '
            'Target="word/document.xml"/></Relationships>')

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("word/document.xml", document)
    return p


# ============================ EXCEL =======================================
def _col_letter(idx: int) -> str:
    letters = ""
    idx += 1
    while idx:
        idx, rem = divmod(idx - 1, 26)
        letters = chr(ord("A") + rem) + letters
    return letters


def write_xlsx(path: str | Path, sheets: dict[str, dict[str, Any]]) -> Path:
    """Собрать .xlsx.

    sheets: {"Имя листа": {"header": [...], "rows": [[...], ...]}}
    Строки пишутся inline-строками (t="inlineStr") — так не нужен
    sharedStrings, а файл всё равно корректен и открывается Excel.
    """
    if not sheets:
        sheets = {"Лист1": {"header": [], "rows": []}}

    sheet_xml: dict[str, str] = {}
    for i, (name, data) in enumerate(sheets.items(), start=1):
        header = list(data.get("header", []))
        rows = list(data.get("rows", []))
        out_rows: list[str] = []
        r = 1
        if header:
            cells = "".join(
                f'<c r="{_col_letter(c)}{r}" t="inlineStr" s="1">'
                f'<is><t xml:space="preserve">{_esc(v)}</t></is></c>'
                for c, v in enumerate(header))
            out_rows.append(f'<row r="{r}">{cells}</row>')
            r += 1
        for row in rows:
            cells = []
            for c, value in enumerate(row):
                ref = f"{_col_letter(c)}{r}"
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    cells.append(f'<c r="{ref}"><v>{value}</v></c>')
                else:
                    cells.append(
                        f'<c r="{ref}" t="inlineStr"><is>'
                        f'<t xml:space="preserve">{_esc(value)}</t></is></c>')
            out_rows.append(f'<row r="{r}">{"".join(cells)}</row>')
            r += 1
        sheet_xml[f"xl/worksheets/sheet{i}.xml"] = (
            f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{S}"><sheetData>{"".join(out_rows)}</sheetData>'
            f'</worksheet>')

    names = list(sheets)
    sheets_decl = "".join(
        f'<sheet name="{_esc(n)}" sheetId="{i}" r:id="rId{i}"/>'
        for i, n in enumerate(names, start=1))
    workbook = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<workbook xmlns="{S}" xmlns:r="{R}">'
                f'<sheets>{sheets_decl}</sheets></workbook>')
    wb_rels = "".join(
        f'<Relationship Id="rId{i}" Type="{R}/worksheet" '
        f'Target="worksheets/sheet{i}.xml"/>'
        for i in range(1, len(names) + 1))
    wb_rels_xml = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                   f'<Relationships xmlns="{PR}">{wb_rels}'
                   f'<Relationship Id="rIdStyles" Type="{R}/styles" '
                   f'Target="styles.xml"/></Relationships>')
    # Минимальные стили: жирная шапка. Без styles.xml Excel ругается на
    # ссылку s="1" в ячейках заголовка.
    styles = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              f'<styleSheet xmlns="{S}">'
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
              '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
              '<borders count="1"><border/></borders>'
              '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
              '<cellXfs count="2"><xf xfId="0"/>'
              '<xf fontId="1" applyFont="1" xfId="0"/></cellXfs>'
              '</styleSheet>')
    overrides = "".join(
        f'<Override PartName="/{name}" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for name in sheet_xml)
    content_types = (
        f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Types xmlns="{CT}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-'
        'package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        f'{overrides}</Types>')
    rels = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<Relationships xmlns="{PR}">'
            f'<Relationship Id="rId1" Type="{R}/officeDocument" '
            'Target="xl/workbook.xml"/></Relationships>')

    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/_rels/workbook.xml.rels", wb_rels_xml)
        z.writestr("xl/styles.xml", styles)
        for name, xml in sheet_xml.items():
            z.writestr(name, xml)
    return p


def timestamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")
