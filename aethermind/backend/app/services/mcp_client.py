import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx


INTERNAL_SERVER_NAME = "__internal__"
INTERNAL_FETCH_TOOL = "fetch_url"


@dataclass
class MCPServer:
    name: str
    url: str
    transport: str = "sse"
    enabled: bool = True


class MCPClientError(RuntimeError):
    pass


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


def _content_to_jsonable(content: Any) -> Any:
    data = _jsonable(content)
    if isinstance(data, list):
        return data
    return data


def _internal_tools() -> list[dict[str, Any]]:
    return [
        {
            "server_name": INTERNAL_SERVER_NAME,
            "server_url": "builtin://fetch",
            "name": INTERNAL_FETCH_TOOL,
            "title": "Встроенный HTTP fetch",
            "description": "Скачивает HTTP/HTTPS страницу и возвращает текст, статус и заголовки. Это внутренний инструмент AetherMind, доступный даже без внешнего MCP сервера.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "HTTP/HTTPS URL"},
                    "max_chars": {"type": "integer", "default": 12000},
                },
                "required": ["url"],
            },
            "status": "ok",
            "internal": True,
        }
    ]


def list_mcp_tools_sync(servers: list[dict[str, Any]], include_internal: bool = True) -> list[dict[str, Any]]:
    return asyncio.run(list_mcp_tools(servers, include_internal=include_internal))


def call_mcp_tool_sync(server: dict[str, Any], tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return asyncio.run(call_mcp_tool(server, tool_name, arguments))


async def list_mcp_tools(servers: list[dict[str, Any]], include_internal: bool = True) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if include_internal:
        result.extend(_internal_tools())

    for raw in servers:
        server = MCPServer(**raw)
        if not server.enabled:
            continue
        if server.name == INTERNAL_SERVER_NAME:
            continue
        if server.transport != "sse":
            result.append(
                {
                    "server_name": server.name,
                    "server_url": server.url,
                    "status": "error",
                    "error": f"Транспорт {server.transport!r} пока не поддержан. Поддерживается sse.",
                }
            )
            continue
        try:
            result.extend(await _list_sse_tools(server))
        except Exception as exc:  # noqa: BLE001 - нужно вернуть ошибку по серверу, не роняя весь UI
            result.append(
                {
                    "server_name": server.name,
                    "server_url": server.url,
                    "status": "error",
                    "error": str(exc),
                }
            )
    return result


async def call_mcp_tool(server: dict[str, Any], tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if server.get("name") == INTERNAL_SERVER_NAME or server.get("url") == "builtin://fetch":
        if tool_name != INTERNAL_FETCH_TOOL:
            raise MCPClientError(f"Неизвестный внутренний инструмент: {tool_name}")
        return await _call_internal_fetch(arguments)

    mcp_server = MCPServer(**server)
    if not mcp_server.enabled:
        raise MCPClientError(f"MCP сервер выключен: {mcp_server.name}")
    if mcp_server.transport != "sse":
        raise MCPClientError(f"Транспорт {mcp_server.transport!r} пока не поддержан. Поддерживается sse.")
    return await _call_sse_tool(mcp_server, tool_name, arguments)


async def _list_sse_tools(server: MCPServer) -> list[dict[str, Any]]:
    try:
        from mcp import ClientSession
        from mcp.client.sse import sse_client
    except ImportError as exc:
        raise MCPClientError("Python пакет `mcp` не установлен в backend container. Пересоберите образ: docker compose up --build") from exc

    async with sse_client(server.url, timeout=15) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            response = await session.list_tools()
            tools = []
            for tool in response.tools:
                tools.append(
                    {
                        "server_name": server.name,
                        "server_url": server.url,
                        "name": tool.name,
                        "title": getattr(tool, "title", None) or tool.name,
                        "description": getattr(tool, "description", None) or "",
                        "input_schema": _jsonable(getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}),
                        "status": "ok",
                        "internal": False,
                    }
                )
            return tools


async def _call_sse_tool(server: MCPServer, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        from mcp import ClientSession
        from mcp.client.sse import sse_client
    except ImportError as exc:
        raise MCPClientError("Python пакет `mcp` не установлен в backend container. Пересоберите образ: docker compose up --build") from exc

    async with sse_client(server.url, timeout=30) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            response = await session.call_tool(tool_name, arguments=arguments)
            return {
                "server_name": server.name,
                "server_url": server.url,
                "tool_name": tool_name,
                "arguments": arguments,
                "is_error": bool(getattr(response, "isError", False) or getattr(response, "is_error", False)),
                "content": _content_to_jsonable(getattr(response, "content", [])),
            }


async def _call_internal_fetch(arguments: dict[str, Any]) -> dict[str, Any]:
    url = str(arguments.get("url") or "")
    if not re.match(r"^https?://", url):
        raise MCPClientError("fetch_url требует аргумент url с http:// или https://")
    max_chars = int(arguments.get("max_chars") or 12000)
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        response = await client.get(url)
    text = response.text[:max(1000, min(max_chars, 100000))]
    return {
        "server_name": INTERNAL_SERVER_NAME,
        "server_url": "builtin://fetch",
        "tool_name": INTERNAL_FETCH_TOOL,
        "arguments": arguments,
        "is_error": response.is_error,
        "content": [
            {
                "type": "text",
                "text": text,
                "metadata": {
                    "url": str(response.url),
                    "status_code": response.status_code,
                    "headers": dict(response.headers),
                },
            }
        ],
    }
