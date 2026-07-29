"""Actions: действия, которые можно выполнить над объектами Ontology
(ТЗ §3.2: "actions — действия, которые можно над ними выполнять,
например «скорректировать остаток», «согласовать»").

Архитектура сознательно узкая и явная — НЕ универсальный движок правил
или скриптов (это было бы избыточно для объёма этой сессии и небезопасно
без песочницы): каждый `handler` — это КОНКРЕТНАЯ python-функция,
зарегистрированная по строковому имени в `HANDLERS`. `ActionDef.handler`
в БД хранит это имя, а не код — выполнить можно только заранее
зарегистрированный, явно написанный обработчик. Добавление нового
действия — это код (`register_handler`), а не пользовательский ввод.

Каждый обработчик:
  - принимает (store, instance, params) и возвращает dict-результат;
  - обязан сам решить, что менять (обычно —
    `store.update_object_instance_attributes`), и вернуть значимую
    информацию для аудита;
  - НЕ обязан быть идемпотентным сам по себе — идемпотентность и
    журналирование обеспечивает `execute_action()` вокруг него (audit
    trail, единая точка отказа).

Пример реализованного действия — `correct_attribute` (ТЗ пример
"скорректировать остаток"): меняет ОДНО поле атрибутов объекта с
обязательным указанием причины (explainability на уровне действия, тот
же принцип, что и в erp_ai/ProcurementAgent).
"""
from __future__ import annotations

from typing import Any, Callable

from ..db.store import Store

ActionHandler = Callable[[Store, dict[str, Any], dict[str, Any]], dict[str, Any]]


class ActionError(RuntimeError):
    """Ошибка выполнения действия: неизвестный handler, неверные params и т.п."""


HANDLERS: dict[str, ActionHandler] = {}


def register_handler(name: str, fn: ActionHandler) -> None:
    """Регистрирует обработчик действия под именем `name`. Повторная
    регистрация того же имени — ошибка (не тихая перезапись чужого
    обработчика)."""
    if name in HANDLERS and HANDLERS[name] is not fn:
        raise ActionError(
            f"Обработчик '{name}' уже зарегистрирован другой функцией — "
            "повторная регистрация под тем же именем запрещена")
    HANDLERS[name] = fn


def _correct_attribute(store: Store, instance: dict[str, Any],
                       params: dict[str, Any]) -> dict[str, Any]:
    field = params.get("field")
    new_value = params.get("value")
    reason = params.get("reason", "")
    if not field:
        raise ActionError("correct_attribute требует params.field")
    if not reason:
        raise ActionError(
            "correct_attribute требует params.reason (явное обоснование "
            "правки — explainability)")
    attrs = dict(instance["attributes"])
    old_value = attrs.get(field)
    attrs[field] = new_value
    store.update_object_instance_attributes(instance["id"], attrs)
    return {"field": field, "old_value": old_value, "new_value": new_value,
           "reason": reason}


def _link_to(store: Store, instance: dict[str, Any],
            params: dict[str, Any]) -> dict[str, Any]:
    link_type = params.get("link_type")
    target_id = params.get("target_instance_id")
    if not link_type or not target_id:
        raise ActionError("link_to требует params.link_type и params.target_instance_id")
    target = store.get_object_instance(target_id)
    if not target:
        raise ActionError(f"Целевой ObjectInstance #{target_id} не найден")
    link_id = store.create_object_link(link_type, instance["id"], target_id,
                                       {k: v for k, v in params.items()
                                        if k not in ("link_type", "target_instance_id")})
    return {"link_id": link_id, "link_type": link_type, "target_instance_id": target_id}


register_handler("ontology.actions.correct_attribute", _correct_attribute)
register_handler("ontology.actions.link_to", _link_to)


def execute_action(store: Store, instance_id: int, action_name: str,
                   params: dict[str, Any], actor: str) -> dict[str, Any]:
    """Находит ActionDef объекта по имени, проверяет что handler
    зарегистрирован, выполняет его и пишет audit-запись независимо от
    успеха/неудачи (неудача тоже логируется — прежде чем бросить
    исключение выше, чтобы в аудите была видна попытка, а не только
    успешные действия)."""
    instance = store.get_object_instance(instance_id)
    if not instance:
        raise ActionError(f"ObjectInstance #{instance_id} не найден")

    action_def = store.get_action_def_by_name(instance["object_type_id"], action_name)
    if not action_def:
        raise ActionError(
            f"Действие '{action_name}' не определено для этого типа объекта "
            "— зарегистрируйте его через Store.create_action_def()")

    handler = HANDLERS.get(action_def["handler"])
    if handler is None:
        raise ActionError(
            f"Обработчик '{action_def['handler']}' не зарегистрирован в "
            "коде (actions.py) — ActionDef ссылается на несуществующий handler")

    try:
        result = handler(store, instance, params)
    except ActionError as exc:
        store.log_audit(actor, f"action_failed:{action_name}", "object_instance",
                        instance_id, {"error": str(exc), "params": params})
        raise

    store.log_audit(actor, f"action:{action_name}", "object_instance", instance_id,
                    {"params": params, "result": result})
    return result
