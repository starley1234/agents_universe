import json
from pathlib import Path
from typing import Any

from app.config import settings


def _registry_path() -> Path:
    root = Path(settings.workspace_path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root / "mcp_servers.json"


def _env_servers() -> list[dict[str, Any]]:
    candidates = []
    for name, url in [
        ("search", getattr(settings, "mcp_search_url", "")),
        ("agent_toolkit", getattr(settings, "mcp_agent_toolkit", "")),
    ]:
        if url:
            candidates.append({"name": name, "url": url, "transport": "sse", "enabled": True})
    return candidates


def load_global_mcp_servers() -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {server["name"]: server for server in _env_servers()}
    path = _registry_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for server in data if isinstance(data, list) else []:
                if isinstance(server, dict) and server.get("name"):
                    merged[server["name"]] = server
        except json.JSONDecodeError:
            pass
    return list(merged.values())


def save_global_mcp_servers(servers: list[dict[str, Any]]) -> None:
    path = _registry_path()
    path.write_text(json.dumps(servers, ensure_ascii=False, indent=2), encoding="utf-8")


def upsert_global_mcp_server(server: dict[str, Any]) -> list[dict[str, Any]]:
    servers = [item for item in load_global_mcp_servers() if item.get("name") != server.get("name")]
    servers.append(server)
    save_global_mcp_servers(servers)
    return servers


def delete_global_mcp_server(name: str) -> list[dict[str, Any]]:
    servers = [item for item in load_global_mcp_servers() if item.get("name") != name]
    save_global_mcp_servers(servers)
    return servers
