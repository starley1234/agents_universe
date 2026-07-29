"""Универсальный SQL-коннектор (ТЗ FR-1.3: "минимум 2 SQL-источника").

ЧЕСТНАЯ ГРАНИЦА: список источников в ТЗ — PostgreSQL, MySQL, MSSQL,
Oracle. В песочнице этой сессии нет реальных серверов MySQL/MSSQL/Oracle
(и нет Docker для их поднятия) — тестировать коннектор к ним пришлось бы
моками, что противоречит принципу проекта "тестировать на настоящей
инфраструктуре". Поэтому реализованы и ЧЕСТНО протестированы два разных
реальных SQL-движка через общий код на DB-API 2.0:

  - PostgreSQL (через psycopg)  — тестируется на embedded pgserver
  - SQLite (через stdlib sqlite3) — тестируется на настоящем файле БД

Оба — настоящие, полнофункциональные SQL СУБД (не заглушки), доступ к
которым идёт через одинаковый DB-API протокол Python (PEP 249), поэтому
код коннектора для них общий. Добавление MySQL/MSSQL/Oracle сводится к
регистрации ещё одного `dialect` в `_CONNECT_FACTORIES` — расширение
без переписывания ядра (ТЗ FR-1.1).

Инкремент (read_changes) реализован через `$filter`-подобный подход:
столбец с монотонно возрастающим значением (обычно `updated_at`/`id`),
курсор — последнее увиденное значение. Это универсальный fallback-режим
(как для 1С в ТЗ), не CDC/Debezium (см. README.md — CDC не реализован).
"""
from __future__ import annotations

from typing import Any, Iterator
from urllib.parse import urlparse

from .base import (
    ChangeBatch,
    ConnectorCapabilityError,
    ConnectorError,
    Cursor,
    DatasetSchema,
    FieldSchema,
    WriteRecord,
    WriteResult,
)

_TYPE_MAP = {
    "integer": "number", "bigint": "number", "smallint": "number",
    "numeric": "number", "real": "number", "double precision": "number",
    "double": "number", "float": "number", "int": "number",
    "boolean": "boolean", "bool": "boolean",
    "timestamp": "datetime", "timestamp without time zone": "datetime",
    "date": "datetime", "datetime": "datetime",
}


def _sql_type_to_field_type(sql_type: str) -> str:
    return _TYPE_MAP.get(sql_type.lower().strip(), "string")


class SqlConnector:
    """Коннектор к одной таблице SQL-источника. `dataset` = имя таблицы.

    dsn: "postgresql://user:pass@host:port/db" | "sqlite:///path/to.db"
    id_field: колонка натурального ключа (для write_back/read_changes)
    cursor_field: колонка для инкремента (обычно timestamp или id)
    """

    def __init__(self, dsn: str, table: str, id_field: str = "id",
                cursor_field: str | None = None) -> None:
        self.dsn = dsn
        self.table = table
        self.id_field = id_field
        self.cursor_field = cursor_field or id_field
        self.dialect = self._detect_dialect(dsn)

    @staticmethod
    def _detect_dialect(dsn: str) -> str:
        parsed = urlparse(dsn)
        if parsed.scheme.startswith("postgres"):
            return "postgresql"
        if parsed.scheme.startswith("sqlite"):
            return "sqlite"
        raise ConnectorError(
            f"Неподдерживаемый SQL-диалект в DSN: {parsed.scheme!r} "
            "(поддерживаются postgresql://, sqlite://)")

    def _connect(self):
        if self.dialect == "postgresql":
            try:
                import psycopg  # type: ignore
            except ImportError as exc:
                raise ConnectorError(
                    "SqlConnector(postgresql) требует psycopg. Установите: "
                    "pip install \"psycopg[binary]\""
                ) from exc
            return psycopg.connect(self.dsn, autocommit=True)
        if self.dialect == "sqlite":
            import sqlite3
            path = self.dsn.replace("sqlite:///", "").replace("sqlite://", "")
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            return conn
        raise ConnectorError(f"Неизвестный диалект: {self.dialect}")

    def _placeholder(self) -> str:
        return "%s" if self.dialect == "postgresql" else "?"

    def discover(self) -> list[DatasetSchema]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            if self.dialect == "postgresql":
                cur.execute(
                    "SELECT column_name, data_type FROM information_schema.columns "
                    "WHERE table_name=%s ORDER BY ordinal_position", (self.table,))
                cols = [(r[0], r[1]) for r in cur.fetchall()]
            else:
                cur.execute(f"PRAGMA table_info({self.table})")
                cols = [(r[1], r[2]) for r in cur.fetchall()]
            if not cols:
                raise ConnectorError(f"Таблица '{self.table}' не найдена или пуста")
            cur.execute(f"SELECT COUNT(*) FROM {self.table}")
            count = cur.fetchone()[0]
            fields = [FieldSchema(name=c, type=_sql_type_to_field_type(t))
                     for c, t in cols]
            return [DatasetSchema(name=self.table, fields=fields,
                                  row_count_hint=int(count))]
        finally:
            conn.close()

    def read_full(self, dataset: str) -> Iterator[dict[str, Any]]:
        self._check_dataset(dataset)
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT * FROM {self.table}")
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                yield dict(zip(cols, row))
        finally:
            conn.close()

    def read_changes(self, dataset: str, cursor: Cursor) -> ChangeBatch:
        self._check_dataset(dataset)
        conn = self._connect()
        try:
            cur = conn.cursor()
            ph = self._placeholder()
            if cursor.value:
                cur.execute(
                    f"SELECT * FROM {self.table} WHERE {self.cursor_field} > {ph} "
                    f"ORDER BY {self.cursor_field}", (cursor.value,))
            else:
                cur.execute(f"SELECT * FROM {self.table} ORDER BY {self.cursor_field}")
            cols = [d[0] for d in cur.description]
            records = [dict(zip(cols, row)) for row in cur.fetchall()]
            next_val = str(records[-1][self.cursor_field]) if records else cursor.value
            return ChangeBatch(records=records, next_cursor=Cursor(value=next_val),
                               has_more=False)
        finally:
            conn.close()

    def write_back(self, dataset: str, records: list[WriteRecord]) -> WriteResult:
        self._check_dataset(dataset)
        conn = self._connect()
        ph = self._placeholder()
        written, errors = 0, []
        try:
            cur = conn.cursor()
            for rec in records:
                try:
                    cols = list(rec.payload.keys())
                    set_clause = ", ".join(f"{c}={ph}" for c in cols)
                    cur.execute(
                        f"UPDATE {self.table} SET {set_clause} WHERE "
                        f"{self.id_field}={ph}",
                        (*rec.payload.values(), rec.natural_key))
                    if cur.rowcount == 0:
                        insert_cols = [self.id_field, *cols]
                        placeholders = ", ".join([ph] * len(insert_cols))
                        cur.execute(
                            f"INSERT INTO {self.table} "
                            f"({', '.join(insert_cols)}) VALUES ({placeholders})",
                            (rec.natural_key, *rec.payload.values()))
                    written += 1
                except Exception as exc:  # noqa: BLE001 - агрегируем построчные ошибки
                    errors.append(f"{rec.natural_key}: {exc}")
            if self.dialect == "sqlite":
                conn.commit()
            return WriteResult(ok=not errors, written=written, errors=errors)
        finally:
            conn.close()

    def _check_dataset(self, dataset: str) -> None:
        if dataset != self.table:
            raise ConnectorCapabilityError(
                f"Неизвестный dataset '{dataset}', ожидалась таблица '{self.table}'")
