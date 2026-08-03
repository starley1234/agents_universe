"""Web UI routes — Postgres only, JWT, streaming, FalkorDB aware."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from astra.api.deps import db_session
from astra.config import settings
from astra.db.repositories import ProjectRepo
from astra.memory.ontology import ontology_store
from astra.mcp.tool_registry import tool_registry
from astra.prompts.registry import prompt_registry
from astra import __version__

_templates_dir = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))

router = APIRouter()


def _render(request: Request, name: str, context: dict | None = None):
    ctx = dict(context or {})
    ctx.setdefault("version", __version__)
    try:
        return templates.TemplateResponse(request, name, ctx)
    except TypeError:
        return templates.TemplateResponse(name, {"request": request, **ctx})


# HTML Pages
@router.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request):
    return _render(request, "index.html")


@router.get("/login", response_class=HTMLResponse)
async def page_login(request: Request):
    return _render(request, "login.html")


@router.get("/ui/projects", response_class=HTMLResponse)
async def page_projects(request: Request):
    return _render(request, "projects.html")


@router.get("/ui/projects/{project_id}", response_class=HTMLResponse)
async def page_project_detail(request: Request, project_id: UUID, db: AsyncSession = Depends(db_session)):
    repo = ProjectRepo(db)
    project = await repo.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return _render(request, "project_detail.html", {"project": project})


@router.get("/ui/projects/{project_id}/playground", response_class=HTMLResponse)
async def page_playground(request: Request, project_id: UUID, db: AsyncSession = Depends(db_session)):
    repo = ProjectRepo(db)
    project = await repo.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return _render(request, "playground.html", {"project": project})


@router.get("/ui/projects/{project_id}/graph", response_class=HTMLResponse)
async def page_graph(request: Request, project_id: UUID, db: AsyncSession = Depends(db_session)):
    repo = ProjectRepo(db)
    project = await repo.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    stats = ontology_store.stats(project_id)
    return _render(request, "graph.html", {"project": project, "graph_stats": stats})


@router.get("/ui/projects/{project_id}/memory", response_class=HTMLResponse)
async def page_memory(request: Request, project_id: UUID, db: AsyncSession = Depends(db_session)):
    repo = ProjectRepo(db)
    project = await repo.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return _render(request, "memory.html", {"project": project})


@router.get("/ui/settings", response_class=HTMLResponse)
async def page_settings(request: Request):
    return _render(request, "settings.html", {"settings": settings})


@router.get("/ui/mcp", response_class=HTMLResponse)
async def page_mcp(request: Request):
    return _render(request, "mcp.html")


@router.get("/ui/prompts", response_class=HTMLResponse)
async def page_prompts(request: Request):
    prompts = prompt_registry.list_prompts()
    details = []
    for name in prompts:
        data = prompt_registry.get_with_metadata(name)
        details.append({"name": name, "data": data})
    return _render(request, "prompts.html", {"prompts": details})


@router.get("/ui/eval", response_class=HTMLResponse)
async def page_eval(request: Request):
    return _render(request, "eval.html")


# JSON API
@router.get("/api/stats")
async def api_stats(db: AsyncSession = Depends(db_session)) -> dict[str, Any]:
    from sqlalchemy import func, select
    from astra.db.models import Project, AgentSession, MemoryChunk, User

    try:
        projects = (await db.execute(select(func.count(Project.id)))).scalar() or 0
        sessions = (await db.execute(select(func.count(AgentSession.id)))).scalar() or 0
        active = (await db.execute(select(func.count(AgentSession.id)).where(AgentSession.status == "running"))).scalar() or 0
        chunks = (await db.execute(select(func.count(MemoryChunk.id)))).scalar() or 0
        users = (await db.execute(select(func.count(User.id)))).scalar() or 0
    except Exception:
        projects = sessions = active = chunks = users = 0

    try:
        tools = await tool_registry.get_tools_for_project(UUID(int=0))
        mcp_tools = len(tools)
    except Exception:
        mcp_tools = 0

    return {
        "projects": projects,
        "sessions": sessions,
        "active_sessions": active,
        "memory_chunks": chunks,
        "mcp_tools": mcp_tools,
        "users": users,
        "environment": settings.environment.value,
        "llm_provider": settings.llm_default_provider.value,
        "llm_model": settings.active_llm_model,
        "auth_enabled": settings.auth_enabled,
        "falkordb_enabled": settings.use_falkordb,
        "langfuse_enabled": settings.langfuse_enabled,
    }


@router.get("/api/data/projects")
async def api_projects(db: AsyncSession = Depends(db_session)) -> list[dict]:
    from sqlalchemy import func, select
    from astra.db.models import AgentSession

    try:
        repo = ProjectRepo(db)
        projects = await repo.list_all()
    except Exception:
        return []

    result: list[dict] = []
    for p in projects:
        try:
            session_count = (await db.execute(select(func.count(AgentSession.id)).where(AgentSession.project_id == p.id))).scalar() or 0
        except Exception:
            session_count = 0
        result.append(
            {
                "id": str(p.id),
                "name": p.name,
                "description": p.description or "",
                "created_at": p.created_at.isoformat() if p.created_at else "",
                "updated_at": p.updated_at.isoformat() if getattr(p, "updated_at", None) else "",
                "session_count": session_count,
            }
        )
    return result


@router.get("/api/projects/{project_id}/sessions")
async def api_sessions(project_id: UUID, db: AsyncSession = Depends(db_session)) -> list[dict]:
    from sqlalchemy import select
    from astra.db.models import AgentSession

    try:
        result = await db.execute(
            select(AgentSession).where(AgentSession.project_id == project_id).order_by(AgentSession.created_at.desc()).limit(50)
        )
        sessions = result.scalars().all()
    except Exception:
        sessions = []

    return [
        {
            "id": str(s.id),
            "goal": s.goal,
            "status": s.status,
            "result": s.result or "",
            "steps_completed": s.steps_completed,
            "job_id": s.job_id,
            "created_at": s.created_at.isoformat() if s.created_at else "",
            "finished_at": s.finished_at.isoformat() if s.finished_at else "",
        }
        for s in sessions
    ]


@router.get("/api/projects/{project_id}/memory")
async def api_memory(project_id: UUID) -> list[dict]:
    from astra.memory.semantic import semantic_memory

    try:
        return await semantic_memory.list_recent(project_id, limit=100)
    except Exception as exc:
        return [{"error": str(exc)}]


@router.get("/api/projects/{project_id}/graph")
async def api_graph(project_id: UUID) -> dict[str, Any]:
    try:
        data = ontology_store.get_full_graph_data(project_id)
        return data
    except Exception as exc:
        return {"nodes": [], "edges": [], "error": str(exc)}


@router.get("/api/config")
async def api_config() -> dict[str, Any]:
    return {
        "project_name": settings.project_name,
        "environment": settings.environment.value,
        "llm_provider": settings.llm_default_provider.value,
        "llm_model": settings.active_llm_model,
        "embedding_model": settings.embedding_model,
        "embedding_dimensions": settings.embedding_dimensions,
        "workspace_path": str(settings.workspace_path),
        "database": "postgres+pgvector",
        "auth_enabled": settings.auth_enabled,
        "falkordb_enabled": settings.use_falkordb,
        "langfuse_enabled": settings.langfuse_enabled,
        "mcp_servers": {
            "search": bool(settings.mcp_search_url),
            "image_gen": bool(settings.mcp_image_gen_url),
            "tts": bool(settings.mcp_tts_url),
        },
        "prompts": prompt_registry.list_prompts(),
    }


@router.get("/api/health/full")
async def api_health_full(db: AsyncSession = Depends(db_session)) -> dict[str, Any]:
    from sqlalchemy import text

    try:
        await db.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as exc:
        db_status = f"error: {exc}"

    mcp_status = []
    try:
        for name, client in tool_registry._clients.items():
            mcp_status.append({"name": name, "connected": getattr(client, "_session", None) is not None})
    except Exception:
        pass

    # FalkorDB check
    falkor_status = "disabled"
    if settings.use_falkordb:
        try:
            from astra.memory.falkor_store import falkor_store

            falkor_status = "connected" if falkor_store._client else "disconnected"
        except Exception as exc:
            falkor_status = f"error: {exc}"

    # Langfuse check
    langfuse_status = "disabled"
    if settings.langfuse_enabled:
        try:
            from astra.llm.tracing.langfuse import get_langfuse_client

            client = get_langfuse_client()
            langfuse_status = "connected" if client else "disconnected"
        except Exception as exc:
            langfuse_status = f"error: {exc}"

    return {
        "version": __version__,
        "env": settings.environment.value,
        "db": db_status,
        "db_type": "postgres+pgvector",
        "llm": {"provider": settings.llm_default_provider.value, "model": settings.active_llm_model, "url": settings.active_llm_url},
        "mcp": mcp_status,
        "falkordb": falkor_status,
        "langfuse": langfuse_status,
        "auth": "enabled" if settings.auth_enabled else "disabled",
        "workspace": str(settings.resolved_workspace),
        "workspace_exists": settings.resolved_workspace.exists(),
    }


@router.get("/api/prompts")
async def api_prompts() -> list[dict]:
    result = []
    for name in prompt_registry.list_prompts():
        data = prompt_registry.get_with_metadata(name)
        result.append({"name": name, "content": data.get("system") or data.get("prompt") or "", "metadata": data.get("metadata", {})})
    return result
