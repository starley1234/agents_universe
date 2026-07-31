"""Agent execution endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from astra.api.deps import db_session
from astra.core.agent import agent_graph
from astra.core.state import make_initial_state
from astra.db.repositories import ProjectRepo, SessionRepo

router = APIRouter()


class RunRequest(BaseModel):
    project_id: UUID
    goal: str = Field(..., min_length=1, max_length=10_000)


class RunResponse(BaseModel):
    session_id: UUID
    status: str
    result: str


@router.post("/run", response_model=RunResponse)
async def run_agent(body: RunRequest, db: AsyncSession = Depends(db_session)):
    """Start an agent session for the given project and goal."""
    # Verify project exists
    project_repo = ProjectRepo(db)
    project = await project_repo.get(body.project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    # Create session record
    session_repo = SessionRepo(db)
    agent_session = await session_repo.create(body.project_id, body.goal)

    # Build initial state as a plain dict (required by LangGraph TypedDict)
    initial_state = make_initial_state(
        session_id=agent_session.id,
        project_id=body.project_id,
        goal=body.goal,
    )

    # Run the agent graph
    try:
        final_state = await agent_graph.ainvoke(initial_state)

        # Extract final result from the last AI message
        ai_messages = [m for m in final_state["messages"] if m.type == "ai"]
        result = ai_messages[-1].content if ai_messages else "Agent completed."

        if final_state.get("is_halted"):
            status = "halted"
            result = f"[Circuit breaker] {result}"
        else:
            status = "completed"

    except Exception as exc:
        result = f"Agent halted with error: {exc}"
        status = "error"

    # Persist result
    await session_repo.finish(agent_session.id, result, status)

    return RunResponse(
        session_id=agent_session.id,
        status=status,
        result=result,
    )
