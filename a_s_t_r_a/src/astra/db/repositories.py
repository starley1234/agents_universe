"""Repository layer — typed CRUD operations for each model.

All vector searches use parameterised queries to prevent SQL injection.
"""

from __future__ import annotations

from typing import Optional
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from astra.db.models import AgentSession, MemoryChunk, Milestone, Project


# ── Projects ─────────────────────────────────────────────────

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
        result = await self.session.execute(
            select(Project).order_by(Project.created_at.desc())
        )
        return list(result.scalars().all())


# ── Memory Chunks ────────────────────────────────────────────

class MemoryChunkRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        project_id: UUID,
        text_content: str,
        embedding: list[float],
        metadata: str = "",
    ) -> MemoryChunk:
        chunk = MemoryChunk(
            project_id=project_id,
            text=text_content,
            embedding=embedding,
            metadata_json=metadata,
        )
        self.session.add(chunk)
        await self.session.flush()
        return chunk

    async def search_by_embedding(
        self,
        project_id: UUID,
        query_embedding: list[float],
        top_k: int = 5,
    ) -> list[MemoryChunk]:
        """Cosine-similarity search via pgvector (<=> operator).

        Uses SQLAlchemy text() with :bind parameters — no SQL injection.
        """
        stmt = text(
            """
            SELECT id, text, metadata_json
            FROM memory_chunks
            WHERE project_id = CAST(:pid AS uuid)
            ORDER BY embedding <=> CAST(:query_vec AS vector)
            LIMIT :k
            """
        )
        result = await self.session.execute(
            stmt,
            {
                "query_vec": str(query_embedding),
                "pid": str(project_id),
                "k": top_k,
            },
        )
        rows = result.fetchall()
        return [
            MemoryChunk(id=r.id, text=r.text, metadata_json=r.metadata_json)
            for r in rows
        ]

    async def get_unconsolidated(
        self, project_id: UUID, limit: int = 50
    ) -> list[MemoryChunk]:
        stmt = (
            select(MemoryChunk)
            .where(
                MemoryChunk.project_id == project_id,
                MemoryChunk.consolidated == False,  # noqa: E712
            )
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())


# ── Sessions ─────────────────────────────────────────────────

class SessionRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, project_id: UUID, goal: str) -> AgentSession:
        sess = AgentSession(project_id=project_id, goal=goal)
        self.session.add(sess)
        await self.session.flush()
        return sess

    async def finish(
        self, session_id: UUID, result: str, status: str = "completed"
    ) -> None:
        sess = await self.session.get(AgentSession, session_id)
        if sess:
            sess.result = result
            sess.status = status
            from datetime import datetime, timezone

            sess.finished_at = datetime.now(timezone.utc)
            await self.session.flush()

    async def add_milestone(
        self, session_id: UUID, title: str, content: str, mtype: str = "result"
    ) -> Milestone:
        m = Milestone(
            session_id=session_id, title=title, content=content, milestone_type=mtype
        )
        self.session.add(m)
        await self.session.flush()
        return m
