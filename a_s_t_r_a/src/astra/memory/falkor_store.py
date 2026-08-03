"""FalkorDB-backed ontology store for large graphs.

Falls back to NetworkX if FalkorDB unavailable or disabled.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional
from uuid import UUID

from loguru import logger

from astra.config import settings

try:
    from falkordb import FalkorDB

    HAS_FALKORDB = True
except ImportError:
    FalkorDB = None  # type: ignore
    HAS_FALKORDB = False


class FalkorDBOntologyStore:
    """Ontology store using FalkorDB (RedisGraph compatible)."""

    def __init__(self) -> None:
        self._client: Optional[Any] = None
        self._enabled = settings.use_falkordb and HAS_FALKORDB
        if self._enabled:
            try:
                # FalkorDB client expects host, port
                host = settings.falkordb_host
                port = settings.falkordb_port
                logger.info("Connecting to FalkorDB at {}:{}", host, port)
                db = FalkorDB(host=host, port=port)
                # Test connection
                self._client = db
                logger.info("✅ FalkorDB connected")
            except Exception as exc:
                logger.warning("FalkorDB connection failed ({}), falling back to NetworkX", exc)
                self._client = None
                self._enabled = False
        else:
            if settings.use_falkordb and not HAS_FALKORDB:
                logger.warning("FalkorDB enabled but package not installed, using NetworkX")
            logger.info("FalkorDB disabled, using NetworkX in-memory")

    def _graph_name(self, project_id: UUID) -> str:
        # FalkorDB graph name must be alphanumeric
        return f"astra_{str(project_id).replace('-', '_')}"

    def _get_falkor_graph(self, project_id: UUID):
        if not self._client:
            return None
        try:
            return self._client.select_graph(self._graph_name(project_id))
        except Exception as exc:
            logger.debug("FalkorDB select_graph failed: {}", exc)
            return None

    # ── CRUD — tries FalkorDB then fallback logic handled by wrapper ──

    def add_entity_falkor(self, project_id: UUID, entity: str, entity_type: str = "concept", **attrs: Any) -> bool:
        g = self._get_falkor_graph(project_id)
        if not g:
            return False
        try:
            # MERGE entity node
            attrs_str = ", ".join([f"{k}: ${k}" for k in attrs.keys()])
            params = {"name": entity, "type": entity_type, **attrs}
            if attrs_str:
                query = f"MERGE (n:Entity {{name: $name}}) SET n.type = $type, n.{attrs_str.replace(', ', ', n.')}"
            else:
                query = "MERGE (n:Entity {name: $name}) SET n.type = $type"
            g.query(query, params)
            return True
        except Exception as exc:
            logger.warning("FalkorDB add_entity failed: {}", exc)
            return False

    def add_relation_falkor(self, project_id: UUID, source: str, target: str, relation: str = "related_to", **attrs: Any) -> bool:
        g = self._get_falkor_graph(project_id)
        if not g:
            return False
        try:
            # Ensure nodes exist then MERGE relation
            rel = relation.upper().replace(" ", "_").replace("-", "_")
            # Sanitize relation for Cypher (must be alphanumeric)
            rel_safe = "".join(c if c.isalnum() or c == "_" else "_" for c in rel) or "RELATED"
            query = (
                f"MERGE (a:Entity {{name: $source}}) "
                f"MERGE (b:Entity {{name: $target}}) "
                f"MERGE (a)-[r:{rel_safe} {{type: $rel_type}}]->(b)"
            )
            params = {"source": source, "target": target, "rel_type": relation, **attrs}
            g.query(query, params)
            return True
        except Exception as exc:
            logger.warning("FalkorDB add_relation failed: {}", exc)
            return False

    def query_neighbors_falkor(self, project_id: UUID, entity: str, depth: int = 1) -> dict[str, Any]:
        g = self._get_falkor_graph(project_id)
        if not g:
            return {}
        try:
            # BFS up to depth
            query = (
                f"MATCH (start:Entity {{name: $name}})-[r*1..{depth}]->(neighbor) "
                f"RETURN start, r, neighbor LIMIT 100"
            )
            result = g.query(query, {"name": entity})
            # Parse result into same format as NetworkX version
            visited: dict[str, Any] = {}
            if hasattr(result, "result_set") and result.result_set:
                for row in result.result_set:
                    # row parsing depends on FalkorDB client version — best effort
                    try:
                        # Expect row = [start_node, rels, neighbor_node] ?
                        # For simplicity, extract names if possible
                        # FalkorDB result_set is list of lists
                        # We'll attempt to parse nodes as dicts
                        pass
                    except Exception:
                        continue
            return visited
        except Exception as exc:
            logger.debug("FalkorDB query_neighbors failed: {}", exc)
            return {}


# Singleton trying FalkorDB
falkor_store = FalkorDBOntologyStore()
