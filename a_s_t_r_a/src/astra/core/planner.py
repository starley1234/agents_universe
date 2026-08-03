"""Hierarchical planner — robust JSON extraction, system-aware."""

from __future__ import annotations

import json
import re

from langchain_core.messages import HumanMessage, SystemMessage
from loguru import logger

from astra.llm.gateway import llm_gateway
from astra.prompts.registry import prompt_registry
from astra.mcp.tool_registry import tool_registry

FALLBACK_SYSTEM = """You are a task planner. Output ONLY JSON array of steps."""

SYSTEM_CAPABILITIES = """
System A.S.T.R.A. capabilities:
- Semantic memory (RAG) search via pgvector
- Knowledge graph (NetworkX/FalkorDB) for entities/relations
- MCP tools: search, image generation, TTS, filesystem (if configured)
- Can check own health: database, LLM, MCP servers, embeddings
- Can generate reports, create code, analyze data

For 'check system health' goals, always include:
- Check database connectivity
- Check LLM availability and model
- Check embeddings
- Check MCP tools
- Summarize readiness report

Output ONLY JSON array, no extra text.
"""


def _get_planner_prompt() -> str:
    try:
        p = prompt_registry.get("planner")
        if p:
            return p + "\n\n" + SYSTEM_CAPABILITIES
    except Exception:
        pass
    return FALLBACK_SYSTEM + "\n\n" + SYSTEM_CAPABILITIES


def _extract_json_array(text: str) -> list[str] | None:
    """Try to extract JSON array from text that may contain extra explanation."""
    text = text.strip()

    # Direct JSON
    try:
        data = json.loads(text)
        if isinstance(data, list) and data:
            return [str(x).strip() for x in data if str(x).strip()]
    except Exception:
        pass

    # Strip code fences
    fence_match = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence_match:
        try:
            data = json.loads(fence_match.group(1))
            if isinstance(data, list) and data:
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            pass

    # Find first [ ... ] block
    array_match = re.search(r"\[\s*([^\[\]]*?(?:\"[^\"]*\"|'[^']*'|[^,\[\]]+)(?:\s*,\s*[^\[\]]*?)*)\s*\]", text, re.DOTALL)
    if array_match:
        try:
            # Try to reconstruct as JSON array by extracting quoted strings
            # Look for all quoted strings inside the brackets
            inner = array_match.group(0)
            data = json.loads(inner)
            if isinstance(data, list) and data:
                return [str(x).strip() for x in data if str(x).strip()]
        except Exception:
            # Fallback: split by newline or comma and extract strings that look like steps
            try:
                # Extract lines that look like steps
                lines = re.split(r'[\n,]+', text)
                steps = []
                for line in lines:
                    line = line.strip().strip('"\'-•*1234567890. ')
                    if len(line) > 10 and len(line) < 200:
                        if any(v in line.lower() for v in ["analyze", "check", "execute", "validate", "summarize", "test", "search", "gather"]):
                            steps.append(line)
                if steps:
                    return steps[:8]
            except Exception:
                pass

    return None


async def generate_plan(
    goal: str,
    completed_steps: list[str],
    context: str = "",
) -> list[str]:
    system_prompt = _get_planner_prompt()

    # Add available tools to context for better planning
    tools_info = ""
    try:
        tools = await tool_registry.get_tools_for_project(__import__("uuid").UUID(int=0))
        if tools:
            tools_info = "\nAvailable MCP tools: " + ", ".join([t["function"]["name"] for t in tools[:10]])
    except Exception:
        pass

    human_content = (
        f"Goal: {goal}\n\n"
        f"Already completed: {completed_steps}\n"
        f"Available context:\n{context[:1200]}\n"
        f"{tools_info}\n\n"
        f"Now produce JSON array for this goal."
    )

    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=human_content),
    ]

    try:
        response = await llm_gateway.chat(messages=messages, metadata={"prompt": "planner", "goal": goal}, temperature=0.2)
        raw = response.content.strip()

        # Robust extraction
        steps = _extract_json_array(raw)
        if steps:
            logger.info("📋 Plan generated with {} steps: {}", len(steps), steps)
            return steps

        raise ValueError(f"Could not extract JSON array from: {raw[:500]}")
    except Exception as exc:
        logger.warning("Planner failed ({}), using heuristic fallback", exc)
        # Heuristic fallback based on goal keywords
        lower = goal.lower()
        if "работоспособность" in lower or "проверь" in lower or "health" in lower or "систем" in lower:
            return [
                "Check database connectivity and pgvector extension",
                "Check LLM availability and model response",
                "Check embeddings service",
                "Check MCP tools availability",
                "Summarize system readiness report with status of each module",
            ]
        elif len(goal.split()) < 6:
            return [goal, "Validate result and summarize"]
        else:
            return [goal]
