"""Инструменты многоагентной оркестрации и вызова субагентов (agent.*).

Позволяют агенту-оркестратору делегировать подзадачи специализированным
субагентам (исследователям, кодерам, аудиторам) и получать результаты.
"""
from __future__ import annotations

import concurrent.futures
import json
import threading
from dataclasses import dataclass
from typing import Any

from ..core import Tool, ToolError, Workspace

BUILTIN_SUBAGENTS: dict[str, dict[str, Any]] = {
    "researcher": {
        "title": "Агент-исследователь (Research AI)",
        "summary": "Поиск информации, анализ рынка и сбор фактов",
        "skills": ["research", "search", "web", "analysis"],
    },
    "coder": {
        "title": "Агент-разработчик (Code & Dev AI)",
        "summary": "Написание кода, автотестов и Code Review",
        "skills": ["code", "dev", "git", "python", "testing"],
    },
    "auditor": {
        "title": "Агент-аудитор (Quality & Compliance AI)",
        "summary": "Проверка соответствия правилам, аудит полок и отчётов",
        "skills": ["audit", "qa", "compliance", "retail"],
    },
    "reporter": {
        "title": "Агент-документовед (Documentation AI)",
        "summary": "Создание отчётов Word (.docx), Excel (.xlsx) и счетов",
        "skills": ["office", "documentation", "reports", "templates"],
    },
}


class SubagentService:
    """Сервис управления и вызова субагентов (потокобезопасный)."""

    def __init__(self, ws: Workspace | None = None) -> None:
        self.ws = ws
        self.call_history: list[dict[str, Any]] = []
        self._lock = threading.RLock()

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"name": k, "title": v["title"], "summary": v["summary"], "skills": v["skills"]}
                for k, v in BUILTIN_SUBAGENTS.items()
            ]

    def call_subagent(
        self, agent_name: str, task: str, context: dict[str, Any] | None = None
    ) -> str:
        with self._lock:
            if agent_name not in BUILTIN_SUBAGENTS:
                available = ", ".join(BUILTIN_SUBAGENTS.keys())
                raise ToolError(
                    f"Субагент {agent_name!r} не найден. Доступны: {available}"
                )
            if not task.strip():
                raise ToolError("Задача для субагента не может быть пустой")

            ctx = context or {}
            self.call_history.append(
                {"agent": agent_name, "task": task, "context": ctx}
            )
            sub = BUILTIN_SUBAGENTS[agent_name]
            return (
                f"[SUBAGENT {agent_name.upper()}] Успешно выполнена задача: {task!r}\n"
                f"Роль: {sub['title']}\n"
                f"Контекст: {json.dumps(ctx, ensure_ascii=False) if ctx else 'нет'}"
            )

    def parallel_map_reduce(
        self, agent_name: str, tasks_json: str, max_workers: int = 5
    ) -> str:
        if not tasks_json.strip():
            raise ToolError("JSON-массив задач для параллельного MapReduce не может быть пустым")
        try:
            tasks_list = json.loads(tasks_json)
            if not isinstance(tasks_list, list) or not tasks_list:
                raise ValueError("tasks_json должен быть непустым массивом задач (list)")
        except Exception as exc:
            raise ToolError(f"Некорректный JSON массив задач: {exc}") from exc

        workers = max(1, min(max_workers, len(tasks_list), 10))
        results: list[tuple[int, str, str]] = []
        errors: list[tuple[int, str, str]] = []

        def _run_task(idx: int, titem: Any) -> tuple[int, str, str, str]:
            tstr = str(titem)
            try:
                res = self.call_subagent(agent_name=agent_name, task=tstr)
                return (idx, tstr, "ok", res)
            except Exception as exc:
                return (idx, tstr, "err", str(exc))

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_idx = {
                executor.submit(_run_task, idx, titem): idx
                for idx, titem in enumerate(tasks_list, 1)
            }
            for fut in concurrent.futures.as_completed(future_to_idx):
                idx, tstr, status, out = fut.result()
                if status == "ok":
                    results.append((idx, tstr, out))
                else:
                    errors.append((idx, tstr, out))

        results.sort(key=lambda x: x[0])
        errors.sort(key=lambda x: x[0])

        lines = [
            f"### Параллельное MapReduce выполнение субагента `{agent_name}`:",
            f"- **Количество задач (Map):** {len(tasks_list)}",
            f"- **Параллельных потоков (Workers):** {workers}",
            f"- **Успешно выполнено:** {len(results)}/{len(tasks_list)}",
        ]
        if errors:
            lines.append(f"- **Ошибок:** {len(errors)}")

        lines.append("\n#### Сводный отчёт (Reduce):")
        for idx, tstr, out in results:
            lines.append(f"{idx}. **Задача:** `{tstr}` -> УСПЕШНО\n   - {out.splitlines()[0]}")
        for idx, tstr, err in errors:
            lines.append(f"{idx}. **Задача:** `{tstr}` -> ОШИБКА: {err}")

        return "\n".join(lines)


def build_subagent_tools(
    ws: Workspace | None = None, service: SubagentService | None = None
) -> list[Tool]:
    """Собрать инструменты вызова субагентов (agent.*)."""
    srv = service or SubagentService(ws=ws)

    def call_subagent(
        agent_name: str, task: str, context_json: str = "{}"
    ) -> str:
        try:
            ctx = json.loads(context_json) if context_json else {}
            if not isinstance(ctx, dict):
                raise ValueError("context_json должен быть JSON-объектом")
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON контекста: {exc}") from exc
        return srv.call_subagent(agent_name=agent_name, task=task, context=ctx)

    def list_agents() -> str:
        agents = srv.list_agents()
        lines = ["### Доступные субагенты в системе:"]
        for a in agents:
            lines.append(
                f"- **`{a['name']}`** — {a['title']} "
                f"({a['summary']})"
            )
        return "\n".join(lines)

    def parallel_map_reduce(
        agent_name: str, tasks_json: str, max_workers: int = 5
    ) -> str:
        return srv.parallel_map_reduce(agent_name, tasks_json, max_workers)

    return [
        Tool(
            name="agent.call_subagent",
            description="Делегировать задачу специализированному субагенту (researcher, coder, auditor, reporter).",
            parameters={
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "description": "Имя субагента (researcher, coder, auditor, reporter)",
                    },
                    "task": {
                        "type": "string",
                        "description": "Формулировка задачи для субагента",
                    },
                    "context_json": {
                        "type": "string",
                        "description": "JSON объект с дополнительным контекстом задачи",
                    },
                },
                "required": ["agent_name", "task"],
            },
            fn=call_subagent,
            skills=["agent", "subagent", "orchestration", "delegate", "multi_agent"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "subagent_task",
                "speed": "medium",
                "tags": ["agent", "subagent", "delegate", "orchestrator", "task"],
            },
            example='agent.call_subagent(agent_name="auditor", task="Проверить отчёт")',
        ),
        Tool(
            name="agent.list_agents",
            description="Получить список всех доступных в системе субагентов и их компетенций.",
            parameters={"type": "object", "properties": {}},
            fn=list_agents,
            skills=["agent", "subagent", "orchestration", "list", "multi_agent"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "subagent_list",
                "speed": "fast",
                "tags": ["agent", "subagent", "list", "orchestrator"],
            },
            example="agent.list_agents()",
        ),
        Tool(
            name="agent.parallel_map_reduce",
            description="Параллельный запуск субагента по паттерну MapReduce для распределённого выполнения задач и сведения отчёта.",
            parameters={
                "type": "object",
                "properties": {
                    "agent_name": {
                        "type": "string",
                        "description": "Имя субагента ('researcher', 'coder', 'auditor', 'reporter')",
                    },
                    "tasks_json": {
                        "type": "string",
                        "description": 'JSON-массив задач (\'["Задача 1", "Задача 2"]\')',
                    },
                    "max_workers": {
                        "type": "integer",
                        "description": "Максимальное число параллельных потоков (по умолчанию 5)",
                    },
                },
                "required": ["agent_name", "tasks_json"],
            },
            fn=parallel_map_reduce,
            skills=["agent", "subagent", "orchestration", "mapreduce", "parallel", "multi_agent"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "subagent_mapreduce",
                "speed": "medium",
                "tags": [
                    "mapreduce",
                    "parallel",
                    "subagent",
                    "orchestrator",
                    "distributed",
                    "распределённый",
                    "параллельный",
                ],
            },
            example='agent.parallel_map_reduce(agent_name="researcher", tasks_json=\'["Анализ А", "Анализ Б"]\')',
        ),
    ]
