"""MCP client — connects to MCP servers via SSE and exposes tools.

Uses the official ``mcp`` SDK's ``sse_client`` async context manager
properly (no manual ``__aenter__``).
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from typing import Any

from loguru import logger
from mcp import ClientSession
from mcp.client.sse import sse_client


class MCPClient:
    """Thin wrapper around the official MCP Python SDK."""

    def __init__(self, server_url: str, name: str = "unnamed") -> None:
        self.server_url = server_url
        self.name = name
        self._exit_stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None
        self._tools: list[dict[str, Any]] = []

    async def connect(self) -> bool:
        """Establish SSE connection.  Returns True on success."""
        logger.info("Connecting to MCP server '{}' at {}", self.name, self.server_url)
        try:
            self._exit_stack = AsyncExitStack()
            read_stream, write_stream = await self._exit_stack.enter_async_context(
                sse_client(self.server_url, timeout=10, sse_read_timeout=300)
            )
            self._session = await self._exit_stack.enter_async_context(
                ClientSession(read_stream, write_stream)
            )
            await self._session.initialize()
            logger.info("✅  Connected to MCP server '{}'", self.name)
            return True
        except Exception as exc:
            logger.warning("Failed to connect to MCP '{}': {}", self.name, exc)
            if self._exit_stack:
                await self._exit_stack.aclose()
                self._exit_stack = None
            self._session = None
            return False

    async def list_tools(self) -> list[dict[str, Any]]:
        """Fetch available tools from the server."""
        if not self._session:
            return []
        try:
            result = await self._session.list_tools()
            self._tools = [
                {
                    "name": tool.name,
                    "description": tool.description or "",
                    "input_schema": tool.inputSchema if hasattr(tool, "inputSchema") else {},
                }
                for tool in result.tools
            ]
            logger.info("MCP '{}': {} tools available", self.name, len(self._tools))
            return self._tools
        except Exception as exc:
            logger.error("Failed to list tools from '{}': {}", self.name, exc)
            return []

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Invoke a tool on the MCP server."""
        if not self._session:
            raise RuntimeError(f"Not connected to MCP server '{self.name}'")
        logger.info("Calling tool '{}' on '{}'", name, self.name)
        result = await self._session.call_tool(name, arguments)
        # Extract text content from result
        if hasattr(result, "content") and result.content:
            parts = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)
                else:
                    parts.append(str(block))
            return "\n".join(parts)
        return str(result)

    async def disconnect(self) -> None:
        if self._exit_stack:
            await self._exit_stack.aclose()
            self._exit_stack = None
            self._session = None
            logger.info("Disconnected from MCP server '{}'", self.name)
