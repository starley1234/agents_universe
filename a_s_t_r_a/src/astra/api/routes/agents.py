"""Agent execution — sync, async TaskIQ, SSE streaming with provider/model override and safe owner_id."""

from __future__ import annotations

import asyncio
import json
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse
from loguru import logger

from astra.api.deps import db_session
from astra.auth.jwt import get_current_user, get_safe_owner_id
from astra.core.agent import agent_graph
from astra.core.streaming import stream_agent
from astra.core.state import make_initial_state
from astra.db.models import User
from astra.db.repositories import ProjectRepo, SessionRepo, MemoryChunkRepo
from astra.llm.context import set_llm_override, reset_llm_override
from astra.llm.embeddings import embedding_service
from astra.tasks.agent_tasks import run_agent_task


router = APIRouter()


class RunRequest(BaseModel):
    project_id: UUID
    goal: str = Field(..., min_length=1, max_length=10_000)
    llm_provider: str | None = Field(None, description="local|openrouter|mock")
    llm_model: str | None = Field(None, description="model name, e.g. unsloth/gemma-4-12b-it")
    llm_url: str | None = Field(None, description="Override LLM URL, e.g. http://host.docker.internal:1234/v1")


class RunResponse(BaseModel):
    session_id: UUID
    status: str
    result: str
    steps_taken: int = 0
    job_id: str | None = None
    model_used: str | None = None


@router.post("/run", response_model=RunResponse)
async def run_agent(
    body: RunRequest,
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
):
    project_repo = ProjectRepo(db)
    project = await project_repo.get(body.project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    session_repo = SessionRepo(db)
    agent_session = await session_repo.create(body.project_id, body.goal)
    owner_id = get_safe_owner_id(current_user)
    if owner_id:
        try:
            agent_session.owner_id = owner_id
            await db.flush()
        except Exception:
            pass

    tokens = set_llm_override(provider=body.llm_provider, model=body.llm_model, url=body.llm_url)
    try:
        initial_state = make_initial_state(session_id=agent_session.id, project_id=body.project_id, goal=body.goal)

        try:
            final_state = await agent_graph.ainvoke(initial_state)
            ai_messages = [m for m in final_state["messages"] if getattr(m, "type", None) == "ai" and (m.content or "").strip()]
            if not ai_messages:
                ai_messages = [m for m in final_state["messages"] if getattr(m, "type", None) == "ai"]
            result = ai_messages[-1].content if ai_messages else "Agent completed but returned empty result."
            steps_taken = len(final_state.get("completed_steps", []))
            status = "halted" if final_state.get("is_halted") else "completed"
            if status == "halted":
                result = f"[Circuit breaker] Halted after {steps_taken} steps.\n\n{result}"

            try:
                vector = None
                try:
                    vector = await embedding_service.embed(f"Goal: {body.goal}\nResult: {result[:2000]}")
                except Exception:
                    pass
                meta = json.dumps({"session_id": str(agent_session.id), "type": "session_result"}, ensure_ascii=False)
                repo = MemoryChunkRepo(db)
                await repo.add(body.project_id, f"Goal: {body.goal}\nResult: {result[:2000]}", vector, meta)
            except Exception as exc:
                logger.warning("Memory store failed: {}", exc)

        except Exception as exc:
            logger.exception("Agent execution failed")
            result = f"Agent halted with error: {exc}"
            status = "error"
            steps_taken = 0

        await session_repo.finish(agent_session.id, result, status)
    finally:
        reset_llm_override(tokens)

    return RunResponse(
        session_id=agent_session.id,
        status=status,
        result=result,
        steps_taken=steps_taken,
        model_used=body.llm_model,
    )


@router.post("/run/async", response_model=RunResponse)
async def run_agent_async(
    body: RunRequest,
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
):
    project_repo = ProjectRepo(db)
    project = await project_repo.get(body.project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    session_repo = SessionRepo(db)
    agent_session = await session_repo.create(body.project_id, body.goal)
    owner_id = get_safe_owner_id(current_user)
    if owner_id:
        try:
            agent_session.owner_id = owner_id
            await db.flush()
        except Exception:
            pass

    try:
        kicker = await run_agent_task.kiq(str(agent_session.id), str(body.project_id), body.goal)
        job_id = getattr(kicker, "task_id", str(agent_session.id))
        agent_session.job_id = job_id
        await db.flush()
        logger.info("Enqueued async job {} for session {}", job_id, agent_session.id)
    except Exception as exc:
        logger.warning("TaskIQ enqueue failed ({}), falling back", exc)
        job_id = str(agent_session.id)

    return RunResponse(
        session_id=agent_session.id,
        status="queued",
        result="Job queued in TaskIQ, poll /api/agents/jobs/{session_id}",
        steps_taken=0,
        job_id=job_id,
        model_used=body.llm_model,
    )


@router.get("/jobs/{session_id}")
async def get_job_status(
    session_id: UUID,
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy import select
    from astra.db.models import AgentSession

    result = await db.execute(select(AgentSession).where(AgentSession.id == session_id))
    sess = result.scalar_one_or_none()
    if not sess:
        raise HTTPException(404, "Session not found")

    return {
        "id": str(sess.id),
        "project_id": str(sess.project_id),
        "job_id": sess.job_id,
        "status": sess.status,
        "goal": sess.goal,
        "result": sess.result,
        "steps_completed": sess.steps_completed,
        "created_at": sess.created_at.isoformat() if sess.created_at else "",
        "finished_at": sess.finished_at.isoformat() if sess.finished_at else "",
    }


@router.post("/run/stream")
async def run_agent_stream(
    body: RunRequest,
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
):
    project_repo = ProjectRepo(db)
    project = await project_repo.get(body.project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    session_repo = SessionRepo(db)
    agent_session = await session_repo.create(body.project_id, body.goal)
    owner_id = get_safe_owner_id(current_user)
    if owner_id:
        try:
            agent_session.owner_id = owner_id
            await db.flush()
        except Exception:
            pass

    provider_ov = body.llm_provider
    model_ov = body.llm_model
    url_ov = body.llm_url

    async def event_generator():
        final_result = ""
        final_status = "completed"
        tokens = set_llm_override(provider=provider_ov, model=model_ov, url=url_ov)
        try:
            async for ev in stream_agent(session_id=agent_session.id, project_id=body.project_id, goal=body.goal):
                event_type = ev.get("event", "message")
                data = ev.get("data", {})
                if event_type == "done":
                    final_result = data.get("result", "")
                    final_status = data.get("status", "completed")
                yield {"event": event_type, "data": json.dumps(data, ensure_ascii=False)}
                await asyncio.sleep(0.01)
        except Exception as exc:
            logger.exception("SSE streaming failed")
            yield {"event": "error", "data": json.dumps({"error": str(exc)}, ensure_ascii=False)}
            final_result = f"Error: {exc}"
            final_status = "error"
        finally:
            reset_llm_override(tokens)
            try:
                from astra.db.engine import get_session

                async with get_session() as new_db:
                    new_repo = SessionRepo(new_db)
                    await new_repo.finish(agent_session.id, final_result, final_status)
                    try:
                        vector = None
                        try:
                            vector = await embedding_service.embed(f"Goal: {body.goal}\nResult: {final_result[:2000]}")
                        except Exception:
                            pass
                        meta = json.dumps({"session_id": str(agent_session.id), "type": "session_result_stream"}, ensure_ascii=False)
                        mem_repo = MemoryChunkRepo(new_db)
                        await mem_repo.add(body.project_id, f"Goal: {body.goal}\nResult: {final_result[:2000]}", vector, meta)
                    except Exception as exc:
                        logger.warning("Memory store after stream failed: {}", exc)
            except Exception as exc:
                logger.error("Failed to persist streamed result: {}", exc)

    return EventSourceResponse(event_generator())


@router.get("/sessions/{session_id}")
async def get_session(
    session_id: UUID,
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
):
    from sqlalchemy import select
    from astra.db.models import AgentSession

    result = await db.execute(select(AgentSession).where(AgentSession.id == session_id))
    sess = result.scalar_one_or_none()
    if not sess:
        raise HTTPException(404, "Session not found")
    return {
        "id": str(sess.id),
        "project_id": str(sess.project_id),
        "goal": sess.goal,
        "status": sess.status,
        "result": sess.result,
        "steps_completed": sess.steps_completed,
        "created_at": sess.created_at.isoformat() if sess.created_at else "",
        "finished_at": sess.finished_at.isoformat() if sess.finished_at else "",
    }
