"""Единый контракт коннектора (см. ТЗ §5.1) и общие типы.

Философия: платформа не должна знать о специфике источника. Каждый
коннектор реализует один и тот же протокол — discover/read_full/
read_changes/write_back — и подключается декларативным конфигом (класс
`Config`-подобный dict, без правки ядра). Не все коннекторы обязаны
поддерживать все операции: source-only коннекторы (файлы) вправе не
реализовывать write_back и read_changes — они бросают
`ConnectorCapabilityError` с понятным сообщением вместо тихого no-op.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Protocol, runtime_checkable


class ConnectorError(RuntimeError):
    """Общая ошибка коннектора: сеть, формат данных, аутентификация."""


class ConnectorCapabilityError(ConnectorError):
    """Коннектор не поддерживает запрошенную операцию (например,
    write_back для источника только для чтения)."""


@dataclass
class FieldSchema:
    name: str
    type: str = "string"     # string | number | boolean | datetime | unknown


@dataclass
class DatasetSchema:
    name: str
    fields: list[FieldSchema] = field(default_factory=list)
    row_count_hint: int = -1   # -1 = неизвестно заранее

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "fields": [{"name": f.name, "type": f.type} for f in self.fields],
            "row_count_hint": self.row_count_hint,
        }


@dataclass
class Cursor:
    """Позиция для инкрементального чтения. `value` — непрозрачна для
    ядра платформы: каждый коннектор кладёт туда то, что ему удобно
    (номер строки, timestamp, курсор плана обмена 1С и т.п.)."""
    value: str = ""


@dataclass
class ChangeBatch:
    records: list[dict[str, Any]]
    deletes: list[str] = field(default_factory=list)  # natural key'и удалённых
    next_cursor: Cursor = field(default_factory=Cursor)
    has_more: bool = False


@dataclass
class WriteRecord:
    natural_key: str
    payload: dict[str, Any]
    idempotency_key: str = ""


@dataclass
class WriteResult:
    ok: bool
    written: int = 0
    errors: list[str] = field(default_factory=list)


@runtime_checkable
class Connector(Protocol):
    """Единый интерфейс коннектора (ТЗ §5.1). Любой источник данных
    платформы обязан реализовать эти четыре метода — ядро (Connect Hub,
    ingest-пайплайн) работает только через них, никогда не заглядывая
    внутрь конкретной реализации."""

    def discover(self) -> list[DatasetSchema]:
        ...

    def read_full(self, dataset: str) -> Iterator[dict[str, Any]]:
        ...

    def read_changes(self, dataset: str, cursor: Cursor) -> ChangeBatch:
        ...

    def write_back(self, dataset: str, records: list[WriteRecord]) -> WriteResult:
        ...
