"""Универсальный клиент HTTP и REST API (http.*).

Позволяет агенту обращаться к сторонним веб-сервисам и REST API
с поддержкой различных методов (GET, POST, PUT, DELETE) и заголовков.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..core import Tool, ToolError


def build_http_tools() -> list[Tool]:
    """Собрать инструменты для вызовов внешних REST API и HTTP-сервисов."""

    def http_request(
        method: str = "GET",
        url: str = "",
        headers_json: str = "{}",
        body: str = "",
        timeout: int = 5,
    ) -> str:
        if not url:
            raise ToolError("URL не может быть пустым")
        method_upper = (method or "GET").upper()
        if method_upper not in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            raise ToolError(f"Метод {method!r} не поддерживается")

        if url.startswith("mock://") or "mock.api" in url:
            return (
                f"### [MOCK HTTP] {method_upper} {url}\n"
                f"Статус: 200 OK\n"
                f"Ответ JSON:\n"
                f'{{"success": true, "message": "Вызов выполнен успешно"}}'
            )

        try:
            headers_dict = json.loads(headers_json) if headers_json else {}
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON в headers_json: {exc}") from exc

        data_bytes = body.encode("utf-8") if body and method_upper in ("POST", "PUT", "PATCH") else None

        try:
            req = urllib.request.Request(
                url,
                data=data_bytes,
                headers=headers_dict,
                method=method_upper,
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw_resp = resp.read().decode("utf-8", errors="replace")
                status = getattr(resp, "status", 200)
                return f"### HTTP {status} ({url}):\n{raw_resp}"
        except urllib.error.HTTPError as exc:
            return f"### HTTP Ошибка {exc.code} ({url}):\n{exc.read().decode('utf-8', errors='replace')}"
        except (urllib.error.URLError, OSError) as exc:
            raise ToolError(f"Ошибка HTTP запроса {method_upper} {url}: {exc}") from exc

    return [
        Tool(
            name="http.request",
            description="Отправить HTTP/REST запрос (GET, POST, PUT, DELETE) к внешнему API.",
            parameters={
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "description": "HTTP метод (GET, POST, PUT, DELETE)",
                    },
                    "url": {"type": "string", "description": "Целевой URL"},
                    "headers_json": {
                        "type": "string",
                        "description": 'JSON-объект заголовков (например, \'{"Authorization": "Bearer token"}\')',
                    },
                    "body": {
                        "type": "string",
                        "description": "Тело запроса (для POST/PUT)",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Таймаут в секундах (по умолчанию 5)",
                    },
                },
                "required": ["method", "url"],
            },
            fn=http_request,
            skills=["http", "api", "rest", "integrations", "web"],
            attributes={
                "category": "integration",
                "read_only": False,
                "dangerous": False,
                "requires_network": True,
                "resource_type": "http_request",
                "speed": "medium",
                "tags": ["http", "api", "rest", "request", "web", "get", "post"],
            },
            example='http.request(method="GET", url="https://api.example.com/status")',
        ),
    ]
