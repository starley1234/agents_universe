"""Поиск по смыслу без PostgreSQL: векторы прямо в SQLite.

Зачем: полнотекстовый поиск не находит «как закрепить деталь», если в
памяти записано «крепление узла болтами». Слова разные, смысл тот же.
PostgreSQL с pgvector это решает, но ставить его ради пяти тысяч
записей на сервере с гигабайтом памяти — перебор.

Замеры, на которых основано решение (они же в OVERVIEW.md):

    списки Python, 50k×384         процесс убит по памяти
    array('f'),    50k×384         75 МБ, поиск ~180 мс
    array('f'),     5k×384          7 МБ, поиск ~18 мс

Отсюда граница: до ~5000 записей это лучше PostgreSQL, дальше — хуже.
При превышении SOFT_LIMIT инструмент честно об этом говорит, а не
делает вид, что всё в порядке.

Векторы хранятся как BLOB (array('f').tobytes()) — вчетверо компактнее
JSON и читаются без разбора. Косинусная близость считается на
нормализованных векторах, поэтому это просто скалярное произведение.
"""
from __future__ import annotations

import json
import math
import sqlite3
import struct
import urllib.error
import urllib.request
from array import array
from typing import Any, Iterable

#: Выше этого числа записей честно предупреждаем: пора в PostgreSQL.
SOFT_LIMIT = 5_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS vec(
  id INTEGER PRIMARY KEY,
  ref_kind TEXT NOT NULL,        -- fact | chunk | entity
  ref_id INTEGER NOT NULL,
  text TEXT NOT NULL,
  dim INTEGER NOT NULL,
  data BLOB NOT NULL,            -- array('f'), нормализованный
  created REAL,
  UNIQUE(ref_kind, ref_id)
);
"""


def normalize(v: Iterable[float]) -> array:
    """Привести к единичной длине: тогда близость = скалярное произведение."""
    a = array("f", v)
    s = math.sqrt(sum(x * x for x in a))
    if s > 0:
        for i in range(len(a)):
            a[i] = a[i] / s
    return a


def dot(a: array, b: array) -> float:
    return sum(x * y for x, y in zip(a, b))


class Embedder:
    """Получение векторов через OpenAI-совместимый /embeddings.

    Недоступность службы НЕ ломает агента: возвращаем None, а
    вызывающий переходит на обычный поиск. Молча притворяться, что
    векторы есть, нельзя — поэтому причина сохраняется в .error.
    """

    def __init__(self, url: str = "", model: str = "", key: str = "",
                 dim: int = 0, timeout: int = 60) -> None:
        self.url = (url or "").rstrip("/")
        self.model = model
        self.key = key
        self.dim = dim
        self.timeout = timeout
        self.error = ""

    @property
    def ready(self) -> bool:
        return bool(self.url and self.model)

    def embed(self, texts: list[str]) -> list[array] | None:
        if not self.ready:
            self.error = "не настроено: нужны embed_url и embed_model"
            return None
        endpoint = self.url
        if not endpoint.endswith("/embeddings"):
            endpoint += "/embeddings"
        body = json.dumps({"model": self.model, "input": texts}).encode()
        headers = {"Content-Type": "application/json"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        req = urllib.request.Request(endpoint, data=body, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as exc:
            self.error = f"служба векторов ответила {exc.code}"
            return None
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            self.error = f"служба векторов недоступна: {exc}"
            return None
        rows = data.get("data") or []
        if len(rows) != len(texts):
            self.error = (f"служба вернула {len(rows)} векторов "
                          f"на {len(texts)} текстов")
            return None
        out = []
        for r in rows:
            v = r.get("embedding")
            if not isinstance(v, list) or not v:
                self.error = "в ответе нет поля embedding"
                return None
            out.append(normalize(v))
        self.error = ""
        return out


class VectorStore:
    """Векторы в той же базе, что и остальное состояние."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.db.executescript(SCHEMA)
        self.db.commit()

    def count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM vec").fetchone()[0])

    def add(self, ref_kind: str, ref_id: int, text: str,
            vector: array) -> int:
        import time
        cur = self.db.execute(
            "INSERT INTO vec(ref_kind,ref_id,text,dim,data,created) "
            "VALUES(?,?,?,?,?,?) ON CONFLICT(ref_kind,ref_id) DO UPDATE SET "
            "text=excluded.text, dim=excluded.dim, data=excluded.data",
            (ref_kind, ref_id, text, len(vector), vector.tobytes(),
             time.time()))
        self.db.commit()
        return int(cur.lastrowid)

    def drop(self, ref_kind: str = "", ref_id: int = 0) -> int:
        if ref_kind and ref_id:
            cur = self.db.execute(
                "DELETE FROM vec WHERE ref_kind=? AND ref_id=?",
                (ref_kind, ref_id))
        elif ref_kind:
            cur = self.db.execute("DELETE FROM vec WHERE ref_kind=?",
                                  (ref_kind,))
        else:
            cur = self.db.execute("DELETE FROM vec")
        self.db.commit()
        return cur.rowcount

    def search(self, query: array, limit: int = 10,
               ref_kind: str = "", min_score: float = 0.0
               ) -> list[dict[str, Any]]:
        """Ближайшие записи. Полный проход — других вариантов без
        индекса нет, но на нескольких тысячах это единицы миллисекунд."""
        sql = "SELECT id,ref_kind,ref_id,text,dim,data FROM vec"
        args: tuple = ()
        if ref_kind:
            sql += " WHERE ref_kind=?"
            args = (ref_kind,)
        best: list[tuple[float, dict[str, Any]]] = []
        qdim = len(query)
        for row in self.db.execute(sql, args):
            if row["dim"] != qdim:
                # Разная размерность = разные модели. Сравнивать нельзя:
                # получится случайное число, похожее на осмысленное.
                continue
            v = array("f")
            v.frombytes(row["data"])
            s = dot(query, v)
            if s < min_score:
                continue
            best.append((s, {"id": row["id"], "ref_kind": row["ref_kind"],
                             "ref_id": row["ref_id"], "text": row["text"],
                             "score": round(s, 4)}))
        best.sort(key=lambda x: x[0], reverse=True)
        return [d for _, d in best[:limit]]

    def stats(self) -> dict[str, Any]:
        n = self.count()
        by = {r[0]: r[1] for r in self.db.execute(
            "SELECT ref_kind, COUNT(*) FROM vec GROUP BY ref_kind")}
        dim = self.db.execute("SELECT dim FROM vec LIMIT 1").fetchone()
        bytes_ = n * (dim[0] if dim else 0) * 4
        return {"count": n, "by_kind": by, "dim": dim[0] if dim else 0,
                "bytes": bytes_, "over_limit": n > SOFT_LIMIT}
