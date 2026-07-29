"""Фабрика коннекторов: строит объект `Connector` из записи `source` в
Store (см. dataforge/db/store.py, таблица `source`).

ВАЖНОЕ решение по безопасности (тот же принцип, что и во всём
репозитории — секреты только из окружения): `source.config` хранится в
PostgreSQL как обычный JSONB и НЕ шифруется на уровне приложения,
поэтому туда нельзя класть пароли/токены. Вместо самого DSN источник
хранит ИМЯ переменной окружения, где лежит настоящий DSN
(`dsn_env`) — так конфиг источника (в т.ч. видимый через GET /v1/sources)
никогда не содержит секрета, а секрет живёт только в окружении процесса.

Для 1С OData используется ОДИН глобальный набор учётных данных из
Config (`cfg.onec_base_url`/`cfg.onec_api_key`) — типичное предприятие
интегрируется с одной конфигурацией 1С, заводить секрет на каждый
"источник 1С" избыточно для этого объёма (при необходимости нескольких
инсталляций 1С схема расширяется по тому же принципу `*_env`).
"""
from __future__ import annotations

import os
from typing import Any

from .base import ConnectorError
from .files import FileConnector
from .onec_odata import OneCODataConnector
from .sql import SqlConnector


def build_connector(source: dict[str, Any], onec_base_url: str = "",
                    onec_api_key: str = "", onec_timeout: int = 30) -> Any:
    """source — запись из Store.get_source()/get_source_by_name()."""
    kind = source["kind"]
    config = source["config"] or {}
    if kind == "file":
        path = config.get("path", "")
        if not path:
            raise ConnectorError("Источник kind='file' требует config.path")
        return FileConnector(path)

    if kind == "sql":
        dsn_env = config.get("dsn_env", "")
        if not dsn_env:
            raise ConnectorError(
                "Источник kind='sql' требует config.dsn_env — имя "
                "переменной окружения с реальным DSN (секрет не хранится "
                "в конфиге источника)")
        dsn = os.getenv(dsn_env, "")
        if not dsn:
            raise ConnectorError(
                f"Переменная окружения {dsn_env} не задана или пуста")
        table = config.get("table", "")
        if not table:
            raise ConnectorError("Источник kind='sql' требует config.table")
        return SqlConnector(
            dsn, table, id_field=config.get("id_field", "id"),
            cursor_field=config.get("cursor_field"))

    if kind == "onec_odata":
        if not onec_base_url:
            raise ConnectorError(
                "Источник kind='onec_odata' требует ONEC_BASE_URL в "
                "конфигурации приложения (dataforge/config.py)")
        return OneCODataConnector(
            onec_base_url, token=onec_api_key, timeout=onec_timeout,
            strategy=config.get("strategy", "exchange_plan"),
            date_field=config.get("date_field", "ДатаИзменения"),
            exchange_point=config.get("exchange_point", ""))

    raise ConnectorError(
        f"Неизвестный тип источника: {kind!r} (поддерживаются file/sql/onec_odata)")
