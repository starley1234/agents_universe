"""Онтология и фрагменты знаний в PostgreSQL + pgvector.

Аналог agent/store.py, но для тех, кому нужна «настоящая» СУБД: общий
доступ из нескольких процессов/машин, надёжность транзакций, и —
главное — векторный поиск pgvector по эмбеддингам сущностей и
фрагментов текста прямо в базе, без выгрузки всего в память.

SQLite-хранилище (agent/store.py) остаётся полноценным вариантом по
умолчанию — ничего не ломается для тех, кому Postgres не нужен. Этот
модуль подключается ТОЛЬКО когда явно указан pg_dsn в конфиге, и только
если установлен psycopg (pip install "psycopg[binary]"). Импорт — как и
у pymupdf/python-docx — ленивый, внутри PgStore.__init__.

Схема сознательно похожа на store.py (entity/relation/chunk), чтобы
инструменты пересекались по смыслу и агенту не приходилось помнить два
разных языка описания графа знаний.
"""
from __future__ import annotations

import json
from typing import Any


class PgError(RuntimeError):
    """Ошибка работы с Postgres: подключение, отсутствие pgvector и т.п."""


def _require_psycopg():
    try:
        import psycopg  # type: ignore
    except ImportError as exc:
        raise PgError(
            'Навык pg_ontology требует psycopg. Установите: '
            'pip install "psycopg[binary]"'
        ) from exc
    return psycopg


class PgStore:
    """Онтология + фрагменты в PostgreSQL с pgvector.

    dim — размерность вектора эмбеддинга. Задаётся один раз при первом
    подключении (обычно определяется реальным вызовом эмбеддера) и
    фиксируется в схеме колонки vector(dim); смена модели эмбеддинга на
    другую размерность потребует новой базы/схемы — pgvector не умеет
    хранить векторы переменной длины в одной колонке, это ограничение
    самого расширения, а не наше.
    """

    def __init__(self, dsn: str, dim: int = 256) -> None:
        if not dsn:
            raise PgError(
                "Не задан pg_dsn. Укажите строку подключения вида "
                "postgresql://user:pass@host:5432/db в конфиге "
                "(поле pg_dsn) или переменной AGENT_PG_DSN."
            )
        psycopg = _require_psycopg()
        self._psycopg = psycopg
        self.dsn = dsn
        self.dim = dim
        try:
            self.conn = psycopg.connect(dsn, autocommit=True)
        except Exception as exc:  # OperationalError и т.п.
            raise PgError(f"Не удалось подключиться к Postgres: {exc}") from exc
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        cur = self.conn.cursor()
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception as exc:
            raise PgError(
                "Расширение pgvector недоступно на сервере. Установите "
                f"pgvector на стороне СУБД. Причина: {exc}"
            ) from exc
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS onto_entity(
                id SERIAL PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                props JSONB DEFAULT '{{}}',
                description TEXT DEFAULT '',
                embedding VECTOR({self.dim}),
                created TIMESTAMPTZ DEFAULT now(),
                UNIQUE(kind, name)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS onto_relation(
                id SERIAL PRIMARY KEY,
                subj INTEGER NOT NULL REFERENCES onto_entity(id) ON DELETE CASCADE,
                pred TEXT NOT NULL,
                obj INTEGER NOT NULL REFERENCES onto_entity(id) ON DELETE CASCADE,
                props JSONB DEFAULT '{}',
                created TIMESTAMPTZ DEFAULT now(),
                UNIQUE(subj, pred, obj)
            )
        """)
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS onto_chunk(
                id SERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                ord INTEGER DEFAULT 0,
                text TEXT NOT NULL,
                tags TEXT DEFAULT '',
                entity_refs JSONB DEFAULT '[]',
                embedding VECTOR({self.dim}),
                created TIMESTAMPTZ DEFAULT now()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_onto_chunk_source "
            "ON onto_chunk(source, ord)")

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _vec(v: list[float] | None) -> str | None:
        return None if v is None else "[" + ",".join(repr(float(x)) for x in v) + "]"

    # -------------------------------------------------------- сущности
    def upsert_entity(self, kind: str, name: str,
                      props: dict[str, Any] | None = None,
                      description: str = "",
                      embedding: list[float] | None = None) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, props, description FROM onto_entity WHERE kind=%s AND name=%s",
            (kind, name))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO onto_entity(kind,name,props,description,embedding) "
                "VALUES(%s,%s,%s,%s,%s) RETURNING id",
                (kind, name, json.dumps(props or {}, ensure_ascii=False),
                 description, self._vec(embedding)))
            return int(cur.fetchone()[0])
        eid, old_props, old_desc = row
        merged = {**(old_props or {}), **(props or {})}
        new_desc = description or old_desc
        if embedding is not None:
            cur.execute(
                "UPDATE onto_entity SET props=%s, description=%s, embedding=%s "
                "WHERE id=%s",
                (json.dumps(merged, ensure_ascii=False), new_desc,
                 self._vec(embedding), eid))
        else:
            cur.execute(
                "UPDATE onto_entity SET props=%s, description=%s WHERE id=%s",
                (json.dumps(merged, ensure_ascii=False), new_desc, eid))
        return int(eid)

    def get_entity(self, kind: str, name: str) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, kind, name, props, description FROM onto_entity "
            "WHERE kind=%s AND name=%s", (kind, name))
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "kind": row[1], "name": row[2],
                "props": row[3], "description": row[4]}

    # -------------------------------------------------------------- связи
    def link(self, subj: tuple[str, str], pred: str, obj: tuple[str, str],
             props: dict[str, Any] | None = None) -> bool:
        a = self.upsert_entity(*subj)
        b = self.upsert_entity(*obj)
        cur = self.conn.cursor()
        try:
            cur.execute(
                "INSERT INTO onto_relation(subj,pred,obj,props) VALUES(%s,%s,%s,%s)",
                (a, pred, b, json.dumps(props or {}, ensure_ascii=False)))
            return True
        except Exception:
            self.conn.rollback() if not self.conn.autocommit else None
            return False

    def neighbours(self, kind: str, name: str) -> list[dict[str, Any]]:
        ent = self.get_entity(kind, name)
        if not ent:
            return []
        cur = self.conn.cursor()
        cur.execute("""
            SELECT r.pred, e.kind, e.name, 'out' FROM onto_relation r
            JOIN onto_entity e ON e.id = r.obj WHERE r.subj = %s
            UNION ALL
            SELECT r.pred, e.kind, e.name, 'in' FROM onto_relation r
            JOIN onto_entity e ON e.id = r.subj WHERE r.obj = %s
        """, (ent["id"], ent["id"]))
        return [{"pred": p, "kind": k, "name": n, "dir": d}
                for p, k, n, d in cur.fetchall()]

    def subgraph(self, kind: str, name: str, depth: int = 2,
                 limit: int = 200) -> list[dict[str, Any]]:
        """BFS по графу до depth шагов — обзор окрестности объекта."""
        ent = self.get_entity(kind, name)
        if not ent:
            return []
        seen = {ent["id"]}
        frontier = [ent["id"]]
        edges: list[dict[str, Any]] = []
        for _ in range(max(1, depth)):
            if not frontier or len(edges) >= limit:
                break
            cur = self.conn.cursor()
            cur.execute("""
                SELECT r.subj, r.pred, r.obj, es.kind, es.name, eo.kind, eo.name
                FROM onto_relation r
                JOIN onto_entity es ON es.id = r.subj
                JOIN onto_entity eo ON eo.id = r.obj
                WHERE r.subj = ANY(%s) OR r.obj = ANY(%s)
            """, (frontier, frontier))
            next_frontier: list[int] = []
            for subj_id, pred, obj_id, sk, sn, ok, on in cur.fetchall():
                edges.append({"subject": [sk, sn], "predicate": pred,
                              "object": [ok, on]})
                for nid in (subj_id, obj_id):
                    if nid not in seen:
                        seen.add(nid)
                        next_frontier.append(nid)
            frontier = next_frontier
        return edges[:limit]

    def graph_stats(self) -> tuple[int, int]:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM onto_entity")
        e = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM onto_relation")
        r = cur.fetchone()[0]
        return int(e), int(r)

    def semantic_search_entities(self, embedding: list[float], kind: str = "",
                                 limit: int = 10) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if kind:
            cur.execute("""
                SELECT kind, name, description, 1 - (embedding <=> %s) AS score
                FROM onto_entity WHERE kind=%s AND embedding IS NOT NULL
                ORDER BY embedding <=> %s LIMIT %s
            """, (self._vec(embedding), kind, self._vec(embedding), limit))
        else:
            cur.execute("""
                SELECT kind, name, description, 1 - (embedding <=> %s) AS score
                FROM onto_entity WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s LIMIT %s
            """, (self._vec(embedding), self._vec(embedding), limit))
        return [{"kind": k, "name": n, "description": d, "score": float(s)}
                for k, n, d, s in cur.fetchall()]

    # ---------------------------------------------------------- фрагменты
    def add_chunks(self, source: str, texts: list[str],
                   embeddings: list[list[float] | None] | None = None,
                   tags: str = "",
                   entity_refs: list[tuple[str, str]] | None = None) -> list[int]:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM onto_chunk WHERE source=%s", (source,))
        refs = json.dumps([list(r) for r in (entity_refs or [])], ensure_ascii=False)
        ids = []
        embs = embeddings or [None] * len(texts)
        for i, (text, emb) in enumerate(zip(texts, embs)):
            cur.execute(
                "INSERT INTO onto_chunk(source,ord,text,tags,entity_refs,embedding) "
                "VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                (source, i, text, tags, refs, self._vec(emb)))
            ids.append(int(cur.fetchone()[0]))
        return ids

    def semantic_search_chunks(self, embedding: list[float],
                               limit: int = 6, source: str | None = None) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        if source:
            cur.execute("""
                SELECT source, ord, text, entity_refs, 1 - (embedding <=> %s) AS score
                FROM onto_chunk WHERE embedding IS NOT NULL AND source=%s
                ORDER BY embedding <=> %s LIMIT %s
            """, (self._vec(embedding), source, self._vec(embedding), limit))
        else:
            cur.execute("""
                SELECT source, ord, text, entity_refs, 1 - (embedding <=> %s) AS score
                FROM onto_chunk WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s LIMIT %s
            """, (self._vec(embedding), self._vec(embedding), limit))
        return [{"source": s, "ord": o, "text": t, "entity_refs": refs, "score": float(sc)}
                for s, o, t, refs, sc in cur.fetchall()]

    def sources(self) -> list[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT DISTINCT source FROM onto_chunk ORDER BY source")
        return [r[0] for r in cur.fetchall()]

    def chunks_for_entities(self, refs: list[tuple[str, str]]) -> list[dict[str, Any]]:
        """Фрагменты, привязанные к любому из объектов — для RAG на графе."""
        if not refs:
            return []
        cur = self.conn.cursor()
        cur.execute("SELECT id, source, ord, text, entity_refs FROM onto_chunk")
        wanted = {tuple(r) for r in refs}
        out = []
        for cid, source, ord_, text, entity_refs in cur.fetchall():
            try:
                have = {tuple(r) for r in (entity_refs or [])}
            except TypeError:
                have = set()
            if have & wanted:
                out.append({"id": cid, "source": source, "ord": ord_, "text": text})
        return out

    def chunk_count(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM onto_chunk")
        return int(cur.fetchone()[0])
