"""Shared test fixtures."""

from __future__ import annotations

import os

# Force test environment before any astra imports
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///test.db")
os.environ.setdefault("LOCAL_LLM_URL", "http://localhost:9999/v1")
os.environ.setdefault("LOCAL_LLM_API_KEY", "test-key")
os.environ.setdefault("EMBEDDING_URL", "http://localhost:9999/v1")
os.environ.setdefault("EMBEDDING_KEY", "test-key")

import pytest


@pytest.fixture
def anyio_backend():
    return "asyncio"
