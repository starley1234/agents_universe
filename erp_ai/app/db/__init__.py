"""Хранилище ERP AI: PostgreSQL (см. app/db/store.py)."""
from __future__ import annotations

from .store import Store, StoreError

__all__ = ["Store", "StoreError"]
