#!/usr/bin/env python3
"""MCP-сервер agent_toolkit для stdio-транспорта (LM Studio, Claude Desktop).

Запускается автоматически из mcp.json:
  "command": "python", "args": ["mcp_stdio.py"]

Обменивается JSON-RPC 2.0 сообщениями через stdin/stdout
по спецификации MCP (Model Context Protocol).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_toolkit import build_default_registry
from agent_toolkit.integrations.mcp import MCPServer


def main():
    registry = build_default_registry()
    server = MCPServer(registry=registry, server_name="agent-toolkit-mcp")

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = server.handle_rpc(request)
        sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
