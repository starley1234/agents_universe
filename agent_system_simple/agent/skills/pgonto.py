"""Онтология в PostgreSQL + pgvector.

Когда это нужно (проверено замерами, см. OVERVIEW.md):
  * агенты работают на РАЗНЫХ машинах — SQLite по сети не работает;
  * нужен СЕМАНТИЧЕСКИЙ поиск на десятках тысяч записей.

Когда НЕ нужно: один агент на одной машине. SQLite выдерживает
10 параллельных процессов без ошибок и ищет за 0.3 мс на 300k фактов.
Этот навык — для первого случая, а не замена SQLite.

Зависимость psycopg НЕ обязательна: без неё навык просто не подключается,
остальная система работает. Ставится как `pip install psycopg[binary]`.

Эмбеддинги считает ваша же локальная модель через OpenAI-совместимый
/v1/embeddings — отдельный сервис не нужен.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from ..tools.base import Tool, ToolError

SCHEMA = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS onto_entity(
  id BIGSERIAL PRIMARY KEY,
  kind TEXT NOT NULL,
  name TEXT NOT NULL,
  props JSONB DEFAULT '{}'::jsonb,
  embedding vector(%(dim)s),
  created TIMESTAMPTZ DEFAULT now(),
  UNIQUE(kind, name)
);

CREATE TABLE IF NOT EXISTS onto_relation(
  id BIGSERIAL PRIMARY KEY,
  subj BIGINT REFERENCES onto_entity(id) ON DELETE CASCADE,
  pred TEXT NOT NULL,
  obj  BIGINT REFERENCES onto_entity(id) ON DELETE CASCADE,
  props JSONB DEFAULT '{}'::jsonb,
  created TIMESTAMPTZ DEFAULT now(),
  UNIQUE(subj, pred, obj)
);

CREATE TABLE IF NOT EXISTS onto_chunk(
  id BIGSERIAL PRIMARY KEY,
  doc TEXT NOT NULL,
  section TEXT DEFAULT '',
  text TEXT NOT NULL,
  entity_id BIGINT REFERENCES onto_entity(id) ON DELETE SET NULL,
  embedding vector(%(dim)s),
  created TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_rel_subj ON onto_relation(subj);
CREATE INDEX IF NOT EXISTS ix_rel_obj  ON onto_relation(obj);
CREATE INDEX IF NOT EXISTS ix_chunk_doc ON onto_chunk(doc);
"""

# Индекс строим отдельно: на пустой таблице ivfflat бесполезен и ругается
INDEX_SQL = """
CREATE INDEX IF NOT EXISTS ix_entity_vec ON onto_entity
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = %(lists)s);
CREATE INDEX IF NOT EXISTS ix_chunk_vec ON onto_chunk
  USING ivfflat (embedding vector_cosine_ops) WITH (lists = %(lists)s);
"""


class PgOnto:
    """Подключение к PostgreSQL. Недоступность БД не роняет агента."""

    def __init__(self, dsn: str, embed_url: str = "", embed_model: str = "",
                 embed_key: str = "", dim: int = 768) -> None:
        self.dsn = dsn
        self.embed_url = embed_url.rstrip("/")
        self.embed_model = embed_model
        self.embed_key = embed_key
        self.dim = dim
        self.conn: Any = None
        self.error = ""

    def connect(self) -> bool:
        try:
            import psycopg                                  # type: ignore
        except ImportError:
            self.error = ("не установлен psycopg. Поставьте: "
                          "pip install 'psycopg[binary]'")
            return False
        try:
            self.conn = psycopg.connect(self.dsn, autocommit=True)
            with self.conn.cursor() as cur:
                cur.execute(SCHEMA % {"dim": self.dim})
            return True
        except Exception as exc:                            # noqa: BLE001
            self.error = str(exc)[:300]
            self.conn = None
            return False

    # ---------------------------------------------------- эмбеддинги
    def embed(self, texts: list[str]) -> list[list[float]] | None:
        """Векторы через OpenAI-совместимый эндпоинт. None, если не настроен."""
        if not self.embed_url or not texts:
            return None
        body = json.dumps({"model": self.embed_model, "input": texts})
        req = urllib.request.Request(
            f"{self.embed_url}/embeddings", data=body.encode(),
            headers={"Content-Type": "application/json",
                     **({"Authorization": f"Bearer {self.embed_key}"}
                        if self.embed_key else {})},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode())
            return [d["embedding"] for d in data["data"]]
        except (urllib.error.URLError, KeyError, json.JSONDecodeError) as exc:
            raise ToolError(
                f"не удалось получить эмбеддинги от {self.embed_url}: {exc}"
            ) from exc

    def _vec(self, v: list[float]) -> str:
        return "[" + ",".join(f"{x:.6f}" for x in v) + "]"

    def require(self) -> Any:
        if self.conn is None:
            raise ToolError(f"нет связи с PostgreSQL: {self.error}")
        return self.conn


def build(pg: PgOnto) -> list[Tool]:

    def pg_status() -> str:
        if pg.conn is None:
            return (f"PostgreSQL НЕДОСТУПЕН: {pg.error}\n"
                    "Онтология работает в SQLite (навык memory).")
        with pg.require().cursor() as cur:
            cur.execute("SELECT count(*) FROM onto_entity")
            e = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM onto_relation")
            r = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM onto_chunk")
            c = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM onto_entity "
                        "WHERE embedding IS NOT NULL")
            ve = cur.fetchone()[0]
            cur.execute("SELECT kind, count(*) FROM onto_entity "
                        "GROUP BY kind ORDER BY 2 DESC LIMIT 10")
            kinds = cur.fetchall()
        out = [f"PostgreSQL подключён: {e} объектов, {r} связей, "
               f"{c} фрагментов",
               f"С векторами: {ve} из {e}"
               + ("" if pg.embed_url else "  (эмбеддинги не настроены)"), ""]
        out += [f"  {k}: {n}" for k, n in kinds]
        return "\n".join(out)

    def pg_add_entity(kind: str, name: str, props: str = "{}") -> str:
        try:
            data = json.loads(props) if props.strip() else {}
        except json.JSONDecodeError as exc:
            raise ToolError(f"props должен быть JSON: {exc}") from exc
        vecs = pg.embed([f"{kind}: {name}"])
        with pg.require().cursor() as cur:
            cur.execute(
                "INSERT INTO onto_entity(kind,name,props,embedding) "
                "VALUES(%s,%s,%s,%s) ON CONFLICT (kind,name) DO UPDATE "
                "SET props = onto_entity.props || EXCLUDED.props "
                "RETURNING id",
                (kind, name, json.dumps(data),
                 pg._vec(vecs[0]) if vecs else None))
            eid = cur.fetchone()[0]
        return f"объект {kind}:{name} сохранён (id={eid})"

    def pg_link(subject_kind: str, subject: str, predicate: str,
                object_kind: str, object: str) -> str:
        with pg.require().cursor() as cur:
            ids = []
            for k, n in ((subject_kind, subject), (object_kind, object)):
                cur.execute(
                    "INSERT INTO onto_entity(kind,name) VALUES(%s,%s) "
                    "ON CONFLICT (kind,name) DO UPDATE SET kind=EXCLUDED.kind "
                    "RETURNING id", (k, n))
                ids.append(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO onto_relation(subj,pred,obj) VALUES(%s,%s,%s) "
                "ON CONFLICT DO NOTHING RETURNING id", (ids[0], predicate, ids[1]))
            created = cur.fetchone() is not None
        return (f"{'связь создана' if created else 'связь уже была'}: "
                f"{subject_kind}:{subject} --{predicate}--> "
                f"{object_kind}:{object}")

    def pg_query(kind: str = "", name_like: str = "", limit: int = 20) -> str:
        cond, args = [], []
        if kind:
            cond.append("kind = %s")
            args.append(kind)
        if name_like:
            cond.append("name ILIKE %s")
            args.append(f"%{name_like}%")
        where = (" WHERE " + " AND ".join(cond)) if cond else ""
        args.append(limit)
        with pg.require().cursor() as cur:
            cur.execute(f"SELECT kind,name,props FROM onto_entity{where} "
                        f"ORDER BY id DESC LIMIT %s", args)
            rows = cur.fetchall()
        if not rows:
            return "объектов не найдено"
        return "\n".join(f"{k}:{n}" + (f"  {json.dumps(p, ensure_ascii=False)}"
                                       if p and p != {} else "")
                         for k, n, p in rows)

    def pg_neighbours(kind: str, name: str, depth: int = 1) -> str:
        """Связи объекта. depth=2 — ещё и связи соседей."""
        depth = max(1, min(depth, 3))
        sql = """
        WITH RECURSIVE walk(id, kind, name, path, lvl) AS (
          SELECT id, kind, name, kind || ':' || name, 0
            FROM onto_entity WHERE kind=%s AND name=%s
          UNION ALL
          SELECT e.id, e.kind, e.name,
                 w.path || ' -> ' || r.pred || ' -> ' || e.kind || ':' || e.name,
                 w.lvl + 1
            FROM walk w
            JOIN onto_relation r ON r.subj = w.id
            JOIN onto_entity e ON e.id = r.obj
           WHERE w.lvl < %s
        )
        SELECT path, lvl FROM walk WHERE lvl > 0 ORDER BY lvl, path LIMIT 100
        """
        with pg.require().cursor() as cur:
            cur.execute(sql, (kind, name, depth))
            rows = cur.fetchall()
        if not rows:
            return f"{kind}:{name} — связей нет (или объект не создан)"
        return "\n".join(f"{'  ' * (l - 1)}{p}" for p, l in rows)

    def pg_semantic_search(query: str, limit: int = 5,
                           target: str = "chunk") -> str:
        """Поиск ПО СМЫСЛУ через pgvector — то, чего не умеет FTS."""
        vecs = pg.embed([query])
        if not vecs:
            raise ToolError(
                "эмбеддинги не настроены: укажите pg.embed_url и embed_model "
                "в конфиге. Без них доступен только pg_query по названию.")
        table = "onto_chunk" if target == "chunk" else "onto_entity"
        col = "text" if target == "chunk" else "kind || ':' || name"
        with pg.require().cursor() as cur:
            cur.execute(
                f"SELECT {col}, 1 - (embedding <=> %s::vector) AS score "
                f"FROM {table} WHERE embedding IS NOT NULL "
                f"ORDER BY embedding <=> %s::vector LIMIT %s",
                (pg._vec(vecs[0]), pg._vec(vecs[0]), limit))
            rows = cur.fetchall()
        if not rows:
            return ("ничего не найдено: в таблице нет записей с векторами. "
                    "Добавьте данные при настроенных эмбеддингах.")
        return "\n\n".join(f"[близость {s:.3f}] {t[:600]}" for t, s in rows)

    def pg_add_chunk(doc: str, text: str, section: str = "",
                     entity_kind: str = "", entity_name: str = "") -> str:
        vecs = pg.embed([text])
        with pg.require().cursor() as cur:
            eid = None
            if entity_kind and entity_name:
                cur.execute(
                    "INSERT INTO onto_entity(kind,name) VALUES(%s,%s) "
                    "ON CONFLICT (kind,name) DO UPDATE SET kind=EXCLUDED.kind "
                    "RETURNING id", (entity_kind, entity_name))
                eid = cur.fetchone()[0]
            cur.execute(
                "INSERT INTO onto_chunk(doc,section,text,entity_id,embedding) "
                "VALUES(%s,%s,%s,%s,%s) RETURNING id",
                (doc, section, text, eid, pg._vec(vecs[0]) if vecs else None))
            cid = cur.fetchone()[0]
        return (f"фрагмент сохранён (id={cid})"
                + ("" if vecs else " БЕЗ вектора: эмбеддинги не настроены"))

    def pg_build_index() -> str:
        """Векторный индекс. На малых объёмах он не нужен и даже вреден."""
        with pg.require().cursor() as cur:
            cur.execute("SELECT count(*) FROM onto_chunk "
                        "WHERE embedding IS NOT NULL")
            n = cur.fetchone()[0]
            if n < 1000:
                return (f"строк с векторами: {n}. Индекс ivfflat нужен от "
                        "~1000 строк — на меньших объёмах полный перебор "
                        "быстрее и точнее. Индекс не создан.")
            lists = max(1, min(int(n ** 0.5), 2000))
            cur.execute(INDEX_SQL % {"lists": lists})
        return f"индекс ivfflat создан (lists={lists}) для {n} строк"

    return [
        Tool("pg_status", "Состояние онтологии в PostgreSQL: объекты, связи, "
             "фрагменты, векторы.",
             {"type": "object", "properties": {}, "required": []}, pg_status),
        Tool("pg_add_entity", "Создать или дополнить объект онтологии.",
             {"type": "object",
              "properties": {"kind": {"type": "string"},
                             "name": {"type": "string"},
                             "props": {"type": "string"}},
              "required": ["kind", "name"]}, pg_add_entity),
        Tool("pg_link", "Связать два объекта: субъект-предикат-объект.",
             {"type": "object",
              "properties": {"subject_kind": {"type": "string"},
                             "subject": {"type": "string"},
                             "predicate": {"type": "string"},
                             "object_kind": {"type": "string"},
                             "object": {"type": "string"}},
              "required": ["subject_kind", "subject", "predicate",
                           "object_kind", "object"]}, pg_link),
        Tool("pg_query", "Найти объекты онтологии по типу и части названия.",
             {"type": "object",
              "properties": {"kind": {"type": "string"},
                             "name_like": {"type": "string"},
                             "limit": {"type": "integer"}},
              "required": []}, pg_query),
        Tool("pg_neighbours",
             "Обойти граф от объекта: связи на глубину 1-3. Так видно "
             "цепочки зависимостей, а не только прямых соседей.",
             {"type": "object",
              "properties": {"kind": {"type": "string"},
                             "name": {"type": "string"},
                             "depth": {"type": "integer"}},
              "required": ["kind", "name"]}, pg_neighbours),
        Tool("pg_add_chunk",
             "Сохранить фрагмент текста с вектором и привязкой к объекту.",
             {"type": "object",
              "properties": {"doc": {"type": "string"},
                             "text": {"type": "string"},
                             "section": {"type": "string"},
                             "entity_kind": {"type": "string"},
                             "entity_name": {"type": "string"}},
              "required": ["doc", "text"]}, pg_add_chunk),
        Tool("pg_semantic_search",
             "Поиск ПО СМЫСЛУ через pgvector: находит близкое по значению, "
             "даже если слова разные. Требует настроенных эмбеддингов.",
             {"type": "object",
              "properties": {"query": {"type": "string"},
                             "limit": {"type": "integer"},
                             "target": {"type": "string",
                                        "description": "chunk или entity"}},
              "required": ["query"]}, pg_semantic_search),
        Tool("pg_build_index",
             "Создать векторный индекс ivfflat (нужен от ~1000 записей).",
             {"type": "object", "properties": {}, "required": []},
             pg_build_index),
    ]
