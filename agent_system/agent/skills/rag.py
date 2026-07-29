"""Навык «rag»: индексация текста в векторную/полнотекстовую базу и поиск
по нему для ответа с опорой на источник — как обычный RAG, так и на базе
онтологии (графа сущностей и связей).

Почему навык, а не просто «читай файл целиком»: у модели ограниченный
контекст, а документов может быть много и они большие. RAG решает это
в два шага — один раз при индексации (разбить текст на фрагменты и
посчитать эмбеддинги) и на каждый запрос (найти релевантные фрагменты и
подставить только их, а не всё хранилище).

ДВА РЕЖИМА:
  1. Обычный RAG (rag_index, rag_query) — гибридный поиск: эмбеддинги
     (семантическое сходство, косинус) + FTS (точные термины и словоформы,
     тот же движок, что у recall() в Store). Гибрид нужен потому, что
     эмбеддинги плохо ловят точные идентификаторы (номера деталей, коды
     ГОСТ), а FTS не понимает синонимы — вместе они закрывают оба случая.
  2. RAG на онтологии (rag_query_entity) — вместо поиска по всем
     фрагментам ищет СНАЧАЛА релевантные ОБЪЕКТЫ онтологии (через
     pg_semantic_search/навык pg_ontology или Store.semantic_search_entities),
     а затем подтягивает фрагменты, ПРИВЯЗАННЫЕ к этим объектам
     (entity_refs). Это точнее для вопросов вида «что известно про деталь
     AB-01» — ответ ограничен документами про конкретный объект, а не
     про всё, что похоже по тексту.

ДВА БЭКЕНДА, один и тот же набор инструментов сверху:
  * SQLite (agent/store.py, Store)     — по умолчанию, косинус в памяти;
    годится для одного agent.db на одной машине.
  * PostgreSQL+pgvector (store_pg.py)  — если задан pg_dsn: векторный
    поиск делает сама БД, годится для больших объёмов и общего доступа.
Оба бэкенда реализуют один и тот же протокол (см. _Backend ниже), поэтому
инструменты не знают, с каким хранилищем говорят.

Источник фрагментов — ЛЮБОЙ готовый текст: результат pdf_extract/
doc_extract, обычный markdown-файл, произвольная строка. Сам навык не
парсит документы — это уже делают pdf/docparse; rag только режет текст
на фрагменты, индексирует и ищет.
"""
from __future__ import annotations

import re
from typing import Any, Protocol

from ..llm.embeddings import BaseEmbedder, EmbeddingError
from ..store import Store
from ..tools.base import Tool, ToolError, Workspace

MAX_INDEX_CHARS = 2_000_000     # грубая защита от индексации гигантских файлов


# ============================================================ чанкинг
def chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    """Разбивает текст на фрагменты по границам абзацев, с перекрытием.

    ПОЧЕМУ по абзацам, а не просто «резать каждые N символов»: обрезка
    посреди предложения/таблицы портит смысл фрагмента и мешает модели
    его понять при поиске. Абзац — минимальная единица, которую не режем
    внутри, если он помещается целиком; слишком длинный абзац всё же
    приходится резать жёстко (иначе для сплошного текста без пустых
    строк чанкинг не сработает вообще).

    Перекрытие (chunk_overlap символов хвоста предыдущего фрагмента в
    начале следующего) снижает риск, что важная мысль разорвётся ровно
    на границе двух фрагментов и не найдётся целиком ни в одном из них.
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
        # абзац сам по себе длиннее chunk_size — режем жёстко с перекрытием
        while len(current) > chunk_size:
            chunks.append(current[:chunk_size])
            current = current[chunk_size - chunk_overlap:]
    if current:
        chunks.append(current)
    return chunks


# =================================================== единый протокол бэкенда
class _Backend(Protocol):
    """То, что нужно навыку rag от хранилища — реализуют и Store, и PgStore
    (через тонкую обёртку ниже), чтобы инструменты не завязывались на
    конкретную СУБД."""

    def index_source(self, source: str, chunks: list[str],
                     embeddings: list[list[float]] | None,
                     entity_refs: list[tuple[str, str]]) -> int: ...
    def search_vector(self, embedding: list[float], limit: int,
                      source: str | None) -> list[dict[str, Any]]: ...
    def search_text(self, query: str, limit: int) -> list[dict[str, Any]]: ...
    def search_by_entity(self, kind: str, name: str) -> list[dict[str, Any]]: ...
    def semantic_entities(self, embedding: list[float], kind: str,
                          limit: int) -> list[dict[str, Any]]: ...
    def stats(self) -> tuple[int, int]: ...          # (фрагментов, источников)
    def list_sources(self) -> list[str]: ...


class _SQLiteBackend:
    """Обёртка над Store — единый протокол для rag поверх SQLite."""

    def __init__(self, store: Store, run_id_getter) -> None:
        self.store = store
        self.run_id_getter = run_id_getter

    def index_source(self, source, chunks, embeddings, entity_refs):
        ids = self.store.add_chunks(
            source, chunks, entity_refs=entity_refs,
            run_id=self.run_id_getter() if self.run_id_getter else None)
        if embeddings:
            for cid, emb in zip(ids, embeddings):
                if emb is not None:
                    self.store.set_chunk_embedding(cid, emb)
        return len(ids)

    def search_vector(self, embedding, limit, source):
        return self.store.semantic_search_chunks(embedding, limit=limit,
                                                  source=source)

    def search_text(self, query, limit):
        return self.store.fts_chunks(query, limit=limit)

    def search_by_entity(self, kind, name):
        return self.store.entity_chunks(kind, name)

    def semantic_entities(self, embedding, kind, limit):
        return self.store.semantic_search_entities(embedding, kind=kind,
                                                    limit=limit)

    def stats(self):
        return self.store.chunk_count(), len(self.store.sources())

    def list_sources(self):
        return self.store.sources()


class _PgBackend:
    """Обёртка над PgStore — тот же протокол поверх PostgreSQL+pgvector.

    Подключение ЛЕНИВОЕ (как в skills/pg_ontology.py): PgStore создаётся
    при первом реальном вызове инструмента, а не при сборке агента —
    недоступный на момент старта Postgres не должен ронять build_agent.

    FTS у PgStore нет (это не задача pgvector) — search_text здесь
    возвращает пусто, а не роняет вызов: гибридный поиск в rag_query
    просто останется чисто векторным на этом бэкенде, что и ожидаемо
    от «взрослой» СУБД, ориентированной на векторный поиск.
    """

    def __init__(self, dsn: str, dim_getter) -> None:
        self.dsn = dsn
        self.dim_getter = dim_getter          # () -> int, вызывается лениво
        self._pg = None

    @property
    def pg(self):
        if self._pg is None:
            from ..store_pg import PgError, PgStore
            try:
                self._pg = PgStore(self.dsn, dim=self.dim_getter())
            except PgError as exc:
                raise ToolError(str(exc)) from exc
        return self._pg

    def index_source(self, source, chunks, embeddings, entity_refs):
        embs = embeddings if embeddings else [None] * len(chunks)
        ids = self.pg.add_chunks(source, chunks, embeddings=embs,
                                 entity_refs=entity_refs)
        return len(ids)

    def search_vector(self, embedding, limit, source):
        return self.pg.semantic_search_chunks(embedding, limit=limit,
                                              source=source)

    def search_text(self, query, limit):
        return []

    def search_by_entity(self, kind, name):
        return self.pg.chunks_for_entities([(kind, name)])

    def semantic_entities(self, embedding, kind, limit):
        return self.pg.semantic_search_entities(embedding, kind=kind, limit=limit)

    def stats(self):
        return self.pg.chunk_count(), len(self.pg.sources())

    def list_sources(self):
        return self.pg.sources()


# ==================================================================== build
def build(ws: Workspace, embedder: BaseEmbedder, backend: _Backend,
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
        """Возвращает (тело_текста, имя_источника)."""
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
        Повторная индексация того же source ЗАМЕНЯЕТ старые фрагменты
        (не плодит дубли при переиндексации обновлённого документа)."""
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
        n = backend.index_source(src, chunks, embeddings, refs)
        return (f"Проиндексировано {src!r}: {n} фрагмент(ов) "
               f"(по ~{chunk_size} симв., перекрытие {chunk_overlap})"
               + (f", привязано к {len(refs)} объект(ам) онтологии"
                  if refs else ""))

    def rag_query(query: str, top_k: int = 0, source: str = "") -> str:
        """Обычный RAG: гибридный поиск (эмбеддинги + FTS) по всем
        проиндексированным фрагментам, с указанием источника у каждого —
        отвечай ТОЛЬКО на основе них и цитируй source."""
        if not query.strip():
            raise ToolError("Пустой запрос")
        k = top_k if top_k > 0 else default_top_k
        vec = _embed(query)
        vector_hits = backend.search_vector(vec, limit=k, source=source or None)
        text_hits = backend.search_text(query, limit=k)

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
        """RAG на онтологии: сначала находит фрагменты, ПРИВЯЗАННЫЕ к
        объекту (entity_refs при индексации), затем, если query задан,
        дополняет их семантически похожими объектами онтологии — так
        вопрос «что известно про AB-01» не размывается похожими по
        тексту, но не связанными фрагментами."""
        direct = backend.search_by_entity(kind, name)
        related_entities: list[dict[str, Any]] = []
        related_chunks: list[dict[str, Any]] = []
        if query.strip():
            vec = _embed(query)
            related_entities = [
                r for r in backend.semantic_entities(vec, kind="", limit=5)
                if not (r["kind"] == kind and r["name"] == name)
            ]
            for r in related_entities:
                related_chunks.extend(backend.search_by_entity(r["kind"], r["name"]))

        if not direct and not related_chunks:
            return (f"Фрагментов, привязанных к {kind}:{name}, не найдено. "
                   "Индексируйте текст с entity_refs, указывающим на этот "
                   "объект (rag_index(..., entity_refs='...')).")

        out = [f"Фрагменты по объекту {kind}:{name}:"]
        for hit in direct:
            out.append(f"\n[источник={hit['source']} "
                      f"фрагмент={hit.get('ord', 0)}]")
            out.append(hit["text"])
        if related_entities:
            out.append("\nПохожие по смыслу запроса объекты онтологии: " +
                      ", ".join(f"{r['kind']}:{r['name']}"
                               for r in related_entities))
            for hit in related_chunks:
                out.append(f"\n[источник={hit['source']} "
                          f"фрагмент={hit.get('ord', 0)}, через похожий объект]")
                out.append(hit["text"])
        out.append("\nОтвечай, опираясь ТОЛЬКО на эти фрагменты, и указывай "
                  "источник каждого утверждения.")
        return "\n".join(out)

    def rag_stats() -> str:
        n_chunks, n_sources = backend.stats()
        sources = backend.list_sources()
        lines = [f"Проиндексировано: {n_chunks} фрагмент(ов) из {n_sources} "
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
             "проиндексированных документов. Используй перед тем, как "
             "отвечать на вопрос по большому корпусу — не читай все файлы "
             "целиком.",
             {"type": "object",
              "properties": {
                  "query": {"type": "string"},
                  "top_k": {"type": "integer",
                            "description": "Сколько фрагментов вернуть, 0 = по умолчанию"},
                  "source": {"type": "string",
                             "description": "Ограничить одним источником (необязательно)"}},
              "required": ["query"]},
             rag_query),
        Tool("rag_query_entity",
             "RAG на онтологии: найти фрагменты, привязанные к КОНКРЕТНОМУ "
             "объекту (entity_refs при индексации), и, если задан query, "
             "дополнить фрагментами похожих по смыслу объектов. Точнее "
             "rag_query для вопросов вида «что известно про X».",
             {"type": "object",
              "properties": {
                  "kind": {"type": "string"},
                  "name": {"type": "string"},
                  "query": {"type": "string",
                            "description": "Расширить похожими объектами (необязательно)"}},
              "required": ["kind", "name"]},
             rag_query_entity),
        Tool("rag_stats",
             "Сводка по индексу: сколько фрагментов и источников "
             "проиндексировано.",
             {"type": "object", "properties": {}, "required": []},
             rag_stats),
    ]
