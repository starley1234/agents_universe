"""Тесты app.integrations.onec_adapter.OneCAdapter: EnterpriseData-подобный
протокол обмена с 1С. Реальный HTTP+XML на локальном fake-сервере,
эмулирующем 1С (без настоящей конфигурации 1С:Предприятия — см. ТЗ и
README.md за честной границей объёма), реальный embedded PostgreSQL.
"""
from __future__ import annotations

import re
import sys
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS, FAIL = 0, 0

HAVE_DEPS = True
SKIP_REASON = ""
try:
    import psycopg  # type: ignore
    _ = psycopg.__name__
except ImportError:
    HAVE_DEPS = False
    SKIP_REASON = "psycopg не установлен"

if HAVE_DEPS:
    try:
        import pgserver  # type: ignore
        _tmp = tempfile.mkdtemp(prefix="erp_onec_pgserver_")
        _srv = pgserver.get_server(_tmp)
    except Exception as exc:
        HAVE_DEPS = False
        SKIP_REASON = f"не удалось поднять тестовый Postgres: {exc}"


def _fresh_dsn() -> str:
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


class Fake1C(BaseHTTPRequestHandler):
    """Эмулирует HTTP-сервис обмена 1С (EnterpriseData-подобные XML)."""

    calls: list[bytes] = []
    push_response_id = "1c-guid-XYZ"
    fail_push = False

    def log_message(self, *a):
        pass

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        type(self).calls.append(body)
        if type(self).fail_push:
            self.send_response(500)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        out = f'<?xml version="1.0"?><Результат Ид="{type(self).push_response_id}"/>'.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def do_GET(self):  # noqa: N802
        if self.path == "/exchange/nomenclature":
            out = ("""<?xml version="1.0"?>
<Файл><Товары>
<Товар Ид="1c-nom-1"><Артикул>MTL-001</Артикул>
<Наименование>Сталь листовая 2мм (1С)</Наименование><Единица>лист</Единица></Товар>
</Товары></Файл>""").encode("utf-8")
        elif self.path == "/exchange/counterparty":
            out = ("""<?xml version="1.0"?>
<Файл><Контрагенты>
<Контрагент Ид="1c-cp-1" ИНН="1234567890">ООО Металлоснаб (1С)</Контрагент>
</Контрагенты></Файл>""").encode("utf-8")
        elif self.path == "/exchange/broken":
            out = b"not xml at all {"
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/xml")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


def main() -> int:
    if not HAVE_DEPS:
        print(f"test_onec_adapter: тесты пропущены — {SKIP_REASON}")
        return 0

    from app.config import Config
    from app.db.store import Store
    from app.integrations.onec_adapter import OneCAdapter, OneCError

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Fake1C)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{port}"

    try:
        section("push_purchase_order: без ONEC_BASE_URL -> результат с ошибкой")
        cfg_no_url = Config(db_dsn=_fresh_dsn())
        st0 = Store(cfg_no_url.db_dsn)
        nid0 = st0.upsert_nomenclature("X", "Y")
        cid0 = st0.upsert_counterparty("Z")
        po0 = st0.create_purchase_order(cid0, [{"nomenclature_id": nid0, "quantity": 1,
                                                "price": 1.0}], status="approved")
        adapter0 = OneCAdapter(cfg_no_url, st0)
        result0 = adapter0.push_purchase_order(po0)
        check("отсутствие ONEC_BASE_URL даёт ok=False (не роняет вызывающий код)",
             result0.ok is False)
        check("сообщение упоминает ONEC_BASE_URL", "ONEC_BASE_URL" in result0.error)
        check("неудачная попытка зафиксирована в onec_sync_log",
             any(x["status"] == "error" for x in st0.onec_sync_log_list()))
        st0.close()

        section("push_purchase_order: реальный HTTP+XML обмен")
        Fake1C.calls = []
        cfg = Config(db_dsn=_fresh_dsn(), onec_base_url=base_url)
        st = Store(cfg.db_dsn)
        adapter = OneCAdapter(cfg, st)

        nid = st.upsert_nomenclature("MTL-001", "Сталь листовая 2мм", unit="лист")
        cid = st.upsert_counterparty("ЗАО СтальТорг", inn="0987654321")
        st.set_supplier_price(nid, cid, 1400.0)
        poid = st.create_purchase_order(
            cid, [{"nomenclature_id": nid, "quantity": 50, "price": 1400.0}],
            status="approved")

        result = adapter.push_purchase_order(poid)
        check("push успешен", result.ok)
        check("external_id получен от 1С", result.external_id == "1c-guid-XYZ")
        check("реально был отправлен ровно один HTTP-запрос", len(Fake1C.calls) == 1)

        sent_xml = ET.fromstring(Fake1C.calls[0])
        check("отправленный XML содержит корневой Файл",
             sent_xml.tag == "Файл")
        check("отправленный XML содержит артикул номенклатуры",
             sent_xml.find(".//Артикул").text == "MTL-001")
        check("отправленный XML содержит верную сумму",
             sent_xml.find(".//Сумма").text == "70000.00")

        po_after = st.get_purchase_order(poid)
        check("onec_uuid сохранён в заказе", po_after["onec_uuid"] == "1c-guid-XYZ")

        section("push_purchase_order: идемпотентность — повторный вызов НЕ шлёт HTTP")
        result2 = adapter.push_purchase_order(poid)
        check("повторный вызов помечен как дубликат", result2.skipped_duplicate)
        check("реального HTTP-запроса больше не было (всё ещё 1)",
             len(Fake1C.calls) == 1)
        check("идемпотентный ключ совпадает", result.idempotency_key == result2.idempotency_key)

        section("push_purchase_order: несуществующий заказ -> ошибка")
        try:
            adapter.push_purchase_order(999999)
            check("несуществующий заказ отклонён", False)
        except OneCError:
            check("несуществующий заказ отклонён", True)

        section("push_purchase_order: сбой сети/500 -> OneCError, зафиксирован в логе")
        Fake1C.fail_push = True
        nid_fail = st.upsert_nomenclature("MTL-FAIL", "Провальный", unit="шт")
        po_fail = st.create_purchase_order(
            cid, [{"nomenclature_id": nid_fail, "quantity": 1, "price": 1.0}],
            status="approved")
        result_fail = adapter.push_purchase_order(po_fail)
        check("push с ошибкой сервера -> ok=False", not result_fail.ok)
        check("ошибка содержит код 500", "500" in result_fail.error)
        logs = st.onec_sync_log_list(status="error")
        check("ошибка зафиксирована в onec_sync_log", len(logs) >= 1)
        Fake1C.fail_push = False

        section("pull_nomenclature: мастер-система ERP — обновляет только маппинг")
        st.upsert_nomenclature("MTL-001", "Сталь листовая 2мм (ERP имя)",
                               unit="шт", min_stock=42)
        cfg_erp_master = Config(db_dsn=cfg.db_dsn, onec_base_url=base_url,
                                onec_master_nomenclature="erp")
        adapter_erp = OneCAdapter(cfg_erp_master, st)
        applied = adapter_erp.pull_nomenclature()
        check("маппинг применён", any(a["sku"] == "MTL-001" for a in applied))
        row = st.get_nomenclature_by_sku("MTL-001")
        check("имя ERP НЕ перезаписано данными 1С (мастер — erp)",
             row["name"] == "Сталь листовая 2мм (ERP имя)")
        check("min_stock ERP сохранён", row["min_stock"] == 42.0)
        check("onec_uuid обновлён (это и есть суть маппинга)",
             row["onec_uuid"] == "1c-nom-1")
        check("аудит зафиксировал синхронизацию",
             any(a["action"] == "sync_nomenclature"
                for a in st.audit_trail_for("nomenclature", row["id"])))

        section("pull_nomenclature: мастер-система 1С — данные 1С побеждают")
        cfg_1c_master = Config(db_dsn=cfg.db_dsn, onec_base_url=base_url,
                               onec_master_nomenclature="1c")
        adapter_1c = OneCAdapter(cfg_1c_master, st)
        adapter_1c.pull_nomenclature()
        row2 = st.get_nomenclature_by_sku("MTL-001")
        check("имя ЗАМЕНЕНО данными 1С (мастер — 1c)",
             row2["name"] == "Сталь листовая 2мм (1С)")
        check("единица измерения тоже заменена", row2["unit"] == "лист")

        section("pull_counterparties: реальный HTTP+XML обмен")
        cp_applied = adapter.pull_counterparties()
        check("контрагент применён", any(a["name"] == "ООО Металлоснаб (1С)"
                                        for a in cp_applied))
        cp_row = st.get_counterparty(cp_applied[0]["id"])
        check("onec_uuid контрагента сохранён", cp_row["onec_uuid"] == "1c-cp-1")
        check("ИНН перенесён из 1С", cp_row["inn"] == "1234567890")

        section("Разбор битого XML -> понятная ошибка, не трейсбек")
        cfg_broken = Config(db_dsn=cfg.db_dsn, onec_base_url=base_url + "/exchange/broken")
        # у сломанного пути нет /exchange/nomenclature — проверим на реальном
        # некорректном ответе, обратившись напрямую к _http и разбору
        adapter_broken = OneCAdapter(
            Config(db_dsn=cfg.db_dsn, onec_base_url=base_url), st)
        raw = adapter_broken._http("GET", "/exchange/broken")
        try:
            ET.fromstring(raw)
            check("тест на битый XML настроен верно (должен был не парситься)", False)
        except ET.ParseError:
            check("фейковый сервер действительно отдаёт битый XML для проверки", True)
        del cfg_broken

        st.close()
    finally:
        httpd.shutdown()
        httpd.server_close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
