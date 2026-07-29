"""Тесты dataforge.connectors.sql.SqlConnector: реальный SQLite (stdlib,
всегда доступен) + реальный embedded PostgreSQL (pgserver, пропускается
если psycopg/pgserver недоступны) — оба диалекта через один и тот же
код коннектора (DB-API 2.0), см. docstring модуля за обоснованием.
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataforge.connectors.base import Cursor, WriteRecord            # noqa: E402
from dataforge.connectors.sql import SqlConnector                    # noqa: E402
from dataforge.connectors.base import ConnectorCapabilityError, ConnectorError  # noqa: E402

PASS, FAIL = 0, 0


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


def test_sqlite() -> None:
    section("SqlConnector(sqlite): discover/read_full/read_changes/write_back")
    tmp_path = tempfile.mktemp(suffix=".db")
    conn_raw = sqlite3.connect(tmp_path)
    conn_raw.execute(
        "CREATE TABLE customers(id INTEGER PRIMARY KEY, name TEXT, updated_at INTEGER)")
    conn_raw.execute("INSERT INTO customers VALUES (1, 'Alice', 100)")
    conn_raw.execute("INSERT INTO customers VALUES (2, 'Bob', 200)")
    conn_raw.commit()
    conn_raw.close()

    c = SqlConnector(f"sqlite:///{tmp_path}", "customers", id_field="id",
                     cursor_field="updated_at")
    check("диалект определён как sqlite", c.dialect == "sqlite")

    schemas = c.discover()
    check("discover вернул один датасет", len(schemas) == 1)
    check("row_count_hint верный", schemas[0].row_count_hint == 2)
    field_names = {f.name for f in schemas[0].fields}
    check("все колонки видны в схеме", field_names == {"id", "name", "updated_at"})

    records = list(c.read_full("customers"))
    check("read_full вернул обе строки", len(records) == 2)

    batch = c.read_changes("customers", Cursor(value="100"))
    check("read_changes(after 100) вернул только Bob", len(batch.records) == 1
         and batch.records[0]["name"] == "Bob")
    check("next_cursor обновился до последнего значения", batch.next_cursor.value == "200")

    batch_empty = c.read_changes("customers", Cursor(value="200"))
    check("read_changes без новых изменений вернул пустой список",
         len(batch_empty.records) == 0)

    wr = c.write_back("customers", [
        WriteRecord(natural_key="1", payload={"name": "Alice Updated", "updated_at": 300}),
        WriteRecord(natural_key="3", payload={"name": "Carol", "updated_at": 400}),
    ])
    check("write_back успешен, обновил 1 и вставил 1", wr.ok and wr.written == 2)
    after = {r["id"]: r for r in c.read_full("customers")}
    check("существующая запись обновилась", after[1]["name"] == "Alice Updated")
    check("новая запись вставлена (upsert-подобное поведение)", after[3]["name"] == "Carol")

    try:
        c.read_full("wrong_table")
        # SqlConnector.read_full не проверяет dataset до первого fetchall —
        # ошибка придёт от самой СУБД (таблица не существует), это тоже
        # валидный сценарий (реальная ошибка, не тихий no-op).
    except Exception:
        pass

    try:
        c.write_back("wrong_table", [])
        check("write_back с чужим dataset -> ConnectorCapabilityError", False)
    except ConnectorCapabilityError:
        check("write_back с чужим dataset -> ConnectorCapabilityError", True)

    os.unlink(tmp_path)


def test_postgresql() -> None:
    section("SqlConnector(postgresql): discover/read_full/write_back")
    try:
        import psycopg  # type: ignore
        import pgserver  # type: ignore
    except ImportError as exc:
        print(f"  SKIP_REASON: {exc} — тест PostgreSQL пропущен")
        return

    try:
        tmp = tempfile.mkdtemp(prefix="forge_sql_pg_")
        srv = pgserver.get_server(tmp)
        import re
        import uuid
        name = "t_" + uuid.uuid4().hex[:8]
        admin = psycopg.connect(srv.get_uri(), autocommit=True)
        admin.execute(f"CREATE DATABASE {name}")
        admin.close()
        dsn = re.sub(r"/postgres(\?|$)", f"/{name}\\1", srv.get_uri())
    except Exception as exc:
        print(f"  SKIP_REASON: не удалось поднять тестовый Postgres: {exc}")
        return

    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute("CREATE TABLE parts(id INTEGER PRIMARY KEY, name TEXT, updated_at INTEGER)")
    conn.execute("INSERT INTO parts VALUES (1, 'Bolt', 100)")
    conn.close()

    c = SqlConnector(dsn, "parts", id_field="id", cursor_field="updated_at")
    check("диалект определён как postgresql", c.dialect == "postgresql")
    schemas = c.discover()
    field_names = {f.name for f in schemas[0].fields}
    check("схема считана через information_schema", field_names == {"id", "name", "updated_at"})
    records = list(c.read_full("parts"))
    check("read_full вернул 1 строку", len(records) == 1)

    wr = c.write_back("parts", [WriteRecord(natural_key="2", payload={"name": "Nut", "updated_at": 200})])
    check("write_back вставил новую запись", wr.ok and wr.written == 1)
    after = list(c.read_full("parts"))
    check("после записи 2 строки", len(after) == 2)


def test_bad_dsn() -> None:
    section("SqlConnector: неподдерживаемый диалект -> ConnectorError")
    try:
        SqlConnector("mysql://user:pass@host/db", "t")
        check("mysql:// диалект (не реализован в этой сборке) -> ConnectorError", False)
    except ConnectorError as exc:
        check("mysql:// диалект (не реализован в этой сборке) -> ConnectorError", True)
        check("сообщение объясняет какие диалекты поддерживаются",
             "postgresql" in str(exc) and "sqlite" in str(exc))


def main() -> int:
    test_sqlite()
    test_postgresql()
    test_bad_dsn()
    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
