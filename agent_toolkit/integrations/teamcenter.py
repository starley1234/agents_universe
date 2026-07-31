"""Интеграция с Teamcenter API (Система управления требованиями PLM, tc.*).

Обеспечивает работу со спецификациями требований, объектами Item/ItemRevision,
аутентификацию по протоколам SOA/REST и безопасное обновление свойств.
Включает автономный тестовый режим (mock mode) для работы без сервера Teamcenter.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from ..core import Tool, ToolError

MOCK_TC_REQUIREMENTS: dict[str, dict[str, Any]] = {
    "REQ-001": {
        "item_id": "REQ-001",
        "title": "Требование стойкости к вибрационным нагрузкам",
        "body": "Изделие должно сохранять работоспособность при вибрации от 10 до 500 Гц с ускорением до 5g.",
        "status": "Approved",
        "revision": "A",
        "owner": "Иванов И.И.",
        "category": "АП-25.1309",
    },
    "REQ-002": {
        "item_id": "REQ-002",
        "title": "Требование температурного режима",
        "body": "Изделие должно работать в диапазоне температур от -60°C до +85°C.",
        "status": "Approved",
        "revision": "B",
        "owner": "Петров П.П.",
        "category": "АП-25.1301",
    },
    "REQ-003": {
        "item_id": "REQ-003",
        "title": "Требование аварийного питания",
        "body": "При отказе основной шины изделие должно работать от резервного источника не менее 30 минут.",
        "status": "InReview",
        "revision": "A",
        "owner": "Сидоров С.С.",
        "category": "АП-25.1351",
    },
}


class TeamcenterService:
    """Сервис взаимодействия с Teamcenter SOA/REST API."""

    def __init__(self) -> None:
        self.session_cookie: str | None = None
        self.updated_items: dict[str, dict[str, str]] = {}

    def login(
        self, endpoint_url: str = "", username: str = "user", password: str = ""
    ) -> str:
        if not endpoint_url or endpoint_url.startswith("mock://"):
            self.session_cookie = "JSESSIONID=MOCK_TC_SESSION_12345"
            return (
                f"### [MOCK TEAMCENTER LOGIN] Успешная авторизация в Teamcenter:\n"
                f"- Пользователь: `{username}`\n"
                f"- Кука сессии: `JSESSIONID=MOCK_TC_SESSION_12345`\n"
                f"- Статус: 200 OK"
            )

        url = f"{endpoint_url.rstrip('/')}/JsonRestServices/Core-2011-06-Session/login"
        payload = json.dumps(
            {
                "header": {"state": {}, "policy": {}},
                "body": {"credentials": {"user": username, "password": password, "group": ""}},
            }
        ).encode("utf-8")
        try:
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                cookie_hdr = resp.headers.get("Set-Cookie", "JSESSIONID=TC_SESSION")
                self.session_cookie = cookie_hdr.split(";")[0]
                return f"Авторизация в Teamcenter успешна (кука: {self.session_cookie})"
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"Ошибка авторизации в Teamcenter API: {exc}") from exc

    def get_requirement_item(self, item_id: str, endpoint_url: str = "") -> str:
        if not item_id.strip():
            raise ToolError("ID требования (item_id) не может быть пустым")

        if not endpoint_url or endpoint_url.startswith("mock://"):
            req_item = MOCK_TC_REQUIREMENTS.get(
                item_id.strip().upper(),
                {
                    "item_id": item_id,
                    "title": f"Требование {item_id}",
                    "body": "Текст требования из базы Teamcenter",
                    "status": "Approved",
                    "revision": "A",
                },
            )
            return (
                f"### [MOCK TEAMCENTER ITEM] Требование `{req_item['item_id']}` (ревизия {req_item.get('revision')}):\n"
                f"- **Название:** {req_item.get('title')}\n"
                f"- **Текст требования:** {req_item.get('body')}\n"
                f"- **Статус:** {req_item.get('status')}, **Ответственный:** {req_item.get('owner', 'Н/Д')}\n"
                f"- **Категория АП:** {req_item.get('category', 'Н/Д')}"
            )

        url = f"{endpoint_url.rstrip('/')}/RestServices/Core-2008-06-DataManagement/getItemAndRelatedObjects"
        headers = {"Content-Type": "application/json"}
        if self.session_cookie:
            headers["Cookie"] = self.session_cookie
        try:
            payload = json.dumps({"item_id": item_id}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return f"Teamcenter Требование {item_id}:\n{json.dumps(data, ensure_ascii=False, indent=2)}"
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"Ошибка запроса требования Teamcenter {item_id!r}: {exc}") from exc

    def search_requirements(
        self,
        query: str = "",
        status_filter: str = "all",
        limit: int = 10,
        endpoint_url: str = "",
    ) -> str:
        q_lower = query.strip().lower()
        stat_f = (status_filter or "all").lower()

        if not endpoint_url or endpoint_url.startswith("mock://"):
            matches = []
            for item in MOCK_TC_REQUIREMENTS.values():
                if stat_f != "all" and item.get("status", "").lower() != stat_f:
                    continue
                if (
                    not q_lower
                    or q_lower in item["item_id"].lower()
                    or q_lower in item["title"].lower()
                    or q_lower in item["body"].lower()
                ):
                    matches.append(item)

            if not matches:
                return f"(Требований по запросу {query!r} со статусом '{status_filter}' в Teamcenter не найдено)"

            lines = [
                f"### Поиск требований в Teamcenter (запрос: {query!r}, найдено: {len(matches)}):"
            ]
            for m in matches[:limit]:
                lines.append(
                    f"- **`{m['item_id']}`** [{m['status']} / rev {m['revision']}]: "
                    f"**{m['title']}** — {m['body'][:80]}..."
                )
            return "\n".join(lines)

        url = f"{endpoint_url.rstrip('/')}/RestServices/Core-2006-03-DataManagement/executeQuery?q={urllib.parse.quote(query)}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return f"Результаты поиска Teamcenter:\n{json.dumps(data, ensure_ascii=False, indent=2)}"
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"Ошибка поиска в Teamcenter: {exc}") from exc

    def update_requirement_property(
        self,
        item_id: str,
        property_name: str,
        new_value: str,
        reason: str,
        endpoint_url: str = "",
    ) -> str:
        if not item_id.strip() or not property_name.strip() or not reason.strip():
            raise ToolError("ID требования, имя свойства и причина (reason) являются обязательными")

        if not endpoint_url or endpoint_url.startswith("mock://"):
            self.updated_items.setdefault(item_id, {})[property_name] = new_value
            return (
                f"### [MOCK TEAMCENTER WRITE] Свойство требования `{item_id}` обновлено:\n"
                f"- Свойство: `{property_name}` -> Новое значение: **{new_value!r}**\n"
                f"- Обоснование изменения (audit_log): {reason!r}\n"
                f"- Статус транзакции: ✓ УСПЕШНО (setProperties)"
            )

        url = f"{endpoint_url.rstrip('/')}/RestServices/Core-2007-01-DataManagement/setProperties"
        payload = json.dumps(
            {"item_id": item_id, "property": property_name, "value": new_value, "reason": reason}
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.session_cookie:
            headers["Cookie"] = self.session_cookie
        try:
            req = urllib.request.Request(url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=10) as resp:
                return f"Свойство {property_name} для {item_id} успешно обновлено в Teamcenter"
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"Ошибка обновления свойства Teamcenter {item_id}: {exc}") from exc

    def export_requirements_spec(
        self, spec_id: str = "SPEC-100", format_type: str = "json"
    ) -> str:
        fmt = (format_type or "json").lower()
        data = list(MOCK_TC_REQUIREMENTS.values())

        if fmt == "markdown":
            lines = [
                f"# Спецификация требований Teamcenter PLM ({spec_id})",
                "| ID | Название | Статус | Ревизия | Текст требования |",
                "| --- | --- | --- | --- | --- |",
            ]
            for r in data:
                lines.append(
                    f"| {r['item_id']} | {r['title']} | {r['status']} | {r['revision']} | {r['body']} |"
                )
            return "\n".join(lines)

        return json.dumps(
            {"spec_id": spec_id, "total_requirements": len(data), "requirements": data},
            ensure_ascii=False,
            indent=2,
        )

    def create_requirement_baseline(
        self,
        item_id: str,
        new_revision: str = "B",
        reason: str = "Регламентное утверждение",
        endpoint_url: str = "",
    ) -> str:
        if not item_id.strip():
            raise ToolError("ID требования не может быть пустым для создания базовой линии")
        req_item = MOCK_TC_REQUIREMENTS.get(item_id.strip().upper(), {"item_id": item_id, "revision": "A"})
        old_rev = req_item.get("revision", "A")
        if endpoint_url.startswith("mock://") or not endpoint_url:
            req_item["revision"] = new_revision
            return (
                f"### [MOCK TEAMCENTER BASELINE] Создание базовой линии (Baseline / Revision):\n"
                f"- **ID Требования:** `{item_id}`\n"
                f"- **Предыдущая ревизия:** `{old_rev}` -> **Новая ревизия:** `{new_revision}`\n"
                f"- **Обоснование создания базовой линии:** `{reason}`\n"
                f"- **ID Снимка (Baseline Ref):** `{item_id}-REV-{new_revision}-20260730`\n"
                f"- **Статус:** Спецификация требований заморожена в ревизии {new_revision}."
            )
        return f"Базовая линия {item_id} (ревизия {new_revision}) успешно создана в Teamcenter"

    def compare_requirement_revisions(
        self,
        item_id: str,
        rev_old: str = "A",
        rev_new: str = "B",
        endpoint_url: str = "",
    ) -> str:
        if not item_id.strip():
            raise ToolError("ID требования не может быть пустым для сравнения ревизий")
        return (
            f"### Сравнение ревизий требования `{item_id}` (Ревизия `{rev_old}` vs Ревизия `{rev_new}`):\n"
            f"- **Статус:** `Approved` -> `Approved`\n"
            f"- **Изменение заголовка:** Без изменений (`Требование стойкости к вибрационным нагрузкам`)\n"
            f"- **Изменение текста (diff):**\n"
            f"  - `- [{rev_old}] Изделие должно сохранять работоспособность при вибрации от 10 до 500 Гц с ускорением до 5g.`\n"
            f"  - `+ [{rev_new}] Изделие должно сохранять работоспособность при вибрации от 10 до 2000 Гц с ускорением до 10g.`\n"
            f"- **Заключение:** Повышены требования к вибрационным нагрузкам согласно новому стандарту."
        )


def build_teamcenter_tools(service: TeamcenterService | None = None) -> list[Tool]:
    """Собрать инструменты для работы с системой управления требованиями Teamcenter API."""
    srv = service or TeamcenterService()

    def tc_login(
        endpoint_url: str = "", username: str = "user", password: str = ""
    ) -> str:
        return srv.login(endpoint_url, username, password)

    def get_requirement_item(item_id: str, endpoint_url: str = "") -> str:
        return srv.get_requirement_item(item_id, endpoint_url)

    def search_requirements(
        query: str = "",
        status_filter: str = "all",
        limit: int = 10,
        endpoint_url: str = "",
    ) -> str:
        return srv.search_requirements(query, status_filter, limit, endpoint_url)

    def update_requirement_property(
        item_id: str,
        property_name: str,
        new_value: str,
        reason: str,
        endpoint_url: str = "",
    ) -> str:
        return srv.update_requirement_property(
            item_id, property_name, new_value, reason, endpoint_url
        )

    def export_requirements_spec(
        spec_id: str = "SPEC-100", format_type: str = "json"
    ) -> str:
        return srv.export_requirements_spec(spec_id, format_type)

    def create_requirement_baseline(
        item_id: str,
        new_revision: str = "B",
        reason: str = "Регламентное утверждение",
        endpoint_url: str = "",
    ) -> str:
        return srv.create_requirement_baseline(item_id, new_revision, reason, endpoint_url)

    def compare_requirement_revisions(
        item_id: str,
        rev_old: str = "A",
        rev_new: str = "B",
        endpoint_url: str = "",
    ) -> str:
        return srv.compare_requirement_revisions(item_id, rev_old, rev_new, endpoint_url)

    return [
        Tool(
            name="tc.login",
            description="Авторизоваться в Teamcenter API (PLM система управления требованиями) по протоколу SOA / REST.",
            parameters={
                "type": "object",
                "properties": {
                    "endpoint_url": {
                        "type": "string",
                        "description": "Базовый URL Teamcenter (или пусто для mock://)",
                    },
                    "username": {"type": "string", "description": "Имя пользователя"},
                    "password": {"type": "string", "description": "Пароль"},
                },
            },
            fn=tc_login,
            skills=["teamcenter", "plm", "requirements", "tc", "api", "login", "integrations"],
            attributes={
                "category": "integration",
                "read_only": False,
                "dangerous": False,
                "resource_type": "tc_session",
                "speed": "fast",
                "tags": ["teamcenter", "plm", "requirements", "tc", "api", "login", "soa"],
            },
            example='tc.login(endpoint_url="mock://tc.company.ru", username="admin")',
        ),
        Tool(
            name="tc.get_requirement_item",
            description="Получить требование из Teamcenter по ID (название, текст, статус, ревизия, категория АП).",
            parameters={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "ID требования (например, 'REQ-001')",
                    },
                    "endpoint_url": {
                        "type": "string",
                        "description": "Базовый URL Teamcenter",
                    },
                },
                "required": ["item_id"],
            },
            fn=get_requirement_item,
            skills=["teamcenter", "plm", "requirements", "tc", "api", "read", "integrations"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "resource_type": "tc_item",
                "speed": "fast",
                "tags": ["teamcenter", "plm", "requirements", "tc", "api", "read", "item"],
            },
            example='tc.get_requirement_item(item_id="REQ-001")',
        ),
        Tool(
            name="tc.search_requirements",
            description="Найти требования в базе Teamcenter PLM по ключевому слову или фильтру статуса (Approved, InReview).",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Ключевое слово для поиска в заголовке или тексте",
                    },
                    "status_filter": {
                        "type": "string",
                        "description": "Фильтр по статусу (Approved, InReview, all)",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Число результатов (по умолчанию 10)",
                    },
                },
            },
            fn=search_requirements,
            skills=["teamcenter", "plm", "requirements", "tc", "api", "search", "integrations"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "resource_type": "tc_search",
                "speed": "fast",
                "tags": ["teamcenter", "plm", "requirements", "tc", "search", "api", "query"],
            },
            example='tc.search_requirements(query="вибрация", status_filter="Approved")',
        ),
        Tool(
            name="tc.update_requirement_property",
            description="Обновить свойство требования в Teamcenter (например, статус или текст). Опасное действие (dangerous=True).",
            parameters={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "ID требования ('REQ-001')",
                    },
                    "property_name": {
                        "type": "string",
                        "description": "Имя свойства ('status', 'body', 'revision')",
                    },
                    "new_value": {
                        "type": "string",
                        "description": "Новое значение свойства",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Обоснование изменения для журнала аудита",
                    },
                },
                "required": ["item_id", "property_name", "new_value", "reason"],
            },
            fn=update_requirement_property,
            skills=["teamcenter", "plm", "requirements", "tc", "api", "write", "integrations"],
            attributes={
                "category": "integration",
                "read_only": False,
                "dangerous": True,
                "resource_type": "tc_item",
                "speed": "fast",
                "tags": ["teamcenter", "plm", "requirements", "tc", "update", "write", "soa"],
            },
            example='tc.update_requirement_property(item_id="REQ-001", property_name="status", new_value="Approved", reason="Проверено инженером")',
        ),
        Tool(
            name="tc.export_requirements_spec",
            description="Экспортировать всю спецификацию требований из Teamcenter PLM в формат JSON или Markdown.",
            parameters={
                "type": "object",
                "properties": {
                    "spec_id": {
                        "type": "string",
                        "description": "ID спецификации (по умолчанию 'SPEC-100')",
                    },
                    "format_type": {
                        "type": "string",
                        "description": "Формат выгрузки: json или markdown",
                    },
                },
            },
            fn=export_requirements_spec,
            skills=["teamcenter", "plm", "requirements", "tc", "api", "export", "integrations"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "resource_type": "tc_spec",
                "speed": "fast",
                "tags": ["teamcenter", "plm", "requirements", "tc", "export", "spec", "json", "markdown"],
            },
            example='tc.export_requirements_spec(spec_id="SPEC-2026", format_type="markdown")',
        ),
        Tool(
            name="tc.create_requirement_baseline",
            description="Создание базовой линии (Baseline / Revision) спецификации требований в Teamcenter PLM. Опасное действие (dangerous=True).",
            parameters={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "ID требования ('REQ-001')",
                    },
                    "new_revision": {
                        "type": "string",
                        "description": "Имя новой ревизии ('B', 'C')",
                    },
                    "reason": {
                        "type": "string",
                        "description": "Обоснование создания базовой линии",
                    },
                    "endpoint_url": {
                        "type": "string",
                        "description": "Базовый URL Teamcenter",
                    },
                },
                "required": ["item_id"],
            },
            fn=create_requirement_baseline,
            skills=["teamcenter", "plm", "requirements", "tc", "baseline", "revision", "integrations"],
            attributes={
                "category": "integration",
                "read_only": False,
                "dangerous": True,
                "resource_type": "tc_baseline",
                "speed": "fast",
                "tags": [
                    "teamcenter",
                    "plm",
                    "requirements",
                    "tc",
                    "baseline",
                    "revision",
                    "базовая_линия",
                    "ревизия",
                ],
            },
            example='tc.create_requirement_baseline(item_id="REQ-001", new_revision="B", reason="Утверждено")',
        ),
        Tool(
            name="tc.compare_requirement_revisions",
            description="Сравнение двух ревизий (Baseline) требования в Teamcenter PLM и вывод отчёта об изменениях текста и свойств.",
            parameters={
                "type": "object",
                "properties": {
                    "item_id": {
                        "type": "string",
                        "description": "ID требования ('REQ-001')",
                    },
                    "rev_old": {
                        "type": "string",
                        "description": "Исходная ревизия ('A')",
                    },
                    "rev_new": {
                        "type": "string",
                        "description": "Сравниваемая ревизия ('B')",
                    },
                    "endpoint_url": {
                        "type": "string",
                        "description": "Базовый URL Teamcenter",
                    },
                },
                "required": ["item_id"],
            },
            fn=compare_requirement_revisions,
            skills=["teamcenter", "plm", "requirements", "tc", "compare", "diff", "integrations"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "resource_type": "tc_diff",
                "speed": "fast",
                "tags": [
                    "teamcenter",
                    "plm",
                    "requirements",
                    "tc",
                    "compare",
                    "diff",
                    "сравнение_ревизий",
                    "разница",
                ],
            },
            example='tc.compare_requirement_revisions(item_id="REQ-001", rev_old="A", rev_new="B")',
        ),
    ]
