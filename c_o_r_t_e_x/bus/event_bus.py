"""Событийная шина C.O.R.T.E.X.

`InMemoryEventBus` — детерминированный transport для development и тестов.
Контракт намеренно близок к Redis Streams/NATS: publish, wildcard topics,
correlation/causation и replay последних событий. Production transport может
реализовать тот же интерфейс, не меняя runtime/workflows.
"""
from __future__ import annotations

import asyncio
import fnmatch
import inspect
import threading
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

from ..signals import Event

Callback = Callable[[Event], Any]


@dataclass
class _Subscriber:
    subscription_id: str
    pattern: str
    queue: asyncio.Queue[Event]
    loop: asyncio.AbstractEventLoop | None = None
    closed: bool = False


class EventSubscription:
    """Асинхронный поток событий с backpressure-safe очередью."""

    def __init__(self, bus: "InMemoryEventBus", item: _Subscriber) -> None:
        self._bus = bus
        self._item = item

    @property
    def subscription_id(self) -> str:
        return self._item.subscription_id

    async def get(self) -> Event:
        if self._item.closed:
            raise StopAsyncIteration
        return await self._item.queue.get()

    def __aiter__(self) -> "EventSubscription":
        return self

    async def __anext__(self) -> Event:
        return await self.get()

    async def close(self) -> None:
        self._bus.unsubscribe(self.subscription_id)
        self._item.closed = True

    async def __aenter__(self) -> "EventSubscription":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()


class InMemoryEventBus:
    """Потокобезопасная async pub/sub шина для локальной работы."""

    def __init__(self, *, max_history: int = 2000) -> None:
        self.max_history = max(1, max_history)
        self._history: deque[Event] = deque(maxlen=self.max_history)
        self._subscribers: dict[str, _Subscriber] = {}
        self._callbacks: list[tuple[str, Callback]] = []
        self._lock = threading.RLock()
        self._counter = 0

    @staticmethod
    def _matches(pattern: str, event_type: str) -> bool:
        return pattern in ("", "*", event_type) or fnmatch.fnmatchcase(event_type, pattern)

    def add_callback(self, callback: Callback, pattern: str = "*") -> None:
        with self._lock:
            self._callbacks.append((pattern, callback))

    def remove_callback(self, callback: Callback) -> None:
        with self._lock:
            self._callbacks = [(p, cb) for p, cb in self._callbacks if cb is not callback]

    def subscribe(self, pattern: str = "*", *, max_queue: int = 200) -> EventSubscription:
        """Подписаться на topic. Pattern поддерживает `*`, например `tool.*`."""
        with self._lock:
            self._counter += 1
            subscription_id = f"sub-{self._counter}"
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            item = _Subscriber(
                subscription_id=subscription_id,
                pattern=pattern or "*",
                queue=asyncio.Queue(maxsize=max(1, max_queue)),
                loop=loop,
            )
            self._subscribers[subscription_id] = item
            return EventSubscription(self, item)

    def unsubscribe(self, subscription_id: str) -> None:
        with self._lock:
            item = self._subscribers.pop(subscription_id, None)
            if item:
                item.closed = True
                # Пробуждаем потенциальный consumer, не оставляя ему вечный await.
                try:
                    item.queue.put_nowait(Event.create("bus.subscription.closed", {}, source="event-bus"))
                except asyncio.QueueFull:
                    pass

    async def publish(self, event: Event | dict[str, Any], *, source: str = "event-bus") -> Event:
        if not isinstance(event, Event):
            event = Event.from_dict(event) if "event_type" in event else Event.create("unknown", event, source=source)
        with self._lock:
            self._history.append(event)
            subscribers = list(self._subscribers.values())
            callbacks = list(self._callbacks)

        for item in subscribers:
            if item.closed or not self._matches(item.pattern, event.event_type):
                continue
            try:
                item.queue.put_nowait(event)
            except asyncio.QueueFull:
                # Потеря старого realtime-события лучше, чем блокировка всей шины.
                try:
                    item.queue.get_nowait()
                    item.queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

        for pattern, callback in callbacks:
            if not self._matches(pattern, event.event_type):
                continue
            result = callback(event)
            if inspect.isawaitable(result):
                await result
        return event

    def publish_sync(self, event: Event | dict[str, Any]) -> Event:
        """Удобный мост для sync callback-ов и stdlib HTTP handler-а."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.publish(event))
        if loop.is_running():
            loop.create_task(self.publish(event))
            return event if isinstance(event, Event) else Event.from_dict(event)
        return loop.run_until_complete(self.publish(event))

    def history(self, *, pattern: str = "*", limit: int = 100) -> list[Event]:
        with self._lock:
            events = [event for event in self._history if self._matches(pattern, event.event_type)]
        return events[-max(0, limit):]

    def clear(self) -> None:
        with self._lock:
            self._history.clear()

    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    async def wait_for(self, pattern: str, *, timeout: float = 10.0) -> Event:
        subscription = self.subscribe(pattern, max_queue=10)
        try:
            return await asyncio.wait_for(subscription.get(), timeout=timeout)
        finally:
            await subscription.close()


class EventBusProtocol:
    """Документационный structural type для Redis/NATS адаптеров."""

    async def publish(self, event: Event | dict[str, Any], **kwargs: Any) -> Event:  # pragma: no cover - interface
        raise NotImplementedError

    def subscribe(self, pattern: str = "*", **kwargs: Any) -> EventSubscription:  # pragma: no cover
        raise NotImplementedError


__all__ = ["InMemoryEventBus", "EventBusProtocol", "EventSubscription"]
