"""Тесты инструментов работы с базами данных SQLite (sql.*) и СУБД PostgreSQL/MySQL (db.*)."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent_toolkit.core import Workspace
from agent_toolkit.integrations.db import build_db_tools
from agent_toolkit.local.sql import build_sql_tools
from tests.harness import TempWorkspace, check, section, summary


def run_tests() -> int:
    with TempWorkspace() as tmp:
        ws = Workspace(tmp.path("ws"))
        section("1. Инструменты баз данных SQLite (sql.*)")
        tools = {t.name: t for t in build_sql_tools(ws)}
        check("зарегистрировано 2 инструмента sql", len(tools) == 2)

        res_schema = tools["sql.inspect_schema"].execute(db_path="test.db")
        check("inspect_schema создаёт БД и показывает таблицы", "products" in res_schema and "CREATE TABLE" in res_schema)

        res_select = tools["sql.execute_query"].execute(
            query="SELECT * FROM products WHERE price > ?",
            db_path="test.db",
            params_json="[100.0]",
        )
        check("execute_query SELECT возвращает строки", "Acme" in res_select and "120.0" in res_select)

        res_insert = tools["sql.execute_query"].execute(
            query="INSERT INTO products (id, brand, name, price) VALUES (?, ?, ?, ?)",
            db_path="test.db",
            params_json='[3, "NewBrand", "Soda 2L", 150.0]',
        )
        check("execute_query INSERT сообщает об изменении строк", "rowcount" in res_insert)

        res_cnt = tools["sql.execute_query"].execute(
            query="SELECT COUNT(*) FROM products", db_path="test.db"
        )
        check("в таблице теперь 3 записи", "3" in res_cnt)

        section("2. Промышленные СУБД PostgreSQL/MySQL и ER-диаграммы (db.*)")
        db_tools = {t.name: t for t in build_db_tools(ws)}
        check("зарегистрировано 3 инструмента db", len(db_tools) == 3)

        res_pg = db_tools["db.postgres_execute"].execute(query="SELECT * FROM users")
        check("db.postgres_execute возвращает строки в mock-режиме", "admin@enterprise.local" in res_pg)

        res_mysql = db_tools["db.mysql_execute"].execute(query="SELECT * FROM products")
        check("db.mysql_execute возвращает строки в mock-режиме", "ART-1001" in res_mysql)

        res_er_mermaid = db_tools["db.generate_er_diagram"].execute(output_format="mermaid")
        check("db.generate_er_diagram генерирует Mermaid диаграмму", "erDiagram" in res_er_mermaid and "USERS" in res_er_mermaid)

        res_er_md = db_tools["db.generate_er_diagram"].execute(output_format="markdown")
        check("db.generate_er_diagram поддерживает формат markdown", "Таблица `USERS`" in res_er_md)

    return summary("Тесты инструментов SQL и СУБД")


def test_sql_pytest():
    assert run_tests() == 0


if __name__ == "__main__":
    raise SystemExit(run_tests())
