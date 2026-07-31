"""Тесты интеллектуального парсера PDF через Vision LLM (vision.parse_pdf_vlm, vision.classify_pdf_pages)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import Workspace
from agent_toolkit.vision.pdf_vlm import build_pdf_vlm_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        ws = Workspace(tmp.path("ws"))
        section("1. Интеллектуальный парсинг PDF через VLM (vision.parse_pdf_vlm, vision.classify_pdf_pages)")
        tools = {t.name: t for t in build_pdf_vlm_tools(ws)}
        check("зарегистрировано 3 инструмента pdf_vlm", len(tools) == 3)

        # 1) Классификация страниц
        res_cls = tools["vision.classify_pdf_pages"].execute(
            path="complex_doc.pdf", pages="all"
        )
        check("classify_pdf_pages определяет типы страниц (invoice, drawing)", "invoice" in res_cls and "drawing" in res_cls)
        check("classify_pdf_pages возвращает таблицу типов", "| Стр. | Тип страницы |" in res_cls)

        # 2) Умное распознавание PDF VLM
        res_parse = tools["vision.parse_pdf_vlm"].execute(
            path="complex_doc.pdf", pages="1,2", doc_type_hint="auto"
        )
        check("parse_pdf_vlm извлекает структурированные поля из счёта (INV-2026-001)", "INV-2026-001" in res_parse)
        check("parse_pdf_vlm проводит самопроверку арифметики сумм", "Арифметическая самопроверка VLM: ✓ УСПЕШНО" in res_parse)
        check("parse_pdf_vlm распознаёт схему/чертёж", "AT-2026-DRW" in res_parse and "Схема подключения сервера" in res_parse)

        # 3) Структурированный парсинг PDF в JSON или Markdown по промпту структуры
        res_json = tools["vision.extract_pdf_structured_vlm"].execute(
            path="complex_doc.pdf",
            pages="1",
            output_format="json",
            structure_prompt='{"invoice_number": "", "total": 0.0}',
        )
        check("extract_pdf_structured_vlm возвращает JSON по заданной структуре", "invoice_number" in res_json and "180000.0" in res_json)
        check("extract_pdf_structured_vlm указывает структуру в отчёте", "custom_schema_applied" in res_json)

        res_md = tools["vision.extract_pdf_structured_vlm"].execute(
            path="complex_doc.pdf",
            pages="1",
            output_format="markdown",
        )
        check("extract_pdf_structured_vlm поддерживает режим Markdown", "VLM PDF PAGE 1" in res_md and "| Товар | Сумма |" in res_md)

    return summary("Тесты парсинга PDF VLM")


def test_pdf_vlm_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
