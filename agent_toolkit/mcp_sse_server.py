#!/usr/bin/env python3
"""MCP-сервер agent_toolkit с поддержкой SSE и Streamable HTTP для LM Studio.

Поддерживает два транспортных протокола MCP:
  1. Streamable HTTP (MCP 2025-03-26): POST /sse → JSON-RPC ответ
  2. SSE (MCP 2024-11-05): GET /sse → SSE поток

Запуск:  python mcp_sse_server.py --port 8090

Подключение в LM Studio (mcp.json):
  { "mcpServers": { "agent-toolkit": { "url": "http://localhost:8090/sse" } } }
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).parent.parent))

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route
from sse_starlette.sse import EventSourceResponse
import uvicorn

from agent_toolkit import build_default_registry
from agent_toolkit.core import ToolRegistry
from agent_toolkit.integrations.mcp import MCPServer

# ============================================================
# Группы инструментов
# ============================================================
TOOL_GROUPS: dict[str, dict[str, Any]] = {
    "physics": {
        "label": "Физика и инженерия",
        "skills": {"physics", "engineering_calc", "strength", "antennas", "airflow",
                   "acoustics", "vswr", "yagi", "patch_antenna", "propeller",
                   "fan_noise", "electromagnetics"},
    },
    "cad": {"label": "САПР / CAD", "skills": {"cad", "openscad", "freecad", "stl", "3d"}},
    "web": {"label": "Веб и браузер", "skills": {"web", "scraping", "playwright", "browser", "duckduckgo", "forms", "sitemap", "browser_auto", "web_table", "web_meta"}},
    "files": {"label": "Файлы, офис, шаблоны", "skills": {"files", "filesystem", "office", "docx", "xlsx", "pdf", "templates", "documentation", "reports", "markdown"}},
    "data": {"label": "Данные, SQL, CSV", "skills": {"data", "sql", "database_sql", "csv_table", "table", "excel_formula", "postgres_db", "mysql_db", "er_diagram"}},
    "code": {"label": "Код, Git, DevOps", "skills": {"code", "git", "vcs", "patch", "deploy", "service_deploy"}},
    "memory": {"label": "Память и RAG", "skills": {"memory", "rag_kb", "vector_store", "vector_search"}},
    "crypto": {"label": "Криптография", "skills": {"crypto", "cryptography", "uuid", "hash", "signature"}},
    "integrations": {"label": "Интеграции", "skills": {"smtp", "telegram", "s3", "erp", "teamcenter", "mcp", "http", "tts", "deploy"}},
    "vision": {"label": "Компьютерное зрение", "skills": {"vision", "inventory", "vlm", "ocr", "vlm_pdf"}},
}


def filter_registry_by_group(registry: ToolRegistry, group_name: str) -> ToolRegistry:
    group = TOOL_GROUPS.get(group_name)
    if not group:
        return registry
    filtered = ToolRegistry()
    for tool in registry.list_tools():
        if any(sk in group["skills"] for sk in tool.skills):
            filtered.add(tool)
    return filtered


# ============================================================
# MCP Manager (серверы + сессии)
# ============================================================
class MCPManager:
    def __init__(self, registry: ToolRegistry):
        self.registry = registry
        self.servers: dict[str, MCPServer] = {}
        self.sessions: dict[str, tuple[str, asyncio.Queue]] = {}

        self.servers["main"] = MCPServer(registry=registry, server_name="agent-toolkit-mcp")
        for g in TOOL_GROUPS:
            filtered = filter_registry_by_group(registry, g)
            self.servers[g] = MCPServer(registry=filtered, server_name=f"agent-toolkit-{g}")

    def handle_rpc(self, server_name: str, data: dict) -> dict:
        srv = self.servers.get(server_name)
        if not srv:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "Unknown server"}}
        return srv.handle_rpc(data)

    def create_session(self, server_name: str) -> str:
        sid = str(uuid.uuid4())
        self.sessions[sid] = (server_name, asyncio.Queue())
        return sid

    async def sse_stream(self, session_id: str, messages_url: str):
        if session_id not in self.sessions:
            return
        _, queue = self.sessions[session_id]
        yield {"event": "endpoint", "data": messages_url}
        try:
            while True:
                resp = await queue.get()
                yield {"event": "message", "data": json.dumps(resp, ensure_ascii=False)}
        except asyncio.CancelledError:
            pass
        finally:
            self.sessions.pop(session_id, None)

    async def handle_message(self, session_id: str, data: dict) -> dict:
        if session_id not in self.sessions:
            return {"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "Session not found"}}
        server_name, queue = self.sessions[session_id]
        resp = self.handle_rpc(server_name, data)
        await queue.put(resp)
        return resp


# ============================================================
# HTTP handlers
# ============================================================
def make_streamable_http_handler(manager: MCPManager, server_name: str):
    """POST /sse — Streamable HTTP (MCP 2025-03-26)."""
    async def handler(request: Request) -> Response:
        try:
            body = await request.json()
        except Exception:
            return Response(
                content=json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}),
                status_code=400, media_type="application/json",
            )

        response = manager.handle_rpc(server_name, body)
        response_json = json.dumps(response, ensure_ascii=False)

        accept = request.headers.get("accept", "")
        if "text/event-stream" in accept:
            async def sse_gen():
                yield {"event": "message", "data": response_json}
            return EventSourceResponse(sse_gen())

        return Response(content=response_json, media_type="application/json")
    return handler


def make_sse_handler(manager: MCPManager, server_name: str, base_url: str):
    """GET /sse — SSE fallback (MCP 2024-11-05)."""
    async def handler(request: Request) -> EventSourceResponse:
        session_id = manager.create_session(server_name)
        messages_url = f"{base_url}/messages?session_id={session_id}"
        return EventSourceResponse(manager.sse_stream(session_id, messages_url))
    return handler


def make_messages_handler(manager: MCPManager):
    """POST /messages — SSE session transport."""
    async def handler(request: Request) -> Response:
        session_id = request.query_params.get("session_id", "")
        if not session_id:
            return Response(
                content=json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "Missing session_id"}}),
                status_code=400, media_type="application/json",
            )
        try:
            body = await request.json()
        except Exception:
            return Response(
                content=json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}),
                status_code=400, media_type="application/json",
            )
        response = await manager.handle_message(session_id, body)
        return Response(content=json.dumps(response, ensure_ascii=False), media_type="application/json")
    return handler


# ============================================================
# App factory
# ============================================================
def create_app(manager: MCPManager, port: int = 8090) -> Starlette:
    base_url = f"http://localhost:{port}"

    async def index(request: Request) -> Response:
        return Response(
            content=json.dumps({
                "server": "Agent Toolkit MCP Server",
                "protocols": ["Streamable HTTP (MCP 2025-03-26)", "SSE (MCP 2024-11-05)"],
                "endpoints": ["/sse"] + [f"/sse/{g}" for g in TOOL_GROUPS],
                "tools_count": {
                    "total": len(manager.registry.list_tools()),
                    **{g: len(srv.registry.list_tools()) for g, srv in manager.servers.items() if g != "main"},
                },
            }, ensure_ascii=False, indent=2),
            media_type="application/json",
        )

    async def health(request: Request) -> Response:
        return Response(
            content=json.dumps({"status": "ok", "total_tools": len(manager.registry.list_tools())}),
            media_type="application/json",
        )

    routes = [
        Route("/", endpoint=index, methods=["GET"]),
        Route("/health", endpoint=health, methods=["GET"]),
        Route("/messages", endpoint=make_messages_handler(manager), methods=["POST"]),
    ]

    # Main endpoint: /sse
    routes.append(Route("/sse", endpoint=make_streamable_http_handler(manager, "main"), methods=["POST"]))
    routes.append(Route("/sse", endpoint=make_sse_handler(manager, "main", base_url), methods=["GET"]))

    # Group endpoints: /sse/{group}
    for g in TOOL_GROUPS:
        routes.append(Route(f"/sse/{g}", endpoint=make_streamable_http_handler(manager, g), methods=["POST"]))
        routes.append(Route(f"/sse/{g}", endpoint=make_sse_handler(manager, g, base_url), methods=["GET"]))

    return Starlette(routes=routes)


def main():
    parser = argparse.ArgumentParser(description="MCP сервер для LM Studio")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--host", default="0.0.0.0")
    args = parser.parse_args()

    registry = build_default_registry()
    manager = MCPManager(registry)

    print("=" * 60)
    print("  🤖 Agent Toolkit MCP Server (SSE + Streamable HTTP)")
    print("=" * 60)
    print(f"📦 Всего инструментов: {len(registry.list_tools())}")
    for name, srv in manager.servers.items():
        n = len(srv.registry.list_tools())
        label = TOOL_GROUPS.get(name, {}).get("label", "Все инструменты")
        print(f"   /sse/{name if name != 'main' else '':15} {n:>3} инстр.  {label}")

    print(f"\n🌐 Запуск на http://localhost:{args.port}")
    print(f"\n💡 LM Studio mcp.json:")
    print(f'   {{"mcpServers": {{')
    print(f'     "agent-toolkit": {{"url": "http://localhost:{args.port}/sse"}}')
    print(f'   }}')

    app = create_app(manager, args.port)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
