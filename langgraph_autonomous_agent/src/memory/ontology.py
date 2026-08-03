"""Ontological memory — knowledge graph with concept relationships."""
from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Knowledge, OntNode
from src.db.repository import graph_neighbours, search_concepts, upsert_concept

log = logging.getLogger(__name__)


class OntologyMemory:
    def __init__(self, db: AsyncSession):
        self.db = db

    async def search(self, query: str, *, depth: int = 2, max_concepts: int = 5) -> list[dict[str, Any]]:
        results: list[dict] = []
        seen: set[str] = set()

        for node in await search_concepts(self.db, query, limit=max_concepts):
            nid = str(node.id)
            if nid in seen:
                continue
            seen.add(nid)
            results.append({"content": f"Concept: {node.concept}. {node.description or ''}",
                            "relevance": min(1.0, node.visits / 10 + 0.3),
                            "metadata": {"type": "concept", "category": node.category}})
            try:
                for nb in await graph_neighbours(self.db, node.id, depth=depth):
                    if nb["id"] in seen:
                        continue
                    seen.add(nb["id"])
                    results.append({"content": f"Related: {nb['concept']}. {nb.get('description', '')}",
                                    "relevance": max(0.1, 1.0 - nb.get("depth", 0) * 0.3),
                                    "metadata": {"type": "related", "depth": nb.get("depth", 0)}})
            except Exception as e:
                log.debug("Graph traversal failed: %s", e)

            rows = (await self.db.execute(
                select(Knowledge).where(Knowledge.node_id == node.id)
                .order_by(Knowledge.confidence.desc()).limit(5))).scalars().all()
            for k in rows:
                results.append({"content": k.text, "relevance": k.confidence * 0.8,
                                "metadata": {"type": "knowledge", "source": k.source}})

        results.sort(key=lambda x: x.get("relevance", 0), reverse=True)
        return results[:20]
