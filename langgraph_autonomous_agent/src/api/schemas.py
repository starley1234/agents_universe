"""Pydantic request/response models."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from pydantic import BaseModel, Field


class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    description: str = Field(..., min_length=1)
    report_email: Optional[str] = None
    notify_telegram: bool = False
    max_iterations: int = Field(20, ge=1, le=100)


class StepOut(BaseModel):
    id: UUID
    step_index: int
    description: str
    status: str
    output: Optional[str] = None
    error: Optional[str] = None
    duration_s: Optional[float] = None
    model_config = {"from_attributes": True}


class TaskOut(BaseModel):
    id: UUID
    title: str
    description: str
    status: str
    priority: int
    current_step: int
    total_steps: int
    progress: float
    quality: float
    iterations: int
    max_iterations: int
    plan: Optional[dict[str, Any]] = None
    result: Optional[str] = None
    error: Optional[str] = None
    report_email: Optional[str] = None
    notify_telegram: bool
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    updated_at: datetime
    steps: list[StepOut] = []
    model_config = {"from_attributes": True}


class TaskBrief(BaseModel):
    id: UUID
    title: str
    status: str
    progress: float
    quality: float
    created_at: datetime
    model_config = {"from_attributes": True}


class TaskList(BaseModel):
    tasks: list[TaskOut]
    total: int


class Stats(BaseModel):
    total: int
    active: int
    completed: int
    failed: int
    pending: int
    avg_quality: float


class MemSearchReq(BaseModel):
    query: str = Field(..., min_length=1)
    top_k: int = Field(10, ge=1, le=100)
    source_type: Optional[str] = None


class MemSearchRes(BaseModel):
    content: str
    source: str
    similarity: float
    metadata: Optional[dict] = None


class KnowledgeAdd(BaseModel):
    concept: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1)
    source: Optional[str] = None
    category: Optional[str] = None
    confidence: float = Field(1.0, ge=0, le=1)


class OntNodeOut(BaseModel):
    id: UUID
    concept: str
    description: Optional[str] = None
    category: Optional[str] = None
    visits: int
    model_config = {"from_attributes": True}
