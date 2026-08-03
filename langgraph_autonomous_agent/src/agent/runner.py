"""Agent runner — bridge between API and LangGraph.

Reads task from DB → runs graph → writes progress/results back → sends reports.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from langchain_core.messages import HumanMessage

from src.agent.graph import get_graph
from src.agent.state import AgentState
from src.config import get_settings
from src.db.engine import db_session
from src.db.repository import create_step, get_steps, get_task, update_task
from src.reporting.email_reporter import send_completion, send_progress
from src.reporting.log_reporter import LogReporter
from src.reporting.ws_manager import ws

log = logging.getLogger(__name__)
_cfg = get_settings()
_running: set[str] = set()


async def run_task(task_id: str) -> None:
    if task_id in _running:
        return
    _running.add(task_id)
    t0 = time.time()
    reporter = LogReporter(task_id, "Task")

    try:
        async with db_session() as db:
            task = await get_task(db, uuid.UUID(task_id))
            if not task:
                log.error("Task %s not found", task_id)
                return
            reporter = LogReporter(task_id, task.title)
            reporter.log_info(f"Starting: {task.title}")
            await update_task(db, uuid.UUID(task_id), status="running",
                              started_at=datetime.now(timezone.utc))
            await ws.broadcast(task_id, {"status": "running", "message": task.title})
            desc, email, mx = task.description, task.report_email, task.max_iterations

        state: AgentState = {
            "task_id": task_id, "task_description": desc,
            "messages": [HumanMessage(content=desc)],
            "plan": [], "current_step": 0, "results": [], "errors": [],
            "quality": 0.0, "iteration": 0, "max_iterations": mx,
            "memory_context": "", "final_result": "", "status": "starting",
            "progress": 0.0, "metadata": {"title": task.title if 'task' in dir() else "", "email": email},
        }

        graph = get_graph()
        last_report = time.time()
        task_title = task.title

        async for event in graph.astream(state, stream_mode="updates"):
            for node, upd in event.items():
                reporter.log_info(f"Node '{node}' done")
                try:
                    async with db_session() as db:
                        kw: dict[str, Any] = {}
                        for f in ("status", "progress", "quality", "iteration", "current_step"):
                            if f in upd:
                                kw[f if f != "iteration" else "iterations"] = upd[f]
                        if "plan" in upd:
                            kw["plan"] = upd["plan"]
                            kw["total_steps"] = len(upd["plan"])
                            existing = {s.step_index for s in await get_steps(db, uuid.UUID(task_id))}
                            for step in upd["plan"]:
                                if step.get("id", 0) not in existing:
                                    await create_step(db, uuid.UUID(task_id),
                                                      step.get("id", 0), step.get("description", ""))
                        if "final_result" in upd and upd["final_result"]:
                            kw.update(result=upd["final_result"], status="completed",
                                      completed_at=datetime.now(timezone.utc))
                        if kw:
                            await update_task(db, uuid.UUID(task_id), **kw)
                except Exception as e:
                    reporter.log_error(f"DB update failed: {e}")

                await ws.broadcast(task_id, {
                    "node": node, "status": upd.get("status", ""),
                    "progress": upd.get("progress", 0), "quality": upd.get("quality", 0),
                    "iteration": upd.get("iteration", 0)})

                now = time.time()
                if email and now - last_report > _cfg.AGENT_REPORT_INTERVAL and upd.get("progress", 0) > 0:
                    last_report = now
                    try:
                        plan = upd.get("plan", [])
                        idx = upd.get("current_step", 0)
                        cur = plan[idx].get("description", "") if idx < len(plan) else ""
                        await send_progress(email, task_title, task_id, upd.get("progress", 0),
                                            cur, sum(1 for s in plan if s.get("status") == "completed"),
                                            len(plan), upd.get("quality", 0), upd.get("iteration", 0))
                        reporter.log_info(f"Progress report → {email}")
                    except Exception as e:
                        reporter.log_warning(f"Report failed: {e}")

        dur = time.time() - t0
        reporter.log_info(f"Completed in {dur:.1f}s")
        async with db_session() as db:
            t = await get_task(db, uuid.UUID(task_id))
            if t and email and t.result:
                await send_completion(email, t.title, task_id, t.result,
                                      t.quality, t.iterations, dur)
        await ws.broadcast(task_id, {"status": "completed", "progress": 100, "duration": dur})

    except Exception as e:
        log.error("Task %s failed: %s", task_id[:8], e, exc_info=True)
        reporter.log_error(str(e))
        try:
            async with db_session() as db:
                await update_task(db, uuid.UUID(task_id), status="failed",
                                  error=str(e)[:2000], completed_at=datetime.now(timezone.utc))
        except Exception:
            pass
        await ws.broadcast(task_id, {"status": "failed", "error": str(e)[:500]})
    finally:
        _running.discard(task_id)
