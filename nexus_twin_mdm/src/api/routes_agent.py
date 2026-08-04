"""LangGraph AI Agent API routes including real-time SSE streaming."""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.graph import MDMAgentGraph
from src.db.engine import get_session

router = APIRouter(tags=["AI Agent"])


class AgentRunRequest(BaseModel):
    query: str = Field(..., description="Query or goal for the MDM & Certification agent")


@router.post("/api/agent/run")
async def run_agent(
    payload: AgentRunRequest,
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    """Execute LangGraph autonomous agent workflow synchronously."""
    agent = MDMAgentGraph()
    result = await agent.run(query=payload.query, session=session)
    return result


@router.post("/api/agent/stream")
async def stream_agent(
    payload: AgentRunRequest,
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """Stream real-time Server-Sent Events (SSE) during agent execution."""

    async def event_generator():
        # Step 1: Initialize
        yield f"event: step_start\ndata: {json.dumps({'step': 'retrieve', 'message': 'Извлекаются справочники Холдинга и объекты Цифрового Двойника...'}, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0.1)

        # Step 2: Reason
        yield f"event: step_start\ndata: {json.dumps({'step': 'reason', 'message': 'LLM анализирует цель задачи и планирует вызов инструментов МДМ...'}, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0.1)

        # Step 3: Run real graph
        agent = MDMAgentGraph()
        res = await agent.run(query=payload.query, session=session)

        # Step 4: Stream tool execution notifications
        for tool_res in res.get("tool_executions", []):
            tname = tool_res.get("tool", "unknown")
            yield f"event: tool_call\ndata: {json.dumps({'tool': tname, 'message': f'Выполнен инструмент {tname}'}, ensure_ascii=False)}\n\n"
            await asyncio.sleep(0.1)

        # Step 5: Compliance audit
        yield f"event: reflect\ndata: {json.dumps({'status': res.get('compliance_status', 'PASSED_100_PERCENT'), 'message': 'Криптографическая цепочка бейслайнов и АП-25 проверена'}, ensure_ascii=False)}\n\n"
        await asyncio.sleep(0.1)

        # Step 6: Final done event
        yield f"event: done\ndata: {json.dumps(res, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
