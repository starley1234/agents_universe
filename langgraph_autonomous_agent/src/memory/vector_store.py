"""Vector memory — pgvector semantic search."""
from __future__ import annotations

import json as _j
import logging
from typing import Any, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import MemoryVector

log = logging.getLogger(__name__)


class VectorStore:
    def __init__(self, db: AsyncSession):
        self.db = db
        self._emb = None

    @property
    def emb(self):
        if self._emb is None:
            from src.agent.llm import get_embeddings
            self._emb = get_embeddings()
        return self._emb

    async def _embed(self, t: str) -> list[float]:
        try:
            return await self.emb.aembed_query(t)
        except Exception as e:
            log.warning("Embedding failed: %s", e)
            return []

    async def store(self, content: str, *, source_type: str = "memory",
                    source_id: str | None = None, meta: dict | None = None) -> str | None:
        vec = await self._embed(content)
        if not vec:
            return None
        v = MemoryVector(content=content, embedding=_j.dumps(vec),
                         source_type=source_type, source_id=source_id, meta=meta)
        self.db.add(v)
        await self.db.flush()
        return str(v.id)

    async def search(self, query: str, *, top_k: int = 10,
                     source_type: str | None = None) -> list[dict[str, Any]]:
        vec = await self._embed(query)
        if not vec:
            return []
        emb_str = _j.dumps(vec)
        sql = ("SELECT id, content, source_type, source_id, meta,"
               " 1 - (embedding::vector <=> :e::vector) AS sim"
               " FROM memory_vectors WHERE embedding IS NOT NULL")
        params: dict = {"e": emb_str, "k": top_k}
        if source_type:
            sql += " AND source_type = :st"
            params["st"] = source_type
        sql += " ORDER BY embedding::vector <=> :e::vector LIMIT :k"
        rows = (await self.db.execute(text(sql), params)).all()
        return [{"id": str(r[0]), "content": r[1], "source_type": r[2],
                 "source_id": r[3], "meta": r[4], "similarity": float(r[5])} for r in rows]

    async def keyword_search(self, query: str, *, top_k: int = 10,
                             source_type: str | None = None) -> list[dict]:
        q = (select(MemoryVector).where(MemoryVector.content.ilike(f"%{query}%"))
             .order_by(MemoryVector.created_at.desc()).limit(top_k))
        if source_type:
            q = q.where(MemoryVector.source_type == source_type)
        rows = (await self.db.execute(q)).scalars().all()
        return [{"id": str(v.id), "content": v.content, "source_type": v.source_type,
                 "source_id": v.source_id, "meta": v.meta, "similarity": 0.3} for v in rows]
