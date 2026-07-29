"""AI Copilot: инструменты (function calling) над ПУБЛИЧНЫМИ REST API
DataForge (ТЗ §3.6: "работающий через инструменты над публичными API...
не имеет собственного скрытого доступа к данным, действует в рамках
RBAC/RLS пользователя").

Реализация: каждый инструмент — это HTTP-вызов ТОГО ЖЕ REST API, что
доступен человеку через дашборд, с ТЕМ ЖЕ токеном пользователя (если
задан `FORGE_API_TOKEN`). Copilot физически не может обойти
авторизацию — у него нет прямого доступа к `Store`/БД, только к httpx-
клиенту, настроенному так же, как у любого внешнего потребителя API.

Набор инструментов НАМЕРЕННО узкий (не "дай мне произвольный SQL-запрос
к API") — каждый инструмент соответствует одному конкретному
одобренному действию, чтобы Copilot не мог случайно (или по указанию
злонамеренного промпта) вызвать что-то не предусмотренное.
"""
from __future__ import annotations

from typing import Any

import httpx


class ToolError(RuntimeError):
    """Ошибка выполнения инструмента: HTTP-ошибка, неверные параметры и т.п."""


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "get_dashboard_stats",
        "description": "Возвращает агрегированную статистику платформы: "
                       "число источников, датасетов, записей по слоям, "
                       "золотых записей, открытых процессов и т.п.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "list_sources",
        "description": "Список зарегистрированных источников данных.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "list_datasets",
        "description": "Список датасетов (наборов данных), опционально "
                       "отфильтрованных по источнику.",
        "parameters": {"type": "object", "properties": {
            "source_id": {"type": "integer", "description": "ID источника (опционально)"}}},
    },
    {
        "name": "list_quarantine",
        "description": "Список записей в карантине (не прошли проверку "
                       "качества данных) для указанного датасета.",
        "parameters": {"type": "object", "properties": {
            "dataset_id": {"type": "integer"},
            "resolved": {"type": "boolean", "description": "Фильтр по статусу решения"}},
            "required": ["dataset_id"]},
    },
    {
        "name": "start_quarantine_correction",
        "description": "Запускает процесс исправления записи из карантина: "
                       "создаёт задачу ответственному сотруднику.",
        "parameters": {"type": "object", "properties": {
            "quarantine_id": {"type": "integer"},
            "assignee": {"type": "string", "description": "Кому назначить, напр. human:ivanov"}},
            "required": ["quarantine_id"]},
    },
    {
        "name": "list_mdm_candidates",
        "description": "Список кандидатов на дубли (stewardship-очередь MDM), "
                       "опционально по статусу решения (pending/confirmed_match/rejected).",
        "parameters": {"type": "object", "properties": {
            "decision": {"type": "string"}}},
    },
    {
        "name": "trace_lineage",
        "description": "Строит цепочку происхождения данных (lineage) для "
                       "указанного актива, например 'gold:entity:5'.",
        "parameters": {"type": "object", "properties": {
            "asset": {"type": "string"}}, "required": ["asset"]},
    },
    {
        "name": "list_processes",
        "description": "Список запущенных сквозных процессов (Process "
                       "Orchestrator), опционально по статусу.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string"}}},
    },
]


class ApiTools:
    """Исполнитель инструментов: HTTP-вызовы к REST API DataForge через
    httpx.Client, настроенный вызывающим кодом (см.
    `dataforge/copilot/assistant.py`)."""

    def __init__(self, client: httpx.Client) -> None:
        self.client = client

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            resp = self.client.get(path, params=params, timeout=10)
        except httpx.HTTPError as exc:
            raise ToolError(f"Ошибка обращения к API ({path}): {exc}") from exc
        if resp.status_code >= 400:
            raise ToolError(f"API вернул {resp.status_code} для {path}: {resp.text[:300]}")
        return resp.json()

    def _post(self, path: str, json_body: dict[str, Any]) -> Any:
        try:
            resp = self.client.post(path, json=json_body, timeout=10)
        except httpx.HTTPError as exc:
            raise ToolError(f"Ошибка обращения к API ({path}): {exc}") from exc
        if resp.status_code >= 400:
            raise ToolError(f"API вернул {resp.status_code} для {path}: {resp.text[:300]}")
        return resp.json()

    def get_dashboard_stats(self) -> Any:
        return self._get("/v1/dashboard/stats")

    def list_sources(self) -> Any:
        return self._get("/v1/sources")

    def list_datasets(self, source_id: int | None = None) -> Any:
        params = {"source_id": source_id} if source_id is not None else None
        return self._get("/v1/datasets", params)

    def list_quarantine(self, dataset_id: int, resolved: bool | None = None) -> Any:
        params: dict[str, Any] = {}
        if resolved is not None:
            params["resolved"] = resolved
        return self._get(f"/v1/datasets/{dataset_id}/quarantine", params)

    def start_quarantine_correction(self, quarantine_id: int, assignee: str = "") -> Any:
        return self._post("/v1/processes/quarantine-correction",
                          {"quarantine_id": quarantine_id, "assignee": assignee,
                           "actor": "agent:copilot"})

    def list_mdm_candidates(self, decision: str = "") -> Any:
        return self._get("/v1/mdm/candidates", {"decision": decision} if decision else None)

    def trace_lineage(self, asset: str) -> Any:
        return self._get("/v1/lineage/trace", {"asset": asset})

    def list_processes(self, status: str = "") -> Any:
        return self._get("/v1/processes", {"status": status} if status else None)

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        method = getattr(self, name, None)
        if method is None or name.startswith("_"):
            raise ToolError(f"Неизвестный инструмент: {name!r}")
        try:
            return method(**arguments)
        except TypeError as exc:
            raise ToolError(f"Неверные аргументы для {name}: {exc}") from exc
