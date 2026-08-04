"""Web UI Routes for NexusTwin MDM & Certification."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.engine import get_session
from src.db.repository import MDMRepository

router = APIRouter(tags=["Web UI"])

_templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(_templates_dir))


@router.get("/", response_class=HTMLResponse)
async def page_dashboard(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render main Dashboard page."""
    repo = MDMRepository(session)
    objects = await repo.list_objects(limit=10)
    org_units = await repo.get_org_units()
    sources = await repo.get_sources()
    types = await repo.get_types()

    # Calculate compliance for first 5 objects
    compliance_ok = 0
    total_checked = 0
    for obj in objects[:5]:
        total_checked += 1
        chain = await repo.verify_baseline_chain(obj["id"])
        if all(b["status"] == "OK" for b in chain):
            compliance_ok += 1
    compliance_rate = int((compliance_ok / max(1, total_checked)) * 100)

    context = {
        "request": request,
        "page_title": "Дашборд Холдинга — NexusTwin MDM",
        "active_tab": "dashboard",
        "stats": {
            "objects_count": len(objects),
            "org_count": len(org_units),
            "sources_count": len(sources),
            "types_count": len(types),
            "compliance_rate": compliance_rate,
        },
        "recent_objects": objects,
        "llm_provider": settings.llm_active_provider,
        "db_dialect": "sqlite" if settings.is_sqlite else "postgresql",
    }
    return templates.TemplateResponse(request=request, name="dashboard.html", context=context)


@router.get("/ui/objects", response_class=HTMLResponse)
async def page_objects(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render Objects & Digital Twin Explorer page."""
    repo = MDMRepository(session)
    objects = await repo.list_objects(limit=100)
    types = await repo.get_types()
    orgs = await repo.get_org_units()

    context = {
        "request": request,
        "page_title": "Реестр Цифровых Двойников — NexusTwin MDM",
        "active_tab": "objects",
        "objects": objects,
        "types": types,
        "org_units": orgs,
    }
    return templates.TemplateResponse(request=request, name="objects.html", context=context)


@router.get("/ui/objects/{object_id}", response_class=HTMLResponse)
async def page_object_detail(
    request: Request,
    object_id: str,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render Object Detail page with EAV properties, EBOM tree, and Baseline hash verification."""
    repo = MDMRepository(session)
    detail = await repo.get_object_detail(object_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Object not found")

    bom = await repo.get_bom_tree(object_id)
    chain = await repo.verify_baseline_chain(object_id)
    sources = await repo.get_sources()

    context = {
        "request": request,
        "page_title": f"Объект {detail['master_code']} — NexusTwin MDM",
        "active_tab": "objects",
        "obj": detail,
        "bom_items": bom,
        "baseline_chain": chain,
        "sources": sources,
    }
    return templates.TemplateResponse(request=request, name="object_detail.html", context=context)


@router.get("/ui/agent", response_class=HTMLResponse)
async def page_agent_playground(request: Request) -> HTMLResponse:
    """Render LangGraph AI Agent Playground page."""
    context = {
        "request": request,
        "page_title": "Агент NexusTwin (LangGraph) — AI Playground",
        "active_tab": "agent",
        "provider": settings.llm_active_provider,
    }
    return templates.TemplateResponse(request=request, name="agent_playground.html", context=context)


@router.get("/ui/ontology", response_class=HTMLResponse)
async def page_ontology(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> HTMLResponse:
    """Render NSI reference dictionaries page (org units, types, UOM, sources)."""
    repo = MDMRepository(session)
    context = {
        "request": request,
        "page_title": "Справочники НСИ и Онтология — NexusTwin MDM",
        "active_tab": "ontology",
        "org_units": await repo.get_org_units(),
        "types": await repo.get_types(),
        "sources": await repo.get_sources(),
        "uom": await repo.get_uom(),
    }
    return templates.TemplateResponse(request=request, name="ontology.html", context=context)


@router.get("/ui/duplicates", response_class=HTMLResponse)
async def page_duplicates(request: Request) -> HTMLResponse:
    """Render MDM Deduplication & Merge page."""
    context = {
        "request": request,
        "page_title": "Анализ Дубликатов и Слияние — NexusTwin MDM",
        "active_tab": "duplicates",
    }
    return templates.TemplateResponse(request=request, name="duplicates.html", context=context)


@router.get("/ui/generator", response_class=HTMLResponse)
async def page_generator(request: Request) -> HTMLResponse:
    """Render LLM Synthetic Testing Mode — Enterprise Generator page."""
    context = {
        "request": request,
        "page_title": "Синтез Вымышленного Предприятия — NexusTwin MDM",
        "active_tab": "generator",
    }
    return templates.TemplateResponse(request=request, name="generator.html", context=context)


@router.get("/ui/mcp", response_class=HTMLResponse)
async def page_mcp_docs(request: Request) -> HTMLResponse:
    """Render MCP Server Docs and interactive test bench page."""
    from src.mcp.server import mcp_server

    context = {
        "request": request,
        "page_title": "MCP Server (Model Context Protocol) — NexusTwin MDM",
        "active_tab": "mcp",
        "tools": mcp_server.list_tools(),
    }
    return templates.TemplateResponse(request=request, name="mcp_docs.html", context=context)
