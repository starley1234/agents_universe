"""Memory Dreaming — uses prompt registry."""

from __future__ import annotations

import json
from uuid import UUID

from langchain_core.messages import SystemMessage
from loguru import logger
from sqlalchemy import update

from astra.db.engine import get_session
from astra.db.models import MemoryChunk
from astra.db.repositories import MemoryChunkRepo
from astra.config import settings
from astra.llm.gateway import llm_gateway
from astra.memory.ontology import ontology_store
from astra.prompts.registry import prompt_registry

FALLBACK_PROMPT = """You are the memory consolidation module of A.S.T.R.A.
Given a batch of recent facts and observations, produce a concise structured summary
that identifies:
1. Key entities (people, tools, concepts)
2. Relations between them
3. Contradictions or outdated information to remove

Output as JSON:
{
  "entities": [{"name": "...", "type": "concept"}],
  "relations": [{"source": "...", "target": "...", "relation": "related_to"}],
  "contradictions": ["..."]
}
Return ONLY valid JSON, no markdown.
"""


def _get_consolidation_prompt() -> str:
    try:
        return prompt_registry.get("consolidation", default=FALLBACK_PROMPT) or FALLBACK_PROMPT
    except Exception:
        return FALLBACK_PROMPT


async def consolidate_project(project_id: UUID, batch_size: int = 50) -> dict:
    logger.info("💤 Memory dreaming started for project {}", project_id)

    try:
        async with get_session() as session:
            repo = MemoryChunkRepo(session)
            chunks = await repo.get_unconsolidated(project_id, limit=batch_size)
            if not chunks:
                logger.info("No unconsolidated chunks for project {}", project_id)
                return {"entities": 0, "relations": 0, "processed": 0}
            recent_facts = "\n---\n".join([c.text[:500] for c in chunks if c.text])
            chunk_ids = [c.id for c in chunks]
    except Exception as exc:
        logger.warning("Failed to fetch unconsolidated chunks: {}, using fallback", exc)
        recent_facts = "[no facts available]"
        chunk_ids = []

    if not recent_facts.strip():
        return {"entities": 0, "relations": 0, "processed": 0}

    messages = [
        SystemMessage(content=_get_consolidation_prompt()),
        SystemMessage(content=f"Recent facts:\n{recent_facts[:8000]}"),
    ]
    try:
        response = await llm_gateway.chat(messages=messages, temperature=0.2, max_tokens=2048, metadata={"prompt": "consolidation"})
        raw = response.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
            if raw.endswith("```"):
                raw = raw.rsplit("```", 1)[0]
            raw = raw.strip()
        data = json.loads(raw)
    except Exception as exc:
        logger.error("Dreaming LLM call or parse failed: {}", exc)
        return {"entities": 0, "relations": 0, "processed": 0, "error": str(exc)}

    entity_count = 0
    relation_count = 0
    try:
        for entity in data.get("entities", []):
            if isinstance(entity, dict) and entity.get("name"):
                ontology_store.add_entity(project_id, entity["name"], entity.get("type", "concept"))
                entity_count += 1

        for rel in data.get("relations", []):
            if isinstance(rel, dict) and rel.get("source") and rel.get("target"):
                ontology_store.add_relation(project_id, rel["source"], rel["target"], rel.get("relation", "related_to"))
                relation_count += 1

        try:
            ws = settings.resolved_workspace / str(project_id) / "ontology.json"
            ontology_store.save(project_id, ws)
        except Exception as exc:
            logger.debug("Failed to save ontology to file: {}", exc)

        logger.info("✅ Dreaming complete for {}: {} entities, {} relations, {} chunks", project_id, entity_count, relation_count, len(chunk_ids))
    except Exception as exc:
        logger.error("Failed to update ontology: {}", exc)

    if chunk_ids:
        try:
            async with get_session() as session:
                stmt = update(MemoryChunk).where(MemoryChunk.id.in_(chunk_ids)).values(consolidated=True)
                await session.execute(stmt)
        except Exception as exc:
            logger.warning("Failed to mark chunks as consolidated: {}", exc)

    return {
        "entities": entity_count,
        "relations": relation_count,
        "processed": len(chunk_ids),
        "contradictions": data.get("contradictions", []),
    }
