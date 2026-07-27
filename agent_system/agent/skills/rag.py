"""RAG: поиск по документам — обычный и по онтологии.

Две стратегии, и обе нужны:

  ОБЫЧНЫЙ    документы режутся на фрагменты, поиск по словам (FTS5) —
             отвечает на «где про это написано».

  ПО ОНТОЛОГИИ  сначала находим сущность в графе (ГОСТ, раздел, изделие),
             затем берём связанные с ней фрагменты — отвечает на
             «что связано с этим объектом», даже если нужных слов в
             самом фрагменте нет.

Второй способ ловит то, что обычный пропускает: фрагмент про «предельное
отклонение» не содержит слова «ГОСТ», но связан с ним через документ.

Честная граница: это лексический поиск, не семантический. Синонимы
ловятся стеммингом (см. Store.recall), но перефразирование смысла —
нет. Для этого нужны эмбеддинги; см. rag_status, он говорит об этом
прямо, а не создаёт видимость.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any

from ..store import Store
from ..tools.base import Tool, ToolError, Workspace
from .documents import classify, read_any, sections

CHUNK = 1200          # символов во фрагменте
OVERLAP = 200         # нахлёст, чтобы не рвать мысль на границе

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunk(
  id INTEGER PRIMARY KEY,
  doc TEXT NOT NULL,
  section TEXT DEFAULT '',
  ord INTEGER DEFAULT 0,
  text TEXT NOT NULL,
  created REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
  text, doc, section, content=chunk, content_rowid=id
);
CREATE TRIGGER IF NOT EXISTS chunk_ai AFTER INSERT ON chunk BEGIN
  INSERT INTO chunk_fts(rowid, text, doc, section)
    VALUES (new.id, new.text, new.doc, new.section);
END;
CREATE TRIGGER IF NOT EXISTS chunk_ad AFTER DELETE ON chunk BEGIN
  INSERT INTO chunk_fts(chunk_fts, rowid, text, doc, section)
    VALUES('delete', old.id, old.text, old.doc, old.section);
END;
CREATE INDEX IF NOT EXISTS ix_chunk_doc ON chunk(doc);
"""


def _split(text: str, size: int = CHUNK, overlap: int = OVERLAP) -> list[str]:
    """Резка по границам предложений, а не по символам вслепую."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    out, pos = [], 0
    while pos < len(text):
        end = min(pos + size, len(text))
        if end < len(text):
            # ищем конец предложения в последней трети куска
            window = text[pos + int(size * 0.6):end]
            m = list(re.finditer(r"[.!?]\s|\n\n", window))
            if m:
                end = pos + int(size * 0.6) + m[-1].end()
        chunk = text[pos:end].strip()
        if chunk:
            out.append(chunk)
        if end >= len(text):
            break
        pos = max(pos + 1, end - overlap)
    return out


def build(ws: Workspace, store: Store, run_id_getter=None) -> list[Tool]:
    store.db.executescript(SCHEMA)
    store.db.commit()

    def rid() -> int:
        return run_id_getter() if run_id_getter else 0

    # ────────────────────────── индексация ──────────────────────
    def rag_index(path: str, link_graph: bool = True) -> str:
        """Проиндексировать файл или папку."""
        p = ws.resolve(path)
        files = ([p] if p.is_file() else
                 sorted(q for q in p.rglob("*")
                        if q.is_file() and q.suffix.lower() in
                        (".pdf", ".docx", ".xlsx", ".csv", ".md", ".txt",
                         ".json", ".html")))
        if not files:
            raise ToolError(f"в {path!r} нет документов поддержанных форматов")

        total_chunks, done, skipped = 0, [], []
        for f in files:
            try:
                text, meta = read_any(f)
            except ToolError as exc:
                skipped.append(f"{f.name}: {exc}")
                continue
            if not text.strip():
                skipped.append(f"{f.name}: текст не извлечён")
                continue

            # переиндексация: старые фрагменты убираем
            store.db.execute("DELETE FROM chunk WHERE doc=?", (f.name,))
            n = 0
            for sec in (sections(text) or [{"title": "", "text": text}]):
                body = sec["text"] or sec["title"]
                for i, ch in enumerate(_split(body)):
                    store.db.execute(
                        "INSERT INTO chunk(doc,section,ord,text,created) "
                        "VALUES(?,?,?,?,strftime('%s','now'))",
                        (f.name, sec["title"][:120], i, ch))
                    n += 1
            store.db.commit()
            total_chunks += n
            done.append(f"{f.name}: {n}")

            if link_graph:
                cls, _ = classify(text)
                store.upsert_entity("document", f.name,
                                    {"class": cls, "chunks": n}, run_id=rid())
                for sec in sections(text):
                    if sec["title"]:
                        store.link(("document", f.name), "содержит_раздел",
                                   ("раздел", sec["title"][:80]), run_id=rid())

        lines = [f"Проиндексировано документов: {len(done)}, "
                 f"фрагментов: {total_chunks}"]
        lines += [f"  {d}" for d in done[:20]]
        if skipped:
            lines.append(f"Пропущено: {len(skipped)}")
            lines += [f"  {s}" for s in skipped[:5]]
        return "\n".join(lines)

    # ──────────────────────── обычный поиск ─────────────────────
    def _search(query: str, limit: int, doc: str = "") -> list[dict[str, Any]]:
        terms = [w for w in re.findall(r"\w+", query, re.U) if len(w) >= 3]
        if not terms:
            return []
        expr = " OR ".join(f"{Store._stem(t)}*" for t in terms)
        cond = " AND c.doc=?" if doc else ""
        args: list[Any] = [expr, max(limit * 10, 100)]
        if doc:
            args.append(doc)
        args.append(limit * 3)
        try:
            rows = store.db.execute(
                "WITH hit AS (SELECT rowid FROM chunk_fts WHERE chunk_fts "
                "MATCH ? ORDER BY rowid DESC LIMIT ?) "
                f"SELECT c.* FROM hit JOIN chunk c ON c.id=hit.rowid"
                f" WHERE 1=1{cond} LIMIT ?", args).fetchall()
        except sqlite3.OperationalError:
            return []
        stems = [Store._stem(t).lower() for t in terms]
        res = [dict(r) for r in rows]
        # выше те фрагменты, где совпало больше разных основ
        res.sort(key=lambda r: sum(s in r["text"].lower() for s in stems),
                 reverse=True)
        return res[:limit]

    def rag_search(query: str, limit: int = 5, doc: str = "") -> str:
        hits = _search(query, limit, doc)
        if not hits:
            n = store.db.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]
            if n == 0:
                raise ToolError("индекс пуст — сначала вызовите rag_index")
            return (f"По запросу {query!r} ничего не найдено "
                    f"(во фрагментах: {n}). Попробуйте другие слова.")
        out = [f"Найдено фрагментов: {len(hits)}", ""]
        for h in hits:
            head = h["doc"] + (f" › {h['section']}" if h["section"] else "")
            out.append(f"── {head}")
            out.append(h["text"][:900])
            out.append("")
        return "\n".join(out)

    # ─────────────────── поиск по онтологии ─────────────────────
    def rag_search_ontology(query: str, limit: int = 5) -> str:
        """Через граф: сущность → связанные документы → их фрагменты.

        Находит то, чего нет в тексте фрагмента дословно: если раздел
        связан с ГОСТом через документ, он найдётся по запросу «ГОСТ».
        """
        terms = [w for w in re.findall(r"[\w.\-/]+", query, re.U)
                 if len(w) >= 3]
        if not terms:
            raise ToolError("слишком короткий запрос")

        # 1) сущности графа, похожие на запрос
        like = " OR ".join(["name LIKE ?"] * len(terms))
        ents = store.db.execute(
            f"SELECT kind,name FROM entity WHERE {like} LIMIT 20",
            [f"%{t}%" for t in terms]).fetchall()
        if not ents:
            return (f"В графе нет объектов, похожих на {query!r}. "
                    "Проиндексируйте документы (rag_index) или используйте "
                    "обычный rag_search.")

        # 2) документы, связанные с этими сущностями
        docs: dict[str, list[str]] = {}
        for e in ents:
            for nb in store.neighbours(e["kind"], e["name"]):
                if nb["kind"] == "document":
                    docs.setdefault(nb["name"], []).append(
                        f"{e['kind']}:{e['name']}")
            if e["kind"] == "document":
                docs.setdefault(e["name"], []).append("прямое совпадение")

        if not docs:
            return (f"Объекты найдены ({len(ents)}), но связей с документами "
                    "нет. Проиндексируйте с link_graph=true.")

        out = [f"Через граф найдено объектов: {len(ents)}, "
               f"документов: {len(docs)}", ""]
        for dname, why in list(docs.items())[:limit]:
            out.append(f"── {dname}  ← {', '.join(sorted(set(why))[:3])}")
            frag = _search(query, 2, dname) or store.db.execute(
                "SELECT * FROM chunk WHERE doc=? ORDER BY id LIMIT 2",
                (dname,)).fetchall()
            for f in frag:
                f = dict(f)
                sec = f" › {f['section']}" if f["section"] else ""
                out.append(f"   {sec}".rstrip())
                out.append("   " + f["text"][:500].replace("\n", "\n   "))
            out.append("")
        return "\n".join(out)

    def rag_answer(query: str, limit: int = 4) -> str:
        """Собрать контекст для ответа: фрагменты + факты + связи."""
        parts: list[str] = []
        hits = _search(query, limit)
        if hits:
            parts.append("ФРАГМЕНТЫ ДОКУМЕНТОВ:")
            for h in hits:
                head = h["doc"] + (f" › {h['section']}" if h["section"] else "")
                parts.append(f"[{head}]\n{h['text'][:700]}")
        facts = store.recall(query, 5)
        if facts:
            parts.append("\nИЗ ПАМЯТИ:")
            parts += [f"- {f['text']}" for f in facts]
        if not parts:
            return ("Ничего не найдено ни в документах, ни в памяти. "
                    "Отвечать не на чем — так и скажите пользователю.")
        parts.append("\nОтвечай ТОЛЬКО по этим данным. Ссылайся на документ "
                     "и раздел. Если данных не хватает — скажи прямо.")
        return "\n\n".join(parts)

    def rag_status() -> str:
        n = store.db.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]
        docs = store.db.execute(
            "SELECT doc, COUNT(*) c FROM chunk GROUP BY doc "
            "ORDER BY c DESC").fetchall()
        e, r = store.graph_stats()
        out = [f"Фрагментов: {n}, документов: {len(docs)}",
               f"Граф: {e} объектов, {r} связей",
               f"Память: {store.fact_count()} фактов", ""]
        out += [f"  {d['doc']}: {d['c']}" for d in docs[:15]]
        out += ["", "Поиск лексический (слова и их основы). Перефразированный "
                    "смысл без общих слов он не найдёт — для этого нужны "
                    "эмбеддинги."]
        return "\n".join(out)

    return [
        Tool("rag_index",
             "Проиндексировать документ или папку для поиска: режет на "
             "фрагменты и заносит документы с разделами в граф знаний.",
             {"type": "object",
              "properties": {"path": {"type": "string"},
                             "link_graph": {"type": "boolean"}},
              "required": ["path"]},
             rag_index),
        Tool("rag_search",
             "Найти фрагменты документов по словам запроса. Обычный поиск.",
             {"type": "object",
              "properties": {"query": {"type": "string"},
                             "limit": {"type": "integer"},
                             "doc": {"type": "string",
                                     "description": "искать в одном документе"}},
              "required": ["query"]},
             rag_search),
        Tool("rag_search_ontology",
             "Поиск ЧЕРЕЗ ГРАФ: находит объект (ГОСТ, раздел, изделие), "
             "затем связанные с ним документы. Находит то, чего нет во "
             "фрагменте дословно.",
             {"type": "object",
              "properties": {"query": {"type": "string"},
                             "limit": {"type": "integer"}},
              "required": ["query"]},
             rag_search_ontology),
        Tool("rag_answer",
             "Собрать контекст для ответа на вопрос: фрагменты документов "
             "плюс факты из памяти, с указанием источников.",
             {"type": "object",
              "properties": {"query": {"type": "string"},
                             "limit": {"type": "integer"}},
              "required": ["query"]},
             rag_answer),
        Tool("rag_status",
             "Что проиндексировано: документы, фрагменты, объём графа.",
             {"type": "object", "properties": {}, "required": []},
             rag_status),
    ]
