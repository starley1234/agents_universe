"""Dynamic tool registry — maps tool names to their MCP server and
converts MCP tool schemas into OpenAI-compatible format for LiteLLM.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from loguru import logger

from astra.config import settings
from astra.mcp.client import MCPClient


class ToolRegistry:
    """Manages MCP clients and provides tools per project."""

    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        # tool_name → server_name mapping for call routing
        self._tool_to_server: dict[str, str] = {}

    async def init_global_servers(self) -> None:
        """Connect to globally configured MCP servers from settings."""
        servers: list[tuple[str, str | None]] = []
        if settings.mcp_search_url:
            servers.append(("search", settings.mcp_search_url))
        if settings.mcp_image_gen_url:
            servers.append(("image_gen", settings.mcp_image_gen_url))
        if settings.mcp_tts_url:
            servers.append(("tts", settings.mcp_tts_url))

        for name, url in servers:
            if not url:
                continue
            client = MCPClient(url, name=name)
            connected = await client.connect()
            if connected:
                self._clients[name] = client
                tools = await client.list_tools()
                for t in tools:
                    self._tool_to_server[t["name"]] = name
                logger.info("MCP '{}': {} tools registered", name, len(tools))
            else:
                logger.warning("MCP '{}' unavailable — tools from this server will be skipped", name)

    async def get_tools_for_project(self, project_id: UUID) -> list[dict[str, Any]]:
        """Return all tools in OpenAI function-calling format."""
        all_tools: list[dict[str, Any]] = []
        for client in self._clients.values():
            for t in client._tools:
                all_tools.append({
                    "type": "function",
                    "function": {
                        "name": t["name"],
                        "description": t["description"],
                        "parameters": t.get("input_schema", {}),
                    },
                })
        return all_tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """Route a tool call to the right MCP server."""
        server_name = self._tool_to_server.get(tool_name)
        if not server_name:
            raise ValueError(f"Unknown tool: {tool_name}.  Available: {list(self._tool_to_server)}")
        client = self._clients.get(server_name)
        if not client:
            raise ValueError(f"MCP server '{server_name}' not connected")
        return await client.call_tool(tool_name, arguments)

    async def shutdown(self) -> None:
        for client in self._clients.values():
            await client.disconnect()
        self._clients.clear()
        self._tool_to_server.clear()


tool_registry = ToolRegistry()
