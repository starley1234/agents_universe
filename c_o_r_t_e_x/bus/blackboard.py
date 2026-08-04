"""Shared Blackboard — согласованное состояние для параллельных агентов."""
from __future__ import annotations

import asyncio
import threading
from copy import deepcopy
from typing import Any

from ..signals import BlackboardEntry, Event
from .event_bus import InMemoryEventBus


class BlackboardConflict(RuntimeError):
    """Оптимистичная блокировка обнаружила устаревшую версию записи."""


class SharedBlackboard:
    """Key/value blackboard с версиями, аудитом и событиями `blackboard.*`.

    Ключи неймспейсируются строкой (`project/plan`, `audit/latest`). В
    production этот контракт маппится на PostgreSQL JSONB + RLS или Redis
    Hash/Streams; in-memory реализация сохраняет семантику CAS для тестов.
    """

    def __init__(self, bus: InMemoryEventBus | None = None, *, owner: str = "cortex") -> None:
        self.bus = bus
        self.owner = owner
        self._values: dict[str, BlackboardEntry] = {}
        self._lock = threading.RLock()

    def read(self, key: str, default: Any = None) -> Any:
        with self._lock:
            entry = self._values.get(key)
            return deepcopy(entry.value) if entry else default

    def read_entry(self, key: str) -> BlackboardEntry | None:
        with self._lock:
            entry = self._values.get(key)
            return deepcopy(entry) if entry else None

    def version(self, key: str) -> int:
        with self._lock:
            return self._values.get(key, BlackboardEntry(key=key, value=None, version=0)).version

    def snapshot(self, prefix: str = "") -> dict[str, Any]:
        with self._lock:
            return {
                key: deepcopy(entry.value)
                for key, entry in self._values.items()
                if not prefix or key.startswith(prefix)
            }

    def entries(self, prefix: str = "") -> list[BlackboardEntry]:
        with self._lock:
            return [
                deepcopy(entry)
                for key, entry in self._values.items()
                if not prefix or key.startswith(prefix)
            ]

    async def write(
        self,
        key: str,
        value: Any,
        *,
        expected_version: int | None = None,
        updated_by: str | None = None,
        correlation_id: str | None = None,
    ) -> BlackboardEntry:
        with self._lock:
            current = self._values.get(key)
            current_version = current.version if current else 0
            if expected_version is not None and expected_version != current_version:
                raise BlackboardConflict(
                    f"Blackboard key {key!r} has version {current_version}; expected {expected_version}"
                )
            entry = BlackboardEntry(
                key=key,
                value=deepcopy(value),
                version=current_version + 1,
                updated_by=updated_by or self.owner,
            )
            self._values[key] = entry

        if self.bus:
            await self.bus.publish(
                Event.create(
                    "blackboard.updated",
                    {"key": key, "value": entry.value, "version": entry.version, "updated_by": entry.updated_by},
                    source="blackboard",
                    correlation_id=correlation_id,
                )
            )
        return deepcopy(entry)

    async def patch(
        self,
        key: str,
        changes: dict[str, Any],
        *,
        expected_version: int | None = None,
        updated_by: str | None = None,
    ) -> BlackboardEntry:
        current = self.read(key, {})
        if not isinstance(current, dict):
            raise TypeError(f"Blackboard value {key!r} is not an object")
        merged = dict(current)
        merged.update(changes)
        return await self.write(
            key,
            merged,
            expected_version=expected_version,
            updated_by=updated_by,
        )

    async def delete(self, key: str, *, expected_version: int | None = None) -> bool:
        with self._lock:
            current = self._values.get(key)
            if current is None:
                return False
            if expected_version is not None and expected_version != current.version:
                raise BlackboardConflict(
                    f"Blackboard key {key!r} has version {current.version}; expected {expected_version}"
                )
            del self._values[key]
        if self.bus:
            await self.bus.publish(Event.create("blackboard.deleted", {"key": key}, source="blackboard"))
        return True

    def write_sync(self, key: str, value: Any, **kwargs: Any) -> BlackboardEntry:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.write(key, value, **kwargs))
        raise RuntimeError("write_sync cannot be called from a running event loop; await write()")


__all__ = ["SharedBlackboard", "BlackboardConflict"]
