"""Тесты слоя импорта: парсеры Word/Excel и распознавание требований.

Работают БЕЗ базы — это чистая логика разбора, и она должна проверяться
на машине, где PostgreSQL ещё не настроен. Документы создаются нашим же
writer'ом (roundtrip): если сломается либо парсер, либо writer, тест это
покажет.
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import (check, check_raises, sample_docx, sample_xlsx,  # noqa: E402
                     section, summary)
from saps.ingest.excel import parse_xlsx, read_workbook, sheet_summary  # noqa: E402
from saps.ingest.pipeline import detect_kind, parse_file             # noqa: E402
from saps.ingest.word import (ParseError, Block, find_requirement_id,  # noqa: E402
                              parse_blocks, parse_docx, read_blocks,
                              summarize)


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="saps_ingest_"))

    section("Распознавание идентификатора требования")
    check("скобочная форма [REQ-123]",
          find_requirement_id("Текст [REQ-123] далее") == "REQ-123")
    check("без скобок",
          find_requirement_id("см. REQ-45 в разделе") == "REQ-45")
    check("составной номер",
          find_requirement_id("[REQ-12.3.4] текст") == "REQ-12.3.4")
    check("кириллический префикс",
          find_requirement_id("[ТРБ-77] текст") == "ТРБ-77")
    check("подчёркивание приводится к дефису",
          find_requirement_id("REQ_88 требование") == "REQ-88")
    check("произвольный префикс проекта",
          find_requirement_id("[SYS-0042] текст") == "SYS-0042")
    check("нет идентификатора", find_requirement_id("обычный текст") == "")
    check("год не считается идентификатором",
          find_requirement_id("в 2024 году") == "")

    section("Разбор Word: иерархия, абзацы, таблицы")
    doc = sample_docx(tmp / "tz.docx")
    blocks = read_blocks(doc)
    kinds = [b.kind for b in blocks]
    check("блоки прочитаны", len(blocks) >= 8, str(kinds))
    check("есть заголовки", kinds.count("heading") >= 3)
    check("есть таблица", "table" in kinds)

    reqs = parse_docx(doc)
    by_id = {r.external_id: r for r in reqs}
    check("найдено 5 требований", len(reqs) == 5,
          f"{len(reqs)}: {sorted(by_id)}")
    check("REQ-001 распознан", "REQ-001" in by_id)
    check("REQ-010 из таблицы распознан", "REQ-010" in by_id)
    check("пояснительный абзац не стал требованием",
          not any("основании технического задания" in r.text for r in reqs),
          "абзац без модального глагола и без номера — не требование")

    r1 = by_id["REQ-001"]
    check("идентификатор убран из текста", not r1.text.startswith("[REQ-001]"),
          r1.text[:40])
    check("текст сохранён", "отказобезопасность" in r1.text)
    check("иерархия заголовка первого уровня",
          r1.section_path == "3 Требования к системе управления",
          r1.section_path)
    r2 = by_id["REQ-002"]
    check("иерархия двух уровней",
          r2.section_path == "3 Требования к системе управления > 3.1 Надёжность",
          r2.section_path)

    r10 = by_id["REQ-010"]
    check("таблица: текст требования", "12 кг" in r10.text)
    check("таблица: ответственный", r10.owner == "Иванов")
    check("таблица: узел изделия", r10.node == "АСДБ.04.32")
    check("таблица: происхождение помечено", r10.origin == "table_row")
    check("таблица: раздел взят из заголовка",
          r10.section_path == "4 Требования к конструкции", r10.section_path)

    section("Уверенность парсера")
    check("требование с номером и глаголом — высокая уверенность",
          r1.confidence >= 0.9, str(r1.confidence))
    check("замечания перечислены при неполноте",
          all(isinstance(r.notes, list) for r in reqs))
    weak = parse_blocks([Block("paragraph", text="Система должна работать")])
    check("без номера уверенность ниже", weak and weak[0].confidence < 0.7,
          str(weak[0].confidence if weak else None))
    check("причина низкой уверенности названа",
          weak and any("идентификатор" in n for n in weak[0].notes))

    section("Разбор Word: таблица «ключ-значение»")
    from saps.export.writers import write_docx
    kv = write_docx(tmp / "kv.docx", [
        {"type": "table", "header": [], "rows": [
            ["Идентификатор", "REQ-500"],
            ["Требование", "Температура в отсеке должна быть не выше 60 °С"],
            ["Ответственный", "Кузнецов"],
            ["Статус", "in_review"],
        ]}])
    kv_reqs = parse_docx(kv)
    check("вертикальная таблица распознана", len(kv_reqs) == 1)
    if kv_reqs:
        k = kv_reqs[0]
        check("kv: идентификатор", k.external_id == "REQ-500")
        check("kv: текст", "60 °С" in k.text)
        check("kv: ответственный", k.owner == "Кузнецов")
        check("kv: происхождение", k.origin == "table_kv")

    section("Разбор Excel")
    xls = sample_xlsx(tmp / "reqs.xlsx")
    wb = read_workbook(xls)
    check("лист прочитан", "Требования" in wb)
    check("строк с шапкой", len(wb["Требования"]) == 3)
    x_reqs = parse_xlsx(xls)
    check("два требования из Excel", len(x_reqs) == 2, str(len(x_reqs)))
    if x_reqs:
        check("Excel: идентификатор", x_reqs[0].external_id == "REQ-100")
        check("Excel: MoC попал в атрибуты",
              x_reqs[0].attributes.get("moc") == "MC2",
              str(x_reqs[0].attributes))
        check("Excel: имя листа как раздел",
              x_reqs[0].section_path == "Требования")
    info = sheet_summary(xls)
    check("сводка листа: шапка найдена", info["Требования"]["recognized"])

    section("Excel: пропущенные ячейки не сдвигают колонки")
    from saps.export.writers import write_xlsx
    import zipfile
    gap = tmp / "gap.xlsx"
    write_xlsx(gap, {"Л": {"header": ["Идентификатор", "Требование",
                                      "Ответственный"],
                           "rows": [["REQ-900", "текст", "Иванов"]]}})
    # Вручную удаляем среднюю ячейку из строки данных — Excel так и делает
    # с пустыми значениями, и наивный парсер сдвинул бы «Иванов» в колонку
    # «Требование».
    with zipfile.ZipFile(gap) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    sheet = parts["xl/worksheets/sheet1.xml"].decode()
    sheet = sheet.replace(
        '<c r="B2" t="inlineStr"><is><t xml:space="preserve">текст</t></is></c>', "")
    parts["xl/worksheets/sheet1.xml"] = sheet.encode()
    with zipfile.ZipFile(gap, "w") as z:
        for n, b in parts.items():
            z.writestr(n, b)
    rows = read_workbook(gap)["Л"]
    check("пустая ячейка сохранила позицию",
          len(rows[1]) == 3 and rows[1][1] == "" and rows[1][2] == "Иванов",
          str(rows[1]))

    section("Ошибки формата — понятный отказ")
    bad = tmp / "notdocx.docx"
    bad.write_bytes("это не zip".encode("utf-8"))
    check_raises("не-zip как .docx", ParseError, read_blocks, bad)
    check_raises("несуществующий файл", ParseError, read_blocks, tmp / "нет.docx")
    check_raises("формат .doc отвергается с подсказкой", ParseError,
                 detect_kind, "файл.doc")
    check_raises("формат .xls отвергается", ParseError, detect_kind, "файл.xls")
    check_raises("неизвестное расширение", ParseError, detect_kind, "файл.pdf")
    check("детект .docx", detect_kind("a.docx") == "word")
    check("детект .xlsx", detect_kind("a.xlsx") == "excel")

    empty = write_docx(tmp / "empty.docx", [
        {"type": "paragraph", "text": "Просто описание изделия без требований."}])
    check("документ без требований не падает", parse_docx(empty) == [])

    section("Сводка разбора")
    s = summarize(reqs)
    check("всего", s["total"] == 5)
    check("с идентификатором", s["with_id"] == 5)
    check("источники посчитаны",
          s["origins"] == {"paragraph": 3, "table_row": 2}, str(s["origins"]))

    section("parse_file выбирает парсер по расширению")
    kind, parsed = parse_file(doc)
    check("word", kind == "word" and len(parsed) == 5)
    kind, parsed = parse_file(xls)
    check("excel", kind == "excel" and len(parsed) == 2)

    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    return summary("Слой импорта")


if __name__ == "__main__":
    raise SystemExit(main())
