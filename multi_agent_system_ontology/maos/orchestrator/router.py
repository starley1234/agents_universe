"""Semantic Router: выбор агента векторным поиском по описаниям (ТЗ п.5).

Отличие от agent_system/agent/router.py: там гибрид "эвристика по
ключевым словам + LLM-фолбэк". Здесь — БЕЗ обращения к LLM вообще:
описание каждого агента заранее векторизовано (agent.description_embedding
в БД), запрос пользователя векторизуется тем же эмбеддером, и побеждает
агент с максимальным косинусным сходством. Это быстрее (нет сетевого
вызова модели) и дешевле (эмбеддинг — на порядки дешевле генерации), а
для задачи "выбрать одного из небольшого фиксированного списка агентов"
качества векторного поиска обычно достаточно.

Ключевые слова (agent.keywords) — как в agent_system, тай-брейк и
подстраховка на случай, если у агента ещё нет эмбеддинга (например,
только что создан и фоновый процесс его не проиндексировал).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..llm.embeddings import BaseEmbedder, EmbeddingError, cosine

_WORD_RE = re.compile(r"\w+", re.UNICODE)
_MIN_WORD = 3


def _stem(word: str) -> str:
    """Грубая эвристика приведения к основе слова — та же, что в
    agent_system/agent/router.py: не настоящая морфология, но снимает
    большинство русских окончаний и достаточно для keyword-фолбэка,
    который и так вторичен после семантического поиска по эмбеддингам."""
    w = word.lower()
    if len(w) <= 3:
        return w
    return w[:3] if len(w) <= 5 else w[:max(4, int(len(w) * 0.75))]


def _terms(text: str) -> set[str]:
    words = _WORD_RE.findall(text)
    return {_stem(w) for w in words if len(w) >= _MIN_WORD}


@dataclass
class RouteDecision:
    agent_slug: str
    method: str          # "semantic" | "keyword" | "default"
    score: float = 0.0
    reason: str = ""


def _keyword_overlap(query: str, agent: dict) -> int:
    q = _terms(query)
    pool = _terms(agent.get("description", "") + " " + agent.get("keywords", ""))
    return len(q & pool)



def route(query: str, agents: list[dict], embedder: BaseEmbedder,
         min_score: float = 0.15, default_agent: str | None = None,
         ) -> RouteDecision:
    """Выбирает slug агента под запрос.

    agents — список словарей вида store.agents_for_routing(): id, slug,
    name, description, keywords, embedding (может быть None, если агент
    ещё не проиндексирован — тогда участвует только в keyword-этапе).
    """
    if not agents:
        if default_agent:
            return RouteDecision(default_agent, "default",
                                 reason="нет зарегистрированных агентов")
        raise ValueError("Нет ни одного доступного агента для роутинга")

    with_emb = [a for a in agents if a.get("embedding")]
    if with_emb:
        try:
            qvec = embedder.embed_one(query)
        except EmbeddingError:
            qvec = None
        if qvec is not None:
            scored = sorted(
                ((cosine(qvec, a["embedding"]), a) for a in with_emb),
                key=lambda t: -t[0])
            best_score, best_agent = scored[0]
            if best_score >= min_score:
                return RouteDecision(best_agent["slug"], "semantic", best_score,
                                     f"косинусное сходство {best_score:.3f}")

    # эмбеддинги не помогли (нет вектора запроса, все ниже порога, или
    # ни у одного агента ещё нет description_embedding) — keyword-фолбэк
    kw_scored = sorted(((_keyword_overlap(query, a), a) for a in agents),
                       key=lambda t: -t[0])
    top_hits, top_agent = kw_scored[0]
    if top_hits > 0:
        return RouteDecision(top_agent["slug"], "keyword", float(top_hits),
                             f"{top_hits} совпадений ключевых слов")

    fallback = default_agent or agents[0]["slug"]
    return RouteDecision(fallback, "default",
                         reason="ни векторный, ни keyword-поиск не дали "
                                "уверенного результата")
