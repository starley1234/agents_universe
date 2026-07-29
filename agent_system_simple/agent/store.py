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
import re
import sqlite3
import time
from pathlib import Path
from typing import Any

#: сколько свежих фактов просматривать подстрочным поиском.
#: Полный проход по большой базе стоит десятки миллисекунд, а старые
#: записи почти никогда не нужны — агент оперирует недавним.
LIKE_SCAN = 20_000

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
  -- blocked: пункт упёрся в вопрос к человеку. Не выполнен и не провален,
  -- в работу больше не берётся, виден в итоге прогона.
  status TEXT DEFAULT 'open',        -- open|doing|done|failed|skipped|blocked
  result TEXT, created REAL, updated REAL,
  ord INTEGER DEFAULT 0,
  -- Декомпозиция: кто делает пункт, от каких пунктов он зависит и чем
  -- подтверждается выполнение. Без этого план был плоским списком, а
  -- порядок — просто нумерацией: пункт «собрать отчёт» брался в работу
  -- раньше «посчитать данные», и агент писал отчёт ни о чём.
  profile TEXT DEFAULT '',           -- назначенный исполнитель
  needs TEXT DEFAULT '',             -- номера пунктов через запятую
  check_hint TEXT DEFAULT ''         -- чем подтверждается выполнение
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
-- Без этого триггера правка факта оставляла в индексе СТАРЫЙ текст:
-- поиск находил исправленное по неверным словам и не находил по верным.
CREATE TRIGGER IF NOT EXISTS fact_au AFTER UPDATE ON fact BEGIN
  INSERT INTO fact_fts(fact_fts, rowid, text, tags)
    VALUES('delete', old.id, old.text, old.tags);
  INSERT INTO fact_fts(rowid, text, tags) VALUES (new.id, new.text, new.tags);
END;

CREATE TABLE IF NOT EXISTS entity(
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                -- part | file | person | idea | metric ...
  name TEXT NOT NULL,
  props TEXT DEFAULT '{}',
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
-- Дискуссия двух моделей: отдельные таблицы, чтобы не путать с прогонами.
-- Прогон — это работа по плану, дискуссия — обмен доводами; общего у них
-- только расход денег.
CREATE TABLE IF NOT EXISTS debate(
  id INTEGER PRIMARY KEY,
  question TEXT NOT NULL,
  status TEXT DEFAULT 'active',   -- active|done|no_consensus|budget|stuck
  model_a TEXT, model_b TEXT, model_arbiter TEXT,
  rounds INTEGER DEFAULT 0,
  tok_in INTEGER DEFAULT 0, tok_out INTEGER DEFAULT 0,
  cost REAL DEFAULT 0,
  verdict TEXT,
  started REAL, finished REAL
);

CREATE TABLE IF NOT EXISTS branch(
  id INTEGER PRIMARY KEY,
  debate_id INTEGER NOT NULL,
  parent_id INTEGER,              -- NULL = основная ветка
  assumption TEXT,                -- при каком предположении идём
  status TEXT DEFAULT 'active',
  verdict TEXT, created REAL
);

CREATE TABLE IF NOT EXISTS turn(
  id INTEGER PRIMARY KEY,
  debate_id INTEGER NOT NULL,
  branch_id INTEGER,
  round INTEGER,
  role TEXT NOT NULL,             -- a|b|arbiter|executor
  model TEXT,
  text TEXT NOT NULL,
  sig TEXT,                       -- подпись довода: ловим топтание
  tokens INTEGER DEFAULT 0,
  cost REAL DEFAULT 0,
  created REAL
);
CREATE INDEX IF NOT EXISTS ix_turn_debate ON turn(debate_id, round);
CREATE INDEX IF NOT EXISTS ix_turn_sig ON turn(debate_id, sig);
CREATE INDEX IF NOT EXISTS ix_branch_debate ON branch(debate_id);

-- Очередь заданий: поставил и забыл. Задание переживает выход из SSH,
-- закрытие браузера и перезагрузку сервера — работает фоновый
-- исполнитель, а не терминал пользователя.
CREATE TABLE IF NOT EXISTS job(
  id INTEGER PRIMARY KEY,
  goal TEXT NOT NULL,
  profile TEXT DEFAULT '',
  status TEXT DEFAULT 'queued',  -- queued|running|done|failed|stopped
  run_id INTEGER,                -- прогон, в котором выполняется
  hours REAL DEFAULT 1.0,
  max_usd REAL DEFAULT 0,
  decompose INTEGER DEFAULT 1,
  notify TEXT DEFAULT '',        -- куда сообщить о готовности
  attempts INTEGER DEFAULT 0,    -- сколько раз брали в работу
  rounds INTEGER DEFAULT 0,      -- сколько подходов к работе сделано
  max_rounds INTEGER DEFAULT 20, -- предел подходов: не вечный двигатель
  progress TEXT DEFAULT '',      -- сколько пунктов было сделано в прошлый раз
  next_at REAL DEFAULT 0,        -- когда можно продолжить (пауза после сбоя)
  worker TEXT DEFAULT '',        -- кто взял: host:pid
  heartbeat REAL,                -- когда исполнитель отметился живым
  result TEXT,
  created REAL, started REAL, finished REAL
);
CREATE INDEX IF NOT EXISTS ix_job_status ON job(status, id);

CREATE INDEX IF NOT EXISTS ix_event_run ON event(run_id, step);
CREATE INDEX IF NOT EXISTS ix_event_sig ON event(run_id, sig);
CREATE INDEX IF NOT EXISTS ix_task_run ON task(run_id, status);
"""


class Store:
    def __init__(self, path: str | Path = "agent.db") -> None:
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self._migrate()
        self.db.commit()

    #: Колонки, добавленные после первых версий. CREATE TABLE IF NOT
    #: EXISTS их не создаст: таблица уже есть. Без миграции старая база
    #: пользователя падала бы с «no such column» на первом же прогоне.
    _ADDED = {
        "task": [("profile", "TEXT DEFAULT ''"),
                 ("needs", "TEXT DEFAULT ''"),
                 ("check_hint", "TEXT DEFAULT ''")],
    }

    def _migrate(self) -> None:
        for table, columns in self._ADDED.items():
            have = {r[1] for r in self.db.execute(
                f"PRAGMA table_info({table})")}
            for name, decl in columns:
                if name not in have:
                    self.db.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

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

    def add_steps(self, run_id: int, steps: list[dict[str, Any]],
                  parent: int | None = None) -> list[int]:
        """Добавить пункты плана с исполнителем и зависимостями.

        steps: [{"title", "profile", "needs": [номера в ЭТОМ списке],
                 "check"}]. Номера в needs — позиции внутри списка (1,2,3),
        а не id в базе: модель не знает будущих id. Переводим здесь.
        """
        base = self.db.execute(
            "SELECT COALESCE(MAX(ord),0) FROM task WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        ids: list[int] = []
        for i, st in enumerate(steps, 1):
            title = str(st.get("title") or "").strip()
            if not title:
                continue
            cur = self.db.execute(
                "INSERT INTO task(run_id,parent_id,title,created,updated,"
                "ord,profile,check_hint) VALUES(?,?,?,?,?,?,?,?)",
                (run_id, parent, title, self._now(), self._now(),
                 base + i, str(st.get("profile") or "")[:40],
                 str(st.get("check") or "")[:300]))
            ids.append(int(cur.lastrowid))
        # Зависимости проставляем ВТОРЫМ проходом: пункт может зависеть
        # от следующего по списку, а его id ещё не существовал.
        for i, st in enumerate(steps[:len(ids)]):
            needs = st.get("needs") or []
            real = [str(ids[n - 1]) for n in needs
                    if isinstance(n, int) and 1 <= n <= len(ids)
                    and ids[n - 1] != ids[i]]      # сам от себя не зависит
            if real:
                self.db.execute("UPDATE task SET needs=? WHERE id=?",
                                (",".join(real), ids[i]))
        self.db.commit()
        return ids

    def _blockers(self, task: dict[str, Any],
                  by_id: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
        """Незакрытые пункты, от которых зависит этот."""
        out = []
        for raw in (task.get("needs") or "").split(","):
            raw = raw.strip()
            if not raw.isdigit():
                continue
            dep = by_id.get(int(raw))
            if dep and dep["status"] not in ("done", "skipped"):
                out.append(dep)
        return out

    def children(self, task_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM task WHERE parent_id=? ORDER BY ord", (task_id,))]

    def next_ready_task(self, run_id: int) -> dict[str, Any] | None:
        """Первый пункт, у которого выполнены зависимости.

        Без этого «собрать отчёт» бралось раньше «посчитать данные»
        просто потому, что стояло выше по номеру.

        Пункт с подшагами сам в работу НЕ берётся: работают его дети.
        Иначе агент делал бы работу дважды — сначала крупный пункт
        целиком, потом его же по частям.
        """
        rows = self.tasks(run_id)
        by_id = {t["id"]: t for t in rows}
        kids: dict[int, list[dict[str, Any]]] = {}
        for t in rows:
            if t["parent_id"]:
                kids.setdefault(t["parent_id"], []).append(t)

        def has_open_kids(t: dict[str, Any]) -> bool:
            return any(k["status"] in ("open", "doing")
                       for k in kids.get(t["id"], []))

        doing = [t for t in rows
                 if t["status"] == "doing" and not has_open_kids(t)]
        if doing:
            return doing[0]
        for t in rows:
            if t["status"] != "open" or has_open_kids(t):
                continue
            if not self._blockers(t, by_id):
                return t
        return None

    def close_finished_parents(self, run_id: int) -> list[dict[str, Any]]:
        """Закрыть пункты, все подшаги которых завершены.

        Родитель — это не работа, а заголовок: своей работы у него нет,
        и держать его «в работе» после того, как дети сделаны, значит
        показывать человеку план, который никогда не закончится.
        """
        closed = []
        for t in self.tasks(run_id):
            if t["status"] not in ("open", "doing"):
                continue
            kids = self.children(t["id"])
            if not kids or any(k["status"] in ("open", "doing")
                               for k in kids):
                continue
            done = [k for k in kids if k["status"] == "done"]
            failed = [k for k in kids if k["status"] == "failed"]
            status = "done" if done and not failed else "failed"
            note = f"подшагов: {len(done)} из {len(kids)}"
            if failed:
                note += "; не вышло: " + "; ".join(
                    k["title"][:60] for k in failed[:3])
            self.set_task(t["id"], status, note)
            t["status"], t["result"] = status, note
            closed.append(t)
        return closed

    def deadlocked(self, run_id: int) -> list[dict[str, Any]]:
        """Открытые пункты, которые НИКОГДА не станут готовы.

        Либо зависят от проваленного, либо образуют кольцо. Молча
        оставить их «открытыми» нельзя: прогон закончится словами
        «план не доделан» без объяснения причины.
        """
        rows = self.tasks(run_id)
        by_id = {t["id"]: t for t in rows}
        openish = {t["id"] for t in rows if t["status"] in ("open", "doing")}
        stuck = []
        for t in rows:
            if t["status"] not in ("open", "doing"):
                continue
            for dep in self._blockers(t, by_id):
                if dep["status"] in ("failed", "blocked"):
                    stuck.append(t)
                    break
            else:
                # кольцо: идём по цепочке зависимостей и ищем возврат
                seen, cur = set(), t
                while True:
                    deps = [d for d in self._blockers(cur, by_id)
                            if d["id"] in openish]
                    if not deps:
                        break
                    nxt = deps[0]
                    if nxt["id"] in seen or nxt["id"] == t["id"]:
                        stuck.append(t)
                        break
                    seen.add(nxt["id"])
                    cur = nxt
        return stuck

    def set_task_profile(self, task_id: int, profile: str) -> None:
        self.db.execute("UPDATE task SET profile=? WHERE id=?",
                        (profile[:40], task_id))
        self.db.commit()

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
        """Пункты прогона: подшаги идут сразу за своим родителем.

        Порядок по ord ставил подшаги в конец списка — план читался
        как «сначала всё крупное, потом непонятно чьи мелочи».
        """
        rows = [dict(r) for r in self.db.execute(
            "SELECT * FROM task WHERE run_id=? ORDER BY ord", (run_id,))]
        kids: dict[int, list[dict[str, Any]]] = {}
        for t in rows:
            if t["parent_id"]:
                kids.setdefault(t["parent_id"], []).append(t)
        if not kids:
            return rows
        out: list[dict[str, Any]] = []
        for t in rows:
            if t["parent_id"]:
                continue
            out.append(t)
            out.extend(kids.get(t["id"], []))
        # Осиротевшие подшаги (родителя убрали при перепланировании)
        # не теряем: иначе они исчезнут из плана молча.
        seen = {t["id"] for t in out}
        out.extend(t for t in rows if t["id"] not in seen)
        return out

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

    # ------------------------------------------------- гигиена памяти
    # Память, которая только копится, со временем начинает врать: старое
    # решение соседствует с новым, и recall выдаёт оба. Отсюда правка и
    # удаление фактов — операции такие же обычные, как запись.

    def get_fact(self, fact_id: int) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM fact WHERE id=?",
                            (fact_id,)).fetchone()
        return dict(r) if r else None

    def revise(self, fact_id: int, text: str = "", tags: str | None = None,
               confidence: float | None = None) -> dict[str, Any] | None:
        """Исправить факт на месте. Возвращает состояние ДО правки."""
        old = self.get_fact(fact_id)
        if old is None:
            return None
        new_text = text.strip() or old["text"]
        if new_text != old["text"]:
            # Уникальность текста: правка «в уже существующий» факт —
            # это на деле удаление дубля, а не ошибка.
            dup = self.db.execute("SELECT id FROM fact WHERE text=? AND id<>?",
                                  (new_text, fact_id)).fetchone()
            if dup:
                self.db.execute("DELETE FROM fact WHERE id=?", (fact_id,))
                self.db.commit()
                return old
        self.db.execute(
            "UPDATE fact SET text=?, tags=?, confidence=? WHERE id=?",
            (new_text,
             old["tags"] if tags is None else tags,
             old["confidence"] if confidence is None else confidence,
             fact_id))
        self.db.commit()
        return old

    def forget(self, fact_id: int = 0, query: str = "",
               limit: int = 50) -> list[dict[str, Any]]:
        """Удалить факт по номеру либо все найденные по запросу.

        Возвращает удалённое — чтобы вызывающий мог показать человеку,
        что именно исчезло. Пустой запрос НЕ чистит всю память: такая
        «оговорка» стоила бы всей истории работы.
        """
        if fact_id:
            row = self.get_fact(fact_id)
            if row is None:
                return []
            self.db.execute("DELETE FROM fact WHERE id=?", (fact_id,))
            self.db.commit()
            return [row]
        if not query.strip():
            raise ValueError("нужен номер факта или непустой запрос")
        rows = self.recall(query, limit)
        if rows:
            self.db.executemany("DELETE FROM fact WHERE id=?",
                                [(r["id"],) for r in rows])
            self.db.commit()
        return rows

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

    def runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """История прогонов, новые первыми. Данные копились с самого
        начала, но посмотреть их было нечем."""
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM run ORDER BY id DESC LIMIT ?", (limit,))]

    def run_events(self, run_id: int, limit: int = 200,
                   kinds: str = "") -> list[dict[str, Any]]:
        """Журнал прогона по порядку. kinds — виды через запятую."""
        sql = "SELECT * FROM event WHERE run_id=?"
        args: list[Any] = [run_id]
        if kinds.strip():
            names = [k.strip() for k in kinds.split(",") if k.strip()]
            sql += f" AND kind IN ({','.join('?' * len(names))})"
            args += names
        sql += " ORDER BY id LIMIT ?"
        args.append(limit)
        return [dict(r) for r in self.db.execute(sql, args)]

    # ------------------------------------------------------- дискуссия
    def start_debate(self, question: str, model_a: str = "",
                     model_b: str = "", model_arbiter: str = "") -> int:
        cur = self.db.execute(
            "INSERT INTO debate(question,model_a,model_b,model_arbiter,started)"
            " VALUES(?,?,?,?,?)",
            (question, model_a, model_b, model_arbiter, self._now()))
        self.db.commit()
        return int(cur.lastrowid)

    def add_turn(self, debate_id: int, role: str, text: str,
                 round_no: int = 0, model: str = "", sig: str = "",
                 tokens: int = 0, cost: float = 0.0,
                 branch_id: int | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO turn(debate_id,branch_id,round,role,model,text,sig,"
            "tokens,cost,created) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (debate_id, branch_id, round_no, role, model, text, sig,
             tokens, cost, self._now()))
        self.db.execute(
            "UPDATE debate SET rounds=MAX(rounds,?), cost=cost+?, "
            "tok_in=tok_in+?, tok_out=tok_out+? WHERE id=?",
            (round_no, cost, 0, tokens, debate_id))
        self.db.commit()
        return int(cur.lastrowid)

    def turns(self, debate_id: int, branch_id: int | None = None,
              role: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM turn WHERE debate_id=?"
        args: list[Any] = [debate_id]
        if branch_id is not None:
            sql += " AND branch_id IS ?"
            args.append(branch_id)
        if role:
            sql += " AND role=?"
            args.append(role)
        sql += " ORDER BY id"
        return [dict(r) for r in self.db.execute(sql, args)]

    def sig_repeats(self, debate_id: int, sig: str,
                    branch_id: int | None = None) -> int:
        """Сколько раз довод с такой подписью уже звучал."""
        if not sig:
            return 0
        sql = "SELECT COUNT(*) FROM turn WHERE debate_id=? AND sig=?"
        args: list[Any] = [debate_id, sig]
        if branch_id is not None:
            sql += " AND branch_id IS ?"
            args.append(branch_id)
        return int(self.db.execute(sql, args).fetchone()[0])

    def finish_debate(self, debate_id: int, status: str,
                      verdict: str = "") -> None:
        self.db.execute(
            "UPDATE debate SET status=?, verdict=?, finished=? WHERE id=?",
            (status, verdict[:4000], self._now(), debate_id))
        self.db.commit()

    def get_debate(self, debate_id: int) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM debate WHERE id=?",
                            (debate_id,)).fetchone()
        return dict(r) if r else None

    def debates(self, limit: int = 20) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM debate ORDER BY id DESC LIMIT ?", (limit,))]

    def add_branch(self, debate_id: int, assumption: str,
                   parent_id: int | None = None) -> int:
        cur = self.db.execute(
            "INSERT INTO branch(debate_id,parent_id,assumption,created) "
            "VALUES(?,?,?,?)",
            (debate_id, parent_id, assumption, self._now()))
        self.db.commit()
        return int(cur.lastrowid)

    def branches(self, debate_id: int) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM branch WHERE debate_id=? ORDER BY id",
            (debate_id,))]

    def finish_branch(self, branch_id: int, status: str,
                      verdict: str = "") -> None:
        self.db.execute(
            "UPDATE branch SET status=?, verdict=? WHERE id=?",
            (status, verdict[:2000], branch_id))
        self.db.commit()

    # ------------------------------------------------------- очередь
    def add_job(self, goal: str, profile: str = "", hours: float = 1.0,
                max_usd: float = 0.0, decompose: bool = True,
                notify: str = "", max_rounds: int = 20) -> int:
        cur = self.db.execute(
            "INSERT INTO job(goal,profile,hours,max_usd,decompose,notify,"
            "max_rounds,created) VALUES(?,?,?,?,?,?,?,?)",
            (goal.strip(), profile, hours, max_usd, int(decompose), notify,
             max(1, max_rounds), self._now()))
        self.db.commit()
        return int(cur.lastrowid)

    def take_job(self, worker: str) -> dict[str, Any] | None:
        """Взять задание в работу. Атомарно: две копии исполнителя не
        должны взять одно и то же.

        UPDATE ... WHERE status='queued' выполняется одной операцией,
        поэтому второй исполнитель получит rowcount=0 и пойдёт дальше.
        """
        # next_at: после неудачного подхода задание отдыхает. Иначе
        # исполнитель молотит его в цикле, сжигая деньги на одном и том
        # же месте.
        row = self.db.execute(
            "SELECT id FROM job WHERE status='queued' "
            "AND COALESCE(next_at,0) <= ? ORDER BY id LIMIT 1",
            (self._now(),)).fetchone()
        if not row:
            return None
        cur = self.db.execute(
            "UPDATE job SET status='running', worker=?, started=?, "
            "heartbeat=?, attempts=attempts+1 "
            "WHERE id=? AND status='queued'",
            (worker, self._now(), self._now(), row["id"]))
        self.db.commit()
        if cur.rowcount == 0:
            return None            # успел другой исполнитель
        return self.get_job(int(row["id"]))

    def beat_job(self, job_id: int, run_id: int | None = None) -> None:
        """Отметиться живым. По этой отметке видно зависшее задание."""
        if run_id:
            self.db.execute("UPDATE job SET heartbeat=?, run_id=? WHERE id=?",
                            (self._now(), run_id, job_id))
        else:
            self.db.execute("UPDATE job SET heartbeat=? WHERE id=?",
                            (self._now(), job_id))
        self.db.commit()

    def continue_job(self, job_id: int, result: str, progress: str,
                     delay: float = 0.0) -> bool:
        """Вернуть задание в очередь для СЛЕДУЮЩЕГО подхода.

        Кончилось время, исчерпан бюджет шага, агент забуксовал — работа
        не закончена, но и не провалена. Раньше такое помечалось
        «failed» и бросалось: «поставил и забыл» превращалось в «поставил
        и проверяй». Теперь задание продолжится с того же прогона.

        Возвращает False, если подходы исчерпаны.
        """
        row = self.get_job(job_id)
        if row is None:
            return False
        rounds = int(row["rounds"] or 0) + 1
        if rounds >= int(row["max_rounds"] or 20):
            self.db.execute(
                "UPDATE job SET status='failed', rounds=?, result=?, "
                "finished=? WHERE id=?",
                (rounds, f"{result}\n\nПодходы исчерпаны ({rounds}): "
                 "работа не доведена до конца.", self._now(), job_id))
            self.db.commit()
            return False
        self.db.execute(
            "UPDATE job SET status='queued', rounds=?, result=?, "
            "progress=?, next_at=?, worker='' WHERE id=?",
            (rounds, result, progress, self._now() + max(0.0, delay), job_id))
        self.db.commit()
        return True

    def finish_job(self, job_id: int, status: str, result: str = "") -> None:
        self.db.execute(
            "UPDATE job SET status=?, result=?, finished=? WHERE id=?",
            (status, result[:4000], self._now(), job_id))
        self.db.commit()

    def get_job(self, job_id: int) -> dict[str, Any] | None:
        r = self.db.execute("SELECT * FROM job WHERE id=?",
                            (job_id,)).fetchone()
        return dict(r) if r else None

    def jobs(self, limit: int = 30, status: str = "") -> list[dict[str, Any]]:
        sql = "SELECT * FROM job"
        args: list[Any] = []
        if status:
            sql += " WHERE status=?"
            args.append(status)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = [dict(r) for r in self.db.execute(sql, args)]
        # Человеку, поставившему задание и ушедшему, важно не «running»,
        # а ЧТО именно сейчас делается и кем. Без этого веб показывает
        # серую полоску часами.
        for row in rows:
            row["now"] = self.job_now(row) if row["run_id"] else {}
        return rows

    def job_now(self, job: dict[str, Any]) -> dict[str, Any]:
        """Что происходит по заданию прямо сейчас: шаг, агент, решения."""
        rid = job.get("run_id")
        if not rid:
            return {}
        tasks = self.tasks(rid)
        doing = next((t for t in tasks if t["status"] == "doing"), None)
        nxt = doing or next((t for t in tasks if t["status"] == "open"), None)
        last = self.db.execute(
            "SELECT name, summary, created FROM event WHERE run_id=? "
            "AND kind='orchestrate' ORDER BY id DESC LIMIT 1",
            (rid,)).fetchone()
        return {
            "step": (nxt or {}).get("title", ""),
            "agent": (nxt or {}).get("profile", ""),
            "doing": bool(doing),
            "done": sum(1 for t in tasks if t["status"] in ("done", "skipped")),
            "total": len(tasks),
            "decision": (f"{last['name']}: {last['summary']}"
                         if last else ""),
        }

    def stop_job(self, job_id: int) -> bool:
        """Снять задание. Работающее пометится и остановится на шаге."""
        cur = self.db.execute(
            "UPDATE job SET status='stopped', finished=? "
            "WHERE id=? AND status IN ('queued','running')",
            (self._now(), job_id))
        self.db.commit()
        return cur.rowcount > 0

    def revive_stale_jobs(self, older_than: float = 300.0) -> int:
        """Вернуть в очередь задания, чей исполнитель умер.

        Сервер перезагрузили или процесс убили — задание осталось
        «running» навсегда. Ждать вечно нельзя: по отсутствию отметки
        видно, что за ним никого нет.
        """
        edge = self._now() - older_than
        cur = self.db.execute(
            "UPDATE job SET status='queued', worker='' "
            "WHERE status='running' AND COALESCE(heartbeat,0) < ? "
            "AND attempts < 3", (edge,))
        # Трижды падавшее задание не гоняем по кругу: скорее всего,
        # дело в нём самом, а не в случайном сбое.
        self.db.execute(
            "UPDATE job SET status='failed', finished=?, "
            "result='исполнитель не отвечает, попытки исчерпаны' "
            "WHERE status='running' AND COALESCE(heartbeat,0) < ? "
            "AND attempts >= 3", (self._now(), edge))
        self.db.commit()
        return cur.rowcount

    def recent_events(self, run_id: int, limit: int = 12) -> list[dict[str, Any]]:
        return [dict(r) for r in self.db.execute(
            "SELECT * FROM event WHERE run_id=? ORDER BY id DESC LIMIT ?",
            (run_id, limit))][::-1]
