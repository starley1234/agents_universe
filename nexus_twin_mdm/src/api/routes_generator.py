"""REST API routes for LLM Synthetic Testing Mode — Enterprise Generator."""
from __future__ import annotations

from typing import Any, Dict, List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.engine import get_session
from src.db.generator import SyntheticEnterpriseGenerator

router = APIRouter(tags=["Synthetic Enterprise Generator"])


class SynthesizeRequest(BaseModel):
    description: str = Field(
        ...,
        min_length=3,
        description="Natural language description of fictional enterprise to synthesize",
    )
    enterprise_code: Optional[str] = Field(
        default=None,
        description="Optional enterprise code prefix (e.g. TITAN, ARCTIC, ROBO, ORBIT)",
    )
    include_duplicates: bool = Field(
        default=True,
        description="Generate duplicate candidate pairs for deduplication testing",
    )
    actor_id: str = Field(default="llm_synthesizer")


@router.get("/api/mdm/synthesize/templates")
async def get_synthesis_templates() -> Dict[str, Any]:
    """Return ready-to-use fictional enterprise description templates."""
    return {
        "status": "ok",
        "templates_count": 4,
        "templates": [
            {
                "id": "titan_uav",
                "code": "TITAN",
                "title": "✈️ Авиастроительный завод 'Небесный Титан'",
                "description": "Авиастроительный завод 'Небесный Титан' — производство беспилотных систем, электродвигателей и авионики. Имеет КБ, сборочный цех и летный полигон.",
            },
            {
                "id": "arctic_marine",
                "code": "ARCTIC",
                "title": "🚢 Судостроительный комплекс 'Арктика-Марин'",
                "description": "Судостроительный комплекс 'Арктика-Марин' — атомные ледоколы, морские газотурбинные агрегаты и радионавигационное оборудование.",
            },
            {
                "id": "robo_2030",
                "code": "ROBO",
                "title": "🤖 Завод робототехники 'РобоТех-2030'",
                "description": "Завод промышленной робототехники 'РобоТех-2030' — 6-осевые манипуляторы, сервоприводы высокой точности и 3D-стереокамеры машинного зрения.",
            },
            {
                "id": "orbit_space",
                "code": "ORBIT",
                "title": "🛰️ Космическая корпорация 'Орбита-Космос'",
                "description": "Корпорация 'Орбита-Космос' — спутники связи низкоорбитальной группировки, ионные двигатели коррекции и солнечные панели GaAs.",
            },
        ],
    }


@router.post("/api/mdm/synthesize")
async def synthesize_fictional_enterprise(
    payload: SynthesizeRequest,
    session: AsyncSession = Depends(get_session),
) -> Dict[str, Any]:
    """Synthesize a complete Digital Twin of a fictional enterprise from description."""
    gen = SyntheticEnterpriseGenerator(session)
    try:
        res = await gen.synthesize_enterprise(
            description=payload.description,
            enterprise_code=payload.enterprise_code,
            include_duplicates=payload.include_duplicates,
            actor_id=payload.actor_id,
        )
        return res
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Error synthesizing fictional enterprise: {exc}",
        )
