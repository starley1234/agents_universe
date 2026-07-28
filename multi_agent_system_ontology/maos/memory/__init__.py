"""Хранилище MAOS: PostgreSQL + pgvector (обязательное, см. maos/config.py)."""
from __future__ import annotations

from .store import Store, StoreError

__all__ = ["Store", "StoreError"]
