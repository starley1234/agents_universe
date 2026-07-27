"""Поиск по смыслу в памяти агента: инструменты поверх vectors.py.

Дополняет recall, а не заменяет его. Полнотекстовый поиск точнее, когда
слова совпадают; смысловой находит то, что названо иначе. Поэтому
smart_recall делает оба и объединяет выдачу, помечая, откуда что взято.

Служба векторов недоступна — не беда: инструменты честно говорят об
этом и предлагают обычный recall, а не возвращают пустоту, которую
модель примет за «ничего не найдено».
"""
from __future__ import annotations

from typing import Callable

from ..store import Store
from ..vectors import SOFT_LIMIT, Embedder, VectorStore
from .base import Tool, ToolError

#: за раз отправляем в службу векторов не больше — иначе таймаут
BATCH = 32


def build(store: Store, embedder: Embedder,
          run_id_getter: Callable[[], int] | None = None) -> list[Tool]:
    vs = VectorStore(store.db)

    def _need_service() -> str | None:
        if embedder.ready:
            return None
        return ("Служба векторов не настроена (embed_url и embed_model в "
                "конфиге). Смысловой поиск недоступен — пользуйся recall.")

    def index_memory(limit: int = 500, rebuild: bool = False) -> str:
        """Построить векторы для фактов, у которых их ещё нет."""
        miss = _need_service()
        if miss:
            raise ToolError(miss)
        if rebuild:
            vs.drop("fact")
        rows = store.db.execute(
            "SELECT f.id, f.text FROM fact f "
            "LEFT JOIN vec v ON v.ref_kind='fact' AND v.ref_id=f.id "
            "WHERE v.id IS NULL ORDER BY f.id DESC LIMIT ?",
            (max(1, min(limit, 5000)),)).fetchall()
        if not rows:
            st = vs.stats()
            return (f"Все факты уже проиндексированы "
                    f"(векторов: {st['count']})")

        done, failed = 0, ""
        for i in range(0, len(rows), BATCH):
            part = rows[i:i + BATCH]
            vecs = embedder.embed([r["text"] for r in part])
            if vecs is None:
                failed = embedder.error
                break
            for r, v in zip(part, vecs):
                vs.add("fact", int(r["id"]), r["text"], v)
                done += 1

        st = vs.stats()
        head = f"Проиндексировано фактов: {done}, всего векторов: {st['count']}"
        if failed:
            head += (f"\nОстановлено на полпути: {failed}. "
                     "Повтори позже — уже сделанное сохранено.")
        if st["over_limit"]:
            head += (f"\nВНИМАНИЕ: записей больше {SOFT_LIMIT:,}. "
                     f"Поиск станет заметно медленнее ({st['bytes'] // 1024} КБ "
                     "в памяти на каждый поиск). Пора переходить на "
                     "PostgreSQL с pgvector (навык pg).")
        return head

    def semantic_recall(query: str, limit: int = 10,
                        min_score: float = 0.25) -> str:
        """Найти по смыслу, а не по совпадению слов."""
        miss = _need_service()
        if miss:
            raise ToolError(miss)
        if not query.strip():
            raise ToolError("Пустой запрос")
        if vs.count() == 0:
            return ("Векторов ещё нет — сначала вызови index_memory. "
                    "Пока пользуйся обычным recall.")
        qv = embedder.embed([query])
        if qv is None:
            raise ToolError(f"Не получить вектор запроса: {embedder.error}")
        rows = vs.search(qv[0], limit=limit, ref_kind="fact",
                         min_score=min_score)
        if not rows:
            return (f"По смыслу ничего не найдено (порог {min_score}). "
                    "Попробуй recall — он ищет по словам.")
        out = [f"- #{r['ref_id']} {r['text']}  (близость {r['score']})"
               for r in rows]
        return "\n".join(out)

    def smart_recall(query: str, limit: int = 10) -> str:
        """Оба поиска сразу: по словам и по смыслу.

        Порядок важен: точные совпадения выше, смысловые дополняют.
        Источник помечается, чтобы агент понимал, насколько доверять.
        """
        if not query.strip():
            raise ToolError("Пустой запрос")
        exact = store.recall(query, limit)
        seen = {r["id"] for r in exact}
        out = [f"- #{r['id']} {r['text']}  [по словам]" for r in exact]

        if embedder.ready and vs.count():
            qv = embedder.embed([query])
            if qv is not None:
                for r in vs.search(qv[0], limit=limit, ref_kind="fact",
                                   min_score=0.25):
                    if r["ref_id"] in seen:
                        continue
                    seen.add(r["ref_id"])
                    out.append(f"- #{r['ref_id']} {r['text']}  "
                               f"[по смыслу, {r['score']}]")
        if not out:
            return f"По запросу {query!r} ничего не найдено ни одним способом"
        note = ""
        if not embedder.ready:
            note = "\n(смысловой поиск не настроен — искал только по словам)"
        return "\n".join(out[:limit * 2]) + note

    def vector_status() -> str:
        st = vs.stats()
        lines = [
            f"векторов        : {st['count']:,}",
            f"размерность     : {st['dim'] or '—'}",
            f"память на поиск : {st['bytes'] // 1024} КБ",
            f"служба          : {'настроена' if embedder.ready else 'НЕ настроена'}",
        ]
        if embedder.error:
            lines.append(f"последняя ошибка: {embedder.error}")
        if st["by_kind"]:
            lines.append("по видам        : " + ", ".join(
                f"{k}: {v}" for k, v in st["by_kind"].items()))
        if st["over_limit"]:
            lines.append(f"ВНИМАНИЕ: больше {SOFT_LIMIT:,} записей — "
                         "пора на PostgreSQL (навык pg)")
        return "Смысловой поиск\n" + "\n".join(lines)

    return [
        Tool("index_memory",
             "Построить векторы для фактов памяти, чтобы работал поиск по "
             "смыслу. Делай после того, как накопилось что искать.",
             {"type": "object",
              "properties": {
                  "limit": {"type": "integer",
                            "description": "Сколько фактов за раз"},
                  "rebuild": {"type": "boolean",
                              "description": "Перестроить всё заново"}},
              "required": []},
             index_memory),
        Tool("semantic_recall",
             "Найти в памяти ПО СМЫСЛУ: «как закрепить деталь» найдёт "
             "«крепление узла болтами». Слова могут не совпадать.",
             {"type": "object",
              "properties": {
                  "query": {"type": "string"},
                  "limit": {"type": "integer"},
                  "min_score": {"type": "number",
                                "description": "Порог близости 0..1"}},
              "required": ["query"]},
             semantic_recall),
        Tool("smart_recall",
             "Поиск в памяти сразу двумя способами: по словам и по "
             "смыслу. Лучший выбор, когда не уверен в формулировке.",
             {"type": "object",
              "properties": {"query": {"type": "string"},
                             "limit": {"type": "integer"}},
              "required": ["query"]},
             smart_recall),
        Tool("vector_status",
             "Состояние смыслового поиска: сколько векторов, сколько "
             "памяти, настроена ли служба.",
             {"type": "object", "properties": {}, "required": []},
             vector_status),
    ]
