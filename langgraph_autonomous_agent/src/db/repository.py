"""CRUD helpers — thin wrappers around SQLAlchemy for async operations."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models import Knowledge, MemoryVector, OntNode, OntRel, Step, Task


# ─── Tasks ──────────────────────────────────────────────────────────────
async def create_task(db: AsyncSession, title: str, description: str,
                      *, report_email: str | None = None,
                      notify_telegram: bool = False,
                      max_iterations: int = 20) -> Task:
    t = Task(title=title, description=description, report_email=report_email,
             notify_telegram=notify_telegram, max_iterations=max_iterations)
    db.add(t)
    await db.flush()
    await db.refresh(t)
    return t


async def get_task(db: AsyncSession, tid: uuid.UUID) -> Task | None:
    return await db.get(Task, tid)


async def list_tasks(db: AsyncSession, *, status: str | None = None,
                     limit: int = 50, offset: int = 0) -> Sequence[Task]:
    q = select(Task).order_by(Task.created_at.desc()).offset(offset).limit(limit)
    if status:
        q = q.where(Task.status == status)
    return (await db.execute(q)).scalars().all()


async def active_tasks(db: AsyncSession) -> Sequence[Task]:
    q = select(Task).where(Task.status.in_(["pending", "running"])).order_by(Task.created_at.desc())
    return (await db.execute(q)).scalars().all()


async def update_task(db: AsyncSession, tid: uuid.UUID, **kw) -> None:
    kw.setdefault("updated_at", datetime.now(timezone.utc))
    await db.execute(update(Task).where(Task.id == tid).values(**kw))
    await db.flush()


async def status_counts(db: AsyncSession) -> dict[str, int]:
    rows = (await db.execute(select(Task.status, func.count(Task.id)).group_by(Task.status))).all()
    return {r[0]: r[1] for r in rows}


# ─── Steps ──────────────────────────────────────────────────────────────
async def create_step(db: AsyncSession, task_id: uuid.UUID, idx: int, desc: str) -> Step:
    s = Step(task_id=task_id, step_index=idx, description=desc)
    db.add(s)
    await db.flush()
    return s


async def get_steps(db: AsyncSession, task_id: uuid.UUID) -> Sequence[Step]:
    q = select(Step).where(Step.task_id == task_id).order_by(Step.step_index)
    return (await db.execute(q)).scalars().all()


async def update_step(db: AsyncSession, sid: uuid.UUID, **kw) -> None:
    await db.execute(update(Step).where(Step.id == sid).values(**kw))
    await db.flush()


# ─── Memory vectors ────────────────────────────────────────────────────
async def store_vector(db: AsyncSession, content: str, *,
                       embedding: list[float] | None = None,
                       source_type: str = "task",
                       source_id: str | None = None,
                       meta: dict | None = None) -> MemoryVector:
    import json as _j
    v = MemoryVector(content=content,
                     embedding=_j.dumps(embedding) if embedding else None,
                     source_type=source_type, source_id=source_id, meta=meta)
    db.add(v)
    await db.flush()
    return v


async def search_vectors(db: AsyncSession, query_emb: list[float], *,
                         top_k: int = 10, source_type: str | None = None) -> list[dict]:
    """Cosine-similarity search via pgvector."""
    import json as _j
    emb_str = _j.dumps(query_emb)
    sql = """
        SELECT id, content, source_type, source_id, meta,
               1 - (embedding::vector <=> :e::vector) AS sim
        FROM memory_vectors WHERE embedding IS NOT NULL
    """
    if source_type:
        sql += " AND source_type = :st"
    sql += " ORDER BY embedding::vector <=> :e::vector LIMIT :k"
    params: dict = {"e": emb_str, "k": top_k}
    if source_type:
        params["st"] = source_type
    rows = (await db.execute(text(sql), params)).all()
    return [{"id": str(r[0]), "content": r[1], "source_type": r[2],
             "source_id": r[3], "meta": r[4], "similarity": float(r[5])} for r in rows]


# ─── Ontology ───────────────────────────────────────────────────────────
async def upsert_concept(db: AsyncSession, concept: str, *,
                         description: str | None = None,
                         category: str | None = None) -> OntNode:
    q = select(OntNode).where(OntNode.concept == concept)
    node = (await db.execute(q)).scalar_one_or_none()
    if node:
        node.visits += 1
        await db.flush()
        return node
    node = OntNode(concept=concept, description=description, category=category)
    db.add(node)
    await db.flush()
    await db.refresh(node)
    return node


async def add_relation(db: AsyncSession, src: uuid.UUID, dst: uuid.UUID,
                       rel_type: str, weight: float = 1.0) -> OntRel:
    r = OntRel(src_id=src, dst_id=dst, rel_type=rel_type, weight=weight)
    db.add(r)
    await db.flush()
    return r


async def graph_neighbours(db: AsyncSession, node_id: uuid.UUID, depth: int = 2) -> list[dict]:
    sql = """
        WITH RECURSIVE g AS (
            SELECT :nid::uuid AS id, 0 AS d
            UNION
            SELECT CASE WHEN r.src_id = g.id THEN r.dst_id ELSE r.src_id END, g.d + 1
            FROM g JOIN ontology_relations r ON (r.src_id = g.id OR r.dst_id = g.id)
            WHERE g.d < :depth
        )
        SELECT DISTINCT n.id, n.concept, n.description, n.category, g.d
        FROM g JOIN ontology_nodes n ON n.id = g.id ORDER BY g.d, n.concept
    """
    rows = (await db.execute(text(sql), {"nid": str(node_id), "depth": depth})).all()
    return [{"id": str(r[0]), "concept": r[1], "description": r[2],
             "category": r[3], "depth": r[4]} for r in rows]


async def search_concepts(db: AsyncSession, q: str, limit: int = 20) -> list[OntNode]:
    stmt = select(OntNode).where(OntNode.concept.ilike(f"%{q}%")).order_by(OntNode.visits.desc()).limit(limit)
    return list((await db.execute(stmt)).scalars().all())


async def store_knowledge(db: AsyncSession, text: str, *,
                          node_id: uuid.UUID | None = None,
                          source: str | None = None,
                          confidence: float = 1.0) -> Knowledge:
    k = Knowledge(text=text, node_id=node_id, source=source, confidence=confidence)
    db.add(k)
    await db.flush()
    return k
