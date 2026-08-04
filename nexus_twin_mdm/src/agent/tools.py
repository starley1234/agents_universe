"""Agent and MCP Tools for MDM, Certification, and Digital Twin."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.deduplication import DeduplicationEngine
from src.db.generator import SyntheticEnterpriseGenerator
from src.db.models import MDMObject
from src.db.repository import MDMRepository


async def _resolve_object_id(session: AsyncSession, obj_ref: str) -> Optional[str]:
    """Resolve master_code or id to UUID string."""
    res = await session.execute(
        select(MDMObject.id).where(
            (MDMObject.id == obj_ref) | (MDMObject.master_code == obj_ref)
        )
    )
    return res.scalar_one_or_none()


async def tool_search_mdm_objects(
    session: AsyncSession, query: str = "", type_id: Optional[str] = None
) -> Dict[str, Any]:
    """Search Digital Twin objects in MDM by text query or type."""
    repo = MDMRepository(session)
    items = await repo.list_objects(search_query=query, type_id=type_id, limit=20)
    return {
        "count": len(items),
        "objects": [
            {
                "id": o["id"],
                "master_code": o["master_code"],
                "display_name": o["display_name"],
                "state": o["state"],
                "type_id": o["type_id"],
            }
            for o in items
        ],
    }


async def tool_get_object_bom(
    session: AsyncSession, object_id: str
) -> Dict[str, Any]:
    """Get EBOM / MBOM hierarchy for an object by ID or master code."""
    resolved_id = await _resolve_object_id(session, object_id)
    if not resolved_id:
        return {"error": f"Object not found: {object_id}"}

    repo = MDMRepository(session)
    bom = await repo.get_bom_tree(resolved_id)
    detail = await repo.get_object_detail(resolved_id)
    return {
        "object_id": resolved_id,
        "master_code": detail["master_code"] if detail else object_id,
        "display_name": detail["display_name"] if detail else "",
        "bom_items_count": len(bom),
        "bom_items": bom,
    }


async def tool_verify_compliance_chain(
    session: AsyncSession, object_id: str
) -> Dict[str, Any]:
    """Verify cryptographic baseline hash chain and certification compliance."""
    resolved_id = await _resolve_object_id(session, object_id)
    if not resolved_id:
        return {"error": f"Object not found: {object_id}"}

    repo = MDMRepository(session)
    chain = await repo.verify_baseline_chain(resolved_id)
    is_valid = all(b["status"] == "OK" for b in chain)
    return {
        "object_id": resolved_id,
        "chain_length": len(chain),
        "all_valid": is_valid,
        "baselines": chain,
    }


async def tool_upsert_object_property(
    session: AsyncSession,
    object_id: str,
    key: str,
    value: Dict[str, Any],
    source_id: str = "plm",
    uom_code: Optional[str] = None,
) -> Dict[str, Any]:
    """Upsert an EAV property with trust verification and time-travel archiving."""
    resolved_id = await _resolve_object_id(session, object_id)
    if not resolved_id:
        return {"error": f"Object not found: {object_id}"}

    repo = MDMRepository(session)
    res = await repo.upsert_property(
        object_id=resolved_id,
        key=key,
        value=value,
        source_id=source_id,
        uom_code=uom_code,
    )
    return {"status": res, "object_id": resolved_id, "key": key}


async def tool_get_org_hierarchy(session: AsyncSession) -> Dict[str, Any]:
    """Get holding organizational ltree structure."""
    repo = MDMRepository(session)
    orgs = await repo.get_org_units()
    return {"count": len(orgs), "org_units": orgs}


async def tool_query_external_mcp(
    url: Optional[str] = None, tool_name: str = "tools/list", arguments: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Query an external MCP server (Model Context Protocol) via JSON-RPC."""
    target_url = url or settings.mcp_agent_toolkit
    rpc_url = target_url.replace("/sse", "/rpc").replace("/mcp/sse", "/mcp/rpc")
    if "/rpc" not in rpc_url:
        rpc_url = f"{target_url.rstrip('/')}/api/mcp/rpc"

    payload = {
        "jsonrpc": "2.0",
        "method": tool_name,
        "params": arguments or {},
        "id": 1,
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.post(rpc_url, json=payload)
            if resp.status_code == 200:
                return {"status": "ok", "result": resp.json()}
            return {"status": "error", "code": resp.status_code, "text": resp.text}
        except Exception as exc:
            return {
                "status": "fallback",
                "message": f"External MCP server at {rpc_url} unreachable ({exc})",
                "note": "Using internal NexusTwin MDM tool processing.",
            }


async def tool_detect_duplicates(
    session: AsyncSession, type_id: Optional[str] = None, threshold: float = 0.70
) -> Dict[str, Any]:
    """Detect duplicate candidate clusters across MDM Holding."""
    engine = DeduplicationEngine(session)
    clusters = await engine.detect_duplicates(type_id=type_id, threshold=threshold)
    return {
        "status": "ok",
        "clusters_count": len(clusters),
        "clusters": clusters,
    }


async def tool_merge_duplicates(
    session: AsyncSession,
    primary_id: str,
    duplicate_id: str,
    strategy: str = "trust_based",
) -> Dict[str, Any]:
    """Merge a duplicate MDM object into a primary master object."""
    engine = DeduplicationEngine(session)
    try:
        res = await engine.merge_objects(
            primary_id=primary_id,
            duplicate_id=duplicate_id,
            merge_strategy=strategy,
            actor_id="ai_agent",
        )
        return {"status": "success", "merge_result": res}
    except Exception as exc:
        return {"status": "error", "message": str(exc)}


async def tool_synthesize_enterprise(
    session: AsyncSession,
    description: str,
    enterprise_code: Optional[str] = None,
    include_duplicates: bool = True,
) -> Dict[str, Any]:
    """Synthesize a complete Digital Twin of a fictional enterprise from description."""
    gen = SyntheticEnterpriseGenerator(session)
    res = await gen.synthesize_enterprise(
        description=description,
        enterprise_code=enterprise_code,
        include_duplicates=include_duplicates,
        actor_id="agent_synthesizer",
    )
    return {
        "status": "success",
        "enterprise_name": res["enterprise_name"],
        "objects_created": res["created_objects_count"],
        "bom_links_created": res["created_bom_links_count"],
        "duplicates_created": res["created_duplicates_count"],
    }


async def tool_audit_data_quality(session: AsyncSession) -> Dict[str, Any]:
    """Audit holding data quality metrics and duplicate clusters."""
    repo = MDMRepository(session)
    engine = DeduplicationEngine(session)
    objs = await repo.list_objects(limit=100)
    clusters = await engine.detect_duplicates(threshold=0.70)
    
    # Check compliance rate
    comp_ok = 0
    for o in objs[:10]:
        chain = await repo.verify_baseline_chain(o["id"])
        if all(b["status"] == "OK" for b in chain):
            comp_ok += 1
    comp_rate = int((comp_ok / max(1, len(objs[:10]))) * 100)

    quality_score = max(50, 100 - len(clusters) * 10)
    return {
        "total_active_objects": len(objs),
        "duplicate_clusters_count": len(clusters),
        "compliance_rate_percent": comp_rate,
        "data_quality_score": quality_score,
        "recommendation": (
            "Требуется слияние дубликатов" if len(clusters) > 0 else "Мастер-данные нормализованы"
        ),
    }


# Tool Schemas for OpenAI / LLM function calling
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_mdm_objects",
            "description": "Поиск объектов в Цифровом Двойнике МДМ по текстовому запросу или типу.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос по коду или имени"},
                    "type_id": {"type": "string", "description": "Опциональный фильтр по типу (part, engine, cert_req)"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_object_bom",
            "description": "Получить конструкторскую/производственную спецификацию (EBOM / MBOM) для объекта.",
            "parameters": {
                "type": "object",
                "properties": {
                    "object_id": {"type": "string", "description": "ID или master_code объекта (напр. ENG-500-MASTER)"},
                },
                "required": ["object_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_compliance_chain",
            "description": "Проверить криптографическую целостность цепочки бейслайнов и сертификационный статус объекта.",
            "parameters": {
                "type": "object",
                "properties": {
                    "object_id": {"type": "string", "description": "ID или master_code объекта"},
                },
                "required": ["object_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "upsert_object_property",
            "description": "Обновить или добавить EAV-свойство объекта с проверкой уровня доверия источника и time-travel архивированием.",
            "parameters": {
                "type": "object",
                "properties": {
                    "object_id": {"type": "string"},
                    "key": {"type": "string"},
                    "value": {"type": "object"},
                    "source_id": {"type": "string", "default": "plm"},
                },
                "required": ["object_id", "key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_org_hierarchy",
            "description": "Получить иерархию организационных единиц Холдинга.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "detect_duplicates",
            "description": "Поиск и обнаружение потенциальных дубликатов объектов в Холдинге МДМ по схожести имен, кодов и XREF.",
            "parameters": {
                "type": "object",
                "properties": {
                    "type_id": {"type": "string", "description": "Тип объекта для фильтрации (например part)"},
                    "threshold": {"type": "number", "description": "Порог схожести от 0.0 до 1.0", "default": 0.70},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "merge_duplicates",
            "description": "Слияние дубликата в мастер-объект с консолидацией EAV-свойств по Trust Score и перепривязкой спецификаций EBOM.",
            "parameters": {
                "type": "object",
                "properties": {
                    "primary_id": {"type": "string", "description": "ID мастер-объекта"},
                    "duplicate_id": {"type": "string", "description": "ID дубликата"},
                    "strategy": {"type": "string", "default": "trust_based"},
                },
                "required": ["primary_id", "duplicate_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "audit_data_quality",
            "description": "Аудит качества мастер-данных НСИ: подсчет активных объектов, дубликатов и процента соответствия АП-25.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "synthesize_enterprise",
            "description": "Режим тестирования: синтез Цифрового Двойника вымышленного предприятия по текстовому описанию.",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Описательный текст вымышленного предприятия"},
                    "include_duplicates": {"type": "boolean", "default": True},
                },
                "required": ["description"],
            },
        },
    },
]
