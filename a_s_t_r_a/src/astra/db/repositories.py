"""Repositories — Postgres prod, SQLite fallback for tests."""

from __future__ import annotations

import json
import math
from typing import Optional
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from astra.config import settings
from astra.db.models import AgentSession, MemoryChunk, Milestone, Project

_is_sqlite = "sqlite" in settings.database_url.lower()


class ProjectRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, name: str, description: str = "") -> Project:
        project = Project(name=name, description=description)
        self.session.add(project)
        await self.session.flush()
        return project

    async def get(self, project_id: UUID) -> Optional[Project]:
        return await self.session.get(Project, project_id)

    async def list_all(self) -> list[Project]:
        result = await self.session.execute(select(Project).order_by(Project.created_at.desc()))
        return list(result.scalars().all())

    async def delete(self, project_id: UUID) -> bool:
        proj = await self.session.get(Project, project_id)
        if not proj:
            return False
        await self.session.delete(proj)
        await self.session.flush()
        return True

    async def update(self, project_id: UUID, name: str | None = None, description: str | None = None) -> Optional[Project]:
        proj = await self.session.get(Project, project_id)
        if not proj:
            return None
        if name is not None:
            proj.name = name
        if description is not None:
            proj.description = description
        await self.session.flush()
        return proj


class MemoryChunkRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, project_id: UUID, text_content: str, embedding: list[float] | None, metadata: str = "") -> MemoryChunk:
        if _is_sqlite:
            emb = json.dumps(embedding) if embedding else None
            chunk = MemoryChunk(project_id=project_id, text=text_content, embedding=emb, metadata_json=metadata)
        else:
            chunk = MemoryChunk(project_id=project_id, text=text_content, embedding=embedding, metadata_json=metadata)
        self.session.add(chunk)
        await self.session.flush()
        return chunk

    async def list_by_project(self, project_id: UUID, limit: int = 100) -> list[MemoryChunk]:
        result = await self.session.execute(
            select(MemoryChunk).where(MemoryChunk.project_id == project_id).order_by(MemoryChunk.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def search_by_embedding(self, project_id: UUID, query_embedding: list[float], top_k: int = 5) -> list[MemoryChunk]:
        if _is_sqlite:
            # Python cosine similarity for tests
            all_chunks = await self.list_by_project(project_id, limit=500)
            scored: list[tuple[float, MemoryChunk]] = []
            for ch in all_chunks:
                emb = None
                try:
                    emb = json.loads(ch.embedding) if isinstance(ch.embedding, str) else ch.embedding
                except Exception:
                    continue
                if not emb:
                    continue
                try:
                    dot = sum(a * b for a, b in zip(query_embedding, emb))
                    norm_q = math.sqrt(sum(a * a for a in query_embedding))
                    norm_e = math.sqrt(sum(a * a for a in emb))
                    sim = dot / (norm_q * norm_e) if norm_q and norm_e else 0
                    scored.append((sim, ch))
                except Exception:
                    continue
            scored.sort(key=lambda x: x[0], reverse=True)
            return [c for _, c in scored[:top_k]]
        else:
            stmt = text(
                """
                SELECT id, text, metadata_json
                FROM memory_chunks
                WHERE project_id = CAST(:pid AS uuid)
                ORDER BY embedding <=> CAST(:query_vec AS vector)
                LIMIT :k
                """
            )
            result = await self.session.execute(stmt, {"query_vec": str(query_embedding), "pid": str(project_id), "k": top_k})
            rows = result.fetchall()
            return [MemoryChunk(id=r.id, text=r.text, metadata_json=r.metadata_json) for r in rows]

    async def get_unconsolidated(self, project_id: UUID, limit: int = 50) -> list[MemoryChunk]:
        stmt = select(MemoryChunk).where(MemoryChunk.project_id == project_id, MemoryChunk.consolidated == False).limit(limit)  # noqa: E712
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def search_text(self, project_id: UUID, query: str, top_k: int = 10) -> list[MemoryChunk]:
        stmt = select(MemoryChunk).where(MemoryChunk.project_id == project_id, MemoryChunk.text.ilike(f"%{query}%")).limit(top_k)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


class SessionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, project_id: UUID, goal: str) -> AgentSession:
        sess = AgentSession(project_id=project_id, goal=goal)
        self.session.add(sess)
        await self.session.flush()
        return sess

    async def list_by_project(self, project_id: UUID, limit: int = 50) -> list[AgentSession]:
        result = await self.session.execute(
            select(AgentSession).where(AgentSession.project_id == project_id).order_by(AgentSession.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def finish(self, session_id: UUID, result: str, status: str = "completed") -> None:
        sess = await self.session.get(AgentSession, session_id)
        if sess:
            sess.result = result
            sess.status = status
            from datetime import datetime, timezone

            sess.finished_at = datetime.now(timezone.utc)
            sess.steps_completed += 1
            await self.session.flush()

    async def add_milestone(self, session_id: UUID, title: str, content: str, mtype: str = "result") -> Milestone:
        m = Milestone(session_id=session_id, title=title, content=content, milestone_type=mtype)
        self.session.add(m)
        await self.session.flush()
        return m
