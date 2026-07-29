"""Тесты навыка rag: чанкинг, обычный RAG и RAG на онтологии, на ДВУХ
бэкендах — SQLite (agent/store.py, всегда доступен) и PostgreSQL+pgvector
(store_pg.py, опционален).

Философия та же, что у остального набора: тест обязан уметь падать.
Рядом с позитивными сценариями — негативные: пустой запрос, отсутствующий
файл, битый entity_refs, переиндексация не плодит дубли, поиск без
проиндексированных данных не роняет вызов.

SQLite-часть работает на голом Python (HashEmbedder — офлайн-эмбеддер без
сети и ключа), поэтому не требует ничего, кроме самого проекта — как и
ядро системы. Postgres-часть — на НАСТОЯЩЕМ embedded-сервере через
pgserver, с graceful skip, если психкопг/pgserver недоступны.
"""
from __future__ import annotations

import re
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.llm.embeddings import HashEmbedder            # noqa: E402
from agent.store import Store                             # noqa: E402
from agent.tools.base import ToolError, Workspace          # noqa: E402
from agent.skills import rag                               # noqa: E402

PASS, FAIL = 0, 0

HAVE_PG = True
PG_SKIP_REASON = ""
try:
    import psycopg  # type: ignore
except ImportError:
    HAVE_PG = False
    PG_SKIP_REASON = "psycopg не установлен (pip install \"psycopg[binary]\")"

_srv = None
if HAVE_PG:
    try:
        import pgserver  # type: ignore
        _tmp_pgdata = tempfile.mkdtemp(prefix="pgserver_rag_")
        _srv = pgserver.get_server(_tmp_pgdata)
    except Exception as exc:  # платформа без embedded Postgres и т.п.
        HAVE_PG = False
        PG_SKIP_REASON = f"не удалось поднять тестовый Postgres: {exc}"


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


SAMPLE_TEXT = (
    "Планетарный редуктор состоит из солнечной шестерни, сателлитов и "
    "водила.\n\n"
    "Корпус AB-01 изготовлен из стали 40Х, масса 3.2 кг, применяется "
    "в редукторе основного привода.\n\n"
    "Крышка AB-02 крепится восемью болтами М6 к корпусу.\n\n"
    "Зазор между щекой водила и сателлитом должен быть не менее 0.1 мм "
    "по результатам расчёта."
)


# ================================================================ chunking
def test_chunk_text_basic() -> None:
    section("chunk_text: базовые сценарии")
    check("пустой текст -> пустой список", rag.chunk_text("", 100, 10) == [])
    check("короткий текст -> один фрагмент",
          rag.chunk_text("короткий текст", 100, 10) == ["короткий текст"])

    chunks = rag.chunk_text(SAMPLE_TEXT, 150, 30)
    check("текст разбит на несколько фрагментов", len(chunks) > 1, str(len(chunks)))
    check("каждый фрагмент не длиннее лимита (с учётом жёсткой резки)",
          all(len(c) <= 150 for c in chunks), str([len(c) for c in chunks]))
    check("абзацы не потеряны — всё содержимое покрыто",
          all(part in "".join(chunks) for part in
              ["AB-01", "AB-02", "Зазор между щекой"]))


def test_chunk_text_edge_cases() -> None:
    section("chunk_text: граничные случаи")
    check("overlap >= size не роняет вызов, а исправляется",
          len(rag.chunk_text("A" * 500, 100, 900)) > 0)
    check("chunk_size<=0 отдаёт текст целиком",
          rag.chunk_text("любой текст", 0, 0) == ["любой текст"])
    long_paragraph = "Б" * 1000  # один абзац без пустых строк
    chunks = rag.chunk_text(long_paragraph, 200, 20)
    check("длинный абзац без переносов режется жёстко",
          len(chunks) > 1 and all(len(c) <= 200 for c in chunks))
    # НЕГАТИВНЫЙ: два фрагмента должны пересекаться на overlap символов
    if len(chunks) >= 2:
        check("между фрагментами есть заявленное перекрытие",
              chunks[0][-15:] in chunks[1] or chunks[1].startswith(chunks[0][-20:]),
              f"{chunks[0][-25:]!r} / {chunks[1][:25]!r}")


# ============================================================ SQLite backend
def _sqlite_setup(tmpdir: Path):
    ws = Workspace(tmpdir / "ws")
    store = Store(str(tmpdir / "t.db"))
    embedder = HashEmbedder(dim=64)
    backend = rag._SQLiteBackend(store, lambda: 0)
    tools = {t.name: t for t in rag.build(ws, embedder, backend,
                                          chunk_size=200, chunk_overlap=30)}
    return ws, store, tools


def test_sqlite_index_and_query() -> None:
    section("SQLite: индексация и обычный RAG-поиск")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _sqlite_setup(Path(td))

        out = tools["rag_index"].fn(text=SAMPLE_TEXT, source="doc1.md",
                                    entity_refs="part:AB-01,part:AB-02")
        check("индексация отчиталась о фрагментах", "фрагмент" in out, out)
        check("индексация отчиталась о привязке к объектам",
              "объект" in out, out)

        stats = tools["rag_stats"].fn()
        check("статистика видит источник", "doc1.md" in stats, stats)

        found = tools["rag_query"].fn(query="зазор между щекой водила")
        check("запрос находит релевантный фрагмент", "Зазор между щекой" in found,
              found)
        check("в ответе указан источник", "источник=doc1.md" in found, found)
        check("есть инструкция не додумывать",
              "не додумывай" in found or "указывай источник" in found)


def test_sqlite_reindex_replaces_not_duplicates() -> None:
    section("SQLite: переиндексация заменяет, а не копит дубли")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _sqlite_setup(Path(td))
        tools["rag_index"].fn(text="Версия документа номер один.", source="doc.md")
        n1 = store.chunk_count("doc.md")
        check("первая версия дала 1 фрагмент", n1 == 1, str(n1))
        tools["rag_index"].fn(text="Совсем другая версия документа номер два, "
                                   "гораздо длиннее прежней.", source="doc.md")
        n2 = store.chunk_count("doc.md")
        result = tools["rag_query"].fn(query="номер один версия")
        check("старая версия не осталась в базе (текст фрагмента не найден)",
              "Версия документа номер один." not in result, result)
        check("новая версия проиндексирована, не накоплена поверх старой",
              n2 == 1, str(n2))


def test_sqlite_entity_rag() -> None:
    section("SQLite: RAG на онтологии (entity_refs)")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _sqlite_setup(Path(td))
        tools["rag_index"].fn(text=SAMPLE_TEXT, source="doc1.md",
                              entity_refs="part:AB-01,part:AB-02")
        tools["rag_index"].fn(
            text="Совершенно не связанный текст про кулинарию и рецепты.",
            source="doc2.md")

        out = tools["rag_query_entity"].fn(kind="part", name="AB-01")
        check("найдены фрагменты привязанного источника", "AB-01" in out, out)
        check("посторонний документ не попал в выдачу",
              "кулинарию" not in out, out)

        out_none = tools["rag_query_entity"].fn(kind="part", name="НЕТ_ТАКОГО")
        check("для непривязанного объекта — понятное сообщение",
              "не найдено" in out_none, out_none)


def test_sqlite_negative_cases() -> None:
    section("SQLite: негативные проверки")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _sqlite_setup(Path(td))

        try:
            tools["rag_index"].fn()
            check("отказ без text и path", False)
        except ToolError:
            check("отказ без text и path", True)

        try:
            tools["rag_index"].fn(path="nope.md")
            check("отказ на отсутствующий файл", False)
        except ToolError:
            check("отказ на отсутствующий файл", True)

        try:
            tools["rag_index"].fn(text="текст", entity_refs="без_двоеточия")
            check("отказ на некорректный entity_refs", False)
        except ToolError:
            check("отказ на некорректный entity_refs", True)

        try:
            tools["rag_query"].fn(query="")
            check("отказ на пустой запрос", False)
        except ToolError:
            check("отказ на пустой запрос", True)

        empty_result = tools["rag_query"].fn(query="что угодно")
        check("поиск по пустому индексу не роняет вызов",
              "не найдено" in empty_result or "ничего не найдено" in empty_result,
              empty_result)

        big_text = "слово " * 2_000_001
        try:
            tools["rag_index"].fn(text=big_text[: rag.MAX_INDEX_CHARS + 100])
            check("отказ на слишком большой источник", False)
        except ToolError:
            check("отказ на слишком большой источник", True)


def test_sqlite_source_filter() -> None:
    section("SQLite: фильтр по source в rag_query")
    with tempfile.TemporaryDirectory() as td:
        ws, store, tools = _sqlite_setup(Path(td))
        tools["rag_index"].fn(text="Уникальный текст А про шестерни.", source="a.md")
        tools["rag_index"].fn(text="Уникальный текст Б про шестерни.", source="b.md")

        out = tools["rag_query"].fn(query="шестерни", source="a.md")
        check("фильтр по source ограничивает выдачу",
              "a.md" in out and "b.md" not in out, out)


def test_build_agent_with_rag_skill_sqlite() -> None:
    section("Сборка агента с навыком rag (SQLite-бэкенд по умолчанию)")
    from agent.build import build_agent
    from agent.config import Config
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="m", workspace=td,
                    skills=["files", "rag"], embedding_provider="hash")
        agent = build_agent(cfg)
        names = agent.tools.names()
        for t in ("rag_index", "rag_query", "rag_query_entity", "rag_stats"):
            check(f"инструмент {t} зарегистрирован", t in names)


def test_example_config_loads() -> None:
    section("examples/config.rag.json грузится без ошибок")
    from agent.config import Config
    root = Path(__file__).resolve().parents[1]
    cfg = Config.load(str(root / "examples" / "config.rag.json"))
    check("навык rag указан", "rag" in cfg.skills)
    check("rag_chunk_size разобран", cfg.rag_chunk_size == 1200)
    check("rag_top_k разобран", cfg.rag_top_k == 6)
    check("комментарные ключи не попали в поля", not hasattr(cfg, "_pg_dsn_пример"))


# ================================================================ Pg backend
def test_pg_index_and_query() -> None:
    section("PostgreSQL+pgvector: индексация и поиск (реальный сервер)")
    from agent.tools.base import Workspace
    with tempfile.TemporaryDirectory() as td:
        ws = Workspace(Path(td) / "ws")
        embedder = HashEmbedder(dim=64)
        dsn = _fresh_dsn()
        backend = rag._PgBackend(dsn, lambda: 64)
        tools = {t.name: t for t in rag.build(ws, embedder, backend)}

        out = tools["rag_index"].fn(text=SAMPLE_TEXT, source="doc.md",
                                    entity_refs="part:AB-01")
        check("индексация в Postgres отработала", "фрагмент" in out, out)

        found = tools["rag_query"].fn(query="зазор водила")
        check("поиск в Postgres находит фрагмент", "Зазор между щекой" in found,
              found)

        entity_out = tools["rag_query_entity"].fn(kind="part", name="AB-01")
        check("RAG на онтологии работает в Postgres", "AB-01" in entity_out,
              entity_out)


def test_pg_lazy_connection() -> None:
    section("PostgreSQL: подключение ленивое (build не падает при недоступном DSN)")
    from agent.build import build_agent
    from agent.config import Config
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="m", workspace=td, skills=["rag"],
                    embedding_provider="hash",
                    pg_dsn="postgresql://nonexistent-host-xyz/db")
        agent = build_agent(cfg)  # не должно бросить исключение
        check("агент собран несмотря на недоступный DSN",
              "rag_stats" in agent.tools.names())
        try:
            agent.tools.get("rag_stats").fn()
            check("реальный вызов к недоступному DSN даёт ошибку", False)
        except ToolError:
            check("реальный вызов к недоступному DSN даёт ошибку", True)


def test_build_agent_with_rag_skill_pg() -> None:
    section("Сборка агента с навыком rag (PostgreSQL-бэкенд по pg_dsn)")
    from agent.build import build_agent
    from agent.config import Config
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="m", workspace=td, skills=["rag"],
                    embedding_provider="hash", pg_dsn=_fresh_dsn())
        agent = build_agent(cfg)
        out = agent.tools.get("rag_index").fn(text=SAMPLE_TEXT, source="doc.md")
        check("индексация через build_agent с Postgres работает",
              "фрагмент" in out, out)
        found = agent.tools.get("rag_query").fn(query="зазор")
        check("поиск через build_agent с Postgres работает",
              "Зазор" in found, found)


def main() -> int:
    test_chunk_text_basic()
    test_chunk_text_edge_cases()
    test_sqlite_index_and_query()
    test_sqlite_reindex_replaces_not_duplicates()
    test_sqlite_entity_rag()
    test_sqlite_negative_cases()
    test_sqlite_source_filter()
    test_build_agent_with_rag_skill_sqlite()
    test_example_config_loads()

    if HAVE_PG:
        try:
            test_pg_index_and_query()
            test_pg_lazy_connection()
            test_build_agent_with_rag_skill_pg()
        finally:
            _srv.cleanup()
    else:
        print(f"\nPostgreSQL-часть пропущена — {PG_SKIP_REASON}")

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
