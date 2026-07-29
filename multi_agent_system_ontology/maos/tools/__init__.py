"""Инструменты MAOS-агентов: files/web/office_docs (см. maos/agents/toolbox.py).

Каркас (Tool/ToolError/Workspace/ToolRegistry) и файловые/веб-инструменты
скопированы из agent_system/agent/tools/ почти без изменений — они не
зависят от остального agent_system и одинаково полезны в обеих системах.
Инструмент вызова инструментов через LLM (function calling) в MAOS
ОПЦИОНАЛЕН для конкретного агента (agent.tools в БД), см.
maos/agents/toolbox.py и maos/agents/loop.py.
"""
from __future__ import annotations
