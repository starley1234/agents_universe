"""FastAPI dependency injection helpers."""

from __future__ import annotations

from typing import AsyncIterator

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from astra.db.engine import get_session


async def db_session() -> AsyncIterator[AsyncSession]:
    async with get_session() as session:
        yield session
