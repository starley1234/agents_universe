"""Тесты инструментов работы с PDF-документами (pdf.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import Workspace
from agent_toolkit.local.pdf import build_pdf_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        ws = Workspace(tmp.path("ws"))
        section("1. Инструменты чтения и анализа PDF (pdf.*)")
        tools = {t.name: t for t in build_pdf_tools(ws)}
        check("зарегистрировано 2 инструмента pdf", len(tools) == 2)

        res_pages = tools["pdf.read_pages"].execute(path="sample.pdf", start_page=1, end_page=2)
        check("read_pages читает текст по страницам", "=== Страница 1 ===" in res_pages and "Акт инвентаризации" in res_pages)

        res_tables = tools["pdf.extract_tables"].execute(path="sample.pdf", page=1)
        check("extract_tables находит Markdown таблицу на странице", "| Бренд | Товар |" in res_tables and "Acme" in res_tables)

    return summary("Тесты инструментов PDF")


def test_pdf_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
