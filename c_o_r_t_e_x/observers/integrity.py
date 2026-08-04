"""Observer, который сигнализирует о потерянном correlation context."""
from __future__ import annotations

from ..bus import InMemoryEventBus
from ..signals import Event


class ContextIntegrityObserver:
    def __init__(self, bus: InMemoryEventBus) -> None:
        self.bus = bus
        self.missing_context = 0
        self.bus.add_callback(self.on_event, "*")

    def on_event(self, event: Event) -> None:
        if event.source in {"event-bus", "context-integrity"}:
            return
        if not event.correlation_id:
            self.missing_context += 1
            # Не публикуем здесь событие: callback вызывается во время publish и
            # рекурсивная публикация ухудшила бы диагностику.

    def snapshot(self) -> dict[str, int]:
        return {"missing_correlation_events": self.missing_context}


__all__ = ["ContextIntegrityObserver"]
