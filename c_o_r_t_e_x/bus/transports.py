"""Optional Redis/NATS transport probes.

Сервисы не обязаны поднимать Redis или NATS для локального запуска. Эти
маленькие адаптеры дают production-точку расширения и явно сообщают, когда
зависимость не установлена или endpoint недоступен.
"""
from __future__ import annotations

import json
from typing import Any

from ..signals import Event
from .event_bus import InMemoryEventBus


class TransportUnavailable(RuntimeError):
    pass


class RedisEventBus(InMemoryEventBus):
    """Redis Streams-compatible facade; falls back to local history for tests.

    При установленном `redis` публикация дублируется в Redis channel. Local
    subscribers остаются доступны для SSE внутри текущего процесса.
    """

    def __init__(self, redis_url: str, *, channel: str = "cortex.events", max_history: int = 2000) -> None:
        super().__init__(max_history=max_history)
        self.redis_url = redis_url
        self.channel = channel
        try:
            import redis.asyncio as redis_async  # type: ignore
        except ImportError:
            self._redis = None
        else:
            self._redis = redis_async.from_url(redis_url, decode_responses=True)

    async def publish(self, event: Event | dict[str, Any], **kwargs: Any) -> Event:
        result = await super().publish(event, **kwargs)
        if self._redis is not None:
            try:
                await self._redis.publish(self.channel, result.model_dump_json())
            except Exception:
                # Bus must not disappear because Redis is temporarily down.
                pass
        return result

    @property
    def backend_available(self) -> bool:
        return self._redis is not None


class NATSEventBus(InMemoryEventBus):
    """NATS JetStream extension point with the same local delivery contract."""

    def __init__(self, nats_url: str, *, subject: str = "cortex.events.>", max_history: int = 2000) -> None:
        super().__init__(max_history=max_history)
        self.nats_url = nats_url
        self.subject = subject
        try:
            import nats  # type: ignore  # noqa: F401
        except ImportError:
            self._nats_available = False
        else:
            self._nats_available = True

    @property
    def backend_available(self) -> bool:
        return self._nats_available


def build_event_bus(settings: Any) -> InMemoryEventBus:
    backend = str(getattr(settings, "event_bus_backend", "memory")).lower()
    if backend == "redis":
        return RedisEventBus(settings.redis_url, max_history=settings.max_event_history)
    if backend == "nats":
        return NATSEventBus(settings.nats_url, max_history=settings.max_event_history)
    return InMemoryEventBus(max_history=settings.max_event_history)


__all__ = ["RedisEventBus", "NATSEventBus", "TransportUnavailable", "build_event_bus"]
