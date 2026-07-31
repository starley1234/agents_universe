"""Инструменты работы с базами данных SQLite и выполнения SQL-запросов.

Обеспечивают инспекцию схемы таблиц и безопасное выполнение запросов.
В режиме read_only разрешены только SELECT и PRAGMA запросы.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from ..core import Tool, ToolError, Workspace

MAX_SQL_ROWS = 200


def _init_sample_db(db_path: Path) -> None:
    """Создать тестовую базу данных с примером таблицы для автономных тестов."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS products "
            "(id INTEGER PRIMARY KEY, brand TEXT, name TEXT, price REAL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS inventory "
            "(id INTEGER PRIMARY KEY, product_id INTEGER, shelf_level INTEGER, count INTEGER)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO products (id, brand, name, price) VALUES "
            "(1, 'Acme', 'Cola 1.5L', 120.0), (2, 'Acme', 'Orange 1L', 110.0)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO inventory (id, product_id, shelf_level, count) VALUES "
            "(1, 1, 2, 15), (2, 2, 2, 8)"
        )
        conn.commit()


def build_sql_tools(ws: Workspace) -> list[Tool]:
    """Собрать инструменты для работы с базами данных (SQLite)."""

    def inspect_schema(db_path: str = "app.db") -> str:
        p = ws.resolve(db_path)
        if not p.exists():
            _init_sample_db(p)

        try:
            with sqlite3.connect(p) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT name, type, sql FROM sqlite_master "
                    "WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' "
                    "ORDER BY name"
                )
                rows = cursor.fetchall()
                if not rows:
                    return f"База данных {ws.relative(p)} пуста (таблиц нет)"
                lines = [f"### Схема базы данных ({ws.relative(p)}):"]
                for name, obj_type, sql_ddl in rows:
                    lines.append(f"\n**{obj_type.upper()} `{name}`**:")
                    if sql_ddl:
                        lines.append(f"```sql\n{sql_ddl.strip()}\n```")
                    # Подсчёт количества записей
                    try:
                        cursor.execute(f"SELECT COUNT(*) FROM \"{name}\"")
                        cnt = cursor.fetchone()[0]
                        lines.append(f"*Записей: {cnt}*")
                    except sqlite3.Error:
                        pass
                return "\n".join(lines)
        except sqlite3.Error as exc:
            raise ToolError(f"Ошибка чтения схемы БД {db_path!r}: {exc}") from exc

    def execute_query(
        query: str, db_path: str = "app.db", params_json: str = "[]"
    ) -> str:
        if not query.strip():
            raise ToolError("SQL-запрос не может быть пустым")

        p = ws.resolve(db_path)
        if not p.exists():
            _init_sample_db(p)

        try:
            params = json.loads(params_json) if params_json else []
            if not isinstance(params, (list, tuple)):
                raise ValueError("params_json должен быть JSON-массивом")
        except ValueError as exc:
            raise ToolError(f"Некорректный JSON параметров запроса: {exc}") from exc

        q_clean = query.strip().upper()
        is_select = (
            q_clean.startswith("SELECT")
            or q_clean.startswith("EXPLAIN")
            or q_clean.startswith("PRAGMA")
            or q_clean.startswith("WITH")
        )

        try:
            with sqlite3.connect(p) as conn:
                cursor = conn.cursor()
                cursor.execute(query, params)

                if is_select and cursor.description:
                    columns = [col[0] for col in cursor.description]
                    rows = cursor.fetchmany(MAX_SQL_ROWS)
                    lines = [
                        f"### Результат SQL-запроса ({ws.relative(p)}):",
                        " | ".join(columns),
                        " | ".join("---" for _ in columns),
                    ]
                    for r in rows:
                        lines.append(" | ".join(str(val) for val in r))
                    if not rows:
                        lines.append("(0 строк)")
                    return "\n".join(lines)

                conn.commit()
                return (
                    f"Запрос выполнен ({ws.relative(p)}). "
                    f"Изменено строк (rowcount): {cursor.rowcount}"
                )
        except sqlite3.Error as exc:
            raise ToolError(f"Ошибка выполнения SQL-запроса: {exc}") from exc

    return [
        Tool(
            name="sql.inspect_schema",
            description="Просмотреть схему базы данных (таблицы, колонки, DDL и количество строк).",
            parameters={
                "type": "object",
                "properties": {
                    "db_path": {
                        "type": "string",
                        "description": "Путь к файлу базы данных SQLite (по умолчанию 'app.db')",
                    }
                },
            },
            fn=inspect_schema,
            skills=["sql", "db", "database", "sqlite", "local", "schema"],
            attributes={
                "category": "local",
                "read_only": True,
                "dangerous": False,
                "resource_type": "database",
                "speed": "fast",
                "tags": ["sql", "db", "sqlite", "schema", "database", "table"],
            },
            example='sql.inspect_schema(db_path="store.db")',
        ),
        Tool(
            name="sql.execute_query",
            description="Выполнить SQL-запрос к базе данных (SELECT, INSERT, UPDATE, DELETE).",
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "SQL-запрос (например, 'SELECT * FROM products')",
                    },
                    "db_path": {
                        "type": "string",
                        "description": "Путь к базе данных SQLite",
                    },
                    "params_json": {
                        "type": "string",
                        "description": 'JSON-массив параметров для подстановки (например, \'[10]\')',
                    },
                },
                "required": ["query"],
            },
            fn=execute_query,
            skills=["sql", "db", "database", "sqlite", "local", "query"],
            attributes={
                "category": "local",
                "read_only": False,
                "dangerous": False,
                "resource_type": "database",
                "speed": "fast",
                "tags": ["sql", "query", "db", "sqlite", "database", "select"],
            },
            example='sql.execute_query(query="SELECT * FROM products WHERE price > ?", params_json="[100]")',
        ),
    ]
