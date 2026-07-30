"""Готовые payload для двух прикладных сценариев MAOS."""
from __future__ import annotations
from typing import Any
from .website_build import initial_state, website_steps

def website_build(topic: str, implementation: str = "") -> dict[str, Any]:
    return {"kind":"website_build", "title":f"Сайт: {topic}", "input":{"topic":topic},
            "state":initial_state(topic), "status":"waiting_for_answer" if not implementation else "queued",
            "steps":website_steps(implementation)}

def room_inventory(title: str = "Опись помещения") -> dict[str, Any]:
    return {"kind":"room_inventory", "title":title, "input":{}, "status":"waiting_for_answer",
            "state":{"question":"Загрузите фотографии помещения и добавьте описание/адрес.", "agent_map":{}},
            "steps":[{"kind":"photo_analysis"},{"kind":"inventory_json"},
                     {"kind":"review"},{"kind":"docx_generation"},{"kind":"delivery"}]}
