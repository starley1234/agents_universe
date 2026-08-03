"""Async SQLAlchemy engine & session factory."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import get_settings

log = logging.getLogger(__name__)
_settings = get_settings()

engine = create_async_engine(
    _settings.DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    pool_recycle=300,
)

_session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — one session per request."""
    async with _session_factory() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


@asynccontextmanager
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Context manager for background / non-FastAPI usage."""
    async with _session_factory() as s:
        try:
            yield s
            await s.commit()
        except Exception:
            await s.rollback()
            raise


async def check_db() -> bool:
    try:
        async with engine.connect() as c:
            await c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def init_db() -> None:
    """Enable pgvector extension, create all tables."""
    from src.db.models import Base

    async with engine.begin() as c:
        await c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        await c.run_sync(Base.metadata.create_all)
    log.info("Database initialised (pgvector + tables)")


async def shutdown_db() -> None:
    await engine.dispose()
