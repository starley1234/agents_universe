"""REST API routes for MDM Deduplication & Merging."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.deduplication import DeduplicationEngine
from src.db.engine import get_session

router = APIRouter(tags=["MDM Deduplication"])


class MergeRequest(BaseModel):
    primary_id: str = Field(..., description="UUID of primary master object")
    duplicate_id: str = Field(..., description="UUID of duplicate object to merge")
    strategy: str = Field(
        default="trust_based",
        description="Merge strategy: 'trust_based', 'primary_wins', or 'duplicate_wins'",
    )
    actor_id: str = Field(default="admin")


@router.get("/api/mdm/duplicates/detect")
async def detect_duplicates(
    type_id: Optional[str] = Query(default=None, description="Filter by type_id"),
    org_id: Optional[str] = Query(default=None, description="Filter by org_id"),
    threshold: float = Query(default=0.70, ge=0.0, le=1.0, description="Similarity threshold (0.0 to 1.0)"),
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    """Scan MDM Holding for duplicate clusters using fuzzy matching, code similarity, and XREF overlap."""
    engine = DeduplicationEngine(session)
    clusters = await engine.detect_duplicates(type_id=type_id, org_id=org_id, threshold=threshold)
    return {
        "status": "ok",
        "clusters_count": len(clusters),
        "clusters": clusters,
    }


@router.post("/api/mdm/duplicates/merge")
async def merge_duplicates(
    payload: MergeRequest,
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    """Merge duplicate object into primary object with EAV property consolidation and EBOM rebinding."""
    engine = DeduplicationEngine(session)
    try:
        res = await engine.merge_objects(
            primary_id=payload.primary_id,
            duplicate_id=payload.duplicate_id,
            merge_strategy=payload.strategy,
            actor_id=payload.actor_id,
        )
        return res
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Internal deduplication error: {e}")
