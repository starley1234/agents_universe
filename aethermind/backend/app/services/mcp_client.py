import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx

from app.tools.code_interpreter import CodeInterpreter


INTERNAL_SERVER_NAME = "__internal__"
INTERNAL_FETCH_TOOL = "fetch_url"
INTERNAL_FETCH_MANY_TOOL = "fetch_many_urls"
INTERNAL_PYTHON_TOOL = "run_python"
INTERNAL_WRITE_FILE_TOOL = "write_file"
INTERNAL_READ_FILE_TOOL = "read_file"
INTERNAL_LIST_DIR_TOOL = "list_dir"


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
            "server_url": "builtin://fetch",
            "name": INTERNAL_FETCH_MANY_TOOL,
            "title": "Встроенный multi-fetch с цитированием",
            "description": "Скачивает несколько HTTP/HTTPS страниц и возвращает markdown-блок с источниками [1], [2] и краткими текстовыми excerpts.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "urls": {"type": "array", "items": {"type": "string"}, "description": "Список HTTP/HTTPS URL"},
                    "max_chars_per_url": {"type": "integer", "default": 6000},
                },
                "required": ["urls"],
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
        {
            "server_name": INTERNAL_SERVER_NAME,
            "server_url": "builtin://filesystem",
            "name": INTERNAL_WRITE_FILE_TOOL,
            "title": "Записать файл в workspace",
            "description": "Создает или перезаписывает файл в workspace задачи. Используй для кода, отчетов, CSV/JSON и других deliverables.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]},
            "status": "ok",
            "internal": True,
        },
        {
            "server_name": INTERNAL_SERVER_NAME,
            "server_url": "builtin://filesystem",
            "name": INTERNAL_READ_FILE_TOOL,
            "title": "Прочитать файл из workspace",
            "description": "Читает текстовый файл из workspace задачи.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
            "status": "ok",
            "internal": True,
        },
        {
            "server_name": INTERNAL_SERVER_NAME,
            "server_url": "builtin://filesystem",
            "name": INTERNAL_LIST_DIR_TOOL,
            "title": "Список файлов workspace",
            "description": "Показывает файлы и директории внутри workspace задачи.",
            "input_schema": {"type": "object", "properties": {"path": {"type": "string", "default": "."}}, "required": []},
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
                    "error": _format_exception(exc),
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
        if tool_name == INTERNAL_FETCH_MANY_TOOL:
            return await _call_internal_fetch_many(arguments)
        if tool_name == INTERNAL_PYTHON_TOOL:
            return await _call_internal_python(arguments, workspace_path=workspace_path)
        if tool_name in {INTERNAL_WRITE_FILE_TOOL, INTERNAL_READ_FILE_TOOL, INTERNAL_LIST_DIR_TOOL}:
            return await _call_internal_filesystem(tool_name, arguments, workspace_path=workspace_path)
        raise MCPClientError(f"Неизвестный внутренний инструмент: {tool_name}")

    mcp_server = MCPServer(**server)
    if not mcp_server.enabled:
        raise MCPClientError(f"MCP сервер выключен: {mcp_server.name}")
    return await _call_tool_auto(mcp_server, tool_name, arguments)


def _replace_host(url: str, host: str) -> str:
    parsed = urlparse(url)
    if not parsed.hostname:
        return url
    netloc = host
    if parsed.port:
        netloc = f"{host}:{parsed.port}"
    return urlunparse((parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment))


def _host_rewrite_candidates(url: str) -> list[str]:
    parsed = urlparse(url)
    if parsed.hostname in {"localhost", "127.0.0.1", "0.0.0.0"}:
        # Внутри Docker `localhost` указывает на api/worker контейнер, а не на хост,
        # где обычно запущены LM Studio / MCP toolkit. Поэтому пробуем docker host alias.
        return [_replace_host(url, "host.docker.internal")]
    return []


def _url_candidates(url: str) -> list[str]:
    seeds = [url, *_host_rewrite_candidates(url)]
    candidates: list[str] = []
    for seed in seeds:
        clean = seed.rstrip("/")
        candidates.append(seed)
        if clean.endswith("/sse"):
            base = clean[:-4]
            candidates.extend([base, f"{base}/mcp"])
        elif "/sse/" in clean:
            # Для endpoint'ов вида /sse/group/files LM Studio ходит ровно туда.
            # Дополнительно пробуем соответствующий streamable endpoint /mcp/group/files.
            candidates.append(clean.replace("/sse/", "/mcp/"))
        elif not clean.endswith("/mcp"):
            candidates.append(f"{clean}/mcp")
    return list(dict.fromkeys(candidates))


def _format_exception(exc: BaseException) -> str:
    if isinstance(exc, BaseExceptionGroup):
        inner = "; ".join(_format_exception(item) for item in exc.exceptions[:3])
        return f"{exc.__class__.__name__}: {exc.message}: {inner}"
    message = str(exc) or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message}"


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
                errors.append(f"{transport} {url}: {_format_exception(exc)}")
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
                errors.append(f"{transport} {url}: {_format_exception(exc)}")
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


async def _call_internal_fetch_many(arguments: dict[str, Any]) -> dict[str, Any]:
    urls = arguments.get("urls") or []
    if not isinstance(urls, list) or not urls:
        raise MCPClientError("fetch_many_urls требует непустой массив urls")
    max_chars = int(arguments.get("max_chars_per_url") or 6000)
    sources = []
    markdown_parts = ["# Источники\n"]
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        for index, raw_url in enumerate(urls[:10], start=1):
            url = str(raw_url)
            if not re.match(r"^https?://", url):
                sources.append({"index": index, "url": url, "is_error": True, "error": "URL должен начинаться с http:// или https://"})
                continue
            try:
                response = await client.get(url)
                text = response.text[:max(1000, min(max_chars, 50000))]
                item = {
                    "index": index,
                    "url": str(response.url),
                    "status_code": response.status_code,
                    "is_error": response.is_error,
                    "excerpt": text,
                }
                sources.append(item)
                markdown_parts.append(f"## [{index}] {response.url}\n\nStatus: {response.status_code}\n\n{text[:3000]}\n")
            except Exception as exc:  # noqa: BLE001
                sources.append({"index": index, "url": url, "is_error": True, "error": str(exc)})
                markdown_parts.append(f"## [{index}] {url}\n\nОшибка: {exc}\n")
    return {
        "server_name": INTERNAL_SERVER_NAME,
        "server_url": "builtin://fetch",
        "tool_name": INTERNAL_FETCH_MANY_TOOL,
        "arguments": arguments,
        "is_error": any(item.get("is_error") for item in sources),
        "content": [
            {"type": "json", "json": {"sources": sources}},
            {"type": "text", "text": "\n".join(markdown_parts)},
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


def _safe_workspace_path(workspace_path: str | None, relative: str) -> Path:
    workspace = Path(workspace_path or "/tmp/aethermind-mcp-workspace").resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    target = (workspace / str(relative or ".")).resolve()
    if not str(target).startswith(str(workspace)):
        raise MCPClientError("Путь выходит за пределы workspace задачи")
    return target


async def _call_internal_filesystem(tool_name: str, arguments: dict[str, Any], workspace_path: str | None = None) -> dict[str, Any]:
    rel = str(arguments.get("path") or ".")
    target = _safe_workspace_path(workspace_path, rel)
    if tool_name == INTERNAL_WRITE_FILE_TOOL:
        content = str(arguments.get("content") or "")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        payload = {"path": rel, "bytes": len(content.encode("utf-8")), "created": True}
    elif tool_name == INTERNAL_READ_FILE_TOOL:
        if not target.exists() or not target.is_file():
            raise MCPClientError(f"Файл не найден: {rel}")
        payload = {"path": rel, "content": target.read_text(encoding="utf-8", errors="replace")}
    elif tool_name == INTERNAL_LIST_DIR_TOOL:
        if not target.exists() or not target.is_dir():
            raise MCPClientError(f"Директория не найдена: {rel}")
        payload = {"path": rel, "entries": [p.name + ("/" if p.is_dir() else "") for p in sorted(target.iterdir())]}
    else:
        raise MCPClientError(f"Неизвестный filesystem инструмент: {tool_name}")
    return {
        "server_name": INTERNAL_SERVER_NAME,
        "server_url": "builtin://filesystem",
        "tool_name": tool_name,
        "arguments": arguments,
        "is_error": False,
        "content": [{"type": "json", "json": payload}],
    }
