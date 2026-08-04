"""C.O.R.T.E.X. HTTP API, Web UI and MCP SSE gateway."""
from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

try:  # Only the request annotation is imported eagerly; all app objects stay optional.
    from fastapi import Request as FastAPIRequest
except ImportError:  # pragma: no cover - stdlib fallback environment
    FastAPIRequest = Any  # type: ignore[misc,assignment]

from ..bus import SharedBlackboard, build_event_bus
from ..config import Settings, get_settings
from ..observers import ContextIntegrityObserver, HealthObserver
from ..runtime import CortexRuntime, ToolCatalog
from ..signals import ApprovalRequest, Event
from ..workflows import ToolAuditWorkflow, WorkflowEngine, probe_orchestration_backends
from .mcp import CortexMCPServer
from .native_tools import CortexNativeProvider
from .toolkit_client import RemoteMCPProvider, build_toolkit_provider
from .ui import get_ui_html


@dataclass
class CortexServices:
    settings: Settings
    bus: Any
    blackboard: SharedBlackboard
    catalog: ToolCatalog
    runtime: CortexRuntime
    workflows: WorkflowEngine
    audit: ToolAuditWorkflow
    mcp: CortexMCPServer
    health_observer: HealthObserver
    integrity_observer: ContextIntegrityObserver
    approvals: dict[str, ApprovalRequest] = field(default_factory=dict)
    started_at: float = field(default_factory=__import__("time").time)

    async def request_approval(self, *, action: str, reason: str, task_id: str | None = None) -> ApprovalRequest:
        approval = ApprovalRequest(action=action, reason=reason, task_id=task_id)
        self.approvals[approval.request_id] = approval
        await self.bus.publish(Event.create(
            "hitl.approval.requested", approval.to_dict(), source="gateway",
            correlation_id=task_id or approval.request_id,
        ))
        return approval

    async def decide_approval(self, request_id: str, *, approved: bool, decided_by: str = "operator", note: str = "") -> ApprovalRequest:
        approval = self.approvals.get(request_id)
        if not approval:
            raise KeyError(request_id)
        approval.status = "approved" if approved else "rejected"
        approval.decided_by = decided_by
        approval.decision_note = note
        approval.decided_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        await self.bus.publish(Event.create(
            "hitl.approval.decided", approval.to_dict(), source="gateway", correlation_id=approval.task_id or request_id,
        ))
        return approval

    def health(self) -> dict[str, Any]:
        report = self.audit.latest
        return {
            "status": "ok",
            "project": self.settings.project_name,
            "version": "0.1.0",
            "environment": self.settings.environment,
            "tools_count": len(self.catalog.list_tools()),
            "events_count": len(self.bus.history(limit=self.settings.max_event_history)),
            "tasks_count": len(self.workflows.tasks),
            "providers": self.catalog.provider_names(),
            "orchestration_backends": [item.to_dict() for item in probe_orchestration_backends()],
            "toolkit": getattr(getattr(self.catalog, "_providers", {}).get("agent-toolkit"), "provider", None).health() if getattr(getattr(self.catalog, "_providers", {}).get("agent-toolkit"), "provider", None) and hasattr(getattr(self.catalog, "_providers", {}).get("agent-toolkit").provider, "health") else {"status": "unknown"},
            "audit": {
                "report_id": report.report_id,
                "tested": report.tested,
                "total": report.total,
                "coverage_percent": report.coverage_percent,
            } if report else None,
            "metrics": self.health_observer.snapshot(),
        }


def create_services(settings: Settings | None = None, *, provider: Any | None = None) -> CortexServices:
    settings = settings or get_settings()
    bus = build_event_bus(settings)
    blackboard = SharedBlackboard(bus)
    catalog = ToolCatalog(bus)
    native_provider = CortexNativeProvider(settings)
    catalog.mount("cortex-native", native_provider, priority=20, endpoint=native_provider.endpoint)
    toolkit_provider = provider or build_toolkit_provider(settings)
    endpoint = getattr(toolkit_provider, "endpoint", "")
    try:
        catalog.mount("agent-toolkit", toolkit_provider, priority=10, endpoint=endpoint)
    except Exception as exc:
        # Provider with a transient remote failure still appears in health; API
        # remains usable for tasks/blackboard/HITL.
        from .toolkit_client import UnavailableToolkitProvider
        fallback = UnavailableToolkitProvider(str(exc))
        catalog.mount("agent-toolkit", fallback, priority=0)
        toolkit_provider = fallback
    runtime = CortexRuntime(bus, blackboard, catalog)
    workflows = WorkflowEngine(bus, blackboard, catalog)
    audit = ToolAuditWorkflow(
        toolkit_provider,
        workspace=settings.workspace,
        native_diagnostics=settings.audit_native_diagnostics,
        allow_network=settings.audit_allow_network,
        allow_side_effects=settings.audit_allow_side_effects,
    )
    workflows.register("toolkit_audit", audit)

    async def simple_context_workflow(context: Any) -> dict[str, Any]:
        await context.checkpoint("input", context.task.payload)
        return {"accepted": True, "payload": context.task.payload}

    workflows.register("context_checkpoint", simple_context_workflow)
    health_observer = HealthObserver(bus)
    integrity_observer = ContextIntegrityObserver(bus)
    services = CortexServices(
        settings=settings, bus=bus, blackboard=blackboard, catalog=catalog, runtime=runtime,
        workflows=workflows, audit=audit,
        mcp=None,  # type: ignore[arg-type]
        health_observer=health_observer, integrity_observer=integrity_observer,
    )
    services.mcp = CortexMCPServer(services)
    return services


def _json(value: Any) -> str:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    return json.dumps(value, ensure_ascii=False, default=str)


def _sse_frame(event: str, data: Any, *, event_id: str = "") -> str:
    lines = []
    if event_id:
        lines.append(f"id: {event_id}")
    if event:
        lines.append(f"event: {event}")
    payload = data if isinstance(data, str) else _json(data)
    for line in str(payload).splitlines() or [""]:
        lines.append(f"data: {line}")
    return "\n".join(lines) + "\n\n"


def _parse_body(body: Any) -> dict[str, Any]:
    if body is None:
        return {}
    if isinstance(body, dict):
        return body
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8")
    if isinstance(body, str):
        return json.loads(body or "{}")
    return dict(body)


def create_app(services: CortexServices | None = None) -> Any:
    """Create FastAPI app when optional web dependencies are installed.

    In the bare repository image a fully functional stdlib fallback object is
    returned, so `python -m c_o_r_t_e_x serve` remains usable and testable.
    """
    services = services or create_services()
    try:
        from fastapi import Body, FastAPI, Query
        from fastapi.middleware.cors import CORSMiddleware
        from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
    except ImportError:
        return FallbackApp(services)

    app = FastAPI(
        title="C.O.R.T.E.X. Operations Gateway",
        version="0.1.0",
        description="Event-driven agent runtime, shared blackboard and MCP SSE gateway.",
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(services.settings.cors_origins),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/", response_class=HTMLResponse)
    @app.get("/ui", response_class=HTMLResponse)
    async def ui() -> str:
        return get_ui_html()

    @app.get("/health")
    @app.get("/api/health")
    async def health() -> dict[str, Any]:
        return services.health()

    @app.get("/api")
    async def api_info() -> dict[str, Any]:
        return {
            "name": services.settings.project_name,
            "version": "0.1.0",
            "protocols": ["REST", "MCP JSON-RPC 2.0", "MCP Streamable HTTP", "MCP SSE"],
            "endpoints": {"mcp": "/sse", "messages": "/messages", "events": "/api/stream", "ui": "/ui", "agent_run": "/api/agent/run"},
            "config": services.settings.public_dict(),
        }

    @app.get("/api/config")
    async def public_config() -> dict[str, Any]:
        return services.settings.public_dict()

    @app.get("/api/tools")
    async def tools(q: str | None = Query(None), limit: int = Query(500, ge=1, le=1000)) -> dict[str, Any]:
        return {"provider": "agent-toolkit", "total": len(services.catalog.list_tools(query=q or "", limit=limit)), "tools": services.catalog.schemas(query=q or "", limit=limit)}

    @app.get("/api/tools/search")
    async def search_tools(q: str = Query(""), limit: int = Query(8, ge=1, le=50)) -> dict[str, Any]:
        return {"query": q, "tools": services.catalog.schemas(query=q, limit=limit)}

    @app.post("/api/tools/hot-swap")
    async def hot_swap(body: dict[str, Any] = Body(default={})):  # noqa: B008
        endpoint = str(body.get("endpoint") or services.settings.mcp_agent_toolkit)
        remote = RemoteMCPProvider(endpoint, api_key=str(body.get("api_key", services.settings.mcp_agent_toolkit_key)))
        result = await services.catalog.hot_swap("agent-toolkit", remote, reason=str(body.get("reason", "operator request")), endpoint=endpoint)
        return result

    @app.post("/api/tools/{name}/execute")
    async def execute_tool(name: str, body: dict[str, Any] = Body(default={})):  # noqa: B008
        result = await services.catalog.execute(name, body.get("arguments") if "arguments" in body else body)
        return result.to_dict()

    @app.get("/api/events")
    async def events(pattern: str = Query("*"), limit: int = Query(100, ge=1, le=2000)) -> dict[str, Any]:
        return {"events": [event.to_dict() for event in services.bus.history(pattern=pattern, limit=limit)]}

    @app.get("/api/stream")
    async def event_stream(pattern: str = Query("*")):
        subscription = services.bus.subscribe(pattern, max_queue=200)

        async def generator():
            try:
                yield _sse_frame("ready", {"status": "subscribed", "pattern": pattern})
                async for event in subscription:
                    if event.event_type == "bus.subscription.closed":
                        break
                    yield _sse_frame(event.event_type, event.to_dict(), event_id=event.event_id)
            finally:
                await subscription.close()

        return StreamingResponse(generator(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    @app.get("/api/blackboard")
    async def blackboard(prefix: str = Query("")) -> dict[str, Any]:
        return {"prefix": prefix, "values": services.blackboard.snapshot(prefix), "entries": [entry.to_dict() for entry in services.blackboard.entries(prefix)]}

    @app.put("/api/blackboard")
    async def blackboard_write(body: dict[str, Any] = Body(default={})):  # noqa: B008
        if not body.get("key"):
            return JSONResponse({"error": "key is required"}, status_code=400)
        entry = await services.blackboard.write(str(body["key"]), body.get("value"), expected_version=body.get("expected_version"), updated_by=str(body.get("updated_by", "api")))
        return entry.to_dict()

    @app.get("/api/tasks")
    async def tasks(limit: int = Query(100, ge=1, le=1000)) -> dict[str, Any]:
        return {"tasks": [task.to_dict() for task in services.workflows.list(limit=limit)]}

    @app.post("/api/agent/run")
    async def run_agent(body: dict[str, Any] = Body(default={})):  # noqa: B008
        """Запустить workflow-агента из UI/API и вернуть task trace."""
        try:
            task = await services.workflows.submit(
                str(body.get("title", "C.O.R.T.E.X. agent task")),
                workflow=str(body.get("workflow", "toolkit_audit")),
                payload=body.get("payload") or {},
                run=True,
            )
            return {"accepted": True, "task": task.to_dict()}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/api/tasks")
    async def create_task(body: dict[str, Any] = Body(default={})):  # noqa: B008
        try:
            task = await services.workflows.submit(
                str(body.get("title", "C.O.R.T.E.X. task")), workflow=str(body.get("workflow", "toolkit_audit")),
                payload=body.get("payload") or {}, run=bool(body.get("run", False)),
            )
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return task.to_dict()

    @app.post("/api/tasks/{task_id}/run")
    async def run_task(task_id: str):
        try:
            task = services.workflows.get(task_id)
            if not task:
                return JSONResponse({"error": "task_not_found"}, status_code=404)
            asyncio.create_task(services.workflows.run(task_id))
            return {"accepted": True, "task": task.to_dict()}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/api/tasks/{task_id}/approve")
    async def approve_task(task_id: str, body: dict[str, Any] = Body(default={})):  # noqa: B008
        try:
            task = await services.workflows.approve(task_id, decision=str(body.get("decision", "approve")), note=str(body.get("note", "")), decided_by=str(body.get("decided_by", "operator")))
            return task.to_dict()
        except KeyError:
            return JSONResponse({"error": "task_not_found"}, status_code=404)
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/api/tasks/{task_id}")
    async def get_task(task_id: str):
        task = services.workflows.get(task_id)
        return task.to_dict() if task else JSONResponse({"error": "task_not_found"}, status_code=404)

    @app.post("/api/toolkit/audit")
    async def toolkit_audit(body: dict[str, Any] = Body(default={})):  # noqa: B008
        try:
            task = await services.workflows.submit(
                str(body.get("title", "Agent Toolkit practical audit")), workflow="toolkit_audit", payload=body.get("payload") or {}, run=False,
            )
            if body.get("background", False):
                asyncio.create_task(services.workflows.run(task.task_id))
                return {"accepted": True, "task": task.to_dict()}
            task = await services.workflows.run(task.task_id)
            return {"accepted": True, "task": task.to_dict(), "result": (task.result or {}).get("value", task.result)}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=500)

    @app.get("/api/toolkit/audit/latest")
    async def latest_audit() -> dict[str, Any]:
        return services.audit.latest.to_dict() if services.audit.latest else {}

    @app.get("/api/approvals")
    async def approvals() -> dict[str, Any]:
        return {"approvals": [item.to_dict() for item in services.approvals.values()]}

    @app.post("/api/approvals/{request_id}")
    async def decide_approval(request_id: str, body: dict[str, Any] = Body(default={})):  # noqa: B008
        try:
            item = await services.decide_approval(request_id, approved=str(body.get("decision", "approve")).lower() in ("approve", "approved", "true"), decided_by=str(body.get("decided_by", "operator")), note=str(body.get("note", "")))
            return item.to_dict()
        except KeyError:
            return JSONResponse({"error": "approval_not_found"}, status_code=404)

    async def mcp_post(request: FastAPIRequest):
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
        response = await services.mcp.handle_rpc_async(payload)
        if response is None:
            return Response(status_code=202)
        if "text/event-stream" in request.headers.get("accept", ""):
            async def one():
                yield _sse_frame("message", response)
            return StreamingResponse(one(), media_type="text/event-stream")
        return JSONResponse(response)

    @app.post("/sse")
    @app.post("/mcp")
    async def mcp_streamable(request: FastAPIRequest):
        return await mcp_post(request)

    @app.get("/sse")
    @app.get("/mcp")
    async def mcp_sse(request: FastAPIRequest):
        base = str(request.base_url).rstrip("/")
        session = services.mcp.sessions.create(f"{base}/messages?session_id={''}")
        session.endpoint = f"/messages?session_id={session.session_id}"

        async def stream():
            async for frame in session.stream():
                yield _sse_frame(frame["event"], frame["data"])

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"})

    @app.post("/messages")
    async def mcp_messages(request: FastAPIRequest):
        session_id = request.query_params.get("session_id", "")
        if session_id not in services.mcp.sessions.sessions:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "Session not found"}}, status_code=404)
        try:
            payload = await request.json()
        except Exception:
            return JSONResponse({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
        response = await services.mcp.handle_rpc_async(payload)
        if response:
            await services.mcp.sessions.push(session_id, response)
        return JSONResponse(response or {"accepted": True})

    return app


class FallbackApp:
    """WSGI-like testable app + ThreadingHTTPServer adapter без FastAPI."""

    def __init__(self, services: CortexServices) -> None:
        self.services = services

    def _response(self, status: int, value: Any, *, content_type: str = "application/json") -> tuple[int, dict[str, str], bytes]:
        if isinstance(value, bytes):
            body = value
        elif content_type != "application/json":
            body = str(value).encode("utf-8")
        else:
            body = _json(value).encode("utf-8")
        return status, {"Content-Type": f"{content_type}; charset=utf-8", "Access-Control-Allow-Origin": "*"}, body

    def handle(self, method: str, target: str, body: Any = None) -> tuple[int, dict[str, str], bytes]:
        parsed = urlparse(target)
        path, query = parsed.path, parse_qs(parsed.query)
        try:
            if method == "GET" and path in ("/", "/ui"):
                return self._response(200, get_ui_html(), content_type="text/html")
            if method == "GET" and path in ("/health", "/api/health"):
                return self._response(200, self.services.health())
            if method == "GET" and path == "/api":
                return self._response(200, {"name": self.services.settings.project_name, "version": "0.1.0", "protocols": ["REST", "MCP JSON-RPC", "MCP SSE"], "config": self.services.settings.public_dict()})
            if method == "GET" and path == "/api/config":
                return self._response(200, self.services.settings.public_dict())
            if method == "GET" and path == "/api/tools":
                q = query.get("q", [""])[0]
                tools = self.services.catalog.schemas(query=q)
                return self._response(200, {"provider": "agent-toolkit", "total": len(tools), "tools": tools})
            if method == "GET" and path == "/api/tools/search":
                q = query.get("q", [""])[0]
                return self._response(200, {"query": q, "tools": self.services.catalog.schemas(query=q, limit=8)})
            if method == "POST" and path == "/api/tools/hot-swap":
                payload = _parse_body(body)
                endpoint = str(payload.get("endpoint") or self.services.settings.mcp_agent_toolkit)
                remote = RemoteMCPProvider(endpoint, api_key=str(payload.get("api_key", self.services.settings.mcp_agent_toolkit_key)))
                result = asyncio.run(self.services.catalog.hot_swap("agent-toolkit", remote, reason=str(payload.get("reason", "operator request")), endpoint=endpoint))
                return self._response(200, result)
            if method == "POST" and path.startswith("/api/tools/") and path.endswith("/execute"):
                tool_name = path[len("/api/tools/"):-len("/execute")].strip("/")
                payload = _parse_body(body)
                args = payload.get("arguments") if isinstance(payload, dict) and "arguments" in payload else payload
                result = asyncio.run(self.services.catalog.execute(tool_name, args or {}))
                return self._response(200, result.to_dict())
            if method == "GET" and path == "/api/events":
                pattern = query.get("pattern", ["*"])[0]
                return self._response(200, {"events": [event.to_dict() for event in self.services.bus.history(pattern=pattern, limit=int(query.get("limit", [100])[0]))]})
            if method == "GET" and path == "/api/stream":
                pattern = query.get("pattern", ["*"])[0]
                frames = [_sse_frame(event.event_type, event.to_dict(), event_id=event.event_id) for event in self.services.bus.history(pattern=pattern, limit=25)]
                return self._response(200, "".join(frames), content_type="text/event-stream")
            if method == "GET" and path == "/api/blackboard":
                prefix = query.get("prefix", [""])[0]
                return self._response(200, {"prefix": prefix, "values": self.services.blackboard.snapshot(prefix), "entries": [entry.to_dict() for entry in self.services.blackboard.entries(prefix)]})
            if method == "GET" and path == "/api/tasks":
                return self._response(200, {"tasks": [task.to_dict() for task in self.services.workflows.list()]})
            if path.startswith("/api/tasks/"):
                task_id = path[len("/api/tasks/"):].strip("/")
                if method == "GET":
                    task = self.services.workflows.get(task_id)
                    return self._response(200 if task else 404, task.to_dict() if task else {"error": "task_not_found"})
                if method == "POST" and task_id.endswith("/run"):
                    task_id = task_id[:-len("/run")].strip("/")
                    task = asyncio.run(self.services.workflows.run(task_id))
                    return self._response(200, task.to_dict())
                if method == "POST" and task_id.endswith("/approve"):
                    task_id = task_id[:-len("/approve")].strip("/")
                    payload = _parse_body(body)
                    task = asyncio.run(self.services.workflows.approve(task_id, decision=str(payload.get("decision", "approve")), note=str(payload.get("note", ""))))
                    return self._response(200, task.to_dict())
            if method == "GET" and path == "/api/toolkit/audit/latest":
                return self._response(200, self.services.audit.latest.to_dict() if self.services.audit.latest else {})
            if method == "GET" and path == "/sse":
                session = self.services.mcp.sessions.create("/messages?session_id=pending")
                session.endpoint = f"/messages?session_id={session.session_id}"
                return self._response(200, _sse_frame("endpoint", session.endpoint), content_type="text/event-stream")
            if method == "GET" and path == "/mcp":
                session = self.services.mcp.sessions.create("/messages?session_id=pending")
                session.endpoint = f"/messages?session_id={session.session_id}"
                return self._response(200, _sse_frame("endpoint", session.endpoint), content_type="text/event-stream")
            if method == "POST" and path in ("/sse", "/mcp"):
                response = self.services.mcp.handle_rpc(_parse_body(body))
                return self._response(200, response or {})
            if method == "POST" and path == "/messages":
                session_id = query.get("session_id", [""])[0]
                if session_id not in self.services.mcp.sessions.sessions:
                    return self._response(404, {"error": "session_not_found"})
                response = self.services.mcp.handle_rpc(_parse_body(body))
                return self._response(200, response or {"accepted": True})
            if method == "POST" and path == "/api/toolkit/audit":
                payload = _parse_body(body)
                task = asyncio.run(self.services.workflows.submit(str(payload.get("title", "Agent Toolkit practical audit")), workflow="toolkit_audit"))
                task = asyncio.run(self.services.workflows.run(task.task_id))
                return self._response(200, {"accepted": True, "task": task.to_dict(), "result": task.result})
            if method == "POST" and path == "/api/agent/run":
                payload = _parse_body(body)
                task = asyncio.run(self.services.workflows.submit(
                    str(payload.get("title", "C.O.R.T.E.X. agent task")),
                    workflow=str(payload.get("workflow", "toolkit_audit")),
                    payload=payload.get("payload") or {}, run=False,
                ))
                task = asyncio.run(self.services.workflows.run(task.task_id))
                return self._response(200, {"accepted": True, "task": task.to_dict()})
            if method == "POST" and path == "/api/tasks":
                payload = _parse_body(body)
                task = asyncio.run(self.services.workflows.submit(str(payload.get("title", "C.O.R.T.E.X. task")), workflow=str(payload.get("workflow", "toolkit_audit")), payload=payload.get("payload") or {}, run=False))
                return self._response(200, task.to_dict())
            if method == "PUT" and path == "/api/blackboard":
                payload = _parse_body(body)
                entry = asyncio.run(self.services.blackboard.write(str(payload["key"]), payload.get("value"), expected_version=payload.get("expected_version"), updated_by="api"))
                return self._response(200, entry.to_dict())
            return self._response(404, {"error": "not_found", "path": path})
        except Exception as exc:
            return self._response(500, {"error": str(exc)})

    def serve(self, host: str, port: int) -> None:
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def _send(self, result: tuple[int, dict[str, str], bytes]) -> None:
                status, headers, data = result
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self) -> None:  # noqa: N802
                self._send(parent.handle("GET", self.path))

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                self._send(parent.handle("POST", self.path, self.rfile.read(length)))

            def do_PUT(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                self._send(parent.handle("PUT", self.path, self.rfile.read(length)))

            def log_message(self, *_: Any) -> None:
                return

        server = ThreadingHTTPServer((host, port), Handler)
        try:
            server.serve_forever()
        finally:
            server.server_close()


def run_server(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    services = create_services(settings)
    app = create_app(services)
    if isinstance(app, FallbackApp):
        print(f"FastAPI не установлен — stdlib server на http://{settings.app_host}:{settings.app_port}")
        app.serve(settings.app_host, settings.app_port)
        return
    try:
        import uvicorn  # type: ignore
    except ImportError:
        print("Для FastAPI-приложения нужен uvicorn: pip install -e '.[api]'")
        return
    print(f"C.O.R.T.E.X. gateway на http://{settings.app_host}:{settings.app_port}")
    uvicorn.run(app, host=settings.app_host, port=settings.app_port)


__all__ = ["CortexServices", "create_services", "create_app", "FallbackApp", "run_server"]
