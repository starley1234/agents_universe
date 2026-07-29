"""Tool Integration: инструменты как системные вызовы среды."""
from __future__ import annotations

from .base import Tool, ToolError, ToolRegistry, Workspace
from .protocol import (ProtocolError, ToolCall, extract_call, is_final,
                       protocol_prompt, strip_calls)
from .registry import build_registry, granted_summary

__all__ = ["Tool", "ToolError", "ToolRegistry", "Workspace", "ToolCall",
           "ProtocolError", "extract_call", "strip_calls", "is_final",
           "protocol_prompt", "build_registry", "granted_summary"]
