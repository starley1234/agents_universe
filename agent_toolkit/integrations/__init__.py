"""Интеграционные инструменты: MCP, SMTP, MAX, Telegram, S3, изображения, деплой, Web, HTTP API, TTS, ERP OData."""
from __future__ import annotations

from .db import build_db_tools
from .deployment import build_deployment_tools
from .erp import build_erp_tools
from .http import build_http_tools
from .image_generation import build_image_generation_tools
from .max import build_max_tools
from .mcp import MCPClient, MCPServer, build_mcp_tools
from .s3 import build_s3_tools
from .smtp import build_smtp_tools
from .teamcenter import build_teamcenter_tools
from .telegram import build_telegram_tools
from .tts import build_tts_tools
from .web import build_web_tools

__all__ = [
    "build_db_tools",
    "build_mcp_tools",
    "build_smtp_tools",
    "build_max_tools",
    "build_telegram_tools",
    "build_s3_tools",
    "build_image_generation_tools",
    "build_deployment_tools",
    "build_web_tools",
    "build_http_tools",
    "build_tts_tools",
    "build_erp_tools",
    "build_teamcenter_tools",
    "MCPClient",
    "MCPServer",
]
