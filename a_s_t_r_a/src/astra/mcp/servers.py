"""MCP server configuration and registration."""

from __future__ import annotations

from pydantic import BaseModel


class MCPServerConfig(BaseModel):
    """Configuration for a single MCP server."""

    name: str
    url: str
    transport: str = "sse"          # "sse" | "stdio"
    enabled: bool = True
    description: str = ""


# Pre-defined server configs (can be extended via DB / YAML)
DEFAULT_SERVERS: list[MCPServerConfig] = [
    MCPServerConfig(
        name="search",
        url="http://localhost:8001/sse",
        description="Web search MCP server",
    ),
    # Uncomment when needed:
    # MCPServerConfig(
    #     name="image_gen",
    #     url="http://localhost:8002/sse",
    #     description="Image generation MCP server",
    # ),
    # MCPServerConfig(
    #     name="tts",
    #     url="http://localhost:8003/sse",
    #     description="Text-to-speech MCP server",
    # ),
]
