"""Состояние среды: SQLite. Прогоны, шаги, доска контекста, чекпоинты, журнал.

ПОЧЕМУ ХРАНИЛИЩЕ — ЧАСТЬ ЯДРА, А НЕ ДЕТАЛЬ РЕАЛИЗАЦИИ.
Среда обещает две вещи, которые невозможны без внешнего состояния:
  * Human-in-the-Loop: прогон останавливается на человеке и ждёт —
    возможно, до завтра, возможно, переживая перезапуск процесса. Если
    состояние живёт в оперативной памяти, «точка контроля» превращается
    в «подождите, не закрывайте вкладку».
  * Аудит: кто из агентов что записал на доску, какой инструмент с
    какими аргументами вызвал, почему Контролёр вернул работу. Это
    ровно то, ради чего платформу выбирают вместо связки промптов.

ПОЧЕМУ SQLite. Ноль зависимостей, один файл, переживает перезапуск,
поддерживает транзакции и WAL — этого достаточно для одной машины и
десятков параллельных прогонов. Соседний проект MAOS требует
PostgreSQL+pgvector, потому что там векторный поиск по онтологии; здесь
семантики нет вообще — только строго типизированное состояние процесса.

СХЕМА (шесть таблиц, больше не нужно):
  run        — прогон workflow: цель, статус, счётчики стоимости;
  step       — шаг прогона: роль-исполнитель, состояние, итог;
  context    — ДОСКА (blackboard): ключ -> значение, версионируется;
  checkpoint — точка контроля для человека: что спросили, что ответили;
  event      — журнал: всё, что происходило, для трассировки и дашборда;
  tool_call  — вызовы инструментов: аргументы, результат, длительность.
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS run(
  id INTEGER PRIMARY KEY,
  workflow TEXT NOT NULL,            -- имя определения workflow
  goal TEXT NOT NULL DEFAULT '',     -- подставляется в {goal} задач
  -- running | waiting_human | done | failed | cancelled
  status TEXT NOT NULL DEFAULT 'running',
  detail TEXT DEFAULT '',            -- причина остановки/провала
  created REAL, updated REAL, finished REAL,
  steps_done INTEGER DEFAULT 0,
  tool_calls INTEGER DEFAULT 0,
  tokens_in INTEGER DEFAULT 0,
  tokens_out INTEGER DEFAULT 0,
  llm_calls INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS step(
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  ord INTEGER NOT NULL,              -- порядковый номер в workflow, с 0
  name TEXT NOT NULL,                -- имя шага из определения
  profile TEXT DEFAULT '',           -- профиль агента-исполнителя
  -- pending | running | waiting_human | done | failed | skipped
  status TEXT NOT NULL DEFAULT 'pending',
  revisions INTEGER DEFAULT 0,       -- сколько раз возвращали на доработку
  score REAL,                        -- последняя оценка Критика [0..1]
  output TEXT DEFAULT '',            -- принятый результат шага
  detail TEXT DEFAULT '',            -- вердикт Контролёра/причина отказа
  started REAL, finished REAL,
  UNIQUE(run_id, ord)
);

-- ДОСКА КОНТЕКСТА. Каждая запись — новая ВЕРСИЯ ключа, старые не
-- затираются: расследование «откуда взялась эта цифра» важнее экономии
-- места. Чтение по умолчанию отдаёт последнюю версию (см. ctx_get).
CREATE TABLE IF NOT EXISTS context(
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  key TEXT NOT NULL,
  value TEXT NOT NULL DEFAULT '',    -- JSON-строка
  version INTEGER NOT NULL DEFAULT 1,
  author TEXT DEFAULT '',            -- какой шаг/роль/человек записал
  created REAL
);
CREATE INDEX IF NOT EXISTS idx_context_run_key ON context(run_id, key, version);

CREATE TABLE IF NOT EXISTS checkpoint(
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES run(id) ON DELETE CASCADE,
  step_id INTEGER REFERENCES step(id) ON DELETE CASCADE,
  kind TEXT NOT NULL DEFAULT 'approval',   -- approval | tool | input
  question TEXT DEFAULT '',
  payload TEXT DEFAULT '',           -- JSON: что именно показываем человеку
  -- pending | approved | rejected | edited | cancelled
  status TEXT NOT NULL DEFAULT 'pending',
  response TEXT DEFAULT '',          -- правка/комментарий человека
  actor TEXT DEFAULT '',             -- кто ответил
  created REAL, resolved REAL
);
CREATE INDEX IF NOT EXISTS idx_checkpoint_status ON checkpoint(status, run_id);

CREATE TABLE IF NOT EXISTS event(
  id INTEGER PRIMARY KEY,
  run_id INTEGER REFERENCES run(id) ON DELETE CASCADE,
  step_id INTEGER,
  ts REAL,
  kind TEXT NOT NULL,                -- run_start | step_start | worker | ...
  role TEXT DEFAULT '',              -- worker | critic | supervisor | human
  message TEXT DEFAULT '',
  data TEXT DEFAULT ''               -- JSON с подробностями
);
CREATE INDEX IF NOT EXISTS idx_event_run ON event(run_id, id);

CREATE TABLE IF NOT EXISTS tool_call(
  id INTEGER PRIMARY KEY,
  run_id INTEGER REFERENCES run(id) ON DELETE CASCADE,
  step_id INTEGER,
  ts REAL,
  tool TEXT NOT NULL,
  args TEXT DEFAULT '',              -- JSON
  ok INTEGER DEFAULT 1,
  result TEXT DEFAULT '',            -- усечённый результат
  elapsed REAL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tool_run ON tool_call(run_id, id);
"""

#: Терминальные статусы прогона — в них ядро больше ничего не делает.
FINAL_RUN_STATUSES = ("done", "failed", "cancelled")


class StoreError(RuntimeError):
    """Ожидаемая ошибка хранилища (нет такого прогона, битый JSON и т.п.)."""


def _dumps(value: Any) -> str:
    """JSON без сюрпризов: русский текст остаётся русским, а не \\uXXXX."""
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(raw: str, default: Any = None) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        # Строка, записанная не нами (правка руками, старая версия) —
        # возвращаем как есть: терять данные хуже, чем отдать текст.
        return raw


class Store:
    """Тонкая обёртка над SQLite. Без ORM — запросов десятки, не сотни."""

    def __init__(self, path: str | Path = "awos.db") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.path = str(Path(self.path).expanduser())
        # check_same_thread=False: HTTP-сервер обслуживает запросы в разных
        # потоках, а SQLite сериализует запись сам. Транзакции короткие,
        # поэтому блокировок на практике не видно.
        self.db = sqlite3.connect(self.path, check_same_thread=False,
                                  timeout=30.0)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.db.commit()

    # --- служебное ------------------------------------------------------
    def close(self) -> None:
        try:
            self.db.close()
        except sqlite3.Error:
            pass

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    @staticmethod
    def _now() -> float:
        return time.time()

    def _one(self, sql: str, args: tuple = ()) -> dict[str, Any] | None:
        row = self.db.execute(sql, args).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, args: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    # --- прогоны --------------------------------------------------------
    def create_run(self, workflow: str, goal: str = "") -> int:
        now = self._now()
        cur = self.db.execute(
            "INSERT INTO run(workflow, goal, status, created, updated) "
            "VALUES(?,?,'running',?,?)", (workflow, goal, now, now))
        self.db.commit()
        return int(cur.lastrowid)

    def get_run(self, run_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM run WHERE id=?", (run_id,))

    def require_run(self, run_id: int) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run is None:
            raise StoreError(f"Прогон #{run_id} не найден")
        return run

    def set_run_status(self, run_id: int, status: str, detail: str = "") -> None:
        now = self._now()
        finished = now if status in FINAL_RUN_STATUSES else None
        self.db.execute(
            "UPDATE run SET status=?, detail=?, updated=?, "
            "finished=COALESCE(?, finished) WHERE id=?",
            (status, detail, now, finished, run_id))
        self.db.commit()

    def bump_run(self, run_id: int, *, steps: int = 0, tools: int = 0,
                 tokens_in: int = 0, tokens_out: int = 0,
                 llm_calls: int = 0) -> None:
        self.db.execute(
            "UPDATE run SET steps_done=steps_done+?, tool_calls=tool_calls+?, "
            "tokens_in=tokens_in+?, tokens_out=tokens_out+?, "
            "llm_calls=llm_calls+?, updated=? WHERE id=?",
            (steps, tools, tokens_in, tokens_out, llm_calls, self._now(), run_id))
        self.db.commit()

    def list_runs(self, limit: int = 50, status: str = "") -> list[dict[str, Any]]:
        if status:
            return self._all(
                "SELECT * FROM run WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, limit))
        return self._all("SELECT * FROM run ORDER BY id DESC LIMIT ?", (limit,))

    def delete_run(self, run_id: int) -> bool:
        cur = self.db.execute("DELETE FROM run WHERE id=?", (run_id,))
        self.db.commit()
        return cur.rowcount > 0

    # --- шаги -----------------------------------------------------------
    def add_step(self, run_id: int, ord_: int, name: str,
                 profile: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO step(run_id, ord, name, profile, status) "
            "VALUES(?,?,?,?, 'pending')", (run_id, ord_, name, profile))
        self.db.commit()
        return int(cur.lastrowid)

    def get_step(self, step_id: int) -> dict[str, Any] | None:
        return self._one("SELECT * FROM step WHERE id=?", (step_id,))

    def steps(self, run_id: int) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM step WHERE run_id=? ORDER BY ord",
                         (run_id,))

    def next_pending_step(self, run_id: int) -> dict[str, Any] | None:
        """Первый шаг, который ещё предстоит выполнить.

        waiting_human тоже сюда попадает: после ответа человека прогон
        обязан продолжить ИМЕННО с этого шага, а не проскочить его.
        """
        return self._one(
            "SELECT * FROM step WHERE run_id=? AND status IN "
            "('pending','running','waiting_human') ORDER BY ord LIMIT 1",
            (run_id,))

    def update_step(self, step_id: int, *, status: str | None = None,
                    output: str | None = None, detail: str | None = None,
                    score: float | None = None,
                    revisions: int | None = None) -> None:
        sets, args = [], []
        if status is not None:
            sets.append("status=?")
            args.append(status)
            if status == "running":
                sets.append("started=COALESCE(started, ?)")
                args.append(self._now())
            elif status in ("done", "failed", "skipped"):
                sets.append("finished=?")
                args.append(self._now())
        for column, value in (("output", output), ("detail", detail),
                              ("score", score), ("revisions", revisions)):
            if value is not None:
                sets.append(f"{column}=?")
                args.append(value)
        if not sets:
            return
        args.append(step_id)
        self.db.execute(f"UPDATE step SET {', '.join(sets)} WHERE id=?", tuple(args))
        self.db.commit()

    # --- доска контекста -------------------------------------------------
    def ctx_put(self, run_id: int, key: str, value: Any,
                author: str = "") -> int:
        """Записать НОВУЮ версию ключа. Старые версии остаются в истории."""
        if not key or not isinstance(key, str):
            raise StoreError("Ключ контекста должен быть непустой строкой")
        row = self.db.execute(
            "SELECT MAX(version) AS v FROM context WHERE run_id=? AND key=?",
            (run_id, key)).fetchone()
        version = int((row["v"] or 0)) + 1
        self.db.execute(
            "INSERT INTO context(run_id, key, value, version, author, created) "
            "VALUES(?,?,?,?,?,?)",
            (run_id, key, _dumps(value), version, author, self._now()))
        self.db.commit()
        return version

    def ctx_get(self, run_id: int, key: str, default: Any = None) -> Any:
        row = self._one(
            "SELECT value FROM context WHERE run_id=? AND key=? "
            "ORDER BY version DESC LIMIT 1", (run_id, key))
        return _loads(row["value"], default) if row else default

    def ctx_all(self, run_id: int) -> dict[str, Any]:
        """Снимок доски: последняя версия каждого ключа."""
        rows = self._all(
            "SELECT c.key, c.value FROM context c JOIN ("
            "  SELECT key, MAX(version) AS v FROM context WHERE run_id=? "
            "  GROUP BY key) m ON c.key=m.key AND c.version=m.v "
            "WHERE c.run_id=?", (run_id, run_id))
        return {r["key"]: _loads(r["value"]) for r in rows}

    def ctx_history(self, run_id: int, key: str = "") -> list[dict[str, Any]]:
        if key:
            rows = self._all(
                "SELECT * FROM context WHERE run_id=? AND key=? "
                "ORDER BY version", (run_id, key))
        else:
            rows = self._all(
                "SELECT * FROM context WHERE run_id=? ORDER BY id", (run_id,))
        for r in rows:
            r["value"] = _loads(r["value"])
        return rows

    def ctx_keys(self, run_id: int) -> list[str]:
        return [r["key"] for r in self._all(
            "SELECT DISTINCT key FROM context WHERE run_id=? ORDER BY key",
            (run_id,))]

    # --- точки контроля (HITL) --------------------------------------------
    def create_checkpoint(self, run_id: int, step_id: int | None, kind: str,
                          question: str, payload: Any = None) -> int:
        cur = self.db.execute(
            "INSERT INTO checkpoint(run_id, step_id, kind, question, payload, "
            "status, created) VALUES(?,?,?,?,?, 'pending', ?)",
            (run_id, step_id, kind, question, _dumps(payload or {}), self._now()))
        self.db.commit()
        return int(cur.lastrowid)

    def get_checkpoint(self, checkpoint_id: int) -> dict[str, Any] | None:
        row = self._one("SELECT * FROM checkpoint WHERE id=?", (checkpoint_id,))
        if row:
            row["payload"] = _loads(row["payload"], {})
        return row

    def pending_checkpoint(self, run_id: int) -> dict[str, Any] | None:
        row = self._one(
            "SELECT * FROM checkpoint WHERE run_id=? AND status='pending' "
            "ORDER BY id LIMIT 1", (run_id,))
        if row:
            row["payload"] = _loads(row["payload"], {})
        return row

    def list_checkpoints(self, run_id: int | None = None,
                         status: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM checkpoint WHERE 1=1"
        args: list[Any] = []
        if run_id is not None:
            sql += " AND run_id=?"
            args.append(run_id)
        if status:
            sql += " AND status=?"
            args.append(status)
        rows = self._all(sql + " ORDER BY id DESC LIMIT 200", tuple(args))
        for r in rows:
            r["payload"] = _loads(r["payload"], {})
        return rows

    def resolve_checkpoint(self, checkpoint_id: int, status: str,
                           response: str = "", actor: str = "human") -> None:
        allowed = ("approved", "rejected", "edited", "cancelled")
        if status not in allowed:
            raise StoreError(
                f"Решение {status!r} неизвестно, допустимо: {', '.join(allowed)}")
        cur = self.db.execute(
            "UPDATE checkpoint SET status=?, response=?, actor=?, resolved=? "
            "WHERE id=? AND status='pending'",
            (status, response, actor, self._now(), checkpoint_id))
        self.db.commit()
        if cur.rowcount == 0:
            raise StoreError(
                f"Точка контроля #{checkpoint_id} не найдена или уже закрыта")

    # --- журнал -----------------------------------------------------------
    def log(self, run_id: int | None, kind: str, message: str = "",
            role: str = "", step_id: int | None = None,
            data: Any = None) -> int:
        cur = self.db.execute(
            "INSERT INTO event(run_id, step_id, ts, kind, role, message, data) "
            "VALUES(?,?,?,?,?,?,?)",
            (run_id, step_id, self._now(), kind, role, message,
             _dumps(data) if data is not None else ""))
        self.db.commit()
        return int(cur.lastrowid)

    def events(self, run_id: int, after_id: int = 0,
               limit: int = 500) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM event WHERE run_id=? AND id>? ORDER BY id LIMIT ?",
            (run_id, after_id, limit))
        for r in rows:
            r["data"] = _loads(r["data"], None)
        return rows

    def log_tool_call(self, run_id: int | None, step_id: int | None, tool: str,
                      args: Any, ok: bool, result: str, elapsed: float) -> int:
        cur = self.db.execute(
            "INSERT INTO tool_call(run_id, step_id, ts, tool, args, ok, result, "
            "elapsed) VALUES(?,?,?,?,?,?,?,?)",
            (run_id, step_id, self._now(), tool, _dumps(args), 1 if ok else 0,
             result[:4000], elapsed))
        self.db.commit()
        return int(cur.lastrowid)

    def tool_calls(self, run_id: int, limit: int = 200) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT * FROM tool_call WHERE run_id=? ORDER BY id DESC LIMIT ?",
            (run_id, limit))
        for r in rows:
            r["args"] = _loads(r["args"], {})
        return rows

    # --- сводка -----------------------------------------------------------
    def stats(self) -> dict[str, Any]:
        def scalar(sql: str, args: tuple = ()) -> int:
            row = self.db.execute(sql, args).fetchone()
            return int(row[0] or 0)

        return {
            "runs": scalar("SELECT COUNT(*) FROM run"),
            "runs_running": scalar("SELECT COUNT(*) FROM run WHERE status='running'"),
            "runs_waiting_human": scalar(
                "SELECT COUNT(*) FROM run WHERE status='waiting_human'"),
            "runs_done": scalar("SELECT COUNT(*) FROM run WHERE status='done'"),
            "runs_failed": scalar("SELECT COUNT(*) FROM run WHERE status='failed'"),
            "steps": scalar("SELECT COUNT(*) FROM step"),
            "checkpoints_pending": scalar(
                "SELECT COUNT(*) FROM checkpoint WHERE status='pending'"),
            "tool_calls": scalar("SELECT COUNT(*) FROM tool_call"),
            "tokens_in": scalar("SELECT SUM(tokens_in) FROM run"),
            "tokens_out": scalar("SELECT SUM(tokens_out) FROM run"),
            "llm_calls": scalar("SELECT SUM(llm_calls) FROM run"),
        }

    def iter_runs_waiting(self) -> Iterator[dict[str, Any]]:
        yield from self.list_runs(limit=500, status="waiting_human")
