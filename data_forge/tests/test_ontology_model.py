"""Тесты dataforge.ontology.model: определение типов бизнес-объектов,
валидация схемы атрибутов, материализация из golden record, связи
между объектами, сбор "карточки объекта" (K1 + Ontology, ТЗ §3.2).

Реальный embedded PostgreSQL (pgserver).
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
        _tmp = tempfile.mkdtemp(prefix="forge_ontology_pgserver_")
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
        print(f"test_ontology_model: тесты пропущены — {SKIP_REASON}")
        return 0

    from dataforge.db.store import Store
    from dataforge.ontology.model import (
        OntologyError,
        define_object_type,
        instance_neighborhood,
        link_instances,
        materialize_from_gold,
        validate_attributes,
    )

    st = Store(_fresh_dsn())

    section("define_object_type: регистрация и валидация схемы")
    ot = define_object_type(st, "Контрагент", gold_entity_type="counterparty",
                            attributes_schema=[
                                {"name": "name", "type": "string", "required": True},
                                {"name": "inn", "type": "string", "required": True},
                            ])
    check("id > 0", ot["id"] > 0)
    check("gold_entity_type сохранён", ot["gold_entity_type"] == "counterparty")
    ot_same = define_object_type(st, "Контрагент", gold_entity_type="counterparty",
                                 attributes_schema=[
                                     {"name": "name", "type": "string", "required": True},
                                     {"name": "inn", "type": "string", "required": True},
                                 ])
    check("повторное определение того же имени — upsert, не дубль",
         ot_same["id"] == ot["id"])
    check("list_object_types видит один тип", len(st.list_object_types()) == 1)
    check("upsert_object_type — ПОЛНАЯ замена схемы (тот же принцип, что "
         "upsert_source для config) — без attributes_schema схема обнулилась бы",
         ot_same["attributes_schema"] == ot["attributes_schema"])

    try:
        define_object_type(st, "Плохой", attributes_schema=[{"name": "x", "type": "wat"}])
        check("неизвестный тип поля -> OntologyError", False)
    except OntologyError as exc:
        check("неизвестный тип поля -> OntologyError", True)
        check("сообщение упоминает неизвестный тип", "wat" in str(exc))

    try:
        define_object_type(st, "БезИмени", attributes_schema=[{"type": "string"}])
        check("поле без 'name' -> OntologyError", False)
    except OntologyError:
        check("поле без 'name' -> OntologyError", True)

    section("validate_attributes: обязательные поля и типы")
    errors_ok = validate_attributes(ot, {"name": "ООО Ромашка", "inn": "123"})
    check("валидные атрибуты -> без ошибок", errors_ok == [])
    errors_missing = validate_attributes(ot, {"name": "ООО Ромашка"})
    check("отсутствие обязательного поля -> ошибка", len(errors_missing) == 1
         and "inn" in errors_missing[0])
    errors_wrong_type = validate_attributes(ot, {"name": 12345, "inn": "123"})
    check("неверный тип поля -> ошибка", any("name" in e for e in errors_wrong_type))
    errors_optional_missing = validate_attributes(
        {"attributes_schema": [{"name": "opt", "type": "string", "required": False}]},
        {})
    check("отсутствие необязательного поля -> без ошибок", errors_optional_missing == [])

    section("materialize_from_gold: создание и повторная синхронизация")
    gold_id = st.create_gold_entity("counterparty", {"name": "ООО Ромашка", "inn": "1234567890"})
    instance = materialize_from_gold(st, gold_id)
    check("instance создан", instance["id"] > 0)
    check("gold_entity_id привязан", instance["gold_entity_id"] == gold_id)
    check("атрибуты скопированы из golden record", instance["attributes"]["name"] == "ООО Ромашка")
    check("audit-запись materialize создана",
         any(a["action"] == "materialize_object_instance"
            for a in st.audit_trail_for("object_instance", instance["id"])))

    st.update_gold_attributes(gold_id, {"name": "ООО Ромашка (изменено)", "inn": "1234567890"})
    instance2 = materialize_from_gold(st, gold_id)
    check("повторная материализация — ТОТ ЖЕ instance", instance2["id"] == instance["id"])
    check("атрибуты обновились из изменённой golden record",
         instance2["attributes"]["name"] == "ООО Ромашка (изменено)")
    check("audit-запись resync создана",
         any(a["action"] == "resync_object_instance"
            for a in st.audit_trail_for("object_instance", instance["id"])))

    section("materialize_from_gold: несуществующая golden record")
    try:
        materialize_from_gold(st, 999999)
        check("несуществующая golden record -> OntologyError", False)
    except OntologyError:
        check("несуществующая golden record -> OntologyError", True)

    section("materialize_from_gold: нет ObjectType для gold_entity_type")
    gold_unregistered = st.create_gold_entity("unregistered_entity_type", {"x": 1})
    try:
        materialize_from_gold(st, gold_unregistered)
        check("нет ObjectType для entity_type -> OntologyError", False)
    except OntologyError as exc:
        check("нет ObjectType для entity_type -> OntologyError", True)
        check("сообщение упоминает entity_type", "unregistered_entity_type" in str(exc))

    section("materialize_from_gold: strict=True блокирует невалидные данные")
    gold_invalid = st.create_gold_entity("counterparty", {"name": "Без ИНН"})
    try:
        materialize_from_gold(st, gold_invalid, strict=True)
        check("strict=True с невалидными атрибутами -> OntologyError", False)
    except OntologyError as exc:
        check("strict=True с невалидными атрибутами -> OntologyError", True)
        check("сообщение перечисляет нарушения", "inn" in str(exc))
    instance_lenient = materialize_from_gold(st, gold_invalid, strict=False)
    check("strict=False материализует несмотря на невалидность", instance_lenient["id"] > 0)

    section("link_instances: связь между объектами")
    ot_part = define_object_type(st, "Деталь", gold_entity_type="part")
    gold_part = st.create_gold_entity("part", {"sku": "A1"})
    instance_part = materialize_from_gold(st, gold_part)
    link = link_instances(st, "поставляет", instance["id"], instance_part["id"],
                          actor="human:tester")
    check("link создан", link["id"] > 0)
    check("link_type сохранён", link["link_type"] == "поставляет")
    check("audit-запись link создана",
         any(a["action"] == "link_instances"
            for a in st.audit_trail_for("object_link", link["id"])))

    try:
        link_instances(st, "x", 999999, instance_part["id"])
        check("несуществующий from_instance -> OntologyError", False)
    except OntologyError:
        check("несуществующий from_instance -> OntologyError", True)
    try:
        link_instances(st, "x", instance["id"], 999999)
        check("несуществующий to_instance -> OntologyError", False)
    except OntologyError:
        check("несуществующий to_instance -> OntologyError", True)

    section("instance_neighborhood: карточка объекта")
    nb = instance_neighborhood(st, instance["id"])
    check("instance включён", nb["instance"]["id"] == instance["id"])
    check("object_type включён", nb["object_type"]["id"] == ot["id"])
    check("outgoing_links содержит связь к детали",
         any(x["to_instance_id"] == instance_part["id"] for x in nb["outgoing_links"]))
    nb_part = instance_neighborhood(st, instance_part["id"])
    check("incoming_links детали видит связь от контрагента",
         any(x["from_instance_id"] == instance["id"] for x in nb_part["incoming_links"]))

    try:
        instance_neighborhood(st, 999999)
        check("несуществующий instance -> OntologyError", False)
    except OntologyError:
        check("несуществующий instance -> OntologyError", True)

    del ot_part

    st.close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
