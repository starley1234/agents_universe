"""Каталог инструментов и dynamic hot-swapping провайдеров."""
from __future__ import annotations

import asyncio
import inspect
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from ..bus import InMemoryEventBus
from ..signals import Event, ToolCallResult, ToolDescriptor
from .circuit_breaker import CircuitBreaker

_SECRET_FIELDS = ("password", "secret", "token", "api_key", "apikey", "authorization", "private_key")


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "***redacted***" if any(word in str(key).lower() for word in _SECRET_FIELDS) else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class ToolProvider(Protocol):
    name: str

    def list_tools(self) -> list[ToolDescriptor]: ...

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


@dataclass
class ProviderRecord:
    name: str
    provider: ToolProvider
    priority: int = 0
    enabled: bool = True
    endpoint: str = ""


class ToolNotFound(KeyError):
    pass


class ToolCatalog:
    """Единая точка поиска/вызова локальных и MCP-инструментов."""

    def __init__(self, bus: InMemoryEventBus | None = None) -> None:
        self.bus = bus
        self._providers: dict[str, ProviderRecord] = {}
        self._routes: dict[str, str] = {}
        self._descriptors: dict[str, ToolDescriptor] = {}
        self._breakers: dict[str, CircuitBreaker] = {}
        self._lock = threading.RLock()

    def mount(self, name: str, provider: ToolProvider, *, priority: int = 0, endpoint: str = "") -> int:
        """Подключить/обновить provider. Возвращает число его инструментов."""
        descriptors = provider.list_tools()
        record = ProviderRecord(name=name, provider=provider, priority=priority, endpoint=endpoint)
        with self._lock:
            previous = self._providers.get(name)
            if previous:
                for tool_name, provider_name in list(self._routes.items()):
                    if provider_name == name:
                        self._routes.pop(tool_name, None)
                        self._descriptors.pop(tool_name, None)
            self._providers[name] = record
            for descriptor in descriptors:
                descriptor.provider = name
                self._descriptors[descriptor.name] = descriptor
                existing = self._routes.get(descriptor.name)
                if existing is None or self._providers.get(existing, record).priority <= priority:
                    self._routes[descriptor.name] = name
                    self._breakers.setdefault(descriptor.name, CircuitBreaker(f"tool:{descriptor.name}"))
        return len(descriptors)

    def unmount(self, name: str) -> bool:
        with self._lock:
            if name not in self._providers:
                return False
            self._providers.pop(name)
            for tool_name, provider_name in list(self._routes.items()):
                if provider_name == name:
                    self._routes.pop(tool_name, None)
                    self._descriptors.pop(tool_name, None)
            return True

    async def hot_swap(self, name: str, provider: ToolProvider, *, reason: str = "runtime reconfiguration", endpoint: str = "") -> dict[str, Any]:
        """Атомарно заменить MCP/local provider и объявить это как событие."""
        old = self._providers.get(name)
        old_names = [d.name for d in self.list_tools(provider=name)] if old else []
        count = self.mount(name, provider, priority=old.priority if old else 0, endpoint=endpoint)
        new_names = [d.name for d in self.list_tools(provider=name)]
        payload = {
            "provider": name,
            "reason": reason,
            "old_tools": old_names,
            "new_tools": new_names,
            "tools_count": count,
            "endpoint": endpoint,
        }
        if self.bus:
            await self.bus.publish(Event.create("runtime.tool_hot_swapped", payload, source="tool-catalog"))
        return payload

    def provider_names(self) -> list[str]:
        with self._lock:
            return sorted(self._providers)

    def list_tools(
        self,
        *,
        query: str = "",
        provider: str | None = None,
        enabled_only: bool = True,
        limit: int = 500,
    ) -> list[ToolDescriptor]:
        with self._lock:
            values = []
            query_tokens = [token for token in query.lower().replace("_", " ").split() if token]
            for name, descriptor in self._descriptors.items():
                route = self._routes.get(name)
                if provider and route != provider:
                    continue
                if enabled_only and (not descriptor.enabled or not route or not self._providers.get(route, ProviderRecord("", None)).enabled):  # type: ignore[arg-type]
                    continue
                haystack = " ".join([name, descriptor.description, *descriptor.skills, *descriptor.attributes.get("tags", [])]).lower()
                score = sum(1 for token in query_tokens if token in haystack)
                if query_tokens and score == 0:
                    continue
                values.append((score, name, descriptor))
            values.sort(key=lambda item: (-item[0], item[1]))
            return [descriptor for _, _, descriptor in values[: max(0, limit)]]

    def get(self, name: str) -> ToolDescriptor | None:
        with self._lock:
            descriptor = self._descriptors.get(name)
            return descriptor if descriptor and descriptor.enabled else None

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        correlation_id: str = "",
    ) -> ToolCallResult:
        args = arguments or {}
        with self._lock:
            descriptor = self._descriptors.get(name)
            provider_name = self._routes.get(name)
            record = self._providers.get(provider_name or "")
            breaker = self._breakers.setdefault(name, CircuitBreaker(f"tool:{name}"))
        if not descriptor or not provider_name or not record or not record.enabled:
            raise ToolNotFound(name)
        if self.bus:
            await self.bus.publish(Event.create(
                "tool.call.started",
                {"tool_name": name, "provider": provider_name, "arguments": _redact(args)},
                source="tool-catalog", correlation_id=correlation_id or None,
            ))
        started = time.perf_counter()
        try:
            async def invoke() -> Any:
                result = record.provider.call_tool(name, args)
                if inspect.isawaitable(result):
                    return await result
                return result

            value = await breaker.call(invoke)
        except Exception as exc:
            duration = (time.perf_counter() - started) * 1000
            if self.bus:
                await self.bus.publish(Event.create(
                    "tool.call.failed",
                    {"tool_name": name, "provider": provider_name, "error": str(exc), "duration_ms": round(duration, 2)},
                    source="tool-catalog", correlation_id=correlation_id or None,
                ))
            return ToolCallResult(
                tool_name=name, success=False, error=str(exc), provider=provider_name,
                duration_ms=round(duration, 2), correlation_id=correlation_id,
            )
        duration = (time.perf_counter() - started) * 1000
        if self.bus:
            await self.bus.publish(Event.create(
                "tool.call.completed",
                {"tool_name": name, "provider": provider_name, "duration_ms": round(duration, 2)},
                source="tool-catalog", correlation_id=correlation_id or None,
            ))
        return ToolCallResult(
            tool_name=name, success=True, result=value, provider=provider_name,
            duration_ms=round(duration, 2), correlation_id=correlation_id,
        )

    def schemas(self, *, query: str = "", limit: int = 500) -> list[dict[str, Any]]:
        return [descriptor.to_dict() for descriptor in self.list_tools(query=query, limit=limit)]

    def breaker_snapshots(self) -> list[dict[str, Any]]:
        with self._lock:
            return [breaker.snapshot().to_dict() for breaker in self._breakers.values()]


__all__ = ["ToolCatalog", "ToolProvider", "ToolNotFound", "ProviderRecord"]
