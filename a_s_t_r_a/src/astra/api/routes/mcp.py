"""MCP servers management API — CRUD + live status."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from astra.api.deps import db_session
from astra.auth.jwt import get_current_user, get_safe_owner_id
from astra.db.models import MCPServerConfig, User
from astra.mcp.tool_registry import tool_registry

router = APIRouter(prefix="/api/mcp", tags=["mcp"])


class MCPServerCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100, pattern=r"^[a-zA-Z0-9_\-]+$")
    url: str = Field(..., min_length=10, max_length=500)
    description: str = ""
    enabled: bool = True


class MCPServerOut(BaseModel):
    id: uuid.UUID
    name: str
    url: str
    description: str | None
    enabled: bool
    connected: bool = False
    tools: list[dict] = []

    model_config = {"from_attributes": True}


@router.get("/servers")
async def list_mcp_servers(db: AsyncSession = Depends(db_session)) -> list[dict]:
    result = await db.execute(select(MCPServerConfig).order_by(MCPServerConfig.created_at.desc()))
    db_servers = result.scalars().all()

    live = {}
    for name, client in tool_registry._clients.items():
        live[name] = {
            "connected": getattr(client, "_session", None) is not None,
            "tools": [{"name": t["name"], "description": t.get("description", "")} for t in getattr(client, "_tools", [])],
            "url": getattr(client, "server_url", ""),
        }

    out: list[dict] = []
    for name, info in live.items():
        db_match = next((s for s in db_servers if s.name == name), None)
        out.append(
            {
                "id": str(db_match.id) if db_match else f"env-{name}",
                "name": name,
                "url": info["url"],
                "description": db_match.description if db_match else "From env / live",
                "enabled": db_match.enabled if db_match else True,
                "connected": info["connected"],
                "tools": info["tools"],
                "source": "db" if db_match else "env",
            }
        )

    for srv in db_servers:
        if srv.name not in live:
            out.append(
                {
                    "id": str(srv.id),
                    "name": srv.name,
                    "url": srv.url,
                    "description": srv.description,
                    "enabled": srv.enabled,
                    "connected": False,
                    "tools": [],
                    "source": "db",
                }
            )

    return out


@router.post("/servers", response_model=dict, status_code=201)
async def create_mcp_server(
    body: MCPServerCreate,
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
):
    existing = await db.execute(select(MCPServerConfig).where(MCPServerConfig.name == body.name))
    if existing.scalar_one_or_none():
        raise HTTPException(400, f"Server name '{body.name}' already exists")

    srv = MCPServerConfig(
        name=body.name,
        url=body.url,
        description=body.description,
        enabled=body.enabled,
        owner_id=get_safe_owner_id(current_user),
    )
    db.add(srv)
    await db.flush()

    connect_result = {"connected": False, "tools": []}
    if body.enabled:
        try:
            connect_result = await tool_registry.add_server(body.name, body.url)
        except Exception as exc:
            connect_result = {"connected": False, "error": str(exc), "tools": []}

    return {
        "id": str(srv.id),
        "name": srv.name,
        "url": srv.url,
        "description": srv.description,
        "enabled": srv.enabled,
        "connected": connect_result.get("connected", False),
        "tools": connect_result.get("tools", []),
    }


@router.delete("/servers/{server_id}", status_code=204)
async def delete_mcp_server(
    server_id: str,
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
):
    if server_id.startswith("env-"):
        raise HTTPException(400, "Cannot delete env-configured server, remove it from .env")

    try:
        uid = uuid.UUID(server_id)
    except ValueError:
        raise HTTPException(400, "Invalid server id")

    srv = await db.get(MCPServerConfig, uid)
    if not srv:
        raise HTTPException(404, "Server not found")

    try:
        await tool_registry.remove_server(srv.name)
    except Exception:
        pass

    await db.delete(srv)
    await db.flush()
    return None


@router.post("/servers/{server_id}/connect", response_model=dict)
async def connect_mcp_server(
    server_id: str,
    db: AsyncSession = Depends(db_session),
    current_user: User = Depends(get_current_user),
):
    if server_id.startswith("env-"):
        name = server_id[4:]
        for n, client in tool_registry._clients.items():
            if n == name:
                return {
                    "name": n,
                    "connected": getattr(client, "_session", None) is not None,
                    "tools": [{"name": t["name"], "description": t.get("description", "")} for t in getattr(client, "_tools", [])],
                }
        raise HTTPException(404, "Env server not found in live registry")

    try:
        uid = uuid.UUID(server_id)
    except ValueError:
        raise HTTPException(400, "Invalid server id")

    srv = await db.get(MCPServerConfig, uid)
    if not srv:
        raise HTTPException(404, "Server not found")

    result = await tool_registry.add_server(srv.name, srv.url)
    return {"id": str(srv.id), "name": srv.name, "url": srv.url, **result}


@router.get("/servers/{server_id}/tools")
async def get_server_tools(server_id: str, db: AsyncSession = Depends(db_session)) -> list[dict]:
    if server_id.startswith("env-"):
        name = server_id[4:]
        client = tool_registry._clients.get(name)
        if not client:
            return []
        return [{"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("input_schema", {})} for t in getattr(client, "_tools", [])]

    try:
        uid = uuid.UUID(server_id)
    except ValueError:
        raise HTTPException(400, "Invalid server id")

    srv = await db.get(MCPServerConfig, uid)
    if not srv:
        raise HTTPException(404, "Server not found")

    client = tool_registry._clients.get(srv.name)
    if not client:
        return []

    return [{"name": t["name"], "description": t.get("description", ""), "input_schema": t.get("input_schema", {})} for t in getattr(client, "_tools", [])]
