"""Project-scoped memory manager — orchestrates semantic + ontology layers."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from loguru import logger

from astra.memory.ontology import ontology_store
from astra.memory.semantic import semantic_memory


class ProjectMemory:
    """Facade combining semantic + ontology for a single project."""

    def __init__(self, project_id: UUID, workspace: Path) -> None:
        self.project_id = project_id
        self.workspace = workspace
        self._ontology_path = workspace / "ontology.json"

    # ── Write ────────────────────────────────────────────────

    async def remember(self, text: str, metadata: dict | None = None) -> None:
        """Store a piece of knowledge in both layers."""
        await semantic_memory.store(text, self.project_id, metadata)
        logger.debug("Remembered text ({} chars) for project {}", len(text), self.project_id)

    def add_entity(self, name: str, entity_type: str = "concept", **kw) -> None:
        ontology_store.add_entity(self.project_id, name, entity_type, **kw)

    def add_relation(self, src: str, tgt: str, rel: str = "related_to", **kw) -> None:
        ontology_store.add_relation(self.project_id, src, tgt, rel, **kw)

    # ── Read ─────────────────────────────────────────────────

    async def recall(self, query: str, top_k: int = 5) -> str:
        """Hybrid recall: semantic search + ontology neighbourhood."""
        semantic = await semantic_memory.search(query, self.project_id, top_k)
        ontology = ontology_store.get_subgraph_text(self.project_id, query.split()[0])
        parts = [semantic]
        if ontology:
            parts.append(f"Knowledge graph:\n{ontology}")
        return "\n---\n".join(parts)

    # ── Persistence ──────────────────────────────────────────

    def save_ontology(self) -> None:
        ontology_store.save(self.project_id, self._ontology_path)

    def load_ontology(self) -> None:
        ontology_store.load(self.project_id, self._ontology_path)
