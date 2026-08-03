"""Combined RAG — semantic (pgvector) + ontological (graph) + keyword fallback."""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger(__name__)


class CombinedRAG:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def retrieve(self, query: str, *, top_k: int = 10,
                       source_type: str | None = None) -> list[dict[str, Any]]:
        from src.memory.vector_store import VectorStore
        from src.memory.ontology import OntologyMemory

        results: list[dict] = []
        seen: set[str] = set()

        def _add(items: list[dict]):
            for r in items:
                key = r["content"][:200]
                if key not in seen:
                    seen.add(key)
                    results.append(r)

        # 1. Semantic
        try:
            vs = VectorStore(self.db)
            _add([{"content": r["content"], "source": "semantic",
                   "similarity": r.get("similarity", 0), "metadata": r.get("meta")}
                  for r in await vs.search(query, top_k=top_k, source_type=source_type)])
        except Exception as e:
            log.debug("Semantic search failed: %s", e)

        # 2. Ontological
        try:
            om = OntologyMemory(self.db)
            _add([{"content": r["content"], "source": "ontology",
                   "similarity": r.get("relevance", 0.5), "metadata": r.get("metadata")}
                  for r in await om.search(query)])
        except Exception as e:
            log.debug("Ontology search failed: %s", e)

        # 3. Keyword fallback
        if len(results) < 3:
            try:
                vs = VectorStore(self.db)
                _add([{"content": r["content"], "source": "keyword",
                       "similarity": 0.3, "metadata": r.get("meta")}
                      for r in await vs.keyword_search(query, top_k=top_k - len(results))])
            except Exception as e:
                log.debug("Keyword search failed: %s", e)

        results.sort(key=lambda x: x.get("similarity", 0), reverse=True)
        return results[:top_k]
