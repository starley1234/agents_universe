"""Тесты dataforge.connectors.onec_odata.OneCODataConnector: реальный
HTTP+JSON/XML на локальном fake-сервере, эмулирующем ИМЕННО задокументированные
эндпоинты и параметры стандартного OData-интерфейса 1С:Предприятия
($metadata, $top/$skip, SelectChanges с DataExchangePoint/MessageNo,
NotifyChangesReceived, PATCH с If-Match) — см. docstring модуля коннектора
за ссылками на протокол. НЕ настоящая конфигурация 1С (см. README.md,
"Честная граница объёма").
"""
from __future__ import annotations

import socket
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dataforge.connectors.base import Cursor, WriteRecord            # noqa: E402
from dataforge.connectors.onec_odata import (                        # noqa: E402
    OneCODataConnector,
    OneCODataError,
)

PASS, FAIL = 0, 0

METADATA_XML = """<?xml version="1.0"?>
<edmx:Edmx xmlns:edmx="http://schemas.microsoft.com/ado/2007/06/edmx" Version="1.0">
  <edmx:DataServices xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
    <Schema xmlns="http://schemas.microsoft.com/ado/2008/09/edm" Namespace="ns">
      <EntityType Name="Catalog_Номенклатура">
        <Property Name="Ref_Key" Type="Edm.Guid"/>
        <Property Name="Артикул" Type="Edm.String"/>
        <Property Name="Наименование" Type="Edm.String"/>
        <Property Name="Цена" Type="Edm.Decimal"/>
        <Property Name="DataVersion" Type="Edm.String"/>
      </EntityType>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>"""

_SELECT_CHANGES_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"
      xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata">
  <entry><content><m:properties>
    <d:Ref_Key>g2</d:Ref_Key><d:Артикул>A2</d:Артикул>
  </m:properties></content></entry>
  <at:deleted-entry xmlns:at="http://www.w3.org/2007/app" ref="g-deleted-1"/>
</feed>"""


class Fake1COData(BaseHTTPRequestHandler):
    calls: list[str] = []
    fail_select_changes = False
    require_auth = False

    def log_message(self, *a):
        pass

    def _check_auth(self) -> bool:
        if not type(self).require_auth:
            return True
        auth = self.headers.get("Authorization", "")
        if auth == "Bearer secret-token" or auth.startswith("Basic "):
            return True
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self):  # noqa: N802
        path = urllib.parse.unquote(self.path)
        type(self).calls.append(f"GET {path}")
        if not self._check_auth():
            return
        if path == "/standard.odata/$metadata":
            body = METADATA_XML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/standard.odata/Catalog_Номенклатура"):
            import json as _json
            if "skip=0" in path:
                data = {"value": [
                    {"Ref_Key": "g1", "Артикул": "A1", "Наименование": "Товар1"}]}
            else:
                data = {"value": []}
            body = _json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):  # noqa: N802
        path = urllib.parse.unquote(self.path)
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        type(self).calls.append(f"POST {path}")
        if not self._check_auth():
            return
        if "SelectChanges" in path:
            if type(self).fail_select_changes:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            assert "DataExchangePoint=" in path and "MessageNo=" in path, path
            body = _SELECT_CHANGES_FEED.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if "NotifyChangesReceived" in path:
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_PATCH(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        self.rfile.read(n)
        if_match = self.headers.get("If-Match", "")
        if "OLDVERSION" in if_match:
            self.send_response(412)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


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


def main() -> int:
    port = _free_port()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Fake1COData)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        base_url = f"http://127.0.0.1:{port}"

        section("discover(): $metadata (EDMX) -> схема датасетов")
        c = OneCODataConnector(base_url, username="u", password="p")
        schemas = c.discover()
        check("найдена ровно одна сущность", len(schemas) == 1)
        check("имя сущности с кириллицей разобрано верно",
             schemas[0].name == "Catalog_Номенклатура")
        fields = {f.name: f.type for f in schemas[0].fields}
        check("типы EDM смаплены верно",
             fields["Ref_Key"] == "string" and fields["Цена"] == "number")

        section("read_full(): пагинация $top/$skip")
        records = list(c.read_full("Catalog_Номенклатура", page_size=1))
        check("получена 1 запись (fake-сервер отдаёт 1 страницу)", len(records) == 1)
        check("кириллические поля читаются", records[0]["Артикул"] == "A1")

        section("read_changes(): SelectChanges + NotifyChangesReceived (FR-1С.3)")
        Fake1COData.calls.clear()
        c2 = OneCODataConnector(base_url, exchange_point="ExchangePlan_X(guid'y')")
        batch = c2.read_changes("Catalog_Номенклатура", Cursor(value=""))
        check("изменения разобраны из atom-feed", len(batch.records) == 1
             and batch.records[0]["Ref_Key"] == "g2")
        check("удалённые записи разобраны (tombstone)", batch.deletes == ["g-deleted-1"])
        check("курсор продвинулся", batch.next_cursor.value == "1")
        select_calls = [call for call in Fake1COData.calls if "SelectChanges" in call]
        notify_calls = [call for call in Fake1COData.calls if "NotifyChangesReceived" in call]
        check("SelectChanges вызван с DataExchangePoint и MessageNo",
             len(select_calls) == 1 and "DataExchangePoint=" in select_calls[0])
        check("NotifyChangesReceived вызван для подтверждения курсора (иначе "
             "повторный SelectChanges вернул бы те же данные)", len(notify_calls) == 1)

        section("read_changes(): без exchange_point -> OneCODataError")
        c3 = OneCODataConnector(base_url)
        try:
            c3.read_changes("Catalog_Номенклатура", Cursor(value=""))
            check("без exchange_point кидает OneCODataError", False)
        except OneCODataError as exc:
            check("без exchange_point кидает OneCODataError", True)
            check("сообщение объясняет что нужно настроить", "exchange_point" in str(exc))

        section("read_changes(): ошибка SelectChanges на сервере -> OneCODataError")
        Fake1COData.fail_select_changes = True
        try:
            c2.read_changes("Catalog_Номенклатура", Cursor(value=""))
            check("HTTP 500 от SelectChanges -> OneCODataError", False)
        except OneCODataError:
            check("HTTP 500 от SelectChanges -> OneCODataError", True)
        finally:
            Fake1COData.fail_select_changes = False

        section("write_back(): PATCH с If-Match, обработка HTTP 412 (FR-1С.5)")
        result = c.write_back("Catalog_Номенклатура", [
            WriteRecord(natural_key="g1",
                       payload={"Наименование": "Новое имя", "DataVersion": "NEWVERSION"}),
            WriteRecord(natural_key="g2",
                       payload={"Наименование": "X", "DataVersion": "OLDVERSION"}),
        ])
        check("частичный успех: ok=False из-за одной ошибки", result.ok is False)
        check("успешная запись учтена в written", result.written == 1)
        check("конфликт версий (412) зафиксирован как построчная ошибка, "
             "не как исключение", len(result.errors) == 1 and "412" in result.errors[0])

        section("Аутентификация: Basic/Bearer (FR-1С.6)")
        Fake1COData.require_auth = True
        try:
            c_noauth = OneCODataConnector(base_url)
            try:
                c_noauth.discover()
                check("запрос без учётных данных отклонён сервером (401 -> ошибка)", False)
            except OneCODataError:
                check("запрос без учётных данных отклонён сервером (401 -> ошибка)", True)

            c_basic = OneCODataConnector(base_url, username="u", password="p")
            schemas_auth = c_basic.discover()
            check("Basic-аутентификация проходит", len(schemas_auth) == 1)

            c_bearer = OneCODataConnector(base_url, token="secret-token")
            schemas_bearer = c_bearer.discover()
            check("Bearer-аутентификация проходит", len(schemas_bearer) == 1)
        finally:
            Fake1COData.require_auth = False

        section("Сетевая ошибка: недоступный сервер -> OneCODataError")
        c_dead = OneCODataConnector("http://127.0.0.1:1", timeout=1)
        try:
            c_dead.discover()
            check("недоступный сервер кидает OneCODataError", False)
        except OneCODataError:
            check("недоступный сервер кидает OneCODataError", True)

    finally:
        srv.shutdown()
        thread.join(timeout=5)

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
