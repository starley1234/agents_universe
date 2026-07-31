"""Web UI routes — serves Jinja2 HTML pages + JSON API for HTMX/Alpine."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from astra.api.deps import db_session
from astra.db.repositories import ProjectRepo, SessionRepo
from astra.memory.ontology import ontology_store
from astra.mcp.tool_registry import tool_registry

_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

router = APIRouter()


# ──────────────────────────────────────────────────────────────
# HTML Pages
# ──────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    return templates.TemplateResponse(request, "index.html")


@router.get("/ui/projects", response_class=HTMLResponse)
async def page_projects(request: Request):
    return templates.TemplateResponse(request, "projects.html")


@router.get("/ui/projects/{project_id}", response_class=HTMLResponse)
async def page_project_detail(request: Request, project_id: UUID,
                              db: AsyncSession = Depends(db_session)):
    repo = ProjectRepo(db)
    project = await repo.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return templates.TemplateResponse(request, "project_detail.html",
                                      {"project": project})


@router.get("/ui/projects/{project_id}/playground", response_class=HTMLResponse)
async def page_playground(request: Request, project_id: UUID,
                          db: AsyncSession = Depends(db_session)):
    repo = ProjectRepo(db)
    project = await repo.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return templates.TemplateResponse(request, "playground.html",
                                      {"project": project})


@router.get("/ui/projects/{project_id}/graph", response_class=HTMLResponse)
async def page_graph(request: Request, project_id: UUID,
                     db: AsyncSession = Depends(db_session)):
    repo = ProjectRepo(db)
    project = await repo.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return templates.TemplateResponse(request, "graph.html",
                                      {"project": project})


# ──────────────────────────────────────────────────────────────
# JSON API for the frontend
# ──────────────────────────────────────────────────────────────

@router.get("/api/stats")
async def api_stats(db: AsyncSession = Depends(db_session)) -> dict[str, Any]:
    from sqlalchemy import func, select
    from astra.db.models import Project, AgentSession, MemoryChunk

    projects = (await db.execute(select(func.count(Project.id)))).scalar() or 0
    sessions = (await db.execute(select(func.count(AgentSession.id)))).scalar() or 0
    active = (await db.execute(
        select(func.count(AgentSession.id)).where(AgentSession.status == "running")
    )).scalar() or 0
    chunks = (await db.execute(select(func.count(MemoryChunk.id)))).scalar() or 0
    mcp_tools = len(await tool_registry.get_tools_for_project(
        UUID(int=0)  # global tools only
    ))

    return {
        "projects": projects,
        "sessions": sessions,
        "active_sessions": active,
        "memory_chunks": chunks,
        "mcp_tools": mcp_tools,
    }


@router.get("/api/data/projects")
async def api_projects(db: AsyncSession = Depends(db_session)) -> list[dict]:
    """Projects list with extra fields for the frontend."""
    from sqlalchemy import func, select
    from astra.db.models import AgentSession

    repo = ProjectRepo(db)
    projects = await repo.list_all()
    result: list[dict] = []
    for p in projects:
        session_count = (
            await db.execute(
                select(func.count(AgentSession.id)).where(AgentSession.project_id == p.id)
            )
        ).scalar() or 0
        result.append({
            "id": str(p.id),
            "name": p.name,
            "description": p.description or "",
            "created_at": p.created_at.isoformat() if p.created_at else "",
            "session_count": session_count,
        })
    return result


@router.get("/api/projects/{project_id}/sessions")
async def api_sessions(project_id: UUID,
                       db: AsyncSession = Depends(db_session)) -> list[dict]:
    from sqlalchemy import select
    from astra.db.models import AgentSession

    result = await db.execute(
        select(AgentSession)
        .where(AgentSession.project_id == project_id)
        .order_by(AgentSession.created_at.desc())
        .limit(50)
    )
    sessions = result.scalars().all()
    return [
        {
            "id": str(s.id),
            "goal": s.goal,
            "status": s.status,
            "result": s.result or "",
            "steps_completed": s.steps_completed,
            "created_at": s.created_at.isoformat() if s.created_at else "",
            "finished_at": s.finished_at.isoformat() if s.finished_at else "",
        }
        for s in sessions
    ]


@router.get("/api/projects/{project_id}/graph")
async def api_graph(project_id: UUID) -> dict[str, Any]:
    g = ontology_store._get_graph(project_id)
    nodes = [
        {"id": n, "label": n, "group": g.nodes[n].get("type", "concept")}
        for n in g.nodes
    ]
    edges = [
        {"from": u, "to": v, "label": g.edges[u, v].get("relation", "")}
        for u, v in g.edges
    ]
    return {"nodes": nodes, "edges": edges}


@router.get("/api/mcp/servers")
async def api_mcp_servers() -> list[dict]:
    result: list[dict] = []
    for name, client in tool_registry._clients.items():
        result.append({
            "name": name,
            "url": client.server_url,
            "connected": client._session is not None,
            "tools": [
                {"name": t["name"], "description": t.get("description", "")}
                for t in client._tools
            ],
        })
    return result
