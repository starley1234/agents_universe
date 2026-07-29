"""Навык «rag»: индексация текста в векторно-полнотекстовую базу MAOS и
поиск по нему — перенесено из agent_system/agent/skills/rag.py и
упрощено под ЕДИНСТВЕННЫЙ бэкенд (PostgreSQL+pgvector — здесь другого
и не бывает, в отличие от agent_system, где RAG работал и над SQLite).

Гибридный поиск: эмбеддинги (косинусное сходство через pgvector) + FTS
(tsvector/plainto_tsquery) — эмбеддинги плохо ловят точные идентификаторы
(коды, номера), FTS не понимает синонимы, вместе они закрывают оба случая.

RAG на онтологии (rag_query_entity) — сначала ищет фрагменты,
ПРИВЯЗАННЫЕ к конкретному объекту графа (entity_refs при индексации), а
затем, если задан query, дополняет их фрагментами семантически похожих
объектов — точнее обычного rag_query для вопросов вида «что известно
про X».
"""
from __future__ import annotations

import re
from typing import Any

from ..llm.embeddings import BaseEmbedder, EmbeddingError
from ..memory.store import Store
from ..tools.base import Tool, ToolError, Workspace

MAX_INDEX_CHARS = 2_000_000


def chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Разбивает текст на фрагменты по границам абзацев, с перекрытием.

    По абзацам, а не жёстко по символам: обрезка посреди предложения или
    таблицы портит смысл фрагмента. Перекрытие снижает риск, что важная
    мысль разорвётся ровно на границе двух фрагментов.
    """
    text = text.strip()
    if not text:
        return []
    if chunk_size <= 0:
        return [text]
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        chunk_overlap = max(0, chunk_size // 4)

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        paragraphs = [text]

    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= chunk_size:
            current = candidate
            continue
        if current:
            chunks.append(current)
            tail = current[-chunk_overlap:] if chunk_overlap else ""
            current = f"{tail}\n\n{para}" if tail else para
        else:
            current = para
        while len(current) > chunk_size:
            chunks.append(current[:chunk_size])
            current = current[chunk_size - chunk_overlap:]
    if current:
        chunks.append(current)
    return chunks


def build(ws: Workspace, store: Store, embedder: BaseEmbedder,
         chunk_size: int = 1200, chunk_overlap: int = 150,
         default_top_k: int = 6) -> list[Tool]:

    def _embed(text: str) -> list[float]:
        try:
            return embedder.embed_one(text)
        except EmbeddingError as exc:
            raise ToolError(f"Не удалось получить эмбеддинг: {exc}") from exc

    def _embed_many(texts: list[str]) -> list[list[float]]:
        try:
            return embedder.embed(texts)
        except EmbeddingError as exc:
            raise ToolError(f"Не удалось получить эмбеддинги: {exc}") from exc

    def _read_source(text: str, path: str) -> tuple[str, str]:
        if path.strip():
            p = ws.resolve(path)
            if not p.exists():
                raise ToolError(f"Файл {path!r} не найден")
            body = p.read_text(encoding="utf-8", errors="replace")
            src = ws.relative(p)
        elif text.strip():
            body = text
            src = "(инлайн-текст)"
        else:
            raise ToolError("Нужно указать либо text, либо path")
        if len(body) > MAX_INDEX_CHARS:
            raise ToolError(
                f"Источник слишком велик для индексации за один вызов "
                f"({len(body)} симв. > {MAX_INDEX_CHARS}). Разбейте файл "
                "или индексируйте частями с разными source."
            )
        return body, src

    def rag_index(text: str = "", path: str = "", source: str = "",
                 entity_refs: str = "") -> str:
        """Разбить текст на фрагменты, посчитать эмбеддинги и сохранить.
        Повторная индексация того же source ЗАМЕНЯЕТ старые фрагменты."""
        body, auto_src = _read_source(text, path)
        src = source.strip() or auto_src

        refs: list[tuple[str, str]] = []
        if entity_refs.strip():
            for pair in entity_refs.split(","):
                pair = pair.strip()
                if not pair:
                    continue
                if ":" not in pair:
                    raise ToolError(
                        f"entity_refs должен быть 'kind:name,kind:name', "
                        f"получено {pair!r}"
                    )
                kind, name = pair.split(":", 1)
                refs.append((kind.strip(), name.strip()))

        chunks = chunk_text(body, chunk_size, chunk_overlap)
        if not chunks:
            raise ToolError("После разбиения не осталось ни одного фрагмента")

        embeddings = _embed_many(chunks)
        ids = store.add_chunks(src, chunks, embeddings=embeddings, entity_refs=refs)
        return (f"Проиндексировано {src!r}: {len(ids)} фрагмент(ов) "
               f"(по ~{chunk_size} симв., перекрытие {chunk_overlap})"
               + (f", привязано к {len(refs)} объект(ам) онтологии"
                  if refs else ""))

    def rag_query(query: str, top_k: int = 0, source: str = "") -> str:
        """Обычный RAG: гибридный поиск (эмбеддинги + FTS) по всем
        проиндексированным фрагментам, с указанием источника у каждого."""
        if not query.strip():
            raise ToolError("Пустой запрос")
        k = top_k if top_k > 0 else default_top_k
        vec = _embed(query)
        vector_hits = store.semantic_search_chunks(vec, limit=k, source=source or None)
        text_hits = store.fts_chunks(query, limit=k)

        seen: set[tuple[str, int]] = set()
        merged: list[dict[str, Any]] = []
        for hit in vector_hits:
            key = (hit["source"], hit.get("ord", 0))
            if key not in seen:
                seen.add(key)
                merged.append({**hit, "match": "векторный"})
        for hit in text_hits:
            if source and hit.get("source") != source:
                continue
            key = (hit["source"], hit.get("ord", 0))
            if key not in seen:
                seen.add(key)
                merged.append({**hit, "match": "текстовый", "score": None})

        if not merged:
            return (f"По запросу {query!r} ничего не найдено. Индекс пуст "
                   "или запрос не пересекается с проиндексированным — "
                   "сначала вызовите rag_index.")

        out = [f"Найдено {len(merged)} фрагмент(ов) по запросу {query!r}:"]
        for i, hit in enumerate(merged[:k * 2], 1):
            score = f", сходство {hit['score']:.3f}" if hit.get("score") is not None else ""
            out.append(f"\n[{i}] источник={hit['source']} "
                      f"фрагмент={hit.get('ord', 0)} ({hit['match']}{score})")
            out.append(hit["text"])
        out.append("\nОтвечай, опираясь ТОЛЬКО на эти фрагменты, и указывай "
                  "источник каждого утверждения. Если ответа здесь нет — "
                  "скажи прямо, не додумывай.")
        return "\n".join(out)

    def rag_query_entity(kind: str, name: str, query: str = "") -> str:
        """RAG на онтологии: фрагменты, привязанные к конкретному объекту,
        плюс (если задан query) фрагменты похожих по смыслу объектов."""
        direct = store.entity_chunks(kind, name)
        related_entities: list[dict[str, Any]] = []
        related_chunks: list[dict[str, Any]] = []
        if query.strip():
            vec = _embed(query)
            related_entities = [
                r for r in store.semantic_search_entities(vec, kind="", limit=5)
                if not (r["kind"] == kind and r["name"] == name)
            ]
            for r in related_entities:
                related_chunks.extend(store.entity_chunks(r["kind"], r["name"]))

        if not direct and not related_chunks:
            return (f"Фрагментов, привязанных к {kind}:{name}, не найдено. "
                   "Индексируйте текст с entity_refs, указывающим на этот "
                   "объект (rag_index(..., entity_refs='...')).")

        out = [f"Фрагменты по объекту {kind}:{name}:"]
        for hit in direct:
            out.append(f"\n[источник={hit['source']} фрагмент={hit.get('ord', 0)}]")
            out.append(hit["text"])
        if related_entities:
            out.append("\nПохожие по смыслу запроса объекты онтологии: " +
                      ", ".join(f"{r['kind']}:{r['name']}" for r in related_entities))
            for hit in related_chunks:
                out.append(f"\n[источник={hit['source']} "
                          f"фрагмент={hit.get('ord', 0)}, через похожий объект]")
                out.append(hit["text"])
        out.append("\nОтвечай, опираясь ТОЛЬКО на эти фрагменты, и указывай "
                  "источник каждого утверждения.")
        return "\n".join(out)

    def rag_stats() -> str:
        n_chunks = store.chunk_count()
        sources = store.chunk_sources()
        lines = [f"Проиндексировано: {n_chunks} фрагмент(ов) из {len(sources)} "
                f"источник(ов)"]
        if sources:
            lines.append("Источники: " + ", ".join(sources[:30])
                        + (f" … ещё {len(sources) - 30}"
                           if len(sources) > 30 else ""))
        return "\n".join(lines)

    return [
        Tool("rag_index",
             "Разбить текст (файл или строку) на фрагменты и проиндексировать "
             "для последующего поиска (rag_query/rag_query_entity). Повторная "
             "индексация того же source заменяет старые фрагменты. "
             "entity_refs='kind:name,kind:name' привязывает фрагменты к "
             "объектам онтологии — нужно для RAG на онтологии.",
             {"type": "object",
              "properties": {
                  "text": {"type": "string", "description": "Текст напрямую"},
                  "path": {"type": "string", "description": "Или файл из workspace"},
                  "source": {"type": "string",
                             "description": "Имя источника; по умолчанию — путь файла"},
                  "entity_refs": {"type": "string",
                                  "description": "'part:AB-01,assembly:Редуктор'"}},
              "required": []},
             rag_index),
        Tool("rag_query",
             "Обычный RAG: найти релевантные фрагменты по запросу (гибрид "
             "эмбеддингов и полнотекстового поиска) среди ВСЕХ "
             "проиндексированных документов.",
             {"type": "object",
              "properties": {
                  "query": {"type": "string"},
                  "top_k": {"type": "integer"},
                  "source": {"type": "string"}},
              "required": ["query"]},
             rag_query),
        Tool("rag_query_entity",
             "RAG на онтологии: найти фрагменты, привязанные к КОНКРЕТНОМУ "
             "объекту, и, если задан query, дополнить фрагментами похожих "
             "по смыслу объектов.",
             {"type": "object",
              "properties": {
                  "kind": {"type": "string"},
                  "name": {"type": "string"},
                  "query": {"type": "string"}},
              "required": ["kind", "name"]},
             rag_query_entity),
        Tool("rag_stats",
             "Сводка по индексу: сколько фрагментов и источников "
             "проиндексировано.",
             {"type": "object", "properties": {}, "required": []},
             rag_stats),
    ]
