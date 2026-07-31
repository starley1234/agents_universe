"""Адаптеры для интеграции agent_toolkit со сторонними фреймворками агентов.

Обеспечивают прозрачную конвертацию инструментов в форматы:
  1. OpenAI Function Calling / Chat Completions (`to_openai_tools`).
  2. LangChain / LlamaIndex (`to_langchain_tools`).
  3. Монорепозиторные агенты agent_system (`to_agent_system_tools`).
  4. Монорепозиторные агенты agentic_workflow_os / AWOS (`to_awos_tools`).
"""
from __future__ import annotations

from typing import Any

from .core import ToolRegistry


def to_openai_tools(registry: ToolRegistry) -> list[dict[str, Any]]:
    """Конвертировать инструменты в формат OpenAI Function Calling."""
    return registry.to_openai_tools()


def to_langchain_tools(registry: ToolRegistry) -> list[Any]:
    """Конвертировать инструменты в формат LangChain StructuredTool / функций."""
    tools = []
    for tool in registry.list_tools():
        try:
            from langchain_core.tools import StructuredTool  # type: ignore

            lt = StructuredTool.from_function(
                func=tool.fn,
                name=tool.name.replace(".", "_"),
                description=tool.description,
            )
            tools.append(lt)
        except ImportError:
            # Если LangChain не установлен, возвращаем универсальный словарь-обёртку
            tools.append(
                {
                    "name": tool.name.replace(".", "_"),
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "func": tool.fn,
                }
            )
    return tools


def to_agent_system_tools(registry: ToolRegistry) -> list[Any]:
    """Конвертировать инструменты в формат agent_system.agent.tools.base.Tool."""
    out = []
    try:
        from agent.tools.base import Tool as AsTool  # type: ignore

        for tool in registry.list_tools():
            out.append(
                AsTool(
                    name=tool.name,
                    description=tool.description,
                    parameters=tool.parameters,
                    fn=tool.fn,
                    dangerous=tool.dangerous,
                )
            )
    except ImportError:
        # Если запускаемся вне контекста agent_system
        for tool in registry.list_tools():
            out.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                    "fn": tool.fn,
                    "dangerous": tool.dangerous,
                }
            )
    return out


def to_awos_tools(registry: ToolRegistry) -> list[Any]:
    """Конвертировать инструменты в формат agentic_workflow_os.awos.tools.base.Tool."""
    out = []
    try:
        from awos.tools.base import Tool as AwosTool  # type: ignore

        for tool in registry.list_tools():
            # В AWOS схема аргументов args — словарь имя -> короткое описание
            args_map: dict[str, str] = {}
            props = tool.parameters.get("properties", {})
            for p_name, p_val in props.items():
                args_map[p_name] = str(p_val.get("description", p_name))
            out.append(
                AwosTool(
                    name=tool.name,
                    description=tool.description,
                    args=args_map,
                    fn=tool.fn,
                    dangerous=tool.dangerous,
                    example=tool.example,
                )
            )
    except ImportError:
        for tool in registry.list_tools():
            out.append(
                {
                    "name": tool.name,
                    "description": tool.description,
                    "fn": tool.fn,
                    "dangerous": tool.dangerous,
                }
            )
    return out
