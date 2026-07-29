"""Тесты dataforge.connectors.files.FileConnector: CSV/JSON/XML на
реальных временных файлах (не моки уровня Python-объектов).

xlsx тестируется только если установлен openpyxl (для этой сборки он в
requirements.txt как обязательная зависимость, но тест устойчив к его
отсутствию — печатает SKIP_REASON вместо падения).
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataforge.connectors.base import ConnectorCapabilityError, ConnectorError  # noqa: E402
from dataforge.connectors.files import FileConnector                            # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n" + "─" * len(title))


def _write(tmpdir: Path, name: str, content: str) -> Path:
    p = tmpdir / name
    p.write_text(content, encoding="utf-8")
    return p


def main() -> int:
    tmpdir = Path(tempfile.mkdtemp(prefix="forge_files_test_"))

    section("FileConnector: отсутствующий файл -> ConnectorError")
    try:
        FileConnector(str(tmpdir / "nope.csv"))
        check("отсутствующий файл кидает ConnectorError", False)
    except ConnectorError as exc:
        check("отсутствующий файл кидает ConnectorError", True)
        check("сообщение упоминает путь", "nope.csv" in str(exc))

    section("FileConnector: неподдерживаемый формат -> ConnectorError")
    bad = _write(tmpdir, "data.txt", "плоский текст")
    try:
        FileConnector(str(bad))
        check("неизвестное расширение кидает ConnectorError", False)
    except ConnectorError:
        check("неизвестное расширение кидает ConnectorError", True)

    section("FileConnector: CSV")
    csv_path = _write(tmpdir, "customers.csv",
                      "name,age,active\nАлиса,30,true\nБорис,,false\n")
    conn = FileConnector(str(csv_path))
    check("dataset_name == имя файла", conn.dataset_name == "customers.csv")
    schemas = conn.discover()
    check("discover вернул ровно один датасет", len(schemas) == 1)
    fields = {f.name: f.type for f in schemas[0].fields}
    check("age определён как number", fields["age"] == "number")
    check("active определён как boolean", fields["active"] == "boolean")
    check("row_count_hint верный", schemas[0].row_count_hint == 2)
    records = list(conn.read_full("customers.csv"))
    check("read_full вернул 2 записи", len(records) == 2)
    check("значения строк корректны", records[0]["name"] == "Алиса")
    try:
        list(conn.read_full("wrong_dataset"))
        check("read_full с чужим именем датасета -> ConnectorError", False)
    except ConnectorError:
        check("read_full с чужим именем датасета -> ConnectorError", True)
    try:
        conn.read_changes("customers.csv", None)
        check("read_changes -> ConnectorCapabilityError (файлы не поддерживают)", False)
    except ConnectorCapabilityError:
        check("read_changes -> ConnectorCapabilityError (файлы не поддерживают)", True)
    try:
        conn.write_back("customers.csv", [])
        check("write_back -> ConnectorCapabilityError (файлы read-only)", False)
    except ConnectorCapabilityError:
        check("write_back -> ConnectorCapabilityError (файлы read-only)", True)

    section("FileConnector: JSON (список записей)")
    json_path = _write(tmpdir, "orders.json",
                       json.dumps([{"id": 1, "total": 100.5},
                                  {"id": 2, "total": 200.0}]))
    conn_json = FileConnector(str(json_path))
    records_json = list(conn_json.read_full("orders.json"))
    check("JSON-список разобран верно", len(records_json) == 2
         and records_json[1]["total"] == 200.0)

    section("FileConnector: JSON (обёртка {\"records\": [...]})")
    json_wrapped = _write(tmpdir, "wrapped.json",
                          json.dumps({"records": [{"x": 1}, {"x": 2}]}))
    conn_wrapped = FileConnector(str(json_wrapped))
    records_wrapped = list(conn_wrapped.read_full("wrapped.json"))
    check("обёрнутый JSON с ключом records разобран", len(records_wrapped) == 2)

    section("FileConnector: XML")
    xml_path = _write(tmpdir, "items.xml", """<?xml version="1.0"?>
<items>
  <item id="1"><name>Болт</name><qty>10</qty></item>
  <item id="2"><name>Гайка</name><qty>20</qty></item>
</items>""")
    conn_xml = FileConnector(str(xml_path))
    records_xml = list(conn_xml.read_full("items.xml"))
    check("XML разобран, 2 записи", len(records_xml) == 2)
    check("атрибут id попал в запись", records_xml[0]["id"] == "1")
    check("дочерний тег name попал в запись", records_xml[0]["name"] == "Болт")

    section("FileConnector: XLSX (если openpyxl доступен)")
    try:
        import openpyxl  # type: ignore
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["name", "price"])
        ws.append(["Деталь А", 150])
        ws.append(["Деталь Б", 300])
        xlsx_path = tmpdir / "parts.xlsx"
        wb.save(xlsx_path)
        conn_xlsx = FileConnector(str(xlsx_path))
        records_xlsx = list(conn_xlsx.read_full("parts.xlsx"))
        check("XLSX разобран, 2 записи", len(records_xlsx) == 2)
        check("значения верны", records_xlsx[0]["name"] == "Деталь А"
             and records_xlsx[1]["price"] == 300)
    except ImportError:
        print("  SKIP_REASON: openpyxl не установлен — тест XLSX пропущен")

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
