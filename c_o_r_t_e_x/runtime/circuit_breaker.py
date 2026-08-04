"""Circuit breaker для внешних агентов, MCP и inference providers."""
from __future__ import annotations

import asyncio
import inspect
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, TypeVar

from ..signals import CircuitSnapshot

T = TypeVar("T")


class CircuitOpenError(RuntimeError):
    pass


class CircuitBreaker:
    def __init__(self, name: str, *, failure_threshold: int = 3, recovery_timeout: float = 30.0) -> None:
        self.name = name
        self.failure_threshold = max(1, failure_threshold)
        self.recovery_timeout = max(0.1, recovery_timeout)
        self.state = "closed"
        self.failures = 0
        self.successes = 0
        self.opened_at: datetime | None = None
        self.last_error: str | None = None
        self._lock = threading.RLock()

    def allow_request(self) -> bool:
        with self._lock:
            if self.state == "closed":
                return True
            if self.state == "open" and self.opened_at:
                if datetime.now(timezone.utc) - self.opened_at >= timedelta(seconds=self.recovery_timeout):
                    self.state = "half_open"
                    return True
            return self.state == "half_open"

    def before_call(self) -> None:
        if not self.allow_request():
            raise CircuitOpenError(f"Circuit breaker {self.name!r} is open")

    def record_success(self) -> None:
        with self._lock:
            self.successes += 1
            self.failures = 0
            self.last_error = None
            self.state = "closed"
            self.opened_at = None

    def record_failure(self, error: Exception | str) -> None:
        with self._lock:
            self.failures += 1
            self.last_error = str(error)
            if self.failures >= self.failure_threshold:
                self.state = "open"
                self.opened_at = datetime.now(timezone.utc)

    def snapshot(self) -> CircuitSnapshot:
        with self._lock:
            return CircuitSnapshot(
                name=self.name,
                state=self.state,
                failures=self.failures,
                successes=self.successes,
                opened_at=self.opened_at,
                last_error=self.last_error,
            )

    async def call(self, fn: Callable[..., T | Awaitable[T]], *args: Any, **kwargs: Any) -> T:
        self.before_call()
        try:
            result = fn(*args, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:
            self.record_failure(exc)
            raise
        else:
            self.record_success()
            return result  # type: ignore[return-value]


__all__ = ["CircuitBreaker", "CircuitOpenError"]
