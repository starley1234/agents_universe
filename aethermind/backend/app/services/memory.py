import hashlib
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.db.models import MemoryItem
from app.services.embeddings import cosine_similarity, embed_text

TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv", ".py", ".scad", ".log", ".yaml", ".yml"}


def content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def chunk_text(text: str, chunk_chars: int | None = None) -> list[str]:
    size = chunk_chars or settings.memory_chunk_chars
    cleaned = text.strip()
    if not cleaned:
        return []
    chunks: list[str] = []
    cursor = 0
    while cursor < len(cleaned):
        end = min(len(cleaned), cursor + size)
        if end < len(cleaned):
            breakpoint = max(cleaned.rfind("\n\n", cursor, end), cleaned.rfind(". ", cursor, end))
            if breakpoint > cursor + size // 2:
                end = breakpoint + 1
        chunks.append(cleaned[cursor:end].strip())
        cursor = end
    return [chunk for chunk in chunks if chunk]


def create_memory(
    db: Session,
    content: str,
    task_id: UUID | None = None,
    metadata: dict[str, Any] | None = None,
) -> MemoryItem | None:
    content = content.strip()
    if not content:
        return None
    metadata = dict(metadata or {})
    digest = content_hash(content)
    metadata["hash"] = digest

    existing = db.execute(
        select(MemoryItem).where(MemoryItem.task_id == task_id, MemoryItem.content == content)
    ).scalar_one_or_none()
    if existing:
        return existing

    embedding_result = embed_text(content)
    metadata["embedding"] = embedding_result.embedding
    metadata["embedding_model"] = embedding_result.model
    metadata["embedding_provider"] = embedding_result.provider

    item = MemoryItem(task_id=task_id, content=content, embedding=None, metadata_json=metadata)
    db.add(item)
    db.flush()
    return item


def retrieve_memories(
    db: Session,
    query: str,
    task_id: UUID | None = None,
    top_k: int | None = None,
    include_global: bool = True,
) -> list[dict[str, Any]]:
    top_k = top_k or settings.memory_top_k
    query_embedding = embed_text(query).embedding
    stmt = select(MemoryItem)
    if task_id and include_global:
        stmt = stmt.where(or_(MemoryItem.task_id == task_id, MemoryItem.task_id.is_(None)))
    elif task_id:
        stmt = stmt.where(MemoryItem.task_id == task_id)
    rows = db.execute(stmt.order_by(MemoryItem.created_at.desc()).limit(1000)).scalars().all()
    scored = []
    for row in rows:
        embedding = (row.metadata_json or {}).get("embedding")
        if not embedding:
            continue
        score = cosine_similarity(query_embedding, [float(value) for value in embedding])
        scored.append(
            {
                "id": str(row.id),
                "task_id": str(row.task_id) if row.task_id else None,
                "content": row.content,
                "score": score,
                "metadata": row.metadata_json or {},
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
        )
    scored.sort(key=lambda item: item["score"], reverse=True)
    return scored[:top_k]


def format_memories_for_prompt(memories: list[dict[str, Any]]) -> str:
    if not memories:
        return "нет релевантных воспоминаний"
    parts = []
    for index, memory in enumerate(memories, start=1):
        source = memory.get("metadata", {}).get("source", "memory")
        score = memory.get("score", 0.0)
        content = str(memory.get("content", ""))[:1200]
        parts.append(f"[{index}] source={source} score={score:.3f}\n{content}")
    return "\n\n".join(parts)


def store_iteration_memories(db: Session, task_id: UUID, state: dict, workspace: Path | None = None) -> list[MemoryItem]:
    created: list[MemoryItem] = []
    iteration = state.get("iteration", 0)
    candidates: list[tuple[str, dict[str, Any]]] = []

    if state.get("executive_summary"):
        candidates.append((state["executive_summary"], {"source": "executive_summary", "iteration": iteration}))
    if state.get("reflection"):
        candidates.append((f"Reflection iteration {iteration}: {state['reflection']}", {"source": "reflection", "iteration": iteration}))
    if state.get("observation"):
        candidates.append((f"Observation iteration {iteration}: {state['observation']}", {"source": "observation", "iteration": iteration}))
    if state.get("critic_advisories"):
        candidates.append((f"Critic advisories: {state['critic_advisories'][-5:]}", {"source": "critic_advisories", "iteration": iteration}))
    if state.get("mcp_calls"):
        candidates.append((f"Recent MCP calls: {state['mcp_calls'][-3:]}", {"source": "mcp_calls", "iteration": iteration}))

    if workspace:
        for artifact in state.get("artifacts", [])[-settings.memory_max_items_per_iteration :]:
            rel = artifact.get("path")
            if not rel:
                continue
            path = (workspace / rel).resolve()
            if not str(path).startswith(str(workspace.resolve())) or not path.exists() or not path.is_file():
                continue
            if path.suffix.lower() not in TEXT_SUFFIXES or path.stat().st_size > 80_000:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for chunk_index, chunk in enumerate(chunk_text(text), start=1):
                candidates.append((chunk, {"source": "artifact", "path": rel, "chunk": chunk_index, "iteration": iteration}))

    for content, metadata in candidates[: settings.memory_max_items_per_iteration * 2]:
        if len(created) >= settings.memory_max_items_per_iteration:
            break
        for chunk in chunk_text(content):
            if len(created) >= settings.memory_max_items_per_iteration:
                break
            item = create_memory(db, chunk, task_id=task_id, metadata=metadata)
            if item:
                created.append(item)
    return created
