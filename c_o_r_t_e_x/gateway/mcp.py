"""MCP JSON-RPC 2.0 + SSE session transport для C.O.R.T.E.X.

Поддерживаются оба режима, которые нужны LM Studio/Claude Desktop:
* Streamable HTTP: POST /sse с JSON response;
* legacy SSE: GET /sse -> endpoint event -> POST /messages?session_id=...
"""
from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..signals import Event
from .toolkit_client import RemoteMCPProvider


class MCPError(RuntimeError):
    pass


@dataclass
class MCPSession:
    session_id: str
    queue: asyncio.Queue[dict[str, Any]]
    endpoint: str
    closed: bool = False

    async def stream(self):
        yield {"event": "endpoint", "data": self.endpoint}
        try:
            while not self.closed:
                message = await self.queue.get()
                yield {"event": "message", "data": json.dumps(message, ensure_ascii=False)}
        except asyncio.CancelledError:
            return


class MCPSessionManager:
    def __init__(self) -> None:
        self.sessions: dict[str, MCPSession] = {}

    def create(self, endpoint: str) -> MCPSession:
        session = MCPSession(str(uuid.uuid4()), asyncio.Queue(maxsize=100), endpoint)
        self.sessions[session.session_id] = session
        return session

    async def push(self, session_id: str, response: dict[str, Any]) -> bool:
        session = self.sessions.get(session_id)
        if not session or session.closed:
            return False
        try:
            session.queue.put_nowait(response)
        except asyncio.QueueFull:
            await session.queue.get()
            await session.queue.put(response)
        return True

    def close(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session:
            session.closed = True


MCPToolHandler = Callable[[dict[str, Any]], Any]


class CortexMCPServer:
    """MCP server с router tools, чтобы LLM не получала 170 схем сразу."""

    protocol_version = "2025-03-26"

    def __init__(self, services: Any, *, server_name: str = "cortex-mcp") -> None:
        self.services = services
        self.server_name = server_name
        self.sessions = MCPSessionManager()

    def tool_definitions(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "cortex.search_tools",
                "description": "Найти инструменты agent_toolkit по описанию задачи; сначала ищите, затем вызывайте.",
                "inputSchema": {
                    "type": "object", "properties": {
                        "query": {"type": "string"}, "limit": {"type": "integer", "default": 8},
                    }, "required": ["query"],
                },
            },
            {
                "name": "cortex.fetch",
                "description": "First-party HTTP(S) fetch для небольшого research ресурса.",
                "inputSchema": {
                    "type": "object", "properties": {
                        "url": {"type": "string"}, "max_bytes": {"type": "integer", "default": 65536},
                    }, "required": ["url"],
                },
            },
            {
                "name": "cortex.call_tool",
                "description": "Вызвать найденный инструмент agent_toolkit через C.O.R.T.E.X. runtime.",
                "inputSchema": {
                    "type": "object", "properties": {
                        "name": {"type": "string"}, "arguments": {"type": "object", "default": {}},
                    }, "required": ["name"],
                },
            },
            {
                "name": "cortex.submit_task",
                "description": "Создать долгоживущий workflow; по умолчанию запускается аудит agent_toolkit.",
                "inputSchema": {
                    "type": "object", "properties": {
                        "title": {"type": "string"}, "workflow": {"type": "string", "default": "toolkit_audit"},
                        "payload": {"type": "object", "default": {}}, "run": {"type": "boolean", "default": True},
                    }, "required": ["title"],
                },
            },
            {
                "name": "cortex.start_agent",
                "description": "Запустить workflow-агента C.O.R.T.E.X. и вернуть task_id для отслеживания.",
                "inputSchema": {
                    "type": "object", "properties": {
                        "title": {"type": "string"}, "workflow": {"type": "string", "default": "toolkit_audit"},
                        "payload": {"type": "object", "default": {}},
                    }, "required": ["title"],
                },
            },
            {
                "name": "cortex.get_task",
                "description": "Получить статус и результат workflow по task_id.",
                "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
            },
            {
                "name": "cortex.run_tool_audit",
                "description": "Практически проверить инструменты agent_toolkit и вернуть рекомендации.",
                "inputSchema": {"type": "object", "properties": {"title": {"type": "string", "default": "Agent Toolkit practical audit"}}},
            },
            {
                "name": "cortex.blackboard_read",
                "description": "Прочитать значение из общей доски состояния.",
                "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]},
            },
            {
                "name": "cortex.list_events",
                "description": "Получить последние события шины с correlation_id для трассировки.",
                "inputSchema": {"type": "object", "properties": {"pattern": {"type": "string", "default": "*"}, "limit": {"type": "integer", "default": 50}}},
            },
            {
                "name": "cortex.hot_swap_provider",
                "description": "Переключить agent-toolkit provider на другой MCP endpoint после circuit event или operator approval.",
                "inputSchema": {"type": "object", "properties": {"endpoint": {"type": "string"}, "reason": {"type": "string"}, "api_key": {"type": "string"}}, "required": ["endpoint"]},
            },
            {
                "name": "cortex.request_approval",
                "description": "Запросить human-in-the-loop согласование опасного шага.",
                "inputSchema": {"type": "object", "properties": {"action": {"type": "string"}, "reason": {"type": "string"}, "task_id": {"type": "string"}}, "required": ["action", "reason"]},
            },
        ]

    def _error(self, request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    async def _call(self, name: str, arguments: dict[str, Any]) -> Any:
        services = self.services
        if name == "cortex.search_tools":
            limit = max(1, min(int(arguments.get("limit", 8)), 50))
            return {"query": arguments.get("query", ""), "tools": services.catalog.schemas(query=str(arguments.get("query", "")), limit=limit)}
        if name == "cortex.fetch":
            result = await services.catalog.execute("cortex.fetch", {"url": arguments.get("url", ""), "max_bytes": arguments.get("max_bytes", 65536)})
            return result.to_dict()
        if name == "cortex.call_tool":
            result = await services.catalog.execute(str(arguments.get("name", "")), arguments.get("arguments") or {})
            return result.to_dict()
        if name == "cortex.submit_task":
            task = await services.workflows.submit(
                str(arguments.get("title", "C.O.R.T.E.X. task")),
                workflow=str(arguments.get("workflow", "toolkit_audit")),
                payload=arguments.get("payload") or {},
                run=bool(arguments.get("run", True)),
            )
            return task.to_dict()
        if name == "cortex.start_agent":
            task = await services.workflows.submit(
                str(arguments.get("title", "C.O.R.T.E.X. agent task")),
                workflow=str(arguments.get("workflow", "toolkit_audit")),
                payload=arguments.get("payload") or {},
                run=True,
            )
            return {"accepted": True, "task": task.to_dict()}
        if name == "cortex.get_task":
            task = services.workflows.get(str(arguments.get("task_id", "")))
            return task.to_dict() if task else {"error": "task_not_found"}
        if name == "cortex.run_tool_audit":
            task = await services.workflows.submit(
                str(arguments.get("title", "Agent Toolkit practical audit")),
                workflow="toolkit_audit", payload={}, run=True,
            )
            return {"task_id": task.task_id, "status": task.status.value, "message": "Аудит запущен; следите за task.completed и toolkit.audit.completed."}
        if name == "cortex.blackboard_read":
            key = str(arguments.get("key", ""))
            entry = services.blackboard.read_entry(key)
            return entry.to_dict() if entry else {"key": key, "value": None, "version": 0}
        if name == "cortex.list_events":
            pattern = str(arguments.get("pattern", "*"))
            limit = max(1, min(int(arguments.get("limit", 50)), 200))
            return {"events": [event.to_dict() for event in services.bus.history(pattern=pattern, limit=limit)]}
        if name == "cortex.hot_swap_provider":
            endpoint = str(arguments.get("endpoint", ""))
            if not endpoint:
                raise MCPError("endpoint is required")
            remote = RemoteMCPProvider(endpoint, api_key=str(arguments.get("api_key", "")))
            return await services.catalog.hot_swap(
                "agent-toolkit", remote, reason=str(arguments.get("reason", "MCP operator request")), endpoint=endpoint,
            )
        if name == "cortex.request_approval":
            approval = await services.request_approval(
                action=str(arguments.get("action", "")), reason=str(arguments.get("reason", "")), task_id=arguments.get("task_id"),
            )
            return approval.to_dict()
        raise MCPError(f"Unknown tool {name!r}")

    async def handle_rpc_async(self, request: dict[str, Any]) -> dict[str, Any] | None:
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return self._error(None, -32600, "Invalid Request")
        request_id = request.get("id")
        method = request.get("method", "")
        params = request.get("params") or {}
        if method == "initialize":
            return {
                "jsonrpc": "2.0", "id": request_id,
                "result": {
                    "protocolVersion": self.protocol_version,
                    "capabilities": {"tools": {}, "logging": {}},
                    "serverInfo": {"name": self.server_name, "version": "0.1.0"},
                },
            }
        if method in ("notifications/initialized", "ping"):
            return {"jsonrpc": "2.0", "id": request_id, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.tool_definitions()}}
        if method == "tools/call":
            name = str(params.get("name", ""))
            try:
                result = await self._call(name, params.get("arguments") or {})
                return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "isError": False}}
            except Exception as exc:
                return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": f"Error: {exc}"}], "isError": True}}
        return self._error(request_id, -32601, f"Method {method!r} not found")

    def handle_rpc(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """Sync bridge for tests and stdlib fallback server."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.handle_rpc_async(request))
        raise RuntimeError("handle_rpc() cannot run inside an active event loop; await handle_rpc_async()")


__all__ = ["CortexMCPServer", "MCPSession", "MCPSessionManager", "MCPError"]
