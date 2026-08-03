"""Project management endpoints — with JWT auth, safe owner_id."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from astra.api.deps import db_session
from astra.auth.jwt import get_current_user, get_safe_owner_id
from astra.db.models import User
from astra.db.repositories import ProjectRepo

router = APIRouter()


class ProjectCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    description: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=255)
    description: str | None = None


class ProjectOut(BaseModel):
    id: UUID
    name: str
    description: str | None

    model_config = {"from_attributes": True}


@router.post("/", response_model=ProjectOut, status_code=201)
async def create_project(
    body: ProjectCreate,
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
):
    repo = ProjectRepo(db)
    project = await repo.create(body.name, body.description)
    # Safe owner assignment — avoids FK violation with zero UUID dev user
    owner_id = get_safe_owner_id(current_user)
    if owner_id:
        try:
            project.owner_id = owner_id
            await db.flush()
        except Exception:
            pass
    return project


@router.get("/", response_model=list[ProjectOut])
async def list_projects(
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
):
    repo = ProjectRepo(db)
    return await repo.list_all()


@router.get("/{project_id}", response_model=ProjectOut)
async def get_project(
    project_id: UUID,
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
):
    repo = ProjectRepo(db)
    project = await repo.get(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: UUID,
    body: ProjectUpdate,
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
):
    repo = ProjectRepo(db)
    project = await repo.update(project_id, body.name, body.description)
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.delete("/{project_id}", status_code=204)
async def delete_project(
    project_id: UUID,
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
):
    repo = ProjectRepo(db)
    ok = await repo.delete(project_id)
    if not ok:
        raise HTTPException(404, "Project not found")
    return None
