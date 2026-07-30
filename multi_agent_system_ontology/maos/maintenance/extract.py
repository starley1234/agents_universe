"""Автоизвлечение сущностей и связей из диалогов в онтологический граф.

Без LLM — детерминированная эвристика по шаблонам определений и
отношений в тексте сообщений (например, 'X — это Y', 'X создал Y',
'X использует Y'), с возможностью подключения кастомного экстрактора
(например, дешёвой локальной LLM для структурного извлечения сущностей).
"""
from __future__ import annotations

import re
from typing import Any, Callable

# Шаблоны для определений вида "MAOS — это мультиагентная система" или "MAOS is a multi-agent system"
_DEF_RE = re.compile(
    r"\b([A-Za-zА-Яа-я0-9_]{3,30})\s*(?:—|-|is)\s*(?:это|a|an)?\s*([^\n.;]{5,100})",
    re.IGNORECASE | re.UNICODE,
)

# Шаблоны для отношений вида "starley создал MAOS", "coder использует python", "a works_on b"
_REL_RE = re.compile(
    r"\b([A-Za-zА-Яа-я0-9_]{3,30})\s+(создал|разработал|использует|помогает|работает над|created|developed|uses|helps|works_on|created_by)\s+([A-Za-zА-Яа-я0-9_]{3,30})\b",
    re.IGNORECASE | re.UNICODE,
)


def _guess_kind(name: str) -> str:
    lower = name.lower()
    if any(k in lower for k in ("maos", "postgres", "pgvector", "python", "system", "система", "проект")):
        return "project"
    if any(k in lower for k in ("agent", "coder", "writer", "analyst", "агент")):
        return "agent"
    if any(k in lower for k in ("starley", "user", "admin", "пользователь", "автор")):
        return "person"
    return "concept"


def _normalize_pred(pred: str) -> str:
    p = pred.lower().strip()
    mapping = {
        "создал": "created",
        "разработал": "created",
        "developed": "created",
        "использует": "uses",
        "помогает": "helps",
        "работает над": "works_on",
    }
    return mapping.get(p, p)


def extract_graph_from_messages(
    messages: list[dict[str, Any]],
    custom_extractor: Callable[[list[dict[str, Any]]], list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Извлекает сущности и связи из текста сообщений диалога.

    Возвращает список словарей вида:
      - {"type": "entity", "kind": ..., "name": ..., "description": ...}
      - {"type": "relation", "subj": (kind, name), "pred": ..., "obj": (kind, name)}
    """
    if custom_extractor is not None:
        return custom_extractor(messages)

    extracted: list[dict[str, Any]] = []
    seen_entities: set[tuple[str, str]] = set()
    seen_relations: set[tuple[str, str, str]] = set()

    for msg in messages:
        content = str(msg.get("content", "")).strip()
        if not content:
            continue

        # 1. Поиск определений (entity)
        for match in _DEF_RE.finditer(content):
            name, desc = match.group(1).strip(), match.group(2).strip()
            if len(name) >= 3 and len(desc) >= 5:
                kind = _guess_kind(name)
                key = (kind, name)
                if key not in seen_entities:
                    seen_entities.add(key)
                    extracted.append({
                        "type": "entity",
                        "kind": kind,
                        "name": name,
                        "description": desc,
                    })

        # 2. Поиск отношений (relation)
        for match in _REL_RE.finditer(content):
            subj_name, pred_raw, obj_name = (
                match.group(1).strip(),
                match.group(2).strip(),
                match.group(3).strip(),
            )
            if (
                len(subj_name) >= 3
                and len(obj_name) >= 3
                and subj_name.lower() != obj_name.lower()
            ):
                subj_kind = _guess_kind(subj_name)
                obj_kind = _guess_kind(obj_name)
                pred = _normalize_pred(pred_raw)
                rel_key = (f"{subj_kind}:{subj_name}", pred, f"{obj_kind}:{obj_name}")
                if rel_key not in seen_relations:
                    seen_relations.add(rel_key)
                    extracted.append({
                        "type": "relation",
                        "subj": (subj_kind, subj_name),
                        "pred": pred,
                        "obj": (obj_kind, obj_name),
                    })

    return extracted
