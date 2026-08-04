"""Durable-by-events workflow engine для локального runtime.

Workflow state хранится в Task + Blackboard и каждый переход публикуется.
Temporal/LangGraph adapters can call the same registered workflow functions;
в development нет обязательного daemon-а.
"""
from __future__ import annotations

import asyncio
import inspect
import threading
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from ..bus import InMemoryEventBus, SharedBlackboard
from ..runtime.tool_catalog import ToolCatalog
from ..signals import Event, Task, TaskStatus

WorkflowHandler = Callable[["WorkflowContext"], Any]


@dataclass
class WorkflowContext:
    task: Task
    bus: InMemoryEventBus
    blackboard: SharedBlackboard
    catalog: ToolCatalog

    async def emit(self, event_type: str, payload: dict[str, Any] | None = None, *, source: str = "workflow") -> Event:
        return await self.bus.publish(Event.create(
            event_type, payload or {}, source=source,
            correlation_id=self.task.correlation_id,
        ))

    async def checkpoint(self, key: str, value: Any) -> Any:
        entry = await self.blackboard.write(
            f"tasks/{self.task.task_id}/{key}", value,
            updated_by=f"workflow:{self.task.workflow}",
            correlation_id=self.task.correlation_id,
        )
        await self.emit("workflow.checkpoint", {"key": key, "version": entry.version})
        return value


class WorkflowNotFound(KeyError):
    pass


class WorkflowEngine:
    def __init__(self, bus: InMemoryEventBus, blackboard: SharedBlackboard, catalog: ToolCatalog) -> None:
        self.bus = bus
        self.blackboard = blackboard
        self.catalog = catalog
        self.handlers: dict[str, WorkflowHandler] = {}
        self.tasks: dict[str, Task] = {}
        self._lock = threading.RLock()

    def register(self, name: str, handler: WorkflowHandler) -> None:
        self.handlers[name] = handler

    async def submit(
        self,
        title: str,
        *,
        workflow: str = "toolkit_audit",
        payload: dict[str, Any] | None = None,
        run: bool = False,
    ) -> Task:
        if workflow not in self.handlers:
            raise WorkflowNotFound(workflow)
        task = Task(title=title, workflow=workflow, payload=payload or {})
        with self._lock:
            self.tasks[task.task_id] = task
        await self.bus.publish(Event.create(
            "task.created", task.to_dict(), source="workflow-engine", correlation_id=task.correlation_id,
        ))
        if run:
            asyncio.create_task(self.run(task.task_id))
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            task = self.tasks.get(task_id)
            return task

    def list(self, *, limit: int = 100) -> list[Task]:
        with self._lock:
            return list(self.tasks.values())[-max(0, limit):]

    async def _transition(self, task: Task, status: TaskStatus, *, result: dict[str, Any] | None = None, error: str | None = None) -> None:
        task.transition(status, result=result, error=error)
        await self.bus.publish(Event.create(
            f"task.{status.value}", task.to_dict(), source="workflow-engine", correlation_id=task.correlation_id,
        ))

    async def run(self, task_id: str) -> Task:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status not in (TaskStatus.PENDING, TaskStatus.WAITING_HITL):
            return task
        handler = self.handlers.get(task.workflow)
        if handler is None:
            await self._transition(task, TaskStatus.FAILED, error=f"Workflow {task.workflow!r} not registered")
            return task
        await self._transition(task, TaskStatus.RUNNING)
        context = WorkflowContext(task, self.bus, self.blackboard, self.catalog)
        await context.emit("workflow.started", {"workflow": task.workflow, "task_id": task.task_id})
        try:
            result = handler(context)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict):
                output = result
            else:
                output = {"value": result}
        except asyncio.CancelledError:
            await self._transition(task, TaskStatus.CANCELLED, error="Workflow task cancelled")
            raise
        except Exception as exc:
            await self._transition(task, TaskStatus.FAILED, error=str(exc))
            await context.emit("workflow.failed", {"error": str(exc), "task_id": task.task_id})
            return task
        await self._transition(task, TaskStatus.COMPLETED, result=output)
        await context.emit("workflow.completed", {"task_id": task.task_id, "workflow": task.workflow})
        return task

    async def approve(self, task_id: str, *, decision: str, note: str = "", decided_by: str = "operator") -> Task:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        decision = decision.lower()
        if decision not in ("approve", "approved", "reject", "rejected"):
            raise ValueError("decision must be approve or reject")
        if decision.startswith("reject"):
            await self._transition(task, TaskStatus.CANCELLED, error=note or "Rejected by operator")
        else:
            task.payload["hitl_decision"] = {"by": decided_by, "note": note}
            await self._transition(task, TaskStatus.PENDING)
            await self.bus.publish(Event.create(
                "hitl.approved", {"task_id": task_id, "decided_by": decided_by, "note": note},
                source="gateway", correlation_id=task.correlation_id,
            ))
        return task

    async def run_parallel(self, context: WorkflowContext, steps: dict[str, Callable[[WorkflowContext], Any]]) -> dict[str, Any]:
        """Fork-join шаги — базовая реализация для R&D/compliance сценариев."""
        async def execute(name: str, handler: Callable[[WorkflowContext], Any]) -> tuple[str, Any]:
            await context.emit("workflow.step.started", {"step": name})
            try:
                value = handler(context)
                if inspect.isawaitable(value):
                    value = await value
                await context.emit("workflow.step.completed", {"step": name})
                return name, value
            except Exception as exc:
                await context.emit("workflow.step.failed", {"step": name, "error": str(exc)})
                raise

        results = await asyncio.gather(*(execute(name, handler) for name, handler in steps.items()))
        return dict(results)


__all__ = ["WorkflowEngine", "WorkflowContext", "WorkflowNotFound"]
