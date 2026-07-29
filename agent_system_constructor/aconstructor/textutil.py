"""Детерминированные текстовые метрики.

Отдельный модуль, потому что этим пользуются пять пайплайнов из семи,
и потому что сопоставление должно быть воспроизводимым: LLM формулирует
признаки, а совпадение считает арифметика.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Iterable

STOP = {
    "the", "a", "an", "of", "and", "or", "for", "to", "in", "on", "with", "by",
    "is", "are", "be", "as", "at", "from", "that", "this", "it", "its", "such",
    "и", "или", "для", "на", "в", "с", "по", "из", "как", "что", "это", "the",
    "system", "method", "device", "система", "метод", "устройство",
}

_TOKEN = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9]+")


def tokens(text: str) -> list[str]:
    return [t for t in (w.lower() for w in _TOKEN.findall(text or "")) if t not in STOP and len(t) > 2]


def jaccard(a: str, b: str) -> float:
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def cosine(a: str, b: str) -> float:
    ca, cb = Counter(tokens(a)), Counter(tokens(b))
    if not ca or not cb:
        return 0.0
    common = set(ca) & set(cb)
    num = sum(ca[t] * cb[t] for t in common)
    den = math.sqrt(sum(v * v for v in ca.values())) * math.sqrt(sum(v * v for v in cb.values()))
    return num / den if den else 0.0


def coverage(needles: Iterable[str], haystack: str) -> tuple[float, list[str]]:
    """Доля фраз-признаков, покрытых текстом; и какие именно покрыты.

    Признак считается покрытым, если хотя бы 60% его значимых слов есть
    в тексте — так формулировка «rolling hash window» находит «window of
    rolling hashes».
    """
    hay = set(tokens(haystack))
    hits: list[str] = []
    needles = [n for n in needles if n]
    for n in needles:
        tn = tokens(n)
        if not tn:
            continue
        if sum(1 for t in tn if t in hay) / len(tn) >= 0.6:
            hits.append(n)
    return (len(hits) / len(needles) if needles else 0.0), hits


def norm_code(s: str) -> str:
    """Нормализация артикула: MS-21042 L4 == ms21042l4."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())
