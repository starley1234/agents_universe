"""Model Context Protocol (MCP) API routes for JSON-RPC and SSE."""
from __future__ import annotations

import json
from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.engine import get_session
from src.mcp.server import mcp_server

router = APIRouter(tags=["MCP Server"])


@router.get("/api/mcp/tools")
async def get_mcp_tools() -> Dict[str, Any]:
    """Return list of available MCP tools and OpenAI JSON-RPC schemas."""
    return {
        "server": mcp_server.server_name,
        "version": mcp_server.server_version,
        "tools_count": len(mcp_server.list_tools()),
        "tools": mcp_server.list_tools(),
    }


@router.post("/api/mcp/rpc")
async def mcp_jsonrpc_endpoint(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> JSONResponse:
    """Handle standard Model Context Protocol JSON-RPC 2.0 requests."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=400,
            content={
                "jsonrpc": "2.0",
                "error": {"code": -32700, "message": "Parse error: Invalid JSON"},
                "id": None,
            },
        )

    result = await mcp_server.handle_rpc(payload, session)
    return JSONResponse(content=result)


@router.get("/api/mcp/sse")
async def mcp_sse_endpoint() -> StreamingResponse:
    """Provide SSE endpoint discovery for MCP clients."""

    async def sse_gen():
        endpoint_info = {
            "jsonrpc_endpoint": "/api/mcp/rpc",
            "tools_endpoint": "/api/mcp/tools",
            "status": "ready",
        }
        yield f"event: endpoint\ndata: {json.dumps(endpoint_info)}\n\n"

    return StreamingResponse(
        sse_gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )
