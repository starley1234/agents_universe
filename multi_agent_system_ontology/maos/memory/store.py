"""Хранилище MAOS: PostgreSQL + pgvector.

ОБЯЗАТЕЛЬНОЕ хранилище системы (см. maos/config.py: без DB_DSN
приложение не стартует) — в отличие от agent_system, где Postgres был
опциональным навыком поверх SQLite по умолчанию.

Схема:
  agent            — личность агента: identity, voice, LLM-привязка,
                      системный промпт, эмбеддинг описания (для роутинга),
                      список навыков (tools) для инструментального цикла
  conversation      — диалог (может быть мультиагентным)
  message           — одно сообщение диалога: кто сказал, какой моделью,
                      сколько токенов — экономика оплачивается прозрачно
  memory_quantum    — mid-term «квант памяти»: пара вопрос-ответ + вектор
  onto_entity       — long-term граф: сущность
  onto_relation     — long-term граф: связь между сущностями
  chain_run         — детерминированная ручная цепочка Agent_A -> Agent_B
  chain_step        — один шаг цепочки (аналог pipeline_stage в agent_system)
  doc_chunk         — фрагменты документов для навыка rag (перенесён из
                      agent_system/agent/skills/rag.py) с векторным и
                      полнотекстовым поиском

Импорт psycopg — ленивый (внутри Store.__init__), чтобы модуль
импортировался даже без установленного пакета (для генерации схемы/CLI
подсказок), а ошибка о недостающей зависимости была понятной.
"""
from __future__ import annotations

import json
import time
from typing import Any


class StoreError(RuntimeError):
    """Ошибка работы с хранилищем: подключение, отсутствие pgvector и т.п."""


def _require_psycopg():
    try:
        import psycopg  # type: ignore
    except ImportError as exc:
        raise StoreError(
            "MAOS требует psycopg. Установите: pip install \"psycopg[binary]\""
        ) from exc
    return psycopg


class Store:
    """Единая точка доступа к PostgreSQL+pgvector для всего MAOS."""

    def __init__(self, dsn: str, dim: int = 256) -> None:
        if not dsn:
            raise StoreError(
                "Не задан DB_DSN. MAOS не работает без PostgreSQL — укажите "
                "переменную окружения DB_DSN или maos.config.Config.db_dsn."
            )
        psycopg = _require_psycopg()
        self._psycopg = psycopg
        self.dsn = dsn
        self.dim = dim
        try:
            self.conn = psycopg.connect(dsn, autocommit=True)
        except Exception as exc:  # OperationalError и т.п.
            raise StoreError(f"Не удалось подключиться к PostgreSQL: {exc}") from exc
        self._ensure_schema()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ------------------------------------------------------------ schema
    def _ensure_schema(self) -> None:
        cur = self.conn.cursor()
        try:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except Exception as exc:
            raise StoreError(
                "Расширение pgvector недоступно на сервере PostgreSQL. "
                f"Причина: {exc}"
            ) from exc
        dim = self.dim
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS agent(
                id SERIAL PRIMARY KEY,
                slug TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT DEFAULT '',
                keywords TEXT DEFAULT '',
                avatar TEXT DEFAULT '',
                voice_provider TEXT DEFAULT '',
                voice_id TEXT DEFAULT '',
                llm_ref TEXT DEFAULT '',      -- provider::model, '' = глобальный дефолт
                system_prompt TEXT DEFAULT '',
                tools TEXT DEFAULT '',        -- через запятую: files,web,rag,office
                description_embedding VECTOR({dim}),
                enabled BOOLEAN DEFAULT TRUE,
                created DOUBLE PRECISION,
                updated DOUBLE PRECISION
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversation(
                id SERIAL PRIMARY KEY,
                title TEXT DEFAULT '',
                status TEXT DEFAULT 'active',   -- active | archived
                created DOUBLE PRECISION,
                updated DOUBLE PRECISION
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS message(
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES conversation(id)
                    ON DELETE CASCADE,
                role TEXT NOT NULL,             -- user | agent | system
                agent_id INTEGER REFERENCES agent(id) ON DELETE SET NULL,
                content TEXT NOT NULL,
                provider_model TEXT DEFAULT '', -- provider::model, реально отвечавшая
                tokens_used INTEGER DEFAULT 0,
                confidence_score REAL,
                created DOUBLE PRECISION
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_message_conv "
            "ON message(conversation_id, id)")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS memory_quantum(
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversation(id) ON DELETE CASCADE,
                agent_id INTEGER REFERENCES agent(id) ON DELETE SET NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                provider_model TEXT DEFAULT '',
                tokens_used INTEGER DEFAULT 0,
                confidence_score REAL DEFAULT 1.0,
                embedding VECTOR({dim}),
                created DOUBLE PRECISION
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_memory_quantum_conv "
            "ON memory_quantum(conversation_id)")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS onto_entity(
                id SERIAL PRIMARY KEY,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                props JSONB DEFAULT '{{}}',
                description TEXT DEFAULT '',
                embedding VECTOR({dim}),
                created DOUBLE PRECISION,
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
                created DOUBLE PRECISION,
                UNIQUE(subj, pred, obj)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chain_run(
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER REFERENCES conversation(id) ON DELETE CASCADE,
                goal TEXT DEFAULT '',
                status TEXT DEFAULT 'active',    -- active | done | failed | stopped
                created DOUBLE PRECISION,
                updated DOUBLE PRECISION,
                finished DOUBLE PRECISION
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS chain_step(
                id SERIAL PRIMARY KEY,
                chain_run_id INTEGER NOT NULL REFERENCES chain_run(id) ON DELETE CASCADE,
                ord INTEGER NOT NULL,
                agent_slug TEXT NOT NULL,
                task TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',   -- pending|running|done|failed|skipped
                answer TEXT DEFAULT '',
                provider_model TEXT DEFAULT '',
                error TEXT DEFAULT '',
                created DOUBLE PRECISION,
                started DOUBLE PRECISION,
                finished DOUBLE PRECISION
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_chain_step_run "
            "ON chain_step(chain_run_id, ord)")
        cur.execute(f"""
            CREATE TABLE IF NOT EXISTS doc_chunk(
                id SERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                ord INTEGER DEFAULT 0,
                text TEXT NOT NULL,
                entity_refs JSONB DEFAULT '[]',
                embedding VECTOR({dim}),
                tsv TSVECTOR,
                created DOUBLE PRECISION
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_doc_chunk_source "
            "ON doc_chunk(source, ord)")
        cur.execute(
            "CREATE INDEX IF NOT EXISTS ix_doc_chunk_tsv "
            "ON doc_chunk USING GIN(tsv)")

    @staticmethod
    def _vec(v: list[float] | None) -> str | None:
        return None if v is None else "[" + ",".join(repr(float(x)) for x in v) + "]"

    @staticmethod
    def _now() -> float:
        return time.time()

    # ------------------------------------------------------------- agent
    def create_agent(self, slug: str, name: str, description: str = "",
                     keywords: str = "", avatar: str = "",
                     voice_provider: str = "", voice_id: str = "",
                     llm_ref: str = "", system_prompt: str = "",
                     tools: str = "",
                     description_embedding: list[float] | None = None) -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO agent(slug,name,description,keywords,avatar,"
            "voice_provider,voice_id,llm_ref,system_prompt,tools,"
            "description_embedding,created,updated) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (slug, name, description, keywords, avatar, voice_provider,
             voice_id, llm_ref, system_prompt, tools,
             self._vec(description_embedding), now, now))
        return int(cur.fetchone()[0])

    def update_agent(self, slug: str, **fields: Any) -> bool:
        allowed = {"name", "description", "keywords", "avatar",
                  "voice_provider", "voice_id", "llm_ref", "system_prompt",
                  "tools", "enabled"}
        emb = fields.pop("description_embedding", None)
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                raise StoreError(f"Поле агента {k!r} нельзя изменить")
            sets.append(f"{k}=%s")
            params.append(v)
        if emb is not None:
            sets.append("description_embedding=%s")
            params.append(self._vec(emb))
        if not sets:
            return False
        sets.append("updated=%s")
        params.append(self._now())
        params.append(slug)
        cur = self.conn.cursor()
        cur.execute(f"UPDATE agent SET {', '.join(sets)} WHERE slug=%s", params)
        return cur.rowcount > 0

    def delete_agent(self, slug: str) -> bool:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM agent WHERE slug=%s", (slug,))
        return cur.rowcount > 0

    _AGENT_COLS = ("id", "slug", "name", "description", "keywords", "avatar",
                  "voice_provider", "voice_id", "llm_ref", "system_prompt",
                  "tools", "enabled", "created", "updated")

    def _agent_row(self, row: tuple) -> dict[str, Any]:
        return dict(zip(self._AGENT_COLS, row))

    def get_agent(self, slug: str) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {', '.join(self._AGENT_COLS)} FROM agent WHERE slug=%s",
            (slug,))
        row = cur.fetchone()
        return self._agent_row(row) if row else None

    def list_agents(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        q = f"SELECT {', '.join(self._AGENT_COLS)} FROM agent"
        if enabled_only:
            q += " WHERE enabled = TRUE"
        q += " ORDER BY slug"
        cur.execute(q)
        return [self._agent_row(r) for r in cur.fetchall()]

    def agents_for_routing(self) -> list[dict[str, Any]]:
        """Агенты + их эмбеддинг описания — для семантического роутера."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, slug, name, description, keywords, description_embedding "
            "FROM agent WHERE enabled = TRUE")
        out = []
        for aid, slug, name, desc, kw, emb in cur.fetchall():
            out.append({"id": aid, "slug": slug, "name": name,
                        "description": desc, "keywords": kw,
                        "embedding": _parse_vec(emb)})
        return out

    # -------------------------------------------------------- conversation
    def create_conversation(self, title: str = "") -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO conversation(title,created,updated) VALUES(%s,%s,%s) "
            "RETURNING id", (title, now, now))
        return int(cur.fetchone()[0])

    def get_conversation(self, cid: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, title, status, created, updated FROM conversation "
            "WHERE id=%s", (cid,))
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "title": row[1], "status": row[2],
                "created": row[3], "updated": row[4]}

    def list_conversations(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, title, status, created, updated FROM conversation "
            "ORDER BY updated DESC LIMIT %s", (limit,))
        return [{"id": r[0], "title": r[1], "status": r[2], "created": r[3],
                "updated": r[4]} for r in cur.fetchall()]

    def add_message(self, conversation_id: int, role: str, content: str,
                    agent_id: int | None = None, provider_model: str = "",
                    tokens_used: int = 0,
                    confidence_score: float | None = None) -> int:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO message(conversation_id,role,agent_id,content,"
            "provider_model,tokens_used,confidence_score,created) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (conversation_id, role, agent_id, content, provider_model,
             tokens_used, confidence_score, now))
        mid = int(cur.fetchone()[0])
        cur.execute("UPDATE conversation SET updated=%s WHERE id=%s",
                    (now, conversation_id))
        return mid

    def messages(self, conversation_id: int, limit: int = 500) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, role, agent_id, content, provider_model, tokens_used, "
            "confidence_score, created FROM message WHERE conversation_id=%s "
            "ORDER BY id LIMIT %s", (conversation_id, limit))
        return [{"id": r[0], "role": r[1], "agent_id": r[2], "content": r[3],
                "provider_model": r[4], "tokens_used": r[5],
                "confidence_score": r[6], "created": r[7]}
                for r in cur.fetchall()]

    def message_count(self, conversation_id: int) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM message WHERE conversation_id=%s",
            (conversation_id,))
        return int(cur.fetchone()[0])

    # ------------------------------------------------------ memory_quantum
    def add_memory_quantum(self, conversation_id: int | None, question: str,
                           answer: str, agent_id: int | None = None,
                           provider_model: str = "", tokens_used: int = 0,
                           confidence_score: float = 1.0,
                           embedding: list[float] | None = None) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO memory_quantum(conversation_id,agent_id,question,"
            "answer,provider_model,tokens_used,confidence_score,embedding,"
            "created) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
            (conversation_id, agent_id, question, answer, provider_model,
             tokens_used, confidence_score, self._vec(embedding), self._now()))
        return int(cur.fetchone()[0])

    def semantic_search_quanta(self, embedding: list[float], limit: int = 5,
                               conversation_id: int | None = None,
                               min_score: float = 0.0) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        vec = self._vec(embedding)
        if conversation_id is not None:
            cur.execute("""
                SELECT id, conversation_id, agent_id, question, answer,
                       provider_model, tokens_used, confidence_score,
                       1 - (embedding <=> %s) AS score
                FROM memory_quantum
                WHERE embedding IS NOT NULL AND conversation_id=%s
                ORDER BY embedding <=> %s LIMIT %s
            """, (vec, conversation_id, vec, limit))
        else:
            cur.execute("""
                SELECT id, conversation_id, agent_id, question, answer,
                       provider_model, tokens_used, confidence_score,
                       1 - (embedding <=> %s) AS score
                FROM memory_quantum WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s LIMIT %s
            """, (vec, vec, limit))
        out = []
        for row in cur.fetchall():
            d = {"id": row[0], "conversation_id": row[1], "agent_id": row[2],
                "question": row[3], "answer": row[4], "provider_model": row[5],
                "tokens_used": row[6], "confidence_score": row[7],
                "score": float(row[8])}
            if d["score"] >= min_score:
                out.append(d)
        return out

    def all_quanta(self, limit: int = 10_000) -> list[dict[str, Any]]:
        """Для фонового обслуживания: дедупликация, сборка мусора."""
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, conversation_id, question, answer, embedding, created "
            "FROM memory_quantum ORDER BY id LIMIT %s", (limit,))
        return [{"id": r[0], "conversation_id": r[1], "question": r[2],
                "answer": r[3], "embedding": _parse_vec(r[4]), "created": r[5]}
                for r in cur.fetchall()]

    def delete_quantum(self, qid: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("DELETE FROM memory_quantum WHERE id=%s", (qid,))
        return cur.rowcount > 0

    def quantum_count(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM memory_quantum")
        return int(cur.fetchone()[0])

    # ---------------------------------------------------------- ontology
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
                "INSERT INTO onto_entity(kind,name,props,description,embedding,"
                "created) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id",
                (kind, name, json.dumps(props or {}, ensure_ascii=False),
                 description, self._vec(embedding), self._now()))
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

    def link(self, subj: tuple[str, str], pred: str, obj: tuple[str, str],
             props: dict[str, Any] | None = None) -> bool:
        a = self.upsert_entity(*subj)
        b = self.upsert_entity(*obj)
        cur = self.conn.cursor()
        try:
            cur.execute(
                "INSERT INTO onto_relation(subj,pred,obj,props,created) "
                "VALUES(%s,%s,%s,%s,%s)",
                (a, pred, b, json.dumps(props or {}, ensure_ascii=False),
                 self._now()))
            return True
        except Exception:
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

    def graph_data(self, limit: int = 500) -> dict[str, Any]:
        """Узлы и рёбра для визуализации графа в дашборде."""
        cur = self.conn.cursor()
        cur.execute("SELECT id, kind, name FROM onto_entity LIMIT %s", (limit,))
        nodes = [{"id": r[0], "kind": r[1], "name": r[2]} for r in cur.fetchall()]
        cur.execute("SELECT subj, pred, obj FROM onto_relation LIMIT %s", (limit,))
        edges = [{"source": r[0], "pred": r[1], "target": r[2]}
                for r in cur.fetchall()]
        return {"nodes": nodes, "edges": edges}

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
        vec = self._vec(embedding)
        if kind:
            cur.execute("""
                SELECT kind, name, description, 1 - (embedding <=> %s) AS score
                FROM onto_entity WHERE kind=%s AND embedding IS NOT NULL
                ORDER BY embedding <=> %s LIMIT %s
            """, (vec, kind, vec, limit))
        else:
            cur.execute("""
                SELECT kind, name, description, 1 - (embedding <=> %s) AS score
                FROM onto_entity WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s LIMIT %s
            """, (vec, vec, limit))
        return [{"kind": k, "name": n, "description": d, "score": float(s)}
                for k, n, d, s in cur.fetchall()]

    def merge_entities(self, kind: str, keep_name: str, drop_name: str) -> bool:
        """Слить дубли: связи drop переносятся на keep, drop удаляется.

        Используется фоновым обслуживанием (maos/maintenance/service.py)
        при "синтезе и очистке" (ТЗ п.6) — устраняет дубли графа, найденные
        по высокому косинусному сходству эмбеддингов описаний.
        """
        keep = self.get_entity(kind, keep_name)
        drop = self.get_entity(kind, drop_name)
        if not keep or not drop or keep["id"] == drop["id"]:
            return False
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE onto_relation SET subj=%s WHERE subj=%s",
            (keep["id"], drop["id"]))
        cur.execute(
            "UPDATE onto_relation SET obj=%s WHERE obj=%s",
            (keep["id"], drop["id"]))
        # дубли связей (keep уже связан так же, как был связан drop)
        # упадут на UNIQUE(subj,pred,obj) — отбрасываем их тихо
        cur.execute("""
            DELETE FROM onto_relation a USING onto_relation b
            WHERE a.id > b.id AND a.subj=b.subj AND a.pred=b.pred AND a.obj=b.obj
        """)
        cur.execute("DELETE FROM onto_entity WHERE id=%s", (drop["id"],))
        return True

    # ------------------------------------------------------------- chains
    def start_chain(self, goal: str, agent_slugs: list[str],
                    conversation_id: int | None = None) -> int:
        """Заводит ручную детерминированную цепочку Agent_A -> Agent_B -> ...

        Все шаги заводятся сразу в статусе pending — сама цепочка задаётся
        заранее списком agent_slugs, а не придумывается моделью на лету
        (тот же принцип, что у agent_system/agent/pipeline.py).
        """
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO chain_run(conversation_id,goal,status,created,updated) "
            "VALUES(%s,%s,'active',%s,%s) RETURNING id",
            (conversation_id, goal, now, now))
        run_id = int(cur.fetchone()[0])
        for i, slug in enumerate(agent_slugs):
            cur.execute(
                "INSERT INTO chain_step(chain_run_id,ord,agent_slug,status,created) "
                "VALUES(%s,%s,%s,'pending',%s)", (run_id, i, slug, now))
        return run_id

    def chain_steps(self, chain_run_id: int) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, ord, agent_slug, task, status, answer, provider_model, "
            "error FROM chain_step WHERE chain_run_id=%s ORDER BY ord",
            (chain_run_id,))
        return [{"id": r[0], "ord": r[1], "agent_slug": r[2], "task": r[3],
                "status": r[4], "answer": r[5], "provider_model": r[6],
                "error": r[7]} for r in cur.fetchall()]

    def get_chain(self, chain_run_id: int) -> dict[str, Any] | None:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, conversation_id, goal, status, created, updated, finished "
            "FROM chain_run WHERE id=%s", (chain_run_id,))
        row = cur.fetchone()
        if not row:
            return None
        return {"id": row[0], "conversation_id": row[1], "goal": row[2],
                "status": row[3], "created": row[4], "updated": row[5],
                "finished": row[6]}

    def list_chains(self, limit: int = 50) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, goal, status, created, updated FROM chain_run "
            "ORDER BY id DESC LIMIT %s", (limit,))
        return [{"id": r[0], "goal": r[1], "status": r[2], "created": r[3],
                "updated": r[4]} for r in cur.fetchall()]

    def set_chain_step(self, step_id: int, status: str, task: str | None = None,
                       answer: str | None = None, provider_model: str = "",
                       error: str | None = None) -> None:
        now = self._now()
        sets = ["status=%s"]
        params: list[Any] = [status]
        if task is not None:
            sets.append("task=%s")
            params.append(task)
        if answer is not None:
            sets.append("answer=%s")
            params.append(answer)
        if provider_model:
            sets.append("provider_model=%s")
            params.append(provider_model)
        if error is not None:
            sets.append("error=%s")
            params.append(error)
        if status == "running":
            sets.append("started=%s")
            params.append(now)
        if status in ("done", "failed", "skipped"):
            sets.append("finished=%s")
            params.append(now)
        params.append(step_id)
        cur = self.conn.cursor()
        cur.execute(f"UPDATE chain_step SET {', '.join(sets)} WHERE id=%s", params)
        cur.execute("UPDATE chain_run SET updated=%s WHERE id=(SELECT chain_run_id "
                   "FROM chain_step WHERE id=%s)", (now, step_id))

    def finish_chain(self, chain_run_id: int, status: str) -> None:
        now = self._now()
        cur = self.conn.cursor()
        cur.execute(
            "UPDATE chain_run SET status=%s, updated=%s, finished=%s WHERE id=%s",
            (status, now, now, chain_run_id))

    # ------------------------------------------------------- doc_chunk (RAG)
    def add_chunks(self, source: str, texts: list[str],
                  embeddings: list[list[float] | None] | None = None,
                  entity_refs: list[tuple[str, str]] | None = None) -> list[int]:
        """Индексирует source: заменяет ВСЕ старые фрагменты этого источника
        новыми (переиндексация обновлённого документа не плодит дублей)."""
        cur = self.conn.cursor()
        cur.execute("DELETE FROM doc_chunk WHERE source=%s", (source,))
        refs = json.dumps([list(r) for r in (entity_refs or [])], ensure_ascii=False)
        embs = embeddings or [None] * len(texts)
        ids: list[int] = []
        now = self._now()
        for i, (text, emb) in enumerate(zip(texts, embs)):
            cur.execute(
                "INSERT INTO doc_chunk(source,ord,text,entity_refs,embedding,"
                "tsv,created) VALUES(%s,%s,%s,%s,%s,to_tsvector('simple',%s),%s) "
                "RETURNING id",
                (source, i, text, refs, self._vec(emb), text, now))
            ids.append(int(cur.fetchone()[0]))
        return ids

    def semantic_search_chunks(self, embedding: list[float], limit: int = 6,
                               source: str | None = None) -> list[dict[str, Any]]:
        cur = self.conn.cursor()
        vec = self._vec(embedding)
        if source:
            cur.execute("""
                SELECT source, ord, text, entity_refs,
                       1 - (embedding <=> %s) AS score
                FROM doc_chunk WHERE embedding IS NOT NULL AND source=%s
                ORDER BY embedding <=> %s LIMIT %s
            """, (vec, source, vec, limit))
        else:
            cur.execute("""
                SELECT source, ord, text, entity_refs,
                       1 - (embedding <=> %s) AS score
                FROM doc_chunk WHERE embedding IS NOT NULL
                ORDER BY embedding <=> %s LIMIT %s
            """, (vec, vec, limit))
        return [{"source": s, "ord": o, "text": t, "entity_refs": refs,
                "score": float(sc)}
                for s, o, t, refs, sc in cur.fetchall()]

    def fts_chunks(self, query: str, limit: int = 6) -> list[dict[str, Any]]:
        """Полнотекстовый поиск через tsvector/plainto_tsquery — ловит точные
        термины и словоформы, которые эмбеддинг может не различить."""
        cur = self.conn.cursor()
        cur.execute("""
            SELECT source, ord, text, entity_refs
            FROM doc_chunk
            WHERE tsv @@ plainto_tsquery('simple', %s)
            ORDER BY ts_rank(tsv, plainto_tsquery('simple', %s)) DESC
            LIMIT %s
        """, (query, query, limit))
        return [{"source": s, "ord": o, "text": t, "entity_refs": refs}
                for s, o, t, refs in cur.fetchall()]

    def entity_chunks(self, kind: str, name: str) -> list[dict[str, Any]]:
        """Фрагменты, привязанные к объекту онтологии (entity_refs при
        индексации) — для RAG на онтологии."""
        cur = self.conn.cursor()
        cur.execute("SELECT id, source, ord, text, entity_refs FROM doc_chunk")
        wanted = (kind, name)
        out = []
        for cid, source, ord_, text, entity_refs in cur.fetchall():
            try:
                have = {tuple(r) for r in (entity_refs or [])}
            except TypeError:
                have = set()
            if wanted in have:
                out.append({"id": cid, "source": source, "ord": ord_, "text": text})
        return out

    def chunk_count(self) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM doc_chunk")
        return int(cur.fetchone()[0])

    def chunk_sources(self) -> list[str]:
        cur = self.conn.cursor()
        cur.execute("SELECT DISTINCT source FROM doc_chunk ORDER BY source")
        return [r[0] for r in cur.fetchall()]

    # ------------------------------------------------------------ metrics
    def memory_stats(self) -> dict[str, Any]:
        """Для GET /v1/memory/stats — статистика использования БД и токенов."""
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) FROM agent")
        agents = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM conversation")
        conversations = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*), COALESCE(SUM(tokens_used),0) FROM message")
        msg_count, msg_tokens = cur.fetchone()
        cur.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens_used),0) FROM memory_quantum")
        quanta_count, quanta_tokens = cur.fetchone()
        entities, relations = self.graph_stats()
        cur.execute(
            "SELECT provider_model, COUNT(*), COALESCE(SUM(tokens_used),0) "
            "FROM message WHERE provider_model != '' "
            "GROUP BY provider_model ORDER BY 3 DESC")
        by_model = [{"provider_model": r[0], "messages": r[1], "tokens": r[2]}
                   for r in cur.fetchall()]
        return {
            "agents": int(agents),
            "conversations": int(conversations),
            "messages": int(msg_count),
            "message_tokens": int(msg_tokens),
            "memory_quanta": int(quanta_count),
            "memory_quanta_tokens": int(quanta_tokens),
            "onto_entities": entities,
            "onto_relations": relations,
            "doc_chunks": self.chunk_count(),
            "tokens_by_model": by_model,
        }


def _parse_vec(raw: Any) -> list[float] | None:
    """pgvector отдаёт вектор либо как строку '[1,2,3]', либо уже как
    список (в зависимости от версии psycopg/адаптера) — нормализуем."""
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        return [float(x) for x in raw]
    s = str(raw).strip().strip("[]")
    if not s:
        return []
    return [float(x) for x in s.split(",")]
