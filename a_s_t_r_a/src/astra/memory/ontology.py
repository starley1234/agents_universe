"""Ontology layer — NetworkX + optional FalkorDB backend for large graphs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

import networkx as nx
from loguru import logger

from astra.config import settings


class OntologyStore:
    """Per-project knowledge graph with NetworkX in-memory + optional FalkorDB."""

    def __init__(self) -> None:
        self._graphs: dict[UUID, nx.DiGraph] = {}
        self._use_falkor = False
        self._falkor_client = None

        # Try to enable FalkorDB if configured
        if settings.use_falkordb:
            try:
                from astra.memory.falkor_store import falkor_store

                if falkor_store._client:
                    self._use_falkor = True
                    self._falkor_client = falkor_store
                    logger.info("OntologyStore: FalkorDB backend enabled")
                else:
                    logger.info("OntologyStore: FalkorDB configured but not connected, using NetworkX")
            except Exception as exc:
                logger.warning("OntologyStore FalkorDB init failed ({}), using NetworkX", exc)

    def _get_graph(self, project_id: UUID) -> nx.DiGraph:
        if project_id not in self._graphs:
            self._graphs[project_id] = nx.DiGraph()
        return self._graphs[project_id]

    def add_entity(self, project_id: UUID, entity: str, entity_type: str = "concept", **attrs: Any) -> None:
        # Always add to NetworkX (local cache)
        g = self._get_graph(project_id)
        g.add_node(entity, type=entity_type, **attrs)
        logger.debug("Added entity '{}' ({}) to project {}", entity, entity_type, project_id)

        # Also try FalkorDB if enabled
        if self._use_falkor and self._falkor_client:
            try:
                from astra.memory.falkor_store import falkor_store

                falkor_store.add_entity_falkor(project_id, entity, entity_type, **attrs)
            except Exception as exc:
                logger.debug("FalkorDB add_entity fallback: {}", exc)

    def add_relation(self, project_id: UUID, source: str, target: str, relation: str = "related_to", **attrs: Any) -> None:
        g = self._get_graph(project_id)
        g.add_edge(source, target, relation=relation, **attrs)
        logger.debug("Added relation {} --[{}]--> {} in project {}", source, relation, target, project_id)

        if self._use_falkor and self._falkor_client:
            try:
                from astra.memory.falkor_store import falkor_store

                falkor_store.add_relation_falkor(project_id, source, target, relation, **attrs)
            except Exception as exc:
                logger.debug("FalkorDB add_relation fallback: {}", exc)

    def query_neighbors(self, project_id: UUID, entity: str, depth: int = 1) -> dict[str, Any]:
        # Try FalkorDB first if enabled? For now use NetworkX as primary (fast)
        # FalkorDB query is more expensive, so use cache
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
                    "edges": [{"target": tgt, **g.edges[node, tgt]} for tgt in g.successors(node)],
                }
                next_frontier.extend(g.successors(node))
            frontier = next_frontier
        return visited

    def get_subgraph_text(self, project_id: UUID, entity: str, depth: int = 2) -> str:
        data = self.query_neighbors(project_id, entity, depth)
        if not data:
            return ""
        lines: list[str] = []
        for name, info in data.items():
            for edge in info.get("edges", []):
                lines.append(f"{name} --[{edge.get('relation', '?')}]--> {edge['target']}")
        return "\n".join(lines)

    def get_full_graph_data(self, project_id: UUID) -> dict[str, Any]:
        """Return nodes/edges for API — from NetworkX cache."""
        g = self._get_graph(project_id)
        nodes = [{"id": n, "label": n, "group": g.nodes[n].get("type", "concept")} for n in g.nodes]
        edges = [{"from": u, "to": v, "label": g.edges[u, v].get("relation", "")} for u, v in g.edges]
        return {"nodes": nodes, "edges": edges, "backend": "falkordb" if self._use_falkor else "networkx"}

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

    def stats(self, project_id: UUID) -> dict[str, Any]:
        g = self._get_graph(project_id)
        return {
            "nodes": g.number_of_nodes(),
            "edges": g.number_of_edges(),
            "backend": "falkordb" if self._use_falkor else "networkx",
            "falkor_enabled": self._use_falkor,
        }


ontology_store = OntologyStore()
