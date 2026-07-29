"""Файловый коннектор: CSV/Excel(xlsx)/JSON/XML (ТЗ FR-1.3, batch-режим).

Источник только для чтения (`read_changes`/`write_back` бросают
`ConnectorCapabilityError`) — обратной записи в файл в этой сборке нет
смысла реализовывать, write-back продемонстрирован на 1С-адаптере
(erp_ai) и здесь — на онтологии "запись-в-источник" не требуется по
согласованному объёму сессии (см. README.md).

Опциональные зависимости (openpyxl для .xlsx) — импортируются лениво,
CSV/JSON/XML работают на стандартной библиотеке без единой внешней
зависимости.
"""
from __future__ import annotations

import csv
import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterator

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


def _require_openpyxl():
    try:
        import openpyxl  # type: ignore
    except ImportError as exc:
        raise ConnectorError(
            "Чтение .xlsx требует openpyxl. Установите: pip install openpyxl"
        ) from exc
    return openpyxl


def _infer_type(value: Any) -> str:
    if value is None or value == "":
        return "unknown"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "false"):
            return "boolean"
        try:
            float(value)
            return "number"
        except ValueError:
            pass
    return "string"


class FileConnector:
    """Коннектор к одному файлу CSV/XLSX/JSON/XML. `dataset` в методах
    протокола — логическое имя, для файлового коннектора всегда равно
    базовому имени файла (без пути) для простоты MVP."""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        if not self.path.exists():
            raise ConnectorError(f"Файл не найден: {path}")
        self._fmt = self._detect_format()

    def _detect_format(self) -> str:
        suffix = self.path.suffix.lower()
        if suffix == ".csv":
            return "csv"
        if suffix in (".xlsx", ".xlsm"):
            return "xlsx"
        if suffix == ".json":
            return "json"
        if suffix == ".xml":
            return "xml"
        raise ConnectorError(
            f"Неподдерживаемый формат файла: {suffix} (поддерживаются "
            "csv/xlsx/json/xml)")

    @property
    def dataset_name(self) -> str:
        return self.path.name

    def discover(self) -> list[DatasetSchema]:
        records = list(self._iter_raw())
        fields: dict[str, str] = {}
        for rec in records[:200]:   # профилируем по выборке — MVP
            for k, v in rec.items():
                t = _infer_type(v)
                if k not in fields or fields[k] == "unknown":
                    fields[k] = t
        schema = DatasetSchema(
            name=self.dataset_name,
            fields=[FieldSchema(name=k, type=t) for k, t in fields.items()],
            row_count_hint=len(records),
        )
        return [schema]

    def read_full(self, dataset: str) -> Iterator[dict[str, Any]]:
        self._check_dataset(dataset)
        yield from self._iter_raw()

    def read_changes(self, dataset: str, cursor: Cursor) -> ChangeBatch:
        raise ConnectorCapabilityError(
            "FileConnector не поддерживает инкрементальное чтение "
            "(read_changes) — файлы читаются целиком (batch), используйте "
            "read_full.")

    def write_back(self, dataset: str, records: list[WriteRecord]) -> WriteResult:
        raise ConnectorCapabilityError(
            "FileConnector не поддерживает write_back — обратная запись "
            "в исходный файл не реализована в этой сборке (см. README.md).")

    def _check_dataset(self, dataset: str) -> None:
        if dataset != self.dataset_name:
            raise ConnectorError(
                f"Неизвестный dataset '{dataset}', ожидался '{self.dataset_name}'")

    def _iter_raw(self) -> Iterator[dict[str, Any]]:
        if self._fmt == "csv":
            yield from self._iter_csv()
        elif self._fmt == "xlsx":
            yield from self._iter_xlsx()
        elif self._fmt == "json":
            yield from self._iter_json()
        elif self._fmt == "xml":
            yield from self._iter_xml()

    def _iter_csv(self) -> Iterator[dict[str, Any]]:
        with open(self.path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                yield dict(row)

    def _iter_xlsx(self) -> Iterator[dict[str, Any]]:
        openpyxl = _require_openpyxl()
        wb = openpyxl.load_workbook(self.path, read_only=True, data_only=True)
        ws = wb.active
        rows = ws.iter_rows(values_only=True)
        try:
            header = [str(h) if h is not None else f"col{i}"
                     for i, h in enumerate(next(rows))]
        except StopIteration:
            return
        for row in rows:
            yield dict(zip(header, row))
        wb.close()

    def _iter_json(self) -> Iterator[dict[str, Any]]:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    yield item
                else:
                    yield {"value": item}
        elif isinstance(data, dict):
            # поддержка формата {"records": [...]}
            if "records" in data and isinstance(data["records"], list):
                for item in data["records"]:
                    yield item if isinstance(item, dict) else {"value": item}
            else:
                yield data
        else:
            raise ConnectorError("JSON верхнего уровня должен быть list или dict")

    def _iter_xml(self) -> Iterator[dict[str, Any]]:
        tree = ET.parse(self.path)
        root = tree.getroot()
        # Эвристика MVP: записи — прямые дети корня (или дети первого
        # повторяющегося тега, если корень оборачивает список записей).
        children = list(root)
        if not children:
            return
        for child in children:
            rec: dict[str, Any] = dict(child.attrib)
            for sub in child:
                rec[sub.tag] = sub.text
            if not rec and child.text and child.text.strip():
                rec = {"value": child.text.strip()}
            yield rec

