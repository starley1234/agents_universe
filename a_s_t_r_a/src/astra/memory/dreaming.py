"""Memory Dreaming — background consolidation of accumulated knowledge."""

from __future__ import annotations

from uuid import UUID

from langchain_core.messages import SystemMessage
from loguru import logger

from astra.llm.gateway import llm_gateway
from astra.memory.ontology import ontology_store
from astra.memory.semantic import semantic_memory

CONSOLIDATION_PROMPT = """You are the memory consolidation module of A.S.T.R.A.
Given a batch of recent facts and observations, produce a concise structured summary
that identifies:
1. Key entities (people, tools, concepts)
2. Relations between them
3. Contradictions or outdated information to remove

Output as JSON:
{
  "entities": [{"name": "...", "type": "..."}],
  "relations": [{"source": "...", "target": "...", "relation": "..."}],
  "contradictions": ["..."]
}
"""


async def consolidate_project(project_id: UUID, batch_size: int = 50) -> None:
    """
    Run a single consolidation cycle for a project.

    1. Pull recent unprocessed chunks from semantic memory.
    2. Ask LLM to extract entities & relations.
    3. Update the ontology graph.
    4. Mark chunks as processed.
    """
    logger.info("💤  Memory dreaming started for project {}", project_id)

    # 1. Get recent facts (placeholder)
    recent_facts = "[placeholder: recent facts from DB]"
    # TODO: query unprocessed embeddings

    # 2. LLM extraction
    messages = [
        SystemMessage(content=CONSOLIDATION_PROMPT),
        SystemMessage(content=f"Recent facts:\n{recent_facts}"),
    ]
    response = await llm_gateway.chat(messages=messages)

    # 3. Parse and apply to ontology
    import json

    try:
        raw = response.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(raw)

        for entity in data.get("entities", []):
            ontology_store.add_entity(
                project_id,
                entity["name"],
                entity.get("type", "concept"),
            )

        for rel in data.get("relations", []):
            ontology_store.add_relation(
                project_id,
                rel["source"],
                rel["target"],
                rel.get("relation", "related_to"),
            )

        logger.info(
            "✅  Dreaming complete: {} entities, {} relations",
            len(data.get("entities", [])),
            len(data.get("relations", [])),
        )
    except Exception as exc:
        logger.error("Dreaming parse failed: {}", exc)

    # 4. TODO: mark chunks as consolidated
