"""Reflector — evaluates progress, detects repetition, computes entropy.

The reflector runs after each ``act`` step and produces metrics consumed
by the circuit-breaker node.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage
from loguru import logger

from astra.llm.gateway import llm_gateway

REFLECTOR_SYSTEM = """You are a reflection module for A.S.T.R.A.
Analyse the agent's recent actions and decide:
1. Is the agent making progress? (yes/no)
2. Is the agent repeating itself? (yes/no)
3. Rate the diversity of recent actions 0.0–1.0

Reply as JSON: {"progress": bool, "repeating": bool, "diversity": float}
"""


async def reflect(
    messages: list[BaseMessage],
    current_repetition: int,
) -> dict[str, Any]:
    """Run a quick reflection pass and return ``{repetition_count, entropy_score}``."""
    recent = messages[-6:] if len(messages) > 6 else messages
    summary = "\n".join(f"- {m.content[:200]}" for m in recent)

    llm_messages = [
        SystemMessage(content=REFLECTOR_SYSTEM),
        SystemMessage(content=f"Recent actions:\n{summary}"),
    ]

    try:
        response = await llm_gateway.chat(messages=llm_messages)
        raw = response.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(raw)

        entropy = float(data.get("diversity", 1.0))
        repeating = bool(data.get("repeating", False))
    except Exception as exc:
        logger.warning("Reflection parse failed, using heuristics: {}", exc)
        entropy, repeating = _heuristic_reflect(messages)

    repetition = current_repetition + 1 if repeating else 0

    return {
        "repetition_count": repetition,
        "entropy_score": max(0.0, min(1.0, entropy)),
    }


def _heuristic_reflect(messages: list[BaseMessage]) -> tuple[float, bool]:
    """Cheap fallback: check hashes of recent messages."""
    if len(messages) < 2:
        return 1.0, False

    hashes = [
        hashlib.md5(m.content.encode()).hexdigest()
        for m in messages[-6:]
        if m.content
    ]
    if not hashes:
        return 1.0, False

    counter = Counter(hashes)
    most_common_count = counter.most_common(1)[0][1]
    diversity = 1.0 - (most_common_count / len(hashes))
    repeating = most_common_count >= 3
    return diversity, repeating
