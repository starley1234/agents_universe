"""Project management endpoints."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from astra.api.deps import db_session
from astra.db.repositories import ProjectRepo

router = APIRouter()


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""


class ProjectOut(BaseModel):
    id: UUID
    name: str
    description: str | None

    model_config = {"from_attributes": True}


@router.post("/", response_model=ProjectOut, status_code=201)
async def create_project(body: ProjectCreate, db: AsyncSession = Depends(db_session)):
    repo = ProjectRepo(db)
    project = await repo.create(body.name, body.description)
    return project


@router.get("/", response_model=list[ProjectOut])
async def list_projects(db: AsyncSession = Depends(db_session)):
    repo = ProjectRepo(db)
    return await repo.list_all()


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(project_id: UUID, db: AsyncSession = Depends(db_session)):
    repo = ProjectRepo(db)
    project = await repo.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project
