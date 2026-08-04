"""State store contract and PostgreSQL JSONB adapter."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS cortex_state (
    namespace text NOT NULL,
    state_key text NOT NULL,
    value jsonb NOT NULL,
    version bigint NOT NULL DEFAULT 1,
    updated_at timestamptz NOT NULL DEFAULT now(),
    updated_by text NOT NULL DEFAULT 'cortex',
    PRIMARY KEY (namespace, state_key)
);
ALTER TABLE cortex_state ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS cortex_state_tenant_policy ON cortex_state;
CREATE POLICY cortex_state_tenant_policy ON cortex_state
    USING (current_setting('app.tenant_id', true) IS NULL
           OR namespace LIKE current_setting('app.tenant_id', true) || '/%')
    WITH CHECK (current_setting('app.tenant_id', true) IS NULL
           OR namespace LIKE current_setting('app.tenant_id', true) || '/%');
"""


class StateStoreUnavailable(RuntimeError):
    pass


class PostgresStateStore:
    """Small JSONB/CAS adapter; psycopg v3 is optional and imported lazily."""

    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        try:
            import psycopg  # type: ignore
        except ImportError:
            self._psycopg = None
        else:
            self._psycopg = psycopg

    @property
    def available(self) -> bool:
        return bool(self.database_url and self._psycopg)

    async def ensure_schema(self) -> None:
        if not self.available:
            raise StateStoreUnavailable("Установите psycopg и задайте DATABASE_URL")
        await asyncio.to_thread(self._execute, SCHEMA_SQL)

    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        if not self.available:
            raise StateStoreUnavailable("PostgreSQL adapter недоступен")
        with self._psycopg.connect(self.database_url) as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                try:
                    rows = cursor.fetchall()
                except Exception:
                    rows = []
            connection.commit()
        return rows

    async def get(self, namespace: str, key: str, default: Any = None) -> Any:
        rows = await asyncio.to_thread(self._execute, "SELECT value FROM cortex_state WHERE namespace=%s AND state_key=%s", (namespace, key))
        return rows[0][0] if rows else default

    async def put(self, namespace: str, key: str, value: Any, *, expected_version: int | None = None, updated_by: str = "cortex") -> int:
        if expected_version is None:
            sql = """INSERT INTO cortex_state(namespace,state_key,value,updated_by) VALUES(%s,%s,%s::jsonb,%s)
                     ON CONFLICT(namespace,state_key) DO UPDATE SET value=EXCLUDED.value, version=cortex_state.version+1, updated_at=now(), updated_by=EXCLUDED.updated_by
                     RETURNING version"""
            rows = await asyncio.to_thread(self._execute, sql, (namespace, key, json.dumps(value), updated_by))
        else:
            sql = """UPDATE cortex_state SET value=%s::jsonb, version=version+1, updated_at=now(), updated_by=%s
                     WHERE namespace=%s AND state_key=%s AND version=%s RETURNING version"""
            rows = await asyncio.to_thread(self._execute, sql, (json.dumps(value), updated_by, namespace, key, expected_version))
            if not rows:
                raise StateStoreUnavailable("CAS conflict or missing PostgreSQL state key")
        return int(rows[0][0])


__all__ = ["PostgresStateStore", "StateStoreUnavailable", "SCHEMA_SQL"]
