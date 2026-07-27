#!/usr/bin/env python3
"""Поддельный MCP-сервер для тестов: настоящий протокол, фиктивные данные.

Нужен, чтобы проверять клиент на реальном JSON-RPC, а не на заглушках
внутри Python. Умеет два транспорта: stdio (по умолчанию) и http.

Запуск:
  python3 fake_mcp_server.py            # stdio
  python3 fake_mcp_server.py --http 8123
"""
from __future__ import annotations

import json
import sys
import time

TOOLS = [
    {
        "name": "search",
        "description": "Поиск в интернете",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"},
                           "count": {"type": "integer"}},
            "required": ["query"],
        },
    },
    {
        "name": "fetch",
        "description": "Загрузить страницу",
        "inputSchema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "boom",
        "description": "Всегда возвращает ошибку — для проверки обработки",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

CALLS: list[float] = []


def handle(msg: dict) -> dict | None:
    mid = msg.get("id")
    method = msg.get("method")

    if method == "initialize":
        return {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake-mcp", "version": "1.0"},
        }}
    if method == "notifications/initialized":
        return None
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": mid, "result": {"tools": TOOLS}}
    if method == "tools/call":
        p = msg.get("params") or {}
        name = p.get("name")
        args = p.get("arguments") or {}
        CALLS.append(time.time())
        if name == "search":
            q = args.get("query", "")
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [
                {"type": "text",
                 "text": f"Результаты по «{q}»:\n1. Первый\n2. Второй"}]}}
        if name == "fetch":
            return {"jsonrpc": "2.0", "id": mid, "result": {"content": [
                {"type": "text",
                 "text": f"Содержимое {args.get('url', '')}: пример текста"}]}}
        if name == "boom":
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "isError": True,
                "content": [{"type": "text", "text": "намеренный сбой"}]}}
        return {"jsonrpc": "2.0", "id": mid,
                "error": {"code": -32601, "message": f"нет инструмента {name}"}}
    return {"jsonrpc": "2.0", "id": mid,
            "error": {"code": -32601, "message": f"нет метода {method}"}}


def run_stdio() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        resp = handle(msg)
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


def run_http(port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length", 0))
            msg = json.loads(self.rfile.read(n).decode())
            resp = handle(msg)
            body = (json.dumps(resp, ensure_ascii=False).encode()
                    if resp is not None else b"{}")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


if __name__ == "__main__":
    if "--http" in sys.argv:
        run_http(int(sys.argv[sys.argv.index("--http") + 1]))
    else:
        run_stdio()
