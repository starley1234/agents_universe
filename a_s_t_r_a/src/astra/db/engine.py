"""Async engine — Postgres prod, SQLite fallback for tests (not advertised)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator
from pathlib import Path

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from astra.config import settings

# Allow SQLite for tests (ENV var contains sqlite) otherwise Postgres
_is_sqlite = "sqlite" in settings.database_url.lower()

if _is_sqlite:
    # Test mode — SQLite
    db_file = settings.database_url.split("///")[-1].split("?")[0]
    if db_file and db_file != ":memory:":
        p = Path(db_file)
        if not p.is_absolute():
            p = Path.cwd() / p
        p.parent.mkdir(parents=True, exist_ok=True)
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        connect_args={"check_same_thread": False},
    )
else:
    # Production — Postgres + pgvector
    engine = create_async_engine(
        settings.database_url,
        echo=settings.environment.value == "development",
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
    )

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    from astra.db.models import Base

    async with engine.begin() as conn:
        if not _is_sqlite:
            try:
                await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            except Exception:
                pass
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables ensured (dialect={})", "sqlite" if _is_sqlite else "postgres")


@asynccontextmanager
async def get_session() -> AsyncIterator[AsyncSession]:
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
