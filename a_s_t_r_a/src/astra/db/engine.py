"""Async SQLAlchemy engine and session factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from astra.config import settings

engine = create_async_engine(
    settings.database_url,
    echo=settings.environment == "development",
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Run startup: enable pgvector extension, create tables in dev."""
    from astra.db.models import Base  # noqa: F811

    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        if settings.environment == "development":
            await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured")


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    """Yield an async session inside a transaction context."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
