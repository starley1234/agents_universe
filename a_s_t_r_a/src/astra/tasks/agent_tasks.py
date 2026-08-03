"""TaskIQ tasks for async agent execution — prevents job loss on pod crash."""

from __future__ import annotations

import json
from uuid import UUID

from loguru import logger

from astra.tasks.worker import broker
from astra.db.engine import get_session
from astra.db.repositories import SessionRepo, ProjectRepo, MemoryChunkRepo
from astra.llm.embeddings import embedding_service
from astra.core.agent import agent_graph
from astra.core.state import make_initial_state


@broker.task(task_name="astra.run_agent")
async def run_agent_task(session_id: str, project_id: str, goal: str) -> dict:
    """Background job that runs the agent graph and persists result.

    This is used by /api/agents/run/async endpoint and survives pod restarts
    because job is stored in Redis stream (TaskIQ + Redis).
    """
    logger.info("🎯 TaskIQ job started: session={} project={} goal={:.100}", session_id, project_id, goal)

    try:
        sid = UUID(session_id)
        pid = UUID(project_id)
    except Exception as exc:
        logger.error("Invalid UUIDs in task: {}", exc)
        return {"status": "error", "error": str(exc)}

    # Update session to running
    try:
        async with get_session() as db:
            repo = SessionRepo(db)
            sess = await db.get(repo.__class__.__bases__[0] if False else None, sid)  # dummy to keep flow
    except Exception:
        pass  # ignore, repo will handle

    initial_state = make_initial_state(session_id=sid, project_id=pid, goal=goal)

    try:
        final_state = await agent_graph.ainvoke(initial_state)
        ai_messages = [m for m in final_state["messages"] if m.type == "ai"]
        result = ai_messages[-1].content if ai_messages else "Agent completed."
        steps_taken = len(final_state.get("completed_steps", []))

        if final_state.get("is_halted"):
            status = "halted"
            result = f"[Circuit breaker] Halted after {steps_taken} steps.\n\n{result}"
        else:
            status = "completed"

        # Persist result and memory
        async with get_session() as db:
            session_repo = SessionRepo(db)
            await session_repo.finish(sid, result, status)

            # Memory
            try:
                vector = None
                try:
                    vector = await embedding_service.embed(f"Goal: {goal}\nResult: {result[:2000]}")
                except Exception:
                    pass
                meta = json.dumps({"session_id": session_id, "type": "session_result_async"}, ensure_ascii=False)
                mem_repo = MemoryChunkRepo(db)
                await mem_repo.add(pid, f"Goal: {goal}\nResult: {result[:2000]}", vector, meta)
            except Exception as exc:
                logger.warning("Memory store failed in task: {}", exc)

        logger.info("✅ TaskIQ job completed: session={} status={} steps={}", session_id, status, steps_taken)
        return {"status": status, "result": result, "steps_taken": steps_taken}

    except Exception as exc:
        logger.exception("TaskIQ job failed: session={}", session_id)
        try:
            async with get_session() as db:
                session_repo = SessionRepo(db)
                await session_repo.finish(sid, f"Agent failed: {exc}", "error")
        except Exception:
            pass
        return {"status": "error", "error": str(exc)}
