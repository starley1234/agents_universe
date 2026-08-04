"""Model Context Protocol (MCP) Server for NexusTwin MDM & Certification."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.tools import (
    tool_get_object_bom,
    tool_get_org_hierarchy,
    tool_search_mdm_objects,
    tool_synthesize_enterprise,
    tool_upsert_object_property,
    tool_verify_compliance_chain,
)


class MCPServer:
    """Implements JSON-RPC 2.0 and SSE MCP server endpoints for NexusTwin MDM."""

    def __init__(self):
        self.server_name = "NexusTwin MDM MCP Server"
        self.server_version = "1.0.0"

    def list_tools(self) -> List[Dict[str, Any]]:
        """Return list of available MCP tools matching Holding MDM schema."""
        return [
            {
                "name": "mdm.search_objects",
                "description": "Search Holding MDM Digital Twin objects by code, name, or type.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Search text"},
                        "type_id": {"type": "string", "description": "Optional type filter (part, engine, cert_req)"},
                    },
                },
            },
            {
                "name": "mdm.get_bom_graph",
                "description": "Get EBOM / MBOM engineering bill of materials graph for an object.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "object_id": {"type": "string", "description": "Object ID or master code (e.g. ENG-500-MASTER)"},
                    },
                    "required": ["object_id"],
                },
            },
            {
                "name": "mdm.verify_compliance",
                "description": "Verify cryptographic SHA-256 baseline hash chain and aviation certification status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "object_id": {"type": "string", "description": "Object ID or master code"},
                    },
                    "required": ["object_id"],
                },
            },
            {
                "name": "mdm.upsert_property",
                "description": "Upsert an EAV property with trust score checking (0-100) and time-travel archiving.",
                "inputSchema": {
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
            {
                "name": "mdm.get_org_hierarchy",
                "description": "Retrieve Holding ltree organizational unit hierarchy.",
                "inputSchema": {"type": "object", "properties": {}},
            },
            {
                "name": "mdm.synthesize_enterprise",
                "description": "Synthesize a complete Digital Twin of a fictional enterprise from natural language description.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "description": {"type": "string", "description": "Text description of fictional enterprise"},
                        "include_duplicates": {"type": "boolean", "default": True},
                    },
                    "required": ["description"],
                },
            },
            {
                "name": "mdm.agent_query",
                "description": "Run the NexusTwin LangGraph AI agent to solve a complex MDM/Certification task.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Task description or question for the agent"},
                    },
                    "required": ["query"],
                },
            },
        ]

    async def call_tool(
        self, name: str, arguments: Dict[str, Any], session: AsyncSession
    ) -> Dict[str, Any]:
        """Execute requested MCP tool inside current database session."""
        logger.info(f"MCP Server execute tool [{name}] with arguments={arguments}")

        if name == "mdm.search_objects":
            res = await tool_search_mdm_objects(
                session,
                query=arguments.get("query", ""),
                type_id=arguments.get("type_id"),
            )
            return {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}]}

        elif name == "mdm.get_bom_graph":
            res = await tool_get_object_bom(
                session, object_id=arguments.get("object_id", "ENG-500-MASTER")
            )
            return {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}]}

        elif name == "mdm.verify_compliance":
            res = await tool_verify_compliance_chain(
                session, object_id=arguments.get("object_id", "ENG-500-MASTER")
            )
            return {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}]}

        elif name == "mdm.upsert_property":
            res = await tool_upsert_object_property(
                session,
                object_id=arguments.get("object_id", ""),
                key=arguments.get("key", ""),
                value=arguments.get("value", {}),
                source_id=arguments.get("source_id", "plm"),
            )
            return {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}]}

        elif name == "mdm.get_org_hierarchy":
            res = await tool_get_org_hierarchy(session)
            return {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}]}

        elif name == "mdm.synthesize_enterprise":
            res = await tool_synthesize_enterprise(
                session,
                description=arguments.get("description", ""),
                include_duplicates=arguments.get("include_duplicates", True),
            )
            return {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}]}

        elif name == "mdm.agent_query":
            from src.agent.graph import MDMAgentGraph

            agent = MDMAgentGraph()
            res = await agent.run(query=arguments.get("query", ""), session=session)
            return {"content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}]}

        else:
            return {
                "isError": True,
                "content": [{"type": "text", "text": f"Unknown MCP tool: {name}"}],
            }

    async def handle_rpc(self, payload: Dict[str, Any], session: AsyncSession) -> Dict[str, Any]:
        """Process JSON-RPC 2.0 request."""
        req_id = payload.get("id", 1)
        method = payload.get("method")
        params = payload.get("params", {})

        if method == "tools/list":
            return {
                "jsonrpc": "2.0",
                "result": {"tools": self.list_tools()},
                "id": req_id,
            }
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            try:
                result = await self.call_tool(name, arguments, session)
                return {"jsonrpc": "2.0", "result": result, "id": req_id}
            except Exception as exc:
                logger.error(f"MCP tool {name} failed: {exc}")
                return {
                    "jsonrpc": "2.0",
                    "error": {"code": -32603, "message": str(exc)},
                    "id": req_id,
                }
        else:
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32601, "message": f"Method not found: {method}"},
                "id": req_id,
            }


mcp_server = MCPServer()
