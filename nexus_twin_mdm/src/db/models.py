"""SQLAlchemy ORM models for MDM + CERTIFICATION + DIGITAL TWIN (v5.7)."""
from __future__ import annotations

import datetime
import uuid
from typing import Any, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import declarative_base
from sqlalchemy.types import JSON, TypeDecorator

from src.config import settings

Base = declarative_base()


class CompatJSON(TypeDecorator):
    """JSON type compatible with both PostgreSQL JSONB and SQLite JSON."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())


class OrgUnit(Base):
    __tablename__ = "org_units"

    id = Column(String, primary_key=True)
    parent_id = Column(String, ForeignKey("org_units.id"), nullable=True)
    name = Column(String, nullable=False)
    path = Column(String, nullable=False, default="")


class Type(Base):
    __tablename__ = "types"

    id = Column(String, primary_key=True)
    parent_id = Column(String, ForeignKey("types.id"), nullable=True)
    display_name = Column(String, nullable=False)
    path = Column(String, nullable=False, default="")
    schema = Column(CompatJSON, nullable=False, default=dict)


class Source(Base):
    __tablename__ = "sources"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    kind = Column(String, nullable=False)
    trust = Column(Integer, nullable=False)


class Uom(Base):
    __tablename__ = "uom"

    code = Column(String, primary_key=True)
    base_code = Column(String, ForeignKey("uom.code"), nullable=True)
    factor = Column(Numeric, nullable=False, default=1.0)
    name = Column(String, nullable=False)
    symbol_nat = Column(String, nullable=True)
    symbol_intl = Column(String, nullable=True)
    code_nat = Column(String, nullable=True)
    code_intl = Column(String, nullable=True)


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True)
    name = Column(String, nullable=False)
    org_id = Column(String, ForeignKey("org_units.id"), nullable=False)
    role = Column(String, nullable=False, default="viewer")
    is_active = Column(Boolean, nullable=False, default=True)


class ObjectCode(Base):
    __tablename__ = "object_codes"

    master_code = Column(String, primary_key=True)
    type_id = Column(String, ForeignKey("types.id"), primary_key=True)
    org_id = Column(String, ForeignKey("org_units.id"), primary_key=True)
    object_id = Column(String, nullable=False)


class MDMObject(Base):
    __tablename__ = "objects"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    type_id = Column(String, ForeignKey("types.id"), nullable=False)
    org_id = Column(String, ForeignKey("org_units.id"), nullable=False)
    org_path = Column(String, nullable=False, default="HOLDING")
    master_code = Column(String, nullable=False)
    state = Column(String, nullable=False, default="draft")
    merged_into = Column(String, nullable=True)
    is_dirty = Column(Boolean, nullable=False, default=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.datetime.now
    )
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.datetime.now
    )
    deleted_at = Column(DateTime(timezone=True), nullable=True)


class ObjectXref(Base):
    __tablename__ = "object_xref"

    source_id = Column(String, ForeignKey("sources.id"), primary_key=True)
    remote_id = Column(String, primary_key=True)
    object_id = Column(String, nullable=False)


class CodeSeries(Base):
    __tablename__ = "code_series"

    type_id = Column(String, ForeignKey("types.id"), primary_key=True)
    org_id = Column(String, ForeignKey("org_units.id"), primary_key=True)
    last_value = Column(Integer, nullable=False, default=0)
    prefix = Column(String, nullable=True)
    pad = Column(Integer, nullable=False, default=6)


class ObjectProperty(Base):
    __tablename__ = "object_properties"

    id = Column(Integer, primary_key=True, autoincrement=True)
    object_id = Column(String, nullable=False, index=True)
    org_path = Column(String, nullable=False, default="HOLDING")
    key = Column(String, nullable=False)
    value = Column(CompatJSON, nullable=False, default=dict)
    uom_code = Column(String, nullable=True)
    source_id = Column(String, ForeignKey("sources.id"), nullable=False)
    actor_id = Column(String, nullable=True)
    confidence = Column(Float, nullable=False, default=1.0)
    valid_period = Column(String, nullable=False, default="current")
    is_current = Column(Boolean, nullable=False, default=True)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.datetime.now
    )


class ObjectLink(Base):
    __tablename__ = "object_links"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    parent_id = Column(String, nullable=False, index=True)
    child_id = Column(String, nullable=False, index=True)
    link_type = Column(String, nullable=False)
    qty = Column(Numeric, nullable=False, default=1.0)
    designator = Column(String, nullable=True)
    valid_period = Column(String, nullable=False, default="current")


class Baseline(Base):
    __tablename__ = "baselines"

    id = Column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    seq = Column(Integer, nullable=False, default=1)
    object_id = Column(String, nullable=False, index=True)
    org_path = Column(String, nullable=False, default="HOLDING")
    code = Column(String, nullable=False)
    snapshot = Column(CompatJSON, nullable=False, default=dict)
    snapshot_hash = Column(String, nullable=False)
    prev_hash = Column(String, nullable=True)
    compliance_ref = Column(CompatJSON, nullable=False, default=dict)
    signature = Column(CompatJSON, nullable=True)
    signed_hash = Column(String, nullable=True)
    actor_id = Column(String, nullable=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.datetime.now
    )


class ObjectState(Base):
    __tablename__ = "object_state"

    object_id = Column(String, primary_key=True)
    type_id = Column(String, nullable=True)
    org_path = Column(String, nullable=True, default="HOLDING")
    attributes = Column(CompatJSON, nullable=False, default=dict)
    display_name = Column(String, nullable=True, index=True)
    display_desc = Column(Text, nullable=True)
    search = Column(Text, nullable=True)
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.datetime.now
    )


class ObjectEmbedding(Base):
    __tablename__ = "object_embeddings"

    object_id = Column(String, primary_key=True)
    embedding_json = Column("embedding", Text, nullable=False, default="[]")
    model = Column(String, nullable=False, default="text-embedding-qwen3-embedding-0.6b")
    updated_at = Column(
        DateTime(timezone=True), nullable=False, default=datetime.datetime.now
    )
