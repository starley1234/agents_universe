import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.tools.code_interpreter import CodeInterpreter


INTERNAL_SERVER_NAME = "__internal__"
INTERNAL_FETCH_TOOL = "fetch_url"
INTERNAL_PYTHON_TOOL = "run_python"


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
        },
        {
            "server_name": INTERNAL_SERVER_NAME,
            "server_url": "builtin://python",
            "name": INTERNAL_PYTHON_TOOL,
            "title": "Встроенный Python sandbox",
            "description": "Выполняет Python-код в workspace задачи через тот же sandbox/runtime, что и Code Interpreter.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python код для выполнения"},
                },
                "required": ["code"],
            },
            "status": "ok",
            "internal": True,
        },
    ]


def list_mcp_tools_sync(servers: list[dict[str, Any]], include_internal: bool = True) -> list[dict[str, Any]]:
    return asyncio.run(list_mcp_tools(servers, include_internal=include_internal))


def call_mcp_tool_sync(
    server: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    workspace_path: str | None = None,
) -> dict[str, Any]:
    return asyncio.run(call_mcp_tool(server, tool_name, arguments, workspace_path=workspace_path))


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
        try:
            result.extend(await _list_tools_auto(server))
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


async def call_mcp_tool(
    server: dict[str, Any],
    tool_name: str,
    arguments: dict[str, Any],
    workspace_path: str | None = None,
) -> dict[str, Any]:
    if server.get("name") == INTERNAL_SERVER_NAME or str(server.get("url", "")).startswith("builtin://"):
        if tool_name == INTERNAL_FETCH_TOOL:
            return await _call_internal_fetch(arguments)
        if tool_name == INTERNAL_PYTHON_TOOL:
            return await _call_internal_python(arguments, workspace_path=workspace_path)
        raise MCPClientError(f"Неизвестный внутренний инструмент: {tool_name}")

    mcp_server = MCPServer(**server)
    if not mcp_server.enabled:
        raise MCPClientError(f"MCP сервер выключен: {mcp_server.name}")
    return await _call_tool_auto(mcp_server, tool_name, arguments)


def _url_candidates(url: str) -> list[str]:
    candidates = [url]
    if url.rstrip('/').endswith('/sse'):
        base = url.rstrip('/')[:-4]
        candidates.extend([base, f"{base}/mcp"])
    elif not url.rstrip('/').endswith('/mcp'):
        candidates.append(f"{url.rstrip('/')}/mcp")
    return list(dict.fromkeys(candidates))


async def _list_tools_auto(server: MCPServer) -> list[dict[str, Any]]:
    errors: list[str] = []
    transports = [server.transport] if server.transport in {"sse", "streamable_http"} else ["sse", "streamable_http"]
    if server.transport == "sse":
        transports.append("streamable_http")
    for transport in dict.fromkeys(transports):
        for url in _url_candidates(server.url):
            trial = MCPServer(server.name, url, transport, server.enabled)
            try:
                return await (_list_sse_tools(trial) if transport == "sse" else _list_streamable_tools(trial))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{transport} {url}: {exc}")
    raise MCPClientError("; ".join(errors[-4:]))


async def _call_tool_auto(server: MCPServer, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    transports = [server.transport] if server.transport in {"sse", "streamable_http"} else ["sse", "streamable_http"]
    if server.transport == "sse":
        transports.append("streamable_http")
    for transport in dict.fromkeys(transports):
        for url in _url_candidates(server.url):
            trial = MCPServer(server.name, url, transport, server.enabled)
            try:
                return await (_call_sse_tool(trial, tool_name, arguments) if transport == "sse" else _call_streamable_tool(trial, tool_name, arguments))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{transport} {url}: {exc}")
    raise MCPClientError("; ".join(errors[-4:]))


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
            return [_tool_description(server, tool) for tool in response.tools]


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
            return _tool_call_response(server, tool_name, arguments, response)


async def _list_streamable_tools(server: MCPServer) -> list[dict[str, Any]]:
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise MCPClientError("Python пакет `mcp` не поддерживает streamable_http. Пересоберите образ: docker compose up --build") from exc

    async with streamable_http_client(server.url) as (read_stream, write_stream, *_):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            response = await session.list_tools()
            return [_tool_description(server, tool) for tool in response.tools]


async def _call_streamable_tool(server: MCPServer, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError as exc:
        raise MCPClientError("Python пакет `mcp` не поддерживает streamable_http. Пересоберите образ: docker compose up --build") from exc

    async with streamable_http_client(server.url) as (read_stream, write_stream, *_):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            response = await session.call_tool(tool_name, arguments=arguments)
            return _tool_call_response(server, tool_name, arguments, response)


def _tool_description(server: MCPServer, tool: Any) -> dict[str, Any]:
    return {
        "server_name": server.name,
        "server_url": server.url,
        "name": tool.name,
        "title": getattr(tool, "title", None) or tool.name,
        "description": getattr(tool, "description", None) or "",
        "input_schema": _jsonable(getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}),
        "status": "ok",
        "internal": False,
        "transport": server.transport,
    }


def _tool_call_response(server: MCPServer, tool_name: str, arguments: dict[str, Any], response: Any) -> dict[str, Any]:
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


async def _call_internal_python(arguments: dict[str, Any], workspace_path: str | None = None) -> dict[str, Any]:
    code = str(arguments.get("code") or "")
    if not code.strip():
        raise MCPClientError("run_python требует аргумент code")
    workspace = Path(workspace_path or "/tmp/aethermind-mcp-python").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    result = CodeInterpreter(workspace).run_python(code)
    return {
        "server_name": INTERNAL_SERVER_NAME,
        "server_url": "builtin://python",
        "tool_name": INTERNAL_PYTHON_TOOL,
        "arguments": {"code": code},
        "is_error": bool(result.get("exit_code", 1)),
        "content": [
            {
                "type": "json",
                "json": result,
            }
        ],
    }
