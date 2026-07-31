"""Интеграция с MCP (Model Context Protocol): MCP-сервер и MCP-клиент.

Позволяет экспортировать любой ToolRegistry как MCP-сервер (tools/list,
tools/call) по протоколу JSON-RPC, а также подключаться к внешним
MCP-серверам и регистрировать их инструменты в локальном реестре.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..core import MCPToolCall, MCPToolResult, Tool, ToolError, ToolRegistry


class MCPServer:
    """MCP-сервер (Model Context Protocol), экспортирующий инструменты ToolRegistry.

    Обрабатывает запросы JSON-RPC 2.0 от MCP-клиентов (например, Claude Desktop,
    агентов или IDE).
    """

    def __init__(self, registry: ToolRegistry, server_name: str = "agent-toolkit-mcp") -> None:
        self.registry = registry
        self.server_name = server_name

    def handle_rpc(self, request_data: dict[str, Any]) -> dict[str, Any]:
        """Обработка одного JSON-RPC запроса по спецификации MCP."""
        req_id = request_data.get("id")
        method = request_data.get("method", "")
        params = request_data.get("params", {})

        if method == "initialize":
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": self.server_name, "version": "0.1.0"},
                },
            }

        if method == "tools/list":
            tools_list = self.registry.to_mcp_tools()
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"tools": tools_list},
            }

        if method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments", {})
            try:
                res = self.registry.execute(name, **args)
                result_obj = MCPToolResult.ok(str(res), call_id=str(req_id or ""))
            except Exception as exc:  # noqa: BLE001
                result_obj = MCPToolResult.err(str(exc), call_id=str(req_id or ""))
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": result_obj.to_dict(),
            }

        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method {method!r} not found"},
        }


class MCPClient:
    """Клиент для подключения к удалённому или локальному MCP-серверу."""

    def __init__(self, endpoint_url: str = "") -> None:
        self.endpoint_url = endpoint_url
        self._mock_tools: dict[str, dict[str, Any]] = {}

    def register_mock_tool(self, name: str, description: str, fn: Any) -> None:
        """Зарегистрировать мок-инструмент для автономных тестов без сетевого сервера."""
        self._mock_tools[name] = {
            "name": name,
            "description": description,
            "fn": fn,
        }

    def list_tools(self) -> list[dict[str, Any]]:
        """Запросить список доступных инструментов у MCP-сервера (tools/list)."""
        if not self.endpoint_url:
            return [
                {
                    "name": name,
                    "description": t["description"],
                    "inputSchema": {"type": "object", "properties": {}},
                }
                for name, t in self._mock_tools.items()
            ]

        req_body = json.dumps(
            {"jsonrpc": "2.0", "id": "list-1", "method": "tools/list", "params": {}}
        ).encode("utf-8")
        try:
            req = urllib.request.Request(
                self.endpoint_url,
                data=req_body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                return raw.get("result", {}).get("tools", [])
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"Ошибка подключения к MCP-серверу {self.endpoint_url}: {exc}") from exc

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Вызвать инструмент на MCP-сервере (tools/call)."""
        args = arguments or {}
        if not self.endpoint_url and name in self._mock_tools:
            fn = self._mock_tools[name]["fn"]
            return str(fn(**args))

        req_body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": "call-1",
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
        ).encode("utf-8")
        try:
            req = urllib.request.Request(
                self.endpoint_url,
                data=req_body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = json.loads(resp.read().decode("utf-8"))
                result = raw.get("result", {})
                if result.get("isError"):
                    raise ToolError(str(result.get("content", [{}])[0].get("text", "")))
                content = result.get("content", [])
                if content and isinstance(content, list):
                    return str(content[0].get("text", ""))
                return ""
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(
                f"Ошибка вызова MCP инструмента {name!r} на {self.endpoint_url}: {exc}"
            ) from exc


def build_mcp_tools(client: MCPClient | None = None) -> list[Tool]:
    """Собрать инструменты для взаимодействия агентов с MCP-протоколом."""
    mcp_cl = client or MCPClient()

    def list_remote_tools() -> str:
        tools = mcp_cl.list_tools()
        if not tools:
            return "(MCP-инструменты отсутствуют)"
        lines = [f"- {t.get('name')}: {t.get('description')}" for t in tools]
        return "Удалённые MCP инструменты:\n" + "\n".join(lines)

    def call_remote_tool(name: str, arguments_json: str = "{}") -> str:
        try:
            args = json.loads(arguments_json) if arguments_json else {}
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON аргументов: {exc}") from exc
        return mcp_cl.call_tool(name, args)

    return [
        Tool(
            name="mcp.list_remote_tools",
            description="Получить список инструментов, доступных на подключённом MCP-сервере.",
            parameters={"type": "object", "properties": {}},
            fn=list_remote_tools,
            skills=["mcp", "protocol", "integrations", "rpc"],
            attributes={
                "category": "integration",
                "read_only": True,
                "dangerous": False,
                "resource_type": "mcp",
                "speed": "fast",
                "tags": ["mcp", "list", "protocol", "rpc"],
            },
            example="mcp.list_remote_tools()",
        ),
        Tool(
            name="mcp.call_remote_tool",
            description="Вызвать удалённый инструмент через Model Context Protocol (tools/call).",
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Имя MCP-инструмента"},
                    "arguments_json": {
                        "type": "string",
                        "description": 'JSON-аргументы вызова (например, \'{"query": "test"}\')',
                    },
                },
                "required": ["name"],
            },
            fn=call_remote_tool,
            skills=["mcp", "protocol", "integrations", "rpc"],
            attributes={
                "category": "integration",
                "read_only": False,
                "dangerous": False,
                "resource_type": "mcp",
                "speed": "medium",
                "tags": ["mcp", "call", "protocol", "rpc"],
            },
            example='mcp.call_remote_tool(name="search", arguments_json=\'{"q": "doc"}\')',
        ),
    ]
