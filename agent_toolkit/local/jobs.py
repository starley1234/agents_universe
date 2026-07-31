"""Инструменты планировщика фоновых заданий и расписания (Cron / Scheduler).

Позволяют агентам регистрировать регулярные задачи и выполнять отложенные вызовы.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace


class JobStore:
    """Хранилище запланированных задач внутри рабочей области (потокобезопасное)."""

    def __init__(self, ws: Workspace, filename: str = "jobs.json") -> None:
        self.ws = ws
        self.p = ws.resolve(filename)
        self._lock = threading.RLock()
        with self._lock:
            self._jobs: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.p.exists():
            return {}
        try:
            return json.loads(self.p.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def _save(self) -> None:
        self.p.parent.mkdir(parents=True, exist_ok=True)
        self.p.write_text(
            json.dumps(self._jobs, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def schedule_task(
        self, name: str, interval_sec: int, tool_name: str, args: dict[str, Any]
    ) -> str:
        with self._lock:
            now = time.time()
            self._jobs[name] = {
                "name": name,
                "interval_sec": interval_sec,
                "tool_name": tool_name,
                "args": args,
                "last_run": 0.0,
                "next_run": now + interval_sec,
                "created_at": now,
            }
            self._save()
            return f"Задача {name!r} запланирована (интервал: {interval_sec} с, вызов: {tool_name})"

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._jobs.values())

    def run_pending(self, registry: Any) -> list[str]:
        with self._lock:
            now = time.time()
            executed: list[str] = []
            for name, job in self._jobs.items():
                if now >= job.get("next_run", 0.0) or job.get("last_run", 0.0) == 0.0:
                    tool_name = job["tool_name"]
                    args = job.get("args", {})
                    try:
                        res = registry.execute(tool_name, **args)
                        job["last_run"] = now
                        job["next_run"] = now + job["interval_sec"]
                        executed.append(
                            f"[JOB {name}] Успешно выполнена {tool_name}: {res!s}"
                        )
                    except Exception as exc:  # noqa: BLE001
                        executed.append(
                            f"[JOB {name}] Ошибка вызова {tool_name}: {exc}"
                        )
            if executed:
                self._save()
            return executed


def build_jobs_tools(ws: Workspace, registry_ref: Any | None = None) -> list[Tool]:
    """Собрать инструменты планировщика задач."""
    store = JobStore(ws=ws)

    def schedule_task(
        name: str, interval_sec: int, tool_name: str, args_json: str = "{}"
    ) -> str:
        if not name.strip() or not tool_name.strip():
            raise ToolError("Имя задачи и имя инструмента не могут быть пустыми")
        try:
            args = json.loads(args_json) if args_json else {}
            if not isinstance(args, dict):
                raise ValueError("args_json должен быть JSON-объектом")
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON аргументов: {exc}") from exc
        return store.schedule_task(
            name.strip(), max(1, interval_sec), tool_name.strip(), args
        )

    def list_tasks() -> str:
        tasks = store.list_tasks()
        if not tasks:
            return "(Запланированных задач нет)"
        lines = ["### Запланированные фоновые задачи:"]
        for t in tasks:
            lines.append(
                f"- **{t['name']}** -> `{t['tool_name']}` "
                f"(интервал: {t['interval_sec']} с)"
            )
        return "\n".join(lines)

    def run_pending() -> str:
        if not registry_ref:
            return "(Реестр инструментов не подключён к планировщику)"
        out = store.run_pending(registry_ref)
        if not out:
            return "(Задач, требующих немедленного запуска, нет)"
        return "### Выполненные задачи:\n" + "\n".join(f"- {ln}" for ln in out)

    return [
        Tool(
            name="jobs.schedule_task",
            description="Запланировать регулярное выполнение инструмента по таймеру (Cron / Scheduler).",
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Уникальное имя задачи (например, 'daily_backup')",
                    },
                    "interval_sec": {
                        "type": "integer",
                        "description": "Интервал выполнения в секундах",
                    },
                    "tool_name": {
                        "type": "string",
                        "description": "Имя вызываемого инструмента",
                    },
                    "args_json": {
                        "type": "string",
                        "description": "JSON аргументы для вызова инструмента",
                    },
                },
                "required": ["name", "interval_sec", "tool_name"],
            },
            fn=schedule_task,
            skills=["jobs", "cron", "scheduler", "automation", "local"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "scheduled_job",
                "speed": "fast",
                "tags": ["jobs", "cron", "scheduler", "timer", "automation"],
            },
            example='jobs.schedule_task(name="check_site", interval_sec=3600, tool_name="site_qa.check_url", args_json=\'{"url": "https://example.com"}\')',
        ),
        Tool(
            name="jobs.list_tasks",
            description="Посмотреть список всех запланированных задач агента.",
            parameters={"type": "object", "properties": {}},
            fn=list_tasks,
            skills=["jobs", "cron", "scheduler", "automation", "local"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "scheduled_job",
                "speed": "fast",
                "tags": ["jobs", "cron", "list", "timer"],
            },
            example="jobs.list_tasks()",
        ),
        Tool(
            name="jobs.run_pending",
            description="Запустить выполнение всех созревших (pending) запланированных задач.",
            parameters={"type": "object", "properties": {}},
            fn=run_pending,
            skills=["jobs", "cron", "scheduler", "automation", "local"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "scheduled_job",
                "speed": "medium",
                "tags": ["jobs", "cron", "run", "timer"],
            },
            example="jobs.run_pending()",
        ),
    ]
