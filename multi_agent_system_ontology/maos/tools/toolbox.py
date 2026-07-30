"""Сборка инструментов конкретного агента по строке agent.tools (БД).

Формат поля agent.tools — через запятую, например "files,web,rag":
  files  — чтение/запись/точечная правка файлов в изолированной рабочей
           папке агента (перенесено из agent_system/agent/tools/files.py)
  web    — поиск (DuckDuckGo) и загрузка страниц, с защитой от SSRF
           (agent_system/agent/tools/web.py, без изменений в логике)
  office — создание Word/Excel/PowerPoint из markdown/JSON
           (agent_system/agent/tools/office_docs.py)
  rag    — индексация текста и гибридный поиск (векторный + FTS) на
           том же PostgreSQL+pgvector, что и остальная память MAOS
           (maos/skills/rag.py — адаптация agent_system/agent/skills/rag.py
           под единственный бэкенд PostgreSQL)

Каждый навык — ОТДЕЛЬНАЯ рабочая папка на агента: workspace_root/<slug>,
не общая для всех личностей — агент А не должен видеть файлы агента Б.

Неизвестное имя в agent.tools — явная ошибка при сборке (ToolboxError),
а не молчаливый пропуск: опечатка в списке навыков не должна тихо
оставить агента без инструмента, который человек рассчитывал дать ему.
"""
from __future__ import annotations

from pathlib import Path

from ..config import Config
from ..llm.embeddings import BaseEmbedder
from ..memory.store import Store
from .base import Tool, Workspace


class ToolboxError(RuntimeError):
    """Ошибка сборки набора инструментов: неизвестный навык и т.п."""


KNOWN_TOOLS = ("files", "web", "office", "rag", "mcp", "messaging", "site_qa", "vision")


def parse_tools_field(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def agent_workspace(cfg: Config, slug: str) -> Workspace:
    """Изолированная рабочая папка агента — своя подпапка на slug, та же
    защита от выхода за пределы, что и в agent_system (Workspace.resolve)."""
    root = Path(cfg.workspace_root).expanduser() / slug
    return Workspace(root)

def build_toolbox(cfg: Config, agent_row: dict, store: Store | None = None,
                  embedder: BaseEmbedder | None = None) -> list[Tool]:
    """Собирает список Tool для агента по его agent_row['tools']."""
    names = parse_tools_field(agent_row.get("tools") or "")
    unknown = [n for n in names if n not in KNOWN_TOOLS]
    if unknown:
        raise ToolboxError(
            f"Неизвестные навыки в agent.tools: {', '.join(unknown)}. "
            f"Доступны: {', '.join(KNOWN_TOOLS)}")
    if not names:
        return []

    ws = agent_workspace(cfg, agent_row["slug"])
    tools: list[Tool] = []
    seen_names: set[str] = set()

    def _extend(new_tools: list[Tool]) -> None:
        for t in new_tools:
            if t.name in seen_names:
                continue
            seen_names.add(t.name)
            tools.append(t)

    if "files" in names:
        from . import files
        _extend(files.build(ws))
    if "web" in names:
        from . import web
        _extend(web.build(ws, cfg.resolve_web_config()))
    if "office" in names:
        from . import office_docs
        _extend(office_docs.build(ws))
    if "mcp" in names:
        from ..mcp import MCPPool, configs_from_dict
        configs = configs_from_dict(cfg.mcp_servers or {})
        if not configs:
            raise ToolboxError("Навык 'mcp' включён, но MAOS_MCP_SERVERS не задан")
        # MCPPool сохраняется вместе с замыканиями инструментов на время хода.
        _extend(MCPPool(configs).tools())
    if "messaging" in names:
        from .messaging import EmailConfig, MaxConfig, MessagingConfig, build as build_messaging
        msg_cfg = MessagingConfig(
            email=EmailConfig(smtp_host=cfg.smtp_host, smtp_port=cfg.smtp_port,
                              smtp_user=cfg.smtp_user, smtp_password=cfg.smtp_password,
                              smtp_use_ssl=cfg.smtp_use_ssl, smtp_starttls=cfg.smtp_starttls,
                              from_addr=cfg.smtp_from_addr),
            max=MaxConfig(bot_token=cfg.max_bot_token, api_base=cfg.max_api_base),
            confirm_sends=cfg.messaging_confirm_sends)
        _extend(build_messaging(ws, msg_cfg))
    if "rag" in names:
        if store is None or embedder is None:
            raise ToolboxError(
                "Навык 'rag' требует подключённый Store и эмбеддер — "
                "передайте их в build_toolbox(store=..., embedder=...)")
        from ..skills import rag
        _extend(rag.build(ws, store, embedder))
    if "site_qa" in names:
        from . import site_qa
        _extend(site_qa.build(ws))
    if "vision" in names:
        from . import vision
        _extend(vision.build(ws, cfg))
    return tools
