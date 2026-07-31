"""Hierarchical planner — generates a DAG of task steps via LLM."""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from astra.llm.gateway import llm_gateway

PLANNER_SYSTEM = """You are a task-planning module for A.S.T.R.A.
Given a user goal and available context, produce a JSON array of concrete,
ordered steps.  Each step is a short imperative sentence.

Rules:
- Return ONLY valid JSON: ["step 1", "step 2", ...]
- Maximum 12 steps
- Each step must be actionable
- If the goal is simple, use fewer steps
"""


async def generate_plan(
    goal: str,
    completed_steps: list[str],
    context: str = "",
) -> list[str]:
    """Ask the LLM to produce an ordered plan."""
    messages = [
        SystemMessage(content=PLANNER_SYSTEM),
        HumanMessage(
            content=(
                f"Goal: {goal}\n\n"
                f"Already completed: {completed_steps}\n"
                f"Available context:\n{context[:2000]}"
            )
        ),
    ]

    try:
        response = await llm_gateway.chat(messages=messages)
        raw = response.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        steps: list[str] = json.loads(raw)
        if not isinstance(steps, list) or not steps:
            raise ValueError("Expected a non-empty list")
        logger.info("📋  Plan generated with {} steps", len(steps))
        return steps
    except Exception as exc:
        logger.warning("Planner failed ({}), using single-step fallback", exc)
        return [goal]
