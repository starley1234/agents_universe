"""Process Orchestrator: сквозной процесс с обратной записью (ТЗ K3).

Единственный реализованный процесс — `quarantine_correction`:

  1. Запись данных нарушила правило качества и попала в карантин
     (`quality.engine.run_quality_checks`, см. README.md за границей
     объёма — карантин уже существовал, здесь он становится ТРИГГЕРОМ
     процесса, а не конечной точкой).
  2. Оркестратор создаёт `process_instance` + `task` ответственному
     (stewardship) — тот же паттерн очереди, что и в MDM
     (`match_candidate`), но для проблем качества, а не дублей.
  3. Человек (или AI Copilot от его имени) присылает исправленный
     payload — `submit_correction()` СНАЧАЛА повторно проверяет его
     теми же правилами качества, что отправили запись в карантин
     (`quality.engine.evaluate_payload`) — GUARDRAIL: если исправление
     не устраняет нарушение, процесс явно остаётся в состоянии
     "требует ещё одной попытки", а не продолжается молча.
  4. Если исправление валидно — Bronze-запись обновляется на месте
     (`Store.update_bronze_payload`, единственное место всего
     приложения, где Bronze не append-only — обосновано тем, что это
     ИСПРАВЛЕНИЕ ошибки источника, а не новая выгрузка), запись
     разрешается из карантина, и запускается write-back в источник.
  5. Write-back идёт через `Connector.write_back()` — то же интерфейсное
     обязательство, что и у ingest (ТЗ §3.4: "платформа не только
     читает, но и пишет обратно"), с идемпотентностью через
     `Store.write_back_log_attempt` (тот же принцип, что
     `onec_log_attempt` в erp_ai). Источники, не поддерживающие
     write_back (например `FileConnector`), приводят к явной ошибке
     `ConnectorCapabilityError`, а не к тихому "как будто записали".
  6. `rollback_process()` — отмена процесса ДО того, как write-back
     успешно завершился (после успешного write-back источник уже
     изменён, откат средствами платформы не имеет смысла — то же
     ограничение, что и в `ProcurementAgent.rollback_proposal`).

Все переходы состояния сопровождаются `Store.log_audit` — полная
history процесса видна и через `audit_trail_for("process_instance", id)`.
"""
from __future__ import annotations

from typing import Any

from ..connectors.base import Connector, WriteRecord
from ..db.store import Store
from ..quality.engine import evaluate_payload

PROCESS_TYPE = "quarantine_correction"


class ProcessError(RuntimeError):
    """Ошибка процесса: неверный переход состояния, guardrail и т.п."""


def start_quarantine_correction(store: Store, quarantine_id: int,
                                assignee: str = "", actor: str = "system"
                                ) -> dict[str, Any]:
    """Запускает (или возвращает уже существующий) процесс коррекции
    для записи карантина. Идемпотентно относительно повторного вызова
    на ТОЙ ЖЕ записи карантина — не плодит параллельные процессы на
    один и тот же предмет (проверка через
    `Store.find_open_process_for_subject`)."""
    quarantine = store.get_quarantine(quarantine_id)
    if not quarantine:
        raise ProcessError(f"Запись карантина #{quarantine_id} не найдена")
    if quarantine["resolved"]:
        raise ProcessError(
            f"Запись карантина #{quarantine_id} уже разрешена — процесс "
            "коррекции не требуется")

    existing = store.find_open_process_for_subject("quarantine_record", quarantine_id)
    if existing:
        return existing

    bronze = store.get_bronze(quarantine["bronze_record_id"])
    process_id = store.create_process_instance(
        PROCESS_TYPE, "quarantine_record", quarantine_id,
        context={"dataset_id": quarantine["dataset_id"],
                 "bronze_record_id": quarantine["bronze_record_id"],
                 "reasons": quarantine["reasons"],
                 "original_payload": bronze["payload"] if bronze else {}},
        created_by=actor)
    store.create_task(
        process_id, title=f"Исправить запись #{quarantine['bronze_record_id']}",
        description="Нарушения качества: " + "; ".join(quarantine["reasons"]),
        assignee=assignee)
    store.set_process_status(process_id, "awaiting_task")
    store.log_audit(actor, "start_process", "process_instance", process_id,
                    {"process_type": PROCESS_TYPE, "quarantine_id": quarantine_id})
    return store.get_process_instance(process_id)


def submit_correction(store: Store, process_id: int, corrected_payload: dict[str, Any],
                      actor: str) -> dict[str, Any]:
    """Исполнитель присылает исправленный payload. GUARDRAIL: заново
    проверяем его теми же правилами качества, что отправили запись в
    карантин — если нарушение осталось, процесс переводится в
    'awaiting_task' СНОВА (новая попытка), Bronze НЕ трогается, и явно
    возвращается список оставшихся нарушений (не молчаливый провал)."""
    process = store.get_process_instance(process_id)
    if not process:
        raise ProcessError(f"Процесс #{process_id} не найден")
    if process["process_type"] != PROCESS_TYPE:
        raise ProcessError(f"Процесс #{process_id} не является '{PROCESS_TYPE}'")
    if process["status"] != "awaiting_task":
        raise ProcessError(
            f"Процесс #{process_id} в статусе {process['status']!r} — "
            "исправление можно подать только в статусе 'awaiting_task'")

    dataset_id = process["context"]["dataset_id"]
    errors = evaluate_payload(store, dataset_id, corrected_payload)
    if errors:
        context = dict(process["context"])
        context["last_attempt_payload"] = corrected_payload
        context["last_attempt_errors"] = errors
        store.set_process_status(process_id, "awaiting_task", context=context)
        store.log_audit(actor, "correction_rejected", "process_instance", process_id,
                        {"errors": errors})
        return {"accepted": False, "errors": errors, "process": store.get_process_instance(process_id)}

    bronze_record_id = process["context"]["bronze_record_id"]
    store.update_bronze_payload(bronze_record_id, corrected_payload)

    tasks = store.list_tasks(process_instance_id=process_id, status="open")
    for t in tasks:
        store.complete_task(t["id"], result={"corrected_payload": corrected_payload})

    quarantine_id = process["subject_id"]
    store.resolve_quarantine(quarantine_id, f"исправлено процессом #{process_id} ({actor})")

    context = dict(process["context"])
    context["corrected_payload"] = corrected_payload
    store.set_process_status(process_id, "corrected", context=context)
    store.log_audit(actor, "correction_accepted", "process_instance", process_id,
                    {"bronze_record_id": bronze_record_id})
    return {"accepted": True, "errors": [], "process": store.get_process_instance(process_id)}


def write_back_correction(store: Store, process_id: int, connector: Connector,
                          source_id: int, dataset_name: str, natural_key: str,
                          actor: str) -> dict[str, Any]:
    """Пишет исправленную запись обратно в источник — завершающий шаг
    K3 ("...корректировка -> write-back в источник"). Идемпотентно:
    повторный вызов с тем же (process_id, natural_key) не отправляет
    запись в источник дважды (см. `Store.write_back_log_attempt`).

    Проверка идемпотентности выполняется ПЕРВОЙ, до проверки статуса
    процесса: после успешного write-back процесс переходит в
    'completed', и если бы статус проверялся раньше идемпотентности,
    легитимный повторный вызов (например, при ретрае после потери
    ответа сети) получил бы ошибку вместо честного "уже сделано"."""
    process = store.get_process_instance(process_id)
    if not process:
        raise ProcessError(f"Процесс #{process_id} не найден")

    idempotency_key = f"{PROCESS_TYPE}:{process_id}:{natural_key}"
    log_id, is_new = store.write_back_log_attempt(
        process_id, source_id, dataset_name, natural_key, idempotency_key)
    if not is_new:
        existing = store.get_write_back_log(log_id)
        return {"ok": existing["status"] == "ok", "skipped_duplicate": True,
               "log": existing, "process": process}

    if process["status"] != "corrected":
        raise ProcessError(
            f"Процесс #{process_id} в статусе {process['status']!r} — "
            "write-back возможен только после статуса 'corrected'")

    store.set_process_status(process_id, "write_back_pending")
    payload = process["context"]["corrected_payload"]
    record = WriteRecord(natural_key=natural_key, payload=payload,
                         idempotency_key=idempotency_key)
    try:
        result = connector.write_back(dataset_name, [record])
    except Exception as exc:  # noqa: BLE001 - любой сбой коннектора фиксируем, не роняем процесс
        store.write_back_mark_result(log_id, "error", error=str(exc))
        store.set_process_status(process_id, "failed")
        store.log_audit(actor, "write_back_failed", "process_instance", process_id,
                        {"error": str(exc)})
        raise

    if result.ok:
        store.write_back_mark_result(log_id, "ok")
        store.set_process_status(process_id, "completed")
        store.log_audit(actor, "write_back_ok", "process_instance", process_id,
                        {"natural_key": natural_key, "written": result.written})
    else:
        store.write_back_mark_result(log_id, "error", error="; ".join(result.errors))
        store.set_process_status(process_id, "failed")
        store.log_audit(actor, "write_back_failed", "process_instance", process_id,
                        {"errors": result.errors})

    return {"ok": result.ok, "skipped_duplicate": False,
           "log": store.get_write_back_log(log_id),
           "process": store.get_process_instance(process_id)}


def rollback_process(store: Store, process_id: int, actor: str,
                     reason: str = "") -> dict[str, Any]:
    """Отменяет процесс ДО успешного write-back. После успешного
    write-back откат средствами платформы невозможен (источник уже
    изменён) — то же ограничение, что и в `ProcurementAgent.
    rollback_proposal` для отправленных в 1С заказов."""
    process = store.get_process_instance(process_id)
    if not process:
        raise ProcessError(f"Процесс #{process_id} не найден")
    if process["status"] == "completed":
        wbl = store.list_write_back_log(process_instance_id=process_id, status="ok")
        if wbl:
            raise ProcessError(
                f"Процесс #{process_id} уже завершён успешным write-back "
                f"(запись #{wbl[0]['id']}) — откат невозможен, требуется "
                "ручная корректировка в источнике")
    if process["status"] == "cancelled":
        raise ProcessError(f"Процесс #{process_id} уже отменён")

    for t in store.list_tasks(process_instance_id=process_id, status="open"):
        store.cancel_task(t["id"])
    store.set_process_status(process_id, "cancelled")
    store.log_audit(actor, "rollback_process", "process_instance", process_id,
                    {"reason": reason})
    return store.get_process_instance(process_id)
