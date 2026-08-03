"""SQLAlchemy ORM models — PostgreSQL + pgvector."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.config import get_settings

_settings = get_settings()

# pgvector type — graceful degradation if pgvector not installed
try:
    from pgvector.sqlalchemy import Vector
    _VEC = Vector(_settings.EMBEDDING_DIMENSIONS)
except ImportError:
    _VEC = Text  # store serialised JSON; search via raw SQL cast


class Base(DeclarativeBase):
    pass


# ─── Task ───────────────────────────────────────────────────────────────
class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="pending", index=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    current_step: Mapped[int] = mapped_column(Integer, default=0)
    total_steps: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    quality: Mapped[float] = mapped_column(Float, default=0.0)
    iterations: Mapped[int] = mapped_column(Integer, default=0)
    max_iterations: Mapped[int] = mapped_column(Integer, default=20)
    plan: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    result: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    report_email: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    notify_telegram: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    steps: Mapped[list["Step"]] = relationship(back_populates="task", cascade="all,delete-orphan", lazy="selectin")


# ─── Step ───────────────────────────────────────────────────────────────
class Step(Base):
    __tablename__ = "task_steps"
    __table_args__ = (Index("ix_step_task_idx", "task_id", "step_index"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("tasks.id", ondelete="CASCADE"))
    step_index: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(50), default="pending")
    output: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    duration_s: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    task: Mapped["Task"] = relationship(back_populates="steps")


# ─── MemoryVector — semantic embeddings ─────────────────────────────────
class MemoryVector(Base):
    __tablename__ = "memory_vectors"
    __table_args__ = (Index("ix_mv_source", "source_type"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    content: Mapped[str] = mapped_column(Text)
    embedding = mapped_column(_VEC, nullable=True)
    source_type: Mapped[str] = mapped_column(String(100), default="task")
    source_id: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    meta: Mapped[Optional[dict]] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ─── Ontology — knowledge graph ─────────────────────────────────────────
class OntNode(Base):
    __tablename__ = "ontology_nodes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    concept: Mapped[str] = mapped_column(String(500), index=True)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    category: Mapped[Optional[str]] = mapped_column(String(200), nullable=True, index=True)
    visits: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    outgoing: Mapped[list["OntRel"]] = relationship(foreign_keys="OntRel.src_id", back_populates="src", cascade="all,delete-orphan")
    incoming: Mapped[list["OntRel"]] = relationship(foreign_keys="OntRel.dst_id", back_populates="dst", cascade="all,delete-orphan")


class OntRel(Base):
    __tablename__ = "ontology_relations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    src_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ontology_nodes.id", ondelete="CASCADE"), index=True)
    dst_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("ontology_nodes.id", ondelete="CASCADE"), index=True)
    rel_type: Mapped[str] = mapped_column(String(200))
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    src: Mapped["OntNode"] = relationship(foreign_keys=[src_id], back_populates="outgoing")
    dst: Mapped["OntNode"] = relationship(foreign_keys=[dst_id], back_populates="incoming")


# ─── KnowledgeEntry — facts linked to ontology ─────────────────────────
class Knowledge(Base):
    __tablename__ = "knowledge_entries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    node_id: Mapped[Optional[uuid.UUID]] = mapped_column(UUID(as_uuid=True), ForeignKey("ontology_nodes.id", ondelete="SET NULL"), nullable=True, index=True)
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[Optional[str]] = mapped_column(String(500), nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
