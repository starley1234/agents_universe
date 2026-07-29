"""MDM: вероятностный матчинг дублей и сборка «золотой записи» (K1, ТЗ §3.3).

Три шага, каждый вызывается независимо (не единый "чёрный ящик"):

  find_match_candidates() — попарное сравнение Silver-записей одного
                            entity_type по набору полей (нечёткое
                            сравнение строк через rapidfuzz + точное для
                            остальных типов), создаёт `match_candidate`
                            со score в [0..1]. score >= cfg.
                            match_auto_threshold — кандидат в дальнейшем
                            может быть авто-подтверждён (merge_candidate
                            с auto=True); score в диапазоне
                            [match_review_threshold, auto_threshold) —
                            остаётся в stewardship-очереди
                            (`list_match_candidates(decision="pending")`)
                            для решения человеком; ниже review_threshold
                            — вообще не создаётся как кандидат (шум).

  merge_candidate()       — превращает подтверждённый кандидат в
                            golden record: если A уже привязана к Gold-
                            записи — B присоединяется к ней и наоборот;
                            если обе новые — создаётся новая Gold-запись
                            через survivorship (см. ниже). Возможен
                            auto-merge (агентское решение) и ручное
                            подтверждение (stewardship).

  apply_survivorship()    — для КАЖДОГО поля выбирает значение по
                            приоритету источников (`survivorship_rule`);
                            если правило для поля не задано — берёт
                            первое непустое значение (детерминированный
                            fallback, не "молча теряем поле").

rapidfuzz — единственная сторонняя лёгкая библиотека здесь (по решению
пользователя "минимум зависимостей + отдельные лёгкие библиотеки при
необходимости"), импортируется лениво.
"""
from __future__ import annotations

from itertools import combinations
from typing import Any

from ..db.store import Store


class MdmError(RuntimeError):
    """Ошибка MDM: неверные параметры сопоставления, статус кандидата и т.п."""


def _require_rapidfuzz():
    try:
        from rapidfuzz import fuzz  # type: ignore
    except ImportError as exc:
        raise MdmError(
            "Матчинг требует rapidfuzz. Установите: pip install rapidfuzz"
        ) from exc
    return fuzz


def _field_similarity(a: Any, b: Any, fuzz) -> float:
    if a is None or b is None or a == "" or b == "":
        return 0.0
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if a == b:
            return 1.0
        denom = max(abs(a), abs(b), 1e-9)
        return max(0.0, 1.0 - abs(a - b) / denom)
    # Регистр не должен влиять на сходство ("ООО Ромашка" и "ооо ромашка" —
    # один и тот же контрагент, а не разные) — нормализуем перед сравнением,
    # как это принято в практике вероятностного матчинга (Splink и т.п.).
    return fuzz.token_sort_ratio(str(a).lower(), str(b).lower()) / 100.0


def compare_records(a_payload: dict[str, Any], b_payload: dict[str, Any],
                    fields: list[str], weights: dict[str, float] | None = None
                    ) -> float:
    """Взвешенное среднее сходства по перечисленным полям, в [0..1].
    Без rapidfuzz для чисто числовых/пустых полей отработает и без него,
    но обычно нужен для текстовых полей (названия, ИНН с опечатками)."""
    fuzz = _require_rapidfuzz()
    weights = weights or {f: 1.0 for f in fields}
    total_weight = sum(weights.get(f, 1.0) for f in fields)
    if total_weight == 0:
        raise MdmError("Сумма весов полей для сравнения не может быть 0")
    score = 0.0
    for f in fields:
        sim = _field_similarity(a_payload.get(f), b_payload.get(f), fuzz)
        score += sim * weights.get(f, 1.0)
    return score / total_weight


def find_match_candidates(store: Store, entity_type: str, dataset_id: int,
                          fields: list[str], review_threshold: float,
                          weights: dict[str, float] | None = None
                          ) -> list[dict[str, Any]]:
    """Попарно сравнивает ВСЕ Silver-записи датасета (O(n^2) — приемлемо
    для MVP на десятках-сотнях записей; для промышленного объёма
    потребовался бы блокинг/индексация, сознательно не реализовано, см.
    README.md). Создаёт match_candidate для пар с score >= review_threshold."""
    records = store.list_silver(dataset_id)
    created = []
    for rec_a, rec_b in combinations(records, 2):
        score = compare_records(rec_a["payload"], rec_b["payload"], fields, weights)
        if score >= review_threshold:
            cid = store.create_match_candidate(
                entity_type, rec_a["id"], rec_b["id"], score)
            created.append(store.get_match_candidate(cid))
    return created


def apply_survivorship(store: Store, entity_type: str,
                       payloads_by_source: list[tuple[str, dict[str, Any]]]
                       ) -> dict[str, Any]:
    """payloads_by_source: [(source_name, payload), ...] — все сырые
    представления объекта, которые сливаются в одну золотую запись.
    Для каждого поля выбирает значение источника с наивысшим приоритетом
    (source_priority[0] — самый приоритетный); если правило для поля не
    задано или ни один приоритетный источник не дал значения — берётся
    первое непустое встреченное значение (не молчаливая потеря поля)."""
    rules = store.survivorship_rules_for(entity_type)
    all_fields: set[str] = set()
    for _, payload in payloads_by_source:
        all_fields.update(payload.keys())

    result: dict[str, Any] = {}
    for field_name in sorted(all_fields):
        priority = rules.get(field_name, [])
        value = None
        for src_name in priority:
            for s, payload in payloads_by_source:
                if s == src_name and payload.get(field_name) not in (None, ""):
                    value = payload[field_name]
                    break
            if value is not None:
                break
        if value is None:
            for _, payload in payloads_by_source:
                if payload.get(field_name) not in (None, ""):
                    value = payload[field_name]
                    break
        result[field_name] = value
    return result


def merge_candidate(store: Store, candidate_id: int, entity_type: str,
                    decided_by: str, auto: bool = False) -> int:
    """Подтверждает кандидат на дубль и сливает записи в golden record.
    Возвращает id golden-записи. Идемпотентно относительно повторного
    вызова на уже решённом кандидате — бросает MdmError, чтобы не
    затирать предыдущее решение молча (нужно явно проверять статус)."""
    cand = store.get_match_candidate(candidate_id)
    if not cand:
        raise MdmError(f"Кандидат #{candidate_id} не найден")
    if cand["decision"] != "pending":
        raise MdmError(
            f"Кандидат #{candidate_id} уже решён ({cand['decision']}) — "
            "повторное слияние не выполняется")

    rec_a = store.get_silver(cand["record_a_id"])
    rec_b = store.get_silver(cand["record_b_id"])
    if not rec_a or not rec_b:
        raise MdmError("Одна из Silver-записей кандидата не найдена")

    gold_a = store.gold_for_silver(rec_a["id"])
    gold_b = store.gold_for_silver(rec_b["id"])

    ds_a = store.get_dataset(rec_a["dataset_id"])
    ds_b = store.get_dataset(rec_b["dataset_id"])
    src_a = store.get_source(ds_a["source_id"])["name"] if ds_a else ""
    src_b = store.get_source(ds_b["source_id"])["name"] if ds_b else ""

    if gold_a and gold_b and gold_a != gold_b:
        raise MdmError(
            f"Записи уже принадлежат РАЗНЫМ золотым записям ({gold_a} и "
            f"{gold_b}) — слияние двух golden entity не реализовано в MVP")

    if gold_a:
        gold_id = gold_a
        existing = store.get_gold_entity(gold_id)
        attrs = apply_survivorship(
            store, entity_type,
            [(src_a, existing["attributes"]), (src_b, rec_b["payload"])])
        store.update_gold_attributes(gold_id, attrs)
    elif gold_b:
        gold_id = gold_b
        existing = store.get_gold_entity(gold_id)
        attrs = apply_survivorship(
            store, entity_type,
            [(src_b, existing["attributes"]), (src_a, rec_a["payload"])])
        store.update_gold_attributes(gold_id, attrs)
    else:
        attrs = apply_survivorship(
            store, entity_type, [(src_a, rec_a["payload"]), (src_b, rec_b["payload"])])
        gold_id = store.create_gold_entity(entity_type, attrs)

    store.link_source_record(gold_id, rec_a["dataset_id"], rec_a["id"], cand["score"])
    store.link_source_record(gold_id, rec_b["dataset_id"], rec_b["id"], cand["score"])

    decision = "auto_merged" if auto else "confirmed_match"
    store.set_match_decision(candidate_id, decision, decided_by, gold_id)

    for silver_id in (rec_a["id"], rec_b["id"]):
        store.add_lineage_edge(
            from_asset=f"silver:record:{silver_id}",
            to_asset=f"gold:entity:{gold_id}",
            transform_ref="mdm.matching.merge_candidate",
            run_ref=f"match_candidate:{candidate_id}")

    store.log_audit(
        decided_by, "merge_candidate", "gold_entity", gold_id,
        {"candidate_id": candidate_id, "score": cand["score"], "auto": auto,
         "record_a_id": rec_a["id"], "record_b_id": rec_b["id"]})
    return gold_id


def reject_candidate(store: Store, candidate_id: int, decided_by: str,
                     reason: str = "") -> bool:
    cand = store.get_match_candidate(candidate_id)
    if not cand:
        raise MdmError(f"Кандидат #{candidate_id} не найден")
    if cand["decision"] != "pending":
        raise MdmError(
            f"Кандидат #{candidate_id} уже решён ({cand['decision']})")
    ok = store.set_match_decision(candidate_id, "rejected", decided_by)
    store.log_audit(decided_by, "reject_candidate", "match_candidate",
                    candidate_id, {"reason": reason})
    return ok


def auto_merge_high_confidence(store: Store, entity_type: str,
                               auto_threshold: float,
                               decided_by: str = "system:mdm_auto") -> list[int]:
    """Проходит по всем pending-кандидатам заданного entity_type и
    автоматически сливает те, чей score >= auto_threshold (guardrail
    аналогичный erp_ai: явный численный порог, не "на глаз"). Кандидаты
    ниже порога остаются в stewardship-очереди для человека."""
    merged_ids = []
    for cand in store.list_match_candidates(decision="pending"):
        if cand["entity_type"] != entity_type:
            continue
        if cand["score"] >= auto_threshold:
            gold_id = merge_candidate(store, cand["id"], entity_type,
                                      decided_by, auto=True)
            merged_ids.append(gold_id)
    return merged_ids
