"""Навык «pg_ontology»: агент ведёт онтологию в PostgreSQL + pgvector и
разбирается в ней — заводит объекты, связывает их, ищет семантически
похожие (через embedding) и обходит граф вокруг объекта.

Отличие от навыка memory/SQLite (agent/store.py): та онтология живёт в
одном файле agent.db на одной машине и ищет только по FTS (точные слова
и словоформы). Здесь — общая база, доступная нескольким агентам/машинам
одновременно, и ПОИСК ПО СМЫСЛУ через векторное расстояние pgvector:
запрос «шестерня» найдёт объект, описанный как «зубчатое колесо», если
у обоих есть эмбеддинг.

Инструменты используют ОБЩИЙ эмбеддер (agent/llm/embeddings.py) — тот
же, что и навык rag, поэтому один и тот же вектор пригоден для поиска
и по онтологии, и по фрагментам документов.

Требует psycopg (ленивый импорт внутри store_pg.PgStore) и доступный
Postgres с расширением pgvector — при их отсутствии инструменты вернут
понятную ошибку, а не уронят сборку агента.
"""
from __future__ import annotations

import json

from ..llm.embeddings import BaseEmbedder, EmbeddingError
from ..store_pg import PgError, PgStore
from ..tools.base import Tool, ToolError


def build(dsn: str, embedder: BaseEmbedder, dim: int) -> list[Tool]:
    _store: list[PgStore | None] = [None]

    def store() -> PgStore:
        if _store[0] is None:
            try:
                _store[0] = PgStore(dsn, dim=dim)
            except PgError as exc:
                raise ToolError(str(exc)) from exc
        return _store[0]

    def _embed(text: str) -> list[float]:
        try:
            return embedder.embed_one(text)
        except EmbeddingError as exc:
            raise ToolError(f"Не удалось получить эмбеддинг: {exc}") from exc

    # ------------------------------------------------------------ объекты
    def pg_upsert_entity(kind: str, name: str, props: str = "{}",
                         description: str = "") -> str:
        try:
            data = json.loads(props) if props.strip() else {}
        except json.JSONDecodeError as exc:
            raise ToolError(f"props должен быть JSON-объектом: {exc}") from exc
        emb = _embed(f"{kind} {name} {description}".strip()) if description else None
        eid = store().upsert_entity(kind, name, data, description, embedding=emb)
        return f"Объект {kind}:{name} сохранён в PostgreSQL (id={eid})"

    def pg_link(subject_kind: str, subject: str, predicate: str,
               object_kind: str, object: str) -> str:
        created = store().link((subject_kind, subject), predicate,
                               (object_kind, object))
        e, r = store().graph_stats()
        return (f"{'Связь создана' if created else 'Связь уже была'}: "
               f"{subject_kind}:{subject} --{predicate}--> "
               f"{object_kind}:{object}\nВ графе PostgreSQL: {e} объектов, "
               f"{r} связей")

    def pg_neighbours(kind: str, name: str) -> str:
        rows = store().neighbours(kind, name)
        if not rows:
            return f"{kind}:{name} — связей нет (объект может быть не создан)"
        out = [f"{kind}:{name}"]
        for r in rows:
            arrow = "-->" if r["dir"] == "out" else "<--"
            out.append(f"  {arrow} {r['pred']} {r['kind']}:{r['name']}")
        return "\n".join(out)

    def pg_subgraph(kind: str, name: str, depth: int = 2) -> str:
        edges = store().subgraph(kind, name, depth=depth)
        if not edges:
            return f"{kind}:{name} — окрестность пуста"
        out = [f"Окрестность {kind}:{name} (глубина {depth}):"]
        for e in edges:
            out.append(f"  {e['subject'][0]}:{e['subject'][1]} "
                      f"--{e['predicate']}--> {e['object'][0]}:{e['object'][1]}")
        return "\n".join(out)

    def pg_semantic_search(query: str, kind: str = "", limit: int = 10) -> str:
        vec = _embed(query)
        rows = store().semantic_search_entities(vec, kind=kind, limit=limit)
        if not rows:
            return f"По смыслу запроса {query!r} в онтологии ничего не найдено"
        out = [f"Похожие на {query!r} объекты:"]
        for r in rows:
            desc = f" — {r['description']}" if r["description"] else ""
            out.append(f"  {r['kind']}:{r['name']} (сходство {r['score']:.3f}){desc}")
        return "\n".join(out)

    def pg_stats() -> str:
        e, r = store().graph_stats()
        c = store().chunk_count()
        return f"PostgreSQL: {e} объектов, {r} связей, {c} фрагментов текста"

    return [
        Tool("pg_upsert_entity",
             "Создать/дополнить объект онтологии в PostgreSQL+pgvector. "
             "Если указано description — считается эмбеддинг для "
             "последующего семантического поиска (pg_semantic_search).",
             {"type": "object",
              "properties": {
                  "kind": {"type": "string"},
                  "name": {"type": "string"},
                  "props": {"type": "string", "description": "JSON со свойствами"},
                  "description": {"type": "string",
                                  "description": "Текстовое описание для "
                                                "семантического поиска"}},
              "required": ["kind", "name"]},
             pg_upsert_entity),
        Tool("pg_link",
             "Связать два объекта онтологии в PostgreSQL: субъект-предикат-"
             "объект.",
             {"type": "object",
              "properties": {
                  "subject_kind": {"type": "string"},
                  "subject": {"type": "string"},
                  "predicate": {"type": "string"},
                  "object_kind": {"type": "string"},
                  "object": {"type": "string"}},
              "required": ["subject_kind", "subject", "predicate",
                           "object_kind", "object"]},
             pg_link),
        Tool("pg_neighbours",
             "Прямые связи объекта онтологии в PostgreSQL.",
             {"type": "object",
              "properties": {"kind": {"type": "string"},
                             "name": {"type": "string"}},
              "required": ["kind", "name"]},
             pg_neighbours),
        Tool("pg_subgraph",
             "Обход окрестности объекта на несколько шагов по графу — "
             "чтобы разобраться, что вокруг объекта происходит, а не "
             "смотреть только на прямых соседей.",
             {"type": "object",
              "properties": {"kind": {"type": "string"},
                             "name": {"type": "string"},
                             "depth": {"type": "integer",
                                      "description": "Глубина обхода, по умолчанию 2"}},
              "required": ["kind", "name"]},
             pg_subgraph),
        Tool("pg_semantic_search",
             "Найти объекты онтологии БЛИЗКИЕ ПО СМЫСЛУ к запросу (через "
             "эмбеддинги, не по точному совпадению слов). Например, запрос "
             "'зубчатое колесо' найдёт объект с описанием 'шестерня', даже "
             "если слова разные.",
             {"type": "object",
              "properties": {
                  "query": {"type": "string"},
                  "kind": {"type": "string",
                           "description": "Ограничить типом объекта (необязательно)"},
                  "limit": {"type": "integer"}},
              "required": ["query"]},
             pg_semantic_search),
        Tool("pg_stats",
             "Сводка по онтологии в PostgreSQL: сколько объектов, связей, "
             "фрагментов.",
             {"type": "object", "properties": {}, "required": []},
             pg_stats),
    ]
