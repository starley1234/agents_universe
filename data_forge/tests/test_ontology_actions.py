"""Тесты dataforge.ontology.actions: реестр обработчиков действий,
выполнение действий над объектами Ontology с обязательным audit trail
(в т.ч. на неудачные попытки), встроенные обработчики correct_attribute
и link_to.

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
        _tmp = tempfile.mkdtemp(prefix="forge_actions_pgserver_")
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
        print(f"test_ontology_actions: тесты пропущены — {SKIP_REASON}")
        return 0

    from dataforge.db.store import Store
    from dataforge.ontology.actions import (
        HANDLERS,
        ActionError,
        execute_action,
        register_handler,
    )
    from dataforge.ontology.model import define_object_type

    st = Store(_fresh_dsn())

    section("register_handler: реестр защищён от тихой перезаписи")
    check("correct_attribute зарегистрирован по умолчанию",
         "ontology.actions.correct_attribute" in HANDLERS)
    check("link_to зарегистрирован по умолчанию", "ontology.actions.link_to" in HANDLERS)

    def _dummy(store, instance, params):
        return {"ok": True}

    register_handler("test.dummy_handler", _dummy)
    check("новый обработчик регистрируется", HANDLERS["test.dummy_handler"] is _dummy)
    register_handler("test.dummy_handler", _dummy)  # та же функция — не ошибка
    check("повторная регистрация ТОЙ ЖЕ функции не ошибка",
         HANDLERS["test.dummy_handler"] is _dummy)

    def _other(store, instance, params):
        return {}

    try:
        register_handler("test.dummy_handler", _other)
        check("регистрация ДРУГОЙ функции под тем же именем -> ActionError", False)
    except ActionError:
        check("регистрация ДРУГОЙ функции под тем же именем -> ActionError", True)

    section("execute_action: полный цикл — correct_attribute")
    ot = define_object_type(st, "Контрагент", gold_entity_type="counterparty")
    gold_id = st.create_gold_entity("counterparty", {"name": "ООО Ромашка", "inn": "111"})
    instance_id = st.create_object_instance(ot["id"], {"name": "ООО Ромашка", "inn": "111"},
                                            gold_entity_id=gold_id)
    st.create_action_def(ot["id"], "correct_attribute",
                         "ontology.actions.correct_attribute")

    result = execute_action(st, instance_id, "correct_attribute",
                            {"field": "inn", "value": "999", "reason": "опечатка"},
                            actor="human:tester")
    check("результат содержит old_value/new_value", result["old_value"] == "111"
         and result["new_value"] == "999")
    check("атрибут реально обновился в БД",
         st.get_object_instance(instance_id)["attributes"]["inn"] == "999")
    check("audit-запись успешного действия создана",
         any(a["action"] == "action:correct_attribute"
            for a in st.audit_trail_for("object_instance", instance_id)))

    section("execute_action: correct_attribute без обязательных params")
    try:
        execute_action(st, instance_id, "correct_attribute",
                       {"field": "inn", "value": "x"}, actor="human:tester")
        check("без reason -> ActionError (explainability)", False)
    except ActionError as exc:
        check("без reason -> ActionError (explainability)", True)
        check("сообщение объясняет требование", "reason" in str(exc))
    check("audit-запись НЕУДАЧНОЙ попытки тоже создана (не только успех)",
         any(a["action"] == "action_failed:correct_attribute"
            for a in st.audit_trail_for("object_instance", instance_id)))
    check("атрибут НЕ изменился после неудачной попытки",
         st.get_object_instance(instance_id)["attributes"]["inn"] == "999")

    try:
        execute_action(st, instance_id, "correct_attribute", {"value": "x", "reason": "r"},
                       actor="human:tester")
        check("без field -> ActionError", False)
    except ActionError:
        check("без field -> ActionError", True)

    section("execute_action: несуществующий instance/действие")
    try:
        execute_action(st, 999999, "correct_attribute", {}, actor="human:x")
        check("несуществующий instance -> ActionError", False)
    except ActionError:
        check("несуществующий instance -> ActionError", True)

    try:
        execute_action(st, instance_id, "not_a_defined_action", {}, actor="human:x")
        check("не определённое для типа действие -> ActionError", False)
    except ActionError as exc:
        check("не определённое для типа действие -> ActionError", True)
        check("сообщение подсказывает зарегистрировать действие",
             "create_action_def" in str(exc))

    section("execute_action: action_def ссылается на незарегистрированный handler")
    st.create_action_def(ot["id"], "orphan_action", "no.such.handler")
    try:
        execute_action(st, instance_id, "orphan_action", {}, actor="human:x")
        check("несуществующий handler -> ActionError", False)
    except ActionError as exc:
        check("несуществующий handler -> ActionError", True)
        check("сообщение упоминает имя handler", "no.such.handler" in str(exc))

    section("execute_action: link_to создаёт связь между объектами")
    ot_part = define_object_type(st, "Деталь", gold_entity_type="part")
    gold_part = st.create_gold_entity("part", {"sku": "A1"})
    instance_part = st.create_object_instance(ot_part["id"], {"sku": "A1"},
                                              gold_entity_id=gold_part)
    st.create_action_def(ot["id"], "link_to", "ontology.actions.link_to")
    link_result = execute_action(st, instance_id, "link_to",
                                 {"link_type": "поставляет", "target_instance_id": instance_part},
                                 actor="human:tester")
    check("link_to вернул link_id", link_result["link_id"] > 0)
    check("связь реально создана", len(st.links_from(instance_id)) == 1)

    try:
        execute_action(st, instance_id, "link_to",
                       {"link_type": "x", "target_instance_id": 999999},
                       actor="human:tester")
        check("link_to с несуществующей целью -> ActionError", False)
    except ActionError:
        check("link_to с несуществующей целью -> ActionError", True)

    st.close()

    print(f"\n{'─' * 40}\nитого: {PASS} ok, {FAIL} fail")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
