"""MCP Gateway/Router — предоставляет LLM только 3 инструмента-роутера.

Вместо 163 инструментов LLM видит:
  1. find_tools(query, limit) — поиск нужных инструментов
  2. call_tool(name, arguments_json) — вызов инструмента
  3. list_groups() — список доступных групп

Это позволяет LLM:
  - Первый проход: find_tools("database") → получает 3-5 подходящих
  - Второй проход: call_tool("sql.execute_query", {...}) → выполняет
"""
from __future__ import annotations

import json
from typing import Any

from .core import Tool, ToolRegistry
from .integrations.mcp import MCPServer


def build_gateway_registry(full_registry: ToolRegistry) -> ToolRegistry:
    """Создать реестр с 3 роутер-инструментами для MCP Gateway."""
    gateway = ToolRegistry()

    # 1. find_tools — поиск инструментов
    def find_tools(query: str, limit: int = 5) -> str:
        """Найти инструменты по описанию задачи."""
        hits = full_registry.search(query=query, limit=limit, min_score=0.1)
        if not hits:
            return f"Инструменты не найдены для запроса: {query}"

        results = []
        for tool, score in hits:
            results.append({
                "name": tool.name,
                "description": tool.description,
                "skills": tool.skills,
                "score": round(score, 2),
                "parameters": tool.parameters,
                "example": tool.example,
            })

        return json.dumps({
            "query": query,
            "found": len(results),
            "tools": results,
        }, ensure_ascii=False, indent=2)

    gateway.add(Tool(
        name="find_tools",
        description="Найти подходящие инструменты по описанию задачи. Возвращает список инструментов с их схемами.",
        parameters={
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Описание задачи (например: 'рассчитать прочность балки', 'создать Excel таблицу', 'парсить PDF')",
                },
                "limit": {
                    "type": "integer",
                    "description": "Максимальное количество результатов (по умолчанию 5)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
        fn=find_tools,
        skills=["gateway", "router", "search"],
        attributes={"category": "gateway", "read_only": True},
        example='find_tools(query="рассчитать антенну", limit=3)',
    ))

    # 2. call_tool — вызов инструмента
    def call_tool(name: str, arguments_json: str = "{}") -> str:
        """Вызвать инструмент по имени с JSON-аргументами."""
        try:
            args = json.loads(arguments_json) if arguments_json else {}
        except json.JSONDecodeError as exc:
            return f"Ошибка парсинга JSON аргументов: {exc}"

        try:
            result = full_registry.execute(name, **args)
            result_str = str(result)
            if len(result_str) > 2000:
                result_str = result_str[:2000] + "\n... (обрезано)"
            return result_str
        except KeyError:
            return f"Инструмент {name!r} не найден. Используйте find_tools() для поиска."
        except Exception as exc:
            return f"Ошибка выполнения {name}: {exc}"

    gateway.add(Tool(
        name="call_tool",
        description="Вызвать инструмент по имени. Сначала используйте find_tools() для поиска нужного инструмента.",
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Полное имя инструмента (например: 'physics.calc_strength', 'files.write_file')",
                },
                "arguments_json": {
                    "type": "string",
                    "description": "JSON-объект с аргументами (например: '{\"load_n\": 10000, \"area_mm2\": 50}')",
                    "default": "{}",
                },
            },
            "required": ["name"],
        },
        fn=call_tool,
        skills=["gateway", "router", "execute"],
        attributes={"category": "gateway", "read_only": False},
        example='call_tool(name="crypto.generate_uuid", arguments_json="{}")',
    ))

    # 3. list_groups — список групп
    def list_groups() -> str:
        """Получить список доступных групп инструментов."""
        from .api import _MCP_TOOL_GROUPS
        groups = []
        for g_name, g_info in _MCP_TOOL_GROUPS.items():
            # Подсчитаем инструменты в группе
            count = 0
            for tool in full_registry.list_tools():
                if any(sk in g_info["skills"] for sk in tool.skills):
                    count += 1
            groups.append({
                "name": g_name,
                "label": g_info["label"],
                "tools_count": count,
                "mcp_url": f"/sse/group/{g_name}",
            })

        return json.dumps({
            "groups": groups,
            "total": len(groups),
            "usage": "Подключитесь к /sse/group/{name} для получения инструментов группы",
        }, ensure_ascii=False, indent=2)

    gateway.add(Tool(
        name="list_groups",
        description="Получить список логических групп инструментов с MCP endpoints.",
        parameters={"type": "object", "properties": {}},
        fn=list_groups,
        skills=["gateway", "router", "groups"],
        attributes={"category": "gateway", "read_only": True},
        example="list_groups()",
    ))

    return gateway


def create_gateway_mcp_server(full_registry: ToolRegistry) -> MCPServer:
    """Создать MCP-сервер с Gateway (3 роутер-инструмента)."""
    gateway_reg = build_gateway_registry(full_registry)
    return MCPServer(registry=gateway_reg, server_name="agent-toolkit-gateway")
