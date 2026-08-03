"""Reflector — uses prompt registry."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any

from langchain_core.messages import BaseMessage, SystemMessage
from loguru import logger

from astra.llm.gateway import llm_gateway
from astra.prompts.registry import prompt_registry

FALLBACK_SYSTEM = """You are a reflection module for A.S.T.R.A.
Analyse the agent's recent actions and decide:
1. Is the agent making progress? (yes/no)
2. Is the agent repeating itself? (yes/no)
3. Rate the diversity of recent actions 0.0–1.0

Reply as JSON: {"progress": bool, "repeating": bool, "diversity": float}
"""


def _get_reflector_prompt() -> str:
    try:
        return prompt_registry.get("reflector", default=FALLBACK_SYSTEM) or FALLBACK_SYSTEM
    except Exception:
        return FALLBACK_SYSTEM


async def reflect(
    messages: list[BaseMessage],
    current_repetition: int,
) -> dict[str, Any]:
    recent = messages[-6:] if len(messages) > 6 else messages
    summary = "\n".join(f"- {m.content[:200]}" for m in recent)

    llm_messages = [
        SystemMessage(content=_get_reflector_prompt()),
        SystemMessage(content=f"Recent actions:\n{summary}"),
    ]

    try:
        response = await llm_gateway.chat(messages=llm_messages, metadata={"prompt": "reflector"})
        raw = response.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0] if "\n" in raw else raw
            raw = raw.strip().rstrip("```").strip()
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
