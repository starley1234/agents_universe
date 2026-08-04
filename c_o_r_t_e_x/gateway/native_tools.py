"""Небольшие first-party tools C.O.R.T.E.X."""
from __future__ import annotations

import socket
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from typing import Any

from ..signals import ToolDescriptor
from .toolkit_client import ToolkitUnavailable


class CortexNativeProvider:
    name = "cortex-native"
    endpoint = "local://cortex"

    def __init__(self, settings: Any) -> None:
        self.settings = settings
        self.allow_hosts = {host.strip().lower() for host in getattr(settings, "fetch_allow_hosts", ()) if host.strip()}

    def list_tools(self) -> list[ToolDescriptor]:
        return [ToolDescriptor(
            name="cortex.fetch",
            description="Получить небольшой текстовый HTTP(S) ресурс для research workflow.",
            input_schema={
                "type": "object", "properties": {
                    "url": {"type": "string", "description": "HTTP(S) URL"},
                    "max_bytes": {"type": "integer", "default": 65536, "minimum": 1, "maximum": 262144},
                }, "required": ["url"],
            },
            skills=["http", "fetch", "research"],
            attributes={"category": "cortex-native", "read_only": True, "dangerous": False, "tags": ["fetch", "http"]},
        )]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name != "cortex.fetch":
            raise ToolkitUnavailable(f"Неизвестный C.O.R.T.E.X. tool: {name}")
        return self.fetch(str(arguments.get("url", "")), int(arguments.get("max_bytes", 65536)))

    def fetch(self, url: str, max_bytes: int = 65536) -> dict[str, Any]:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("cortex.fetch принимает только http(s) URL")
        if getattr(self.settings, "environment", "development").lower() == "production" and self.allow_hosts and parsed.hostname.lower() not in self.allow_hosts:
            raise PermissionError(f"Host {parsed.hostname!r} отсутствует в CORTEX_FETCH_ALLOW_HOSTS")
        if getattr(self.settings, "environment", "development").lower() == "production" and not self.allow_hosts:
            raise PermissionError("cortex.fetch отключён в production без CORTEX_FETCH_ALLOW_HOSTS")
        max_bytes = max(1, min(max_bytes, 262144))
        # Resolve once to make the allowlist/diagnostic result explicit. Full
        # enterprise SSRF policy should live in the egress proxy.
        try:
            socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ToolkitUnavailable(f"Host resolution failed: {exc}") from exc
        request = urllib.request.Request(url, headers={"User-Agent": "C.O.R.T.E.X./0.1"}, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                content = response.read(max_bytes)
                return {
                    "url": url,
                    "status": getattr(response, "status", 200),
                    "content_type": response.headers.get("content-type", ""),
                    "truncated": len(content) >= max_bytes,
                    "body": content.decode("utf-8", errors="replace"),
                }
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ToolkitUnavailable(f"fetch failed: {exc}") from exc

    def health(self) -> dict[str, Any]:
        return {"status": "ok", "mode": "native", "tools_count": 1, "endpoint": self.endpoint}


__all__ = ["CortexNativeProvider"]
