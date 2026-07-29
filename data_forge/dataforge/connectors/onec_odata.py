"""Коннектор 1С:Предприятие через стандартный OData (ТЗ §5.1, таблица
"Коннектор onec_odata", FR-1С.1..FR-1С.6).

ЧЕСТНАЯ ГРАНИЦА ОБЪЁМА (согласовано с пользователем): здесь НЕТ
реального сервера 1С — по явному решению пользователя ("протокольно-
честная заглушка"). Но сам HTTP+XML/JSON обмен реализован НАСТОЯЩИЙ и
соответствует документированному поведению OData-интерфейса
1С:Предприятия, а не придуман с нуля:

  - discover()     — GET .../standard.odata/$metadata (EDMX), разбор
                     <EntityType>/<Property> в схему датасетов (FR-1С.1)
  - read_full()     — GET .../standard.odata/<Entity>?$format=json с
                     пагинацией $top/$skip (FR-1С.2)
  - read_changes()  — план обмена: POST .../<Entity>/SelectChanges с
                     параметрами ИмяУзла/НомерСообщения, разбор atom-feed
                     изменений/удалений, затем POST NotifyChangesReceived
                     для подтверждения курсора (FR-1С.3). Fallback-режим
                     `filter_by_date`: GET с $filter по полю даты
                     изменения, если план обмена не настроен.
  - write_back()    — PATCH с заголовком If-Match: W/"<DataVersion>"
                     (оптимистичная блокировка), обработка HTTP 412
                     (конфликт версий) как отдельного, не фатального,
                     результата на запись; заголовок
                     1C_OData-DataLoadMode: true при массовой загрузке,
                     чтобы 1С не проводила бизнес-логику на каждую
                     запись (FR-1С.5)
  - Basic/Bearer аутентификация (FR-1С.6)

Тестируется на локальном fake-HTTP-сервере, эмулирующем ИМЕННО эти
эндпоинты и форматы (см. tests/test_onec_odata.py) — не на настоящей
конфигурации 1С:Предприятия.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any, Iterator

from .base import (
    ChangeBatch,
    ConnectorError,
    Cursor,
    DatasetSchema,
    FieldSchema,
    WriteRecord,
    WriteResult,
)

_EDMX_NS = {"edmx": "http://schemas.microsoft.com/ado/2007/06/edmx",
           "edm": "http://schemas.microsoft.com/ado/2008/09/edm"}
_ATOM_NS = {"a": "http://www.w3.org/2005/Atom",
           "d": "http://schemas.microsoft.com/ado/2007/08/dataservices",
           "m": "http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"}

_EDM_TYPE_MAP = {
    "Edm.String": "string", "Edm.Guid": "string", "Edm.Boolean": "boolean",
    "Edm.Int32": "number", "Edm.Int64": "number", "Edm.Decimal": "number",
    "Edm.Double": "number", "Edm.DateTime": "datetime",
}


class OneCODataError(ConnectorError):
    """Ошибка обмена по OData с 1С: сеть, конфликт версий, неверный формат."""


class OneCVersionConflict(OneCODataError):
    """HTTP 412 — DataVersion записи изменилась с момента последнего
    чтения (оптимистичная блокировка). Не фатальная ошибка на весь
    write_back — фиксируется построчно в WriteResult.errors."""


class OneCODataConnector:
    """Коннектор к 1С:Предприятие через стандартный OData-интерфейс.

    base_url: адрес опубликованной базы, например
              "http://1c-server/erp_base"
    strategy: "exchange_plan" (использовать SelectChanges) или
              "filter_by_date" (fallback через $filter по полю даты)
    date_field: имя поля даты изменения для стратегии filter_by_date
    exchange_point: канонический URL узла плана обмена (параметр
              DataExchangePoint), например
              "ExchangePlan_ОбменДанными(guid'...')" — 1С сама выдаёт
              это значение при настройке плана обмена; берётся из
              конфига интеграции, не из данных
    """

    def __init__(self, base_url: str, username: str = "", password: str = "",
                token: str = "", timeout: int = 30,
                strategy: str = "exchange_plan",
                date_field: str = "ДатаИзменения",
                exchange_point: str = "") -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.token = token
        self.timeout = timeout
        self.strategy = strategy
        self.date_field = date_field
        self.exchange_point = exchange_point

    # ------------------------------------------------------------- HTTP
    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        h: dict[str, str] = {}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        elif self.username:
            creds = base64.b64encode(
                f"{self.username}:{self.password}".encode()).decode()
            h["Authorization"] = f"Basic {creds}"
        if extra:
            h.update(extra)
        return h

    def _request(self, method: str, path: str, body: bytes | None = None,
                headers: dict[str, str] | None = None) -> tuple[int, bytes, dict[str, str]]:
        # Путь может содержать кириллицу (имена сущностей/полей 1С) —
        # urllib не умеет отправлять non-ASCII в request-line, поэтому
        # percent-кодируем, сохраняя структурные символы OData нетронутыми.
        safe_path = urllib.parse.quote(path, safe="/?&=()'\",:$%")
        req = urllib.request.Request(
            f"{self.base_url}{safe_path}", data=body, method=method,
            headers=self._headers(headers))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers or {})
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OneCODataError(
                f"Не удалось связаться с 1С OData ({self.base_url}): {exc}") from exc

    # -------------------------------------------------------- discover
    def discover(self) -> list[DatasetSchema]:
        status, body, _ = self._request("GET", "/standard.odata/$metadata")
        if status != 200:
            raise OneCODataError(f"$metadata вернул HTTP {status}")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise OneCODataError(f"Неверный EDMX в $metadata: {exc}") from exc

        schemas: list[DatasetSchema] = []
        for entity_type in root.iter("{http://schemas.microsoft.com/ado/2008/09/edm}EntityType"):
            name = entity_type.get("Name", "")
            if not name:
                continue
            fields = []
            for prop in entity_type.findall(
                    "{http://schemas.microsoft.com/ado/2008/09/edm}Property"):
                pname = prop.get("Name", "")
                ptype = prop.get("Type", "Edm.String")
                fields.append(FieldSchema(
                    name=pname, type=_EDM_TYPE_MAP.get(ptype, "string")))
            schemas.append(DatasetSchema(name=name, fields=fields))
        if not schemas:
            raise OneCODataError(
                "$metadata не содержит ни одного EntityType — проверьте "
                "публикацию OData-интерфейса 1С")
        return schemas

    # -------------------------------------------------------- read_full
    def read_full(self, dataset: str, page_size: int = 100) -> Iterator[dict[str, Any]]:
        """GET .../<Entity>?$format=json с пагинацией $top/$skip (FR-1С.2)."""
        skip = 0
        while True:
            status, body, _ = self._request(
                "GET", f"/standard.odata/{dataset}"
                      f"?$format=json&$top={page_size}&$skip={skip}")
            if status != 200:
                raise OneCODataError(
                    f"Чтение '{dataset}' вернуло HTTP {status}: "
                    f"{body.decode('utf-8', 'replace')[:300]}")
            try:
                data = json.loads(body)
            except json.JSONDecodeError as exc:
                raise OneCODataError(f"Неверный JSON-ответ OData: {exc}") from exc
            page = data.get("value", [])
            for rec in page:
                yield rec
            if len(page) < page_size:
                break
            skip += page_size

    # ----------------------------------------------------- read_changes
    def read_changes(self, dataset: str, cursor: Cursor) -> ChangeBatch:
        if self.strategy == "filter_by_date":
            return self._read_changes_filter_by_date(dataset, cursor)
        return self._read_changes_exchange_plan(dataset, cursor)

    def _read_changes_exchange_plan(self, dataset: str, cursor: Cursor) -> ChangeBatch:
        """FR-1С.3: POST SelectChanges -> atom-feed изменений/удалений,
        затем POST NotifyChangesReceived для подтверждения курсора.

        В реальном протоколе 1С (см. документацию "Интерфейс OData:
        примеры типовых операций") номер сообщения (`MessageNo`)
        генерирует и увеличивает СТОРОНА-ПОЛУЧАТЕЛЬ (то есть эта
        платформа), а не 1С — сервер не возвращает "следующий номер" в
        теле ответа. Поэтому курсор — это последний ПОДТВЕРЖДЁННЫЙ
        номер сообщения; при каждом вызове читаем со следующим номером
        (cursor+1) и, если чтение успешно, подтверждаем именно этот
        номер через NotifyChangesReceived — БЕЗ этого подтверждения
        следующий SelectChanges с тем же номером вернул бы те же данные
        повторно (план обмена не продвинется)."""
        if not self.exchange_point:
            raise OneCODataError(
                "Для strategy='exchange_plan' обязателен exchange_point "
                "(канонический URL узла плана обмена, DataExchangePoint) — "
                "настройте его в конфиге интеграции.")
        current_no = int(cursor.value) if cursor.value else 0
        next_no = current_no + 1
        status, body, _ = self._request(
            "POST", f"/standard.odata/{dataset}/SelectChanges"
                   f"?DataExchangePoint='{self.exchange_point}'&MessageNo={next_no}")
        if status != 200:
            raise OneCODataError(f"SelectChanges вернул HTTP {status}")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as exc:
            raise OneCODataError(f"Неверный atom-feed в SelectChanges: {exc}") from exc

        records: list[dict[str, Any]] = []
        for entry in root.findall("a:entry", _ATOM_NS):
            props = entry.find(".//m:properties", _ATOM_NS)
            if props is None:
                continue
            rec = {}
            for prop in props:
                tag = prop.tag.split("}", 1)[-1]
                rec[tag] = prop.text
            records.append(rec)

        # Удалённые записи атом-фида приходят как <at:deleted-entry ref="..."/>
        # (RFC 6721) — тег "deleted-entry" в пространстве имён atom-tombstones,
        # но многие клиенты 1С разбирают его просто по локальному имени тега.
        deletes = [el.get("ref", "") for el in root.iter()
                  if el.tag.split("}", 1)[-1] == "deleted-entry"]

        # Подтверждаем получение ИМЕННО этого номера — иначе повторный
        # SelectChanges с ним же вернёт те же данные снова.
        confirm_status, _, _ = self._request(
            "POST", f"/standard.odata/{dataset}/NotifyChangesReceived"
                   f"?DataExchangePoint='{self.exchange_point}'&MessageNo={next_no}")
        if confirm_status != 200:
            raise OneCODataError(
                f"NotifyChangesReceived вернул HTTP {confirm_status} — курсор "
                "не подтверждён, следующий SelectChanges вернёт те же данные")

        return ChangeBatch(records=records, deletes=deletes,
                           next_cursor=Cursor(value=str(next_no)), has_more=False)

    def _read_changes_filter_by_date(self, dataset: str, cursor: Cursor) -> ChangeBatch:
        """Fallback без плана обмена: $filter по полю даты изменения."""
        filt = f"{self.date_field} gt datetime'{cursor.value}'" if cursor.value else ""
        path = f"/standard.odata/{dataset}?$format=json"
        if filt:
            path += f"&$filter={urllib.parse.quote(filt)}"
        status, body, _ = self._request("GET", path)
        if status != 200:
            raise OneCODataError(f"Чтение изменений вернуло HTTP {status}")
        data = json.loads(body)
        records = data.get("value", [])
        next_val = cursor.value
        for rec in records:
            v = rec.get(self.date_field)
            if v and (not next_val or v > next_val):
                next_val = v
        return ChangeBatch(records=records, next_cursor=Cursor(value=next_val),
                           has_more=False)

    # ------------------------------------------------------- write_back
    def write_back(self, dataset: str, records: list[WriteRecord],
                   bulk_load: bool = False) -> WriteResult:
        """FR-1С.5: PATCH с If-Match для оптимистичной блокировки. Если
        DataVersion записи изменилась в 1С с момента последнего чтения —
        сервер вернёт 412, это фиксируется как построчная ошибка (не
        обрывает обработку остальных записей).

        bulk_load=True добавляет заголовок 1C_OData-DataLoadMode: true —
        массовая загрузка без выполнения бизнес-логики/проведения
        документов на каждую запись (ТЗ, таблица коннектора)."""
        written, errors = 0, []
        for rec in records:
            headers = {"Content-Type": "application/json"}
            if bulk_load:
                headers["1C_OData-DataLoadMode"] = "true"
            data_version = rec.payload.get("DataVersion")
            if data_version:
                headers["If-Match"] = f'W/"{data_version}"'
            body = json.dumps(
                {k: v for k, v in rec.payload.items() if k != "DataVersion"},
                ensure_ascii=False).encode("utf-8")
            status, resp_body, _ = self._request(
                "PATCH", f"/standard.odata/{dataset}(guid'{rec.natural_key}')",
                body=body, headers=headers)
            if status == 412:
                errors.append(
                    f"{rec.natural_key}: HTTP 412 — конфликт версий "
                    "(DataVersion изменилась в 1С с момента чтения)")
                continue
            if status not in (200, 201, 204):
                errors.append(
                    f"{rec.natural_key}: HTTP {status} — "
                    f"{resp_body.decode('utf-8', 'replace')[:200]}")
                continue
            written += 1
        return WriteResult(ok=not errors, written=written, errors=errors)
