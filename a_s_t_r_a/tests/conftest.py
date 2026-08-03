"""Shared test fixtures."""

from __future__ import annotations

import os

# Force test environment BEFORE any astra imports
os.environ["ENVIRONMENT"] = "development"
os.environ["DATABASE_URL"] = "sqlite+aiosqlite:///./test.db"
os.environ["LLM_DEFAULT_PROVIDER"] = "mock"
os.environ["LOCAL_LLM_URL"] = "http://localhost:9999/v1"
os.environ["LOCAL_LLM_API_KEY"] = "test-key"
os.environ["EMBEDDING_URL"] = "http://localhost:9999/v1"
os.environ["EMBEDDING_KEY"] = "test-key"

import asyncio
import pytest


@pytest.fixture(autouse=True, scope="session")
def _init_db():
    """Create all tables once for the test session."""
    from astra.db.engine import engine
    from astra.db.models import Base

    async def setup():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(setup())
    yield
    # Cleanup: remove the test db file
    if os.path.exists("test.db"):
        try:
            os.remove("test.db")
        except Exception:
            pass


@pytest.fixture
def anyio_backend():
    return "asyncio"
