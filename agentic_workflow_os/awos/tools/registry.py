"""Сборка набора инструментов из грантов среды.

ЕДИНСТВЕННОЕ МЕСТО, где решается, что вообще существует в этой среде.
Никакой другой код не создаёт инструменты «по требованию»: если хоста
нет в `http_allow`, инструмента `http_request` не будет ни у одного
агента, независимо от того, что написано в его профиле. Профиль и шаг
workflow работают методом `ToolRegistry.subset()` — только сужение.
"""
from __future__ import annotations

from typing import Any, Callable

from ..config import Config
from .base import Tool, ToolRegistry, Workspace
from .builtin import (context_tools, file_tools, http_tool, now_tool,
                      shell_tool, sql_tool)


def build_registry(cfg: Config, *, workspace: Workspace | None = None,
                   ctx_read: Callable[[str], Any] | None = None,
                   ctx_write: Callable[[str, Any], None] | None = None,
                   ctx_keys: Callable[[], list[str]] | None = None,
                   extra: list[Tool] | None = None) -> ToolRegistry:
    """Инструменты, разрешённые ЭТОЙ средой. Гранты — только из конфига."""
    ws = workspace or Workspace(cfg.workspace)
    reg = ToolRegistry()

    for tool in file_tools(ws):
        reg.add(tool)
    reg.add(now_tool())

    if cfg.http_allow:
        reg.add(http_tool(cfg.http_allow, timeout=cfg.http_timeout))
    if cfg.sql_databases:
        reg.add(sql_tool(cfg.sql_databases))
    if cfg.allow_shell:
        reg.add(shell_tool(ws, timeout=cfg.shell_timeout))

    if ctx_read and ctx_write and ctx_keys:
        for tool in context_tools(ctx_read, ctx_write, ctx_keys):
            reg.add(tool)

    for tool in extra or []:
        reg.add(tool)
    return reg


def granted_summary(cfg: Config) -> dict[str, Any]:
    """Что среда выдала — для `awos check`, дашборда и журнала прогона."""
    return {
        "files": True,
        "http": sorted(h.lower() for h in cfg.http_allow),
        "sql": sorted(cfg.sql_databases),
        "shell": bool(cfg.allow_shell),
    }
