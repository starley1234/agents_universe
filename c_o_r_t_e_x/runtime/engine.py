"""Runtime facade: lifecycle, events, tool execution and context integrity."""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable

from ..bus import InMemoryEventBus, SharedBlackboard
from ..signals import Event
from .tool_catalog import ToolCatalog, ToolNotFound

AgentHandler = Callable[[Event], Any]


class CortexRuntime:
    """Минимальный event-driven runtime для агентов.

    Обработчики подписываются на topics вместо прямого вызова друг друга. Это
    позволяет позже заменить in-memory bus на Redis/NATS и сохранить граф
    взаимодействий.
    """

    def __init__(self, bus: InMemoryEventBus, blackboard: SharedBlackboard, catalog: ToolCatalog) -> None:
        self.bus = bus
        self.blackboard = blackboard
        self.catalog = catalog
        self._handlers: dict[str, tuple[AgentHandler, list[Any]]] = {}
        self._tasks: list[asyncio.Task[Any]] = []
        self.started = False

    def register_agent(self, name: str, handler: AgentHandler, *, topics: list[str] | None = None) -> None:
        if name in self._handlers:
            raise ValueError(f"Agent {name!r} is already registered")
        subscriptions = []
        for topic in topics or ["*"]:
            subscription = self.bus.subscribe(topic)
            task = asyncio.create_task(self._consume(name, handler, subscription)) if self.started else None
            subscriptions.append((subscription, task))
            if task:
                self._tasks.append(task)
        self._handlers[name] = (handler, subscriptions)

    async def _consume(self, name: str, handler: AgentHandler, subscription: Any) -> None:
        try:
            async for event in subscription:
                if event.event_type == "bus.subscription.closed":
                    break
                try:
                    result = handler(event)
                    if inspect.isawaitable(result):
                        result = await result
                    if isinstance(result, Event):
                        await self.bus.publish(result)
                    elif isinstance(result, list):
                        for item in result:
                            if isinstance(item, Event):
                                await self.bus.publish(item)
                except Exception as exc:
                    await self.bus.publish(Event.create(
                        "agent.handler.failed",
                        {"agent": name, "event_type": event.event_type, "error": str(exc)},
                        source=f"agent:{name}", correlation_id=event.correlation_id, causation_id=event.event_id,
                    ))
        except asyncio.CancelledError:
            return

    async def start(self) -> None:
        self.started = True
        for name, (handler, subscriptions) in self._handlers.items():
            for index, (subscription, task) in enumerate(subscriptions):
                if task is None:
                    running = asyncio.create_task(self._consume(name, handler, subscription))
                    subscriptions[index] = (subscription, running)
                    self._tasks.append(running)
        await self.bus.publish(Event.create("runtime.started", {"agents": list(self._handlers)}, source="runtime"))

    async def stop(self) -> None:
        self.started = False
        for _, subscriptions in self._handlers.values():
            for subscription, _ in subscriptions:
                await subscription.close()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()
        await self.bus.publish(Event.create("runtime.stopped", {}, source="runtime"))

    async def emit(self, event_type: str, payload: dict[str, Any] | None = None, *, source: str = "runtime", correlation_id: str | None = None) -> Event:
        return await self.bus.publish(Event.create(event_type, payload or {}, source=source, correlation_id=correlation_id))

    async def execute_tool(self, name: str, arguments: dict[str, Any] | None = None, *, correlation_id: str = ""):
        return await self.catalog.execute(name, arguments, correlation_id=correlation_id)


__all__ = ["CortexRuntime"]
