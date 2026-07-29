"""Ontology: бизнес-язык поверх сырых таблиц (ТЗ §3.2).

Идея раздела: пользователи и процессы должны работать с понятными
бизнес-объектами ("Контрагент", "Деталь", "Заказ") и их связями, а не с
`gold_entity`/`silver_record` напрямую. Этот модуль — материализующий
слой ПОВЕРХ уже готового MDM (`dataforge/mdm/matching.py`): он НЕ
подменяет golden record, а даёт ему понятное имя типа и извлекает
типизированные атрибуты по объявленной схеме.

Два источника ObjectInstance:
  1. Материализация из golden record (`materialize_from_gold()`) — типовой
     путь: ObjectType привязан к `gold_entity_type` через
     `Store.upsert_object_type(gold_entity_type=...)`, экземпляр создаётся
     или обновляется по мере появления/изменения golden record.
  2. Прямое создание (`Store.create_object_instance(gold_entity_id=None)`)
     — для объектов, у которых пока нет источника данных (ручной ввод,
     объекты без интеграции), например "ПроизводственнаяОперация".

Валидация атрибутов (`validate_attributes()`) — против объявленной
`attributes_schema` ObjectType (`required`/`type`), чтобы неправильно
сформированный объект не создавался молча — та же философия guardrails,
что в остальном репозитории (явная ошибка вместо тихого дефекта).
"""
from __future__ import annotations

from typing import Any

from ..db.store import Store

_TYPE_CHECKERS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
}


class OntologyError(RuntimeError):
    """Ошибка Ontology: неизвестный тип, нарушение схемы атрибутов и т.п."""


def define_object_type(store: Store, name: str, gold_entity_type: str = "",
                       attributes_schema: list[dict[str, Any]] | None = None
                       ) -> dict[str, Any]:
    """Регистрирует (или обновляет) тип бизнес-объекта. `attributes_schema`
    — список `{"name":..., "type": "string"|"number"|"boolean", "required": bool}`."""
    for field in attributes_schema or []:
        if "name" not in field:
            raise OntologyError("Каждое поле attributes_schema обязано иметь 'name'")
        ftype = field.get("type", "string")
        if ftype not in _TYPE_CHECKERS:
            raise OntologyError(
                f"Неизвестный тип поля '{ftype}' у '{field['name']}' — "
                f"допустимы: {sorted(_TYPE_CHECKERS)}")
    otid = store.upsert_object_type(name, gold_entity_type, attributes_schema)
    store.log_audit("system:ontology", "define_object_type", "object_type", otid,
                    {"name": name, "gold_entity_type": gold_entity_type})
    return store.get_object_type(otid)


def validate_attributes(object_type: dict[str, Any],
                        attributes: dict[str, Any]) -> list[str]:
    """Возвращает список ошибок валидации (пустой список — валидно). Не
    бросает исключение сама — вызывающий код решает, как поступить с
    ошибками (например, только предупредить, а не заблокировать создание,
    если объект материализуется из уже существующих грязных данных)."""
    errors: list[str] = []
    for field in object_type["attributes_schema"]:
        name = field["name"]
        ftype = field.get("type", "string")
        required = field.get("required", False)
        if name not in attributes or attributes[name] in (None, ""):
            if required:
                errors.append(f"обязательное поле '{name}' отсутствует или пусто")
            continue
        checker = _TYPE_CHECKERS.get(ftype)
        if checker and not checker(attributes[name]):
            errors.append(
                f"поле '{name}' должно быть типа {ftype}, получено "
                f"{type(attributes[name]).__name__}")
    return errors


def materialize_from_gold(store: Store, gold_entity_id: int,
                          strict: bool = False) -> dict[str, Any]:
    """Создаёт или обновляет ObjectInstance из golden record. ObjectType
    определяется по `entity_type` золотой записи через `gold_entity_type`
    (должен быть предварительно объявлен через `define_object_type()`).

    strict=True — бросает OntologyError при нарушении схемы атрибутов;
    strict=False (по умолчанию) — материализует как есть, ошибки
    валидации доступны отдельно через `validate_attributes()` (типичный
    сценарий: данные из MDM могут быть неидеальны, Ontology не должна
    молча терять объект из-за одного отсутствующего необязательного
    поля, но обязана дать инструмент проверить полноту)."""
    gold = store.get_gold_entity(gold_entity_id)
    if not gold:
        raise OntologyError(f"Золотая запись #{gold_entity_id} не найдена")

    object_type = store.get_object_type_by_gold_entity_type(gold["entity_type"])
    if not object_type:
        raise OntologyError(
            f"Нет ObjectType, привязанного к gold_entity_type="
            f"'{gold['entity_type']}' — вызовите define_object_type() сначала")

    if strict:
        errors = validate_attributes(object_type, gold["attributes"])
        if errors:
            raise OntologyError(
                f"Атрибуты golden record #{gold_entity_id} нарушают схему "
                f"ObjectType '{object_type['name']}': {'; '.join(errors)}")

    existing = store.get_object_instance_by_gold(gold_entity_id)
    if existing:
        store.update_object_instance_attributes(existing["id"], gold["attributes"])
        store.log_audit("system:ontology", "resync_object_instance",
                        "object_instance", existing["id"],
                        {"gold_entity_id": gold_entity_id})
        return store.get_object_instance(existing["id"])

    instance_id = store.create_object_instance(
        object_type["id"], gold["attributes"], gold_entity_id=gold_entity_id)
    store.log_audit("system:ontology", "materialize_object_instance",
                    "object_instance", instance_id,
                    {"gold_entity_id": gold_entity_id,
                     "object_type": object_type["name"]})
    return store.get_object_instance(instance_id)


def link_instances(store: Store, link_type: str, from_instance_id: int,
                   to_instance_id: int, attributes: dict[str, Any] | None = None,
                   actor: str = "human:ontology") -> dict[str, Any]:
    """Создаёт типизированную связь между двумя ObjectInstance. Обе
    стороны обязаны существовать — иначе OntologyError вместо тихого
    создания связи в никуда (ссылочная целостность на прикладном
    уровне, до срабатывания FK в БД)."""
    if not store.get_object_instance(from_instance_id):
        raise OntologyError(f"ObjectInstance #{from_instance_id} не найден")
    if not store.get_object_instance(to_instance_id):
        raise OntologyError(f"ObjectInstance #{to_instance_id} не найден")
    link_id = store.create_object_link(link_type, from_instance_id, to_instance_id,
                                       attributes)
    store.log_audit(actor, "link_instances", "object_link", link_id,
                    {"link_type": link_type, "from": from_instance_id,
                     "to": to_instance_id})
    return {"id": link_id, "link_type": link_type,
           "from_instance_id": from_instance_id, "to_instance_id": to_instance_id}


def instance_neighborhood(store: Store, instance_id: int) -> dict[str, Any]:
    """Собирает для дашборда/API "карточку объекта": сам объект +
    исходящие/входящие связи + (если материализован из Gold) исходные
    Silver-записи, слившиеся в golden record — своего рода мини-lineage
    для конкретного объекта Ontology."""
    instance = store.get_object_instance(instance_id)
    if not instance:
        raise OntologyError(f"ObjectInstance #{instance_id} не найден")
    object_type = store.get_object_type(instance["object_type_id"])
    result: dict[str, Any] = {
        "instance": instance, "object_type": object_type,
        "outgoing_links": store.links_from(instance_id),
        "incoming_links": store.links_to(instance_id),
        "source_links": [],
    }
    if instance["gold_entity_id"]:
        result["source_links"] = store.links_for_gold(instance["gold_entity_id"])
    return result
