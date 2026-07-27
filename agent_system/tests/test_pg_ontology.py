"""Тесты навыка pg_ontology: онтология и семантический поиск в
PostgreSQL + pgvector.

Проверяется на НАСТОЯЩЕМ Postgres (embedded-сервер через pgserver —
временный кластер поднимается один раз на весь модуль и уничтожается
после), а не на моках уровня SQL — иначе тест проверял бы сам себя и не
поймал бы, например, ошибку в SQL-запросе к pgvector.

Каждая тестовая функция получает СВОЮ базу данных внутри общего кластера
(CREATE DATABASE) — так тесты не видят чужие сущности и не спорят из-за
несовпадающей размерности вектора между разными эмбеддерами.

Требует psycopg и pgserver. Если их нет или не удалось поднять сервер —
модуль пропускается с понятным сообщением, остальной набор (make test)
от Postgres не зависит.
"""
from __future__ import annotations

import re
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.llm.embeddings import HashEmbedder                  # noqa: E402
from agent.tools.base import ToolError                          # noqa: E402

PASS, FAIL = 0, 0

HAVE_DEPS = True
SKIP_REASON = ""
try:
    import psycopg  # type: ignore
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = "psycopg не установлен (pip install \"psycopg[binary]\")"

_srv = None
if HAVE_DEPS:
    try:
        import pgserver  # type: ignore
        _tmp_pgdata = tempfile.mkdtemp(prefix="pgserver_")
        _srv = pgserver.get_server(_tmp_pgdata)
    except Exception as exc:  # платформа без embedded Postgres и т.п.
        HAVE_DEPS = False
        SKIP_REASON = f"не удалось поднять тестовый Postgres: {exc}"


def _fresh_dsn() -> str:
    """Новая пустая база в общем тестовом кластере — для изоляции тестов."""
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


def _import_pg():
    from agent.skills import pg_ontology
    from agent.store_pg import PgError, PgStore
    return pg_ontology, PgError, PgStore


# ================================================================ tests
def test_store_pg_basic() -> None:
    section("PgStore напрямую: сущности, связи, обход графа")
    _, PgError, PgStore = _import_pg()
    store = PgStore(_fresh_dsn(), dim=4)
    try:
        store.upsert_entity("part", "AB-01", {"material": "steel"},
                            embedding=[1, 0, 0, 0])
        store.upsert_entity("part", "AB-02", embedding=[0, 1, 0, 0])
        store.upsert_entity("assembly", "Редуктор", embedding=[0, 0, 1, 0])
        created = store.link(("part", "AB-01"), "входит_в", ("assembly", "Редуктор"))
        check("связь создана", created is True)
        dup = store.link(("part", "AB-01"), "входит_в", ("assembly", "Редуктор"))
        check("повторная связь не дублируется", dup is False)

        e, r = store.graph_stats()
        check("3 объекта в графе", e == 3, str(e))
        check("1 связь в графе", r == 1, str(r))

        neigh = store.neighbours("part", "AB-01")
        check("сосед найден", len(neigh) == 1 and neigh[0]["name"] == "Редуктор")

        # НЕГАТИВНЫЙ: несуществующий объект — пустой список, не исключение
        check("несуществующий объект даёт пустой список соседей",
              store.neighbours("part", "НЕТ_ТАКОГО") == [])

        sub = store.subgraph("assembly", "Редуктор", depth=2)
        check("обход графа находит связь", any(
            e["subject"] == ["part", "AB-01"] for e in sub))
    finally:
        store.close()


def test_semantic_search_orders_by_similarity() -> None:
    section("Семантический поиск в pgvector: порядок по сходству")
    _, _, PgStore = _import_pg()
    store = PgStore(_fresh_dsn(), dim=4)
    try:
        store.upsert_entity("part", "близкий", embedding=[1, 0, 0, 0])
        store.upsert_entity("part", "далёкий", embedding=[0, 0, 0, 1])
        rows = store.semantic_search_entities([0.9, 0.1, 0, 0], limit=2)
        check("ближайший объект первый", rows[0]["name"] == "близкий", str(rows))
        check("оценка сходства убывает",
              rows[0]["score"] >= rows[1]["score"], str(rows))

        # НЕГАТИВНЫЙ: фильтр по kind реально фильтрует
        store.upsert_entity("assembly", "тоже_близкий", embedding=[1, 0, 0, 0])
        only_parts = store.semantic_search_entities([1, 0, 0, 0], kind="part")
        check("фильтр по kind соблюдён",
              all(r["kind"] == "part" for r in only_parts), str(only_parts))
    finally:
        store.close()


def test_chunks_and_entity_refs() -> None:
    section("Фрагменты текста: векторный поиск и привязка к сущностям")
    _, _, PgStore = _import_pg()
    store = PgStore(_fresh_dsn(), dim=4)
    try:
        ids = store.add_chunks("doc.md", ["про корпус", "про крышку"],
                               embeddings=[[1, 0, 0, 0], [0, 1, 0, 0]],
                               entity_refs=[("part", "AB-01")])
        check("вставлено 2 фрагмента", len(ids) == 2, str(ids))

        # повторная индексация того же источника заменяет старые фрагменты
        store.add_chunks("doc.md", ["новый текст"], embeddings=[[0, 0, 1, 0]])
        check("переиндексация заменяет, не копит",
              store.chunk_count() == 1, str(store.chunk_count()))

        store.add_chunks("doc2.md", ["ещё текст"],
                         embeddings=[[1, 0, 0, 0]],
                         entity_refs=[("part", "AB-01")])
        found = store.chunks_for_entities([("part", "AB-01")])
        check("фрагмент найден по привязке к сущности",
              any(f["source"] == "doc2.md" for f in found), str(found))

        sim = store.semantic_search_chunks([1, 0, 0, 0], limit=1)
        check("векторный поиск фрагментов работает", len(sim) == 1, str(sim))
    finally:
        store.close()


def test_tool_layer_with_hash_embedder() -> None:
    section("Инструменты pg_ontology поверх HashEmbedder (без сети/ключей)")
    pg_ontology, _, _ = _import_pg()
    embedder = HashEmbedder(dim=32)
    tools = {t.name: t for t in pg_ontology.build(_fresh_dsn(), embedder, dim=0)}

    out1 = tools["pg_upsert_entity"].fn(
        kind="part", name="AB-01", props='{"material": "steel"}',
        description="корпус редуктора")
    check("объект создан через инструмент", "сохранён в PostgreSQL" in out1, out1)

    out2 = tools["pg_upsert_entity"].fn(kind="assembly", name="Редуктор",
                                        description="планетарный редуктор")
    check("второй объект создан", "сохранён в PostgreSQL" in out2)

    out3 = tools["pg_link"].fn(subject_kind="part", subject="AB-01",
                               predicate="входит_в", object_kind="assembly",
                               object="Редуктор")
    check("связь создана через инструмент", "Связь создана" in out3, out3)

    out4 = tools["pg_neighbours"].fn(kind="part", name="AB-01")
    check("соседи видны через инструмент", "Редуктор" in out4, out4)

    out5 = tools["pg_subgraph"].fn(kind="assembly", name="Редуктор", depth=2)
    check("окрестность видна через инструмент", "AB-01" in out5, out5)

    out6 = tools["pg_semantic_search"].fn(query="корпус")
    check("семантический поиск возвращает результат", "AB-01" in out6, out6)

    out7 = tools["pg_stats"].fn()
    check("статистика отражает вставленные объекты", "2 объектов" in out7, out7)

    # НЕГАТИВНЫЙ: props не JSON-объект
    try:
        tools["pg_upsert_entity"].fn(kind="part", name="X", props="не json")
        check("отказ на невалидный props", False)
    except ToolError:
        check("отказ на невалидный props", True)


def test_lazy_connect_does_not_fail_on_build() -> None:
    section("Ленивое подключение: сборка агента не падает при недоступном DSN")
    from agent.build import build_agent
    from agent.config import Config
    with tempfile.TemporaryDirectory() as td:
        cfg = Config(provider="ollama", model="m", workspace=td,
                    skills=["pg_ontology"],
                    pg_dsn="postgresql://nonexistent-host-xyz/db",
                    embedding_provider="hash")
        agent = build_agent(cfg)  # не должно бросить исключение
        check("агент собран несмотря на недоступный DSN",
              "pg_stats" in agent.tools.names())
        try:
            agent.tools.get("pg_stats").fn()
            check("реальный вызов к недоступному DSN даёт ошибку", False)
        except ToolError:
            check("реальный вызов к недоступному DSN даёт ошибку", True)


def test_dim_autodetect() -> None:
    section("Автоопределение размерности вектора по эмбеддеру")
    pg_ontology, _, _ = _import_pg()
    embedder = HashEmbedder(dim=64)
    tools = {t.name: t for t in pg_ontology.build(_fresh_dsn(), embedder, dim=0)}
    out = tools["pg_upsert_entity"].fn(kind="probe", name="x", description="текст")
    check("объект с автоопределённой размерностью создан",
          "сохранён" in out, out)


def main() -> int:
    if not HAVE_DEPS:
        print(f"pg_ontology: тесты пропущены — {SKIP_REASON}")
        return 0
    try:
        test_store_pg_basic()
        test_semantic_search_orders_by_similarity()
        test_chunks_and_entity_refs()
        test_tool_layer_with_hash_embedder()
        test_lazy_connect_does_not_fail_on_build()
        test_dim_autodetect()
    finally:
        if _srv is not None:
            _srv.cleanup()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())

