"""Интеграция с корпоративными системами 1С / ERP по протоколу OData (erp.fetch_odata).

Обеспечивает чтение справочников и документов с поддержкой параметров
$filter, $top, $skip и автономного режима тестирования.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..core import Tool, ToolError

MOCK_ERP_ENTITIES: dict[str, list[dict[str, Any]]] = {
    "Catalog_Nomenclature": [
        {"Ref_Key": "001", "Code": "N-101", "Description": "Cola 1.5L", "Price": 120.0},
        {"Ref_Key": "002", "Code": "N-102", "Description": "Orange 1L", "Price": 110.0},
    ],
    "Document_Order": [
        {
            "Ref_Key": "ORD-1",
            "Number": "000001",
            "Date": "2026-07-29T10:00:00",
            "Amount": 12000.0,
            "Counterparty": "ООО Ритейл",
        },
    ],
}


def build_erp_tools() -> list[Tool]:
    """Собрать инструменты для взаимодействия с 1С / ERP (OData)."""

    def fetch_odata(
        entity: str,
        filter_str: str = "",
        top: int = 10,
        endpoint_url: str = "",
    ) -> str:
        if not entity.strip():
            raise ToolError("Имя OData сущности (entity) не может быть пустым")

        if not endpoint_url or endpoint_url.startswith("mock://"):
            data = MOCK_ERP_ENTITIES.get(
                entity,
                [
                    {
                        "Ref_Key": "999",
                        "Description": f"Пример записи {entity}",
                        "Status": "OK",
                    }
                ],
            )
            selected = data[:top]
            lines = [
                f"⚠️ **MOCK-РЕЖИМ** — ERP/1С OData не настроен! Настройте ERP_ODATA_URL в .env",
                f"### Сущность `{entity}` (найдено: {len(selected)}):"
            ]
            for it in selected:
                lines.append(f"- {json.dumps(it, ensure_ascii=False)}")
            return "\n".join(lines)

        params: dict[str, Any] = {"$top": top, "$format": "json"}
        if filter_str:
            params["$filter"] = filter_str

        url = f"{endpoint_url.rstrip('/')}/odata/standard.odata/{entity}?{urllib.parse.urlencode(params)}"
        try:
            req = urllib.request.Request(
                url, headers={"Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw_json = json.loads(resp.read().decode("utf-8"))
                items = raw_json.get("value", [])
                lines = [
                    f"### ERP OData `{entity}` ({url}):",
                    f"Найдено записей: {len(items)}",
                ]
                for it in items[:top]:
                    lines.append(f"- {json.dumps(it, ensure_ascii=False)}")
                return "\n".join(lines)
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"Ошибка вызова OData 1С/ERP {entity!r}: {exc}") from exc

    def post_odata_document(
        entity: str,
        document_json: str = "{}",
        endpoint_url: str = "",
    ) -> str:
        if not entity.strip():
            raise ToolError("Имя OData сущности документа (entity) не может быть пустым")
        try:
            doc_data = json.loads(document_json)
            if not isinstance(doc_data, dict):
                raise ValueError("document_json должен быть JSON-объектом (dict)")
        except Exception as exc:
            raise ToolError(f"Некорректный JSON в document_json: {exc}") from exc

        if not endpoint_url or endpoint_url.startswith("mock://") or endpoint_url.startswith("test://"):
            return (
                f"⚠️ **MOCK-РЕЖИМ** — ERP/1С OData не настроен! Настройте ERP_ODATA_URL в .env\n"
                f"### Создание документа `{entity}`:\n"
                f"- HTTP Статус: 201 Created (mock)\n"
                f"- Ref_Key: `0f83d2e1-45a2-11ec-9a1b-00155d003201`\n"
                f"- Данные: `{json.dumps(doc_data, ensure_ascii=False)}`"
            )

        url = f"{endpoint_url.rstrip('/')}/odata/standard.odata/{entity}"
        payload = json.dumps(doc_data).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
        }
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=12) as resp:
                resp_txt = resp.read().decode("utf-8", errors="replace")[:1500]
                return (
                    f"### ERP OData POST `{entity}` ({url}):\n"
                    f"- **HTTP Статус:** {resp.status} Created\n"
                    f"- **Ответ 1С/ERP:**\n{resp_txt}"
                )
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"Ошибка POST запроса к OData 1С/ERP {entity!r}: {exc}") from exc

    return [
        Tool(
            name="erp.fetch_odata",
            description="Запросить сущности из 1С/ERP по OData API (справочники, документы) с фильтрацией.",
            parameters={
                "type": "object",
                "properties": {
                    "entity": {
                        "type": "string",
                        "description": "Имя OData сущности (например, 'Catalog_Nomenclature')",
                    },
                    "filter_str": {
                        "type": "string",
                        "description": "Условие $filter (например, 'Price gt 100')",
                    },
                    "top": {
                        "type": "integer",
                        "description": "Ограничение числа записей $top (по умолчанию 10)",
                    },
                    "endpoint_url": {
                        "type": "string",
                        "description": "Базовый URL OData сервиса 1С/ERP",
                    },
                },
                "required": ["entity"],
            },
            fn=fetch_odata,
            skills=["erp", "1c", "odata", "enterprise", "integrations", "data"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "resource_type": "erp_odata",
                "speed": "medium",
                "tags": ["erp", "1c", "odata", "enterprise", "catalog", "document"],
            },
            example='erp.fetch_odata(entity="Catalog_Nomenclature", filter_str="Price gt 100")',
        ),
        Tool(
            name="erp.post_odata_document",
            description="Создание и проведение документа (счёт, заказ, накладная) в 1С / ERP через OData API.",
            parameters={
                "type": "object",
                "properties": {
                    "entity": {
                        "type": "string",
                        "description": "Имя OData сущности документа (например, 'Document_CustomerOrder')",
                    },
                    "document_json": {
                        "type": "string",
                        "description": "JSON-объект с реквизитами создаваемого документа",
                    },
                    "endpoint_url": {
                        "type": "string",
                        "description": "Базовый URL OData сервиса 1С/ERP",
                    },
                },
                "required": ["entity"],
            },
            fn=post_odata_document,
            skills=["erp", "1c", "odata", "enterprise", "integrations", "document"],
            attributes={
                "category": "integration",
                "read_only": False,
                "dangerous": True,
                "resource_type": "erp_odata_post",
                "speed": "medium",
                "tags": [
                    "erp",
                    "1c",
                    "odata",
                    "post_document",
                    "enterprise",
                    "1с",
                    "проведение",
                    "документ_1с",
                ],
            },
            example='erp.post_odata_document(entity="Document_Order", document_json=\'{"Amount": 5000}\')',
        ),
    ]
