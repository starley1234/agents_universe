"""Semantic memory — Postgres prod, SQLite tests fallback."""

from __future__ import annotations

from uuid import UUID

from loguru import logger

from astra.llm.embeddings import embedding_service
from astra.db.engine import get_session
from astra.db.repositories import MemoryChunkRepo


class SemanticMemory:
    async def store(self, text: str, project_id: UUID, metadata: dict | None = None) -> str:
        import json

        try:
            vector = await embedding_service.embed(text)
        except Exception as exc:
            logger.warning("Embedding failed ({}), storing without vector", exc)
            vector = None

        meta_json = json.dumps(metadata or {}, ensure_ascii=False)

        try:
            async with get_session() as session:
                repo = MemoryChunkRepo(session)
                chunk = await repo.add(project_id, text, vector, meta_json)
                logger.debug("Stored chunk {} for project {}", chunk.id, project_id)
                return str(chunk.id)
        except Exception as exc:
            logger.error("DB store failed: {}", exc)
            raise

    async def search(self, query: str, project_id: UUID, top_k: int = 5) -> str:
        if not query or not query.strip():
            return ""

        try:
            query_vec = await embedding_service.embed(query)
            async with get_session() as session:
                repo = MemoryChunkRepo(session)
                results = await repo.search_by_embedding(project_id, query_vec, top_k)
                if results:
                    return "\n---\n".join([r.text for r in results if r.text])
        except Exception as exc:
            logger.debug("Embedding search failed ({}), trying text search", exc)

        try:
            async with get_session() as session:
                repo = MemoryChunkRepo(session)
                results = await repo.search_text(project_id, query, top_k)
                if results:
                    return "\n---\n".join([r.text for r in results if r.text])
        except Exception as exc:
            logger.debug("Text search failed: {}", exc)

        return ""

    async def list_recent(self, project_id: UUID, limit: int = 20) -> list[dict]:
        try:
            async with get_session() as session:
                repo = MemoryChunkRepo(session)
                chunks = await repo.list_by_project(project_id, limit)
                return [
                    {
                        "id": str(c.id),
                        "text": c.text,
                        "metadata": c.metadata_json,
                        "created_at": c.created_at.isoformat() if c.created_at else "",
                        "consolidated": c.consolidated,
                    }
                    for c in chunks
                ]
        except Exception as exc:
            logger.warning("list_recent failed: {}", exc)
            return []


semantic_memory = SemanticMemory()
