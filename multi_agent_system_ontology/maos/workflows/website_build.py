"""Детерминированное описание этапов workflow создания сайта."""
from __future__ import annotations
from typing import Any

WEBSITE_STEPS = ("requirements", "research", "design", "implementation", "qa", "delivery")
def website_steps(choice: str = "") -> list[dict[str, Any]]:
    if choice and choice not in {"static", "koseven"}:
        raise ValueError("implementation должен быть static или koseven")
    return [{"kind": x, "input": ({"implementation": choice} if x == "implementation" and choice else {})}
            for x in WEBSITE_STEPS]
def initial_state(topic: str) -> dict[str, Any]:
    return {"topic": topic, "question": "Какой вариант реализации выбрать?",
            "options": [{"id":"static","label":"Статичный HTML/CSS/JS"},
                        {"id":"koseven","label":"На базе Koseven"}]}
