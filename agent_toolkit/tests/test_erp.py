"""Тесты коннектора 1С / ERP по протоколу OData (erp.fetch_odata, erp.post_odata_document)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.integrations.erp import build_erp_tools
from tests.harness import check, section, summary


def run_tests() -> int:
    section("1. Коннектор 1С / ERP (OData)")
    tools = {t.name: t for t in build_erp_tools()}
    check("зарегистрировано 2 инструмента erp", len(tools) == 2)

    res_cat = tools["erp.fetch_odata"].execute(
        entity="Catalog_Nomenclature", top=5
    )
    check("fetch_odata возвращает сущности справочника 1С", "Cola 1.5L" in res_cat and "Catalog_Nomenclature" in res_cat)

    res_ord = tools["erp.fetch_odata"].execute(
        entity="Document_Order", top=5
    )
    check("fetch_odata читает документы заказа", "ORD-1" in res_ord and "ООО Ритейл" in res_ord)

    res_post = tools["erp.post_odata_document"].execute(
        entity="Document_Order",
        document_json='{"Customer": "Acme", "Amount": 10000.0}',
        endpoint_url="mock://localhost:8080",
    )
    check("post_odata_document создаёт и проводит документ в mock-режиме", "201 Created" in res_post and "10000.0" in res_post)

    return summary("Тесты 1С/ERP OData")


def test_erp_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
