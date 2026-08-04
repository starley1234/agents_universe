"""Async Database engine supporting PostgreSQL (prod) and SQLite (dev/test fallback)."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from loguru import logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings

_is_sqlite = settings.is_sqlite

if _is_sqlite:
    db_url = settings.database_url
    db_file = db_url.split("///")[-1].split("?")[0]
    if db_file and db_file != ":memory:":
        p = Path(db_file)
        if not p.is_absolute():
            p = Path.cwd() / p
        p.parent.mkdir(parents=True, exist_ok=True)

    engine = create_async_engine(
        db_url,
        echo=False,
        connect_args={"check_same_thread": False},
    )
else:
    db_url = settings.database_url
    if db_url.startswith("postgresql://") and "+asyncpg" not in db_url:
        db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(
        db_url,
        echo=settings.environment == "development",
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True,
    )

async_session_factory = async_sessionmaker(engine, expire_on_commit=False)


async def init_db() -> None:
    """Initialize database tables, partitions, and standard seed data."""
    from src.db.models import Base

    async with engine.begin() as conn:
        if not _is_sqlite:
            # When connected to PostgreSQL, try to run extensions and schema functions
            try:
                await conn.execute(
                    text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";')
                )
                await conn.execute(
                    text("CREATE EXTENSION IF NOT EXISTS ltree;")
                )
                await conn.execute(
                    text("CREATE EXTENSION IF NOT EXISTS btree_gist;")
                )
                await conn.execute(
                    text("CREATE EXTENSION IF NOT EXISTS pgcrypto;")
                )
                await conn.execute(
                    text("CREATE EXTENSION IF NOT EXISTS vector;")
                )
            except Exception as e:
                logger.warning(f"Could not create PostgreSQL extensions: {e}")

        # Create tables
        await conn.run_sync(Base.metadata.create_all)

    # Populate seed data (HOLDING org_unit, sources, admin user, etc.)
    from src.db.init_db import seed_initial_data

    async with async_session_factory() as session:
        await seed_initial_data(session)

    logger.info(
        f"Database initialized successfully (dialect={'sqlite' if _is_sqlite else 'postgres'})"
    )


async def get_session() -> AsyncIterator[AsyncSession]:
    """Provide transactional async session for FastAPI dependency injection."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


@asynccontextmanager
async def get_session_ctx() -> AsyncIterator[AsyncSession]:
    """Provide transactional async session as context manager."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
