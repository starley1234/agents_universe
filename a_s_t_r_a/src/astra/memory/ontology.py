"""Ontology layer — dynamic Knowledge Graph powered by NetworkX."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

import networkx as nx
from loguru import logger


class OntologyStore:
    """Per-project knowledge graph with persistence."""

    def __init__(self) -> None:
        self._graphs: dict[UUID, nx.DiGraph] = {}

    # ── Graph access ─────────────────────────────────────────

    def _get_graph(self, project_id: UUID) -> nx.DiGraph:
        if project_id not in self._graphs:
            self._graphs[project_id] = nx.DiGraph()
        return self._graphs[project_id]

    # ── CRUD ─────────────────────────────────────────────────

    def add_entity(
        self,
        project_id: UUID,
        entity: str,
        entity_type: str = "concept",
        **attrs: Any,
    ) -> None:
        g = self._get_graph(project_id)
        g.add_node(entity, type=entity_type, **attrs)
        logger.debug("Added entity '{}' ({}) to project {}", entity, entity_type, project_id)

    def add_relation(
        self,
        project_id: UUID,
        source: str,
        target: str,
        relation: str = "related_to",
        **attrs: Any,
    ) -> None:
        g = self._get_graph(project_id)
        g.add_edge(source, target, relation=relation, **attrs)
        logger.debug("Added relation {} --[{}]--> {} in project {}", source, relation, target, project_id)

    def query_neighbors(
        self,
        project_id: UUID,
        entity: str,
        depth: int = 1,
    ) -> dict[str, Any]:
        """BFS neighbourhood up to *depth* hops."""
        g = self._get_graph(project_id)
        if entity not in g:
            return {}

        visited: dict[str, Any] = {}
        frontier = [entity]
        for _ in range(depth + 1):
            next_frontier: list[str] = []
            for node in frontier:
                if node in visited:
                    continue
                visited[node] = {
                    "attrs": dict(g.nodes[node]),
                    "edges": [
                        {"target": tgt, **g.edges[node, tgt]}
                        for tgt in g.successors(node)
                    ],
                }
                next_frontier.extend(g.successors(node))
            frontier = next_frontier
        return visited

    def get_subgraph_text(self, project_id: UUID, entity: str, depth: int = 2) -> str:
        """Return a human-readable summary of the neighbourhood."""
        data = self.query_neighbors(project_id, entity, depth)
        if not data:
            return ""
        lines: list[str] = []
        for name, info in data.items():
            for edge in info.get("edges", []):
                lines.append(f"{name} --[{edge.get('relation', '?')}]--> {edge['target']}")
        return "\n".join(lines)

    # ── Persistence ──────────────────────────────────────────

    def save(self, project_id: UUID, path: Path) -> None:
        g = self._get_graph(project_id)
        data = nx.node_link_data(g)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        logger.info("Saved ontology for project {} → {}", project_id, path)

    def load(self, project_id: UUID, path: Path) -> None:
        if not path.exists():
            logger.warning("Ontology file not found: {}", path)
            return
        data = json.loads(path.read_text())
        self._graphs[project_id] = nx.node_link_graph(data)
        logger.info("Loaded ontology for project {} ← {}", project_id, path)


ontology_store = OntologyStore()
