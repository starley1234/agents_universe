"""Тесты maos.skills.rag: индексация, гибридный поиск (векторный + FTS),
RAG на онтологии. Реальный embedded Postgres+pgvector (pgserver).
"""
from __future__ import annotations

import re
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from maos.llm.embeddings import HashEmbedder                       # noqa: E402
from maos.skills.rag import chunk_text                              # noqa: E402
from maos.tools.base import ToolError, Workspace                    # noqa: E402

PASS, FAIL = 0, 0

HAVE_DEPS = True
SKIP_REASON = ""
try:
    import psycopg  # type: ignore
    _ = psycopg.__name__
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = "psycopg не установлен"

if HAVE_DEPS:
    try:
        import pgserver  # type: ignore
        _tmp = tempfile.mkdtemp(prefix="maos_rag_pgserver_")
        _srv = pgserver.get_server(_tmp)
    except Exception as exc:
        HAVE_DEPS = False
        SKIP_REASON = f"не удалось поднять тестовый Postgres: {exc}"


def _fresh_dsn() -> str:
    name = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(_srv.get_uri(), autocommit=True)
    try:
        admin.execute(f"CREATE DATABASE {name}")
    finally:
        admin.close()
    return re.sub(r"/postgres(\?|$)", f"/{name}\\1", _srv.get_uri())


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}" + (f" — {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n{title}\n" + "─" * len(title))


def main() -> int:
    section("chunk_text: разбиение по абзацам с перекрытием")
    text = "Абзац один.\n\nАбзац два.\n\nАбзац три."
    chunks = chunk_text(text, chunk_size=1000, chunk_overlap=50)
    check("короткий текст — один фрагмент", len(chunks) == 1)
    check("пустой текст -> пустой список", chunk_text("", 100, 10) == [])
    long_text = "Слово. " * 500
    long_chunks = chunk_text(long_text, chunk_size=200, chunk_overlap=30)
    check("длинный текст режется на несколько фрагментов", len(long_chunks) > 1)
    check("каждый фрагмент не длиннее лимита + запас",
         all(len(c) <= 200 + 5 for c in long_chunks))

    if not HAVE_DEPS:
        print(f"\ntest_rag: тесты с реальным Postgres пропущены — {SKIP_REASON}")
        print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
        return 1 if FAIL else 0

    from maos.memory.store import Store
    from maos.skills import rag as rag_mod

    st = Store(_fresh_dsn(), dim=64)
    emb = HashEmbedder(dim=64)
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(td)
        tools = {t.name: t for t in rag_mod.build(ws, st, emb, chunk_size=500)}

        section("rag_index + rag_query: инлайн-текст")
        out = tools["rag_index"].fn(
            text="Python — язык программирования общего назначения. "
                "Он широко используется в анализе данных и веб-разработке.",
            source="python_doc")
        check("индексация прошла", "Проиндексировано" in out)
        check("chunk реально в базе", st.chunk_count() >= 1)

        found = tools["rag_query"].fn(query="язык программирования")
        check("запрос находит проиндексированный фрагмент",
             "python_doc" in found and "Python" in found, found)

        section("rag_index: файл из workspace")
        (Path(td) / "doc.md").write_text(
            "# Заголовок\n\nОписание редуктора модели AB-01.", encoding="utf-8")
        out2 = tools["rag_index"].fn(path="doc.md")
        check("индексация файла указывает путь как источник", "doc.md" in out2, out2)

        section("rag_index: повторная индексация ЗАМЕНЯЕТ старые фрагменты")
        before = st.chunk_count()
        tools["rag_index"].fn(text="Совсем другой текст.", source="python_doc")
        after = st.chunk_count()
        check("повторная индексация того же source не плодит дубли",
             after <= before + 1, f"before={before} after={after}")
        found2 = tools["rag_query"].fn(query="Python")
        check("старый текст источника python_doc больше не находится",
             "широко используется" not in found2)

        section("rag_query: пустой запрос -> ToolError")
        try:
            tools["rag_query"].fn(query="")
            check("пустой запрос отклонён", False)
        except ToolError:
            check("пустой запрос отклонён", True)

        section("rag_query: слабое совпадение — результат всё равно ранжирован по сходству")
        # semantic_search_chunks не фильтрует по порогу — всегда возвращает
        # top-k из того, что проиндексировано, просто с низким сходством.
        # Честное "ничего не найдено" бывает только когда индекс пуст —
        # проверяем это на СВЕЖЕЙ базе без единого проиндексированного chunk.
        st_empty = Store(_fresh_dsn(), dim=64)
        tools_empty = {t.name: t for t in rag_mod.build(ws, st_empty, emb, chunk_size=500)}
        empty_result = tools_empty["rag_query"].fn(query="что угодно")
        check("пустой индекс -> честное сообщение",
             "ничего не найдено" in empty_result.lower(), empty_result)
        st_empty.close()

        section("rag_index: entity_refs привязывает фрагмент к объекту онтологии")
        tools["rag_index"].fn(
            text="Редуктор AB-01 имеет передаточное число 1:20.",
            source="spec.md", entity_refs="part:AB-01")
        by_entity = tools["rag_query_entity"].fn(kind="part", name="AB-01")
        check("фрагмент, привязанный к part:AB-01, находится",
             "передаточное число" in by_entity, by_entity)

        no_entity = tools["rag_query_entity"].fn(kind="part", name="НЕСУЩЕСТВУЮЩИЙ")
        check("запрос по несуществующему объекту — честное сообщение",
             "не найдено" in no_entity.lower(), no_entity)

        section("rag_stats: сводка отражает реальное состояние индекса")
        stats = tools["rag_stats"].fn()
        check("статистика упоминает число фрагментов", "фрагмент" in stats)
        check("статистика перечисляет источники",
             "python_doc" in stats or "spec.md" in stats, stats)

        section("rag_index: невалидный entity_refs -> ToolError")
        try:
            tools["rag_index"].fn(text="текст", source="bad", entity_refs="без_двоеточия")
            check("entity_refs без ':' отклонён", False)
        except ToolError:
            check("entity_refs без ':' отклонён", True)

    st.close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
