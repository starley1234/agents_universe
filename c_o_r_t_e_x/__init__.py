"""C.O.R.T.E.X. — событийный runtime и MCP gateway для агентских систем.

Проект намеренно запускается без обязательных внешних сервисов: локальный
in-memory bus используется для разработки, а Redis/NATS, PostgreSQL, LiteLLM,
Temporal/LangGraph и FastAPI подключаются как production-адаптеры.
"""
from __future__ import annotations

__version__ = "0.1.0"
__project__ = "C.O.R.T.E.X."

from .config import Settings, get_settings
from .signals import Event, Task, TaskStatus

__all__ = ["__version__", "__project__", "Settings", "get_settings", "Event", "Task", "TaskStatus"]
