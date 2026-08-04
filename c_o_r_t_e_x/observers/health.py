"""Наблюдатели за качеством runtime и circuit breaker сигналами."""
from __future__ import annotations

import threading
from collections import Counter, deque
from typing import Any

from ..bus import InMemoryEventBus
from ..signals import Event


class HealthObserver:
    """Собирает дешёвые runtime metrics без перехвата payload-секретов."""

    def __init__(self, bus: InMemoryEventBus, *, max_recent: int = 100) -> None:
        self.bus = bus
        self.counts: Counter[str] = Counter()
        self.recent: deque[dict[str, Any]] = deque(maxlen=max_recent)
        self._lock = threading.RLock()
        self.bus.add_callback(self.on_event)

    def on_event(self, event: Event) -> None:
        with self._lock:
            self.counts[event.event_type] += 1
            if event.event_type.endswith((".failed", ".completed", ".opened", ".hot_swapped")):
                self.recent.append({
                    "event_id": event.event_id,
                    "event_type": event.event_type,
                    "source": event.source,
                    "occurred_at": event.occurred_at.isoformat(),
                    "correlation_id": event.correlation_id,
                })

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {"events_total": sum(self.counts.values()), "by_type": dict(self.counts), "recent": list(self.recent)}


__all__ = ["HealthObserver"]
