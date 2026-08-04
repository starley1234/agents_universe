"""Адаптеры для локального `agent_toolkit` и удалённого MCP SSE endpoint."""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from ..signals import ToolDescriptor


class ToolkitUnavailable(RuntimeError):
    pass


def _normalise_tool(value: Any, *, provider: str) -> ToolDescriptor:
    if isinstance(value, ToolDescriptor):
        value.provider = provider
        return value
    if hasattr(value, "to_mcp_tool"):
        data = value.to_mcp_tool()
    elif hasattr(value, "to_schema"):
        schema = value.to_schema()
        data = {
            "name": schema.name,
            "description": schema.description,
            "inputSchema": schema.parameters,
            "metadata": {
                "skills": schema.skills,
                "attributes": schema.attributes,
                "dangerous": schema.dangerous,
            },
        }
    elif isinstance(value, dict):
        data = value
    else:
        data = {"name": str(getattr(value, "name", value)), "description": str(value)}
    return ToolDescriptor.from_mcp(data, provider=provider)


class LocalToolkitProvider:
    """Подключает sibling-проект agent_toolkit без копирования его инструментов."""

    name = "agent-toolkit-local"

    def __init__(self, registry: Any, *, workspace: str | Path) -> None:
        self.registry = registry
        self.workspace = str(workspace)
        self.endpoint = "local://agent_toolkit"

    @classmethod
    def discover(cls, workspace: str | Path) -> "LocalToolkitProvider":
        try:
            import agent_toolkit  # type: ignore
        except ImportError as exc:
            raise ToolkitUnavailable("Локальный пакет agent_toolkit не найден") from exc
        try:
            registry = agent_toolkit.build_default_registry(workspace_root=str(workspace))
        except Exception as exc:
            raise ToolkitUnavailable(f"agent_toolkit не удалось собрать реестр: {exc}") from exc
        return cls(registry, workspace=workspace)

    def list_tools(self) -> list[ToolDescriptor]:
        descriptors = []
        for tool in self.registry.list_tools(include_disabled=True):
            descriptor = _normalise_tool(tool, provider=self.name)
            descriptor.enabled = bool(self.registry.is_enabled(tool.name))
            descriptors.append(descriptor)
        return descriptors

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        return self.registry.execute(name, **arguments)

    def run_native_diagnostics(self) -> dict[str, Any]:
        """Использует проверенный ProductionTester самого agent_toolkit.

        Его fixtures создаются внутри workspace provider-а; вызовы не выходят
        в сеть при mock URL и классифицируют отсутствующие реквизиты отдельно.
        """
        try:
            from agent_toolkit.core import Workspace
            from agent_toolkit.core.diagnostics import ProductionTester
        except ImportError as exc:
            raise ToolkitUnavailable("ProductionTester agent_toolkit недоступен") from exc
        tester = ProductionTester(self.registry, Workspace(self.workspace))
        return tester.test_all()

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "mode": "local", "tools_count": len(self.list_tools()), "endpoint": self.endpoint}


class RemoteMCPProvider:
    """Маленький stdlib MCP client для Streamable HTTP/SSE-compatible POST /sse."""

    def __init__(self, endpoint: str, *, api_key: str = "", timeout: float = 8.0, name: str = "agent-toolkit") -> None:
        self.endpoint = endpoint.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.name = name
        self._request_id = 0
        self._cache: list[ToolDescriptor] | None = None

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self._request_id += 1
        body = json.dumps({
            "jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params or {},
        }, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8")
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            raise ToolkitUnavailable(f"MCP endpoint {self.endpoint} недоступен: {exc}") from exc
        # Streamable HTTP may return one SSE frame even when Accept contains SSE.
        if raw.lstrip().startswith("event:") or "data:" in raw[:80]:
            data_lines = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")]
            raw = data_lines[-1] if data_lines else "{}"
        try:
            result = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ToolkitUnavailable(f"MCP endpoint вернул не JSON: {raw[:200]}") from exc
        if "error" in result:
            error = result["error"]
            raise ToolkitUnavailable(f"MCP {method}: {error.get('message', error)}")
        return result

    def list_tools(self) -> list[ToolDescriptor]:
        result = self._rpc("tools/list")
        raw_tools = result.get("result", {}).get("tools", [])
        self._cache = [_normalise_tool(item, provider=self.name) for item in raw_tools]
        return list(self._cache)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = self._rpc("tools/call", {"name": name, "arguments": arguments})
        payload = result.get("result", {})
        if payload.get("isError"):
            text = "\n".join(str(item.get("text", "")) for item in payload.get("content", []))
            raise ToolkitUnavailable(text or "MCP tool call failed")
        content = payload.get("content", [])
        texts = [item.get("text", "") for item in content if item.get("type") == "text"]
        if len(texts) == 1:
            return texts[0]
        return texts or payload

    def health(self) -> dict[str, Any]:
        started = time.perf_counter()
        try:
            tools = self.list_tools()
        except ToolkitUnavailable as exc:
            return {"status": "unavailable", "endpoint": self.endpoint, "error": str(exc)}
        return {
            "status": "ok",
            "mode": "remote",
            "endpoint": self.endpoint,
            "tools_count": len(tools),
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        }


class UnavailableToolkitProvider:
    name = "agent-toolkit-unavailable"
    endpoint = ""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def list_tools(self) -> list[ToolDescriptor]:
        return []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        raise ToolkitUnavailable(self.reason)

    def health(self) -> dict[str, Any]:
        return {"status": "unavailable", "mode": "none", "error": self.reason, "tools_count": 0}


def build_toolkit_provider(settings: Any) -> LocalToolkitProvider | RemoteMCPProvider | UnavailableToolkitProvider:
    mode = str(getattr(settings, "toolkit_mode", "auto")).lower()
    workspace = getattr(settings, "workspace", Path("./workspace"))
    errors: list[str] = []
    if mode in ("auto", "local"):
        try:
            return LocalToolkitProvider.discover(workspace)
        except ToolkitUnavailable as exc:
            errors.append(str(exc))
            if mode == "local":
                return UnavailableToolkitProvider(str(exc))
    if mode in ("auto", "remote"):
        endpoint = str(getattr(settings, "mcp_agent_toolkit", "")).strip()
        if endpoint:
            provider = RemoteMCPProvider(endpoint, api_key=getattr(settings, "mcp_agent_toolkit_key", ""))
            try:
                provider.list_tools()
                return provider
            except ToolkitUnavailable as exc:
                errors.append(str(exc))
    if mode == "disabled":
        return UnavailableToolkitProvider("agent_toolkit отключён настройкой CORTEX_TOOLKIT_MODE=disabled")
    return UnavailableToolkitProvider("; ".join(errors) or "agent_toolkit provider не настроен")


__all__ = [
    "ToolkitUnavailable",
    "LocalToolkitProvider",
    "RemoteMCPProvider",
    "UnavailableToolkitProvider",
    "build_toolkit_provider",
]
