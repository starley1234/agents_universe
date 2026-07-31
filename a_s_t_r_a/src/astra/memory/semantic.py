"""Semantic memory — vector-based RAG store backed by pgvector."""

from __future__ import annotations

from uuid import UUID

from loguru import logger

from astra.llm.embeddings import embedding_service


class SemanticMemory:
    """Provides vector search over project knowledge."""

    async def store(
        self,
        text: str,
        project_id: UUID,
        metadata: dict | None = None,
    ) -> str:
        """Embed and store a text chunk, return its ID."""
        vector = await embedding_service.embed(text)
        # TODO: INSERT into pgvector table via MemoryChunkRepo
        logger.debug("Stored embedding (dim={}) for project {}", len(vector), project_id)
        return "placeholder-id"

    async def search(
        self,
        query: str,
        project_id: UUID,
        top_k: int = 5,
    ) -> str:
        """Return concatenated top-k similar chunks."""
        try:
            query_vec = await embedding_service.embed(query)
            # TODO: SELECT … ORDER BY embedding <-> $query_vec LIMIT $top_k
            logger.debug("Semantic search: query='{}' top_k={}", query[:60], top_k)
        except Exception as exc:
            logger.debug("Embedding unavailable ({}), returning empty context", exc)
        return ""

    async def delete(self, chunk_id: str, project_id: UUID) -> None:
        logger.debug("Deleted chunk {} from project {}", chunk_id, project_id)


semantic_memory = SemanticMemory()
