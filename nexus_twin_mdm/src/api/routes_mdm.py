"""REST API routes for MDM, Certification, and Digital Twin."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.engine import get_session
from src.db.repository import MDMRepository

router = APIRouter(tags=["MDM & Digital Twin"])


# Request Schemas
class CreateObjectRequest(BaseModel):
    type_id: str = Field(..., description="Type ID (e.g. part, engine, cert_req)")
    org_id: str = Field(default="HOLDING", description="Org unit ID")
    master_code: str = Field(..., description="Unique Master Code")
    name: str = Field(..., description="Display name")
    description: str = Field(default="", description="Description")
    attributes: Optional[Dict[str, Any]] = Field(default=None)
    source_id: str = Field(default="manual")


class UpsertPropertyRequest(BaseModel):
    key: str = Field(...)
    value: Dict[str, Any] = Field(...)
    source_id: str = Field(default="plm")
    uom_code: Optional[str] = Field(default=None)
    actor_id: str = Field(default="admin")


class AddBomLinkRequest(BaseModel):
    child_id: str = Field(..., description="Child Object UUID")
    link_type: str = Field(default="EBOM")
    qty: float = Field(default=1.0)
    designator: Optional[str] = Field(default=None)


class CreateBaselineRequest(BaseModel):
    code: str = Field(...)
    snapshot: Dict[str, Any] = Field(...)
    compliance_ref: Dict[str, Any] = Field(default_factory=dict)
    actor_id: str = Field(default="admin")


# --- Reference Dictionaries (НСИ) ---
@router.get("/api/org-units")
async def list_org_units(session: AsyncSession = Depends(get_session)) -> List[Dict[str, Any]]:
    repo = MDMRepository(session)
    return await repo.get_org_units()


@router.get("/api/types")
async def list_types(session: AsyncSession = Depends(get_session)) -> List[Dict[str, Any]]:
    repo = MDMRepository(session)
    return await repo.get_types()


@router.get("/api/sources")
async def list_sources(session: AsyncSession = Depends(get_session)) -> List[Dict[str, Any]]:
    repo = MDMRepository(session)
    return await repo.get_sources()


@router.get("/api/uom")
async def list_uom(session: AsyncSession = Depends(get_session)) -> List[Dict[str, Any]]:
    repo = MDMRepository(session)
    return await repo.get_uom()


# --- Objects & Digital Twin ---
@router.get("/api/objects")
async def list_objects(
    query: Optional[str] = Query(default=None, description="Search text query"),
    type_id: Optional[str] = Query(default=None, description="Filter by type_id"),
    org_id: Optional[str] = Query(default=None, description="Filter by org_id"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> List[Dict[str, Any]]:
    repo = MDMRepository(session)
    return await repo.list_objects(
        search_query=query,
        type_id=type_id,
        org_id=org_id,
        limit=limit,
        offset=offset,
    )


@router.post("/api/objects", status_code=201)
async def create_object(
    payload: CreateObjectRequest,
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    repo = MDMRepository(session)
    res = await repo.create_object(
        type_id=payload.type_id,
        org_id=payload.org_id,
        master_code=payload.master_code,
        name=payload.name,
        description=payload.description,
        attributes=payload.attributes,
        source_id=payload.source_id,
    )
    return res


@router.get("/api/objects/{object_id}")
async def get_object_detail(
    object_id: str,
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    repo = MDMRepository(session)
    detail = await repo.get_object_detail(object_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Object not found")
    return detail


@router.post("/api/objects/{object_id}/properties")
async def upsert_property(
    object_id: str,
    payload: UpsertPropertyRequest,
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    repo = MDMRepository(session)
    detail = await repo.get_object_detail(object_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Object not found")

    res = await repo.upsert_property(
        object_id=object_id,
        key=payload.key,
        value=payload.value,
        source_id=payload.source_id,
        uom_code=payload.uom_code,
        actor_id=payload.actor_id,
    )
    if res == "rejected_low_trust":
        raise HTTPException(
            status_code=403,
            detail="Property update rejected: Source trust score is lower than current property's source.",
        )

    updated_detail = await repo.get_object_detail(object_id)
    return {"status": res, "object": updated_detail}


# --- EBOM / MBOM Graph ---
@router.get("/api/objects/{object_id}/bom")
async def get_bom_tree(
    object_id: str,
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    repo = MDMRepository(session)
    detail = await repo.get_object_detail(object_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Object not found")

    links = await repo.get_bom_tree(object_id)
    return {
        "object_id": object_id,
        "master_code": detail["master_code"],
        "display_name": detail["display_name"],
        "bom_items": links,
    }


@router.post("/api/objects/{object_id}/bom")
async def add_bom_link(
    object_id: str,
    payload: AddBomLinkRequest,
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    repo = MDMRepository(session)
    detail = await repo.get_object_detail(object_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Parent object not found")

    link = await repo.add_bom_link(
        parent_id=object_id,
        child_id=payload.child_id,
        link_type=payload.link_type,
        qty=payload.qty,
        designator=payload.designator,
    )
    return {"status": "ok", "link": link}


# --- Baselines & Hash-Chain Compliance Verification ---
@router.get("/api/objects/{object_id}/baselines")
async def list_baselines(
    object_id: str,
    session: AsyncSession = Depends(get_session),
) -> List[Dict[str, Any]]:
    repo = MDMRepository(session)
    return await repo.list_baselines(object_id)


@router.post("/api/objects/{object_id}/baselines", status_code=201)
async def create_baseline(
    object_id: str,
    payload: CreateBaselineRequest,
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    repo = MDMRepository(session)
    b = await repo.create_baseline(
        object_id=object_id,
        code=payload.code,
        snapshot=payload.snapshot,
        compliance_ref=payload.compliance_ref,
        actor_id=payload.actor_id,
    )
    return {"status": "ok", "baseline": b}


@router.get("/api/objects/{object_id}/verify")
async def verify_baseline_chain(
    object_id: str,
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    repo = MDMRepository(session)
    detail = await repo.get_object_detail(object_id)
    if not detail:
        raise HTTPException(status_code=404, detail="Object not found")

    chain = await repo.verify_baseline_chain(object_id)
    is_valid = all(item["status"] == "OK" for item in chain)

    return {
        "object_id": object_id,
        "master_code": detail["master_code"],
        "chain_length": len(chain),
        "all_valid": is_valid,
        "chain": chain,
    }
