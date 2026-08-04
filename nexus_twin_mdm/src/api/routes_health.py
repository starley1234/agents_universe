"""Health check and configuration endpoints."""
from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text

from src.config import settings
from src.db.engine import get_session

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health_check() -> Dict[str, str]:
    """Basic liveness probe."""
    return {"status": "ok", "service": "NexusTwin MDM"}


@router.get("/health/ready")
async def readiness_check(session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    """Readiness check including database liveness."""
    try:
        await session.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as exc:
        db_status = f"error: {exc}"

    return {
        "status": "ready" if db_status == "ok" else "not_ready",
        "database": db_status,
    }


@router.get("/api/health/full")
async def full_health(session: AsyncSession = Depends(get_session)) -> Dict[str, Any]:
    """Detailed health check for Holding MDM & Certification system."""
    try:
        await session.execute(text("SELECT 1"))
        db_status = "ok"
    except Exception as exc:
        db_status = f"error: {exc}"

    return {
        "status": "ok",
        "project_name": settings.project_name,
        "environment": settings.environment,
        "port": settings.app_port,
        "database": {
            "status": db_status,
            "dialect": "sqlite" if settings.is_sqlite else "postgresql",
        },
        "llm_provider": {
            "active_provider": settings.llm_active_provider,
            "model": (
                settings.custom_remote_model
                if settings.llm_active_provider == "custom_remote"
                else settings.openrouter_model
            ),
        },
        "mcp_servers": {
            "search_url": settings.mcp_search_url,
            "agent_toolkit": settings.mcp_agent_toolkit,
        },
        "workspace": str(settings.resolved_workspace_dir),
    }


@router.get("/api/config")
async def public_config() -> Dict[str, Any]:
    """Return public non-sensitive configuration settings."""
    return {
        "project_name": settings.project_name,
        "environment": settings.environment,
        "app_host": settings.app_host,
        "app_port": settings.app_port,
        "llm_active_provider": settings.llm_active_provider,
        "database_dialect": "sqlite" if settings.is_sqlite else "postgresql",
    }
