"""Тесты app.db.store: PostgreSQL-хранилище ERP AI — справочники,
документы снабжения, агентские предложения, неизменяемый аудит,
журнал синхронизации с 1С.

Проверяется на РЕАЛЬНОМ embedded PostgreSQL (pgserver) — общий кластер
на весь модуль, каждая тестовая функция получает СВОЮ базу (CREATE
DATABASE) для изоляции.

Требует psycopg и pgserver. Если их нет или не удалось поднять сервер —
модуль пропускается с понятным сообщением.
"""
from __future__ import annotations

import re
import sys
import tempfile
import uuid
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

_srv = None
if HAVE_DEPS:
    try:
        import pgserver  # type: ignore
        _tmp = tempfile.mkdtemp(prefix="erp_store_pgserver_")
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


def main() -> int:
    if not HAVE_DEPS:
        print(f"test_store: тесты пропущены — {SKIP_REASON}")
        return 0

    from app.db.store import Store, StoreError

    section("Store: подключение и схема")
    try:
        Store("")
        check("пустой DSN кидает StoreError", False)
    except StoreError as exc:
        check("пустой DSN кидает StoreError", True)
        check("сообщение упоминает DB_DSN", "DB_DSN" in str(exc))

    st = Store(_fresh_dsn())
    check("схема создана без ошибок", True)

    section("Nomenclature: CRUD и типы полей")
    nid = st.upsert_nomenclature("MTL-001", "Сталь листовая 2мм", unit="лист",
                                 min_stock=10, lead_time_days=5)
    check("id > 0", nid > 0)
    row = st.get_nomenclature(nid)
    check("min_stock возвращается как float, не Decimal",
         isinstance(row["min_stock"], float))
    check("значения совпадают", row["sku"] == "MTL-001" and row["unit"] == "лист")

    same_id = st.upsert_nomenclature("MTL-001", "Сталь листовая 2мм (обновлено)",
                                     unit="лист", min_stock=15)
    check("повторный upsert по тому же sku не создаёт дубль", same_id == nid)
    check("данные обновились", st.get_nomenclature(nid)["min_stock"] == 15.0)

    check("get_nomenclature_by_sku находит", st.get_nomenclature_by_sku("MTL-001") is not None)
    check("get_nomenclature для отсутствующего -> None", st.get_nomenclature(999999) is None)

    nid2 = st.upsert_nomenclature("PLK-002", "Плёнка упаковочная", unit="рулон")
    check("list_nomenclature видит обе позиции", len(st.list_nomenclature()) == 2)
    check("вторая номенклатура реально создана", nid2 != nid)

    section("Counterparty: CRUD, идентификация по (name, inn)")
    cid1 = st.upsert_counterparty("ООО Металлоснаб", inn="1234567890",
                                  reliability_score=0.9)
    cid2 = st.upsert_counterparty("ООО Металлоснаб", inn="0000000000")  # другой ИНН — другая запись
    check("разный ИНН создаёт отдельного контрагента", cid1 != cid2)
    same_cid = st.upsert_counterparty("ООО Металлоснаб", inn="1234567890",
                                      reliability_score=0.5)
    check("тот же (name, inn) — обновление, не дубль", same_cid == cid1)
    check("reliability_score обновился", st.get_counterparty(cid1)["reliability_score"] == 0.5)
    check("get_counterparty для отсутствующего -> None", st.get_counterparty(999999) is None)

    section("Counterparty: фильтр по kind")
    st.upsert_counterparty("Клиент ООО", kind="customer")
    suppliers = st.list_counterparties(kind="supplier")
    check("фильтр kind=supplier не включает customer",
         "Клиент ООО" not in {c["name"] for c in suppliers})

    section("Supplier price + сортировка по цене")
    st.set_supplier_price(nid, cid1, 1500.0)
    st.set_supplier_price(nid, cid2, 1400.0)
    prices = st.supplier_prices_for(nid)
    check("цены отсортированы по возрастанию", prices[0]["price"] <= prices[1]["price"])
    check("самая дешёвая цена первая", prices[0]["price"] == 1400.0)
    price_id_again = st.set_supplier_price(nid, cid1, 1600.0)
    check("повторный set_supplier_price обновляет, не дублирует",
         len(st.supplier_prices_for(nid)) == 2)
    check("цена обновилась", any(p["price"] == 1600.0 for p in st.supplier_prices_for(nid)))
    del price_id_again

    section("Stock balance и расчёт дефицита")
    st.set_stock(nid, 5, warehouse="Склад А")
    st.set_stock(nid, 3, warehouse="Склад Б", reserved=2)
    stock = st.stock_for(nid)
    check("остаток суммируется по складам", stock["quantity"] == 8.0)
    check("резерв учитывается", stock["reserved"] == 2.0)
    check("available = quantity - reserved", stock["available"] == 6.0)

    low = st.low_stock_nomenclature()
    check("MTL-001 в дефиците (available 6 < min_stock 15)",
         any(x["sku"] == "MTL-001" for x in low))
    check("PLK-002 без остатков и без min_stock не в дефиците (0 < 0 неверно)",
         not any(x["sku"] == "PLK-002" for x in low))

    section("Purchase request: lifecycle")
    prid = st.create_purchase_request(nid, 20, reason="тест", created_by="human")
    pr = st.get_purchase_request(prid)
    check("quantity — float, не Decimal", isinstance(pr["quantity"], float))
    check("статус по умолчанию open", pr["status"] == "open")
    check("list_purchase_requests(status='open') находит", 
         any(r["id"] == prid for r in st.list_purchase_requests(status="open")))
    ok = st.set_purchase_request_status(prid, "cancelled")
    check("статус обновлён", ok and st.get_purchase_request(prid)["status"] == "cancelled")

    section("Purchase order: создание со строками, привязка к заявке")
    prid2 = st.create_purchase_request(nid, 20, created_by="human")
    poid = st.create_purchase_order(
        cid2, [{"nomenclature_id": nid, "quantity": 20, "price": 1400.0,
               "purchase_request_id": prid2}],
        created_by="human", status="draft")
    po = st.get_purchase_order(poid)
    check("total_amount посчитан верно", po["total_amount"] == 28000.0)
    check("строки заказа присутствуют", len(po["lines"]) == 1)
    check("строка ссылается на правильную номенклатуру", po["lines"][0]["sku"] == "MTL-001")
    check("привязанная заявка получила статус ordered",
         st.get_purchase_request(prid2)["status"] == "ordered")
    check("get_purchase_order для отсутствующего -> None", st.get_purchase_order(999999) is None)

    ok2 = st.set_purchase_order_status(poid, "approved", approved_by="human:ivanov")
    check("статус и approved_by обновлены", ok2)
    po2 = st.get_purchase_order(poid)
    check("approved_by сохранён", po2["approved_by"] == "human:ivanov")

    check("list_purchase_orders(status=) фильтрует",
         all(o["status"] == "approved" for o in st.list_purchase_orders(status="approved")))

    deleted = st.delete_purchase_order(poid)
    check("delete_purchase_order удаляет (для rollback)", deleted)
    check("после удаления заказ не находится", st.get_purchase_order(poid) is None)
    check("повторное удаление -> False", st.delete_purchase_order(poid) is False)

    section("Agent proposal: создание, решения")
    propid = st.create_proposal(
        "procurement", "create_purchase_order", {"x": 1}, "обоснование",
        "draft", sources=[{"type": "stock", "id": nid}])
    prop = st.get_proposal(propid)
    check("статус по умолчанию pending", prop["status"] == "pending")
    check("payload сохранён как dict", prop["payload"] == {"x": 1})
    check("sources сохранены", prop["sources"] == [{"type": "stock", "id": nid}])
    check("get_proposal для отсутствующего -> None", st.get_proposal(999999) is None)

    ok3 = st.set_proposal_decision(propid, "approved", "human:x",
                                   result_document_type="purchase_order",
                                   result_document_id=42)
    check("решение зафиксировано", ok3)
    prop2 = st.get_proposal(propid)
    check("decided_by и результат сохранены",
         prop2["decided_by"] == "human:x" and prop2["result_document_id"] == 42)
    check("decided timestamp выставлен", prop2["decided"] is not None)

    check("list_proposals(status=) фильтрует",
         all(p["status"] == "approved" for p in st.list_proposals(status="approved")))

    section("Audit log: неизменяемость на уровне API (только INSERT)")
    audit_id = st.log_audit("human:test", "test_action", "nomenclature", nid,
                            {"detail": "проверка"})
    check("audit id > 0", audit_id > 0)
    trail = st.audit_trail_for("nomenclature", nid)
    check("audit_trail_for находит запись", any(a["id"] == audit_id for a in trail))
    check("В классе Store НЕТ метода update/delete для audit_log",
         not hasattr(st, "update_audit") and not hasattr(st, "delete_audit"))
    recent = st.recent_audit(limit=5)
    check("recent_audit возвращает записи в порядке убывания id",
         recent == sorted(recent, key=lambda a: -a["id"]))

    section("OneC sync log: идемпотентность")
    log_id, is_new = st.onec_log_attempt("to_1c", "purchase_order", nid, "key-abc")
    check("первая попытка — новая запись", is_new)
    log_id2, is_new2 = st.onec_log_attempt("to_1c", "purchase_order", nid, "key-abc")
    check("повторная попытка с тем же ключом -> не новая", not is_new2)
    check("id одинаковый (та же запись)", log_id == log_id2)
    st.onec_mark_result(log_id, "ok", external_id="1c-guid-1")
    logs = st.onec_sync_log_list()
    check("статус и external_id обновлены",
         any(x["id"] == log_id and x["status"] == "ok" and x["external_id"] == "1c-guid-1"
            for x in logs))
    check("attempts увеличился", any(x["id"] == log_id and x["attempts"] == 1 for x in logs))
    check("onec_sync_log_list(status=) фильтрует",
         all(x["status"] == "ok" for x in st.onec_sync_log_list(status="ok")))

    section("Dashboard stats: агрегированная статистика")
    stats = st.dashboard_stats()
    check("nomenclature >= 2", stats["nomenclature"] >= 2)
    check("counterparties >= 3", stats["counterparties"] >= 3)
    check("audit_entries >= 1", stats["audit_entries"] >= 1)
    check("low_stock_items совпадает с low_stock_nomenclature",
         stats["low_stock_items"] == len(st.low_stock_nomenclature()))

    st.close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
