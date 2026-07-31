"""Схемы данных для API и MCP: запросы, ответы, описания инструментов."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class ToolSchema:
    """Схема инструмента, совместимая с OpenAI Function Calling и MCP."""

    name: str
    description: str
    parameters: dict[str, Any]
    skills: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    dangerous: bool = False
    example: str = ""

    def to_openai(self) -> dict[str, Any]:
        """Формат для OpenAI function calling / chat completions."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def to_mcp(self) -> dict[str, Any]:
        """Формат MCP (Model Context Protocol: tools/list)."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.parameters,
            "metadata": {
                "skills": self.skills,
                "attributes": self.attributes,
                "dangerous": self.dangerous,
                "example": self.example,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ToolSearchRequest:
    """Запрос на умный поиск инструментов в реестре."""

    query: str = ""
    skill: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)
    limit: int = 10
    min_score: float = 0.1

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolSearchRequest":
        return cls(
            query=str(data.get("query", "")),
            skill=data.get("skill"),
            attributes=data.get("attributes", {}),
            limit=int(data.get("limit", 10)),
            min_score=float(data.get("min_score", 0.1)),
        )


@dataclass
class ToolSearchResponse:
    """Ответ поискового запроса с оценкой релевантности (score)."""

    query: str
    total_found: int
    results: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass
class ToolExecuteRequest:
    """Запрос на выполнение инструмента."""

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    context_id: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ToolExecuteRequest":
        return cls(
            tool_name=str(data.get("tool_name") or data.get("name", "")),
            arguments=data.get("arguments") or data.get("args") or {},
            context_id=data.get("context_id"),
        )


@dataclass
class ToolExecuteResponse:
    """Результат выполнения инструмента."""

    tool_name: str
    success: bool
    result: Any = None
    error: str | None = None
    execution_time_ms: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


@dataclass
class MCPToolCall:
    """Вызов инструмента по протоколу MCP (Model Context Protocol)."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str = "call-0"


@dataclass
class MCPToolResult:
    """Ответ на вызов инструмента в формате MCP."""

    content: list[dict[str, Any]] = field(default_factory=list)
    isError: bool = False
    call_id: str = "call-0"

    @classmethod
    def ok(cls, text: str, call_id: str = "call-0") -> "MCPToolResult":
        return cls(
            content=[{"type": "text", "text": str(text)}],
            isError=False,
            call_id=call_id,
        )

    @classmethod
    def err(cls, text: str, call_id: str = "call-0") -> "MCPToolResult":
        return cls(
            content=[{"type": "text", "text": f"Error: {text}"}],
            isError=True,
            call_id=call_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "isError": self.isError,
            "call_id": self.call_id,
        }
