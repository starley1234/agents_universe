"""Хранилище прогонов на SQLite.

Прогон пайплайна — это минуты работы и деньги за токены, поэтому результат
обязан переживать перезапуск процесса. Отсюда журнал на диске, а не словарь
в памяти: после падения видно, что запускали, чем кончилось и сколько стоило.

Схема одна на всё приложение; WAL включён, чтобы веб-запросы не блокировали
пишущий воркер.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    pipeline     TEXT NOT NULL,
    status       TEXT NOT NULL,
    provider     TEXT,
    model        TEXT,
    created_at   REAL NOT NULL,
    started_at   REAL,
    finished_at  REAL,
    duration_s   REAL,
    task_json    TEXT,
    result_json  TEXT,
    report_md    TEXT,
    error        TEXT,
    findings_n   INTEGER DEFAULT 0,
    tokens_in    INTEGER DEFAULT 0,
    tokens_out   INTEGER DEFAULT 0,
    cost_usd     REAL DEFAULT 0.0,
    created_by   TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_pipeline ON runs(pipeline, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

CREATE TABLE IF NOT EXISTS artifacts (
    run_id  TEXT NOT NULL,
    name    TEXT NOT NULL,
    kind    TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (run_id, name)
);
"""

STATUSES = ("queued", "running", "done", "failed", "cancelled")


@dataclass
class Run:
    """Одна запись журнала. Совпадает по полям с REST-ответом."""

    id: str
    pipeline: str
    status: str = "queued"
    provider: str | None = None
    model: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    duration_s: float | None = None
    task: dict[str, Any] = field(default_factory=dict)
    result: dict[str, Any] | None = None
    report: str = ""
    error: str | None = None
    findings_n: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    created_by: str | None = None

    def summary(self) -> dict[str, Any]:
        """Короткая форма для списков — без тяжёлого result/report."""
        d = asdict(self)
        d.pop("result", None)
        d.pop("report", None)
        d.pop("task", None)
        return d


class RunStore:
    """Потокобезопасный журнал прогонов."""

    def __init__(self, path: str | Path = "aconstructor.db"):
        self.path = str(path)
        self._lock = threading.Lock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: соединение делят воркер и веб-обработчики,
        # доступ сериализован self._lock
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

    # --- запись ----------------------------------------------------------
    def create(self, pipeline: str, task: dict, provider: str | None = None,
               model: str | None = None, created_by: str | None = None) -> Run:
        run = Run(id=uuid.uuid4().hex[:16], pipeline=pipeline, task=task,
                  provider=provider, model=model, created_by=created_by)
        with self._tx() as c:
            c.execute(
                "INSERT INTO runs (id, pipeline, status, provider, model, created_at,"
                " task_json, created_by) VALUES (?,?,?,?,?,?,?,?)",
                (run.id, run.pipeline, run.status, run.provider, run.model,
                 run.created_at, json.dumps(task, ensure_ascii=False, default=str),
                 run.created_by),
            )
        return run

    def mark_running(self, run_id: str) -> None:
        with self._tx() as c:
            c.execute("UPDATE runs SET status='running', started_at=? WHERE id=?",
                      (time.time(), run_id))

    def finish(self, run_id: str, result: dict, usage: dict | None = None) -> None:
        now = time.time()
        usage = usage or {}
        with self._tx() as c:
            row = c.execute("SELECT started_at FROM runs WHERE id=?", (run_id,)).fetchone()
            started = (row["started_at"] if row else None) or now
            c.execute(
                "UPDATE runs SET status='done', finished_at=?, duration_s=?, result_json=?,"
                " report_md=?, findings_n=?, tokens_in=?, tokens_out=?, cost_usd=? WHERE id=?",
                (now, round(now - started, 3),
                 json.dumps(result, ensure_ascii=False, default=str),
                 result.get("report", ""), len(result.get("findings") or []),
                 usage.get("tokens_in", 0), usage.get("tokens_out", 0),
                 usage.get("cost_usd", 0.0), run_id),
            )
            for name, value in (result.get("artifacts") or {}).items():
                # артефакт — это файл на выгрузку (скрипт, досье), а не
                # служебная строка состояния вроде названия листа
                if isinstance(value, str) and _is_artifact(name, value):
                    c.execute(
                        "INSERT OR REPLACE INTO artifacts (run_id, name, kind, content)"
                        " VALUES (?,?,?,?)",
                        (run_id, name, _artifact_kind(name), value),
                    )

    def fail(self, run_id: str, error: str) -> None:
        now = time.time()
        with self._tx() as c:
            row = c.execute("SELECT started_at FROM runs WHERE id=?", (run_id,)).fetchone()
            started = (row["started_at"] if row else None) or now
            c.execute(
                "UPDATE runs SET status='failed', finished_at=?, duration_s=?, error=?"
                " WHERE id=?",
                (now, round(now - started, 3), error[:4000], run_id),
            )

    def cancel(self, run_id: str) -> bool:
        """Отменить можно только то, что ещё не взято в работу."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE runs SET status='cancelled', finished_at=? WHERE id=? AND status='queued'",
                (time.time(), run_id),
            )
            return cur.rowcount > 0

    def requeue_stale(self) -> int:
        """После аварийного рестарта «running» уже никто не доведёт до конца."""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE runs SET status='failed', error='прервано рестартом сервиса',"
                " finished_at=? WHERE status IN ('running','queued')",
                (time.time(),),
            )
            return cur.rowcount

    # --- чтение ----------------------------------------------------------
    def get(self, run_id: str) -> Run | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _row_to_run(row) if row else None

    def list(self, pipeline: str | None = None, status: str | None = None,
             limit: int = 50, offset: int = 0) -> list[Run]:
        q = "SELECT * FROM runs WHERE 1=1"
        args: list[Any] = []
        if pipeline:
            q += " AND pipeline=?"
            args.append(pipeline)
        if status:
            q += " AND status=?"
            args.append(status)
        q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        args += [min(limit, 500), offset]
        with self._lock:
            rows = self._conn.execute(q, args).fetchall()
        return [_row_to_run(r) for r in rows]

    def artifacts(self, run_id: str) -> list[dict[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT name, kind, length(content) AS size FROM artifacts WHERE run_id=?",
                (run_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def artifact(self, run_id: str, name: str) -> dict[str, str] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT name, kind, content FROM artifacts WHERE run_id=? AND name=?",
                (run_id, name),
            ).fetchone()
        return dict(row) if row else None

    def stats(self) -> dict[str, Any]:
        with self._lock:
            by_status = {r["status"]: r["n"] for r in self._conn.execute(
                "SELECT status, COUNT(*) n FROM runs GROUP BY status")}
            agg = self._conn.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) cost,"
                " COALESCE(AVG(duration_s),0) avg_s, COALESCE(SUM(findings_n),0) findings"
                " FROM runs WHERE status='done'").fetchone()
            by_pipeline = [dict(r) for r in self._conn.execute(
                "SELECT pipeline, COUNT(*) n, COALESCE(AVG(duration_s),0) avg_s"
                " FROM runs GROUP BY pipeline ORDER BY n DESC")]
        total = sum(by_status.values())
        done, failed = by_status.get("done", 0), by_status.get("failed", 0)
        return {
            "total": total,
            "by_status": by_status,
            "by_pipeline": by_pipeline,
            "completed": agg["n"],
            "total_cost_usd": round(agg["cost"], 4),
            "avg_duration_s": round(agg["avg_s"], 3),
            "total_findings": agg["findings"],
            "success_rate": round(done / (done + failed), 3) if (done + failed) else None,
        }

    def purge(self, older_than_days: float) -> int:
        cutoff = time.time() - older_than_days * 86400
        with self._tx() as c:
            c.execute("DELETE FROM artifacts WHERE run_id IN"
                      " (SELECT id FROM runs WHERE created_at < ?)", (cutoff,))
            return c.execute("DELETE FROM runs WHERE created_at < ?", (cutoff,)).rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()


ARTIFACT_SUFFIXES = ("_script", "_md", "_json", "_csv", "_xml")


def _is_artifact(name: str, value: str) -> bool:
    return name.endswith(ARTIFACT_SUFFIXES) and len(value) > 40


def _artifact_kind(name: str) -> str:
    if name.endswith("_script"):
        return "py" if "revit" in name else "lsp"
    if name.endswith("_md"):
        return "md"
    return "txt"


def _row_to_run(row: sqlite3.Row) -> Run:
    return Run(
        id=row["id"], pipeline=row["pipeline"], status=row["status"],
        provider=row["provider"], model=row["model"], created_at=row["created_at"],
        started_at=row["started_at"], finished_at=row["finished_at"],
        duration_s=row["duration_s"],
        task=json.loads(row["task_json"]) if row["task_json"] else {},
        result=json.loads(row["result_json"]) if row["result_json"] else None,
        report=row["report_md"] or "", error=row["error"],
        findings_n=row["findings_n"] or 0, tokens_in=row["tokens_in"] or 0,
        tokens_out=row["tokens_out"] or 0, cost_usd=row["cost_usd"] or 0.0,
        created_by=row["created_by"],
    )
