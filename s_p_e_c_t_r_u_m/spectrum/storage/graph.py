"""Семантический граф: связи между документами, сущностями и концепциями."""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("spectrum.graph")


@dataclass
class GraphNode:
    """Узел графа: документ, сущность или концепция."""
    node_id: str
    node_type: str          # "document", "entity", "concept", "chunk"
    label: str
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "label": self.label,
            "properties": self.properties,
        }


@dataclass
class GraphEdge:
    """Ребро графа: связь между двумя узлами."""
    source_id: str
    target_id: str
    edge_type: str          # "contains", "references", "related_to", "mentions"
    weight: float = 1.0
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "target_id": self.target_id,
            "edge_type": self.edge_type,
            "weight": self.weight,
            "properties": self.properties,
        }


class SemanticGraph:
    """In-memory семантический граф для связи документов.

    Хранит:
    - Документы как узлы
    - Чанки как узлы, привязанные к документам
    - Сущности (упоминания в тексте) как узлы
    - Связи между ними

    Персистентность: JSON-файл на диске.
    """

    def __init__(self, persist_path: str | Path | None = None):
        self._nodes: dict[str, GraphNode] = {}
        self._edges: list[GraphEdge] = []
        self._adjacency: dict[str, list[str]] = defaultdict(list)  # node_id → [edge indices]
        self._persist_path = Path(persist_path) if persist_path else None

        if self._persist_path and self._persist_path.is_file():
            self._load()

    # --- Управление узлами ---

    def add_node(self, node: GraphNode) -> None:
        self._nodes[node.node_id] = node

    def get_node(self, node_id: str) -> GraphNode | None:
        return self._nodes.get(node_id)

    def add_document(self, doc_path: str, doc_hash: str, **props) -> GraphNode:
        """Добавляет документ как узел графа."""
        node = GraphNode(
            node_id=f"doc:{doc_hash[:16]}",
            node_type="document",
            label=Path(doc_path).name,
            properties={"path": doc_path, "hash": doc_hash, **props},
        )
        self.add_node(node)
        return node

    def add_chunk_node(self, chunk_id: str, text_preview: str, doc_node_id: str) -> GraphNode:
        """Добавляет чанк и связывает с документом."""
        node = GraphNode(
            node_id=f"chunk:{chunk_id}",
            node_type="chunk",
            label=text_preview[:80],
            properties={"full_text_length": len(text_preview)},
        )
        self.add_node(node)
        self.add_edge(doc_node_id, node.node_id, "contains")
        return node

    def add_entity(self, entity_text: str, entity_type: str = "unknown") -> GraphNode:
        """Добавляет сущность (имя, дата, сумма, организация)."""
        entity_id = f"ent:{entity_text.lower().strip()[:64]}"
        existing = self._nodes.get(entity_id)
        if existing:
            return existing

        node = GraphNode(
            node_id=entity_id,
            node_type="entity",
            label=entity_text,
            properties={"entity_type": entity_type},
        )
        self.add_node(node)
        return node

    # --- Управление связями ---

    def add_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        weight: float = 1.0,
        **props,
    ) -> None:
        edge = GraphEdge(
            source_id=source_id,
            target_id=target_id,
            edge_type=edge_type,
            weight=weight,
            properties=props,
        )
        idx = len(self._edges)
        self._edges.append(edge)
        self._adjacency[source_id].append(str(idx))
        self._adjacency[target_id].append(str(idx))

    def get_edges_from(self, node_id: str) -> list[GraphEdge]:
        """Все рёбра, исходящие из узла."""
        indices = self._adjacency.get(node_id, [])
        return [self._edges[int(i)] for i in indices if self._edges[int(i)].source_id == node_id]

    def get_edges_to(self, node_id: str) -> list[GraphEdge]:
        """Все рёбра, входящие в узел."""
        indices = self._adjacency.get(node_id, [])
        return [self._edges[int(i)] for i in indices if self._edges[int(i)].target_id == node_id]

    def get_neighbors(self, node_id: str, edge_type: str | None = None) -> list[GraphNode]:
        """Соседние узлы (с фильтром по типу связи)."""
        neighbors = []
        for edge in self.get_edges_from(node_id) + self.get_edges_to(node_id):
            if edge_type and edge.edge_type != edge_type:
                continue
            other_id = edge.target_id if edge.source_id == node_id else edge.source_id
            node = self._nodes.get(other_id)
            if node:
                neighbors.append(node)
        return neighbors

    def find_documents_for_entity(self, entity_text: str) -> list[GraphNode]:
        """Находит все документы, упоминающие сущность."""
        entity_id = f"ent:{entity_text.lower().strip()[:64]}"
        entity_node = self._nodes.get(entity_id)
        if not entity_node:
            return []

        # Идём от сущности → чанки → документы
        doc_ids = set()
        for chunk_node in self.get_neighbors(entity_id, "mentions"):
            for doc_node in self.get_neighbors(chunk_node.node_id, "contains"):
                if doc_node.node_type == "document":
                    doc_ids.add(doc_node.node_id)

        return [self._nodes[did] for did in doc_ids if did in self._nodes]

    # --- Статистика ---

    def stats(self) -> dict[str, int]:
        node_types = defaultdict(int)
        edge_types = defaultdict(int)
        for n in self._nodes.values():
            node_types[n.node_type] += 1
        for e in self._edges:
            edge_types[e.edge_type] += 1
        return {
            "total_nodes": len(self._nodes),
            "total_edges": len(self._edges),
            **{f"nodes_{k}": v for k, v in node_types.items()},
            **{f"edges_{k}": v for k, v in edge_types.items()},
        }

    # --- Персистентность ---

    def save(self) -> None:
        """Сохраняет граф в JSON."""
        if not self._persist_path:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "nodes": [n.to_dict() for n in self._nodes.values()],
            "edges": [e.to_dict() for e in self._edges],
        }
        self._persist_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info("Graph saved: %d nodes, %d edges", len(self._nodes), len(self._edges))

    def _load(self) -> None:
        """Загружает граф из JSON."""
        try:
            data = json.loads(self._persist_path.read_text(encoding="utf-8"))
            for nd in data.get("nodes", []):
                node = GraphNode(**nd)
                self._nodes[node.node_id] = node
            for ed in data.get("edges", []):
                edge = GraphEdge(**ed)
                idx = len(self._edges)
                self._edges.append(edge)
                self._adjacency[edge.source_id].append(str(idx))
                self._adjacency[edge.target_id].append(str(idx))
            logger.info("Graph loaded: %d nodes, %d edges", len(self._nodes), len(self._edges))
        except Exception as e:
            logger.warning("Failed to load graph: %s", e)

    def clear(self) -> None:
        self._nodes.clear()
        self._edges.clear()
        self._adjacency.clear()
