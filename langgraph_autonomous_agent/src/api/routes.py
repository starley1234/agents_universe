"""REST + WebSocket API routes."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.schemas import (
    KnowledgeAdd, MemSearchReq, MemSearchRes, OntNodeOut,
    Stats, TaskBrief, TaskCreate, TaskList, TaskOut,
)
from src.db.engine import get_db
from src.db.models import Task
from src.db.repository import (
    active_tasks, create_task, get_task, list_tasks, status_counts, update_task,
)
from src.reporting.ws_manager import ws

router = APIRouter()


# ─── Tasks ──────────────────────────────────────────────────────────────
@router.post("/tasks", response_model=TaskOut, status_code=201)
async def create(body: TaskCreate, db: AsyncSession = Depends(get_db)):
    t = await create_task(db, body.title, body.description,
                          report_email=body.report_email,
                          notify_telegram=body.notify_telegram,
                          max_iterations=body.max_iterations)
    t.priority = 0
    await db.flush()
    import asyncio
    from src.agent.runner import run_task
    asyncio.create_task(run_task(str(t.id)))
    return t


@router.get("/tasks", response_model=TaskList)
async def list_(status: Optional[str] = None, limit: int = 50,
                offset: int = 0, db: AsyncSession = Depends(get_db)):
    ts = await list_tasks(db, status=status, limit=limit, offset=offset)
    return TaskList(tasks=list(ts), total=len(ts))


@router.get("/tasks/active", response_model=list[TaskBrief])
async def active(db: AsyncSession = Depends(get_db)):
    return await active_tasks(db)


@router.get("/tasks/{tid}", response_model=TaskOut)
async def detail(tid: uuid.UUID, db: AsyncSession = Depends(get_db)):
    t = await get_task(db, tid)
    if not t:
        raise HTTPException(404, "Not found")
    return t


@router.delete("/tasks/{tid}", status_code=204)
async def cancel(tid: uuid.UUID, db: AsyncSession = Depends(get_db)):
    t = await get_task(db, tid)
    if not t:
        raise HTTPException(404, "Not found")
    if t.status in ("completed", "failed", "cancelled"):
        raise HTTPException(400, f"Already {t.status}")
    await update_task(db, tid, status="cancelled", completed_at=datetime.now(timezone.utc))


# ─── Dashboard ──────────────────────────────────────────────────────────
@router.get("/dashboard/stats", response_model=Stats)
async def stats(db: AsyncSession = Depends(get_db)):
    c = await status_counts(db)
    avg = (await db.execute(select(func.avg(Task.quality)).where(Task.status == "completed"))).scalar() or 0
    return Stats(total=sum(c.values()), active=c.get("running", 0) + c.get("pending", 0),
                 completed=c.get("completed", 0), failed=c.get("failed", 0),
                 pending=c.get("pending", 0), avg_quality=float(avg))


# ─── Memory ─────────────────────────────────────────────────────────────
@router.post("/memory/search", response_model=list[MemSearchRes])
async def mem_search(body: MemSearchReq, db: AsyncSession = Depends(get_db)):
    from src.memory.combined_rag import CombinedRAG
    return await CombinedRAG(db).retrieve(body.query, top_k=body.top_k, source_type=body.source_type)


@router.post("/memory/knowledge", status_code=201)
async def add_knowledge(body: KnowledgeAdd, db: AsyncSession = Depends(get_db)):
    from src.db.repository import store_knowledge, upsert_concept
    node = await upsert_concept(db, body.concept, category=body.category)
    entry = await store_knowledge(db, body.text, node_id=node.id,
                                  source=body.source, confidence=body.confidence)
    return {"id": str(entry.id), "node_id": str(node.id)}


@router.get("/ontology/concepts", response_model=list[OntNodeOut])
async def concepts(query: Optional[str] = None, limit: int = 50,
                   db: AsyncSession = Depends(get_db)):
    from src.db.models import OntNode
    from src.db.repository import search_concepts
    if query:
        return await search_concepts(db, query, limit=limit)
    r = await db.execute(select(OntNode).order_by(OntNode.visits.desc()).limit(limit))
    return list(r.scalars().all())


# ─── Agent status ───────────────────────────────────────────────────────
@router.get("/agent/status")
async def agent_status(db: AsyncSession = Depends(get_db)):
    return {"status": "running", "active_tasks": len(await active_tasks(db)),
            "ws_connections": ws.connections}


# ─── MCP cache reset ───────────────────────────────────────────────────
@router.post("/mcp/reset")
async def mcp_reset():
    from src.mcp.manager import reset_cache
    reset_cache()
    return {"status": "ok"}


# ─── WebSocket ──────────────────────────────────────────────────────────
@router.websocket("/ws/dashboard")
async def ws_dash(websocket: WebSocket):
    await ws.connect_global(websocket)
    try:
        while True:
            d = await websocket.receive_text()
            if d == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        ws.disconnect_global(websocket)


@router.websocket("/ws/tasks/{task_id}")
async def ws_task(websocket: WebSocket, task_id: str):
    await ws.connect_task(task_id, websocket)
    try:
        while True:
            d = await websocket.receive_text()
            if d == "ping":
                await websocket.send_text("pong")
    except WebSocketDisconnect:
        ws.disconnect_task(task_id, websocket)
