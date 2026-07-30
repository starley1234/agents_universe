"""Контроль контекста: экономия токенов через three-tier память (ТЗ п.4, п.5).

Перед каждым обращением к LLM оркестратор:
  1. Оценивает размер накопленной short-term истории. Оценка токенов —
     грубая (len(text) / 4, стандартное для английского и рабочее для
     большинства языков приближение), но детерминированная и не требует
     тяжёлого токенизатора конкретной модели — нам не нужна точность до
     токена, только решение "влезаем или нет".
  2. Если не влезаем (или окно модели меньше small_context_window из
     конфига) — запускает ЭКСТРЕННУЮ суммаризацию: старые сообщения
     сворачиваются в одну заметку через summarizer (обычно дешёвая
     локальная модель), последние short_term_keep_last сообщений
     остаются как есть.
  3. Подмешивает mid-term "кванты памяти": векторный поиск по вопросу
     пользователя среди memory_quantum, чтобы в контекст попадали только
     релевантные фрагменты прошлых диалогов, а не вся история целиком.
"""
from __future__ import annotations

from typing import Any, Callable

from ..config import Config
from ..llm.embeddings import BaseEmbedder, EmbeddingError
from ..memory.store import Store

#: грубая оценка символов на токен для большинства языков/моделей.
CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // CHARS_PER_TOKEN) if text else 0


def estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
    return sum(estimate_tokens(str(m.get("content", ""))) for m in messages)


Summarizer = Callable[[str], str]


def _default_summarizer(text: str) -> str:
    """Суммаризация без LLM — на крайний случай, если summarizer не передан
    (например, тесты или полностью офлайн-режим). Не заменяет настоящую
    модель по качеству, но детерминирована и не роняет пайплайн."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) <= 6:
        return " / ".join(lines)
    head = lines[:3]
    tail = lines[-3:]
    return " / ".join(head) + f" … [{len(lines) - 6} строк опущено] … " + " / ".join(tail)


def needs_summarization(history: list[dict[str, Any]], context_window: int,
                        small_context_window: int) -> bool:
    """Порог срабатывания — либо у модели маленькое окно (ТЗ: "если окно
    модели < 4k"), либо накопленная история сама по себе близка к любому
    разумному окну (консервативный запас 70%, чтобы остался простор под
    system prompt, mid-term кванты и сам ответ модели)."""
    if context_window and context_window < small_context_window:
        return True
    budget = context_window or small_context_window
    return estimate_messages_tokens(history) > budget * 0.7


def summarize_history(history: list[dict[str, Any]], keep_last: int,
                      summarizer: Summarizer | None = None) -> list[dict[str, Any]]:
    """Сжимает всё, кроме последних keep_last сообщений, в одну заметку."""
    if len(history) <= keep_last:
        return history
    old, recent = history[:-keep_last] if keep_last > 0 else history, \
        history[-keep_last:] if keep_last > 0 else []
    if not old:
        return history
    joined = "\n".join(f"{m.get('role', '?')}: {m.get('content', '')}" for m in old)
    fn = summarizer or _default_summarizer
    summary_text = fn(joined)
    note = {"role": "system",
           "content": f"[Сжатая сводка более ранней части диалога: {summary_text}]"}
    return [note, *recent]


def retrieve_long_term_graph(store: Store, embedder: BaseEmbedder, query: str,
                             cfg: Config) -> list[dict[str, Any]]:
    """Сущности онтологии и их связи, релевантные текущему запросу (Graph-RAG).

    Использует гибридный поиск и многошаговый обход графа (Multi-Hop):
      1. Семантический векторный поиск + keyword-совпадение по именам (Hop 0).
      2. Обход соседей в глубину до long_term_max_hops (Hop 1..k), чтобы агент
         видел не только прямые связи, но и связанные цепочки фактов.
    """
    seeds: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()

    # 1. Семантический поиск по эмбеддингам (Hop 0)
    try:
        vec = embedder.embed_one(query)
        sem_hits = store.semantic_search_entities(
            vec, limit=getattr(cfg, "long_term_top_k", 3) * 2)
        for h in sem_hits:
            if h["score"] >= getattr(cfg, "long_term_min_score", 0.10):
                key = (h["kind"], h["name"])
                if key not in seen_keys:
                    seen_keys.add(key)
                    h["hop"] = 0
                    seeds.append(h)
    except Exception:
        pass

    # 2. Keyword-совпадение по именам сущностей
    if len(seeds) < getattr(cfg, "long_term_top_k", 3):
        try:
            q_lower = query.lower()
            graph = store.graph_data(limit=500)
            for node in graph.get("nodes", []):
                name = str(node.get("name", "")).lower()
                kind = str(node.get("kind", "")).lower()
                if (name and len(name) >= 3 and name in q_lower) or (
                    kind and len(kind) >= 3 and kind in q_lower):
                    key = (node["kind"], node["name"])
                    if key not in seen_keys:
                        seen_keys.add(key)
                        ent = store.get_entity(node["kind"], node["name"])
                        if ent:
                            seeds.append({
                                "kind": ent["kind"],
                                "name": ent["name"],
                                "description": ent.get("description", ""),
                                "score": 1.0,
                                "hop": 0,
                            })
        except Exception:
            pass

    seeds = seeds[:getattr(cfg, "long_term_top_k", 3)]
    for item in seeds:
        try:
            item["neighbours"] = store.neighbours(item["kind"], item["name"])
        except Exception:
            item["neighbours"] = []

    subgraph: list[dict[str, Any]] = list(seeds)
    max_hops = getattr(cfg, "long_term_max_hops", 2)
    max_subgraph_nodes = getattr(cfg, "long_term_top_k", 3) * 4

    # 3. Многошаговый обход (BFS Hop 1..max_hops-1)
    for step in range(1, max_hops):
        frontier: list[dict[str, Any]] = []
        for node in subgraph:
            if node.get("hop", 0) == step - 1:
                for nbr in node.get("neighbours", []):
                    key = (nbr["kind"], nbr["name"])
                    if key not in seen_keys and len(subgraph) + len(frontier) < max_subgraph_nodes:
                        seen_keys.add(key)
                        ent = store.get_entity(nbr["kind"], nbr["name"])
                        if ent:
                            new_node = {
                                "kind": ent["kind"],
                                "name": ent["name"],
                                "description": ent.get("description", ""),
                                "score": 0.0,
                                "hop": step,
                            }
                            try:
                                new_node["neighbours"] = store.neighbours(ent["kind"], ent["name"])
                            except Exception:
                                new_node["neighbours"] = []
                            frontier.append(new_node)
        if not frontier:
            break
        subgraph.extend(frontier)

    # 4. Автоматический поиск связующих путей между исходными сущностями (Pathfinding)
    connecting_paths: list[str] = []
    if len(seeds) >= 2 and subgraph:
        for i in range(len(seeds)):
            for j in range(i + 1, len(seeds)):
                try:
                    p = store.find_path((seeds[i]["kind"], seeds[i]["name"]),
                                        (seeds[j]["kind"], seeds[j]["name"]), max_depth=4)
                    if p and len(p) > 1:
                        parts = [f"{p[0]['kind']}:{p[0]['name']}"]
                        for idx in range(1, len(p), 2):
                            edge = p[idx]
                            next_node = p[idx + 1]
                            if edge.get("dir") == "out":
                                parts.append(f"--[{edge['pred']}]--> {next_node['kind']}:{next_node['name']}")
                            else:
                                parts.append(f"<--[{edge['pred']}]-- {next_node['kind']}:{next_node['name']}")
                        connecting_paths.append(" ".join(parts))
                        for item in p:
                            if "kind" in item and "name" in item:
                                k_node = (item["kind"], item["name"])
                                if k_node not in seen_keys and len(subgraph) < max_subgraph_nodes:
                                    seen_keys.add(k_node)
                                    ent = store.get_entity(item["kind"], item["name"])
                                    if ent:
                                        subgraph.append({
                                            "kind": ent["kind"],
                                            "name": ent["name"],
                                            "description": ent.get("description", ""),
                                            "score": 0.0,
                                            "hop": 1,
                                            "neighbours": [],
                                        })
                except Exception:
                    pass
        if connecting_paths:
            subgraph[0]["_connecting_paths"] = connecting_paths

    return subgraph


def format_long_term_note(graph_data: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not graph_data:
        return None
    lines = ["[Релевантный подграф онтологии (Multi-Hop Graph-RAG, long-term memory):]"]
    for item in graph_data:
        hop_label = f" (hop {item['hop']})" if item.get("hop", 0) > 0 else ""
        desc_part = f" — {item['description']}" if item.get("description") else ""
        lines.append(f"- Сущность {item['kind']}:{item['name']}{hop_label}{desc_part}")
        nbrs = item.get("neighbours", [])
        if nbrs:
            rel_strs = []
            for n in nbrs[:6]:  # до 6 связей на сущность
                if n["dir"] == "out":
                    rel_strs.append(f"--[{n['pred']}]--> {n['kind']}:{n['name']}")
                else:
                    rel_strs.append(f"<--[{n['pred']}]-- {n['kind']}:{n['name']}")
            if rel_strs:
                lines.append(f"  Связи: {', '.join(rel_strs)}")
    paths = graph_data[0].get("_connecting_paths", [])
    for p in paths:
        lines.append(f"  ★ Найден связующий путь: {p}")
    return {"role": "system", "content": "\n".join(lines)}


def retrieve_mid_term(store: Store, embedder: BaseEmbedder, query: str,
                      cfg: Config, conversation_id: int | None = None,
                      ) -> list[dict[str, Any]]:
    """Кванты памяти, релевантные текущему запросу (semantic search)."""
    try:
        vec = embedder.embed_one(query)
    except EmbeddingError:
        return []
    quanta = store.semantic_search_quanta(
        vec, limit=cfg.mid_term_top_k, conversation_id=conversation_id,
        min_score=cfg.mid_term_min_score)
    return quanta


def format_mid_term_note(quanta: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not quanta:
        return None
    lines = ["[Релевантные фрагменты памяти из прошлых диалогов:]"]
    for q in quanta:
        lines.append(
            f"- Q: {q['question']}\n  A: {q['answer']} "
            f"(источник: {q.get('provider_model') or 'н/д'}, "
            f"сходство {q['score']:.2f})")
    return {"role": "system", "content": "\n".join(lines)}


def build_messages(system_prompt: str, history: list[dict[str, Any]],
                   user_message: str, cfg: Config, context_window: int,
                   store: Store | None = None, embedder: BaseEmbedder | None = None,
                   conversation_id: int | None = None,
                   summarizer: Summarizer | None = None) -> list[dict[str, Any]]:
    """Собирает полный набор сообщений для отправки в LLM.

    Порядок: system prompt -> (опционально) long-term граф (Graph-RAG) ->
    (опционально) mid-term кванты -> (короткая или суммированная) short-term
    история -> новое сообщение пользователя.
    """
    short = history
    if needs_summarization(history, context_window, cfg.small_context_window):
        short = summarize_history(history, cfg.short_term_keep_last, summarizer)

    messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
    if store is not None and embedder is not None:
        long_term = retrieve_long_term_graph(store, embedder, user_message, cfg)
        lt_note = format_long_term_note(long_term)
        if lt_note:
            messages.append(lt_note)
        quanta = retrieve_mid_term(store, embedder, user_message, cfg,
                                   conversation_id=conversation_id)
        note = format_mid_term_note(quanta)
        if note:
            messages.append(note)
    messages.extend(short)
    messages.append({"role": "user", "content": user_message})
    return messages
