"""Контракты сообщений C.O.R.T.E.X.

При наличии Pydantic используются его dataclass-валидаторы; в минимальной
stdlib-установке те же модели остаются обычными dataclass и сохраняют API
`model_dump()`/`model_validate()`. Это позволяет запускать тестовый runtime без
PostgreSQL и без pip-install, не меняя протокол событий.
"""
from __future__ import annotations

import json
from dataclasses import asdict, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, ClassVar, TypeVar
from uuid import uuid4

try:  # Pydantic — рекомендуемый production-валидатор, но не hard dependency для dev
    from pydantic.dataclasses import dataclass as _model_dataclass
    PYDANTIC_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in the stdlib CI image
    from dataclasses import dataclass as _model_dataclass
    PYDANTIC_AVAILABLE = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    return value


T = TypeVar("T")


class ModelMixin:
    """Небольшой общий слой совместимости с Pydantic v2."""

    _model_type: ClassVar[type | None] = None

    def model_dump(self, *, mode: str = "python", **_: Any) -> dict[str, Any]:
        data = asdict(self)  # type: ignore[arg-type]
        return data if mode == "python" else _jsonable(data)

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self.model_dump())

    def model_dump_json(self, **kwargs: Any) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, **kwargs)

    @classmethod
    def model_validate(cls: type[T], value: Any) -> T:
        if isinstance(value, cls):
            return value
        if hasattr(value, "model_dump"):
            value = value.model_dump()
        if not isinstance(value, dict):
            raise TypeError(f"{cls.__name__} expects an object")
        return cls(**value)  # type: ignore[call-arg]


@_model_dataclass
class Event(ModelMixin):
    event_type: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    event_id: str = field(default_factory=lambda: str(uuid4()))
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    causation_id: str | None = None
    occurred_at: datetime = field(default_factory=utc_now)
    schema_version: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        source: str = "cortex",
        correlation_id: str | None = None,
        causation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> "Event":
        return cls(
            event_type=event_type,
            source=source,
            payload=payload or {},
            correlation_id=correlation_id or str(uuid4()),
            causation_id=causation_id,
            metadata=metadata or {},
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Event":
        data = dict(value)
        timestamp = data.get("occurred_at")
        if isinstance(timestamp, str):
            data["occurred_at"] = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return cls.model_validate(data)


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_HITL = "waiting_hitl"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@_model_dataclass
class Task(ModelMixin):
    title: str
    workflow: str = "toolkit_audit"
    payload: dict[str, Any] = field(default_factory=dict)
    task_id: str = field(default_factory=lambda: str(uuid4()))
    status: TaskStatus = TaskStatus.PENDING
    result: dict[str, Any] | None = None
    error: str | None = None
    correlation_id: str = field(default_factory=lambda: str(uuid4()))
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    version: int = 0

    def transition(self, status: TaskStatus, *, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        self.status = status
        self.result = result if result is not None else self.result
        self.error = error
        self.updated_at = utc_now()
        self.version += 1


@_model_dataclass
class ToolDescriptor(ModelMixin):
    name: str
    description: str = ""
    input_schema: dict[str, Any] = field(default_factory=lambda: {"type": "object", "properties": {}})
    skills: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    dangerous: bool = False
    provider: str = "agent-toolkit"
    enabled: bool = True

    @classmethod
    def from_mcp(cls, value: dict[str, Any], *, provider: str = "agent-toolkit") -> "ToolDescriptor":
        schema = value.get("inputSchema") or value.get("parameters") or {"type": "object", "properties": {}}
        metadata = value.get("metadata") or {}
        attributes = dict(metadata.get("attributes") or value.get("attributes") or {})
        dangerous = bool(metadata.get("dangerous", value.get("dangerous", attributes.get("dangerous", False))))
        return cls(
            name=str(value.get("name", "")),
            description=str(value.get("description", "")),
            input_schema=schema,
            skills=list(metadata.get("skills") or value.get("skills") or []),
            attributes=attributes,
            dangerous=dangerous,
            provider=provider,
            enabled=bool(value.get("enabled", True)),
        )


@_model_dataclass
class ToolCallResult(ModelMixin):
    tool_name: str
    success: bool
    result: Any = None
    error: str | None = None
    provider: str = ""
    duration_ms: float = 0.0
    correlation_id: str = ""


@_model_dataclass
class BlackboardEntry(ModelMixin):
    key: str
    value: Any
    version: int = 1
    updated_at: datetime = field(default_factory=utc_now)
    updated_by: str = "cortex"


@_model_dataclass
class ApprovalRequest(ModelMixin):
    action: str
    reason: str
    request_id: str = field(default_factory=lambda: str(uuid4()))
    task_id: str | None = None
    status: str = "pending"
    requested_at: datetime = field(default_factory=utc_now)
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_note: str | None = None


@_model_dataclass
class AuditItem(ModelMixin):
    name: str
    status: str
    status_label: str
    preview: str = ""
    duration_ms: float = 0.0
    recommendation: str = ""
    hint: str | None = None
    provider: str = "agent-toolkit"
    tested: bool = False
    dangerous: bool = False


@_model_dataclass
class AuditReport(ModelMixin):
    report_id: str = field(default_factory=lambda: str(uuid4()))
    provider: str = "agent-toolkit"
    created_at: datetime = field(default_factory=utc_now)
    success: bool = True
    total: int = 0
    tested: int = 0
    passed: int = 0
    requires_configuration: int = 0
    failed: int = 0
    skipped: int = 0
    duration_ms: float = 0.0
    items: list[AuditItem] = field(default_factory=list)
    recommendations: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def coverage_percent(self) -> float:
        return round((self.tested / self.total) * 100.0, 1) if self.total else 0.0


@_model_dataclass
class CircuitSnapshot(ModelMixin):
    name: str
    state: str = "closed"
    failures: int = 0
    successes: int = 0
    opened_at: datetime | None = None
    last_error: str | None = None


__all__ = [
    "PYDANTIC_AVAILABLE",
    "Event",
    "Task",
    "TaskStatus",
    "ToolDescriptor",
    "ToolCallResult",
    "BlackboardEntry",
    "ApprovalRequest",
    "AuditItem",
    "AuditReport",
    "CircuitSnapshot",
    "utc_now",
]
