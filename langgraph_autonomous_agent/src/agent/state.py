"""Agent state — TypedDict for LangGraph StateGraph."""
from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph import add_messages


class AgentState(TypedDict, total=False):
    # Identity
    task_id: str
    task_description: str

    # Conversation
    messages: Annotated[list[AnyMessage], add_messages]

    # Planning
    plan: list[dict[str, Any]]      # [{id, description, status, expected_output}]
    current_step: int

    # Accumulators (reducer: append)
    results: Annotated[list[dict[str, Any]], operator.add]
    errors: Annotated[list[str], operator.add]

    # Quality / budget
    quality: float                   # 0.0–1.0
    iteration: int
    max_iterations: int

    # Memory injection
    memory_context: str

    # Final
    final_result: str
    status: str                      # planning|executing|reflecting|completed|failed
    progress: float                  # 0–100
    metadata: dict[str, Any]
