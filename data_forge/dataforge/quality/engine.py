"""Quality Engine (ТЗ: "Движок качества данных" §1.1, K2).

Два независимых блока:

  profile_dataset()  — автоматическое статистическое профилирование
                       Bronze-слоя: total/null/distinct/min/max/примеры
                       по каждому полю. Основа для того, чтобы человек
                       (или AI Copilot) мог предложить осмысленные
                       правила качества, не разглядывая сырые данные
                       вручную.

  run_quality_checks() — применяет активные `quality_rule` к каждой
                       Bronze-записи датасета. Запись, нарушившая ХОТЯ
                       БЫ ОДНО правило с severity="error", уходит в
                       карантин (`quarantine_record`) и НЕ продвигается
                       в Silver. Нарушения severity="warning" не
                       блокируют продвижение, но фиксируются в отчёте
                       (`quality_result`) — видно, что не идеально, но
                       не критично.

Правила настраиваются декларативно (dict `params`), без кода:
  not_null        — {} (поле обязано быть непустым и не null)
  unique          — {} (для набора Bronze-записей значение не повторяется)
  regex           — {"pattern": "..."}
  range           — {"min": ..., "max": ...} (любое из двух опционально)
  allowed_values  — {"values": [...]}

Чисто детерминированная логика на Python — без Great Expectations
(решение пользователя "минимум зависимостей"), но семантика правил
намеренно подобна GX-expectations, чтобы миграция была прямолинейной,
если объём проекта вырастет за пределы MVP.
"""
from __future__ import annotations

import re
from typing import Any

from ..db.store import Store


class QualityError(RuntimeError):
    """Ошибка Quality Engine: неверные параметры правила и т.п."""


_RULE_TYPES = ("not_null", "unique", "regex", "range", "allowed_values")


def _get_field(payload: dict[str, Any], field_name: str) -> Any:
    return payload.get(field_name)


def profile_dataset(store: Store, dataset_id: int) -> list[dict[str, Any]]:
    """Профилирует ВСЕ поля, встречающиеся хотя бы в одной Bronze-записи
    датасета. Возвращает список профилей (то же, что вернёт
    `store.list_profiles` после вызова)."""
    records = store.list_bronze(dataset_id)
    field_names: set[str] = set()
    for rec in records:
        field_names.update(rec["payload"].keys())

    profiles = []
    for field_name in sorted(field_names):
        values = [_get_field(r["payload"], field_name) for r in records]
        non_null = [v for v in values if v is not None and v != ""]
        distinct = set(_stringify(v) for v in non_null)
        samples = [v for v in non_null[:5]]
        min_v = min((_stringify(v) for v in non_null), default="")
        max_v = max((_stringify(v) for v in non_null), default="")
        store.upsert_profile(
            dataset_id, field_name,
            total_count=len(values), null_count=len(values) - len(non_null),
            distinct_count=len(distinct), min_value=min_v, max_value=max_v,
            sample_values=samples)
        profiles.append({
            "field_name": field_name, "total_count": len(values),
            "null_count": len(values) - len(non_null),
            "distinct_count": len(distinct), "min_value": min_v,
            "max_value": max_v, "sample_values": samples,
        })
    return profiles


def _stringify(v: Any) -> str:
    return str(v)


def _check_not_null(value: Any, params: dict[str, Any]) -> tuple[bool, str]:
    ok = value is not None and value != ""
    return ok, "" if ok else "значение пустое или отсутствует"


def _check_regex(value: Any, params: dict[str, Any]) -> tuple[bool, str]:
    pattern = params.get("pattern", "")
    if not pattern:
        raise QualityError("Правило regex требует параметр 'pattern'")
    if value is None:
        return False, "значение отсутствует (regex не может совпасть)"
    ok = re.fullmatch(pattern, str(value)) is not None
    return ok, "" if ok else f"'{value}' не соответствует шаблону {pattern!r}"


def _check_range(value: Any, params: dict[str, Any]) -> tuple[bool, str]:
    if value is None or value == "":
        return False, "значение отсутствует"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return False, f"'{value}' не число"
    lo, hi = params.get("min"), params.get("max")
    if lo is not None and num < lo:
        return False, f"{num} < min={lo}"
    if hi is not None and num > hi:
        return False, f"{num} > max={hi}"
    return True, ""


def _check_allowed_values(value: Any, params: dict[str, Any]) -> tuple[bool, str]:
    allowed = params.get("values", [])
    ok = value in allowed
    return ok, "" if ok else f"'{value}' не входит в допустимые {allowed}"


_CHECKERS = {
    "not_null": _check_not_null,
    "regex": _check_regex,
    "range": _check_range,
    "allowed_values": _check_allowed_values,
}


def evaluate_payload(store: Store, dataset_id: int,
                     payload: dict[str, Any]) -> list[str]:
    """Проверяет ОДИН payload против активных правил датасета (кроме
    `unique`, которое по определению требует контекста всего набора
    записей — здесь неприменимо и пропускается). Возвращает список
    нарушений severity="error" (пустой список — payload валиден).

    Используется Process Orchestrator (см. `pipeline/orchestrator.py`)
    как guardrail ПОСЛЕ ручной корректировки записи из карантина: прежде
    чем считать процесс завершённым и делать write-back, повторно
    проверяем те же правила, что отправили запись в карантин — если
    исправление не устранило нарушение, процесс не должен молча продолжаться."""
    rules = store.list_quality_rules(dataset_id, active_only=True)
    errors: list[str] = []
    for rule in rules:
        if rule["rule_type"] == "unique":
            continue
        if rule["rule_type"] not in _RULE_TYPES:
            raise QualityError(f"Неизвестный тип правила: {rule['rule_type']}")
        field_value = _get_field(payload, rule["field_name"]) if rule["field_name"] else None
        checker = _CHECKERS[rule["rule_type"]]
        passed, detail = checker(field_value, rule["params"])
        if not passed and rule["severity"] == "error":
            errors.append(f"{rule['field_name'] or '<запись>'}: {rule['rule_type']} — {detail}")
    return errors


def run_quality_checks(store: Store, dataset_id: int) -> dict[str, Any]:
    """Прогоняет все активные правила по всем Bronze-записям датасета.
    Записи без нарушений severity="error" продвигаются в Silver
    (append-only: повторный запуск создаёт НОВЫЕ silver_record — история
    прогонов не перетирается, что важно для lineage). Записи с хотя бы
    одним нарушением severity="error" уходят в карантин."""
    rules = store.list_quality_rules(dataset_id, active_only=True)
    records = store.list_bronze(dataset_id)
    if not rules:
        # Без правил всё продвигается "как есть" — платформа не должна
        # блокировать данные, для которых ещё не настроено качество.
        run_id = store.start_quality_run(dataset_id)
        promoted = 0
        for rec in records:
            store.insert_silver(dataset_id, rec["id"], rec["payload"], run_id)
            promoted += 1
        store.finish_quality_run(run_id, 0, len(records), 0, 0, promoted)
        return {"run_id": run_id, "rules_checked": 0,
               "records_checked": len(records), "violations_count": 0,
               "quarantined_count": 0, "promoted_count": promoted}

    for rule in rules:
        if rule["rule_type"] not in _RULE_TYPES:
            raise QualityError(f"Неизвестный тип правила: {rule['rule_type']}")

    unique_seen: dict[int, set[str]] = {}   # rule_id -> увиденные значения

    run_id = store.start_quality_run(dataset_id)
    violations = 0
    quarantined = 0
    promoted = 0

    for rec in records:
        record_errors: list[str] = []
        record_warnings: list[str] = []
        for rule in rules:
            field_value = (_get_field(rec["payload"], rule["field_name"])
                           if rule["field_name"] else None)
            if rule["rule_type"] == "unique":
                seen = unique_seen.setdefault(rule["id"], set())
                key = _stringify(field_value)
                passed = key not in seen or field_value is None
                seen.add(key)
                detail = "" if passed else f"дублирующееся значение '{field_value}'"
            else:
                checker = _CHECKERS[rule["rule_type"]]
                passed, detail = checker(field_value, rule["params"])

            store.insert_quality_result(run_id, rule["id"], rec["id"], passed, detail)
            if not passed:
                violations += 1
                label = f"{rule['field_name'] or '<запись>'}: {rule['rule_type']} — {detail}"
                if rule["severity"] == "error":
                    record_errors.append(label)
                else:
                    record_warnings.append(label)

        if record_errors:
            store.insert_quarantine(dataset_id, rec["id"], record_errors, run_id)
            quarantined += 1
        else:
            store.insert_silver(dataset_id, rec["id"], rec["payload"], run_id)
            promoted += 1

    store.finish_quality_run(run_id, len(rules), len(records), violations,
                             quarantined, promoted)
    return {"run_id": run_id, "rules_checked": len(rules),
           "records_checked": len(records), "violations_count": violations,
           "quarantined_count": quarantined, "promoted_count": promoted}
