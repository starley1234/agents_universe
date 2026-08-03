"""Dynamic tool registry — env + DB MCP servers, auto-reconnect."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from loguru import logger

from astra.config import settings
from astra.mcp.client import MCPClient


class ToolRegistry:
    def __init__(self) -> None:
        self._clients: dict[str, MCPClient] = {}
        self._tool_to_server: dict[str, str] = {}

    async def init_global_servers(self) -> None:
        servers: list[tuple[str, str | None, str | None]] = []  # name, url, description
        if settings.mcp_search_url:
            servers.append(("search", settings.mcp_search_url, "Search MCP from env"))
        if settings.mcp_image_gen_url:
            servers.append(("image_gen", settings.mcp_image_gen_url, "ImageGen MCP from env"))
        if settings.mcp_tts_url:
            servers.append(("tts", settings.mcp_tts_url, "TTS MCP from env"))

        # Also load from DB (dynamic servers configured via UI)
        try:
            from astra.db.engine import get_session
            from sqlalchemy import select
            from astra.db.models import MCPServerConfig

            async with get_session() as db:
                result = await db.execute(select(MCPServerConfig).where(MCPServerConfig.enabled == True))  # noqa: E712
                db_servers = result.scalars().all()
                for srv in db_servers:
                    # Avoid duplicate names
                    if srv.name not in [s[0] for s in servers]:
                        servers.append((srv.name, srv.url, srv.description))
        except Exception as exc:
            logger.debug("Could not load MCP servers from DB: {}", exc)

        for name, url, desc in servers:
            if not url:
                continue
            client = MCPClient(url, name=name)
            connected = await client.connect()
            if connected:
                self._clients[name] = client
                tools = await client.list_tools()
                for t in tools:
                    self._tool_to_server[t["name"]] = name
                logger.info("MCP '{}' ({}): {} tools", name, desc or url, len(tools))
            else:
                logger.warning("MCP '{}' unavailable at {} — skipped", name, url)

    async def add_server(self, name: str, url: str) -> dict[str, Any]:
        """Dynamically add and connect a new MCP server."""
        if name in self._clients:
            # Disconnect old
            try:
                await self._clients[name].disconnect()
            except Exception:
                pass

        client = MCPClient(url, name=name)
        connected = await client.connect()
        if not connected:
            return {"connected": False, "tools": [], "error": "Connection failed"}

        self._clients[name] = client
        tools = await client.list_tools()
        for t in tools:
            self._tool_to_server[t["name"]] = name

        logger.info("MCP '{}' dynamically added: {} tools", name, len(tools))
        return {"connected": True, "tools": tools, "name": name, "url": url}

    async def remove_server(self, name: str) -> bool:
        client = self._clients.pop(name, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
            # Remove tool mappings
            to_remove = [tool for tool, srv in self._tool_to_server.items() if srv == name]
            for tool in to_remove:
                self._tool_to_server.pop(tool, None)
            logger.info("MCP '{}' removed", name)
            return True
        return False

    async def get_tools_for_project(self, project_id: UUID) -> list[dict[str, Any]]:
        all_tools: list[dict[str, Any]] = []
        for client in self._clients.values():
            for t in client._tools:
                all_tools.append(
                    {
                        "type": "function",
                        "function": {
                            "name": t["name"],
                            "description": t["description"],
                            "parameters": t.get("input_schema", {}),
                        },
                    }
                )
        return all_tools

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        server_name = self._tool_to_server.get(tool_name)
        if not server_name:
            raise ValueError(f"Unknown tool: {tool_name}. Available: {list(self._tool_to_server)}")
        client = self._clients.get(server_name)
        if not client:
            raise ValueError(f"MCP server '{server_name}' not connected")
        return await client.call_tool(tool_name, arguments)

    async def shutdown(self) -> None:
        for client in self._clients.values():
            try:
                await client.disconnect()
            except Exception:
                pass
        self._clients.clear()
        self._tool_to_server.clear()


tool_registry = ToolRegistry()
