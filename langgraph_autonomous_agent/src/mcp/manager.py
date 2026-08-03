"""MCP (Model Context Protocol) client — connects to SSE MCP servers."""
from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool

log = logging.getLogger(__name__)
_cache: list[BaseTool] | None = None


class MCPConnection:
    def __init__(self, name: str, url: str):
        self.name, self.url = name, url.rstrip("/")
        self.tools_info: list[dict] = []
        self._ok = False

    async def connect(self) -> bool:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self.url}/tools",
                                 timeout=aiohttp.ClientTimeout(total=10)) as r:
                    if r.status == 200:
                        data = await r.json()
                        self.tools_info = data if isinstance(data, list) else data.get("tools", [])
            self._ok = True
            log.info("MCP[%s]: %d tools", self.name, len(self.tools_info))
            return True
        except Exception as e:
            log.warning("MCP[%s] failed: %s", self.name, e)
            return False

    async def call(self, tool_name: str, args: dict[str, Any]) -> str:
        try:
            import aiohttp
            async with aiohttp.ClientSession() as s:
                async with s.post(self.url, json={"method": "tools/call",
                                                  "params": {"name": tool_name, "arguments": args}},
                                  timeout=aiohttp.ClientTimeout(total=60)) as r:
                    data = await r.json()
                    if isinstance(data, dict):
                        content = data.get("result", data.get("content", data))
                        if isinstance(content, list):
                            return "\n".join(c.get("text", str(c)) for c in content)
                        return str(content)
                    return str(data)
        except Exception as e:
            return f"MCP error: {e}"


def _make_tool(conn: MCPConnection, info: dict) -> BaseTool:
    name = info.get("name", "unknown")
    desc = info.get("description", f"MCP tool: {name}")

    async def _run(**kw):
        return await conn.call(name, kw)

    return StructuredTool(name=f"mcp_{name}", description=f"[MCP:{conn.name}] {desc}", coroutine=_run)


async def get_mcp_tools() -> list[BaseTool]:
    global _cache
    if _cache is not None:
        return _cache
    from src.config import get_settings
    s = get_settings()
    tools: list[BaseTool] = []
    if s.MCP_SEARCH_URL:
        c = MCPConnection("search", s.MCP_SEARCH_URL)
        if await c.connect():
            if c.tools_info:
                tools.extend(_make_tool(c, t) for t in c.tools_info)
            else:
                from langchain_core.tools import tool as _t

                @_t
                async def mcp_web_search(query: str) -> str:
                    """Search the web via MCP server."""
                    return await c.call("search", {"query": query})
                tools.append(mcp_web_search)
    _cache = tools
    return tools


def reset_cache():
    global _cache
    _cache = None
