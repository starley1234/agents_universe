"""Строгая проверка результата VLM до генерации юридически значимой описи."""
from __future__ import annotations
from typing import Any
REQUIRED_ITEM = ("name", "category", "quantity", "condition", "description", "photo_refs", "confidence", "requires_review")
def validate_inventory(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict): raise ValueError("опись должна быть JSON-объектом")
    rooms = data.get("premises")
    if not isinstance(rooms, list) or not rooms: raise ValueError("premises: непустой список обязателен")
    for ri, room in enumerate(rooms):
        if not isinstance(room, dict) or not isinstance(room.get("name"), str) or not room["name"].strip():
            raise ValueError(f"premises[{ri}].name обязателен")
        if not isinstance(room.get("items"), list): raise ValueError(f"premises[{ri}].items должен быть списком")
        for ii, item in enumerate(room["items"]):
            if not isinstance(item, dict): raise ValueError(f"item {ri}/{ii} должен быть объектом")
            missing=[k for k in REQUIRED_ITEM if k not in item]
            if missing: raise ValueError(f"item {ri}/{ii}: нет полей {', '.join(missing)}")
            if not isinstance(item["quantity"], int) or item["quantity"] < 1: raise ValueError("quantity — целое >= 1")
            if not isinstance(item["confidence"], (float,int)) or not 0 <= item["confidence"] <= 1: raise ValueError("confidence в диапазоне 0..1")
            if not isinstance(item["photo_refs"], list): raise ValueError("photo_refs должен быть списком")
            if item["confidence"] < .8: item["requires_review"] = True
    data.setdefault("notes", []); data.setdefault("requires_review", any(i["requires_review"] for r in rooms for i in r["items"]))
    return data
