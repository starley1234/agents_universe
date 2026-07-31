"""Тесты планировщика заданий и фоновых задач (jobs.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import ToolRegistry, Workspace
from agent_toolkit.local import build_file_tools
from agent_toolkit.local.jobs import build_jobs_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        ws = Workspace(tmp.path("ws"))
        section("1. Планировщик задач агента (jobs.*)")
        reg = ToolRegistry()
        for t in build_file_tools(ws):
            reg.add(t)

        jobs_tools = {t.name: t for t in build_jobs_tools(ws, reg)}
        check("зарегистрировано 3 инструмента jobs", len(jobs_tools) == 3)

        res_sched = jobs_tools["jobs.schedule_task"].execute(
            name="create_note",
            interval_sec=60,
            tool_name="files.write_file",
            args_json='{"path": "scheduled.txt", "content": "hello from cron"}',
        )
        check("schedule_task регистрирует задачу", "запланирована" in res_sched)

        res_list = jobs_tools["jobs.list_tasks"].execute()
        check("list_tasks отображает зарегистрированную задачу", "create_note" in res_list)

        res_run = jobs_tools["jobs.run_pending"].execute()
        check("run_pending выполняет задачу", "create_note" in res_run and "Успешно" in res_run)
        check("файл scheduled.txt создан планировщиком", ws.exists("scheduled.txt"))

    return summary("Тесты планировщика задач")


def test_jobs_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
