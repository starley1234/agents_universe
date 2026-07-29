"""Журнал прогонов и кеш результатов на SQLite.

Два разных механизма в одном файле, потому что оба про деньги:

1. **Кеш.** У каждого изображения уже есть sha256. Один и тот же кадр с
   теми же параметрами обязан стоить один вызов модели, а не десять:
   мобильный клиент переотправляет фото при ретраях сети, оператор
   открывает карточку повторно, тестировщик гоняет один и тот же файл.
   На реальном трафике это самая крупная статья экономии после сжатия
   картинок.

2. **Журнал.** VLM-запрос стоит денег, поэтому нужно знать, кто, что и
   почём запускал. Без этого невозможно ни выставить счёт клиенту, ни
   заметить, что кто-то жжёт бюджет.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    service     TEXT NOT NULL,
    status      TEXT NOT NULL,
    provider    TEXT,
    model       TEXT,
    created_at  REAL NOT NULL,
    duration_s  REAL,
    images_n    INTEGER DEFAULT 0,
    images_kb   REAL DEFAULT 0,
    tokens_in   INTEGER DEFAULT 0,
    tokens_out  INTEGER DEFAULT 0,
    cost_usd    REAL DEFAULT 0,
    cached      INTEGER DEFAULT 0,
    warnings_n  INTEGER DEFAULT 0,
    error       TEXT,
    client      TEXT,
    request_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_service ON runs(service, created_at DESC);

CREATE TABLE IF NOT EXISTS cache (
    key         TEXT PRIMARY KEY,
    service     TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at  REAL NOT NULL,
    hits        INTEGER DEFAULT 0,
    cost_saved  REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_cache_created ON cache(created_at);
"""


def cache_key(service: str, image_hashes: list[str], params: dict,
              provider: str, model: str) -> str:
    """Ключ кеша.

    В ключ входят модель и провайдер: ответ gpt-4o-mini и claude на одну
    картинку — разные ответы, и подменять один другим нельзя.
    """
    payload = json.dumps(
        {"s": service, "i": sorted(image_hashes), "p": _stable(params),
         "v": provider, "m": model},
        ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _stable(value: Any) -> Any:
    """Привести параметры к сравнимому виду: порядок ключей не должен влиять."""
    if isinstance(value, dict):
        return {k: _stable(value[k]) for k in sorted(value)}
    if isinstance(value, (list, tuple)):
        return [_stable(v) for v in value]
    return value


class Store:
    """Потокобезопасный журнал и кеш. Одно соединение под общим замком."""

    def __init__(self, path: str | Path = "data/vlm.db", cache_ttl_s: float = 86400 * 7):
        self.path = str(path)
        self.cache_ttl_s = cache_ttl_s
        self._lock = threading.Lock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    # --- кеш --------------------------------------------------------------
    def cache_get(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json, created_at FROM cache WHERE key=?", (key,)).fetchone()
        if row is None:
            return None
        if self.cache_ttl_s and time.time() - row["created_at"] > self.cache_ttl_s:
            self.cache_drop(key)
            return None
        try:
            return json.loads(row["result_json"])
        except json.JSONDecodeError:
            self.cache_drop(key)
            return None

    def cache_put(self, key: str, service: str, result: dict) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO cache (key, service, result_json, created_at,"
                " hits, cost_saved) VALUES (?,?,?,?,"
                " COALESCE((SELECT hits FROM cache WHERE key=?),0),"
                " COALESCE((SELECT cost_saved FROM cache WHERE key=?),0))",
                (key, service, json.dumps(result, ensure_ascii=False, default=str),
                 time.time(), key, key))

    def cache_hit(self, key: str, cost_saved: float = 0.0) -> None:
        with self._tx() as c:
            c.execute("UPDATE cache SET hits=hits+1, cost_saved=cost_saved+? WHERE key=?",
                      (cost_saved, key))

    def cache_drop(self, key: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM cache WHERE key=?", (key,))

    def cache_stats(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(hits),0) hits,"
                " COALESCE(SUM(cost_saved),0) saved FROM cache").fetchone()
        return {"entries": row["n"], "hits": row["hits"],
                "cost_saved_usd": round(row["saved"], 4)}

    def cache_purge(self, older_than_s: float | None = None) -> int:
        cutoff = time.time() - (older_than_s if older_than_s is not None else self.cache_ttl_s)
        with self._tx() as c:
            return c.execute("DELETE FROM cache WHERE created_at < ?", (cutoff,)).rowcount

    # --- журнал -----------------------------------------------------------
    def log_run(self, service: str, status: str, *, provider: str = "", model: str = "",
                duration_s: float = 0.0, images_n: int = 0, images_kb: float = 0.0,
                tokens_in: int = 0, tokens_out: int = 0, cost_usd: float = 0.0,
                cached: bool = False, warnings_n: int = 0, error: str | None = None,
                client: str | None = None, request_id: str | None = None) -> str:
        run_id = uuid.uuid4().hex[:16]
        with self._tx() as c:
            c.execute(
                "INSERT INTO runs (id, service, status, provider, model, created_at,"
                " duration_s, images_n, images_kb, tokens_in, tokens_out, cost_usd,"
                " cached, warnings_n, error, client, request_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (run_id, service, status, provider, model, time.time(), duration_s,
                 images_n, images_kb, tokens_in, tokens_out, cost_usd, int(cached),
                 warnings_n, (error or "")[:2000] or None, client, request_id))
        return run_id

    def runs(self, service: str | None = None, status: str | None = None,
             limit: int = 50) -> list[dict[str, Any]]:
        q, args = "SELECT * FROM runs WHERE 1=1", []
        if service:
            q += " AND service=?"
            args.append(service)
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT ?"
        args.append(min(limit, 500))
        with self._lock:
            return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def spend_since(self, since_ts: float) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(cost_usd),0) s FROM runs WHERE created_at >= ?",
                (since_ts,)).fetchone()
        return round(row["s"], 6)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            by_status = {r["status"]: r["n"] for r in self._conn.execute(
                "SELECT status, COUNT(*) n FROM runs GROUP BY status")}
            agg = self._conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) cost,"
                " COALESCE(AVG(duration_s),0) avg_s, COALESCE(SUM(images_n),0) imgs,"
                " COALESCE(SUM(cached),0) cached FROM runs").fetchone()
            by_service = [dict(r) for r in self._conn.execute(
                "SELECT service, COUNT(*) n, COALESCE(SUM(cost_usd),0) cost,"
                " COALESCE(AVG(duration_s),0) avg_s FROM runs"
                " GROUP BY service ORDER BY n DESC")]
        total = sum(by_status.values())
        ok, err = by_status.get("ok", 0), by_status.get("error", 0)
        return {
            "total_runs": total,
            "by_status": by_status,
            "by_service": by_service,
            "total_cost_usd": round(agg["cost"], 4),
            "avg_duration_s": round(agg["avg_s"], 3),
            "images_processed": agg["imgs"],
            "served_from_cache": agg["cached"],
            "cache_hit_rate": round(agg["cached"] / total, 3) if total else 0.0,
            "success_rate": round(ok / (ok + err), 3) if (ok + err) else None,
            "cache": self.cache_stats(),
        }

    def purge_runs(self, older_than_days: float) -> int:
        cutoff = time.time() - older_than_days * 86400
        with self._tx() as c:
            return c.execute("DELETE FROM runs WHERE created_at < ?", (cutoff,)).rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
