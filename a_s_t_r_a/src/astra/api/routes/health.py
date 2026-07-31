"""Health-check endpoint — liveness + readiness probes."""

from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from astra.db.engine import engine

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    """Liveness probe — always returns 200 if the process is up."""
    return {"status": "ok", "service": "astra"}


@router.get("/health/ready")
async def readiness() -> dict:
    """Readiness probe — checks database connectivity."""
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ready", "db": "ok"}
    except Exception as exc:
        return {"status": "not_ready", "db": str(exc)}
