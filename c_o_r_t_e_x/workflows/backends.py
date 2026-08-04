"""Optional orchestration backend capability probes."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class BackendStatus:
    name: str
    available: bool
    mode: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "available": self.available, "mode": self.mode, "detail": self.detail}


def probe_orchestration_backends() -> list[BackendStatus]:
    statuses = [BackendStatus("in_memory", True, "active", "stdlib event-backed workflow engine")]
    try:
        import temporalio  # type: ignore  # noqa: F401
    except ImportError:
        statuses.append(BackendStatus("temporal", False, "optional", "pip install temporalio and run Temporal worker"))
    else:
        statuses.append(BackendStatus("temporal", True, "available", "Temporal SDK imported; worker wiring is deployment-specific"))
    try:
        import langgraph  # type: ignore  # noqa: F401
    except ImportError:
        statuses.append(BackendStatus("langgraph", False, "optional", "pip install langgraph to add graph-native workflows"))
    else:
        statuses.append(BackendStatus("langgraph", True, "available", "LangGraph imported; adapt WorkflowContext nodes"))
    return statuses


__all__ = ["BackendStatus", "probe_orchestration_backends"]
