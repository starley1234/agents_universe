"""Постоянное состояние: SQLite. Онтология, память, прогоны, артефакты.

Почему SQLite, а не «настоящая» БД: он в стандартной библиотеке, живёт
одним файлом, переживает перезапуск и держит FTS5-поиск. Для суточного
прогона на одной машине этого достаточно, а зависимостей по-прежнему ноль.

Четыре сущности, больше не нужно:

  fact      — что агент узнал (семантическая память, полнотекстовый поиск)
  entity    — объект предметной области: деталь, персона, файл, гипотеза
  relation  — связь между объектами: тройка субъект-предикат-объект
  event     — что агент делал (эпизодическая память, для рефлексии)

Онтология здесь = entity + relation. Она не зашита в код: типы сущностей
и предикаты задаёт сам агент по ходу работы, поэтому одна и та же схема
обслуживает и редуктор, и маркетинг.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

#: сколько свежих фактов просматривать подстрочным поиском.
#: Полный проход по большой базе стоит десятки миллисекунд, а старые
#: записи почти никогда не нужны — агент оперирует недавним.
LIKE_SCAN = 20_000


def _cosine(a: list[float], b: list[float]) -> float:
    """Косинусное сходство. Дублирует agent/llm/embeddings.cosine —
    намеренно: store.py не зависит от llm/, чтобы память/онтология
    работали даже если пакет embeddings когда-нибудь изменит форму
    экспорта; формула в три строки не стоит того, чтобы городить
    зависимость между модулями разного назначения."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS run(
  id INTEGER PRIMARY KEY,
  goal TEXT NOT NULL,
  profile TEXT,
  status TEXT DEFAULT 'active',      -- active | done | stopped | failed
  started REAL, updated REAL, finished REAL,
  steps INTEGER DEFAULT 0,
  tool_calls INTEGER DEFAULT 0,
  chars_sent INTEGER DEFAULT 0,      -- грубая оценка расхода контекста
  tok_in INTEGER DEFAULT 0,          -- токенов на вход
  tok_out INTEGER DEFAULT 0,         -- токенов на выход
  cost REAL DEFAULT 0                -- оценка стоимости, доллары
);

CREATE TABLE IF NOT EXISTS task(
  id INTEGER PRIMARY KEY,
  run_id INTEGER, parent_id INTEGER,
  title TEXT NOT NULL,
  status TEXT DEFAULT 'open',        -- open | doing | done | failed | skipped
  result TEXT, created REAL, updated REAL,
  ord INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS fact(
  id INTEGER PRIMARY KEY,
  text TEXT NOT NULL,
  tags TEXT DEFAULT '',
  source TEXT DEFAULT '',
  confidence REAL DEFAULT 1.0,
  run_id INTEGER, created REAL,
  UNIQUE(text)
);
CREATE VIRTUAL TABLE IF NOT EXISTS fact_fts USING fts5(
  text, tags, content=fact, content_rowid=id
);
CREATE TRIGGER IF NOT EXISTS fact_ai AFTER INSERT ON fact BEGIN
  INSERT INTO fact_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS fact_ad AFTER DELETE ON fact BEGIN
  INSERT INTO fact_fts(fact_fts, rowid, text, tags)
    VALUES('delete', old.id, old.text, old.tags);
END;

CREATE TABLE IF NOT EXISTS entity(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                -- part | file | person | idea | metric ...
  name TEXT NOT NULL,
  props TEXT DEFAULT '{}',
  description TEXT DEFAULT '',       -- текстовое описание для семантического поиска
  embedding TEXT,                    -- JSON-массив float, для RAG на онтологии
  run_id INTEGER, created REAL,
  UNIQUE(kind, name)
);

CREATE TABLE IF NOT EXISTS relation(
  id INTEGER PRIMARY KEY,
  subj INTEGER NOT NULL, pred TEXT NOT NULL, obj INTEGER NOT NULL,
  props TEXT DEFAULT '{}', created REAL,
  UNIQUE(subj, pred, obj)
);

CREATE TABLE IF NOT EXISTS event(
  id INTEGER PRIMARY KEY,
  run_id INTEGER, step INTEGER,
  kind TEXT,                         -- tool | answer | reflect | error
  name TEXT, summary TEXT,
  sig TEXT,                          -- подпись действия: ловим повторы
  created REAL
);
CREATE INDEX IF NOT EXISTS ix_event_run ON event(run_id, step);
CREATE INDEX IF NOT EXISTS ix_event_sig ON event(run_id, sig);
CREATE INDEX IF NOT EXISTS ix_task_run ON task(run_id, status);

CREATE TABLE IF NOT EXISTS chunk(
  id INTEGER PRIMARY KEY,
  source TEXT NOT NULL,              -- откуда фрагмент: имя файла/документа
  ord INTEGER DEFAULT 0,             -- порядковый номер фрагмента в источнике
  text TEXT NOT NULL,
  tags TEXT DEFAULT '',
  entity_refs TEXT DEFAULT '[]',     -- JSON [[kind,name], ...] — привязка
                                      -- фрагмента к объектам онтологии,
                                      -- нужна для RAG на базе онтологии
  embedding TEXT,                    -- JSON-массив float, пусто пока не векторизован
  dim INTEGER DEFAULT 0,
  run_id INTEGER, created REAL
);
CREATE INDEX IF NOT EXISTS ix_chunk_source ON chunk(source, ord);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
  text, source, content=chunk, content_rowid=id
);
CREATE TRIGGER IF NOT EXISTS chunk_ai AFTER INSERT ON chunk BEGIN
  INSERT INTO chunk_fts(rowid, text, source) VALUES (new.id, new.text, new.source);
END;
CREATE TRIGGER IF NOT EXISTS chunk_ad AFTER DELETE ON chunk BEGIN
  INSERT INTO chunk_fts(chunk_fts, rowid, text, source)
    VALUES('delete', old.id, old.text, old.source);
END;

CREATE TABLE IF NOT EXISTS cert_check(
  id INTEGER PRIMARY KEY,
  run_id INTEGER,
  requirement TEXT NOT NULL,         -- пункт требования, напр. "ФАП-21 п.21.16А"
  requirement_text TEXT DEFAULT '',  -- текст/суть требования, если известен
  verdict TEXT NOT NULL,             -- compliant | non_compliant | not_found | needs_review
  evidence_source TEXT DEFAULT '',   -- файл/документ с доказательством
  evidence_quote TEXT DEFAULT '',    -- ДОСЛОВНАЯ цитата из источника
  quote_verified INTEGER DEFAULT 0,  -- 1, если цитата реально найдена в источнике
  comment TEXT DEFAULT '',
  created REAL
);
CREATE INDEX IF NOT EXISTS ix_cert_check_run ON cert_check(run_id, verdict);
"""



class Store:
    def __init__(self, path: str | Path = "agent.db") -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.close()

    def _now(self) -> float:
        return time.time()

    # ---------------------------------------------------------- прогоны
    def start_run(self, goal: str, profile: str | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO run(goal, profile, started, updated) VALUES(?,?,?,?)",
            (goal, profile, self._now(), self._now()))
        self.db.commit()
        return int(cur.lastrowid)

    def bump_run(self, run_id: int, steps: int = 0, calls: int = 0,
                 chars: int = 0, tok_in: int = 0, tok_out: int = 0,
                 cost: float = 0.0) -> None:
        self.db.execute(
            "UPDATE run SET steps=steps+?, tool_calls=tool_calls+?, "
            "chars_sent=chars_sent+?, tok_in=tok_in+?, tok_out=tok_out+?, "
            "cost=cost+?, updated=? WHERE id=?",
            (steps, calls, chars, tok_in, tok_out, cost, self._now(), run_id))
        self.db.commit()

    def finish_run(self, run_id: int, status: str) -> None:
        self.db.execute("UPDATE run SET status=?, finished=? WHERE id=?",
                        (status, self._now(), run_id))
        self.db.commit()

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM run WHERE id=?", (run_id,)).fetchone()
        return dict(r) if r else None

    def last_active_run(self) -> dict[str, Any] | None:
        r = self.db.execute(
            "SELECT * FROM run WHERE status='active' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return dict(r) if r else None

    # ------------------------------------------------------------ план
    def add_tasks(self, run_id: int, titles: list[str],
                  parent: int | None = None) -> list[int]:
        ids = []
        base = self.db.execute(
            "SELECT COALESCE(MAX(ord),0) FROM task WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        for i, t in enumerate(titles, 1):
            cur = self.db.execute(
                "INSERT INTO task(run_id,parent_id,title,created,updated,ord) "
                "VALUES(?,?,?,?,?,?)",
                (run_id, parent, t.strip(), self._now(), self._now(), base + i))
            ids.append(int(cur.lastrowid))
        self.db.commit()
        return ids

    def next_task(self, run_id: int) -> dict[str, Any] | None:
        r = self.db.execute(
            "SELECT * FROM task WHERE run_id=? AND status IN ('open','doing') "
            "ORDER BY status='doing' DESC, ord LIMIT 1", (run_id,)).fetchone()
        return dict(r) if r else None

    def set_task(self, task_id: int, status: str, result: str = "") -> None:
        self.db.execute(
            "UPDATE task SET status=?, result=?, updated=? WHERE id=?",
            (status, result[:2000], self._now(), task_id))
        self.db.commit()

    def drop_open_tasks(self, run_id: int) -> int:
        """Убрать невыполненные пункты — для перепланирования.

        Сделанное и проваленное НЕ трогаем: это история, из которой
        новый план должен исходить.
        """
        cur = self.db.execute(
            "DELETE FROM task WHERE run_id=? AND status IN ('open','doing')",
            (run_id,))
        self.db.commit()
        return cur.rowcount

    def tasks(self, run_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM task WHERE run_id=? ORDER BY ord", (run_id,))]

    # ----------------------------------------------------------- память
    def remember(self, text: str, tags: str = "", source: str = "",
                 confidence: float = 1.0, run_id: int | None = None) -> int:
        text = text.strip()
        if not text:
            return 0
        try:
            cur = self.db.execute(
                "INSERT INTO fact(text,tags,source,confidence,run_id,created) "
                "VALUES(?,?,?,?,?,?)",
                (text, tags, source, confidence, run_id, self._now()))
            self.db.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            # факт уже известен — обновляем уверенность, не плодим дубли
            self.db.execute(
                "UPDATE fact SET confidence=MAX(confidence,?), tags=? "
                "WHERE text=?", (confidence, tags, text))
            self.db.commit()
            row = self.db.execute("SELECT id FROM fact WHERE text=?",
                                  (text,)).fetchone()
            return int(row["id"]) if row else 0

    #: слова короче считаем шумом: "и", "в", "на"
    _MIN_WORD = 3

    @classmethod
    def _terms(cls, query: str) -> list[str]:
        """Слова запроса, пригодные для FTS. Служебные символы убираем:
        на них FTS5 падает с синтаксической ошибкой."""
        words = re.findall(r"\w+", query, flags=re.UNICODE)
        return [w for w in words if len(w) >= cls._MIN_WORD]

    @staticmethod
    def _stem(word: str) -> str:
        """Грубая основа слова: отбрасываем окончание.

        Полноценная морфология тянула бы зависимость. Для поиска хватает
        префикса: «щека»/«щекой»/«щеки» дают общее начало «щек».
        Короткие слова не режем — от них ничего не останется.
        """
        w = word.lower()
        if len(w) <= 3:
            return w
        # 4-5 букв режем до 3: «щека»->«щек» ловит «щекой», «щеки».
        # Длинные — до 3/4: «вершинами»->«вершин».
        return w[:3] if len(w) <= 5 else w[:max(4, int(len(w) * 0.75))]

    def _fts(self, expr: str, limit: int) -> list[dict[str, Any]]:
        """Один FTS-запрос. Кандидатов берём по rowid (дёшево, по индексу),
        ранжируем только их — иначе ORDER BY rank обходит все совпадения."""
        try:
            rows = self.db.execute(
                "WITH hit AS ("
                "  SELECT rowid FROM fact_fts WHERE fact_fts MATCH ?"
                "  ORDER BY rowid DESC LIMIT ?"
                ") SELECT f.* FROM hit JOIN fact f ON f.id=hit.rowid"
                " ORDER BY f.confidence DESC, f.id DESC LIMIT ?",
                (expr, max(limit * 20, 200), limit)).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

    def recall(self, query: str = "", limit: int = 10) -> list[dict[str, Any]]:
        """Гибридный поиск по памяти.

        Раньше искалась только ТОЧНАЯ ФРАЗА, и запрос «зазор щеки» не
        находил факт «зазор между щекой водила…», хотя оба слова есть.
        Для агента это означало повторное исследование уже известного.

        Три ступени, от точного к широкому:
          1. фраза целиком   — самое релевантное;
          2. основы слов     — ловит словоформы («щека» найдёт «щекой»),
                               ранжируем по числу совпавших основ;
          3. подстрока       — части слов и числа: «83.8» -> «Ø83.875».
        Каждая дополняет выдачу, не вытесняя более точную.

        Отдельной AND-ступени нет намеренно: замер показал, что после
        стемминга она находит ровно то же, что ступень 2, и не ускоряет
        поиск. Лишний код без пользы.
        """
        if not query.strip():
            return [dict(r) for r in self.db.execute(
                "SELECT * FROM fact ORDER BY id DESC LIMIT ?", (limit,))]

        out: list[dict[str, Any]] = []
        seen: set[int] = set()

        def add(rows: list[dict[str, Any]]) -> None:
            for r in rows:
                if r["id"] not in seen:
                    seen.add(r["id"])
                    out.append(r)

        terms = self._terms(query)
        # FTS ищет слово целиком: «щека» не найдёт «щекой». Русские
        # словоформы отличаются окончанием, поэтому ищем по ОСНОВЕ —
        # префиксным запросом term*. Основу берём грубо: 3/4 слова.
        prefixes = [self._stem(t) for t in terms]

        # 1) точная фраза
        add(self._fts('"' + query.replace('"', " ") + '"', limit))
        # 2) все слова в любом порядке, с учётом словоформ
        if len(out) < limit and len(terms) > 1:
            add(self._fts(" AND ".join(f'{p}*' for p in prefixes), limit))
        # 2) основы слов; выше те, где совпало больше основ
        if len(out) < limit and terms:
            loose = self._fts(" OR ".join(f'{p}*' for p in prefixes), limit * 5)
            low = [p.lower() for p in prefixes]
            loose.sort(key=lambda r: sum(p in r["text"].lower() for p in low),
                       reverse=True)
            add(loose)
        # 3) подстрока — ловит части слов, которые FTS дробит иначе.
        #    Он единственный ловит части слов и числа: 'лыск' -> 'лыска',
        #    '83.8' -> 'Ø83.875'. FTS их дробит иначе и не находит.
        #    Цена: полный проход, ~24 мс на 50k. Поэтому включаем только
        #    когда FTS не дал НИЧЕГО и запрос короткий (для длинной фразы
        #    точное вхождение уже проверила ступень 1), и ограничиваем
        #    просмотр свежими записями.
        if not out and len(query) <= 24:
            rows = self.db.execute(
                "SELECT * FROM (SELECT * FROM fact ORDER BY id DESC LIMIT ?) "
                "WHERE text LIKE ? OR tags LIKE ? LIMIT ?",
                (LIKE_SCAN, f"%{query}%", f"%{query}%", limit)).fetchall()
            add([dict(r) for r in rows])
        return out[:limit]

    def fact_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM fact").fetchone()[0])

    # -------------------------------------------------------- онтология
    def upsert_entity(self, kind: str, name: str,
                      props: dict[str, Any] | None = None,
                      run_id: int | None = None) -> int:
        js = json.dumps(props or {}, ensure_ascii=False)
        try:
            cur = self.db.execute(
                "INSERT INTO entity(kind,name,props,run_id,created) "
                "VALUES(?,?,?,?,?)", (kind, name, js, run_id, self._now()))
            self.db.commit()
            return int(cur.lastrowid)
        except sqlite3.IntegrityError:
            row = self.db.execute(
                "SELECT id, props FROM entity WHERE kind=? AND name=?",
                (kind, name)).fetchone()
            if props:
                merged = {**json.loads(row["props"] or "{}"), **props}
                self.db.execute("UPDATE entity SET props=? WHERE id=?",
                                (json.dumps(merged, ensure_ascii=False), row["id"]))
                self.db.commit()
            return int(row["id"])

    def link(self, subj: tuple[str, str], pred: str, obj: tuple[str, str],
             props: dict[str, Any] | None = None,
             run_id: int | None = None) -> bool:
        a = self.upsert_entity(*subj, run_id=run_id)
        b = self.upsert_entity(*obj, run_id=run_id)
        try:
            self.db.execute(
                "INSERT INTO relation(subj,pred,obj,props,created) "
                "VALUES(?,?,?,?,?)",
                (a, pred, b, json.dumps(props or {}, ensure_ascii=False),
                 self._now()))
            self.db.commit()
            return True
        except sqlite3.IntegrityError:
            return False

    def get_entity(self, kind: str, name: str) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM entity WHERE kind=? AND name=?",
                            (kind, name)).fetchone()
        return dict(r) if r else None

    def set_entity_embedding(self, kind: str, name: str, vector: list[float],
                             description: str = "") -> None:
        """Записать эмбеддинг объекта (для RAG на онтологии, см. skills/rag.py).

        Если объекта ещё нет — создаём пустой, чтобы вызывающему коду не
        приходилось делать upsert_entity отдельно перед векторизацией.
        """
        self.upsert_entity(kind, name)
        if description:
            self.db.execute(
                "UPDATE entity SET embedding=?, description=? "
                "WHERE kind=? AND name=?",
                (json.dumps(vector), description, kind, name))
        else:
            self.db.execute(
                "UPDATE entity SET embedding=? WHERE kind=? AND name=?",
                (json.dumps(vector), kind, name))
        self.db.commit()

    def entities_with_embedding(self, kind: str = "") -> list[dict[str, Any]]:
        if kind:
            rows = self.db.execute(
                "SELECT * FROM entity WHERE kind=? AND embedding IS NOT NULL",
                (kind,))
        else:
            rows = self.db.execute(
                "SELECT * FROM entity WHERE embedding IS NOT NULL")
        return [dict(r) for r in rows]

    def semantic_search_entities(self, embedding: list[float], kind: str = "",
                                 limit: int = 10) -> list[dict[str, Any]]:
        """Косинусное сходство В ПАМЯТИ — линейный проход по векторизованным
        объектам. Для SQLite (один файл, один процесс) это уместно: даже
        десятки тысяч объектов проходятся за миллисекунды, а собственного
        векторного индекса SQLite не имеет. Для большего масштаба —
        навык pg_ontology/PgStore с pgvector.
        """
        out = []
        for row in self.entities_with_embedding(kind):
            try:
                vec = json.loads(row["embedding"])
            except (TypeError, json.JSONDecodeError):
                continue
            score = _cosine(embedding, vec)
            out.append({"kind": row["kind"], "name": row["name"],
                       "description": row["description"], "score": score})
        out.sort(key=lambda r: -r["score"])
        return out[:limit]

    def neighbours(self, kind: str, name: str) -> list[dict[str, Any]]:
        row = self.db.execute("SELECT id FROM entity WHERE kind=? AND name=?",
                              (kind, name)).fetchone()
        if not row:
            return []
        eid = row["id"]
        out = []
        for r in self.db.execute(
            "SELECT r.pred, e.kind, e.name, 'out' AS dir FROM relation r "
            "JOIN entity e ON e.id=r.obj WHERE r.subj=? "
            "UNION ALL "
            "SELECT r.pred, e.kind, e.name, 'in' AS dir FROM relation r "
            "JOIN entity e ON e.id=r.subj WHERE r.obj=?", (eid, eid)):
            out.append(dict(r))
        return out

    def graph_stats(self) -> tuple[int, int]:
        e = self.db.execute("SELECT COUNT(*) FROM entity").fetchone()[0]
        r = self.db.execute("SELECT COUNT(*) FROM relation").fetchone()[0]
        return int(e), int(r)

    # --------------------------------------------------- события/повторы
    def log_event(self, run_id: int, step: int, kind: str, name: str = "",
                  summary: str = "", sig: str = "") -> None:
        self.db.execute(
            "INSERT INTO event(run_id,step,kind,name,summary,sig,created) "
            "VALUES(?,?,?,?,?,?,?)",
            (run_id, step, kind, name, summary[:1000], sig, self._now()))
        self.db.commit()

    def sig_count(self, run_id: int, sig: str) -> int:
        return int(self.db.execute(
            "SELECT COUNT(*) FROM event WHERE run_id=? AND sig=?",
            (run_id, sig)).fetchone()[0])

    def recent_events(self, run_id: int, limit: int = 12) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM event WHERE run_id=? ORDER BY id DESC LIMIT ?",
            (run_id, limit))][::-1]

    # -------------------------------------------------- RAG: фрагменты
    def add_chunks(self, source: str, texts: list[str], tags: str = "",
                   entity_refs: list[tuple[str, str]] | None = None,
                   run_id: int | None = None) -> list[int]:
        """Заменяет все фрагменты источника на новые (переиндексация)."""
        self.db.execute("DELETE FROM chunk WHERE source=?", (source,))
        refs = json.dumps(list(entity_refs or []), ensure_ascii=False)
        ids: list[int] = []
        for i, text in enumerate(texts):
            cur = self.db.execute(
                "INSERT INTO chunk(source,ord,text,tags,entity_refs,run_id,"
                "created) VALUES(?,?,?,?,?,?,?)",
                (source, i, text, tags, refs, run_id, self._now()))
            ids.append(int(cur.lastrowid))
        self.db.commit()
        return ids

    def set_chunk_embedding(self, chunk_id: int, vector: list[float]) -> None:
        self.db.execute(
            "UPDATE chunk SET embedding=?, dim=? WHERE id=?",
            (json.dumps(vector), len(vector), chunk_id))
        self.db.commit()

    def chunks_without_embedding(self, limit: int = 500) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM chunk WHERE embedding IS NULL LIMIT ?", (limit,))]

    def all_chunks(self, source: str | None = None) -> list[dict[str, Any]]:
        if source:
            rows = self.db.execute(
                "SELECT * FROM chunk WHERE source=? ORDER BY ord", (source,))
        else:
            rows = self.db.execute("SELECT * FROM chunk ORDER BY source, ord")
        return [dict(r) for r in rows]

    def chunk_count(self, source: str | None = None) -> int:
        if source:
            return int(self.db.execute(
                "SELECT COUNT(*) FROM chunk WHERE source=?",
                (source,)).fetchone()[0])
        return int(self.db.execute("SELECT COUNT(*) FROM chunk").fetchone()[0])

    def sources(self) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT source FROM chunk ORDER BY source")]

    def fts_chunks(self, query: str, limit: int = 20) -> list[dict[str, Any]]:
        """Полнотекстовый поиск по фрагментам — та же логика ступеней,
        что и в recall(), но по-крупному тексту документов, а не фактам."""
        terms = self._terms(query)
        if not terms:
            return []
        prefixes = [self._stem(t) for t in terms]
        try:
            rows = self.db.execute(
                "WITH hit AS ("
                "  SELECT rowid FROM chunk_fts WHERE chunk_fts MATCH ?"
                "  ORDER BY rowid DESC LIMIT ?"
                ") SELECT c.* FROM hit JOIN chunk c ON c.id=hit.rowid LIMIT ?",
                (" OR ".join(f"{p}*" for p in prefixes),
                 max(limit * 20, 200), limit)).fetchall()
            return [dict(r) for r in rows]
        except sqlite3.OperationalError:
            return []

    def entity_chunks(self, kind: str, name: str) -> list[dict[str, Any]]:
        """Фрагменты, привязанные к объекту онтологии — RAG-по-графу."""
        out = []
        for r in self.db.execute("SELECT * FROM chunk"):
            d = dict(r)
            try:
                refs = json.loads(d.get("entity_refs") or "[]")
            except json.JSONDecodeError:
                refs = []
            if any(list(ref) == [kind, name] for ref in refs):
                out.append(d)
        return out

    def semantic_search_chunks(self, embedding: list[float], limit: int = 6,
                               source: str | None = None) -> list[dict[str, Any]]:
        """Косинусное сходство В ПАМЯТИ по фрагментам — см. пояснение у
        semantic_search_entities. Уместно для объёма одного agent.db;
        для большего масштаба — навык pg_ontology/rag с бэкендом PgStore.
        """
        if source:
            rows = self.db.execute(
                "SELECT * FROM chunk WHERE embedding IS NOT NULL AND source=?",
                (source,))
        else:
            rows = self.db.execute(
                "SELECT * FROM chunk WHERE embedding IS NOT NULL")
        out = []
        for row in rows:
            try:
                vec = json.loads(row["embedding"])
            except (TypeError, json.JSONDecodeError):
                continue
            score = _cosine(embedding, vec)
            out.append({"id": row["id"], "source": row["source"],
                       "ord": row["ord"], "text": row["text"],
                       "entity_refs": row["entity_refs"], "score": score})
        out.sort(key=lambda r: -r["score"])
        return out[:limit]

    # ------------------------------------------ cert_verify: чек-лист СБ
    _VALID_VERDICTS = {"compliant", "non_compliant", "not_found", "needs_review"}

    def add_cert_check(self, requirement: str, verdict: str,
                       requirement_text: str = "", evidence_source: str = "",
                       evidence_quote: str = "", quote_verified: bool = False,
                       comment: str = "", run_id: int | None = None) -> int:
        if verdict not in self._VALID_VERDICTS:
            raise ValueError(
                f"verdict должен быть одним из {sorted(self._VALID_VERDICTS)}, "
                f"получено {verdict!r}"
            )
        cur = self.db.execute(
            "INSERT INTO cert_check(run_id,requirement,requirement_text,"
            "verdict,evidence_source,evidence_quote,quote_verified,comment,"
            "created) VALUES(?,?,?,?,?,?,?,?,?)",
            (run_id, requirement.strip(), requirement_text, verdict,
             evidence_source, evidence_quote, int(quote_verified),
             comment, self._now()))
        self.db.commit()
        return int(cur.lastrowid)

    def cert_checks(self, run_id: int | None = None) -> list[dict[str, Any]]:
        if run_id is not None:
            rows = self.db.execute(
                "SELECT * FROM cert_check WHERE run_id=? ORDER BY id",
                (run_id,))
        else:
            rows = self.db.execute("SELECT * FROM cert_check ORDER BY id")
        return [dict(r) for r in rows]

    def cert_summary(self, run_id: int | None = None) -> dict[str, int]:
        checks = self.cert_checks(run_id)
        out = {v: 0 for v in self._VALID_VERDICTS}
        for c in checks:
            out[c["verdict"]] = out.get(c["verdict"], 0) + 1
        out["total"] = len(checks)
        out["unverified_quotes"] = sum(
            1 for c in checks
            if c["evidence_quote"] and not c["quote_verified"])
        return out

    def clear_cert_checks(self, run_id: int | None = None) -> int:
        """Начать проверку заново — например, при повторном прогоне на
        обновлённом комплекте документов."""
        if run_id is not None:
            cur = self.db.execute("DELETE FROM cert_check WHERE run_id=?",
                                  (run_id,))
        else:
            cur = self.db.execute("DELETE FROM cert_check")
        self.db.commit()
        return cur.rowcount

    # ---------------------------------------------- веб-морда (webui.py)
    # Read-only выборки для дашборда конфигов/логов: агент сам эти методы
    # не использует, они существуют ТОЛЬКО ради отображения состояния
    # человеку — поэтому без пагинации на курсорах (лимит сверху хватает
    # для одного файла на одну машину, тот же принцип, что у LIKE_SCAN).
    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM run ORDER BY id DESC LIMIT ?", (limit,))]

    def list_facts(self, limit: int = 100, query: str = "") -> list[dict[str, Any]]:
        """Обёртка над recall для дашборда: пустой запрос -> последние N."""
        return self.recall(query, limit=limit)

    def list_entities(self, kind: str = "", limit: int = 200) -> list[dict[str, Any]]:
        if kind:
            rows = self.db.execute(
                "SELECT * FROM entity WHERE kind=? ORDER BY id DESC LIMIT ?",
                (kind, limit))
        else:
            rows = self.db.execute(
                "SELECT * FROM entity ORDER BY id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def list_relations(self, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT r.id, sa.kind AS subj_kind, sa.name AS subj_name, "
            "r.pred, sb.kind AS obj_kind, sb.name AS obj_name, r.created "
            "FROM relation r "
            "JOIN entity sa ON sa.id=r.subj JOIN entity sb ON sb.id=r.obj "
            "ORDER BY r.id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def entity_kinds(self) -> list[str]:
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT kind FROM entity ORDER BY kind")]

    def run_events(self, run_id: int, limit: int = 500) -> list[dict[str, Any]]:
        """Полный (не только последние, как recent_events) журнал одного

        прогона в хронологическом порядке — для страницы "детали прогона"
        в дашборде.
        """
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM event WHERE run_id=? ORDER BY id LIMIT ?",
            (run_id, limit))]

    def counts(self) -> dict[str, int]:
        """Сводка для главной страницы дашборда: сколько чего накоплено."""
        out = {}
        for name, table in (("runs", "run"), ("facts", "fact"),
                            ("entities", "entity"), ("relations", "relation"),
                            ("events", "event"), ("chunks", "chunk"),
                            ("cert_checks", "cert_check")):
            out[name] = int(self.db.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        return out
