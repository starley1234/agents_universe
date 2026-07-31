"""Agent state — TypedDict for LangGraph compatibility.

Uses ``add_messages`` reducer so each node can append new messages
without overwriting the full conversation history.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict
from uuid import UUID

from langchain_core.messages import BaseMessage, HumanMessage
from langgraph.graph import add_messages


class AgentState(TypedDict):
    """State flowing through the LangGraph DAG.

    LangGraph inspects ``Annotated`` hints on TypedDict to wire reducers.
    ``add_messages`` appends new messages instead of overwriting.
    """

    # ── Identity ─────────────────────────────────────────────
    session_id: UUID
    project_id: UUID

    # ── Conversation (reducer: append) ────────────────────────
    messages: Annotated[list[BaseMessage], add_messages]

    # ── Planning ─────────────────────────────────────────────
    current_plan: list[str]
    completed_steps: list[str]
    current_step_index: int

    # ── Memory context ───────────────────────────────────────
    retrieved_context: str
    ontology_context: str

    # ── Circuit Breaker ──────────────────────────────────────
    repetition_count: int
    entropy_score: float
    is_halted: bool


def make_initial_state(
    *,
    session_id: UUID,
    project_id: UUID,
    goal: str,
) -> dict[str, Any]:
    """Build the initial state dict for a new agent run."""
    return {
        "session_id": session_id,
        "project_id": project_id,
        "messages": [HumanMessage(content=goal)],
        "current_plan": [],
        "completed_steps": [],
        "current_step_index": 0,
        "retrieved_context": "",
        "ontology_context": "",
        "repetition_count": 0,
        "entropy_score": 1.0,
        "is_halted": False,
    }
